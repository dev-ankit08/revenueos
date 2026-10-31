"""The approval queue: the only path by which anything leaves the building."""
import random
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Activity, Approval, Contact
from ..tools.crm import get_crm
from ..tools.email import send_email
from . import guardrails, memory


class ApprovalError(Exception):
    pass


def queue(
    db: Session,
    *,
    kind: str,
    body: str,
    created_by_agent: str,
    contact: Contact | None = None,
    subject: str | None = None,
    sequence_step: int = 1,
    send_after_days: int = 0,
    variant: str = "A",
    rationale: str | None = None,
    run_id: int | None = None,
    grounding_extra: str = "",
    content_type: str | None = None,
    title: str | None = None,
    campaign_id: int | None = None,
    account_id: int | None = None,
    claims: list[dict] | None = None,
) -> Approval:
    grounding = guardrails.grounding_text(db, grounding_extra)
    flags = guardrails.check_draft(db, contact, kind, subject, body, grounding)
    if claims is not None:  # only agents that cite sources pass claims (possibly empty)
        flags += guardrails.check_claims(db, claims, body, grounding)
    approval = Approval(
        kind=kind, contact_id=contact.id if contact else None, subject=subject, body=body,
        sequence_step=sequence_step, send_after_days=send_after_days, variant=variant,
        rationale=rationale, guardrail_flags=flags, created_by_agent=created_by_agent, run_id=run_id,
        content_type=content_type, title=title, campaign_id=campaign_id, claims=claims,
        account_id=account_id if account_id is not None else (contact.account_id if contact else None),
    )
    db.add(approval)
    db.flush()
    if contact and kind == "email" and variant in ("A", "B"):
        _assign_ab_variant(db, approval)
    # Posted to the Slack approvals channel once committed (integrations/slack_bot.py).
    db.info.setdefault("new_approvals", []).append(approval.id)
    return approval


AB_HOLDOUT_NOTE = "A/B test: this contact was randomly assigned the other variant."


def _assign_ab_variant(db: Session, approval: Approval) -> None:
    """Each contact gets exactly one variant of an A/B step, chosen at random, so results are unbiased."""
    sibling = db.scalar(select(Approval).where(
        Approval.contact_id == approval.contact_id, Approval.sequence_step == approval.sequence_step,
        Approval.kind == "email", Approval.status == "pending", Approval.id != approval.id,
        Approval.variant.in_(["A", "B"]), Approval.variant != approval.variant,
    ))
    if not sibling:
        return
    loser = random.choice([approval, sibling])
    loser.status, loser.review_note = "rejected", AB_HOLDOUT_NOTE
    loser.decided_at = datetime.now(timezone.utc)


def reject(db: Session, approval: Approval, reviewer: str, note: str | None) -> Approval:
    if approval.status != "pending":
        raise ApprovalError(f"Approval is already {approval.status}.")
    approval.status = "rejected"
    approval.reviewed_by, approval.review_note = reviewer, note
    approval.decided_at = datetime.now(timezone.utc)
    return approval


def approve_and_send(
    db: Session, approval: Approval, reviewer: str,
    subject: str | None = None, body: str | None = None,
) -> Approval:
    """Apply reviewer edits, re-run guardrails, then send (emails) or mark approved (other kinds)."""
    if approval.status != "pending":
        raise ApprovalError(f"Approval is already {approval.status}.")
    if subject is not None:
        approval.subject = subject
    if body is not None:
        approval.body = body

    contact = approval.contact
    flags = guardrails.check_draft(
        db, contact, approval.kind, approval.subject, approval.body, guardrails.grounding_text(db)
    )
    approval.guardrail_flags = flags
    blocking = [f["message"] for f in flags if f["severity"] == "block"]
    if blocking:
        raise ApprovalError("Blocked: " + " ".join(blocking))

    approval.reviewed_by = reviewer
    approval.decided_at = datetime.now(timezone.utc)

    if approval.kind != "email":
        # LinkedIn messages and marketing content are published by a person (respects
        # LinkedIn's terms and keeps a human on every public post); approval marks them ready.
        approval.status = "approved"
        return approval

    if approval.send_after_days > 0:
        # Follow-up steps are approved now and released by the scheduler later.
        approval.status = "approved"
        return approval

    return _send(db, approval)


def _send(db: Session, approval: Approval) -> Approval:
    contact = approval.contact
    cap_error = guardrails.send_capacity_error(db, contact.email)
    if cap_error:
        raise ApprovalError(cap_error)
    try:
        transport = send_email(contact.email, approval.subject or "", approval.body + guardrails.compliance_footer())
    except Exception as exc:  # SMTP failure: keep the draft, surface the error
        approval.status = "failed"
        approval.review_note = f"Send failed: {exc}"
        return approval

    approval.status = "sent"
    approval.sent_at = datetime.now(timezone.utc)
    memory.record(db, source="email", source_ref=f"email-sent:{approval.id}", contact_id=contact.id,
                  account_id=contact.account_id, occurred_at=approval.sent_at, title=approval.subject,
                  raw_text=approval.body, summary=f"We emailed {contact.full_name}: \"{approval.subject}\"",
                  processed=True)
    if contact.status in ("new", "researched", "sequenced"):
        contact.status = "contacted"
    db.add(Activity(contact_id=contact.id, type="email_sent",
                    detail=f"[{transport}] {approval.subject}"))
    crm = get_crm()
    crm.log_activity(contact, f"Email: {approval.subject}", approval.body)
    crm.update_lead_status(contact, contact.status, contact.score)
    return approval


def release_due_followups(db: Session) -> int:
    """Send approved follow-ups whose delay has elapsed, unless the contact replied or was suppressed."""
    now = datetime.now(timezone.utc)
    sent = 0
    for a in db.scalars(select(Approval).where(Approval.status == "approved", Approval.kind == "email")):
        decided = a.decided_at if a.decided_at.tzinfo else a.decided_at.replace(tzinfo=timezone.utc)
        if (now - decided).days < a.send_after_days:
            continue
        if a.contact.status in ("replied", "meeting", "disqualified"):
            a.status = "rejected"
            a.review_note = f"Auto-cancelled: contact is {a.contact.status}."
            continue
        try:
            _send(db, a)
            sent += a.status == "sent"
        except ApprovalError as exc:
            a.review_note = str(exc)
    return sent
