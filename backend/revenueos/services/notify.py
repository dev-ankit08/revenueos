"""Telling people things: hot replies to their owner, SLA breaches, failed sends and jobs (ops alerts),
and each rep's daily digest. Internal only: these never go to prospects.

Channels: a Slack DM when the bot can find the person by email (scope users:read.email), otherwise an internal
email (when sending is configured); ops alerts go to the Slack approvals channel and/or ALERT_EMAIL.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import AgentRun, Approval, Contact

log = logging.getLogger(__name__)


def _slack_dm(email: str, text: str) -> bool:
    from ..integrations import slack_bot

    if not slack_bot.enabled() or not email or "@" not in email:
        return False
    try:
        user = slack_bot.slack_call("users.lookupByEmail", {"email": email})
        uid = (user.get("user") or {}).get("id")
        if not uid:
            return False
        slack_bot.slack_call("chat.postMessage", {"channel": uid, "text": text})
        return True
    except Exception as exc:
        log.info("Slack DM to %s failed: %s", email, exc)
        return False


def tell(person: str | None, subject: str, text: str) -> str | None:
    """Notify one person (an owner's email). Returns the channel used, or None."""
    if not person:
        return None
    if _slack_dm(person, f"*{subject}*\n{text}"):
        return "slack"
    if "@" in person:
        from ..tools.email import send_internal

        try:
            if send_internal(person, subject, text) != "dry_run":
                return "email"
        except Exception as exc:
            log.warning("internal email to %s failed: %s", person, exc)
    return None


def alert(subject: str, text: str) -> None:
    """Ops alert to the Slack approvals channel and/or ALERT_EMAIL."""
    log.warning("ALERT %s: %s", subject, text)
    from ..integrations import slack_bot

    if slack_bot.enabled() and settings.slack_approvals_channel:
        try:
            slack_bot.slack_call("chat.postMessage", {"channel": settings.slack_approvals_channel,
                                                      "text": f":warning: *{subject}*\n{text}"})
        except Exception:
            log.exception("Slack alert failed")
    if settings.alert_email:
        from ..tools.email import send_internal

        try:
            send_internal(settings.alert_email, f"[RevenueOS] {subject}", text)
        except Exception:
            log.exception("alert email failed")


def hot_reply(contact: Contact, category: str, summary: str) -> str | None:
    link = f"{settings.app_base_url.rstrip('/')}/pipeline/{contact.id}" if settings.app_base_url else ""
    return tell(contact.owner, f"{contact.full_name} replied: {category.replace('_', ' ')}",
                f"{contact.full_name} ({contact.account.name}): {summary}\nA reply draft is waiting in Approvals. {link}".strip())


def failures_since(db: Session, since: datetime) -> dict:
    sends = db.scalars(select(Approval).where(Approval.status == "failed", Approval.decided_at >= since)).all()
    runs = db.scalars(select(AgentRun).where(AgentRun.status == "failed", AgentRun.finished_at >= since,
                                             AgentRun.parent_run_id.is_(None))).all()
    return {"sends": sends, "runs": runs}


def alert_failures(db: Session, since: datetime) -> int:
    f = failures_since(db, since)
    if not f["sends"] and not f["runs"]:
        return 0
    lines = [f"- Email to {a.contact.full_name if a.contact else '?'} failed: {a.review_note}" for a in f["sends"][:10]]
    lines += [f"- {r.agent} run {r.id} failed: {(r.error or '')[:200]}" for r in f["runs"][:10]]
    alert(f"{len(f['sends'])} failed sends, {len(f['runs'])} failed jobs", "\n".join(lines))
    return len(f["sends"]) + len(f["runs"])


def sla_alerts(db: Session) -> int:
    """Tell each owner (once) about MQLs waiting past the response-time SLA."""
    from . import lifecycle

    n = 0
    for b in lifecycle.sla_breaches(db):
        c = db.get(Contact, b["contact_id"])
        if c.sla_alerted_at is not None:
            continue
        c.sla_alerted_at = datetime.now(timezone.utc)
        tell(c.owner, f"{c.full_name} has waited {b['hours_waiting']}h",
             f"{c.full_name} ({c.account.name}) became an {(c.lifecycle or 'mql').upper()} and has had no reply from us "
             f"in {b['hours_waiting']} hours (target: {settings.mql_sla_hours}h).")
        n += 1
    return n


def digest_for(db: Session, owner: str) -> str | None:
    """A rep's day in a few lines (None when there is nothing for them)."""
    from ..models import Task

    now = datetime.now(timezone.utc)
    end = now.replace(hour=23, minute=59)
    tasks = db.scalars(select(Task).where(Task.status == "open", Task.owner == owner, Task.due_at.is_not(None),
                                          Task.due_at <= end)).all()
    drafts = [a for a in db.scalars(select(Approval).where(Approval.status == "pending"))
              if a.contact and a.contact.owner == owner]
    from . import lifecycle

    breaches = [b for b in lifecycle.sla_breaches(db, now) if b["owner"] == owner]
    if not tasks and not drafts and not breaches:
        return None
    lines = [f"Good morning. Your RevenueOS day:"]
    if breaches:
        lines.append(f"- {len(breaches)} lead(s) waiting past the response-time target: "
                     + ", ".join(b["name"] for b in breaches[:5]))
    if tasks:
        lines.append(f"- {len(tasks)} task(s) due: " + "; ".join(t.title for t in tasks[:5]))
    if drafts:
        lines.append(f"- {len(drafts)} draft(s) to review in Approvals")
    if settings.app_base_url:
        lines.append(f"{settings.app_base_url.rstrip('/')}/today")
    return "\n".join(lines)


def send_digests(db: Session) -> int:
    owners = {c for (c,) in db.execute(select(Contact.owner).where(Contact.owner.is_not(None)).distinct())}
    sent = 0
    for owner in sorted(owners):
        text = digest_for(db, owner)
        if text and tell(owner, "Your RevenueOS day", text):
            sent += 1
    return sent


def due_for_digest(last_day: str | None, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    from zoneinfo import ZoneInfo

    try:
        local = now.astimezone(ZoneInfo(settings.default_timezone or "UTC"))
    except Exception:
        local = now
    return int(settings.digest_hour) >= 0 and local.hour >= int(settings.digest_hour) and last_day != local.date().isoformat()


def local_today() -> str:
    from zoneinfo import ZoneInfo

    try:
        return datetime.now(ZoneInfo(settings.default_timezone or "UTC")).date().isoformat()
    except Exception:
        return datetime.now(timezone.utc).date().isoformat()


def recent(minutes: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)
