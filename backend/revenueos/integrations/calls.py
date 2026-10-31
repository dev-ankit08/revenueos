"""Call transcripts → Call Analyst → deal update + follow-up draft + timeline entry.

Native: Zoom (`recording.transcript_completed` webhook). Everything else (Teams via Graph or
Power Automate, Google Meet, Gong/Chorus, manual upload) posts to the generic signed endpoint
/api/integrations/transcripts with attendees + transcript text.
"""
import hashlib
import hmac
import logging
import re
import time
import urllib.request
from datetime import datetime

from sqlalchemy import select

from ..agents import deals
from ..db import session_scope
from ..models import CalendarEvent, ContextEvent
from ..services import memory

log = logging.getLogger(__name__)
_TIMING = re.compile(r"^\d{2}:\d{2}(:\d{2})?[.,]\d{3}\s+-->\s+")


def parse_vtt(text: str) -> str:
    """WebVTT → plain 'Speaker: words' lines (drops header, cue numbers and timings)."""
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.upper().startswith("WEBVTT") or line.isdigit() or _TIMING.match(line):
            continue
        if not lines or lines[-1] != line:
            lines.append(line)
    return "\n".join(lines)


# ----------------------------------------------------------------------------- Zoom

def zoom_signature_ok(secret: str, timestamp: str | None, body: bytes, signature: str | None,
                      now: float | None = None) -> bool:
    """x-zm-signature = 'v0=' + HMAC_SHA256(secret, 'v0:{x-zm-request-timestamp}:{body}')."""
    if not (secret and timestamp and signature):
        return False
    try:
        ts = int(timestamp)
        ts = ts / 1000 if ts > 1e12 else ts  # Zoom sends seconds; tolerate milliseconds
        if abs((now or time.time()) - ts) > 300:
            return False
    except ValueError:
        return False
    msg = f"v0:{timestamp}:".encode() + body
    expected = "v0=" + hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def zoom_url_validation(secret: str, plain_token: str) -> dict:
    """Answer Zoom's endpoint.url_validation challenge."""
    return {"plainToken": plain_token,
            "encryptedToken": hmac.new(secret.encode(), plain_token.encode(), hashlib.sha256).hexdigest()}


def zoom_transcripts(payload: dict) -> list[dict]:
    """Pull the transcript files and meeting facts out of a recording.transcript_completed event."""
    obj = payload.get("payload", {}).get("object", {})
    token = payload.get("download_token")
    files = [f for f in obj.get("recording_files", [])
             if f.get("file_type") == "TRANSCRIPT" or f.get("recording_type") == "audio_transcript"]
    return [{"external_id": f"zoom:{obj.get('uuid') or obj.get('id')}:{f.get('id', i)}",
             "meeting_ref": str(obj.get("id") or ""), "title": obj.get("topic") or "Zoom call",
             "started_at": obj.get("start_time"), "host_email": obj.get("host_email"),
             "download_url": f.get("download_url"), "download_token": token}
            for i, f in enumerate(files)]


def download_text(url: str, token: str | None) -> str:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 (URL comes from a signed Zoom event)
        return resp.read().decode("utf-8", errors="replace")


# ----------------------------------------------------------------------------- shared pipeline

def _resolve_contact(attendees: list[str], meeting_ref: str | None, title: str | None) -> int | None:
    """Who was this call with? Attendee emails, then the calendar entry for the meeting, then the title."""
    with session_scope() as db:
        hits = memory.contacts_by_emails(db, attendees)
        if hits:
            return hits[0].id
        if meeting_ref:
            ev = db.scalar(select(CalendarEvent).where(CalendarEvent.meeting_url.contains(meeting_ref),
                                                       CalendarEvent.contact_id.isnot(None)))
            if ev:
                return ev.contact_id
        if title and (acc := memory.account_mentioned(db, title)) and acc.contacts:
            return acc.contacts[0].id
    return None


async def process_transcript(*, source: str, external_id: str, transcript: str, title: str | None = None,
                             attendees: list[str] | None = None, meeting_ref: str | None = None,
                             started_at: str | None = None, parent_run_id: int | None = None) -> dict:
    source_ref = f"call:{external_id}"
    with session_scope() as db:
        if db.scalar(select(ContextEvent.id).where(ContextEvent.source_ref == source_ref)):
            return {"status": "duplicate"}
    occurred = None
    if started_at:
        try:
            occurred = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        except ValueError:
            occurred = None

    contact_id = _resolve_contact(attendees or [], meeting_ref, title)
    if contact_id is None:
        # Keep it for a person to assign on the Integrations page; nothing is analysed blindly.
        with session_scope() as db:
            memory.record(db, source="call", source_ref=source_ref, occurred_at=occurred, title=title,
                          author=source, raw_text=transcript[:200_000],
                          facts={"attendees": attendees or [], "meeting_ref": meeting_ref})
        return {"status": "unmatched"}

    r = await deals.analyze_call(contact_id, transcript, parent_run_id=parent_run_id,
                                 source_ref=source_ref, occurred_at=occurred, title=title)
    return {"status": "analysed", **r}


async def process_zoom_event(payload: dict, parent_run_id: int | None = None) -> list[dict]:
    results = []
    for t in zoom_transcripts(payload):
        try:
            text = parse_vtt(download_text(t["download_url"], t["download_token"]))
        except Exception as exc:
            log.warning("zoom transcript download failed: %s", exc)
            results.append({"status": "download_failed", "external_id": t["external_id"]})
            continue
        results.append(await process_transcript(
            source="zoom", external_id=t["external_id"], transcript=text, title=t["title"],
            attendees=[t["host_email"]] if t.get("host_email") else [], meeting_ref=t["meeting_ref"],
            started_at=t["started_at"], parent_run_id=parent_run_id))
    return results
