"""Deliverability Guardian: keep cold email out of spam and protect the company's domains.

The #1 reason teams abandon AI outreach tools is burned sender reputation. This module:

- spreads sends over a POOL of mailboxes (SendingMailbox), each with a daily cap (default 40, the commonly cited
  safe ceiling for cold email per mailbox) and a minimum gap between automated sends, so nothing goes out in bursts;
- WARMS UP new mailboxes: 5 a day on day one, rising to the full cap over WARMUP_DAYS (default 21);
- watches each mailbox's HEALTH from delivery events (bounces, spam complaints) and pauses it automatically when it
  crosses the thresholds mailbox providers enforce (Gmail/Yahoo: keep complaints under 0.3%, aim for 0.1%);
- checks the sending domain's DNS (SPF, DKIM, DMARC, MX) with plain-English fixes and a 0-100 score;
- runs INBOX PLACEMENT tests: a test email from each mailbox to the team's own connected Gmail/Outlook
  mailboxes, then reads back whether it landed in the inbox, promotions or spam.

With no mailboxes configured, sending works exactly as before (one sender, SENDER_EMAIL).
"""
import logging
import random
import secrets as pysecrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Approval, Connection, EmailEvent, PlacementTest, SendingMailbox

log = logging.getLogger(__name__)
UTC = timezone.utc

WARMUP_START = 5
COMPLAINT_PAUSE_RATE = 0.001   # 0.1%
BOUNCE_PAUSE_RATE = 0.02       # 2%
MIN_SENDS_FOR_RATES = 100      # below this, absolute counts decide
ABS_COMPLAINTS_PAUSE = 2       # 2+ spam complaints in 7 days pauses a small mailbox
ABS_BOUNCES_PAUSE = 5          # 5+ hard bounces in 7 days pauses a small mailbox


class NoSendingCapacity(Exception):
    """Every mailbox is paused, at today's cap, or sent too recently."""


def _aware(dt):
    return None if dt is None else (dt if dt.tzinfo else dt.replace(tzinfo=UTC))


# ------------------------------------------------------------------ caps and selection

def warmup_cap(mb: SendingMailbox, now: datetime | None = None) -> int:
    """Today's limit: ramps linearly from WARMUP_START to daily_cap over warmup_days."""
    now = now or datetime.now(UTC)
    started = _aware(mb.warmup_started_at)
    if not started or not mb.warmup_days:
        return mb.daily_cap
    day = max(0, (now - started).days)
    if day >= mb.warmup_days:
        return mb.daily_cap
    return min(mb.daily_cap, WARMUP_START + round((mb.daily_cap - WARMUP_START) * day / mb.warmup_days))


def sent_last_24h(db: Session, mailbox_id: int, now: datetime | None = None) -> int:
    since = (now or datetime.now(UTC)) - timedelta(days=1)
    return db.scalar(select(func.count()).select_from(Approval).where(
        Approval.mailbox_id == mailbox_id, Approval.status.in_(["sent", "sending"]), Approval.claimed_at >= since)) or 0


def _last_send(db: Session, mailbox_id: int):
    return _aware(db.scalar(select(func.max(Approval.claimed_at)).where(Approval.mailbox_id == mailbox_id)))


def has_pool(db: Session) -> bool:
    return db.scalar(select(SendingMailbox.id).where(SendingMailbox.status != "removed")) is not None


def pick_mailbox(db: Session, *, automated: bool, now: datetime | None = None) -> SendingMailbox | None:
    """The mailbox to send from, or None when no pool is configured (legacy single sender).

    Chooses the active mailbox with the most room left today. Automated sends (sequences, nurture, scheduled
    follow-ups) also respect a minimum gap per mailbox, with jitter, so they never go out in bursts. A person
    clicking "Approve & send" is not delayed by the gap but still respects the daily cap.
    """
    now = now or datetime.now(UTC)
    pool = list(db.scalars(select(SendingMailbox).where(SendingMailbox.status.in_(["active", "warming"]))))
    if not pool:
        if has_pool(db):
            raise NoSendingCapacity("Every sending mailbox is paused. Check Deliverability.")
        return None
    gap = int(settings.mailbox_min_gap_seconds)
    best, best_room = None, 0
    for mb in pool:
        room = warmup_cap(mb, now) - sent_last_24h(db, mb.id, now)
        if room <= 0:
            continue
        if automated and gap:
            last = _last_send(db, mb.id)
            jitter = random.uniform(0, gap * 0.5)
            if last and (now - last).total_seconds() < gap + jitter:
                continue
        if room > best_room:
            best, best_room = mb, room
    if best is None:
        raise NoSendingCapacity("All sending mailboxes have reached today's limit or sent moments ago; "
                                "this email goes out on the next run when a mailbox has room.")
    return best


# ------------------------------------------------------------------ health

def health(db: Session, mb: SendingMailbox, days: int = 30, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    since = now - timedelta(days=days)
    sent = db.scalar(select(func.count()).select_from(Approval).where(
        Approval.mailbox_id == mb.id, Approval.status == "sent", Approval.sent_at >= since)) or 0

    def events(kind: str, window: datetime) -> int:
        return db.scalar(select(func.count()).select_from(EmailEvent).join(Approval, Approval.id == EmailEvent.approval_id)
                         .where(Approval.mailbox_id == mb.id, EmailEvent.event == kind,
                                EmailEvent.occurred_at >= window)) or 0

    bounces, complaints = events("bounce", since), events("complaint", since)
    week = now - timedelta(days=7)
    return {
        "sent_30d": sent, "bounces_30d": bounces, "complaints_30d": complaints,
        "bounce_rate": round(bounces / sent, 4) if sent else None,
        "complaint_rate": round(complaints / sent, 5) if sent else None,
        "bounces_7d": events("bounce", week), "complaints_7d": events("complaint", week),
        "today_cap": warmup_cap(mb, now), "sent_24h": sent_last_24h(db, mb.id, now),
    }


def evaluate(db: Session, mb: SendingMailbox, now: datetime | None = None) -> str | None:
    """Pause the mailbox when it crosses a threshold. Returns the reason, or None if it is healthy."""
    if mb.status not in ("active", "warming"):
        return None
    h = health(db, mb, now=now)
    reason = None
    if h["sent_30d"] >= MIN_SENDS_FOR_RATES:
        if (h["complaint_rate"] or 0) > COMPLAINT_PAUSE_RATE:
            reason = f"spam complaints at {h['complaint_rate']:.2%} (limit 0.1%)"
        elif (h["bounce_rate"] or 0) > BOUNCE_PAUSE_RATE:
            reason = f"bounces at {h['bounce_rate']:.1%} (limit 2%)"
    if not reason and h["complaints_7d"] >= ABS_COMPLAINTS_PAUSE:
        reason = f"{h['complaints_7d']} spam complaints in 7 days"
    if not reason and h["bounces_7d"] >= ABS_BOUNCES_PAUSE:
        reason = f"{h['bounces_7d']} hard bounces in 7 days"
    if reason:
        mb.status, mb.paused_reason, mb.paused_at = "paused", f"Paused automatically: {reason}.", now or datetime.now(UTC)
        from . import notify

        notify.alert(f"Sending mailbox {mb.address} paused",
                     f"{mb.address} was paused to protect your domain: {reason}. Check the list quality and "
                     "the recent messages, then resume it on the Deliverability page.")
        if mb.owner:
            notify.tell(mb.owner, f"Your sending mailbox {mb.address} was paused", mb.paused_reason)
    return reason


def evaluate_all(db: Session) -> int:
    return sum(1 for mb in db.scalars(select(SendingMailbox).where(SendingMailbox.status.in_(["active", "warming"])))
               if evaluate(db, mb))


def refresh_warmup_status(db: Session, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    for mb in db.scalars(select(SendingMailbox).where(SendingMailbox.status == "warming")):
        if warmup_cap(mb, now) >= mb.daily_cap:
            mb.status = "active"


# ------------------------------------------------------------------ DNS readiness (DNS-over-HTTPS, no extra library)

DKIM_SELECTORS = ("google", "selector1", "selector2", "s1", "s2", "k1", "k2", "default", "dkim", "mail", "smtp",
                  "pm", "mandrill", "mxvault", "sig1", "everlytickey1", "zoho")


def _txt(name: str) -> list[str]:
    """TXT records for a name via DNS-over-HTTPS (Google, falling back to Cloudflare)."""
    from ..integrations import http

    for url, headers in (("https://dns.google/resolve", {}),
                         ("https://cloudflare-dns.com/dns-query", {"Accept": "application/dns-json"})):
        try:
            data = http.request("GET", url, params={"name": name, "type": "TXT"}, headers=headers, timeout=10)
        except Exception:
            continue
        return ["".join(part.strip('"') for part in str(a.get("data", "")).split('" "')).strip('"')
                for a in data.get("Answer") or [] if a.get("type") == 16]
    raise RuntimeError("DNS lookup unavailable")


def _has_records(name: str, rtype: str) -> bool:
    from ..integrations import http

    try:
        data = http.request("GET", "https://dns.google/resolve", params={"name": name, "type": rtype}, timeout=10)
    except Exception:
        return False
    return bool(data.get("Answer"))


def dns_check(domain: str) -> dict:
    """SPF, DKIM, DMARC and MX for a sending domain, with a score and the fix for anything missing."""
    domain = domain.strip().lower().removeprefix("@")
    checks, fixes, score = {}, [], 0

    spf = [t for t in _txt(domain) if t.lower().startswith("v=spf1")]
    checks["spf"] = {"ok": len(spf) == 1, "record": spf[0] if spf else None}
    if len(spf) == 1:
        score += 25
        if spf[0].rstrip().endswith("+all"):
            fixes.append("SPF ends with +all, which lets anyone send as you. Change it to ~all or -all.")
            score -= 10
    elif len(spf) > 1:
        fixes.append("There are two SPF records; mailbox providers then treat SPF as failed. Merge them into one.")
    else:
        fixes.append(f"Add an SPF record: a TXT record on {domain} such as \"v=spf1 include:_spf.google.com ~all\" "
                     "(Google Workspace) or \"v=spf1 include:spf.protection.outlook.com ~all\" (Microsoft 365), "
                     "plus your sending service's include.")

    selector = next((s for s in DKIM_SELECTORS
                     if any("v=dkim1" in t.lower() or "p=" in t for t in _txt(f"{s}._domainkey.{domain}"))), None)
    checks["dkim"] = {"ok": bool(selector), "selector": selector}
    if selector:
        score += 30
    else:
        fixes.append("No DKIM key found under the common selectors. Turn on DKIM signing in Google Workspace "
                     "(Apps > Gmail > Authenticate email), Microsoft 365 (Defender > DKIM) or your sending service, "
                     "and publish the TXT record it gives you. (If you use a custom selector, this check can't see it.)")

    dmarc = [t for t in _txt(f"_dmarc.{domain}") if t.lower().startswith("v=dmarc1")]
    policy = None
    if dmarc:
        for part in dmarc[0].split(";"):
            k, _, v = part.strip().partition("=")
            if k.lower() == "p":
                policy = v.strip().lower()
    checks["dmarc"] = {"ok": bool(dmarc), "record": dmarc[0] if dmarc else None, "policy": policy}
    if dmarc:
        score += 25 + (5 if policy in ("quarantine", "reject") else 0)
        if policy == "none":
            fixes.append("DMARC is in monitor-only mode (p=none). Once reports look clean, move to p=quarantine.")
    else:
        fixes.append(f"Add a DMARC record: a TXT record on _dmarc.{domain} such as "
                     f"\"v=DMARC1; p=none; rua=mailto:dmarc@{domain}\". Gmail and Yahoo require it for bulk senders.")

    mx = _has_records(domain, "MX")
    checks["mx"] = {"ok": mx}
    if mx:
        score += 15
    else:
        fixes.append("The domain has no MX record, so replies can't reach you and filters treat it as suspicious.")

    warning = cold_domain_warning(domain)
    if warning:
        fixes.insert(0, warning)
    return {"domain": domain, "score": max(0, min(100, score)), "checks": checks, "fixes": fixes,
            "verdict": "ready" if score >= 85 else "needs work" if score >= 50 else "not ready"}


def primary_domains() -> set[str]:
    out = {d.strip().lower().removeprefix("www.") for d in (settings.primary_domain or "").split(",") if d.strip()}
    return out


def cold_domain_warning(domain: str) -> str | None:
    if domain.lower() in primary_domains():
        return (f"{domain} is your main company domain. Cold outreach from it puts your everyday email at risk. "
                "Use a separate, similar domain for outreach (e.g. get" + domain + " or " + domain.split(".")[0]
                + "-mail.com) with its own SPF, DKIM and DMARC.")
    return None


# ------------------------------------------------------------------ inbox placement tests

def placement_seeds(db: Session) -> list[Connection]:
    """The team's own connected mailboxes (read access) act as the test inboxes."""
    return list(db.scalars(select(Connection).where(Connection.provider.in_(["google", "microsoft"]),
                                                    Connection.status == "active")))


def start_placement_test(db: Session) -> PlacementTest:
    from ..tools.email import send_from_mailbox

    seeds = placement_seeds(db)
    if not seeds:
        raise ValueError("Connect at least one Gmail or Outlook mailbox on Integrations to use as a test inbox.")
    senders = list(db.scalars(select(SendingMailbox).where(SendingMailbox.status.in_(["active", "warming"]))))
    if not senders:
        raise ValueError("Add a sending mailbox first.")
    token = pysecrets.token_hex(5)
    test = PlacementTest(token=token, status="sent", results=[])
    db.add(test)
    db.flush()
    results = []
    for mb in senders:
        for seed in seeds:
            if not seed.account_email or seed.account_email.lower() == mb.address.lower():
                continue
            try:
                send_from_mailbox(db, mb, seed.account_email, f"Quick check-in [{token}]",
                                  "Hi,\n\nJust checking in on the note from last week. Does Thursday still work?\n\nThanks")
                results.append({"from": mb.address, "seed": seed.account_email, "seed_connection": seed.id,
                                "provider": seed.provider, "result": "pending"})
            except Exception as exc:
                results.append({"from": mb.address, "seed": seed.account_email, "seed_connection": seed.id,
                                "provider": seed.provider, "result": "send_failed", "detail": str(exc)[:200]})
    test.results = results
    return test


def _gmail_placement(token_value: str, access: str) -> str:
    from ..integrations import http

    auth = {"Authorization": f"Bearer {access}"}
    found = http.request("GET", "https://gmail.googleapis.com/gmail/v1/users/me/messages", headers=auth,
                         params={"q": f'subject:"{token_value}"', "includeSpamTrash": "true", "maxResults": 5})
    msgs = found.get("messages") or []
    if not msgs:
        return "not_found"
    m = http.request("GET", f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msgs[0]['id']}",
                     headers=auth, params={"format": "minimal"})
    labels = set(m.get("labelIds") or [])
    if "SPAM" in labels:
        return "spam"
    if "CATEGORY_PROMOTIONS" in labels:
        return "promotions"
    return "inbox" if "INBOX" in labels else "other"


def _graph_placement(token_value: str, access: str) -> str:
    from ..integrations import http

    auth = {"Authorization": f"Bearer {access}", "ConsistencyLevel": "eventual"}
    for folder, result in (("inbox", "inbox"), ("junkemail", "spam")):
        data = http.request("GET", f"https://graph.microsoft.com/v1.0/me/mailFolders/{folder}/messages",
                            headers=auth, params={"$search": f'"{token_value}"', "$top": 5, "$select": "subject"})
        if data.get("value"):
            return result
    return "not_found"


def check_placement_test(db: Session, test: PlacementTest) -> PlacementTest:
    """Read back where each test email landed (run a few minutes after starting the test)."""
    from ..integrations import oauth

    results = []
    for r in test.results or []:
        if r.get("result") not in ("pending", "not_found"):
            results.append(r)
            continue
        conn = db.get(Connection, r.get("seed_connection"))
        try:
            access = oauth.access_token(db, conn)
            r = {**r, "result": (_gmail_placement if conn.provider == "google" else _graph_placement)(test.token, access)}
        except Exception as exc:
            r = {**r, "result": "check_failed", "detail": str(exc)[:200]}
        results.append(r)
    test.results = results
    landed = [r for r in results if r["result"] in ("inbox", "promotions", "spam", "other", "not_found")]
    inbox = sum(1 for r in landed if r["result"] == "inbox")
    test.placement_pct = round(inbox / len(landed) * 100, 1) if landed else None
    test.status = "pending" if any(r["result"] in ("pending",) for r in results) else "checked"
    test.checked_at = datetime.now(UTC)
    return test


# ------------------------------------------------------------------ overview

def mailbox_dict(db: Session, mb: SendingMailbox) -> dict:
    return {"id": mb.id, "address": mb.address, "provider": mb.provider, "owner": mb.owner, "status": mb.status,
            "paused_reason": mb.paused_reason, "daily_cap": mb.daily_cap, "warmup_days": mb.warmup_days,
            "warmup_started_at": mb.warmup_started_at, "connection_id": mb.connection_id,
            "health": health(db, mb), "cold_domain_warning": cold_domain_warning(mb.address.split("@")[-1])}


def overview(db: Session) -> dict:
    mailboxes = [mailbox_dict(db, mb) for mb in db.scalars(select(SendingMailbox).where(SendingMailbox.status != "removed")
                                                             .order_by(SendingMailbox.id))]
    active = [m for m in mailboxes if m["status"] in ("active", "warming")]
    latest = db.scalar(select(PlacementTest).order_by(PlacementTest.id.desc()))
    return {
        "mailboxes": mailboxes,
        "capacity_today": sum(m["health"]["today_cap"] for m in active),
        "sent_24h": sum(m["health"]["sent_24h"] for m in active),
        "paused": sum(1 for m in mailboxes if m["status"] == "paused"),
        "latest_placement": {"id": latest.id, "status": latest.status, "placement_pct": latest.placement_pct,
                             "created_at": latest.created_at} if latest else None,
        "thresholds": {"complaint_rate": COMPLAINT_PAUSE_RATE, "bounce_rate": BOUNCE_PAUSE_RATE,
                       "per_mailbox_daily_cap_default": int(settings.mailbox_daily_cap)},
    }
