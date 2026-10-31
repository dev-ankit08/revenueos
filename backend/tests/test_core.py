from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from revenueos import seed
from revenueos.agents import base
from revenueos.db import session_scope
from revenueos.main import app
from revenueos.models import AgentRun, Approval, Contact
from revenueos.services import approvals, guardrails
from revenueos.tools import kb


@pytest.fixture
def api():
    with TestClient(app) as client:
        yield client


@pytest.fixture
def seeded():
    seed.main()


def _contact(email="priya.sample@example.com") -> Contact:
    with session_scope() as db:
        from sqlalchemy import select
        return db.scalar(select(Contact).where(Contact.email == email))


def test_kb_search_ranks_relevant_chunks(seeded):
    with session_scope() as db:
        hits = kb.search(db, "manufacturing demand forecasting inventory")
        ctx = kb.company_context(db)
    assert hits[0]["category"] == "case_study"
    assert "Manufacturer" in hits[0]["title"]
    assert "WHAT WE SELL" in ctx and "BRAND VOICE" in ctx


def test_guardrails_flag_unverified_numbers_and_placeholders(seeded):
    with session_scope() as db:
        g = guardrails.grounding_text(db)
        c = db.get(Contact, _contact().id)
        flags = guardrails.check_draft(
            db, c, "email", "quick idea",
            "Hi [First Name], we cut inventory costs 18% and boosted revenue 47% for a peer.", g,
        )
    codes = {f["code"]: f for f in flags}
    assert codes["placeholder"]["severity"] == "block"
    assert "47%" in codes["unverified_claims"]["message"]
    assert "18%" not in codes["unverified_claims"]["message"]  # in the case study, so grounded


def test_approve_sends_in_dry_run_and_logs_activity(seeded):
    cid = _contact().id
    with session_scope() as db:
        a = approvals.queue(db, kind="email", contact=db.get(Contact, cid), subject="forecasting at Siemens",
                            body="Hi Priya, a short note.", created_by_agent="test")
        aid = a.id
    with session_scope() as db:
        a = approvals.approve_and_send(db, db.get(Approval, aid), "rep@company.com")
        assert a.status == "sent"
        assert db.get(Contact, cid).status == "contacted"


def test_suppressed_contact_is_blocked(seeded, api):
    cid = _contact().id
    api.post("/api/webhooks/unsubscribe", json={"email": "priya.sample@example.com"})
    with session_scope() as db:
        aid = approvals.queue(db, kind="email", contact=db.get(Contact, cid), subject="hello",
                              body="Hi Priya.", created_by_agent="test").id
    r = api.post(f"/api/approvals/{aid}/approve", json={"reviewer": "rep"})
    assert r.status_code == 409 and "suppression" in r.json()["detail"]


def test_reply_webhook_cancels_followups(seeded, api):
    cid = _contact().id
    with session_scope() as db:
        approvals.queue(db, kind="email", contact=db.get(Contact, cid), subject="follow up",
                        body="Hi Priya, one more idea.", sequence_step=2, send_after_days=3,
                        created_by_agent="test")
    r = api.post("/api/webhooks/reply", json={"from_email": "priya.sample@example.com", "text": "Interesting, call me Tuesday.", "triage": False})
    assert r.json() == {"matched": True, "contact_id": cid, "status": "replied", "cancelled_followups": 1}


def test_api_leads_kb_and_analytics(seeded, api):
    assert api.get("/api/health").json()["crm"] == "local"
    leads = api.get("/api/leads").json()
    assert len(leads) == 4
    assert api.get(f"/api/leads/{leads[0]['contact_id']}").status_code == 200
    r = api.post("/api/kb", json={"title": "X", "category": "nonsense", "content": "y"})
    assert r.status_code == 422
    assert api.get("/api/analytics").json()["leads"]["total"] == 4


class _FakeMessages:
    """Stands in for client().beta.messages and returns a canned structured result."""

    def stream(self, **params):
        outer = self

        class _Stream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get_final_message(self):
                return await outer.parse(**params)
        return _Stream()

    async def parse(self, **params):
        assert params["model"] == "claude-haiku-5-5"
        assert "fallbacks" not in params  # no fallback for Haiku
        assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
        fmt = params["output_format"]
        parsed = fmt(fit_score=90, intent_score=40, total_score=70, tier="B", persona="Economic buyer",
                     reasons=["CDO at large manufacturer"], missing_data=[], disqualify=False,
                     recommended_action="research_and_sequence_now")
        usage = SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0,
                                cache_creation_input_tokens=0)
        return SimpleNamespace(content=[], usage=usage, stop_reason="end_turn", parsed_output=parsed)


@pytest.mark.anyio
async def test_scorer_updates_lead_and_logs_cost(seeded, monkeypatch):
    from revenueos.agents import scorer

    monkeypatch.setattr(base, "client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=_FakeMessages())))
    cid = _contact().id
    result = await scorer.score_contact(cid)
    assert result.tier == "B"
    with session_scope() as db:
        assert db.get(Contact, cid).score == 70
        run = db.query(AgentRun).filter_by(agent="lead_scorer").one()
        assert run.status == "succeeded" and run.cost_usd > 0


def test_inbound_webhook_runs_job_in_background(seeded, api, monkeypatch):
    """Sync endpoints run in a worker thread; jobs must still reach the event loop."""
    import time

    monkeypatch.setattr(base, "client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=_FakeMessages())))
    r = api.post("/api/webhooks/inbound", json={
        "first_name": "Ana", "last_name": "Example", "email": "ana@example.com",
        "company": "Example Corp", "title": "CIO", "message": "We need help with Snowflake.",
    })
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    for _ in range(50):
        run = api.get(f"/api/runs/{run_id}").json()
        if run["status"] != "running":
            break
        time.sleep(0.1)
    # The fake scorer returns tier B, so the workflow moves on to research, which the fake
    # client can't do (no tool runner); what matters is that the job ran and recorded its steps.
    assert run["status"] in ("succeeded", "failed")
    assert run["children"][0]["agent"] == "lead_scorer"
    assert run["children"][0]["status"] == "succeeded"


@pytest.fixture
def anyio_backend():
    return "asyncio"
