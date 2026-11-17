"""Phase B: activity capture and contact suggestions, quick updates, forecast calibration and slip prediction,
meeting outcomes, reminders and funnel, relationships and job changes."""
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from revenueos import seed
from revenueos.agents import deals
from revenueos.config import settings
from revenueos.db import session_scope
from revenueos.integrations import calendar, http, mail
from revenueos.main import app
from revenueos.models import (Activity, Approval, CalendarEvent, Connection, Contact, ContactSuggestion, ContextEvent,
                              ForecastSnapshot, Opportunity, Signal, Task)
from revenueos.services import approvals, pipeline_intel

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


def _priya(db) -> Contact:
    return db.scalar(select(Contact).where(Contact.email == "priya.sample@example.com"))


def _deal(db, **kw) -> Opportunity:
    p = _priya(db)
    o = Opportunity(account_id=p.account_id, primary_contact_id=p.id, name=kw.pop("name", "Siemens pilot"),
                    stage=kw.pop("stage", "proposal"), amount=kw.pop("amount", 100000), **kw)
    db.add(o)
    db.flush()
    return o


# ------------------------------------------------------------------ 5. zero-entry CRM

def test_rep_email_is_logged_and_colleagues_are_suggested(seeded, api):
    with session_scope() as db:
        o = _deal(db, last_activity_at=datetime.now(UTC) - timedelta(days=20))
        oid = o.id
        r = mail.process_message(db, "google", {
            "id": "m1", "from": "ankit@us.example", "to": ["priya.sample@example.com"],
            "cc": ["dana.cfo@example.com", "friend@gmail.com"], "subject": "Pilot scope",
            "text": "Hi Priya, attached is the pilot scope we discussed.", "sent_at": datetime.now(UTC)}, "ankit@us.example")
        assert r == "outbound"
    with session_scope() as db:
        p = _priya(db)
        assert db.scalar(select(Activity).where(Activity.contact_id == p.id, Activity.type == "rep_email"))
        assert (datetime.now(UTC) - db.get(Opportunity, oid).last_activity_at.replace(tzinfo=UTC)).days == 0
        s = db.scalars(select(ContactSuggestion)).all()
        assert [x.email for x in s] == ["dana.cfo@example.com"]  # same company only; free-mail ignored
    sid = api.get("/api/suggestions").json()[0]["id"]
    added = api.post(f"/api/suggestions/{sid}/add").json()
    with session_scope() as db:
        assert db.get(Contact, added["contact_id"]).email == "dana.cfo@example.com"


@pytest.mark.anyio
async def test_quick_update_updates_the_deal_without_drafting_an_email(seeded, monkeypatch):
    from tests.test_phase2 import CALL, fake_run_agent

    monkeypatch.setattr(deals, "run_agent", fake_run_agent(CALL))
    with session_scope() as db:
        pid = _priya(db).id
    r = await deals.analyze_call(pid, "Rep's quick update: pilot agreed, CFO joins next call.", follow_up=False)
    assert r["approval_id"] is None and r["opportunity_id"]
    with session_scope() as db:
        assert db.scalar(select(Approval.id)) is None
        assert db.scalar(select(Task).where(Task.created_by == "call_analyst"))


# ------------------------------------------------------------------ 6. forecasting

def test_close_date_pushes_and_slip_flags(seeded):
    with session_scope() as db:
        o = _deal(db, close_date="2026-11-30", last_activity_at=datetime.now(UTC) - timedelta(days=20))
        o.close_date = "2026-12-15"
        o.close_date = "2026-12-31"
        o.close_date = "2026-12-20"  # earlier: not a push
        assert o.close_date_pushes == 2
        oid = o.id
    with session_scope() as db:
        assert pipeline_intel.flag_slips(db) == 1
        o = db.get(Opportunity, oid)
        assert o.slip_risk == "high" and any("pushed 2 times" in r for r in o.slip_reasons)
        assert pipeline_intel.flag_slips(db) == 0  # one task per fortnight
        assert db.scalar(select(Task).where(Task.created_by == "slip_predictor")).opportunity_id == oid


def test_forecast_accuracy_compares_snapshots_with_what_closed(seeded):
    with session_scope() as db:
        a = _deal(db, name="A", amount=100000, forecast_category="commit", rep_forecast_category="commit",
                  close_date="2026-12-01", owner="anna@us.example")
        b = _deal(db, name="B", amount=50000, forecast_category="commit", rep_forecast_category="best_case",
                  close_date="2026-12-10", owner="anna@us.example")
        snap = pipeline_intel.take_snapshot(db, now=datetime(2026, 11, 10, tzinfo=UTC))
        period = snap.period
        assert snap.commit == 150000
        a.stage = "closed_won"
        a.closed_at = datetime(2026, 12, 2, tzinfo=UTC)
        b.stage = "closed_lost"
        b.closed_at = datetime(2026, 12, 12, tzinfo=UTC)
    with session_scope() as db:
        r = pipeline_intel.accuracy(db, period)
    assert r["actual_won"] == 100000
    assert r["mid_quarter_accuracy"]["commit"] == 50.0  # forecast 150k, closed 100k
    assert r["ai_commit"] == {"commit_calls": 2, "closed": 1, "hit_rate": 50.0}
    assert r["rep_commit"] == {"commit_calls": 1, "closed": 1, "hit_rate": 100.0}  # the rep called it better


# ------------------------------------------------------------------ 7. meetings

def test_meetings_are_logged_reminded_and_counted(seeded, api):
    _set(send_weekdays_only=False, send_window_start_hour=0, send_window_end_hour=24)
    soon = datetime.now(UTC) + timedelta(hours=20)
    with session_scope() as db:
        calendar.upsert_events(db, [{"id": "ev1", "title": "Pilot scoping", "start": soon.isoformat(),
                                     "attendees": ["priya.sample@example.com", "ankit@us.example"]}])
        ev = db.scalar(select(CalendarEvent))
        assert ev.crm_logged and db.scalar(select(Activity).where(Activity.type == "meeting"))
        eid = ev.id
    t = api.post("/api/meetings/reminder-template", json={
        "subject": "See you {{meeting_time}}", "body": "Hi {{first_name}}, looking forward to {{meeting_title}}."}).json()
    assert t["flags"] == []  # merge fields are allowed in templates
    with session_scope() as db:
        assert pipeline_intel.send_reminders(db) == 0  # template not approved yet
    api.post(f"/api/approvals/{t['approval_id']}/approve", json={})
    with session_scope() as db:
        assert pipeline_intel.send_reminders(db) == 1
        assert pipeline_intel.send_reminders(db) == 0  # once per meeting
        sent = db.scalar(select(Approval).where(Approval.created_by_agent == "meeting_reminder"))
        assert sent.status == "sent" and "Pilot scoping" in sent.body and "Hi Priya" in sent.body
    assert api.put(f"/api/meetings/{eid}/outcome", json={"outcome": "no_show"}).json()["outcome"] == "no_show"
    with session_scope() as db:
        db.get(CalendarEvent, eid).starts_at = datetime.now(UTC) - timedelta(days=1)
    f = api.get("/api/insights/meetings").json()
    assert f["total"]["meetings"] == 1 and f["total"]["no_show"] == 1


# ------------------------------------------------------------------ 8. relationships and job changes

def test_relationships_rank_who_knows_whom(seeded, api):
    with session_scope() as db:
        p = _priya(db)
        conn = Connection(provider="google", account_email="ankit@us.example", status="active")
        db.add(conn)
        db.flush()
        for i in range(2):
            db.add(CalendarEvent(external_id=f"m{i}", title="Sync", starts_at=datetime.now(UTC) - timedelta(days=10 + i),
                                 attendees=["priya.sample@example.com"], account_id=p.account_id, contact_id=p.id,
                                 connection_id=conn.id))
        db.add(ContextEvent(source="email", source_ref="e1", account_id=p.account_id, contact_id=p.id,
                            author="ben@us.example", occurred_at=datetime.now(UTC) - timedelta(days=400)))
        acc = p.account_id
    rel = api.get(f"/api/accounts/{acc}/relationships").json()
    assert rel[0]["rep"] == "ankit@us.example" and rel[0]["meetings"] == 2 and rel[0]["level"] == "strong"
    assert rel[1]["rep"] == "ben@us.example" and rel[1]["level"] == "light"


def test_job_changes_and_bounces_raise_signals(seeded, monkeypatch):
    _set(enrichment_provider="apollo", apollo_api_key="k")
    with session_scope() as db:
        p = _priya(db)
        p.account.domain = "siemens.example"
        _deal(db)
    monkeypatch.setattr(http, "request", lambda *a, **kw: {"person": {
        "title": "VP Data", "organization": {"primary_domain": "newco.example"}}})
    with session_scope() as db:
        assert pipeline_intel.check_job_changes(db) >= 1
        sig = db.scalar(select(Signal).where(Signal.type == "champion_left"))
        assert "newco.example" in sig.headline
        s = db.scalar(select(ContactSuggestion).where(ContactSuggestion.source == "job change"))
        assert s.new_company_domain == "newco.example"
        assert pipeline_intel.check_job_changes(db) == 0  # checked again only after 90 days
    with session_scope() as db:
        m = db.scalar(select(Contact).where(Contact.email == "marcus.demo@example.com"))
        _deal(db, name="Crocs", **{})  # deal on Priya's account; Marcus's account has none
        pipeline_intel.bounce_means_left(db, m)
        assert db.scalar(select(Signal).where(Signal.headline.like("Marcus%"))) is None
        pipeline_intel.bounce_means_left(db, _priya(db))
        assert db.scalar(select(Signal).where(Signal.headline.like("%email bounced%")))
