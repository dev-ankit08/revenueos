"""Nurture flows: a series of marketing emails that goes to many contacts, approved once per step.

Each step is a template (an Approval of kind "nurture_email") that a person approves in the Approvals inbox.
Templates may use merge fields {{first_name}}, {{last_name}}, {{company}} and {{title}}. For every member,
the engine sends the next approved step when it is due (days after the previous one, inside business hours
in the contact's time zone). Each individual send is a normal email draft that goes through every guardrail
(suppression, sensitive content...) before it is sent, and is recorded like any other email.

Members leave the flow when they become an MQL (or later), reply, opt out, bounce or are disqualified:
from then on a person or a sequence takes over.
"""
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Approval, Contact, NurtureFlow, NurtureMember, NurtureStep
from . import approvals as approvals_svc
from . import guardrails, lifecycle, sequences

MERGE_FIELDS = ("first_name", "last_name", "company", "title")
_MERGE = re.compile(r"\{\{\s*(first_name|last_name|company|title)\s*\}\}")
EXIT_STATUSES = ("replied", "meeting", "disqualified")


def _aware(dt):
    return None if dt is None else (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))


def merge(text: str, c: Contact) -> str:
    values = {"first_name": c.first_name, "last_name": c.last_name, "company": c.account.name, "title": c.title or ""}
    return _MERGE.sub(lambda m: values[m.group(1)] or "", text or "")


def add_step(db: Session, flow: NurtureFlow, *, subject: str, body: str, delay_days: int, created_by: str) -> NurtureStep:
    position = 1 + max((s.position for s in flow.steps), default=0)
    template = approvals_svc.queue(db, kind="nurture_email", subject=subject, body=body, created_by_agent=created_by,
                                   campaign_id=flow.campaign_id, title=f"{flow.name}: step {position}",
                                   rationale=f"Nurture step {position}, sent {delay_days} days after the previous one.")
    step = NurtureStep(flow_id=flow.id, position=position, delay_days=delay_days, approval_id=template.id)
    db.add(step)
    db.flush()
    return step


def enroll(db: Session, flow: NurtureFlow, contact_ids: list[int]) -> dict:
    added = skipped = 0
    have = {m.contact_id for m in flow.members}
    for cid in contact_ids:
        c = db.get(Contact, cid)
        if (not c or cid in have or not c.email or c.status in EXIT_STATUSES or c.experiment_group == "holdout"
                or guardrails.is_suppressed(db, c.email)):
            skipped += 1
            continue
        db.add(NurtureMember(flow_id=flow.id, contact_id=cid))
        have.add(cid)
        added += 1
    return {"enrolled": added, "skipped": skipped}


def _exit_reason(db: Session, c: Contact) -> str | None:
    if c.status in EXIT_STATUSES:
        return f"contact {c.status}"
    if c.lifecycle and lifecycle.STAGES.index(c.lifecycle) >= lifecycle.STAGES.index("mql"):
        return f"became {c.lifecycle.upper()}: sales takes over"
    if guardrails.is_suppressed(db, c.email):
        return "suppressed"
    return None


def advance_all(db: Session, now: datetime | None = None) -> int:
    """Send every member's next step that is due. Returns emails sent."""
    now = _aware(now) or datetime.now(timezone.utc)
    sent = 0
    flows = db.scalars(select(NurtureFlow).where(NurtureFlow.status == "active")).all()
    for flow in flows:
        steps = sorted(flow.steps, key=lambda s: s.position)
        for m in [m for m in flow.members if m.status == "active"]:
            sent += _advance(db, flow, steps, m, now)
    return sent


def _advance(db: Session, flow: NurtureFlow, steps: list[NurtureStep], m: NurtureMember, now: datetime) -> int:
    c = m.contact
    if reason := _exit_reason(db, c):
        m.status, m.exit_reason, m.finished_at = "exited", reason, now
        return 0
    if m.current_position >= len(steps):
        m.status, m.finished_at = "completed", now
        return 0
    step = steps[m.current_position]
    template = db.get(Approval, step.approval_id)
    if not template or template.status != "approved":
        return 0  # waiting for a person to approve this step's template
    base = (_aware(m.last_sent_at) or _aware(m.enrolled_at)) + timedelta(days=step.delay_days)
    due = sequences.next_send_time(base, sequences.contact_tz(c))
    if now < due:
        return 0
    draft = approvals_svc.queue(db, kind="email", contact=c, subject=merge(template.subject, c),
                                body=merge(template.body, c), created_by_agent="nurture",
                                campaign_id=flow.campaign_id,
                                rationale=f"{flow.name} step {step.position} (template approval {template.id})")
    if any(f["severity"] == "block" for f in draft.guardrail_flags):
        draft.status, draft.review_note = "rejected", "Nurture send blocked by guardrails."
        m.status, m.exit_reason, m.finished_at = "exited", "blocked: " + "; ".join(
            f["message"] for f in draft.guardrail_flags if f["severity"] == "block"), now
        return 0
    draft.status, draft.reviewed_by, draft.decided_at = "approved", template.reviewed_by, now
    db.flush()
    try:
        approvals_svc.claim_and_send(db, draft, from_status="approved", commit=True)
    except approvals_svc.ApprovalError as exc:  # daily cap: try again next tick
        draft.status, draft.review_note = "rejected", f"Not sent: {exc}"
        return 0
    if draft.status != "sent":
        return 0
    m.current_position += 1
    m.last_sent_at = draft.sent_at
    if m.current_position >= len(steps):
        m.status, m.finished_at = "completed", now
    return 1


def flow_stats(db: Session, flow: NurtureFlow) -> dict:
    statuses: dict[str, int] = {}
    for m in flow.members:
        statuses[m.status] = statuses.get(m.status, 0) + 1
    return {
        "id": flow.id, "name": flow.name, "status": flow.status, "campaign_id": flow.campaign_id,
        "members": statuses, "exited_to_sales": sum(1 for m in flow.members if (m.exit_reason or "").startswith("became")),
        "steps": [{"position": s.position, "delay_days": s.delay_days, "approval_id": s.approval_id,
                   "template_status": (db.get(Approval, s.approval_id).status if s.approval_id else None),
                   "subject": (db.get(Approval, s.approval_id).subject if s.approval_id else None)}
                  for s in sorted(flow.steps, key=lambda s: s.position)],
    }
