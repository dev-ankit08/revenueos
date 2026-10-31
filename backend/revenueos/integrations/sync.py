"""Scheduled sync of connected accounts: calendars, mail, Teams/Meet transcripts after meetings, Gong calls.

Provider HTTP calls are blocking, so each feed runs in a worker thread; the Call Analyst (async)
runs on the event loop once a transcript is in hand.
"""
import asyncio
import base64
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..config import settings
from ..db import session_scope
from ..models import CalendarEvent, Connection
from . import calendar, calls, http, mail, oauth

log = logging.getLogger(__name__)
GCAL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
GRAPH = "https://graph.microsoft.com/v1.0/me"
MEET = "https://meet.googleapis.com/v2"
LOOKAHEAD_DAYS = 14


# ----------------------------------------------------------------------------- calendars

def google_events(token: str, now: datetime) -> list[dict]:
    res = http.request("GET", GCAL, headers={"Authorization": f"Bearer {token}"}, params={
        "timeMin": (now - timedelta(days=1)).isoformat(), "timeMax": (now + timedelta(days=LOOKAHEAD_DAYS)).isoformat(),
        "singleEvents": "true", "orderBy": "startTime", "maxResults": 250})
    out = []
    for e in res.get("items", []):
        start = (e.get("start") or {}).get("dateTime")
        if not start or e.get("status") == "cancelled":
            continue  # all-day items aren't meetings
        video = next((p.get("uri") for p in (e.get("conferenceData") or {}).get("entryPoints", [])
                      if p.get("entryPointType") == "video"), None)
        url = e.get("hangoutLink") or video or _first_url(f"{e.get('location', '')} {e.get('description', '')}")
        out.append({"id": f"google:{e['id']}", "title": e.get("summary") or "Meeting", "start": start,
                    "end": (e.get("end") or {}).get("dateTime"), "meeting_url": url,
                    "attendees": [a["email"] for a in e.get("attendees", []) if a.get("email")]})
    return out


def outlook_events(token: str, now: datetime) -> list[dict]:
    res = http.request("GET", f"{GRAPH}/calendarView", headers={
        "Authorization": f"Bearer {token}", "Prefer": 'outlook.timezone="UTC"'}, params={
        "startDateTime": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDateTime": (now + timedelta(days=LOOKAHEAD_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "$top": 100, "$select": "id,subject,start,end,attendees,onlineMeeting,location,bodyPreview,isCancelled"})
    out = []
    for e in res.get("value", []):
        if e.get("isCancelled"):
            continue
        url = (e.get("onlineMeeting") or {}).get("joinUrl") or _first_url(
            f"{(e.get('location') or {}).get('displayName', '')} {e.get('bodyPreview', '')}")
        out.append({"id": f"microsoft:{e['id']}", "title": e.get("subject") or "Meeting",
                    "start": e["start"]["dateTime"] + "Z", "end": e["end"]["dateTime"] + "Z", "meeting_url": url,
                    "attendees": [a["emailAddress"]["address"] for a in e.get("attendees", [])
                                  if a.get("emailAddress", {}).get("address")]})
    return out


def _first_url(text: str) -> str | None:
    m = re.search(r"https://[^\s<>\"]+", text or "")
    return m.group(0) if m else None


# ----------------------------------------------------------------------------- transcripts after meetings

def teams_transcript(token: str, join_url: str) -> tuple[str, str] | None:
    """(transcript id, plain text) for a Teams meeting, or None if no transcript exists yet."""
    auth = {"Authorization": f"Bearer {token}"}
    meetings = http.request("GET", f"{GRAPH}/onlineMeetings", headers=auth,
                            params={"$filter": f"JoinWebUrl eq '{join_url}'"}).get("value", [])
    if not meetings:
        return None
    mid = meetings[0]["id"]
    transcripts = http.request("GET", f"{GRAPH}/onlineMeetings/{mid}/transcripts", headers=auth).get("value", [])
    if not transcripts:
        return None
    t = sorted(transcripts, key=lambda x: x.get("createdDateTime", ""))[-1]
    vtt = http.request("GET", f"{GRAPH}/onlineMeetings/{mid}/transcripts/{t['id']}/content", headers=auth,
                       params={"$format": "text/vtt"}, raw=True)
    return t["id"], calls.parse_vtt(vtt)


def meet_transcript(token: str, meeting_url: str) -> tuple[str, str] | None:
    """(transcript name, plain text) for a Google Meet meeting, or None if none is ready."""
    m = re.search(r"meet\.google\.com/([a-z]{3}-[a-z]{4}-[a-z]{3})", meeting_url or "")
    if not m:
        return None
    auth = {"Authorization": f"Bearer {token}"}
    records = http.request("GET", f"{MEET}/conferenceRecords", headers=auth,
                           params={"filter": f'space.meeting_code = "{m.group(1)}"'}).get("conferenceRecords", [])
    if not records:
        return None
    record = sorted(records, key=lambda r: r.get("startTime", ""))[-1]["name"]
    transcripts = [t for t in http.request("GET", f"{MEET}/{record}/transcripts", headers=auth).get("transcripts", [])
                   if t.get("state") in ("FILE_GENERATED", "ENDED")]
    if not transcripts:
        return None
    people = {p["name"]: (p.get("signedinUser") or p.get("anonymousUser") or p.get("phoneUser") or {}).get("displayName", "Speaker")
              for p in http.request("GET", f"{MEET}/{record}/participants", headers=auth).get("participants", [])}
    name, lines, page = transcripts[-1]["name"], [], None
    while True:
        params = {"pageSize": 100, **({"pageToken": page} if page else {})}
        res = http.request("GET", f"{MEET}/{name}/entries", headers=auth, params=params)
        lines += [f"{people.get(e.get('participant'), 'Speaker')}: {e.get('text', '')}" for e in res.get("transcriptEntries", [])]
        page = res.get("nextPageToken")
        if not page:
            break
    return name, "\n".join(lines)


def _ended_meetings_needing_transcripts(now: datetime) -> list[dict]:
    with session_scope() as db:
        rows = db.scalars(select(CalendarEvent).where(
            CalendarEvent.transcript_status == "pending", CalendarEvent.ends_at < now - timedelta(minutes=10),
            CalendarEvent.ends_at > now - timedelta(days=3), CalendarEvent.connection_id.isnot(None)))
        return [{"id": e.id, "platform": e.platform, "url": e.meeting_url, "title": e.title,
                 "attendees": e.attendees, "start": e.starts_at.isoformat(), "ends_at": e.ends_at,
                 "connection_id": e.connection_id} for e in rows]


def _fetch_transcript(event: dict) -> tuple[str, str] | None:
    with session_scope() as db:
        conn = db.get(Connection, event["connection_id"])
        token = oauth.access_token(db, conn)
    reader = teams_transcript if event["platform"] == "teams" else meet_transcript
    return reader(token, event["url"])


def _set_transcript_status(event_id: int, status: str) -> None:
    with session_scope() as db:
        db.get(CalendarEvent, event_id).transcript_status = status


# ----------------------------------------------------------------------------- Gong

def gong_calls(since: datetime) -> list[dict]:
    """Calls since `since` with transcripts and the customer-side attendee emails."""
    auth = {"Authorization": "Basic " + base64.b64encode(
        f"{settings.gong_access_key}:{settings.gong_access_secret}".encode()).decode()}
    base, calls_meta, cursor = settings.gong_base_url.rstrip("/"), [], None
    while True:
        body = {"filter": {"fromDateTime": since.strftime("%Y-%m-%dT%H:%M:%SZ")},
                "contentSelector": {"exposedFields": {"parties": True}}}
        if cursor:
            body["cursor"] = cursor
        res = http.request("POST", f"{base}/v2/calls/extensive", headers=auth, json_body=body)
        calls_meta += res.get("calls", [])
        cursor = (res.get("records") or {}).get("cursor")
        if not cursor or len(calls_meta) >= 200:
            break
    if not calls_meta:
        return []
    ids = [c["metaData"]["id"] for c in calls_meta]
    tr = http.request("POST", f"{base}/v2/calls/transcript", headers=auth, json_body={"filter": {"callIds": ids}})
    by_id = {t["callId"]: t.get("transcript", []) for t in tr.get("callTranscripts", [])}
    out = []
    for c in calls_meta:
        meta, parties = c["metaData"], c.get("parties", [])
        names = {p.get("speakerId"): p.get("name") or p.get("emailAddress") or "Speaker" for p in parties}
        lines = [f"{names.get(seg.get('speakerId'), 'Speaker')}: " + " ".join(s.get("text", "") for s in seg.get("sentences", []))
                 for seg in by_id.get(meta["id"], [])]
        if lines:
            out.append({"id": meta["id"], "title": meta.get("title"), "started": meta.get("started"),
                        "url": meta.get("url"), "transcript": "\n".join(lines),
                        "attendees": [p["emailAddress"] for p in parties
                                      if p.get("emailAddress") and p.get("affiliation") != "Internal"]})
    return out


def _gong_connection() -> int:
    with session_scope() as db:
        conn = db.scalar(select(Connection).where(Connection.provider == "gong"))
        if not conn:
            conn = Connection(provider="gong", account_email="gong", cursors={})
            db.add(conn)
            db.flush()
        return conn.id


# ----------------------------------------------------------------------------- orchestration

def _sync_connection(conn_id: int) -> dict:
    """Calendar + mail for one Google/Microsoft connection (blocking; runs in a thread)."""
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        conn = db.get(Connection, conn_id)
        try:
            token = oauth.access_token(db, conn)
            events = (google_events if conn.provider == "google" else outlook_events)(token, now)
            cal = calendar.upsert_events(db, [{**e, "connection_id": conn.id} for e in events])
            mail_counts = mail.sync_mail(db, conn)
            conn.status, conn.last_error = "active", None
            return {"calendar": cal, "mail": mail_counts}
        except Exception as exc:  # keep other connections syncing; surface the error on the Integrations page
            log.warning("sync failed for connection %s: %s", conn_id, exc)
            conn.status, conn.last_error = "error", str(exc)[:500]
            return {"error": str(exc)[:200]}


async def run_sync(parent_run_id: int | None = None) -> str:
    with session_scope() as db:
        conn_ids = list(db.scalars(select(Connection.id).where(
            Connection.provider.in_(["google", "microsoft"]), Connection.status != "disabled")))
    results = {cid: await asyncio.to_thread(_sync_connection, cid) for cid in conn_ids}

    analysed = 0
    for ev in _ended_meetings_needing_transcripts(datetime.now(timezone.utc)):
        try:
            got = await asyncio.to_thread(_fetch_transcript, ev)
        except Exception as exc:
            log.warning("transcript fetch failed for event %s: %s", ev["id"], exc)
            got = None
        if got:
            tid, text = got
            await calls.process_transcript(source=ev["platform"], external_id=f"{ev['platform']}:{tid}", transcript=text,
                                           title=ev["title"], attendees=ev["attendees"], meeting_ref=ev["url"],
                                           started_at=ev["start"], parent_run_id=parent_run_id)
            _set_transcript_status(ev["id"], "done")
            analysed += 1
        elif datetime.now(timezone.utc) - _aware(ev["ends_at"]) > timedelta(days=1):
            _set_transcript_status(ev["id"], "unavailable")  # recording/transcription was off

    gong_done = 0
    if settings.gong_access_key and settings.gong_access_secret:
        gid = _gong_connection()
        with session_scope() as db:
            since = mail._since(db.get(Connection, gid), "gong")
        started = datetime.now(timezone.utc)
        for c in await asyncio.to_thread(gong_calls, since):
            r = await calls.process_transcript(source="gong", external_id=f"gong:{c['id']}", transcript=c["transcript"],
                                               title=c["title"], attendees=c["attendees"], started_at=c["started"],
                                               parent_run_id=parent_run_id)
            gong_done += r.get("status") == "analysed"
        with session_scope() as db:
            mail._set_cursor(db.get(Connection, gid), "gong", started - timedelta(minutes=5))

    crm = await asyncio.to_thread(run_crm_pull)
    return f"CRM {crm}. Synced {len(conn_ids)} accounts {results}; {analysed} meeting transcripts and {gong_done} Gong calls analysed."


def run_crm_pull() -> dict:
    """Bring in new leads and deal changes from the CRM (blocking; run in a thread)."""
    from ..tools.crm import get_crm

    crm = get_crm()
    if crm.name == "local":
        return {}
    with session_scope() as db:
        conn = db.scalar(select(Connection).where(Connection.provider == f"crm:{crm.name}"))
        if not conn:
            conn = Connection(provider=f"crm:{crm.name}", account_email=crm.name, cursors={})
            db.add(conn)
            db.flush()
        started = datetime.now(timezone.utc)
        try:
            result = crm.pull_changes(db, mail._since(conn, "pull"))
            mail._set_cursor(conn, "pull", started - timedelta(minutes=5))
            conn.status, conn.last_error = "active", None
            return result
        except Exception as exc:
            log.warning("CRM pull failed: %s", exc)
            conn.status, conn.last_error = "error", str(exc)[:500]
            return {"error": str(exc)[:200]}


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
