"""Phase 2 Tracks B and C: routing, quotas, win/loss, products and line items; enrichment, email verification,
nurture flows and multi-touch attribution. Provider HTTP is mocked at integrations.http.request."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from revenueos import seed
from revenueos.config import settings
from revenueos.db import session_scope
from revenueos.integrations import http
from revenueos.main import app
from revenueos.models import (Account, Activity, Approval, CampaignMember, Campaign, Contact, NurtureMember,
                              Opportunity, Territory)
from revenueos.services import approvals, datahealth, insights, nurture, salesmgmt

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


# ------------------------------------------------------------------ routing

def test_new_leads_are_routed_by_territory_round_robin(seeded, api):
    with session_scope() as db:
        db.add(Territory(name="DACH manufacturing", priority=1, countries=["Germany", "DE"], industries=["manufactur"],
                         members=["anna@us.example", "ben@us.example"]))
        db.add(Territory(name="Everyone else", priority=99, members=["catch@us.example"]))
    owners = []
    for i in range(3):
        r = api.post("/api/leads", json={"first_name": f"L{i}", "last_name": "X", "company": f"Werk {i} GmbH",
                                         "email": f"l{i}@werk{i}.example", "country": "Germany",
                                         "industry": "Manufacturing"})
        owners.append(db_owner(r.json()["contact_id"]))
    assert owners == ["anna@us.example", "ben@us.example", "anna@us.example"]
    r = api.post("/api/leads", json={"first_name": "Z", "last_name": "Y", "company": "Shop Ltd", "email": "z@shop.example",
                                     "country": "UK"})
    assert db_owner(r.json()["contact_id"]) == "catch@us.example"
    # A second contact at an owned account goes to the same rep.
    r = api.post("/api/leads", json={"first_name": "Q", "last_name": "Q", "company": "Werk 0 GmbH",
                                     "email": "q@werk0.example", "country": "Germany", "industry": "Manufacturing"})
    assert db_owner(r.json()["contact_id"]) == "anna@us.example"


def db_owner(cid: int) -> str | None:
    with session_scope() as db:
        return db.get(Contact, cid).owner


# ------------------------------------------------------------------ quotas, win/loss, products

def test_quota_attainment_win_loss_and_line_items(seeded, api):
    pid = _cid()
    with session_scope() as db:
        acc_id = db.get(Contact, pid).account_id
        won = Opportunity(account_id=acc_id, name="Won deal", stage="discovery", amount=60000, owner="anna@us.example",
                          competitors=["BigSI"])
        open_ = Opportunity(account_id=acc_id, name="Open deal", stage="proposal", owner="anna@us.example",
                            close_date=datetime.now(UTC).date().isoformat())
        db.add_all([won, open_])
        db.flush()
        won_id, open_id = won.id, open_.id
    period = salesmgmt.current_period()
    assert api.patch(f"/api/deals/{won_id}", json={"stage": "closed_won"}).status_code == 422  # needs a reason
    assert api.patch(f"/api/deals/{won_id}", json={"stage": "closed_won", "close_reason": "proof_case_study"}).status_code == 200
    api.put("/api/quotas", json={"owner": "anna@us.example", "period": period, "amount": 100000})

    p = api.post("/api/products", json={"name": "Data assessment", "sku": "DA-4W", "unit_price": 20000}).json()
    r = api.post(f"/api/deals/{open_id}/lines", json={"product_id": p["id"], "quantity": 2, "discount_pct": 10}).json()
    assert r["amount"] == 36000.0 and r["lines"][0]["total"] == 36000.0

    rep = api.get("/api/insights/reps").json()["reps"][0]
    assert rep["owner"] == "anna@us.example" and rep["attainment_pct"] == 60.0
    assert rep["pipeline"] == 36000.0 and rep["coverage"] == 0.9

    wl = api.get("/api/insights/win-loss").json()
    assert wl["won_reasons"] == {"proof_case_study": 1} and wl["by_competitor"]["BigSI"]["won"] == 1


# ------------------------------------------------------------------ enrichment + verification

def test_enrichment_fills_only_empty_fields(seeded, monkeypatch):
    _set(enrichment_provider="apollo", apollo_api_key="k")
    monkeypatch.setattr(http, "request", lambda *a, **kw: {"person": {
        "title": "Chief Data Officer (from Apollo)", "linkedin_url": "https://linkedin.com/in/priya",
        "time_zone": "Europe/Berlin", "organization": {"industry": "industrial machinery", "estimated_num_employees": 300000,
                                                       "country": "Germany"}}})
    cid = _cid()
    with session_scope() as db:
        c = db.get(Contact, cid)
        original_title = c.title
        filled = datahealth.enrich_contact(db, c)
        assert "linkedin_url" in filled and "timezone" in filled
        assert c.title == original_title  # rep data is never overwritten
        assert c.timezone == "Europe/Berlin"


def test_invalid_email_is_blocked_and_risky_warned(seeded, monkeypatch):
    _set(verification_provider="zerobounce", zerobounce_api_key="k")
    answers = {"priya.sample@example.com": "invalid", "marcus.demo@example.com": "catch-all"}
    calls = []

    def fake(method, url, params=None, **kw):
        calls.append(params["email"])
        return {"status": answers[params["email"]]}
    monkeypatch.setattr(http, "request", fake)
    with session_scope() as db:
        p = db.get(Contact, _cid())
        m = db.get(Contact, _cid("marcus.demo@example.com"))
        a1 = approvals.queue(db, kind="email", contact=p, subject="s", body="Hi Priya.", created_by_agent="t")
        a2 = approvals.queue(db, kind="email", contact=m, subject="s", body="Hi Marcus.", created_by_agent="t")
        approvals.queue(db, kind="email", contact=p, subject="s2", body="Hi again.", created_by_agent="t")
        assert {f["code"] for f in a1.guardrail_flags} >= {"email_invalid"}
        assert "email_risky" in {f["code"] for f in a2.guardrail_flags}
    assert calls.count("priya.sample@example.com") == 1  # cached on the contact


# ------------------------------------------------------------------ nurture

def test_nurture_sends_approved_steps_with_merge_fields_and_exits_mqls(seeded, api):
    _set(send_weekdays_only=False, send_window_start_hour=0, send_window_end_hour=24, mql_score=95)
    pid, mid = _cid(), _cid("marcus.demo@example.com")
    flow = api.post("/api/nurture", json={"name": "Data maturity series"}).json()
    s1 = api.post(f"/api/nurture/{flow['id']}/steps",
                  json={"subject": "{{first_name}}, a data idea", "body": "Hi {{first_name}}, a thought for {{company}}.",
                        "delay_days": 0}).json()
    s2 = api.post(f"/api/nurture/{flow['id']}/steps", json={"subject": "Part 2", "body": "More for {{company}}.",
                                                             "delay_days": 7}).json()
    with session_scope() as db:
        assert db.get(Approval, s1["approval_id"]).guardrail_flags == [] or all(
            f["code"] != "placeholder" for f in db.get(Approval, s1["approval_id"]).guardrail_flags)
    r = api.post(f"/api/nurture/{flow['id']}/enroll", json={"contact_ids": [pid, mid, 99999]}).json()
    assert r["enrolled"] == 2 and r["skipped"] == 1
    with session_scope() as db:
        assert nurture.advance_all(db) == 0  # templates not approved yet
    for step in (s1, s2):
        assert api.post(f"/api/approvals/{step['approval_id']}/approve", json={}).json()["status"] == "approved"
    with session_scope() as db:
        assert nurture.advance_all(db) == 2
        sent = db.scalars(select(Approval).where(Approval.created_by_agent == "nurture")).all()
        assert {a.subject for a in sent} == {"Priya, a data idea", "Marcus, a data idea"}
        assert any("Siemens" in a.body for a in sent)
        assert nurture.advance_all(db) == 0  # step 2 is 7 days later
        db.get(Contact, mid).status = "replied"
    with session_scope() as db:
        assert nurture.advance_all(db, datetime.now(UTC) + timedelta(days=7, minutes=1)) == 1  # Priya only
        members = {m.contact_id: m for m in db.scalars(select(NurtureMember))}
        assert members[mid].status == "exited" and members[pid].status == "completed"


# ------------------------------------------------------------------ multi-touch attribution

def test_multi_touch_attribution_models(seeded):
    pid = _cid()
    t0 = datetime.now(UTC) - timedelta(days=30)
    with session_scope() as db:
        c = db.get(Contact, pid)
        camp = Campaign(name="Q4 Manufacturing", goal="g")
        db.add(camp)
        db.flush()
        for i, (agent, campaign) in enumerate([("outreach_writer", None), ("nurture", camp.id), ("reply_writer", None)]):
            db.add(Approval(kind="email", contact_id=pid, subject="s", body="b", status="sent", created_by_agent=agent,
                            campaign_id=campaign, sent_at=t0 + timedelta(days=i), decided_at=t0))
        db.add(Opportunity(account_id=c.account_id, name="Deal", amount=100000, created_at=t0 + timedelta(days=5)))
    with session_scope() as db:
        r = insights.multi_touch_attribution(db)
    m = r["models"]
    assert m["first_touch"] == {"email: outreach_writer": 100000.0}
    assert m["last_touch"] == {"email: reply_writer": 100000.0}
    assert m["w_shaped"] == {"email: outreach_writer": 40000.0, "email: reply_writer": 40000.0,
                             "campaign: Q4 Manufacturing": 20000.0}
    assert round(sum(m["linear"].values())) == 100000
