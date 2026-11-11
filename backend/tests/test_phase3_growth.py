"""Phase 3 batch 2: website visits, intent data, events, renewals and customer health, LinkedIn publishing,
campaign spend and ROI."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from revenueos import seed
from revenueos.config import settings
from revenueos.db import session_scope
from revenueos.integrations import http
from revenueos.main import app
from revenueos.models import (Account, Activity, Approval, Campaign, Contact, Contract, NurtureMember, Opportunity,
                              Signal, Task, WebVisit)
from revenueos.services import approvals, growth, nurture

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


# ------------------------------------------------------------------ website visits

def test_email_click_identifies_visitor_and_pricing_view_is_a_signal(seeded, api):
    pid = _cid()
    with session_scope() as db:
        aid = approvals.queue(db, kind="email", contact=db.get(Contact, pid), subject="s", body="b",
                              created_by_agent="t").id
    assert "sendBeacon" in api.get("/api/public/t.js").text
    beacon = lambda url: api.post("/api/public/track", content=json.dumps({"v": "visitor-1", "u": url}),
                                  headers={"Content-Type": "text/plain"})
    assert beacon(f"https://example.com/blog/post?utm_source=revenueos&utm_content=approval-{aid}").status_code == 204
    beacon("https://example.com/pricing")  # later visit, no UTM: still known through the visitor id
    beacon("https://example.com/pricing")  # repeat view the same day: no duplicate signal
    with session_scope() as db:
        visits = db.scalars(select(WebVisit).order_by(WebVisit.id)).all()
        assert [v.contact_id for v in visits] == [pid, pid, pid]
        signals = db.scalars(select(Signal).where(Signal.type == "website_visit")).all()
        assert len(signals) == 1 and signals[0].strength == "high"
        assert db.scalar(select(Activity).where(Activity.type == "web_visit_high", Activity.contact_id == pid))
    assert api.get("/api/visits", params={"contact_id": pid}).json()[0]["path"] == "/pricing"


# ------------------------------------------------------------------ intent

def test_intent_webhook_and_csv_create_signals(seeded, api):
    _set(intent_create_accounts=True)
    with session_scope() as db:
        db.scalar(select(Account).where(Account.name == "Siemens")).domain = "siemens.example"
    r = api.post("/api/webhooks/intent", json=[
        {"provider": "bombora", "topic": "Data Governance", "score": 82, "domain": "https://www.siemens.example"},
        {"provider": "bombora", "topic": "CRM", "score": 20, "domain": "siemens.example"},           # too weak
        {"provider": "g2", "topic": "Salesforce consulting", "score": 91, "company": "Brand New Co"}]).json()
    assert r == {"received": 3, "signals": 2}
    csv_text = "domain,topic,score\nsiemens.example,Demand forecasting,55\n"
    r = api.post("/api/intel/intent/import", files={"file": ("intent.csv", csv_text, "text/csv")}).json()
    assert r == {"rows": 1, "signals": 1}
    with session_scope() as db:
        assert db.scalar(select(Account).where(Account.name == "Brand New Co")).source == "intent"
        sig = growth.recent_signals(db, db.scalar(select(Account.id).where(Account.name == "Siemens")))
        assert {s["strength"] for s in sig} == {"high", "medium"}


# ------------------------------------------------------------------ events

def test_event_registration_attendance_and_follow_up(seeded, api):
    flow_a = api.post("/api/nurture", json={"name": "Attended"}).json()
    flow_n = api.post("/api/nurture", json={"name": "Missed it"}).json()
    ev = api.post("/api/events", json={"name": "Data Summit webinar", "external_id": "zoom-123",
                                       "attended_flow_id": flow_a["id"], "noshow_flow_id": flow_n["id"]}).json()
    api.post(f"/api/events/{ev['id']}/registrations", json={"email": "priya.sample@example.com"})
    api.post("/api/webhooks/event-registration", json={"external_event_id": "zoom-123", "email": "new.person@acme.example",
                                                       "first_name": "New", "last_name": "Person", "company": "Acme"})
    api.post("/api/webhooks/event-registration", json={"external_event_id": "zoom-123", "email": "marcus.demo@example.com"})
    r = api.post(f"/api/events/{ev['id']}/attendance", json={"attendees": [
        {"email": "priya.sample@example.com", "minutes": 52, "questions": "How long does an assessment take?"},
        {"email": "new.person@acme.example", "minutes": 5}]}).json()
    assert r["attended"] == 2 and r["no_show"] == 1
    out = api.post(f"/api/events/{ev['id']}/follow-up").json()
    assert out == {"attended_enrolled": 2, "no_show_enrolled": 1, "tasks": 1}
    with session_scope() as db:
        task = db.scalar(select(Task).where(Task.created_by == "events"))
        assert "assessment" in task.notes and task.contact_id == _cid()
        assert db.scalar(select(Contact).where(Contact.email == "new.person@acme.example")).source == "event"
    assert api.post(f"/api/events/{ev['id']}/follow-up").json()["tasks"] == 0  # idempotent


# ------------------------------------------------------------------ customers

def test_won_deal_becomes_contract_then_renewal_and_health(seeded, api):
    pid = _cid()
    p = api.post("/api/products", json={"name": "Managed analytics", "unit_price": 5000, "billing": "monthly"}).json()
    api.post("/api/products", json={"name": "Data assessment", "unit_price": 20000})
    with session_scope() as db:
        acc_id = db.get(Contact, pid).account_id
        db.get(Contact, pid).owner = "anna@us.example"
        o = Opportunity(account_id=acc_id, name="Siemens analytics", stage="negotiation")
        db.add(o)
        db.flush()
        oid = o.id
    api.post(f"/api/deals/{oid}/lines", json={"product_id": p["id"], "quantity": 12})
    api.patch(f"/api/deals/{oid}", json={"stage": "closed_won", "close_reason": "product_fit"})
    with session_scope() as db:
        k = db.scalar(select(Contract).where(Contract.opportunity_id == oid))
        assert k.amount == 60000 and k.products == ["Managed analytics"]
        k.end_date = (date.today() + timedelta(days=45)).isoformat()  # renewal coming up
    r = api.post("/api/customers/renewals/run").json()
    assert r["renewal_deals_created"] == 1
    assert api.post("/api/customers/renewals/run").json()["renewal_deals_created"] == 0  # only once
    cust = api.get("/api/customers").json()[0]
    assert cust["arr"] == 60000 and cust["upsell_ideas"][0]["product"] == "Data assessment"
    assert cust["health"]["churn_risk"] in ("low", "medium", "high") and cust["health"]["reasons"]  # scored in its own test
    with session_scope() as db:
        renewal = db.scalar(select(Opportunity).where(Opportunity.deal_type == "renewal"))
        assert renewal.owner == "anna@us.example" and renewal.amount == 60000
        assert db.scalar(select(Task).where(Task.created_by == "renewals"))
        rid = renewal.id
    api.patch(f"/api/deals/{rid}", json={"stage": "closed_won", "close_reason": "relationship"})
    with session_scope() as db:
        assert db.scalar(select(Contract).where(Contract.opportunity_id == oid)).status == "renewed"


def test_quiet_customer_with_negative_signals_is_high_churn_risk(seeded):
    pid = _cid()
    with session_scope() as db:
        c = db.get(Contact, pid)
        db.add(Contract(account_id=c.account_id, name="Support", amount=10000,
                        end_date=(date.today() + timedelta(days=30)).isoformat()))
        old = datetime.now(UTC) - timedelta(days=75)
        db.add(Activity(contact_id=pid, type="reply_objection", detail="unhappy", created_at=old))
        db.add(Activity(contact_id=pid, type="reply_not_now", detail="busy", created_at=old))
    with session_scope() as db:
        h = growth.health(db, db.get(Contact, pid).account)
        assert h["churn_risk"] == "high" and h["score"] < 50
        assert growth.refresh_health(db) == 1 and growth.refresh_health(db) == 0


# ------------------------------------------------------------------ publishing and spend

def test_publish_approved_linkedin_post(seeded, api, monkeypatch):
    with session_scope() as db:
        aid = approvals.queue(db, kind="content", content_type="linkedin_post", title="Post", subject="Post",
                              body="Forecast accuracy belongs on the board agenda.", created_by_agent="content_studio").id
    assert api.post(f"/api/approvals/{aid}/publish").status_code == 409  # not approved yet
    api.post(f"/api/approvals/{aid}/approve", json={})
    assert "isn't set up" in api.post(f"/api/approvals/{aid}/publish").json()["detail"]
    _set(linkedin_access_token="tok", linkedin_author_urn="urn:li:organization:42")
    calls = []
    monkeypatch.setattr(http, "request", lambda method, url, **kw: calls.append((url, kw)) or {"id": "urn:li:share:7"})
    r = api.post(f"/api/approvals/{aid}/publish").json()
    assert r["published_url"].endswith("urn:li:share:7")
    assert calls[0][1]["json_body"]["author"] == "urn:li:organization:42"
    assert api.post(f"/api/approvals/{aid}/publish").status_code == 409  # never twice


def test_campaign_spend_feeds_roi(seeded, api):
    with session_scope() as db:
        camp = Campaign(name="Q4 Manufacturing", goal="meetings")
        db.add(camp)
        db.flush()
        cid = camp.id
    csv_text = "Date,Spend,Impressions,Clicks\n2026-11-01,1200.50,40000,310\n2026-11-02,799.50,30000,200\n"
    assert api.post(f"/api/campaigns/{cid}/spend/import?channel=linkedin_ads",
                    files={"file": ("ads.csv", csv_text, "text/csv")}).json() == {"rows": 2}
    row = next(c for c in api.get("/api/insights/attribution").json()["by_campaign"] if c["campaign_id"] == cid)
    assert row["spend"] == 2000.0
