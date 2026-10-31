"""Phase D: client workspaces (isolation, settings, sign-in, webhooks, jobs), Zoho and Pipedrive adapters,
blind evaluation, ROI ledger and pilot report, onboarding."""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from revenueos import jobs, seed
from revenueos.api import evaluation as eval_api
from revenueos.config import settings
from revenueos.db import control_scope, session_scope, use_workspace
from revenueos.integrations import http
from revenueos.main import app
from revenueos.models import (Account, AgentRun, Approval, Contact, Opportunity, Suppression, User, Workspace)
from revenueos.services import auth, insights, workspaces
from revenueos.tools.crm import get_crm
from revenueos.tools.crm.pipedrive import PipedriveCRM
from revenueos.tools.crm.zoho import ZohoCRM

NOW = datetime.now(timezone.utc)


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
        for k in values:  # remove the test's shadow so workspace overrides apply again
            settings.__dict__.pop(k, None)


@pytest.fixture
def acme():
    workspaces.ensure_default()
    with setting(secret_key="test-secret"):
        ws = workspaces.create_workspace("Acme Corp", "acme")
        yield ws.id


# ----------------------------------------------------------------------------- isolation and settings

def test_workspaces_have_separate_data_and_settings(seeded, acme):
    with use_workspace(acme), session_scope() as db:
        db.add(Account(name="Acme Prospect"))
    with session_scope() as db:  # default workspace
        assert db.scalar(select(Account).where(Account.name == "Acme Prospect")) is None
        assert db.scalar(select(Account).where(Account.name == "Siemens")) is not None
    with use_workspace(acme), session_scope() as db:
        assert db.scalar(select(Account).where(Account.name == "Siemens")) is None  # seed data stays in default

    with setting(secret_key="test-secret"):
        workspaces.update_settings(acme, {"crm_provider": "hubspot", "hubspot_token": "pat-secret-123", "holdout_pct": "0.3"})
        with use_workspace(acme):
            assert settings.company_name == "Acme Corp"
            assert (settings.crm, settings.hubspot_token, settings.holdout_pct) == ("hubspot", "pat-secret-123", 0.3)
            assert get_crm().name == "hubspot"
        assert settings.crm == "local" and settings.hubspot_token is None  # default workspace untouched
        with control_scope() as db:
            stored = db.get(Workspace, acme).settings["hubspot_token"]
            assert "pat-secret-123" not in stored  # encrypted at rest
        public = workspaces.public_settings(acme)
        assert public["hubspot_token"] == {"secret": True, "set": True, "own": True}
        with pytest.raises(ValueError):
            workspaces.update_settings(acme, {"anthropic_api_key": "x"})


def test_jobs_run_in_the_workspace_that_started_them(seeded, acme):
    async def scenario():
        jobs.bind_loop(asyncio.get_running_loop())
        with use_workspace(acme):
            run_id = jobs.start("job:test", "in acme", lambda rid: asyncio.sleep(0, result="done"))
        await asyncio.sleep(0.05)
        return run_id
    run_id = asyncio.run(scenario())
    with use_workspace(acme), session_scope() as db:
        assert db.get(AgentRun, run_id).status == "succeeded"
    with session_scope() as db:
        assert db.get(AgentRun, run_id) is None or db.get(AgentRun, run_id).input != "in acme"


# ----------------------------------------------------------------------------- sign-in and roles

def test_sign_in_roles_and_client_isolation_over_http(seeded, acme):
    with TestClient(app) as api:
        assert api.get("/api/auth/me").json()["setup_needed"] is True
        owner = api.post("/api/auth/bootstrap", json={"email": "owner@us.example", "password": "a-long-password"}).json()
        assert owner["user"]["role"] == "owner"
        assert api.post("/api/auth/bootstrap", json={"email": "x@y.example", "password": "a-long-password"}).status_code == 409
        assert api.get("/api/leads").status_code == 401  # sign-in now required
        assert api.post("/api/auth/login", json={"email": "owner@us.example", "password": "wrong-password"}).status_code == 401
        H = {"Authorization": f"Bearer {owner['token']}"}
        assert len(api.get("/api/leads", headers=H).json()) == 4  # default workspace data

        r = api.post("/api/workspaces", headers=H, json={"name": "Beta Ltd", "slug": "beta",
                                                         "admin_email": "admin@beta.example", "admin_password": "beta-password-1"})
        assert r.status_code == 200
        client = api.post("/api/auth/login", json={"email": "admin@beta.example", "password": "beta-password-1"}).json()
        C = {"Authorization": f"Bearer {client['token']}"}
        assert api.get("/api/auth/me", headers=C).json()["workspace"]["slug"] == "beta"
        assert api.get("/api/leads", headers=C).json() == []  # sees none of the default workspace's leads
        api.post("/api/leads", headers=C, json={"first_name": "Bea", "last_name": "Client", "company": "BetaCo"})
        assert len(api.get("/api/leads", headers=C).json()) == 1
        assert len(api.get("/api/leads", headers=H).json()) == 4
        assert api.get("/api/workspaces", headers=C).status_code == 403  # client admins can't see other clients

        api.post("/api/workspace/users", headers=C, json={"email": "rep@beta.example", "password": "rep-password-1"})
        rep = api.post("/api/auth/login", json={"email": "rep@beta.example", "password": "rep-password-1"}).json()
        R = {"Authorization": f"Bearer {rep['token']}"}
        assert api.get("/api/workspace/settings", headers=R).status_code == 403  # members can't change settings
        api.post("/api/auth/logout", headers=R)
        assert api.get("/api/leads", headers=R).status_code == 401
    with control_scope() as db:
        assert db.scalar(select(User).where(User.email == "owner@us.example")).password_hash.startswith("scrypt$")


def test_webhooks_route_by_workspace_slug(seeded, acme):
    with TestClient(app) as api:
        assert api.post("/api/webhooks/unsubscribe?ws=acme", json={"email": "gone@x.example"}).status_code == 200
        assert api.post("/api/webhooks/unsubscribe?ws=nope", json={"email": "a@b.example"}).status_code == 404
    with use_workspace(acme), session_scope() as db:
        assert db.scalar(select(Suppression).where(Suppression.email_or_domain == "gone@x.example"))
    with session_scope() as db:
        assert db.scalar(select(Suppression).where(Suppression.email_or_domain == "gone@x.example")) is None


def test_oauth_state_remembers_workspace(acme):
    from revenueos.integrations import oauth

    with setting(secret_key="test-secret"):
        with use_workspace(acme):
            state = oauth.make_state("google")
        assert oauth.check_state("google", state) == acme


# ----------------------------------------------------------------------------- Zoho and Pipedrive

class FakeHTTP:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for (m, part), resp in self.routes.items():
            if m == method and part in url:
                return resp
        raise AssertionError(f"unexpected {method} {url}")


def _contract(crm, db, fake, expect_status_call: bool):
    assert crm.import_open_leads(db) == 1 and crm.import_open_leads(db) == 0
    hana = db.scalar(select(Contact).where(Contact.email == "hana.ito@example.com"))
    assert hana.crm_id and hana.account.name == "Kaizen Logistics"
    crm.log_activity(hana, "Email: hello", "body")
    crm.update_lead_status(hana, "contacted", 70)
    opp = Opportunity(account_id=hana.account_id, primary_contact_id=hana.id, name="Kaizen pilot", stage="proposal",
                      amount=40_000, close_date="2026-12-15")
    db.add(opp)
    db.flush()
    assert crm.upsert_opportunity(opp) == "d1" and opp.crm_id == "d1"
    crm.upsert_opportunity(opp)
    assert crm.pull_deals(db, NOW - timedelta(days=1)) == 1
    return opp


def test_zoho_adapter(seeded, monkeypatch):
    fake = FakeHTTP({
        ("POST", "/oauth/v2/token"): {"access_token": "z", "expires_in": 3600},
        ("GET", "/crm/v2/Leads/search"): {"data": [{"id": 77, "First_Name": "Hana", "Last_Name": "Ito",
                                                    "Email": "hana.ito@example.com", "Company": "Kaizen Logistics"}]},
        ("POST", "/crm/v2/Notes"): {"data": [{"details": {"id": "n1"}}]},
        ("PUT", "/crm/v2/Leads/"): {"data": [{}]},
        ("POST", "/crm/v2/Deals"): {"data": [{"details": {"id": "d1"}}]},
        ("PUT", "/crm/v2/Deals/"): {"data": [{}]},
        ("GET", "/crm/v2/Deals"): {"data": [{"id": "d1", "Stage": "Closed Won", "Amount": 52000, "Closing_Date": "2026-12-01"}]},
    })
    monkeypatch.setattr(http, "request", fake)
    with setting(zoho_refresh_token="r", zoho_client_id="c", zoho_client_secret="s"), session_scope() as db:
        opp = _contract(ZohoCRM(), db, fake, True)
        assert (opp.stage, opp.amount) == ("closed_won", 52000)
    status = next(kw for m, u, kw in fake.calls if m == "PUT" and "/Leads/" in u)
    assert status["json_body"]["data"][0]["Lead_Status"] == "Attempted to Contact"
    assert next(kw for m, u, kw in fake.calls if "/Notes" in u)["json_body"]["data"][0]["se_module"] == "Leads"
    deals_get = next(kw for m, u, kw in fake.calls if m == "GET" and u.endswith("/crm/v2/Deals"))
    assert "If-Modified-Since" in deals_get["headers"]


def test_pipedrive_adapter(seeded, monkeypatch):
    fake = FakeHTTP({
        ("GET", "/v1/leads"): {"data": [{"id": "L1", "title": "Inbound demo", "person_id": 9, "organization_id": 3}]},
        ("GET", "/v1/persons/9"): {"data": {"id": 9, "name": "Hana Ito", "email": [{"value": "hana.ito@example.com"}]}},
        ("GET", "/v1/organizations/3"): {"data": {"name": "Kaizen Logistics"}},
        ("POST", "/v1/notes"): {"data": {"id": 5}},
        ("POST", "/v1/deals"): {"data": {"id": "d1"}},
        ("PUT", "/v1/deals/d1"): {"data": {}},
        ("GET", "/v1/recents"): {"data": [{"item": "deal", "data": {"id": "d1", "status": "lost", "value": 40000}}]},
    })
    monkeypatch.setattr(http, "request", fake)
    with setting(pipedrive_token="pd", crm_field_map='{"deal_stage": {"proposal": "12"}}'), session_scope() as db:
        opp = _contract(PipedriveCRM(), db, fake, False)
        assert opp.stage == "closed_lost"
    create = next(kw for m, u, kw in fake.calls if m == "POST" and u.endswith("/v1/deals"))
    assert create["json_body"]["stage_id"] == 12 and create["json_body"]["person_id"] == 9
    assert all(kw["params"]["api_token"] == "pd" for m, u, kw in fake.calls)


# ----------------------------------------------------------------------------- blind evaluation

def test_blind_evaluation(seeded):
    with session_scope() as db:
        priya = db.scalar(select(Contact).where(Contact.email == "priya.sample@example.com"))
        db.add(Approval(kind="email", status="pending", contact_id=priya.id, subject="forecasting at Siemens",
                        body="RevenueOS draft body", sequence_step=1, created_by_agent="outreach_writer", guardrail_flags=[]))
        pid = priya.id
        elena = db.scalar(select(Contact.id).where(Contact.email == "elena.test@example.com"))
    with TestClient(app) as api:
        r = api.post("/api/evaluations", json={"name": "Oct test", "items": [
            {"contact_id": pid, "subject": "Quick question", "body": "Our template email body"},
            {"contact_id": elena, "body": "No RevenueOS draft for this one"}]}).json()
        assert r["items"] == 1 and r["skipped_contacts_without_draft"] == [elena]
        [item] = api.get(f"/api/evaluations/{r['id']}?rater=sam").json()
        assert "revenueos" not in str(item).lower() or "RevenueOS draft body" in str(item)  # sources hidden
        assert set(item) == {"item_id", "recipient", "a", "b", "your_choice"}
        rev_side = "A" if item["a"]["body"] == "RevenueOS draft body" else "B"
        api.post(f"/api/evaluations/{r['id']}/items/{item['item_id']}/rate", json={"rater": "sam", "choice": rev_side,
                                                                                    "comment": "more specific"})
        res = api.get(f"/api/evaluations/{r['id']}/results").json()
    assert (res["revenueos_wins"], res["incumbent_wins"]) == (1, 0) and "Not enough" in res["verdict"]
    assert res["comments"][0]["preferred"] == "RevenueOS"
    assert eval_api.sign_test_p(20, 5) < 0.01 and eval_api.sign_test_p(5, 5) == 1.0


# ----------------------------------------------------------------------------- ROI, pilot report, onboarding

def test_roi_ledger_and_pilot_report(seeded):
    with session_scope() as db:
        priya = db.scalar(select(Contact).where(Contact.email == "priya.sample@example.com"))
        db.add(Approval(kind="email", status="sent", contact_id=priya.id, subject="s", body="b", created_by_agent="t",
                        sent_at=NOW - timedelta(days=10), personalisation_score=85, guardrail_flags=[]))
        db.add(Opportunity(account_id=priya.account_id, name="Siemens", stage="closed_won", amount=100_000))
        db.add(Opportunity(account_id=_acct(db, "Crocs"), name="Crocs", stage="proposal", amount=50_000))  # no AI touch
        db.add(AgentRun(agent="outreach_writer", model="m", input="x", status="succeeded", cost_usd=12.5))
        priya.status = "meeting"
    with session_scope() as db:
        roi = insights.roi_ledger(db)
        assert (roi["influenced_opportunities"], roi["influenced_won"], roi["influenced_pipeline"]) == (1, 100_000, 0)
        assert roi["cost_per_meeting"] == 12.5 and roi["won_per_dollar_of_ai"] == 8000.0
        assert insights.personalisation_vs_replies(db)[2] == {"score": "80+", "sent": 1, "positive_replies": 0, "positive_rate": 0.0}
    with TestClient(app) as api:
        md = api.get("/api/pilot/report.md").text
    assert "Return on AI spend" in md and "AI-influenced deals: 1" in md


def _acct(db, name):
    return db.scalar(select(Account.id).where(Account.name == name))


def test_onboarding_checklist(seeded, monkeypatch):
    started = []
    monkeypatch.setattr(jobs, "start", lambda name, desc, work, **kw: started.append(desc) or 1)
    with TestClient(app) as api:
        steps = {s["key"]: s["done"] for s in api.get("/api/onboarding/status").json()["steps"]}
        assert steps == {"company": False, "crm": False, "knowledge": False, "leads": False,
                         "integrations": False, "pilot": False}
        r = api.post("/api/onboarding/knowledge", json={"offering": "We do X", "icp": "Mid-market", "case_study": "Y grew 20%",
                                                        "brand_voice": "Plain", "remove_samples": True}).json()
        assert len(r["added"]) == 4 and r["samples_removed"] == 10
        steps = {s["key"]: s["done"] for s in api.get("/api/onboarding/status").json()["steps"]}
        assert steps["knowledge"] is True
        assert api.post("/api/onboarding/test-crm").json()["crm"] == "local"
        assert api.post("/api/onboarding/pilot", json={"max_leads": 5}).json()["leads"] == 4
    assert started == ["Pilot on 4 leads"]
