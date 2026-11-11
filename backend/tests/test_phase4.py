"""Phase 4: forecast math, attribution, A/B testing, Deal Strategist, Forecast Analyst, learning loop."""
from datetime import datetime, timedelta, timezone

import pytest

from revenueos.config import settings
from fastapi.testclient import TestClient
from sqlalchemy import select

from revenueos import seed
from revenueos.agents import strategy
from revenueos.agents.base import AgentResult, new_run
from revenueos.db import session_scope
from revenueos.main import app
from revenueos.models import Account, Activity, Approval, Contact, KBDocument, Opportunity, SalesDocument
from revenueos.services import approvals, insights
from revenueos.tools import kb


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def seeded():
    seed.main()


NOW = datetime.now(timezone.utc)


def fake_run_agent(parsed, captured: list | None = None):
    async def _run(**kwargs):
        if captured is not None:
            captured.append(kwargs)
        return AgentResult(text="ok", run_id=new_run(kwargs["agent"], kwargs["model"], kwargs["prompt"]), parsed=parsed)
    return _run


def _contact(email: str) -> Contact:
    with session_scope() as db:
        return db.scalar(select(Contact).where(Contact.email == email))


def _sent(db, contact_id: int, when: datetime, variant="A", step=1, agent="outreach_writer", subject="s") -> Approval:
    a = Approval(kind="email", status="sent", contact_id=contact_id, subject=subject, body="b", variant=variant,
                 sequence_step=step, created_by_agent=agent, sent_at=when, guardrail_flags=[])
    db.add(a)
    db.flush()
    return a


def test_forecast_numbers(seeded):
    _, q_end = insights.quarter_bounds(NOW)
    in_q = (q_end - timedelta(days=2)).date().isoformat()
    past = (NOW - timedelta(days=5)).date().isoformat()
    with session_scope() as db:
        acc = db.scalar(select(Account).where(Account.name == "Siemens"))
        db.add_all([
            Opportunity(account_id=acc.id, name="Commit deal", stage="negotiation", amount=100_000, close_date=in_q),
            Opportunity(account_id=acc.id, name="Best case deal", stage="proposal", amount=50_000, close_date=in_q),
            Opportunity(account_id=acc.id, name="Slipped", stage="discovery", amount=20_000, close_date=past),
            Opportunity(account_id=acc.id, name="Next quarter", stage="solution", amount=999_000, close_date="2099-01-01"),
        ])
        won = Opportunity(account_id=acc.id, name="Won", stage="proposal", amount=30_000)
        db.add(won)
        db.flush()
        won.stage = "closed_won"
    with session_scope() as db:
        n = insights.forecast_numbers(db, target=300_000)
    assert n["closed_won_this_quarter"] == 30_000
    assert n["commit"] == 100_000                       # negotiation = 80%
    assert n["best_case"] == 150_000                    # commit + proposal (60%)
    assert n["weighted"] == 100_000 * .8 + 50_000 * .6 + 20_000 * .1  # next-quarter deal excluded
    assert n["slipped_deals"] == ["Slipped"]
    assert n["coverage_ratio"] == round(170_000 / 270_000, 2)
    assert n["historical_win_rate"] == 100.0


def test_last_touch_attribution_and_campaigns(seeded):
    cid = _contact("priya.sample@example.com").id
    with session_scope() as db:
        first = _sent(db, cid, NOW - timedelta(days=10), step=1)
        second = _sent(db, cid, NOW - timedelta(days=3), step=2)
        db.add(Activity(contact_id=cid, type="reply", detail="raw", created_at=NOW - timedelta(days=1)))
        db.add(Activity(contact_id=cid, type="reply_interested", detail="x", created_at=NOW - timedelta(days=1)))
        first_id, second_id = first.id, second.id
    with session_scope() as db:
        credited = insights.credited_replies(db)
        assert credited == {second_id: "reply_interested"} and first_id not in credited
        steps = {r["step"]: r for r in insights.attribution(db)["by_sequence_step"]}
        assert steps[2]["positive_rate"] == 100.0 and steps[1]["replies"] == 0


def test_ab_variants_randomly_assigned_one_per_contact(seeded):
    cid = _contact("elena.test@example.com").id
    kept = set()
    for _ in range(30):
        with session_scope() as db:
            c = db.get(Contact, cid)
            a = approvals.queue(db, kind="email", contact=c, subject="A", body="Hi Elena.", created_by_agent="t", variant="A")
            b = approvals.queue(db, kind="email", contact=c, subject="B", body="Hi Elena.", created_by_agent="t", variant="B")
            pending = [x for x in (a, b) if x.status == "pending"]
            assert len(pending) == 1
            kept.add(pending[0].variant)
            for x in (a, b):  # clear for next round
                x.status = "rejected"
    assert kept == {"A", "B"}  # both outcomes occur: the assignment is random


def test_ab_significance(seeded):
    cid = _contact("elena.test@example.com").id
    with session_scope() as db:
        for variant, wins in (("A", 2), ("B", 14)):
            for i in range(40):
                a = _sent(db, cid, NOW - timedelta(days=30, minutes=i + (100 if variant == "B" else 0)), variant=variant)
                if i < wins:
                    # A reply right after this send, before the next one
                    db.add(Activity(contact_id=cid, type="reply_interested", detail="y",
                                    created_at=a.sent_at + timedelta(seconds=30)))
    with session_scope() as db:
        [test] = insights.ab_results(db)
    assert test["A"]["sent"] == 40 and test["B"]["sent"] == 40
    assert test["winner"] == "B" and test["p_value"] < 0.05


STRATEGY = strategy.DealStrategy(
    health="red", win_probability=25, forecast_category="pipeline", assessment="Single-threaded, CFO unknown.",
    stakeholders=[strategy.Stakeholder(name_or_title="Priya Sample", role_in_deal="champion", engagement="engaged",
                                       action="Ask her to introduce the CFO")],
    missing_roles=["Economic buyer (CFO)"], risks=["No economic buyer"],
    next_best_actions=[strategy.NextAction(action="Get CFO meeting", owner="rep", due_in_days=7, why="No budget owner")],
    talk_track="Priya, to make this real we need the CFO.",
    draft_email=strategy.DraftEmail(contact_id=0, subject="Bringing in finance", body="Hi Priya, could we include your CFO?"),
)


@pytest.mark.anyio
async def test_deal_strategist_updates_deal_and_drafts_only_for_known_contacts(seeded, monkeypatch):
    cid = _contact("priya.sample@example.com").id
    with session_scope() as db:
        acc = db.get(Contact, cid).account_id
        o = Opportunity(account_id=acc, primary_contact_id=cid, name="Siemens data", stage="qualification", amount=80_000)
        db.add(o)
        db.flush()
        oid = o.id

    good = STRATEGY.model_copy(update={"draft_email": STRATEGY.draft_email.model_copy(update={"contact_id": cid})})
    monkeypatch.setattr(strategy, "run_agent", fake_run_agent(good))
    r = await strategy.review_deal(oid)
    assert r["approval_id"] is not None
    with session_scope() as db:
        o = db.get(Opportunity, oid)
        assert (o.health, o.win_probability, o.forecast_category) == ("red", 25, "pipeline")
        assert o.next_steps == ["Get CFO meeting"]

    bad = STRATEGY.model_copy(update={"draft_email": STRATEGY.draft_email.model_copy(update={"contact_id": 999})})
    monkeypatch.setattr(strategy, "run_agent", fake_run_agent(bad))
    assert (await strategy.review_deal(oid))["approval_id"] is None  # contact not at this account


@pytest.mark.anyio
async def test_forecast_agent_gets_computed_numbers(seeded, monkeypatch):
    report = strategy.ForecastReport(headline="On track if Siemens closes", confidence="medium", narrative="n",
                                     key_risks=["r"], deals_to_focus=[], pipeline_gaps=[], recommendations=["do x"])
    captured = []
    monkeypatch.setattr(strategy, "run_agent", fake_run_agent(report, captured))
    r = await strategy.forecast(target=500_000)
    assert '"target": 500000' in captured[0]["prompt"]
    with session_scope() as db:
        assert "On track if Siemens closes" in db.get(SalesDocument, r["document_id"]).content


@pytest.mark.anyio
async def test_learning_needs_enough_data_then_updates_playbook(seeded, monkeypatch):
    calls = []
    monkeypatch.setattr(strategy, "run_agent", fake_run_agent(None, calls))
    r = await strategy.learn_from_results()
    assert r["updated"] is False and not calls  # no AI spend on too little data

    cid = _contact("priya.sample@example.com").id
    with session_scope() as db:
        for i in range(25):
            a = _sent(db, cid, NOW - timedelta(days=40, hours=i))
            if i % 6 == 0:
                db.add(Activity(contact_id=cid, type="reply_interested", detail="y",
                                created_at=a.sent_at + timedelta(minutes=5)))
    pb = strategy.Playbook(summary="Short, specific openers win", what_works=["Lead with their news (5/5 replies)"],
                           what_to_avoid=["Long intros"], subject_lines=["2-4 words"], by_persona=[],
                           confidence="low", sample_note="25 emails, 5 replies")
    monkeypatch.setattr(strategy, "run_agent", fake_run_agent(pb))
    r = await strategy.learn_from_results()
    assert r["updated"] is False and "Need at least 50" in r["reason"]  # 25 sends: below the bar
    object.__setattr__(settings, "learn_min_sent", 20)
    object.__setattr__(settings, "learn_min_replies", 3)
    r = await strategy.learn_from_results()
    # A proposal, not live: an admin approves it first.
    assert r["drafted"] and r["status"] == "draft" and r["emails_analysed"] == 25
    with session_scope() as db:
        assert db.scalar(select(KBDocument).where(KBDocument.title == strategy.PLAYBOOK_TITLE)) is None
    strategy.publish_playbook(r["document_id"], "admin")
    with session_scope() as db:
        assert db.scalar(select(KBDocument).where(KBDocument.title == strategy.PLAYBOOK_TITLE)).category == "playbook"
        assert "WHAT WORKS" in kb.company_context(db)  # every agent now sees it


def test_insights_api(seeded):
    with TestClient(app) as api:
        assert api.get("/api/forecast/numbers?target=100000").json()["target"] == 100000
        assert "by_campaign" in api.get("/api/insights/attribution").json()
        assert api.get("/api/insights/ab-tests").json() == []
        assert api.get("/api/insights/playbook").json() == {}
        assert api.post("/api/deals/999/review").status_code == 404
