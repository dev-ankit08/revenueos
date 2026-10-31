"""Gmail / Outlook sync: replies trigger Reply Triage; emails reps send themselves join the timeline.

Privacy: only messages to or from a KNOWN CONTACT are read further; everything else is skipped
after its headers are checked.
"""
import base64
import logging
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parsedate_to_datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Approval, Connection, ContextEvent
from ..services import inbound, memory
from . import http, oauth

log = logging.getLogger(__name__)
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
GRAPH = "https://graph.microsoft.com/v1.0/me"
FIRST_SYNC_DAYS = 7
MAX_MESSAGES = 200


def _since(conn: Connection, feed: str) -> datetime:
    iso = (conn.cursors or {}).get(feed)
    return datetime.fromisoformat(iso) if iso else datetime.now(timezone.utc) - timedelta(days=FIRST_SYNC_DAYS)


def _set_cursor(conn: Connection, feed: str, when: datetime) -> None:
    conn.cursors = {**(conn.cursors or {}), feed: when.isoformat()}  # reassign so the JSON change is saved


# ----------------------------------------------------------------------------- provider readers

def _gmail_body(payload: dict) -> str:
    """First text/plain part, decoded."""
    stack = [payload]
    while stack:
        part = stack.pop(0)
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"] + "===").decode("utf-8", errors="replace")
        stack.extend(part.get("parts", []))
    return ""


def gmail_messages(token: str, since: datetime) -> list[dict]:
    auth = {"Authorization": f"Bearer {token}"}
    ids, page = [], None
    while len(ids) < MAX_MESSAGES:
        params = {"q": f"after:{int(since.timestamp())}", "maxResults": 100}
        if page:
            params["pageToken"] = page
        res = http.request("GET", f"{GMAIL}/messages", headers=auth, params=params)
        ids += [m["id"] for m in res.get("messages", [])]
        page = res.get("nextPageToken")
        if not page:
            break
    out = []
    for mid in ids[:MAX_MESSAGES]:
        m = http.request("GET", f"{GMAIL}/messages/{mid}", headers=auth, params={"format": "full"})
        h = {x["name"].lower(): x["value"] for x in m.get("payload", {}).get("headers", [])}
        out.append({
            "id": m["id"], "thread_id": m.get("threadId"),
            "from": (getaddresses([h.get("from", "")]) or [("", "")])[0][1].lower(),
            "to": [a.lower() for _, a in getaddresses([h.get("to", ""), h.get("cc", "")]) if a],
            "subject": h.get("subject"),
            "sent_at": datetime.fromtimestamp(int(m.get("internalDate", "0")) / 1000, tz=timezone.utc),
            "text": _gmail_body(m.get("payload", {})) or m.get("snippet", ""),
        })
    return out


def outlook_messages(token: str, since: datetime) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}", "Prefer": 'outlook.body-content-type="text"'}
    url = f"{GRAPH}/messages"
    params = {"$filter": f"receivedDateTime ge {since.strftime('%Y-%m-%dT%H:%M:%SZ')}", "$top": 50,
              "$select": "id,conversationId,subject,from,toRecipients,ccRecipients,receivedDateTime,body"}
    out = []
    while url and len(out) < MAX_MESSAGES:
        res = http.request("GET", url, headers=headers, params=params)
        for m in res.get("value", []):
            out.append({
                "id": m["id"], "thread_id": m.get("conversationId"),
                "from": (m.get("from") or {}).get("emailAddress", {}).get("address", "").lower(),
                "to": [r["emailAddress"]["address"].lower() for r in
                       (m.get("toRecipients") or []) + (m.get("ccRecipients") or []) if r.get("emailAddress")],
                "subject": m.get("subject"),
                "sent_at": datetime.fromisoformat(m["receivedDateTime"].replace("Z", "+00:00")),
                "text": (m.get("body") or {}).get("content", ""),
            })
        url, params = res.get("@odata.nextLink"), None  # nextLink already carries the query
    return out


# ----------------------------------------------------------------------------- processing

def process_message(db: Session, provider: str, msg: dict, mailbox: str | None) -> str:
    source_ref = f"email:{provider}:{msg['id']}"
    if db.scalar(select(ContextEvent.id).where(ContextEvent.source_ref == source_ref)):
        return "duplicate"
    sender = memory.contacts_by_emails(db, [msg["from"]])
    if sender:  # a prospect wrote to us
        c = sender[0]
        memory.record(db, source="email", source_ref=source_ref, contact_id=c.id, author=c.full_name,
                      occurred_at=msg["sent_at"], title=msg["subject"], raw_text=inbound.strip_quoted(msg["text"])[:20000],
                      processed=True)
        inbound.handle_reply(db, c, msg["text"], msg["subject"])
        return "reply"
    recipients = memory.contacts_by_emails(db, msg["to"])
    if not recipients:
        return "skipped: no known contact"
    c = recipients[0]
    # Already recorded when RevenueOS sent it?
    sent_by_us = db.scalar(select(Approval.id).where(
        Approval.contact_id == c.id, Approval.status == "sent", Approval.subject == msg["subject"]))
    if sent_by_us:
        return "skipped: sent by RevenueOS"
    memory.record(db, source="email", source_ref=source_ref, contact_id=c.id, author=mailbox,
                  occurred_at=msg["sent_at"], title=msg["subject"], raw_text=inbound.strip_quoted(msg["text"])[:20000],
                  summary=f"{mailbox or 'Rep'} emailed {c.full_name}: \"{msg['subject']}\"", processed=True)
    return "outbound"


def sync_mail(db: Session, conn: Connection) -> dict:
    started = datetime.now(timezone.utc)
    token = oauth.access_token(db, conn)
    since = _since(conn, "mail")
    reader = gmail_messages if conn.provider == "google" else outlook_messages
    counts: dict[str, int] = {}
    for msg in sorted(reader(token, since), key=lambda m: m["sent_at"]):
        outcome = process_message(db, conn.provider, msg, conn.account_email)
        counts[outcome] = counts.get(outcome, 0) + 1
    _set_cursor(conn, "mail", started - timedelta(minutes=5))  # small overlap; duplicates are skipped
    return counts
