"""Phase 0 foundations + sequence engine + sending service (plan Track B1, Track C1)."""
import base64
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select

from revenueos import jobs, seed
from revenueos.agents import base
from revenueos.config import settings
from revenueos.db import engine, session_scope
from revenueos.integrations import http
from revenueos.main import app
from revenueos.models import Activity, AgentRun, Approval, Contact, EmailEvent, Enrollment, SchedulerLease, Task
from revenueos.services import approvals, email_events, guardrails, leases, sequences
from revenueos.tools import email as email_tool

UTC = timezone.utc


@pytest.fixture
def api():
    with TestClient(app) as client:
        yield client


@pytest.fixture
def seeded():
    seed.main()


def _set(**values):
    for k, v in values.items():
        object.__setattr__(settings, k, v)


def _cid(email="priya.sample@example.com") -> int:
    with session_scope() as db:
        return db.scalar(select(Contact.id).where(Contact.email == email))


def _queue_sequence(cid: int, steps=((1, 0, "email"), (2, 3, "email"))) -> list[int]:
    ids = []
    with session_scope() as db:
        c = db.get(Contact, cid)
        for step, days, kind in steps:
            ids.append(approvals.queue(db, kind=kind, contact=c, subject=f"step {step}" if kind == "email" else None,
                                       body=f"Hi Priya, note number {step}.", sequence_step=step,
                                       send_after_days=days, created_by_agent="outreach_writer", enroll=True).id)
    return ids


def _approve(*ids):
    with session_scope() as db:
        for i in ids:
            approvals.approve_and_send(db, db.get(Approval, i), "rep")


# ------------------------------------------------------------------ send windows

def test_send_window_respects_time_zone_and_weekends():
    _set(send_window_start_hour=8, send_window_end_hour=18, send_weekdays_only=True)
    berlin = sequences.contact_tz(SimpleNamespace(timezone="Europe/Berlin"))
    # Friday 17:30 UTC = 18:30 in Berlin (after hours) -> Monday 08:00 Berlin = 06:00 UTC (winter time).
    friday_evening = datetime(2026, 11, 6, 17, 30, tzinfo=UTC)
    assert sequences.next_send_time(friday_evening, berlin) == datetime(2026, 11, 9, 7, 0, tzinfo=UTC)
    # Tuesday 03:00 UTC = 04:00 Berlin (too early) -> 08:00 Berlin same day.
    assert sequences.next_send_time(datetime(2026, 11, 10, 3, 0, tzinfo=UTC), berlin) == datetime(2026, 11, 10, 7, 0, tzinfo=UTC)
    # Inside the window: unchanged.
    noon = datetime(2026, 11, 10, 11, 0, tzinfo=UTC)
    assert sequences.next_send_time(noon, berlin) == noon
    # Unknown zone falls back to the default.
    assert sequences.contact_tz(SimpleNamespace(timezone="Mars/Olympus")).key == "UTC"


# ------------------------------------------------------------------ sequence engine

def test_follow_up_waits_for_first_send_then_goes_out_on_schedule(seeded):
    _set(send_weekdays_only=False, send_window_start_hour=0, send_window_end_hour=24)
    cid = _cid()
    s1, s2 = _queue_sequence(cid)
    with session_scope() as db:
        e = db.get(Enrollment, db.get(Approval, s1).enrollment_id)
        assert e.status == "active" and db.get(Approval, s2).enrollment_id == e.id
    _approve(s2)  # follow-up approved first: nothing goes out before step 1
    with session_scope() as db:
        assert sequences.advance_all(db) == 0
        assert db.get(Approval, s2).status == "approved"
    _approve(s1)  # step 1 sends immediately on approval and starts the clock
    with session_scope() as db:
        e = db.get(Enrollment, db.get(Approval, s1).enrollment_id)
        assert db.get(Approval, s1).status == "sent" and e.started_at is not None
        assert sequences.advance_all(db) == 0  # day 0: step 2 is due on day 3
        assert e.next_due_at is not None
        assert sequences.advance_all(db, datetime.now(UTC) + timedelta(days=3, minutes=1)) == 1
        assert db.get(Approval, s2).status == "sent" and e.status == "completed"


def test_reply_pauses_out_of_office_resumes_after_return_date(seeded, api):
    _set(send_weekdays_only=False, send_window_start_hour=0, send_window_end_hour=24)
    cid = _cid()
    s1, s2 = _queue_sequence(cid)
    _approve(s1, s2)
    r = api.post("/api/webhooks/reply", json={"from_email": "priya.sample@example.com",
                                               "text": "I'm out of office until Monday.", "triage": False})
    assert r.json()["cancelled_followups"] == 1
    with session_scope() as db:
        e = db.get(Enrollment, db.get(Approval, s1).enrollment_id)
        assert e.status == "paused"
        # What Reply Triage does for an out-of-office (agents/replies.py): restore + resume after return.
        db.get(Approval, s2).status = "approved"
        back = datetime.now(UTC) + timedelta(days=10)
        sequences.resume_contact(db, cid, resume_at=back)
    with session_scope() as db:
        assert sequences.advance_all(db, datetime.now(UTC) + timedelta(days=4)) == 0  # due, but they are away
        assert sequences.advance_all(db, datetime.now(UTC) + timedelta(days=10, minutes=1)) == 1


def test_stop_cancels_unsent_steps_and_tasks(seeded, api):
    cid = _cid()
    s1, s2 = _queue_sequence(cid)
    with session_scope() as db:
        task = sequences.add_task_step(db, db.get(Contact, cid), sequence_step=3, offset_days=5, title="Call Priya")
        eid, tid = task.enrollment_id, task.id
    r = api.post(f"/api/enrollments/{eid}/stop", json={"reason": "Not a fit after all"})
    assert r.status_code == 200 and r.json()["status"] == "stopped"
    with session_scope() as db:
        assert {db.get(Approval, i).status for i in (s1, s2)} == {"rejected"}
        assert db.get(Task, tid).status == "skipped"


def test_linkedin_and_call_steps_become_dated_tasks(seeded, api):
    _set(send_weekdays_only=False, send_window_start_hour=0, send_window_end_hour=24)
    cid = _cid()
    s1, li = _queue_sequence(cid, steps=((1, 0, "email"), (2, 2, "linkedin_message")))
    with session_scope() as db:
        sequences.add_task_step(db, db.get(Contact, cid), sequence_step=3, offset_days=4, title="Call Priya")
    _approve(s1, li)
    with session_scope() as db:
        sequences.advance_all(db)
        tasks = db.scalars(select(Task).order_by(Task.sequence_step)).all()
        assert [t.type for t in tasks] == ["linkedin", "call"]
        sent = db.get(Approval, s1).sent_at.replace(tzinfo=UTC)
        assert tasks[0].due_at.replace(tzinfo=UTC) >= sent + timedelta(days=2)
        assert tasks[1].due_at.replace(tzinfo=UTC) >= sent + timedelta(days=4)
    assert len(api.get("/api/tasks", params={"due": "scheduled"}).json()) == 2
    t = api.get("/api/tasks").json()[0]
    assert api.patch(f"/api/tasks/{t['id']}", json={"status": "done", "outcome": "Sent"}).json()["status"] == "done"


# ------------------------------------------------------------------ double-send protection

def test_a_draft_can_only_be_claimed_once(seeded):
    cid = _cid()
    with session_scope() as db:
        aid = approvals.queue(db, kind="email", contact=db.get(Contact, cid), subject="hello",
                              body="Hi Priya.", created_by_agent="test").id
    with session_scope() as db:
        a = db.get(Approval, aid)
        assert approvals.claim_and_send(db, a, from_status="pending", commit=True)
        assert a.status == "sent"
    with session_scope() as db:  # a second worker with a stale view gets nothing
        assert not approvals.claim_and_send(db, db.get(Approval, aid), from_status="pending")
    with session_scope() as db:
        assert db.scalar(select(Activity.id).where(Activity.type == "email_sent")) is not None
        assert len(db.scalars(select(Activity).where(Activity.type == "email_sent")).all()) == 1


def test_interrupted_sends_are_failed_not_resent(seeded):
    cid = _cid()
    with session_scope() as db:
        a = approvals.queue(db, kind="email", contact=db.get(Contact, cid), subject="x", body="Hi.",
                            created_by_agent="test")
        a.status, a.claimed_at = "sending", datetime.now(UTC) - timedelta(hours=1)
        aid = a.id
    with session_scope() as db:
        assert approvals.sweep_interrupted_sends(db) == 1
        assert db.get(Approval, aid).status == "failed"


def test_scheduler_lease_has_one_holder_and_keeps_state(seeded):
    with session_scope() as db:
        assert leases.acquire(db, "scheduler", 60, holder="worker-a") == {}
        assert leases.acquire(db, "scheduler", 60, holder="worker-b") is None
        assert leases.save_state(db, "scheduler", {"hygiene_day": "2026-11-07"}, holder="worker-a")
        assert leases.acquire(db, "scheduler", 60, holder="worker-a") == {"hygiene_day": "2026-11-07"}
        db.get(SchedulerLease, "scheduler").expires_epoch = 0  # worker-a died
        db.commit()
        assert leases.acquire(db, "scheduler", 60, holder="worker-b") == {"hygiene_day": "2026-11-07"}
    assert jobs._decode(jobs._encode({"hygiene_day": datetime(2026, 11, 7).date()})) == {
        "hygiene_day": datetime(2026, 11, 7).date()}


def test_stuck_runs_are_marked_failed(seeded):
    with session_scope() as db:
        old = AgentRun(agent="x", model="m", input="i", started_at=datetime.now(UTC) - timedelta(hours=3))
        new = AgentRun(agent="y", model="m", input="i")
        db.add_all([old, new])
        db.flush()
        ids = (old.id, new.id)
    with session_scope() as db:
        assert jobs.sweep_stuck_runs(db) == 1
        assert db.get(AgentRun, ids[0]).status == "failed" and db.get(AgentRun, ids[1]).status == "running"


def test_migrations_stamp_the_database():
    assert "alembic_version" in inspect(engine).get_table_names()
    with engine.connect() as conn:
        assert conn.exec_driver_sql("select version_num from alembic_version").scalar() == "0001_baseline"


# ------------------------------------------------------------------ retries

@pytest.mark.anyio
async def test_cut_off_answer_is_retried_with_more_room(seeded, monkeypatch):
    calls = []

    class Messages:
        def stream(self, **params):
            calls.append(params["max_tokens"])

            class S:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *exc):
                    return False

                async def get_final_message(self):
                    usage = SimpleNamespace(input_tokens=10, output_tokens=10, cache_read_input_tokens=0,
                                            cache_creation_input_tokens=0)
                    stop = "max_tokens" if len(calls) == 1 else "end_turn"
                    return SimpleNamespace(content=[SimpleNamespace(type="text", text="done")], usage=usage,
                                           stop_reason=stop)
            return S()

    monkeypatch.setattr(base, "client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=Messages())))
    r = await base.run_agent(agent="t", model="claude-haiku-5-5", role_prompt="r", prompt="p", max_tokens=1000)
    assert calls == [1000, 2000] and r.text == "done"


def test_http_retries_rate_limits(monkeypatch):
    import urllib.error

    attempts = []

    def fake_urlopen(req, timeout):
        attempts.append(req.get_method())
        if len(attempts) < 3:
            raise urllib.error.HTTPError(req.full_url, 429, "slow down", {"Retry-After": "0"}, None)

        class R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"ok": true}'
        return R()

    monkeypatch.setattr(http.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    assert http.request("POST", "https://api.example.com/x", json_body={}) == {"ok": True}
    assert attempts == ["POST", "POST", "POST"]


# ------------------------------------------------------------------ sending service

def test_sendgrid_send_includes_html_tracking_and_approval_id(seeded, monkeypatch):
    _set(email_provider="sendgrid", sendgrid_api_key="SG.test", email_tracking=True)
    sent = []
    monkeypatch.setattr(http, "request", lambda method, url, **kw: sent.append((method, url, kw)) or {})
    cid = _cid()
    with session_scope() as db:
        aid = approvals.queue(db, kind="email", contact=db.get(Contact, cid), subject="hello",
                              body="Hi Priya,\n\nSee https://example.com/x", created_by_agent="test").id
    _approve(aid)
    method, url, kw = sent[0]
    body = kw["json_body"]
    assert url.endswith("/v3/mail/send")
    assert body["personalizations"][0]["custom_args"] == {"approval_id": str(aid)}
    html = body["content"][1]["value"]
    assert body["content"][1]["type"] == "text/html" and '<a href="https://example.com/x?utm_source=revenueos' in html
    # Our own domain (the sender's) gets UTM tags; the stored draft keeps the clean link.
    assert f"utm_content=approval-{aid}" in body["content"][0]["value"]
    assert body["tracking_settings"]["open_tracking"]["enable"] is True
    with session_scope() as db:
        assert db.get(Approval, aid).provider == "sendgrid"
    assert email_tool.active_provider() == "sendgrid"


def test_hard_bounce_suppresses_and_stops_the_sequence(seeded, api):
    _set(webhook_secret="hook-secret")
    cid = _cid()
    s1, s2 = _queue_sequence(cid)
    _approve(s1, s2)
    events = [{"event": "bounce", "type": "bounce", "email": "priya.sample@example.com", "approval_id": str(s1),
               "reason": "550 no such user", "sg_event_id": "ev1", "timestamp": 1794046000},
              {"event": "open", "email": "priya.sample@example.com", "approval_id": str(s1), "sg_event_id": "ev2"}]
    assert api.post("/api/webhooks/email/sendgrid", json=events).status_code == 401
    r = api.post("/api/webhooks/email/sendgrid?token=hook-secret", json=events)
    assert r.json() == {"received": 2, "processed": {"bounce": 1, "open": 1}}
    again = api.post("/api/webhooks/email/sendgrid?token=hook-secret", json=events)
    assert again.json()["processed"] == {}  # provider retries are ignored
    with session_scope() as db:
        assert guardrails.is_suppressed(db, "priya.sample@example.com")
        assert db.get(Approval, s2).status == "rejected"
        assert db.get(Enrollment, db.get(Approval, s1).enrollment_id).status == "stopped"
        assert len(db.scalars(select(EmailEvent)).all()) == 2


def test_postmark_complaint_and_click(seeded, api):
    _set(webhook_secret="hook-secret")
    H = {"X-Webhook-Secret": "hook-secret"}
    click = {"RecordType": "Click", "Recipient": "marcus.demo@example.com", "OriginalLink": "https://example.com/case"}
    assert api.post("/api/webhooks/email/postmark", json=click, headers=H).json()["processed"] == {"click": 1}
    spam = {"RecordType": "SpamComplaint", "Email": "marcus.demo@example.com", "ID": 42}
    assert api.post("/api/webhooks/email/postmark", json=spam, headers=H).json()["processed"] == {"complaint": 1}
    with session_scope() as db:
        c = db.get(Contact, _cid("marcus.demo@example.com"))
        assert c.status == "disqualified" and guardrails.is_suppressed(db, c.email)
        types = {a.type for a in db.scalars(select(Activity).where(Activity.contact_id == c.id))}
        assert {"email_click", "email_complaint"} <= types


def test_sendgrid_signature_verification():
    key = ec.generate_private_key(ec.SECP256R1())
    pub = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
    payload, ts = json.dumps([{"event": "open"}]).encode(), "1794046000"
    sig = base64.b64encode(key.sign(ts.encode() + payload, ec.ECDSA(hashes.SHA256()))).decode()
    assert email_events.verify_sendgrid(pub, payload, sig, ts)
    assert not email_events.verify_sendgrid(pub, payload + b" ", sig, ts)
