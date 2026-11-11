"""Accuracy & trust (plan Phase 0 + Track A items 1-2): the approval re-check uses the same evidence
as the original check, reviewer edits and reject reasons are kept, and identity comes from the session."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from revenueos import seed
from revenueos.db import session_scope
from revenueos.main import app
from revenueos.models import Approval, AuditEvent, Contact
from revenueos.services import approvals, auth, workspaces

BRIEF = "Account brief: Siemens grew its digital revenue 47% last year (https://example.com/siemens-results)."


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


def _queue(**kw) -> int:
    cid = _cid()
    with session_scope() as db:
        args = dict(kind="email", contact=db.get(Contact, cid), subject="Digital growth at Siemens",
                    body="Hi Priya, congratulations on growing digital revenue 47% last year.",
                    created_by_agent="outreach_writer", grounding_extra=BRIEF)
        args.update(kw)
        return approvals.queue(db, **args).id


def _codes(aid: int) -> set[str]:
    with session_scope() as db:
        return {f["code"] for f in db.get(Approval, aid).guardrail_flags}


def test_recheck_at_approval_uses_the_brief_not_just_the_kb(seeded):
    aid = _queue()
    assert "unverified_claims" not in _codes(aid)  # 47% is in the brief
    with session_scope() as db:
        a = approvals.approve_and_send(db, db.get(Approval, aid), "rep")
        assert a.status == "sent"
    assert "unverified_claims" not in _codes(aid)  # before the fix, the KB-only re-check flagged it


def test_personalisation_warning_survives_approval(seeded):
    aid = _queue()
    with session_scope() as db:
        a = db.get(Approval, aid)
        a.guardrail_flags = [*a.guardrail_flags, {"severity": "warn", "code": "low_personalisation",
                                                   "message": "Personalisation 41/100 after one rewrite."}]
    with session_scope() as db:
        approvals.approve_and_send(db, db.get(Approval, aid), "rep")
    assert "low_personalisation" in _codes(aid)


def test_claims_are_rechecked_after_edits(seeded):
    aid = _queue(claims=[{"text": "47% digital growth", "source": "https://example.com/siemens-results"}])
    assert "unsourced_claim" not in _codes(aid)
    with session_scope() as db:
        a = db.get(Approval, aid)
        a.claims = [{"text": "47% digital growth", "source": "https://made-up.example/page"}]
    with session_scope() as db:
        approvals.approve_and_send(db, db.get(Approval, aid), "rep")
    assert "unsourced_claim" in _codes(aid)


def test_edits_keep_the_ai_original_and_measure_the_change(seeded):
    untouched, edited = _queue(), _queue()
    with session_scope() as db:
        approvals.approve_and_send(db, db.get(Approval, untouched), "rep")
        approvals.approve_and_send(db, db.get(Approval, edited), "rep",
                                   body="Hi Priya, saw the digital revenue news, well done. Worth a chat?")
    with session_scope() as db:
        u, e = db.get(Approval, untouched), db.get(Approval, edited)
        assert u.edit_pct == 0 and u.ai_body is None
        assert e.ai_body.startswith("Hi Priya, congratulations") and e.body.startswith("Hi Priya, saw")
        assert 0 < e.edit_pct <= 100
        actions = [ev.action for ev in db.scalars(select(AuditEvent))]
        assert actions.count("approval.approve") == 2


def test_blocked_edit_still_keeps_original(seeded):
    aid = _queue()
    with session_scope() as db, pytest.raises(approvals.ApprovalError):
        approvals.approve_and_send(db, db.get(Approval, aid), "rep", body="Hi [First Name], quick one.")
    with session_scope() as db:
        a = db.get(Approval, aid)
        assert a.status == "pending" and a.ai_body.startswith("Hi Priya, congratulations")


def test_reject_reason_is_validated_stored_and_reported(seeded, api):
    a1, a2, a3 = _queue(), _queue(), _queue()
    assert api.post(f"/api/approvals/{a1}/reject", json={"reason": "bogus"}).status_code == 409
    assert api.post(f"/api/approvals/{a1}/reject", json={"reason": "too_generic", "note": "No hook"}).status_code == 200
    assert api.post(f"/api/approvals/{a2}/approve", json={}).json()["edit_pct"] == 0
    api.post(f"/api/approvals/{a3}/approve", json={"body": "Hi Priya, totally different words here."})
    assert {r["code"] for r in api.get("/api/approvals/reject-reasons").json()} >= {"wrong_facts", "too_generic"}

    q = api.get("/api/insights/quality").json()
    assert q["decided"] == 3 and q["rejected"] == 1
    assert q["approved_unedited_pct"] == 50.0
    assert q["reject_reasons"] == {"too_generic": 1}
    assert q["by_agent"]["outreach_writer"]["approved"] == 2


def test_reviewer_comes_from_the_session_and_governance_needs_admin(seeded, api):
    owner = api.post("/api/auth/bootstrap",
                     json={"email": "owner@us.example", "password": "a-long-password", "name": "Olu Owner"}).json()
    auth.create_user("rep@us.example", "rep-password-1", workspaces.ensure_default().id, role="member", name="Rae Rep")
    rep = api.post("/api/auth/login", json={"email": "rep@us.example", "password": "rep-password-1"}).json()
    O, R = {"Authorization": f"Bearer {owner['token']}"}, {"Authorization": f"Bearer {rep['token']}"}

    aid = _queue()
    r = api.post(f"/api/approvals/{aid}/approve", json={"reviewer": "Somebody Else"}, headers=R)
    assert r.status_code == 200 and r.json()["reviewed_by"] == "Rae Rep"

    body = {"monthly_budget_usd": 50, "enabled": True}
    assert api.put("/api/governance/*", json=body, headers=R).status_code == 403
    assert api.put("/api/governance/*", json=body, headers=O).status_code == 200
    with session_scope() as db:
        ev = db.scalar(select(AuditEvent).where(AuditEvent.action == "governance.update"))
        assert ev.actor == "Olu Owner" and ev.object_id == "*"
