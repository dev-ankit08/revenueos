"""Phase C: CRM adapter contract (HubSpot, Dynamics, Salesforce), lookup for the side panel, MCP server,
AI governance, claim citations, sensitive-personal guard, rule packs."""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from revenueos import seed
from revenueos.agents import base, writer
from revenueos.agents.base import new_run
from revenueos.config import settings
from revenueos.db import session_scope
from revenueos.integrations import http
from revenueos.main import app
from revenueos.models import Account, AgentRun, AgentSetting, Approval, Contact, Opportunity
from revenueos.services import approvals, governance, guardrails
from revenueos.tools import crm as crm_pkg
from revenueos.tools import kb
from revenueos.tools.crm.dynamics import DynamicsCRM
from revenueos.tools.crm.hubspot import HubSpotCRM
from revenueos.tools.crm.salesforce import SalesforceCRM

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


class FakeHTTP:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for (m, part), resp in self.routes.items():
            if m == method and part in url:
                return resp(url, kw) if callable(resp) else resp
        raise AssertionError(f"unexpected {method} {url}")

    def sent(self, method, part):
        return [kw for m, u, kw in self.calls if m == method and part in u]


# ----------------------------------------------------------------------------- CRM contract

def hubspot_case():
    routes = {
        ("POST", "/crm/v3/objects/contacts/search"): {"results": [{"id": "501", "properties": {
            "firstname": "Hana", "lastname": "Ito", "email": "hana.ito@example.com", "jobtitle": "VP Ops",
            "company": "Kaizen Logistics", "website": "https://www.kaizen.example"}}]},
        ("POST", "/crm/v3/objects/notes"): {"id": "n1"},
        ("PATCH", "/crm/v3/objects/contacts/"): {},
        ("POST", "/crm/v3/objects/deals/search"): lambda u, kw: {"results": [
            {"id": "d1", "properties": {"dealstage": "contractsent", "amount": "75000", "closedate": "2027-01-31T00:00:00Z"}}]},
        ("POST", "/crm/v3/objects/deals"): {"id": "d1"},
        ("PATCH", "/crm/v3/objects/deals/"): {},
    }
    return HubSpotCRM, routes, dict(hubspot_token="tok"), {
        "note": ("POST", "/crm/v3/objects/notes"), "status": ("PATCH", "/crm/v3/objects/contacts/"),
        "deal_create": ("POST", "/crm/v3/objects/deals"), "deal_update": ("PATCH", "/crm/v3/objects/deals/"),
        "status_value": lambda kw: kw["json_body"]["properties"]["hs_lead_status"], "contacted": "ATTEMPTED_TO_CONTACT"}


def dynamics_case():
    routes = {
        ("POST", "login.microsoftonline.com"): {"access_token": "dyn", "expires_in": 3600},
        ("GET", "/leads"): {"value": [{"leadid": "aaaa-1", "firstname": "Hana", "lastname": "Ito",
                                       "emailaddress1": "hana.ito@example.com", "jobtitle": "VP Ops",
                                       "companyname": "Kaizen Logistics", "websiteurl": "kaizen.example"}]},
        ("POST", "/tasks"): {"activityid": "t1"},
        ("PATCH", "/leads("): {},
        ("GET", "/opportunities"): {"value": [{"opportunityid": "d1", "stepname": "4-Close", "estimatedvalue": 75000,
                                               "estimatedclosedate": "2027-01-31", "statecode": 0}]},
        ("POST", "/opportunities"): {"opportunityid": "d1"},
        ("PATCH", "/opportunities("): {},
    }
    return DynamicsCRM, routes, dict(dynamics_url="https://org.crm.dynamics.com", dynamics_client_id="c",
                                     dynamics_client_secret="s"), {
        "note": ("POST", "/tasks"), "status": ("PATCH", "/leads("), "deal_create": ("POST", "/opportunities"),
        "deal_update": ("PATCH", "/opportunities("),
        "status_value": lambda kw: kw["json_body"]["statuscode"], "contacted": 2}


@pytest.mark.parametrize("case", [hubspot_case, dynamics_case], ids=["hubspot", "dynamics"])
def test_crm_adapter_contract(seeded, monkeypatch, case):
    cls, routes, cfg, ops = case()
    fake = FakeHTTP(routes)
    monkeypatch.setattr(http, "request", fake)
    with setting(**cfg):
        crm = cls()
        with session_scope() as db:
            # 1. import open leads (idempotent)
            assert crm.import_open_leads(db) == 1
            assert crm.import_open_leads(db) == 0
            hana = db.scalar(select(Contact).where(Contact.email == "hana.ito@example.com"))
            assert hana.crm_id and hana.source == crm.name and hana.account.name == "Kaizen Logistics"
            # 2. activity + status
            crm.log_activity(hana, "Email: hello", "body")
            assert fake.sent(*ops["note"])
            crm.update_lead_status(hana, "contacted", 70)
            assert ops["status_value"](fake.sent(*ops["status"])[-1]) == ops["contacted"]
            # 3. deal create, then update (id stored on our record)
            opp = Opportunity(account_id=hana.account_id, primary_contact_id=hana.id, name="Kaizen pilot",
                              stage="qualification", amount=40_000, close_date="2026-12-15")
            db.add(opp)
            db.flush()
            assert crm.upsert_opportunity(opp) == "d1" and opp.crm_id == "d1" and opp.sf_id is None
            crm.upsert_opportunity(opp)
            assert fake.sent(*ops["deal_create"]) and fake.sent(*ops["deal_update"])
            # 4. a rep moved the deal in the CRM: mirrored locally
            assert crm.pull_deals(db, NOW - timedelta(days=1)) == 1
            assert (opp.stage, opp.amount, opp.close_date) == ("negotiation", 75_000, "2027-01-31")
            assert opp.stage_changes[-1].to_stage == "negotiation"


def test_field_map_override_and_provider_selection(monkeypatch):
    with setting(crm_field_map='{"deal_stage": {"proposal": "custom_stage_9"}}'):
        crm = HubSpotCRM()
        assert crm.stage_map["proposal"] == "custom_stage_9" and crm.stage_from_crm("custom_stage_9") == "proposal"
    assert isinstance(crm_pkg.make_crm("local"), crm_pkg.LocalCRM)
    with pytest.raises(ValueError):
        crm_pkg.make_crm("freshsales")
    with setting(crm_provider="", sf_username=None):
        assert settings.crm == "local"


def test_salesforce_adapter_with_fake_client(seeded):
    calls = []

    class Obj:
        def __init__(self, name):
            self.name = name

        def create(self, fields):
            calls.append((self.name, "create", fields))
            return {"id": "006X"}

        def update(self, i, fields):
            calls.append((self.name, "update", i, fields))

    queries = []

    def query_all(soql):
        queries.append(soql)
        if "FROM Lead" in soql:
            return {"records": [{"Id": "00QABC", "FirstName": "Ana", "LastName": "Lima", "Email": "ana.lima@example.com",
                                 "Company": "Lima Freight", "Title": "CIO"}]}
        return {"records": [{"Id": "006X", "StageName": "Closed Won", "Amount": 90000, "CloseDate": "2026-11-30"}]}

    client = SimpleNamespace(Task=Obj("Task"), Lead=Obj("Lead"), Opportunity=Obj("Opportunity"),
                             Campaign=Obj("Campaign"), CampaignMember=Obj("CampaignMember"), query_all=query_all)
    sf = SalesforceCRM(client=client)
    with session_scope() as db:
        assert sf.import_open_leads(db) == 1
        ana = db.scalar(select(Contact).where(Contact.sf_id == "00QABC"))
        ana.account.sf_id = "001A"
        opp = Opportunity(account_id=ana.account_id, name="Lima", stage="proposal", amount=50_000)
        db.add(opp)
        db.flush()
        assert sf.upsert_opportunity(opp) == "006X" and opp.sf_id == "006X" and opp.crm_id is None
        assert sf.pull_deals(db, NOW - timedelta(days=1)) == 1
        assert opp.stage == "closed_won" and opp.closed_at is not None
    assert "LastModifiedDate >" in queries[-1] and "'006X'" in queries[-1]


def test_side_panel_lookup(seeded):
    with session_scope() as db:
        c = db.scalar(select(Contact).where(Contact.email == "elena.test@example.com"))
        c.crm_id, cid = "hs-777", c.id
    with TestClient(app) as api:
        assert api.get("/api/leads/lookup?crm=hubspot&id=hs-777").json()["contact_id"] == cid
        assert api.get("/api/leads/lookup?email=ELENA.TEST@example.com").json()["contact_id"] == cid
        assert api.get("/api/leads/lookup?crm=dynamics&id=nope").status_code == 404


# ----------------------------------------------------------------------------- MCP

def test_mcp_server_exposes_skills(seeded):
    from revenueos import mcp_server

    names = {t.name for t in asyncio.run(mcp_server.server.list_tools())}
    assert {"find_leads", "account_timeline", "draft_sequence", "analyse_call", "review_deal",
            "draft_committee_outreach", "pipeline_status"} <= names
    out = asyncio.run(mcp_server.server.call_tool("find_leads", {"search": "Siemens"}))
    assert "Priya Sample" in str(out)


# ----------------------------------------------------------------------------- governance

class StubMessages:
    def __init__(self):
        self.models = []

    def stream(self, **params):
        self.models.append(params["model"])
        msg = SimpleNamespace(content=[], stop_reason="end_turn", parsed_output=None,
                              usage=SimpleNamespace(input_tokens=1_000_000, output_tokens=0,
                                                    cache_read_input_tokens=0, cache_creation_input_tokens=0))

        class S:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get_final_message(self):
                return msg
        return S()


def test_governance_budgets_models_and_switches(seeded, monkeypatch):
    stub = StubMessages()
    monkeypatch.setattr(base, "client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=stub)))
    call = lambda: asyncio.run(base.run_agent(agent="lead_scorer", model="claude-haiku-5-5",  # noqa: E731
                                               role_prompt="r", prompt="p", effort="low"))

    governance.update("lead_scorer", model="claude-sonnet-5-5", monthly_budget_usd=None)
    call()
    assert stub.models[-1] == "claude-sonnet-5-5"  # override applied
    with session_scope() as db:
        run = db.scalar(select(AgentRun).where(AgentRun.agent == "lead_scorer").order_by(AgentRun.id.desc()))
        assert run.model == "claude-sonnet-5-5" and run.cost_usd == pytest.approx(2.0)  # 1M input tokens at $2

    governance.update("lead_scorer", model=None, monthly_budget_usd=1.0)  # already spent $2
    with pytest.raises(governance.BudgetExceeded):
        call()
    governance.update("lead_scorer", model=None, monthly_budget_usd=None, enabled=False)
    with pytest.raises(governance.AgentDisabled):
        call()
    governance.update("lead_scorer", model=None, monthly_budget_usd=None, enabled=True)
    governance.update("*", model=None, monthly_budget_usd=2.05)  # total: $2.00 of 2.05 = 98% -> alert once
    call()  # default Haiku now: +$0.10
    with session_scope() as db:
        assert db.scalar(select(AgentSetting).where(AgentSetting.agent == "*")).alerted_month == NOW.strftime("%Y-%m")
    with pytest.raises(governance.BudgetExceeded, match="Monthly AI budget"):
        call()  # $2.10 spent >= $2.05
    with pytest.raises(ValueError, match="searches the web"):
        governance.update("account_researcher", model="claude-haiku-5-5", monthly_budget_usd=None)

    with TestClient(app) as api:
        o = api.get("/api/governance").json()
        assert o["total"]["budget"] == 2.05 and any(a["agent"] == "lead_scorer" for a in o["agents"])
        assert api.put("/api/governance/nobody", json={}).status_code == 422


# ----------------------------------------------------------------------------- brand safety

def test_claims_are_stored_and_checked(seeded, monkeypatch):
    from revenueos.services import personalisation

    async def ok_grade(**kw):
        return personalisation.Grade(score=90, specific_facts_used=[], unsupported_claims=[], generic_phrases=[], feedback="")
    monkeypatch.setattr(personalisation, "grade", ok_grade)
    priya = None
    with session_scope() as db:
        priya = db.scalar(select(Contact).where(Contact.email == "priya.sample@example.com")).id
    tool = writer.make_queue_tool({priya}, new_run("t", "m", "p"), grounding="brief https://news.example/a")
    asyncio.run(tool.call({
        "contact_id": priya, "channel": "email", "sequence_step": 1, "send_after_days": 0, "subject": "forecasting",
        "body": "Hi Priya, a manufacturer lifted forecast accuracy from 61% to 84%.", "rationale": "r",
        "claims": [{"text": "61% to 84%", "source": "kb:Global Manufacturer Forecasting"},
                   {"text": "Siemens news", "source": "https://news.example/a"},
                   {"text": "made up", "source": "https://somewhere-else.example"}]}))
    with session_scope() as db:
        a = db.scalar(select(Approval).order_by(Approval.id.desc()))
        assert len(a.claims) == 3 and a.claims[0]["source"].startswith("kb:")
        [flag] = [f for f in a.guardrail_flags if f["code"] == "unsourced_claim"]
        assert "made up" in flag["message"] and "61%" not in flag["message"]
        b = approvals.queue(db, kind="email", contact=db.get(Contact, priya), subject="s", created_by_agent="t",
                            body="We cut costs 40% for a client.", claims=[])
        assert any(f["code"] == "no_sources" for f in b.guardrail_flags)


def test_sensitive_personal_details_blocked_business_terms_allowed():
    assert guardrails.sensitive_personal("Hope your surgery went well!") == "health"
    assert guardrails.sensitive_personal("Congrats to your wife on the new role") == "family"
    assert guardrails.sensitive_personal("Saw your birthday post") == "age"
    assert guardrails.sensitive_personal("We help hospital networks and family-owned manufacturers") is None


def test_rule_packs(seeded):
    with session_scope() as db:
        kb.add_document(db, "Brand rules", "rules",
                        "BANNED: synergy\nREQUIRED: Results vary by client\nTONE: Never use exclamation marks")
        c = db.scalar(select(Contact).where(Contact.email == "elena.test@example.com"))
        a = approvals.queue(db, kind="email", contact=c, subject="idea", created_by_agent="t",
                            body="Hi Elena, real synergy here.")
        codes = {f["code"]: f["severity"] for f in a.guardrail_flags}
        assert codes["banned_phrase"] == "block" and codes["required_missing"] == "warn"
        assert "Never use exclamation marks" in kb.company_context(db)
