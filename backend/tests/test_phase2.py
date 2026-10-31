"""Phase 2 agents with the model call replaced by canned structured outputs."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from revenueos import seed
from revenueos.agents import deals, hygiene as hygiene_agent, proposals, replies
from revenueos.agents.base import AgentResult, new_run
from revenueos.config import settings
from revenueos.db import session_scope
from revenueos.main import app
from revenueos.models import Account, Approval, Contact, Opportunity, SalesDocument
from revenueos.services import approvals, hygiene


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def seeded():
    seed.main()


def _priya() -> int:
    with session_scope() as db:
        return db.scalar(select(Contact.id).where(Contact.email == "priya.sample@example.com"))


def fake_run_agent(parsed, captured: list | None = None):
    async def _run(**kwargs):
        if captured is not None:
            captured.append(kwargs)
        return AgentResult(text="ok", run_id=new_run(kwargs["agent"], kwargs["model"], kwargs["prompt"]), parsed=parsed)
    return _run


@pytest.mark.anyio
async def test_out_of_office_resumes_paused_followups(seeded, monkeypatch):
    cid = _priya()
    with session_scope() as db:
        a = approvals.queue(db, kind="email", contact=db.get(Contact, cid), subject="idea",
                            body="Hi Priya.", sequence_step=2, send_after_days=3, created_by_agent="t")
        a.status = "rejected"  # as the webhook leaves it
        aid = a.id
    ooo = replies.ReplyTriage(category="out_of_office", sentiment="neutral", summary="Away until the 10th.",
                              urgency="low", needs_response=False, return_date="2026-11-10",
                              suggested_next_step="Wait")
    monkeypatch.setattr(replies, "run_agent", fake_run_agent(ooo))
    await replies.triage_reply(cid, "I'm out of office", cancelled=[(aid, "pending")])
    with session_scope() as db:
        assert db.get(Approval, aid).status == "pending"
        assert db.get(Contact, cid).status == "contacted"


@pytest.mark.anyio
async def test_referral_creates_lead_and_drafts_reply(seeded, monkeypatch):
    cid = _priya()
    t = replies.ReplyTriage(category="referral", sentiment="positive", summary="Talk to Jon.", urgency="medium",
                            needs_response=True, referral_name="Jon Ref", referral_email="jon.ref@example.com",
                            referral_title="Head of Data", suggested_next_step="Email Jon")
    monkeypatch.setattr(replies, "run_agent", fake_run_agent(t))
    drafted = []

    async def fake_draft(*args, **kwargs):
        drafted.append(args)
        return "queued"
    monkeypatch.setattr(replies, "draft_reply", fake_draft)
    out = await replies.triage_reply(cid, "Please talk to Jon (jon.ref@example.com)")
    assert "referral" in out and drafted
    with session_scope() as db:
        jon = db.scalar(select(Contact).where(Contact.email == "jon.ref@example.com"))
        assert jon.source == "referral" and jon.title == "Head of Data"


CALL = deals.CallAnalysis(
    summary="Discussed forecasting pain across plants.",
    meddicc=deals.Meddicc(metrics="Reduce stock-outs", economic_buyer="CFO (not met)", decision_criteria="Unknown",
                          decision_process="Unknown", identify_pain="Spreadsheet forecasts", champion="Priya",
                          competition="Large integrator"),
    stage="qualification", amount_estimate=120000, close_date_estimate="2027-03-31",
    buying_signals=["Asked about timelines"], risks=["CFO not engaged"],
    next_steps=["Send assessment outline by Friday (us)"], questions_for_next_call=["Who signs?"], health="amber",
    follow_up_email=deals.FollowUp(subject="Great talking today", body="Hi Priya, thanks for the time today."),
)


@pytest.mark.anyio
async def test_call_analysis_creates_deal_doc_and_followup(seeded, monkeypatch):
    cid = _priya()
    monkeypatch.setattr(deals, "run_agent", fake_run_agent(CALL))
    r = await deals.analyze_call(cid, "Transcript text " * 10)
    with session_scope() as db:
        opp = db.get(Opportunity, r["opportunity_id"])
        assert (opp.stage, opp.amount, opp.health) == ("qualification", 120000, "amber")
        assert opp.qualification["champion"] == "Priya"
        assert db.get(Approval, r["approval_id"]).created_by_agent == "call_analyst"
        assert db.get(Contact, cid).status == "meeting"
    # A second call updates the same open deal rather than creating another
    r2 = await deals.analyze_call(cid, "More transcript " * 10)
    assert r2["opportunity_id"] == r["opportunity_id"]


@pytest.mark.anyio
async def test_proposal_renders_files_and_advances_stage(seeded, monkeypatch, tmp_path):
    cid = _priya()
    monkeypatch.setattr(deals, "run_agent", fake_run_agent(CALL))
    await deals.analyze_call(cid, "Transcript text " * 10)

    content = proposals.ProposalContent(
        title="Forecasting modernization for Siemens", executive_summary="Para one.\n\nPara two.",
        client_situation=["Spreadsheet forecasts"], objectives=["Better accuracy"],
        approach=[proposals.Phase(name="Assessment", duration="4 weeks", activities=["Map data"], deliverables=["Roadmap"])],
        why_us=["Senior team"], proof_points=[proposals.ProofPoint(title="Global manufacturer", result="61% to 84%", why_relevant="Same problem")],
        investment="Fixed-price assessment from $40,000.", assumptions=["Data access"], next_steps=["Sign assessment"],
        deck=[proposals.Slide(title="Your situation", bullets=["Spreadsheets"], speaker_notes="Open with their words")],
    )
    captured = []
    monkeypatch.setattr(proposals, "run_agent", fake_run_agent(content, captured))
    original_dir = settings.generated_dir
    object.__setattr__(settings, "generated_dir", tmp_path)  # Settings is frozen
    try:
        r = await proposals.build_proposal(cid, "Focus on forecasting")
        await _check_proposal(r, captured, tmp_path)
    finally:
        object.__setattr__(settings, "generated_dir", original_dir)


async def _check_proposal(r, captured, tmp_path):
    assert "call_summaries" in captured[0]["prompt"] and "Discussed forecasting" in captured[0]["prompt"]
    with session_scope() as db:
        doc = db.get(SalesDocument, r["proposal_document_id"])
        deck = db.get(SalesDocument, r["deck_document_id"])
        assert (tmp_path / doc.file_name).exists() and (tmp_path / deck.file_name).exists()
        assert db.scalar(select(Opportunity)).stage == "proposal"

    with TestClient(app) as api:
        resp = api.get(f"/api/documents/{deck.id}/file")
        assert resp.status_code == 200 and len(resp.content) > 1000


@pytest.mark.anyio
async def test_hygiene_finds_duplicates_and_merges(seeded, monkeypatch):
    with session_scope() as db:
        acc = db.scalar(select(Account).where(Account.name == "Siemens"))
        db.add(Contact(account_id=acc.id, first_name="Priya", last_name="Sample", title=None,
                       email="PRIYA.SAMPLE@example.com"))
    enrich = hygiene_agent.Enrichment(accounts=[])
    monkeypatch.setattr(hygiene_agent, "run_agent", fake_run_agent(enrich))
    r = await hygiene_agent.run_hygiene(enrich=True)
    assert r["issue_counts"]["duplicate_contacts"] == 1
    with session_scope() as db:
        dup = hygiene.find_issues(db)["duplicate_contacts"][0]
        hygiene.merge_contacts(db, dup["keep_id"], dup["merge_ids"][0])
    with session_scope() as db:
        assert hygiene.find_issues(db)["duplicate_contacts"] == []


def test_reply_webhook_starts_triage_and_honours_opt_out(seeded):
    with TestClient(app) as api:
        r = api.post("/api/webhooks/reply", json={"from_email": "marcus.demo@example.com", "text": "Please unsubscribe me"})
        assert r.json()["status"] == "disqualified" and "run_id" not in r.json()
        r = api.post("/api/webhooks/reply", json={"from_email": "elena.test@example.com", "text": "Sounds interesting"})
        assert "run_id" in r.json()
