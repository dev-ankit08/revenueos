"""Phase 3 agents. The fake model 'calls' the agents' real tools, so queueing/filtering logic is exercised."""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from revenueos import seed
from revenueos.agents import intel, marketing
from revenueos.agents.base import AgentResult, new_run
from revenueos.db import session_scope
from revenueos.main import app
from revenueos.models import (Account, AccountBrief, Approval, Campaign, CampaignMember, Contact, KBDocument,
                              SalesDocument, Signal)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def seeded():
    seed.main()


def fake_agent(calls: dict[str, list[dict]] | None = None, parsed=None):
    """run_agent stand-in: invokes the named client tools with the given inputs, in order."""
    async def _run(**kwargs):
        tools = {t.to_dict()["name"]: t for t in kwargs.get("tools", []) if hasattr(t, "to_dict")}
        for name, inputs in (calls or {}).items():
            for payload in inputs:
                await tools[name].call(payload)
        run_id = kwargs.get("run_id") or new_run(kwargs["agent"], kwargs["model"], kwargs["prompt"])
        return AgentResult(text="done", run_id=run_id, parsed=parsed)
    return _run


def _account(name: str) -> Account:
    with session_scope() as db:
        return db.scalar(select(Account).where(Account.name == name))


PLAN = marketing.CampaignPlan(
    name="Manufacturing data Q1", objective="30 meetings with data leaders",
    kpis=[marketing.Kpi(metric="Meetings", target="30")],
    audience=marketing.Audience(industries=["Manufacturing", "Logistics"], title_keywords=["Data"], regions=["EU"]),
    key_message="Stop forecasting in spreadsheets", value_props=["Fast assessment"], offer="Free data assessment workshop",
    channels=[marketing.ChannelMix(channel="LinkedIn", budget_pct=60, rationale="Where they are"),
              marketing.ChannelMix(channel="Email", budget_pct=40, rationale="Direct")],
    timeline=[marketing.TimelineWeek(week=1, activities=["Launch"])],
    content_plan=[marketing.ContentItem(content_type="linkedin_post", title="Spreadsheet forecasting is costing you",
                                        angle="Pain", channel="LinkedIn", week=1)],
    sales_play="SDRs call engaged contacts within 48h", risks=["Small audience"],
)


@pytest.mark.anyio
async def test_campaign_plan_targets_matching_contacts_and_drafts_content(seeded, monkeypatch):
    monkeypatch.setattr(marketing, "run_agent", fake_agent(parsed=PLAN))
    r = await marketing.plan_campaign("30 meetings with data leaders in manufacturing", budget=20000)
    with session_scope() as db:
        c = db.get(Campaign, r["campaign_id"])
        names = {db.get(Contact, m.contact_id).full_name
                 for m in db.scalars(select(CampaignMember).where(CampaignMember.campaign_id == c.id))}
    # "Data" in the title + Manufacturing/Logistics: Priya (CDO, Siemens) and Elena (Data Eng, Maersk)
    assert names == {"Priya Sample", "Elena Test"}

    queued = {"queue_content": [{"content_type": "linkedin_post", "title": "Spreadsheets cost you",
                                 "body": "Forecast accuracy went from 61% to 84% for a manufacturer.",
                                 "rationale": "Pain angle"}]}
    monkeypatch.setattr(marketing, "run_agent", fake_agent(queued))
    await marketing.generate_campaign_content(r["campaign_id"])
    with session_scope() as db:
        a = db.scalar(select(Approval).where(Approval.campaign_id == r["campaign_id"]))
        assert (a.kind, a.content_type) == ("content", "linkedin_post")
        assert not any(f["code"] == "unverified_claims" for f in a.guardrail_flags)  # figures are in the KB


@pytest.mark.anyio
async def test_content_with_invented_figure_is_flagged(seeded, monkeypatch):
    queued = {"queue_content": [{"content_type": "ad", "title": "Ad", "body": "Cut costs 73% in 30 days.",
                                 "rationale": "x"}]}
    monkeypatch.setattr(marketing, "run_agent", fake_agent(queued))
    await marketing.create_content("ad", "Promote the assessment")
    with session_scope() as db:
        a = db.scalar(select(Approval).where(Approval.kind == "content"))
        assert any(f["code"] == "unverified_claims" and "73%" in f["message"] for f in a.guardrail_flags)


@pytest.mark.anyio
async def test_abm_pack_queues_assets_and_one_email_per_contact(seeded, monkeypatch):
    acc = _account("Siemens")
    with session_scope() as db:
        db.add(AccountBrief(account_id=acc.id, summary="Industrial leader", recommended_angle="Forecasting"))
        cid = db.scalar(select(Contact.id).where(Contact.account_id == acc.id))
    calls = {
        "queue_content": [
            {"content_type": t, "title": t, "body": f"{t} for Siemens", "rationale": "ABM"}
            for t in ("landing_page", "ad", "one_pager")
        ],
        "queue_message": [{"contact_id": cid, "channel": "email", "sequence_step": 1, "send_after_days": 0,
                           "subject": "forecasting at Siemens", "body": "Hi Priya, a thought.", "rationale": "ABM"}],
    }
    monkeypatch.setattr(marketing, "run_agent", fake_agent(calls))
    from revenueos.services import personalisation

    async def high_grade(**kw):
        return personalisation.Grade(score=90, specific_facts_used=["forecasting"], unsupported_claims=[],
                                     generic_phrases=[], feedback="")
    monkeypatch.setattr(personalisation, "grade", high_grade)
    await marketing.abm_pack(acc.id)
    with session_scope() as db:
        rows = list(db.scalars(select(Approval).where(Approval.account_id == acc.id)))
        assert sorted(r.content_type or r.kind for r in rows) == ["ad", "email", "landing_page", "one_pager"]
        assert db.scalar(select(SalesDocument).where(SalesDocument.kind == "abm_strategy"))


CARD = {"competitor": "BigCo Consulting", "overview": "Large SI.", "target_customers": "Global 2000",
        "strengths": ["Scale"], "weaknesses": ["Junior staffing"], "recent_moves": ["2026-09 - new AI unit - https://x"],
        "pricing_notes": "T&M", "how_we_win": ["Senior team"], "landmine_questions": ["Who staffs it?"],
        "objection_responses": ["They say scale -> we say speed"], "sources": ["https://x"]}


@pytest.mark.anyio
async def test_battlecard_saved_and_kb_copy_replaced(seeded, monkeypatch):
    monkeypatch.setattr(intel, "run_agent", fake_agent({"submit_battlecard": [CARD]}))
    await intel.competitor_battlecard("BigCo")
    await intel.competitor_battlecard("BigCo")
    with session_scope() as db:
        kb_docs = db.scalar(select(func.count()).select_from(KBDocument)
                            .where(KBDocument.title == "Battlecard: BigCo Consulting"))
        assert kb_docs == 1  # refreshed in place, not duplicated
        assert db.scalar(select(func.count()).select_from(SalesDocument).where(SalesDocument.kind == "battlecard")) == 2


@pytest.mark.anyio
async def test_signals_filter_unknown_accounts_old_events_and_duplicates(seeded, monkeypatch):
    acc = _account("Maersk")
    recent = date.today().isoformat()
    old = (date.today() - timedelta(days=400)).isoformat()
    sig = lambda **kw: {"account_id": acc.id, "type": "new_executive", "headline": "New CDO", "detail": "d",  # noqa: E731
                        "url": "https://news/1", "event_date": recent, "strength": "high",
                        "recommended_action": "Email the new CDO"} | kw
    payload = {"signals": [sig(), sig(account_id=99999), sig(url="https://news/old", event_date=old)]}
    monkeypatch.setattr(intel, "run_agent", fake_agent({"submit_signals": [payload]}))
    r = await intel.detect_signals([acc.id])
    assert r["signals"] == 1
    r = await intel.detect_signals([acc.id])
    assert r["signals"] == 0  # same URL already recorded
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(Signal)) == 1


@pytest.mark.anyio
async def test_sourcer_skips_existing_domains(seeded, monkeypatch):
    found = {"accounts": [
        {"name": "Siemens AG", "domain": "https://www.siemens.com/", "industry": "Manufacturing",
         "fit_reason": "dup", "evidence_url": "https://x", "target_titles": ["CIO"]},
        {"name": "NewCo Logistics", "domain": "newco-logistics.example", "industry": "Logistics",
         "employees_estimate": 2000, "fit_reason": "Announced data platform program", "evidence_url": "https://y",
         "target_titles": ["CIO", "Head of Data"]},
    ]}
    monkeypatch.setattr(intel, "run_agent", fake_agent({"submit_accounts": [found]}))
    r = await intel.source_accounts("logistics firms", 5)
    assert r == {"added": ["NewCo Logistics"], "skipped_duplicates": 1}
    assert _account("NewCo Logistics").source == "sourced"


@pytest.mark.anyio
async def test_agent_that_never_submits_fails_loudly(seeded, monkeypatch):
    monkeypatch.setattr(intel, "run_agent", fake_agent({}))
    with pytest.raises(Exception, match="without calling submit_accounts"):
        await intel.source_accounts("anything", 3)


def test_csv_import_maps_provider_headers(seeded):
    csv_text = ("First Name,Last Name,Title,Email,Company Name,Website,# Employees,Industry\n"
                "Ravi,Kumar,CIO,ravi.kumar@example.com,Acme Freight,https://www.acmefreight.example,1200,Logistics\n"
                ",,,,,,,\n")
    with TestClient(app) as api:
        r = api.post("/api/leads/import-csv", files={"file": ("apollo.csv", csv_text, "text/csv")})
        assert r.status_code == 200, r.text
        assert r.json()["imported"] == 1 and r.json()["skipped"] == 1
    a = _account("Acme Freight")
    assert (a.domain, a.employees, a.source) == ("acmefreight.example", 1200, "import")
