"""Phase B: OAuth, Gmail/Outlook sync, calendar sync, Teams/Meet/Gong transcripts, Slack approvals,
person dossiers, committee outreach, holdout lift. All provider HTTP is mocked at integrations.http.request."""
import base64
import hashlib
import hmac
import json
import threading
import time
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from revenueos import jobs, seed
from revenueos.agents import committee, person_researcher, workflows, writer
from revenueos.agents.base import AgentResult, new_run
from revenueos.config import settings
from revenueos.db import session_scope
from revenueos.integrations import calls, http, mail, oauth, slack_bot, sync
from revenueos.main import app
from revenueos.models import (Activity, Approval, CalendarEvent, Connection, Contact, ContextEvent, Opportunity,
                              PersonProfile)
from revenueos.services import approvals, insights, secrets

NOW = datetime.now(timezone.utc)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def seeded():
    seed.main()


@contextmanager
def setting(**values):
    old = {k: getattr(settings, k) for k in values}
    for k, v in values.items():
        object.__setattr__(settings, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            object.__setattr__(settings, k, v)


@pytest.fixture
def secret_key():
    with setting(secret_key="test-secret", google_client_id="gid", google_client_secret="gsec",
                 ms_client_id="mid", ms_client_secret="msec"):
        yield


class FakeHTTP:
    """Routes (method, url-prefix) to canned responses; records every call."""

    def __init__(self, routes: dict):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for (m, prefix), resp in self.routes.items():
            if m == method and url.startswith(prefix):
                return resp(url, kw) if callable(resp) else resp
        raise AssertionError(f"unexpected {method} {url}")


def _contact(email):
    with session_scope() as db:
        return db.scalar(select(Contact).where(Contact.email == email))


def _connection(provider="google", expires_in=3600) -> int:
    with session_scope() as db:
        c = Connection(provider=provider, account_email=f"rep@{provider}.example", cursors={},
                       access_token_enc=secrets.encrypt("live-token"), refresh_token_enc=secrets.encrypt("refresh"),
                       expires_at=NOW + timedelta(seconds=expires_in))
        db.add(c)
        db.flush()
        return c.id


# ----------------------------------------------------------------------------- secrets & OAuth

def test_tokens_encrypted_and_secret_key_required(secret_key):
    enc = secrets.encrypt("abc")
    assert enc != "abc" and secrets.decrypt(enc) == "abc"
    with setting(secret_key=None):
        with pytest.raises(secrets.SecretKeyMissing):
            secrets.encrypt("abc")


def test_oauth_state_and_code_exchange(seeded, secret_key, monkeypatch):
    state = oauth.make_state("google")
    assert oauth.check_state("google", state) is None  # valid, issued in the default workspace
    assert oauth.check_state("microsoft", state) is False
    assert oauth.check_state("google", state[:-2] + "xx") is False
    url = oauth.authorize_url("microsoft")
    assert url.startswith("https://login.microsoftonline.com/common/oauth2/v2.0/authorize?")
    assert "OnlineMeetingTranscript.Read.All" in urllib.parse.unquote_plus(url)

    fake = FakeHTTP({
        ("POST", "https://oauth2.googleapis.com/token"): {"access_token": "at1", "refresh_token": "rt1", "expires_in": 3600},
        ("GET", "https://openidconnect.googleapis.com/v1/userinfo"): {"email": "rep@us.example"},
    })
    monkeypatch.setattr(http, "request", fake)
    with session_scope() as db:
        conn = oauth.exchange_code(db, "google", "code123")
        assert conn.account_email == "rep@us.example"
        assert "rt1" not in (conn.refresh_token_enc or "") and secrets.decrypt(conn.refresh_token_enc) == "rt1"
        conn.expires_at = NOW - timedelta(minutes=1)  # force a refresh
        fake.routes[("POST", "https://oauth2.googleapis.com/token")] = {"access_token": "at2", "expires_in": 3600}
        assert oauth.access_token(db, conn) == "at2"
        assert fake.calls[-1][2]["form"]["grant_type"] == "refresh_token"


def test_oauth_callback_rejects_bad_state(seeded, secret_key):
    with TestClient(app) as api:
        r = api.get("/api/integrations/oauth/google/callback?code=x&state=forged", follow_redirects=False)
        assert r.status_code == 400


# ----------------------------------------------------------------------------- mail

def _gmail_routes(messages: list[dict]):
    def get_msg(url, kw):
        mid = url.rsplit("/", 1)[1]
        m = next(x for x in messages if x["id"] == mid)
        body = base64.urlsafe_b64encode(m["text"].encode()).decode()
        return {"id": mid, "threadId": "t", "internalDate": str(int(NOW.timestamp() * 1000)),
                "payload": {"headers": [{"name": "From", "value": m["from"]}, {"name": "To", "value": m["to"]},
                                        {"name": "Subject", "value": m["subject"]}],
                            "mimeType": "multipart/alternative",
                            "parts": [{"mimeType": "text/plain", "body": {"data": body}}]}}
    return {("GET", f"{mail.GMAIL}/messages/"): get_msg,
            ("GET", f"{mail.GMAIL}/messages"): {"messages": [{"id": m["id"]} for m in messages]}}


def test_gmail_sync_handles_replies_rep_emails_and_ignores_strangers(seeded, secret_key, monkeypatch):
    started = []
    monkeypatch.setattr(jobs, "start", lambda name, desc, work, **kw: started.append(name) or 1)
    priya = _contact("priya.sample@example.com")
    with session_scope() as db:
        approvals.queue(db, kind="email", contact=db.get(Contact, priya.id), subject="follow-up", body="Hi Priya.",
                        created_by_agent="t", sequence_step=2, send_after_days=3)
    messages = [
        {"id": "m1", "from": "Priya Sample <priya.sample@example.com>", "to": "rep@us.example", "subject": "Re: idea",
         "text": "Yes, let's talk Tuesday.\n\nOn Mon, Rep wrote:\n> earlier text"},
        {"id": "m2", "from": "rep@us.example", "to": "Elena Test <elena.test@example.com>", "subject": "Notes from today",
         "text": "Thanks Elena, attaching the plan."},
        {"id": "m3", "from": "newsletter@shop.example", "to": "rep@us.example", "subject": "Sale", "text": "50% off"},
    ]
    monkeypatch.setattr(http, "request", FakeHTTP(_gmail_routes(messages)))
    cid = _connection("google")
    with session_scope() as db:
        counts = mail.sync_mail(db, db.get(Connection, cid))
        assert counts == {"reply": 1, "outbound": 1, "skipped: no known contact": 1}
        assert db.get(Connection, cid).cursors["mail"]
    assert started == ["job:reply_triage"]
    with session_scope() as db:
        assert db.get(Contact, priya.id).status == "replied"
        assert db.scalar(select(Approval).where(Approval.contact_id == priya.id)).status == "rejected"  # paused
        reply = db.scalar(select(ContextEvent).where(ContextEvent.source_ref == "email:google:m1"))
        assert "earlier text" not in reply.raw_text  # quoted thread stripped
        assert db.scalar(select(ContextEvent).where(ContextEvent.source_ref == "email:google:m2")).summary.startswith("rep@google.example emailed Elena")
        assert db.scalar(select(ContextEvent).where(ContextEvent.source_ref.like("%m3"))) is None  # stranger: not stored
        # Second sync: everything already seen
        monkeypatch.setattr(http, "request", FakeHTTP(_gmail_routes(messages)))
        assert mail.sync_mail(db, db.get(Connection, cid)) == {"duplicate": 2, "skipped: no known contact": 1}


def test_outlook_reader():
    fake = FakeHTTP({("GET", f"{mail.GRAPH}/messages"): {"value": [{
        "id": "o1", "conversationId": "c", "subject": "Hello", "receivedDateTime": "2026-10-30T10:00:00Z",
        "from": {"emailAddress": {"address": "Priya.Sample@example.com"}},
        "toRecipients": [{"emailAddress": {"address": "rep@us.example"}}], "body": {"content": "Hi"}}]}})
    import revenueos.integrations.mail as m
    orig, m.http.request = m.http.request, fake
    try:
        [msg] = mail.outlook_messages("tok", NOW - timedelta(days=1))
    finally:
        m.http.request = orig
    assert msg["from"] == "priya.sample@example.com" and msg["to"] == ["rep@us.example"]
    assert "receivedDateTime ge" in fake.calls[0][2]["params"]["$filter"]


# ----------------------------------------------------------------------------- calendar + Teams transcript

@pytest.mark.anyio
async def test_outlook_calendar_then_teams_transcript_is_analysed(seeded, secret_key, monkeypatch):
    join = "https://teams.microsoft.com/l/meetup-join/abc"
    start, end = NOW - timedelta(hours=2), NOW - timedelta(hours=1)
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n<v Elena>We need a pilot by March</v>"
    fake = FakeHTTP({
        ("GET", f"{sync.GRAPH}/calendarView"): {"value": [{
            "id": "ev1", "subject": "Maersk pilot", "isCancelled": False,
            "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S")}, "end": {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%S")},
            "attendees": [{"emailAddress": {"address": "elena.test@example.com"}}], "onlineMeeting": {"joinUrl": join}}]},
        ("GET", f"{mail.GRAPH}/messages"): {"value": []},
        ("GET", f"{sync.GRAPH}/onlineMeetings/MID/transcripts/T1/content"): vtt,
        ("GET", f"{sync.GRAPH}/onlineMeetings/MID/transcripts"): {"value": [{"id": "T1", "createdDateTime": "x"}]},
        ("GET", f"{sync.GRAPH}/onlineMeetings"): {"value": [{"id": "MID"}]},
    })
    monkeypatch.setattr(http, "request", fake)
    processed = []

    async def fake_process(**kw):
        processed.append(kw)
        return {"status": "analysed"}
    monkeypatch.setattr(calls, "process_transcript", fake_process)
    _connection("microsoft")
    out = await sync.run_sync()
    assert "1 meeting transcripts" in out
    [p] = processed
    assert p["source"] == "teams" and p["external_id"] == "teams:T1" and "pilot by March" in p["transcript"]
    assert p["attendees"] == ["elena.test@example.com"]
    with session_scope() as db:
        ev = db.scalar(select(CalendarEvent).where(CalendarEvent.external_id == "microsoft:ev1"))
        assert (ev.platform, ev.transcript_status, ev.contact_id) == ("teams", "done", _contact("elena.test@example.com").id)


def test_google_calendar_and_meet_transcript_readers(monkeypatch):
    fake = FakeHTTP({
        ("GET", sync.GCAL): {"items": [
            {"id": "g1", "summary": "Siemens sync", "start": {"dateTime": "2026-11-02T10:00:00Z"},
             "end": {"dateTime": "2026-11-02T10:30:00Z"}, "hangoutLink": "https://meet.google.com/abc-defg-hij",
             "attendees": [{"email": "priya.sample@example.com"}]},
            {"id": "g2", "summary": "Holiday", "start": {"date": "2026-11-03"}}]},
        ("GET", f"{sync.MEET}/conferenceRecords/r1/transcripts/t1/entries"): {"transcriptEntries": [
            {"participant": "conferenceRecords/r1/participants/p1", "text": "Forecasts are our pain"}]},
        ("GET", f"{sync.MEET}/conferenceRecords/r1/transcripts"): {"transcripts": [
            {"name": "conferenceRecords/r1/transcripts/t1", "state": "FILE_GENERATED"}]},
        ("GET", f"{sync.MEET}/conferenceRecords/r1/participants"): {"participants": [
            {"name": "conferenceRecords/r1/participants/p1", "signedinUser": {"displayName": "Priya"}}]},
        ("GET", f"{sync.MEET}/conferenceRecords"): {"conferenceRecords": [{"name": "conferenceRecords/r1", "startTime": "x"}]},
    })
    monkeypatch.setattr(http, "request", fake)
    [e] = sync.google_events("tok", NOW)  # all-day item skipped
    assert e["meeting_url"].endswith("abc-defg-hij") and e["attendees"] == ["priya.sample@example.com"]
    name, text = sync.meet_transcript("tok", e["meeting_url"])
    assert name.endswith("t1") and text == "Priya: Forecasts are our pain"
    meet_query = next(kw for m, u, kw in fake.calls if u == f"{sync.MEET}/conferenceRecords")
    assert meet_query["params"]["filter"] == 'space.meeting_code = "abc-defg-hij"'


@pytest.mark.anyio
async def test_gong_calls_imported_with_external_attendees_only(seeded, monkeypatch):
    fake = FakeHTTP({
        ("POST", "https://api.gong.io/v2/calls/extensive"): {"calls": [{
            "metaData": {"id": "g9", "title": "Crocs discovery", "started": "2026-10-30T09:00:00Z"},
            "parties": [{"speakerId": "s1", "name": "Marcus", "emailAddress": "marcus.demo@example.com", "affiliation": "External"},
                        {"speakerId": "s2", "name": "Rep", "emailAddress": "rep@us.example", "affiliation": "Internal"}]}],
            "records": {}},
        ("POST", "https://api.gong.io/v2/calls/transcript"): {"callTranscripts": [{"callId": "g9", "transcript": [
            {"speakerId": "s1", "sentences": [{"text": "Our CRM data is a mess."}]}]}]},
    })
    monkeypatch.setattr(http, "request", fake)
    seen = []

    async def fake_process(**kw):
        seen.append(kw)
        return {"status": "analysed"}
    monkeypatch.setattr(calls, "process_transcript", fake_process)
    with setting(gong_access_key="k", gong_access_secret="s"):
        out = await sync.run_sync()
    assert "1 Gong calls" in out
    assert seen[0]["attendees"] == ["marcus.demo@example.com"] and seen[0]["transcript"] == "Marcus: Our CRM data is a mess."
    assert fake.calls[0][2]["headers"]["Authorization"] == "Basic " + base64.b64encode(b"k:s").decode()


# ----------------------------------------------------------------------------- Slack approvals & command

def _signed(api, path, form: dict, secret="s3cret"):
    body = urllib.parse.urlencode(form).encode()
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
    return api.post(path, content=body, headers={"x-slack-request-timestamp": ts, "x-slack-signature": sig,
                                                 "content-type": "application/x-www-form-urlencoded"})


def test_new_drafts_post_to_slack_and_buttons_approve(seeded, monkeypatch):
    posted, done = [], threading.Event()
    monkeypatch.setattr(slack_bot, "post_approvals", lambda ids: (posted.extend(ids), done.set()))
    with setting(slack_bot_token="xoxb", slack_approvals_channel="C1", slack_signing_secret="s3cret"):
        with session_scope() as db:
            a = approvals.queue(db, kind="email", contact=db.get(Contact, _contact("elena.test@example.com").id),
                                subject="pilot idea", body="Hi Elena, a short note.", created_by_agent="t")
            aid = a.id
        assert done.wait(5) and posted == [aid]
        with session_scope() as db:
            blocks = slack_bot.approval_blocks(db.get(Approval, aid))
            assert blocks[-1]["elements"][0]["value"] == str(aid)

        responses = []
        monkeypatch.setattr(http, "request", lambda m, url, **kw: responses.append((url, kw["json_body"])) or {})
        payload = {"type": "block_actions", "user": {"name": "sam"}, "response_url": "https://hooks.slack.com/r/1",
                   "actions": [{"action_id": "approve", "value": str(aid)}]}
        with TestClient(app) as api:
            r = _signed(api, "/api/integrations/slack/interactions", {"payload": json.dumps(payload)})
            assert r.status_code == 200
            bad = api.post("/api/integrations/slack/interactions", content=b"payload=%7B%7D",
                           headers={"x-slack-request-timestamp": str(int(time.time())), "x-slack-signature": "v0=x"})
            assert bad.status_code == 401
    with session_scope() as db:
        a = db.get(Approval, aid)
        assert a.status == "sent" and a.reviewed_by == "sam (Slack)"
    assert responses[0][0] == "https://hooks.slack.com/r/1" and "approved" in responses[0][1]["text"]


def test_slash_command_starts_copilot(seeded, monkeypatch):
    started = []
    monkeypatch.setattr(jobs, "start", lambda name, desc, work, **kw: started.append((name, desc)) or 7)
    with setting(slack_signing_secret="s3cret"), TestClient(app) as api:
        r = _signed(api, "/api/integrations/slack/commands",
                    {"command": "/revenueos", "text": "brief me on Siemens", "user_name": "sam",
                     "response_url": "https://hooks.slack.com/c/1"})
        assert "On it" in r.json()["text"]
        assert started == [("orchestrator", "brief me on Siemens")]
        assert "Ask me anything" in _signed(api, "/api/integrations/slack/commands", {"text": ""}).json()["text"]


# ----------------------------------------------------------------------------- dossier, committee, holdout

def fake_agent(calls_=None, parsed=None, captured=None):
    async def _run(**kw):
        if captured is not None:
            captured.append(kw)
        tools = {t.to_dict()["name"]: t for t in kw.get("tools", []) if hasattr(t, "to_dict")}
        for name, payloads in (calls_ or {}).items():
            for p in payloads:
                await tools[name].call(p)
        return AgentResult(text="done", run_id=kw.get("run_id") or new_run(kw["agent"], kw["model"], kw["prompt"]),
                           parsed=parsed)
    return _run


DOSSIER = {"summary": "CDO focused on forecasting.", "role_history": ["CDO, Siemens (2024-)"],
           "public_statements": [{"statement": "Forecast accuracy is our top data priority",
                                  "source": "https://example.com/talk", "date": "2026-05"}],
           "priorities": ["Forecast accuracy"], "talking_points": ["Her keynote on forecasting"],
           "communication_style": "data-driven", "sources": ["https://example.com/talk"]}


@pytest.mark.anyio
async def test_dossier_saved_and_used_by_writer(seeded, monkeypatch):
    priya = _contact("priya.sample@example.com")
    monkeypatch.setattr(person_researcher, "run_agent", fake_agent({"submit_dossier": [DOSSIER]}))
    d = await person_researcher.research_person(priya.id)
    assert d["public_statements"][0]["source"] == "https://example.com/talk"
    captured = []
    monkeypatch.setattr(writer, "run_agent", fake_agent(captured=captured))
    await writer.write_sequence(priya.id)
    assert "PERSON DOSSIER" in captured[0]["prompt"] and "top data priority" in captured[0]["prompt"]
    with session_scope() as db:
        assert db.scalar(select(PersonProfile).where(PersonProfile.contact_id == priya.id)).priorities == ["Forecast accuracy"]


@pytest.mark.anyio
async def test_committee_outreach_gives_each_person_their_role(seeded, monkeypatch):
    priya = _contact("priya.sample@example.com")
    with session_scope() as db:
        cfo = Contact(account_id=priya.account_id, first_name="Ravi", last_name="Cfo", title="CFO", email="ravi@example.com")
        db.add(cfo)
        db.flush()
        cfo_id = cfo.id
    plan = committee.CommitteePlan(
        account_narrative="Spreadsheet forecasting costs Siemens inventory; a 4-week assessment proves the fix.",
        stakeholders=[
            committee.StakeholderPlan(contact_id=priya.id, role_in_deal="champion", angle="Her forecasting agenda",
                                      call_to_action="15-minute scoping", start_after_days=0, steps=2),
            committee.StakeholderPlan(contact_id=cfo_id, role_in_deal="finance", angle="Inventory cost and payback",
                                      call_to_action="Review the business case", start_after_days=5, steps=1),
            committee.StakeholderPlan(contact_id=99999, role_in_deal="user", angle="x", call_to_action="y",
                                      start_after_days=1, steps=1)],
        sequencing_rationale="Champion first", missing_roles=["IT security lead"])
    monkeypatch.setattr(committee, "run_agent", fake_agent(parsed=plan))
    monkeypatch.setattr(committee.researcher, "research_account", lambda *a, **k: _noop())
    written = []

    async def fake_write(contact_id, steps=3, instructions=None, **kw):
        written.append((contact_id, steps, instructions))
        return "ok"
    monkeypatch.setattr(writer, "write_sequence", fake_write)
    r = await committee.committee_outreach(priya.account_id, research_people=False)
    assert [w[0] for w in written] == [priya.id, cfo_id]  # unknown contact id ignored
    assert "Spreadsheet forecasting costs Siemens" in written[1][2] and "day 5" in written[1][2]
    assert r["missing_roles"] == ["IT security lead"]


async def _noop():
    return "brief"


def test_holdout_assignment_writer_guard_and_lift(seeded, monkeypatch):
    rolls = iter([0.05, 0.9])  # first contact held out, second treated
    monkeypatch.setattr(workflows.random, "random", lambda: next(rolls))
    priya, elena = _contact("priya.sample@example.com"), _contact("elena.test@example.com")
    assert workflows.assign_group(priya.id) == "holdout"
    assert workflows.assign_group(elena.id) == "treatment"
    assert workflows.assign_group(priya.id) == "holdout"  # sticky
    with pytest.raises(ValueError, match="holdout control group"):
        import asyncio
        asyncio.run(writer.write_sequence(priya.id))
    with session_scope() as db:
        e = db.get(Contact, elena.id)
        e.status = "meeting"
        db.add(Opportunity(account_id=e.account_id, primary_contact_id=e.id, name="Maersk", stage="proposal", amount=50_000))
    with session_scope() as db:
        rep = insights.lift_report(db)
    assert rep["treatment"]["meetings"] == 1 and rep["holdout"]["meetings"] == 0
    assert rep["treatment"]["pipeline"] == 50_000 and "Collecting data" in rep["verdict"]
    with TestClient(app) as api:
        assert api.post(f"/api/leads/{priya.id}/release-holdout").json()["experiment_group"] == "released"
        assert api.get("/api/insights/lift").json()["holdout"]["contacts"] == 0
