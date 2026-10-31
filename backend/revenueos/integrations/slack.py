"""Slack Events API ingestion. Reads ONLY channels registered (opted in) in SlackChannel.

Setup: create a Slack app, subscribe to `message.channels` and `message.groups` bot events,
set the Request URL to /api/integrations/slack/events, invite the bot to the channels you
opt in, and register each channel (and its account) on the Integrations page.
"""
import hashlib
import hmac
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import SlackChannel
from ..services import memory

MAX_SKEW_SECONDS = 300
IGNORED_SUBTYPES = {"channel_join", "channel_leave", "bot_message", "message_changed", "message_deleted",
                    "channel_topic", "channel_purpose", "channel_name"}


def verify_signature(secret: str, timestamp: str | None, body: bytes, signature: str | None,
                     now: float | None = None) -> bool:
    """Slack request signing: v0=HMAC_SHA256(secret, 'v0:{timestamp}:{body}'), within 5 minutes."""
    if not (secret and timestamp and signature):
        return False
    try:
        if abs((now or time.time()) - int(timestamp)) > MAX_SKEW_SECONDS:
            return False  # replay protection
    except ValueError:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def ingest_message(db: Session, event: dict) -> str:
    """Store one channel message as a raw context event. Returns what happened (for logs/tests)."""
    if event.get("type") != "message" or event.get("subtype") in IGNORED_SUBTYPES or event.get("bot_id"):
        return "ignored: not a person's message"
    channel = db.scalar(select(SlackChannel).where(SlackChannel.channel_id == event.get("channel"),
                                                   SlackChannel.enabled.is_(True)))
    if not channel:
        return "ignored: channel not opted in"
    text = (event.get("text") or "").strip()
    if not text:
        return "ignored: empty"
    account_id = channel.account_id
    if account_id is None:
        hit = memory.account_mentioned(db, text)
        account_id = hit.id if hit else None
    ev = memory.record(
        db, source="slack", source_ref=f"slack:{event['channel']}:{event.get('ts')}",
        account_id=account_id, author=event.get("user"), title=f"#{channel.name or channel.channel_id}",
        raw_text=text[:8000],
        # Everything in a shared (Slack Connect) channel is visible to the customer;
        # anything in an internal channel must never be shown to them.
        internal=not channel.shared,
    )
    if not ev:
        return "duplicate"
    return "stored" if account_id else "stored: no account matched"
