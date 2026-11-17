"""Phase C: Buyer Room (plan, visits, buyer Q&A under the rules), buying-group coverage, ROI Statement,
knowledge base from the website, imports, first-hour onboarding."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from revenueos import seed
from revenueos.agents import base, kb_builder
from revenueos.config import settings
from revenueos.db import session_scope
from revenueos.main import app
from revenueos.models import (Activity, BuyerRoom, Contact, KBDocument, Opportunity, RoomQuestion, Task)
from revenueos.services import buyer_rooms, notify, roi_statement

UTC = None


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


def _deal() -> int:
    with session_scope() as db:
        p = db.scalar(select(Contact).where(Contact.email == "priya.sample@example.com"))
        p.owner = "anna@us.example"
        o = Opportunity(account_id=p.account_id, primary_contact_id=p.id, name="Siemens pilot", stage="proposal",
                        amount=90000, close_date="2026-12-31", owner="anna@us.example")
        db.add(o)
        db.flush()
        return o.id


async def _room(opp_id: int) -> str:
    rid = await buyer_rooms.create_room(opp_id, "anna", generate=False)
    with session_scope() as db:
        return db.get(BuyerRoom, rid).token


@pytest.mark.anyio
async def test_buyer_room_plan_visits_and_comments(seeded, api):
    oid = _deal()
    token = await _room(oid)
    assert await buyer_rooms.create_room(oid, "anna", generate=False)  # one active room per deal
    room = api.get(f"/api/public/rooms/{token}").json()
    assert len(room["steps"]) == 7 and room["steps"][-2]["title"] == "Contract signed"
    assert api.get("/api/public/rooms/not-a-real-token").status_code == 404

    api.post(f"/api/public/rooms/{token}/view", json={"visitor": "priya.sample@example.com", "section": "plan", "seconds": 95})
    api.post(f"/api/public/rooms/{token}/view", json={"visitor": "cfo@example.com", "section": "proposal", "seconds": 40})
    step = room["steps"][2]["id"]
    api.post(f"/api/public/rooms/{token}/steps/{step}",
             json={"visitor": "cfo@example.com", "done": True, "comment": "Security questionnaire attached next week."})
    with session_scope() as db:
        cfo = db.scalar(select(Contact).where(Contact.email == "cfo@example.com"))
        assert cfo and cfo.source == "buyer room" and cfo.owner == "anna@us.example"  # new buying-group member found
        assert db.scalar(select(Activity).where(Activity.type == "room_view", Activity.contact_id == cfo.id))
        assert db.scalar(select(Task).where(Task.created_by == "buyer_room")).owner == "anna@us.example"
    rep_view = api.get(f"/api/deals/{oid}/room").json()
    assert rep_view["plan_progress"] == {"done": 1, "total": 7}
    assert {v["visitor"] for v in rep_view["visitors"]} == {"priya.sample@example.com", "cfo@example.com"}


@pytest.mark.anyio
async def test_buyer_questions_are_answered_only_within_the_rules(seeded, api, monkeypatch):
    oid = _deal()
    token = await _room(oid)
    answers = iter([
        buyer_rooms._Answer(answer="The assessment takes four weeks and ends with a prioritised roadmap.",
                            confident=True, is_pricing_question=False, sources=["Data assessment"]),
        buyer_rooms._Answer(answer="It costs 25,000.", confident=True, is_pricing_question=True, sources=["Pricing"]),
        buyer_rooms._Answer(answer="We cut costs 93% for every client.", confident=True, is_pricing_question=False,
                            sources=["?"]),
    ])

    async def fake(**kw):
        return SimpleNamespace(parsed=next(answers), run_id=None)
    monkeypatch.setattr(base, "run_agent", fake)
    ok = api.post(f"/api/public/rooms/{token}/ask", json={"question": "How long is the assessment?", "visitor": "priya.sample@example.com"}).json()
    assert ok["pending"] is False and "four weeks" in ok["answer"]
    price = api.post(f"/api/public/rooms/{token}/ask", json={"question": "What does it cost?"}).json()
    assert price["pending"] is True and price["answer"] is None  # pricing goes to the rep unless allowed
    invented = api.post(f"/api/public/rooms/{token}/ask", json={"question": "What results do you get?"}).json()
    assert invented["pending"] is True  # an unverifiable figure is never shown to the buyer
    with session_scope() as db:
        tasks = db.scalars(select(Task).where(Task.created_by == "buyer_room")).all()
        assert len(tasks) == 2 and any("pricing" in t.notes for t in tasks)
        q = db.scalars(select(RoomQuestion)).all()
        assert [x.answer is None for x in q] == [False, True, True]


def test_buying_group_coverage(seeded, api):
    with session_scope() as db:
        p = db.scalar(select(Contact).where(Contact.email == "priya.sample@example.com"))
        acc = p.account_id
        cfo = Contact(account_id=acc, first_name="Carl", last_name="Finance", title="Chief Financial Officer", email="carl@example.com")
        cto = Contact(account_id=acc, first_name="Tina", last_name="Tech", title="CTO", email="tina@example.com")
        db.add_all([cfo, cto])
        db.flush()
        db.add(Activity(contact_id=p.id, type="reply_interested", detail="y"))
        db.add(Activity(contact_id=p.id, type="meeting", detail="m"))
    c = api.get(f"/api/accounts/{acc}/coverage").json()
    assert c["single_threaded"] is True and c["roles"]["champion"] == ["Priya Sample"]
    assert "finance" in c["missing_roles"] and "Carl Finance" in c["never_engaged"]
    with session_scope() as db:
        db.add(Activity(contact_id=db.scalar(select(Contact.id).where(Contact.email == "carl@example.com")),
                        type="room_view", detail="v"))
    c = api.get(f"/api/accounts/{acc}/coverage").json()
    assert c["single_threaded"] is False and c["roles"]["finance"] == ["Carl Finance"] and c["score"] >= 50


# ------------------------------------------------------------------ ROI statement

def test_roi_statement_json_markdown_docx_and_monthly_email(seeded, api, monkeypatch):
    from revenueos.models import AgentRun

    with session_scope() as db:
        for _ in range(4):
            db.add(AgentRun(agent="account_researcher", model="m", input="i", status="succeeded", cost_usd=0.5))
        db.add(AgentRun(agent="proposal_builder", model="m", input="i", status="succeeded", cost_usd=1.0))
    r = api.get("/api/insights/roi-statement").json()
    assert r["efficiency"]["hours_saved"] == 4.0  # 4 x 30 min + 1 x 120 min
    assert r["efficiency"]["ai_spend_usd"] == 3.0 and "ROI statement" in r["markdown"]
    doc = api.get("/api/insights/roi-statement.docx")
    assert doc.status_code == 200 and doc.content[:2] == b"PK"  # a real .docx (zip)
    _set(roi_minutes='{"account_researcher": 60}')
    assert api.get("/api/insights/roi-statement").json()["efficiency"]["hours_saved"] == 6.0
    sent = []
    monkeypatch.setattr("revenueos.tools.email.send_internal", lambda to, s, b: sent.append(to) or "smtp")
    assert api.post("/api/insights/roi-statement/send").status_code == 409
    _set(roi_recipients="cro@us.example, cfo@us.example")
    assert api.post("/api/insights/roi-statement/send").json() == {"sent_to": 2} and sent == ["cro@us.example", "cfo@us.example"]


# ------------------------------------------------------------------ onboarding

@pytest.mark.anyio
async def test_kb_from_website_creates_drafts_for_approval(seeded, api, monkeypatch):
    drafts = kb_builder.Drafts(documents=[
        kb_builder.Draft(category="offering", title="Salesforce implementation", content="We implement Sales Cloud. " * 10,
                         source_urls=["https://criticalriver.example/salesforce"]),
        kb_builder.Draft(category="pricing", title="Not allowed", content="x", source_urls=[])])
    monkeypatch.setattr(kb_builder, "submit_tool", lambda *a, **k: (None, lambda: drafts))

    async def fake_run(**kw):
        return SimpleNamespace(text="", run_id=None)
    monkeypatch.setattr(kb_builder, "run_agent", fake_run)
    assert await kb_builder.build_from_website("https://criticalriver.example") == {"drafts": 1}
    d = api.get("/api/kb/drafts").json()
    assert len(d) == 1 and d[0]["source_urls"] == ["https://criticalriver.example/salesforce"]
    with session_scope() as db:
        assert db.scalar(select(KBDocument).where(KBDocument.title == "Salesforce implementation")) is None  # not live yet
    api.post(f"/api/kb/drafts/{d[0]['id']}/approve", json={})
    with session_scope() as db:
        doc = db.scalar(select(KBDocument).where(KBDocument.title == "Salesforce implementation"))
        assert doc.category == "offering" and "Sources: https://criticalriver.example/salesforce" in doc.content


def test_imports_from_other_tools_and_first_hour_checklist(seeded, api):
    seq = "Sequence Name,Step,Day,Subject,Body\nOutbound Q4,1,0,quick idea,Hi {first}, saw your news.\nOutbound Q4,2,3,one more,Adding a case study.\n"
    r = api.post("/api/onboarding/import-sequences", files={"file": ("seq.csv", seq, "text/csv")}).json()
    assert r["steps_imported"] == 2
    outreach = "First,Last,Emails,Account,Title\nOla,Nordmann,ola@fjord.example,Fjord AS,Head of Data\n"
    r = api.post("/api/leads/import-csv", files={"file": ("o.csv", outreach, "text/csv")}).json()
    assert r["imported"] == 1
    status = api.get("/api/onboarding/status").json()
    assert [s["key"] for s in status["first_hour"]] == ["mailbox", "sending", "kb_site", "ten_leads", "first_drafts"]
