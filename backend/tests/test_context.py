"""Unified customer context: Slack, call transcripts, calendar, memory, leak guardrail, personalisation gate."""
import hashlib
import hmac
import json
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from revenueos import seed
from revenueos.agents import context as context_agent
from revenueos.agents import deals, writer
from revenueos.agents.base import AgentResult, new_run
from revenueos.config import settings
from revenueos.db import session_scope
from revenueos.integrations import calendar, calls, slack
from revenueos.main import app
from revenueos.models import Account, Approval, CalendarEvent, Contact, ContextEvent, SlackChannel
from revenueos.services import approvals, guardrails, memory, personalisation

NOW = datetime.now(timezone.utc)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def seeded():
    seed.main()


@contextmanager
def setting(**values):
    """Settings is frozen; tests change a value and always restore it."""
    old = {k: getattr(settings, k) for k in values}
    for k, v in values.items():
        object.__setattr__(settings, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            object.__setattr__(settings, k, v)


def _account(name: str) -> Account:
    with session_scope() as db:
        return db.scalar(select(Account).where(Account.name == name))


def _contact(email: str) -> Contact:
    with session_scope() as db:
        return db.scalar(select(Contact).where(Contact.email == email))


def _slack_sig(secret: str, ts: str, body: bytes) -> str:
    return "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()


# ----------------------------------------------------------------------------- Slack

def test_slack_signature_checks():
    body, ts = b'{"a":1}', str(int(time.time()))
    good = _slack_sig("s3cret", ts, body)
    assert slack.verify_signature("s3cret", ts, body, good)
    assert not slack.verify_signature("s3cret", ts, body + b"x", good)          # tampered
    assert not slack.verify_signature("wrong", ts, body, good)                   # wrong secret
    old = str(int(time.time()) - 3600)
    assert not slack.verify_signature("s3cret", old, body, _slack_sig("s3cret", old, body))  # replay


def test_slack_reads_only_opted_in_channels_and_maps_accounts(seeded):
    siemens = _account("Siemens")
    with session_scope() as db:
        db.add_all([SlackChannel(channel_id="CDEAL", name="deal-siemens", account_id=siemens.id),
                    SlackChannel(channel_id="CSHARED", name="ext-maersk", shared=True),
                    SlackChannel(channel_id="COFF", name="old", enabled=False)])
    msg = lambda ch, text, ts="1.1": {"type": "message", "channel": ch, "user": "U1", "text": text, "ts": ts}  # noqa: E731
    with session_scope() as db:
        assert slack.ingest_message(db, msg("CRANDOM", "Siemens news")) == "ignored: channel not opted in"
        assert slack.ingest_message(db, msg("COFF", "Siemens news")) == "ignored: channel not opted in"
        assert slack.ingest_message(db, msg("CDEAL", "Their CFO is sceptical about budget")) == "stored"
        assert slack.ingest_message(db, msg("CDEAL", "Their CFO is sceptical about budget")) == "duplicate"
        assert slack.ingest_message(db, msg("CSHARED", "Can Maersk see the pilot plan?", "2.2")) == "stored"
        assert slack.ingest_message(db, msg("CSHARED", "general chat", "3.3")) == "stored: no account matched"
        assert slack.ingest_message(db, {**msg("CDEAL", "x", "4.4"), "bot_id": "B1"}).startswith("ignored")
    with session_scope() as db:
        rows = {e.raw_text: e for e in db.scalars(select(ContextEvent).where(ContextEvent.source == "slack"))}
        assert rows["Their CFO is sceptical about budget"].internal is True
        assert rows["Their CFO is sceptical about budget"].account_id == siemens.id
        assert rows["Can Maersk see the pilot plan?"].internal is False      # shared channel = customer-visible
        assert rows["Can Maersk see the pilot plan?"].account_id == _account("Maersk").id


def test_slack_endpoint_verifies_and_answers_challenge(seeded):
    with setting(slack_signing_secret="s3cret"), TestClient(app) as api:
        body = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
        ts = str(int(time.time()))
        r = api.post("/api/integrations/slack/events", content=body,
                     headers={"x-slack-request-timestamp": ts, "x-slack-signature": _slack_sig("s3cret", ts, body)})
        assert r.json() == {"challenge": "abc"}
        r = api.post("/api/integrations/slack/events", content=body,
                     headers={"x-slack-request-timestamp": ts, "x-slack-signature": "v0=bad"})
        assert r.status_code == 401


@pytest.mark.anyio
async def test_slack_digest_summarises_per_account_and_marks_processed(seeded, monkeypatch):
    siemens = _account("Siemens")
    with session_scope() as db:
        db.add(SlackChannel(channel_id="CDEAL", account_id=siemens.id))
        for i, t in enumerate(["CFO wants ROI case by Friday", "Legal asks for a DPA"]):
            slack.ingest_message(db, {"type": "message", "channel": "CDEAL", "user": "U1", "text": t, "ts": f"{i}.0"})
    digest = context_agent.Digest(summary="Finance needs an ROI case; legal wants a DPA.",
                                  commitments=["Send ROI case by Friday (us)"], objections=[],
                                  stakeholders=["CFO, sceptical"], risks=["Legal review"], open_questions=[])

    async def fake(**kw):
        assert "CFO wants ROI case" in kw["prompt"] and "Internal team channel" in kw["prompt"]
        return AgentResult(text="", run_id=new_run(kw["agent"], kw["model"], kw["prompt"]), parsed=digest)
    monkeypatch.setattr(context_agent, "run_agent", fake)
    assert (await context_agent.digest_slack())["digests"] == 1
    with session_scope() as db:
        raw = list(db.scalars(select(ContextEvent).where(ContextEvent.source_ref.like("slack:%"))))
        assert all(e.processed for e in raw)
    block = memory.context_block(siemens.id)
    assert "INTERNAL TEAM NOTES" in block and "Send ROI case by Friday" in block
    assert "Finance needs an ROI case" not in memory.context_block(siemens.id, include_internal=False)


def test_internal_slack_text_is_blocked_from_drafts(seeded):
    priya = _contact("priya.sample@example.com")
    with session_scope() as db:
        memory.record(db, source="slack", source_ref="slack:C:9", account_id=priya.account_id, internal=True,
                      raw_text="honestly their CFO thinks our pricing is way too high for this", processed=True)
        c = db.get(Contact, priya.id)
        leaky = approvals.queue(db, kind="email", contact=c, subject="pricing", created_by_agent="t",
                                body="Hi Priya, I hear your CFO thinks our pricing is way too high for this year.")
        fine = approvals.queue(db, kind="email", contact=c, subject="pricing", created_by_agent="t",
                               body="Hi Priya, happy to walk your finance team through the business case.")
        assert any(f["code"] == "internal_leak" and f["severity"] == "block" for f in leaky.guardrail_flags)
        assert not any(f["code"] == "internal_leak" for f in fine.guardrail_flags)


# ----------------------------------------------------------------------------- calls

def test_vtt_and_zoom_helpers():
    vtt = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:04.000\nPriya: We need better forecasts\n\n2\n00:00:05.000 --> 00:00:07.000\nYou: Understood"
    assert calls.parse_vtt(vtt) == "Priya: We need better forecasts\nYou: Understood"
    assert calls.zoom_url_validation("k", "tok")["encryptedToken"] == hmac.new(b"k", b"tok", hashlib.sha256).hexdigest()
    body, ts = b'{"event":"x"}', str(int(time.time()))
    sig = "v0=" + hmac.new(b"k", f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    assert calls.zoom_signature_ok("k", ts, body, sig) and not calls.zoom_signature_ok("k", ts, body, "v0=no")
    event = {"download_token": "dt", "payload": {"object": {"id": 8812, "uuid": "u1", "topic": "Siemens sync",
             "host_email": "rep@us.example", "start_time": "2026-10-30T10:00:00Z",
             "recording_files": [{"id": "f1", "file_type": "TRANSCRIPT", "download_url": "https://zoom/x"},
                                 {"id": "f2", "file_type": "MP4", "download_url": "https://zoom/v"}]}}}
    [t] = calls.zoom_transcripts(event)
    assert (t["meeting_ref"], t["download_token"], t["external_id"]) == ("8812", "dt", "zoom:u1:f1")


@pytest.mark.anyio
async def test_transcript_routing_matched_unmatched_duplicate(seeded, monkeypatch):
    priya = _contact("priya.sample@example.com")
    analysed = []

    async def fake_analyze(contact_id, transcript, **kw):
        analysed.append((contact_id, kw["source_ref"]))
        with session_scope() as db:
            memory.record(db, source="call", source_ref=kw["source_ref"], contact_id=contact_id,
                          summary="Discussed forecasting", processed=True)
        return {"stage": "discovery", "health": "amber", "summary": "Discussed forecasting"}
    monkeypatch.setattr(deals, "analyze_call", fake_analyze)

    text = "Priya: our forecasts are in spreadsheets and it hurts. " * 3
    r = await calls.process_transcript(source="teams", external_id="teams:1", transcript=text,
                                       attendees=["rep@us.example", "PRIYA.SAMPLE@example.com"])
    assert r["status"] == "analysed" and analysed == [(priya.id, "call:teams:1")]
    assert (await calls.process_transcript(source="teams", external_id="teams:1", transcript=text))["status"] == "duplicate"

    # Meeting id resolves through the calendar entry
    with session_scope() as db:
        calendar.upsert_events(db, [{"id": "g1", "title": "Sync", "start": (NOW + timedelta(days=3)).isoformat(),
                                     "attendees": ["elena.test@example.com"], "meeting_url": "https://zoom.us/j/8812"}])
    r = await calls.process_transcript(source="zoom", external_id="zoom:u1:f1", transcript=text, meeting_ref="8812")
    assert r["status"] == "analysed" and analysed[-1][0] == _contact("elena.test@example.com").id

    r = await calls.process_transcript(source="meet", external_id="meet:9", transcript=text, title="Weekly sync")
    assert r["status"] == "unmatched"
    with TestClient(app) as api:
        [u] = api.get("/api/integrations/unmatched").json()
        assert u["source"] == "call"
        assert api.post(f"/api/integrations/events/{u['id']}/assign",
                        json={"account_id": priya.account_id}).status_code == 422  # a call needs its contact
        r = api.post(f"/api/integrations/events/{u['id']}/assign",
                     json={"account_id": priya.account_id, "contact_id": priya.id})
        assert "run_id" in r.json()


def test_transcript_endpoint_requires_secret_when_set(seeded):
    with setting(webhook_secret="hook"), TestClient(app) as api:
        body = {"source": "upload", "external_id": "1", "transcript": "x" * 60}
        assert api.post("/api/integrations/transcripts", json=body).status_code == 401
        assert api.post("/api/integrations/transcripts", json=body, headers={"X-Webhook-Secret": "hook"}).status_code == 200


# ----------------------------------------------------------------------------- calendar

@pytest.mark.anyio
async def test_calendar_matches_customers_and_preps_before_meeting(seeded, monkeypatch):
    with session_scope() as db:
        r = calendar.upsert_events(db, [
            {"id": "e1", "title": "Siemens pilot scoping", "start": (NOW + timedelta(hours=5)).isoformat(),
             "attendees": ["me@us.example", "priya.sample@example.com"]},
            {"id": "e2", "title": "Team standup", "start": (NOW + timedelta(hours=2)).isoformat(),
             "attendees": ["me@us.example"]},
            {"id": "e3", "title": "Maersk QBR", "start": (NOW + timedelta(days=5)).isoformat(),
             "attendees": ["elena.test@example.com"]},
        ])
        assert r == {"received": 3, "customer_meetings": 2}
        due = calendar.due_for_prep(db)
        assert [db.get(CalendarEvent, i).external_id for i in due] == ["e1"]  # within 24h, customer, pending
    preps = []

    async def fake_prep(contact_id, goal, attendees, when, parent_run_id=None):
        preps.append((contact_id, goal))
        return 123

    monkeypatch.setattr(deals, "prep_meeting", fake_prep)
    out = await calendar.run_due_preps()
    assert out.startswith("Prepared 1 of 1") and preps == [(_contact("priya.sample@example.com").id, "Siemens pilot scoping")]
    with session_scope() as db:
        assert db.scalar(select(CalendarEvent).where(CalendarEvent.external_id == "e1")).prep_status == "done"
        assert calendar.due_for_prep(db) == []  # not prepped twice


# ----------------------------------------------------------------------------- personalisation gate

@pytest.mark.anyio
async def test_personalisation_gate_rewrites_once_then_flags(seeded, monkeypatch):
    priya = _contact("priya.sample@example.com")
    scores = iter([30, 40, 85])

    async def fake_grade(**kw):
        return personalisation.Grade(score=next(scores), specific_facts_used=[], unsupported_claims=[],
                                     generic_phrases=["hope this finds you well"], feedback="Use her forecasting remarks.")
    monkeypatch.setattr(personalisation, "grade", fake_grade)
    tool = writer.make_queue_tool({priya.id}, new_run("t", "m", "p"), grounding="brief")
    args = {"contact_id": priya.id, "channel": "email", "sequence_step": 1, "send_after_days": 0,
            "subject": "hello", "body": "Hope this finds you well.", "rationale": "r"}
    first = await tool.call(args)
    assert "NOT queued" in str(first) and "forecasting" in str(first)
    second = await tool.call(args)   # still generic after the rewrite: queued, flagged
    assert "Queued as approval" in str(second)
    third = await tool.call({**args, "sequence_step": 2, "send_after_days": 3})
    with session_scope() as db:
        rows = list(db.scalars(select(Approval).order_by(Approval.id)))
        assert rows[0].personalisation_score == 40
        assert any(f["code"] == "low_personalisation" for f in rows[0].guardrail_flags)
        assert rows[1].personalisation_score == 85
        assert not any(f["code"] == "low_personalisation" for f in rows[1].guardrail_flags)
    assert "Queued" in str(third)


# ----------------------------------------------------------------------------- memory

@pytest.mark.anyio
async def test_call_analysis_lands_on_timeline_with_commitments(seeded, monkeypatch):
    from tests.test_phase2 import CALL, fake_run_agent

    priya = _contact("priya.sample@example.com")
    monkeypatch.setattr(deals, "run_agent", fake_run_agent(CALL))
    await deals.analyze_call(priya.id, "Transcript text " * 10, source_ref="call:zoom:abc")
    with session_scope() as db:
        ev = db.scalar(select(ContextEvent).where(ContextEvent.source_ref == "call:zoom:abc"))
        assert ev.summary == CALL.summary and ev.facts["commitments"] == CALL.next_steps
    assert "Send assessment outline by Friday" in memory.context_block(priya.account_id)


def test_retention_purges_raw_text_but_keeps_summaries(seeded):
    with session_scope() as db:
        memory.record(db, source="slack", source_ref="old", account_id=_account("Siemens").id, raw_text="secret",
                      summary="kept", processed=True, occurred_at=NOW - timedelta(days=400))
        assert memory.purge_raw_text(db) == 1
    with session_scope() as db:
        ev = db.scalar(select(ContextEvent).where(ContextEvent.source_ref == "old"))
        assert ev.raw_text is None and ev.summary == "kept"


def test_timeline_and_notes_api(seeded):
    acc = _account("Siemens")
    with TestClient(app) as api:
        r = api.post(f"/api/accounts/{acc.id}/notes", json={"text": "Priya prefers short emails"})
        assert r.json()["internal"] is True
        assert api.get(f"/api/accounts/{acc.id}/timeline").json()[0]["summary"] == "Priya prefers short emails"
        assert api.get("/api/integrations/status").json()["slack"]["configured"] is False
