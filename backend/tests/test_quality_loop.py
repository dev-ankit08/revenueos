"""Phase 2 Track A: prompt versions and workspace prompt additions, learning loop v2 (reviewer feedback,
admin approval, rollback), grader calibration, and the eval harness mechanics (with the AI mocked)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from revenueos import seed
from revenueos.agents import base, strategy
from revenueos.config import settings
from revenueos.db import session_scope
from revenueos.main import app
from revenueos.models import AgentRun, Approval, Contact, KBDocument, PromptVersion
from revenueos.services import approvals, governance, insights

UTC = timezone.utc


@pytest.fixture
def api():
    with TestClient(app) as client:
        yield client


@pytest.fixture
def seeded():
    seed.main()


def _cid(email="priya.sample@example.com") -> int:
    with session_scope() as db:
        return db.scalar(select(Contact.id).where(Contact.email == email))


class _Msgs:
    def __init__(self, seen):
        self.seen = seen

    def stream(self, **params):
        self.seen.append(params)

        class S:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get_final_message(self):
                usage = SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0,
                                        cache_creation_input_tokens=0)
                return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")], usage=usage,
                                       stop_reason="end_turn")
        return S()


@pytest.mark.anyio
async def test_prompt_versions_and_workspace_addendum(seeded, monkeypatch):
    seen = []
    monkeypatch.setattr(base, "client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=_Msgs(seen))))
    r1 = await base.run_agent(agent="outreach_writer", model="claude-haiku-5-5", role_prompt="Write well.", prompt="p")
    governance.set_prompt_addendum("outreach_writer", "Never mention pricing.")
    r2 = await base.run_agent(agent="outreach_writer", model="claude-haiku-5-5", role_prompt="Write well.", prompt="p")
    assert "Never mention pricing." in seen[1]["system"][1]["text"] and "Never mention" not in seen[0]["system"][1]["text"]
    with session_scope() as db:
        v1, v2 = db.get(AgentRun, r1.run_id).prompt_version, db.get(AgentRun, r2.run_id).prompt_version
        assert v1 and v2 and v1 != v2
        assert {p.version for p in db.scalars(select(PromptVersion))} >= {v1, v2}
        a = approvals.queue(db, kind="email", contact=db.get(Contact, _cid()), subject="s", body="Hi Priya.",
                            created_by_agent="outreach_writer", run_id=r2.run_id)
        assert a.prompt_version == v2


def test_prompt_endpoints_need_admin(seeded, api):
    api.post("/api/auth/bootstrap", json={"email": "o@us.example", "password": "a-long-password"})
    from revenueos.services import auth, workspaces

    auth.create_user("m@us.example", "member-password", workspaces.ensure_default().id, role="member")
    member = api.post("/api/auth/login", json={"email": "m@us.example", "password": "member-password"}).json()
    owner = api.post("/api/auth/login", json={"email": "o@us.example", "password": "a-long-password"}).json()
    M, O = {"Authorization": f"Bearer {member['token']}"}, {"Authorization": f"Bearer {owner['token']}"}
    assert api.put("/api/governance/outreach_writer/prompt", json={"text": "x"}, headers=M).status_code == 403
    r = api.put("/api/governance/outreach_writer/prompt", json={"text": "Keep it under 90 words."}, headers=O)
    assert r.json()["prompt_addendum"] == "Keep it under 90 words."


def _sent_with_reply(db, cid, n, reply_every):
    from revenueos.models import Activity

    for i in range(n):
        at = datetime.now(UTC) - timedelta(days=40, hours=i)
        a = Approval(kind="email", contact_id=cid, subject=f"s{i}", body="b", status="sent", sent_at=at,
                     decided_at=at, created_by_agent="outreach_writer")
        db.add(a)
        db.flush()
        if i % reply_every == 0:
            db.add(Activity(contact_id=cid, type="reply_interested", detail="y", created_at=at + timedelta(minutes=5)))


@pytest.mark.anyio
async def test_learning_uses_reviewer_feedback_and_needs_admin_approval(seeded, monkeypatch):
    object.__setattr__(settings, "learn_min_sent", 10)
    object.__setattr__(settings, "learn_min_replies", 2)
    cid = _cid()
    with session_scope() as db:
        _sent_with_reply(db, cid, 12, 4)
        db.add(Approval(kind="email", contact_id=cid, subject="x", body="Hi Priya, short.", ai_body="Hi Priya, long intro...",
                        edit_pct=40, status="sent", decided_at=datetime.now(UTC), created_by_agent="outreach_writer"))
        db.add(Approval(kind="email", contact_id=cid, subject="y", body="b", status="rejected", reject_reason="too_generic",
                        review_note="No hook", decided_at=datetime.now(UTC), created_by_agent="outreach_writer"))
    prompts = []

    def fake(pb):
        async def run_agent(**kw):
            prompts.append(kw["prompt"])
            return SimpleNamespace(parsed=pb, run_id=None, text="")
        return run_agent

    pb = lambda s: strategy.Playbook(summary=s, what_works=["w"], what_to_avoid=["Generic openers (1 rejection)"],
                                     subject_lines=[], by_persona=[], confidence="low", sample_note="n")
    monkeypatch.setattr(strategy, "run_agent", fake(pb("first")))
    r1 = await strategy.learn_from_results()
    assert r1["status"] == "draft" and "too_generic" in prompts[0] and "long intro" in prompts[0]
    with session_scope() as db:
        assert db.scalar(select(KBDocument).where(KBDocument.title == strategy.PLAYBOOK_TITLE)) is None
    strategy.publish_playbook(r1["document_id"], "admin")
    monkeypatch.setattr(strategy, "run_agent", fake(pb("second")))
    r2 = await strategy.learn_from_results()
    strategy.publish_playbook(r2["document_id"], "admin")
    with session_scope() as db:
        assert "second" in db.scalar(select(KBDocument).where(KBDocument.title == strategy.PLAYBOOK_TITLE)).content
    strategy.rollback_playbook("admin")
    with session_scope() as db:
        assert "first" in db.scalar(select(KBDocument).where(KBDocument.title == strategy.PLAYBOOK_TITLE)).content
        statuses = {v["id"]: v["status"] for v in strategy.playbook_versions(db)}
        assert statuses[r1["document_id"]] == "published" and statuses[r2["document_id"]] == "rolled_back"


def test_grader_calibration_compares_scores_with_reviewer_decisions(seeded):
    cid = _cid()
    now = datetime.now(UTC)
    with session_scope() as db:
        for i in range(12):
            db.add(Approval(kind="email", contact_id=cid, subject="s", body="b", status="sent", decided_at=now,
                            personalisation_score=85, edit_pct=0, created_by_agent="outreach_writer"))
            db.add(Approval(kind="email", contact_id=cid, subject="s", body="b", status="sent", decided_at=now,
                            personalisation_score=45, edit_pct=50, created_by_agent="outreach_writer"))
    with session_scope() as db:
        cal = insights.draft_quality(db)["grader_calibration"]
    assert cal["80_plus"]["approved_unedited_pct"] == 100.0 and cal["under_60"]["approved_unedited_pct"] == 0.0
    assert cal["verdict"] == "agrees with reviewers"


# ------------------------------------------------------------------ eval harness (AI mocked)

def test_eval_compare_and_summary():
    from evals.run import compare, summarise

    s = summarise("triage", [{"id": "a", "passed": True}, {"id": "b", "passed": False}])
    assert s["score"] == 0.5 and s["passed"] == 1 and s["failures"][0]["id"] == "b"
    assert compare({"triage": {"score": 0.5}}, {"triage": {"score": 0.9}}) == ["triage: 0.50 < baseline 0.90"]
    assert compare({"triage": {"score": 0.88}}, {"triage": {"score": 0.9}}) == []  # within tolerance
    assert compare({"writer": {"score": 0.1}}, {}) == []  # no baseline yet


@pytest.mark.anyio
async def test_triage_suite_scores_accuracy(seeded, monkeypatch):
    from evals import run as evals_run
    from evals.cases import REPLIES
    from revenueos.agents import replies

    async def fake_classify(contact_id, text, subject=None, parent_run_id=None):
        expected = next(e for _, t, e in REPLIES if t == text)
        got = "other" if expected == "objection" else expected  # one deliberate miss
        return SimpleNamespace(category=got), None
    monkeypatch.setattr(replies, "classify_reply", fake_classify)
    results = await evals_run.suite_triage()
    s = evals_run.summarise("triage", results)
    assert s["cases"] == len(REPLIES) and s["passed"] == len(REPLIES) - 1
