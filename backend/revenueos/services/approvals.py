"""The approval queue: the only path by which anything leaves the building."""
import difflib
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..models import Activity, Approval, AuditEvent, Contact
from ..tools.crm import get_crm
from ..tools.email import send_email
from . import guardrails, memory


class ApprovalError(Exception):
    pass


# Why a reviewer rejected a draft. Structured so the learning loop and Insights can use it.
REJECT_REASONS = {
    "wrong_facts": "Wrong or unverifiable facts",
    "wrong_person": "Wrong person or company",
    "tone": "Tone or brand voice",
    "too_generic": "Too generic",
    "too_long": "Too long",
    "timing": "Bad timing",
    "other": "Other",
}

# Flags raised outside check_draft/check_claims (e.g. by the personalisation gate in agents/writer.py).
# The re-check at approval cannot recompute them, so they are carried over instead of being dropped.
STICKY_FLAG_CODES = {"low_personalisation", "not_graded", "grader_unsupported"}


def audit(db: Session, actor: str, action: str, object_type: str | None = None,
          object_id: object = None, **detail) -> None:
    db.add(AuditEvent(actor=actor or "unknown", action=action, object_type=object_type,
                      object_id=str(object_id) if object_id is not None else None, detail=detail or None))


def edit_percentage(before: str, after: str) -> int:
    """0 = untouched, 100 = completely rewritten."""
    if before == after:
        return 0
    return round((1 - difflib.SequenceMatcher(None, before, after, autojunk=False).ratio()) * 100) or 1


def recheck(db: Session, approval: Approval) -> list[dict]:
    """Re-run every automatic check against the same evidence the draft was queued with."""
    grounding = guardrails.grounding_text(db, approval.grounding_extra or "")
    flags = guardrails.check_draft(db, approval.contact, approval.kind, approval.subject, approval.body, grounding)
    if approval.claims is not None:
        flags += guardrails.check_claims(db, approval.claims, approval.body, grounding)
    have = {f["code"] for f in flags}
    flags += [f for f in (approval.guardrail_flags or [])
              if f.get("code") in STICKY_FLAG_CODES and f.get("code") not in have]
    return flags


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
    enroll: bool = False,
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
        grounding_extra=grounding_extra or None, prompt_version=_run_prompt_version(db, run_id),
        account_id=account_id if account_id is not None else (contact.account_id if contact else None),
    )
    db.add(approval)
    db.flush()
    if enroll and contact and kind in ("email", "linkedin_message"):
        from . import sequences

        sequences.attach(db, approval, created_by_agent)
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


def reject(db: Session, approval: Approval, reviewer: str, note: str | None,
           reason: str | None = None) -> Approval:
    if approval.status != "pending":
        raise ApprovalError(f"Approval is already {approval.status}.")
    if reason is not None and reason not in REJECT_REASONS:
        raise ApprovalError(f"Unknown reject reason '{reason}'. Use one of: {', '.join(REJECT_REASONS)}.")
    approval.status = "rejected"
    approval.reviewed_by, approval.review_note, approval.reject_reason = reviewer, note, reason
    approval.decided_at = datetime.now(timezone.utc)
    audit(db, reviewer, "approval.reject", "approval", approval.id, reason=reason, note=note)
    return approval


def approve_and_send(
    db: Session, approval: Approval, reviewer: str,
    subject: str | None = None, body: str | None = None,
) -> Approval:
    """Apply reviewer edits, re-run guardrails, then send (emails) or mark approved (other kinds)."""
    if approval.status != "pending":
        raise ApprovalError(f"Approval is already {approval.status}.")
    new_subject = approval.subject if subject is None else subject
    new_body = approval.body if body is None else body
    if (new_subject or "") != (approval.subject or "") or new_body != approval.body:
        if approval.ai_body is None:  # keep the AI's first wording, even across several edit attempts
            approval.ai_subject, approval.ai_body = approval.subject, approval.body
        approval.subject, approval.body = new_subject, new_body

    flags = recheck(db, approval)
    approval.guardrail_flags = flags
    blocking = [f["message"] for f in flags if f["severity"] == "block"]
    if blocking:
        raise ApprovalError("Blocked: " + " ".join(blocking))

    if approval.ai_body is None:
        approval.edit_pct = 0
    else:
        approval.edit_pct = edit_percentage(f"{approval.ai_subject or ''}\n{approval.ai_body}",
                                            f"{approval.subject or ''}\n{approval.body}")
    approval.reviewed_by = reviewer
    approval.decided_at = datetime.now(timezone.utc)
    audit(db, reviewer, "approval.approve", "approval", approval.id, edit_pct=approval.edit_pct)

    if approval.kind != "email":
        # LinkedIn messages and marketing content are published by a person (respects
        # LinkedIn's terms and keeps a human on every public post); approval marks them ready.
        if approval.kind == "content":
            from . import utm

            approval.body = utm.tag_links(approval.body, campaign=_utm_campaign(db, approval),
                                          medium=approval.content_type or "content", content=f"approval-{approval.id}")
        approval.status = "approved"
        return approval

    if approval.send_after_days > 0:
        # Follow-up steps are approved now and released by the scheduler later.
        approval.status = "approved"
        return approval

    if not claim_and_send(db, approval, from_status="pending"):
        raise ApprovalError("Someone else is already sending this draft.")
    return approval


def claim_and_send(db: Session, approval: Approval, *, from_status: str, commit: bool = False) -> bool:
    """Atomically move the draft to "sending", then send it. False if another worker got there first.

    The status flip is one conditional UPDATE, so two workers (or two clicks) can never both send.
    With commit=True (scheduler) the claim is committed before sending: if the process dies mid-send the
    draft stays "sending" and is marked failed for a person to check, never silently sent twice.
    """
    cap_error = guardrails.send_capacity_error(db, approval.contact.email)
    if cap_error:
        raise ApprovalError(cap_error)
    now = datetime.now(timezone.utc)
    res = db.execute(
        update(Approval).where(Approval.id == approval.id, Approval.status == from_status)
        .values(status="sending", claimed_at=now).execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        db.refresh(approval)
        return False
    approval.status, approval.claimed_at = "sending", now
    if commit:
        db.commit()
    _send(db, approval)
    return True


INTERRUPTED_AFTER = timedelta(minutes=15)


def sweep_interrupted_sends(db: Session) -> int:
    """Drafts stuck in "sending" (the process died mid-send) become "failed" for a person to check.
    They are never retried automatically: the email may already have gone out."""
    cutoff = datetime.now(timezone.utc) - INTERRUPTED_AFTER
    n = 0
    for a in db.scalars(select(Approval).where(Approval.status == "sending")):
        claimed = a.claimed_at
        if claimed is not None and claimed.tzinfo is None:
            claimed = claimed.replace(tzinfo=timezone.utc)
        if claimed is None or claimed < cutoff:
            a.status = "failed"
            a.review_note = "Interrupted while sending. Check the sent folder before approving a resend."
            n += 1
    return n


def _send(db: Session, approval: Approval) -> Approval:
    """Deliver a claimed ("sending") email. Callers go through claim_and_send."""
    contact = approval.contact
    from . import utm

    body = utm.tag_links(approval.body, campaign=_utm_campaign(db, approval), medium="email",
                         content=f"approval-{approval.id}")
    try:
        result = send_email(contact.email, approval.subject or "", body + guardrails.compliance_footer(contact.email),
                            approval_id=approval.id)
    except Exception as exc:  # transport failure: keep the draft, surface the error
        approval.status = "failed"
        approval.review_note = f"Send failed: {exc}"
        return approval

    transport = result.transport
    approval.provider, approval.provider_message_id = result.transport, result.message_id
    approval.status = "sent"
    approval.sent_at = datetime.now(timezone.utc)
    if approval.enrollment_id:
        from ..models import Enrollment

        enrollment = db.get(Enrollment, approval.enrollment_id)
        if enrollment and enrollment.started_at is None:
            enrollment.started_at = approval.sent_at
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
    """Send what is due: sequence steps (services/sequences.py: timing, send windows, pauses), plus
    approved follow-ups queued before sequences existed (no enrollment), unless the contact replied."""
    from . import sequences

    now = datetime.now(timezone.utc)
    sent = 0
    legacy = db.scalars(select(Approval).where(Approval.status == "approved", Approval.kind == "email",
                                               Approval.enrollment_id.is_(None))).all()
    for a in legacy:
        decided = a.decided_at if a.decided_at.tzinfo else a.decided_at.replace(tzinfo=timezone.utc)
        if (now - decided).days < a.send_after_days:
            continue
        if a.contact.status in ("replied", "meeting", "disqualified"):
            a.status = "rejected"
            a.review_note = f"Auto-cancelled: contact is {a.contact.status}."
            continue
        if now < sequences.next_send_time(now, sequences.contact_tz(a.contact)):
            continue  # outside business hours for this contact
        try:
            claim_and_send(db, a, from_status="approved", commit=True)
            sent += a.status == "sent"
        except ApprovalError as exc:
            a.review_note = str(exc)
    return sent + sequences.advance_all(db, now)


def _utm_campaign(db: Session, approval: Approval) -> str:
    if approval.campaign_id:
        from ..models import Campaign

        camp = db.get(Campaign, approval.campaign_id)
        if camp:
            return camp.name
    return "sequence" if approval.enrollment_id else (approval.created_by_agent or "outreach")


def _run_prompt_version(db: Session, run_id: int | None) -> str | None:
    if not run_id:
        return None
    from ..models import AgentRun

    run = db.get(AgentRun, run_id)
    return run.prompt_version if run else None
