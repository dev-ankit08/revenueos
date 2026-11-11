"""Delivery events from the sending service (SendGrid, Postmark), normalised and acted on.

- hard bounce, spam complaint, unsubscribe -> the address is suppressed (never emailed again), the contact's
  sequence stops, and the timeline notes why. Bad data stops costing sender reputation at once.
- soft bounce, delivered, open, click -> recorded. Clicks are logged as activity (a real interest signal);
  opens are recorded but treated as weak evidence (privacy proxies open emails automatically).
"""
import base64
import logging
from datetime import datetime, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Activity, Approval, Contact, EmailEvent
from . import sequences
from .inbound import suppress

log = logging.getLogger(__name__)

SUPPRESSING = {"bounce": "email bounced (hard)", "complaint": "marked as spam", "unsubscribe": "unsubscribed via email provider"}


def verify_sendgrid(public_key_b64: str, payload: bytes, signature_b64: str, timestamp: str) -> bool:
    """SendGrid signed event webhook: ECDSA (P-256, SHA-256) over timestamp + raw body."""
    try:
        key = serialization.load_der_public_key(base64.b64decode(public_key_b64))
        key.verify(base64.b64decode(signature_b64), timestamp.encode() + payload, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def _ts(value) -> datetime:
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        if isinstance(value, str) and value:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OSError):
        pass
    return datetime.now(timezone.utc)


def normalise_sendgrid(events: list[dict]) -> list[dict]:
    kinds = {"delivered": "delivered", "open": "open", "click": "click", "spamreport": "complaint",
             "unsubscribe": "unsubscribe", "group_unsubscribe": "unsubscribe", "dropped": "dropped"}
    out = []
    for e in events if isinstance(events, list) else []:
        raw = e.get("event")
        if raw == "bounce":
            # SendGrid "blocked" is temporary (e.g. a spam filter); "bounce" is a permanent failure.
            kind = "soft_bounce" if e.get("type") == "blocked" else "bounce"
        else:
            kind = kinds.get(raw)
        if not kind:
            continue
        out.append({"event": kind, "email": e.get("email"), "approval_id": e.get("approval_id"),
                    "url": e.get("url"), "detail": e.get("reason") or e.get("response"),
                    "occurred_at": _ts(e.get("timestamp")),
                    "dedupe_key": f"sendgrid:{e.get('sg_event_id') or ''}" if e.get("sg_event_id") else None})
    return out


def normalise_postmark(e: dict) -> list[dict]:
    record = (e or {}).get("RecordType")
    meta = e.get("Metadata") or {}
    base = {"approval_id": meta.get("approval_id"), "url": e.get("OriginalLink")}
    if record == "Bounce":
        hard = e.get("Type") in ("HardBounce", "BadEmailAddress", "ManuallyDeactivated", "SpamNotification")
        return [{**base, "event": "bounce" if hard else "soft_bounce", "email": e.get("Email"),
                 "detail": e.get("Description") or e.get("Type"), "occurred_at": _ts(e.get("BouncedAt")),
                 "dedupe_key": f"postmark:bounce:{e.get('ID')}" if e.get("ID") else None}]
    if record == "SpamComplaint":
        return [{**base, "event": "complaint", "email": e.get("Email"), "detail": "spam complaint",
                 "occurred_at": _ts(e.get("BouncedAt")),
                 "dedupe_key": f"postmark:spam:{e.get('ID')}" if e.get("ID") else None}]
    kinds = {"Delivery": ("delivered", "Recipient", "DeliveredAt"), "Open": ("open", "Recipient", "ReceivedAt"),
             "Click": ("click", "Recipient", "ReceivedAt")}
    if record in kinds:
        kind, email_key, at_key = kinds[record]
        return [{**base, "event": kind, "email": e.get(email_key), "detail": None, "occurred_at": _ts(e.get(at_key)),
                 "dedupe_key": None}]
    if record == "SubscriptionChange" and e.get("SuppressSending"):
        return [{**base, "event": "unsubscribe", "email": e.get("Recipient"), "detail": e.get("SuppressionReason"),
                 "occurred_at": _ts(e.get("ChangedAt")), "dedupe_key": None}]
    return []


def record(db: Session, provider: str, events: list[dict]) -> dict:
    """Store events and act on them. Idempotent for events carrying a provider event id."""
    counts: dict[str, int] = {}
    seen: set[str | None] = set()
    for ev in events:
        approval = None
        if ev.get("approval_id"):
            try:
                approval = db.get(Approval, int(ev["approval_id"]))
            except (TypeError, ValueError):
                approval = None
        email = (ev.get("email") or (approval.contact.email if approval and approval.contact else "") or "").lower()
        contact = approval.contact if approval and approval.contact else (
            db.scalar(select(Contact).where(Contact.email.ilike(email))) if email else None)
        row = EmailEvent(provider=provider, event=ev["event"], email=email or None,
                         approval_id=approval.id if approval else None, contact_id=contact.id if contact else None,
                         url=ev.get("url"), detail=(ev.get("detail") or None), dedupe_key=ev.get("dedupe_key"),
                         occurred_at=ev.get("occurred_at") or datetime.now(timezone.utc))
        if row.dedupe_key and (db.scalar(select(EmailEvent.id).where(EmailEvent.dedupe_key == row.dedupe_key))
                               or row.dedupe_key in seen):
            continue  # already processed (the provider retried the webhook)
        seen.add(row.dedupe_key)
        db.add(row)
        counts[ev["event"]] = counts.get(ev["event"], 0) + 1
        _act(db, ev["event"], email, contact, row)
    return counts


def _act(db: Session, kind: str, email: str, contact: Contact | None, row: EmailEvent) -> None:
    if kind in SUPPRESSING and email:
        suppress(db, email, SUPPRESSING[kind])
        if contact:
            sequences.stop_contact(db, contact.id, SUPPRESSING[kind])
            if kind in ("complaint", "unsubscribe"):
                contact.status = "disqualified"
            db.add(Activity(contact_id=contact.id, type=f"email_{kind}",
                            detail=f"{SUPPRESSING[kind].capitalize()}. Address suppressed. {row.detail or ''}".strip()))
    elif kind == "click" and contact:
        db.add(Activity(contact_id=contact.id, type="email_click", detail=f"Clicked {row.url or 'a link'}"))
    elif kind == "soft_bounce" and contact:
        db.add(Activity(contact_id=contact.id, type="email_soft_bounce",
                        detail=f"Temporary delivery problem: {row.detail or 'unknown'}"))
