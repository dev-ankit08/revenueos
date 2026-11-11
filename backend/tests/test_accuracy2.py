"""Accuracy batch 2 (plan Track A 3-6) and Track B 2-3 / Track C 2-3: real source checks, grading every draft,
better knowledge search, fresh research, My day, booking link, UTM tags, lifecycle stages and SLA."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from revenueos import seed
from revenueos.agents import base, researcher, shared_tools, writer
from revenueos.config import settings
from revenueos.db import session_scope
from revenueos.main import app
from revenueos.models import (Account, AccountBrief, Activity, AgentRun, Approval, Contact, Opportunity, PersonProfile,
                              Task)
from revenueos.services import approvals, guardrails, lifecycle, personalisation, sources, utm
from revenueos.tools import kb

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


def _contact(email="priya.sample@example.com") -> int:
    with session_scope() as db:
        return db.scalar(select(Contact.id).where(Contact.email == email))


# ------------------------------------------------------------------ sources really seen

def test_urls_are_collected_from_search_results_and_citations():
    result = SimpleNamespace(type="web_search_tool_result", content=[
        SimpleNamespace(type="web_search_result", url="https://news.example.com/a", title="A"),
        SimpleNamespace(type="web_search_result", url="https://news.example.com/b", title="B")])
    cited = SimpleNamespace(type="text", text="x", citations=[SimpleNamespace(url="https://site.example.com/c")])
    usage = base.Usage()
    usage.add(SimpleNamespace(content=[result, cited], usage=SimpleNamespace(
        input_tokens=1, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0)))
    assert usage.urls == {"https://news.example.com/a", "https://news.example.com/b", "https://site.example.com/c"}
    assert sources.normalise("https://WWW.News.example.com/a/?utm_source=x#top") == sources.normalise("http://news.example.com/a")


def test_claims_must_match_the_cited_document_and_a_page_actually_opened(seeded):
    with session_scope() as db:
        case = db.scalar(select(Contact).where(Contact.email == "priya.sample@example.com"))
        title = db.execute(select(kb.KBDocument.title).where(kb.KBDocument.category == "case_study")).first()[0]
        doc = db.scalar(select(kb.KBDocument).where(kb.KBDocument.title == title))
        real_figure = sorted(guardrails._numbers(doc.content) - {str(n) for n in range(11)})[0]
        grounding = guardrails.grounding_text(db, "Brief cites https://news.example.com/siemens-plan")
        flags = guardrails.check_claims(db, [
            {"text": f"we delivered {real_figure} for a client", "source": f"kb:{title}"},
            {"text": "we cut costs 93% for a bank", "source": f"kb:{title}"},
            {"text": "Siemens plans a new plant", "source": "https://news.example.com/siemens-plan"},
        ], "body", grounding)
        codes = {f["code"]: f["message"] for f in flags}
        assert "93%" in codes["claim_not_in_source"] and real_figure not in codes["claim_not_in_source"]
        assert "news.example.com/siemens-plan" in codes["source_not_opened"]
        sources.record(db, ["https://news.example.com/siemens-plan/"], agent="account_researcher")
        flags = guardrails.check_claims(db, [{"text": "Siemens plans a new plant",
                                              "source": "https://news.example.com/siemens-plan"}], "body", grounding)
        assert flags == []


def test_brief_sources_nobody_opened_are_removed(seeded):
    with session_scope() as db:
        acc = db.scalar(select(Account).where(Account.name == "Siemens"))
        run = AgentRun(agent="account_researcher", model="m", input="i",
                       started_at=datetime.now(UTC) - timedelta(minutes=5), source_urls=["https://real.example.com/news"])
        db.add(run)
        db.flush()
        sources.record(db, run.source_urls, run_id=run.id)
        db.add(AccountBrief(account_id=acc.id, summary="s", sources=["https://real.example.com/news", "https://made-up.example.com/x"],
                            recent_news=["2026-10 - Real news - https://real.example.com/news",
                                         "2026-09 - Invented news - https://made-up.example.com/y"]))
        db.flush()
        acc_id, run_id = acc.id, run.id
    note = researcher.verify_brief_sources(acc_id, run_id)
    assert "Removed 2" in note
    with session_scope() as db:
        b = db.scalar(select(AccountBrief).where(AccountBrief.account_id == acc_id).order_by(AccountBrief.id.desc()))
        assert b.sources == ["https://real.example.com/news"] and len(b.recent_news) == 1
        assert set(b.unverified_sources) == {"https://made-up.example.com/x", "https://made-up.example.com/y"}


# ------------------------------------------------------------------ grading every customer-facing draft

def _queued(kind="email", **kw) -> int:
    cid = _contact()
    with session_scope() as db:
        return approvals.queue(db, kind=kind, contact=db.get(Contact, cid) if kind != "content" else None,
                               subject="hi", body="Hi Priya, a generic note.", created_by_agent="call_analyst", **kw).id


@pytest.mark.anyio
async def test_grade_approval_adds_warnings_and_never_loses_the_draft(seeded, monkeypatch):
    aid = _queued()

    async def low(**kw):
        return personalisation.Grade(score=35, specific_facts_used=[], unsupported_claims=["They opened a plant in Ohio"],
                                     generic_phrases=["hope you're well"], feedback="Use the call notes.")
    monkeypatch.setattr(personalisation, "grade", low)
    assert await personalisation.grade_approval(aid, "evidence") == 35
    with session_scope() as db:
        a = db.get(Approval, aid)
        codes = {f["code"] for f in a.guardrail_flags}
        assert a.personalisation_score == 35 and {"low_personalisation", "grader_unsupported"} <= codes

    async def broken(**kw):
        raise RuntimeError("overloaded")
    monkeypatch.setattr(personalisation, "grade", broken)
    aid2 = _queued()
    assert await personalisation.grade_approval(aid2, "evidence") is None
    with session_scope() as db:
        a = db.get(Approval, aid2)
        assert a.status == "pending" and "not_graded" in {f["code"] for f in a.guardrail_flags}


@pytest.mark.anyio
async def test_marketing_content_uses_the_content_rubric(seeded, monkeypatch):
    seen = {}

    async def capture(**kw):
        seen.update(kw)
        return personalisation.Grade(score=82, specific_facts_used=["x"], unsupported_claims=[], generic_phrases=[],
                                     feedback="ok")
    monkeypatch.setattr(personalisation, "grade", capture)
    aid = _queued(kind="content", content_type="linkedin_post", title="Post")
    await personalisation.grade_approval(aid, "kb evidence")
    assert seen["mode"] == "content" and seen["recipient"]["audience"] == "linkedin_post"


# ------------------------------------------------------------------ knowledge search

def test_kb_search_matches_word_forms_and_drops_weak_hits(seeded):
    with session_scope() as db:
        kb.add_document(db, "Forecasting playbook", "offering", "We improve demand forecasts for retailers.")
        kb.add_document(db, "Unrelated", "other", "Retailers also like coffee.")
        hits = kb.search(db, "forecasting accuracy")
    assert hits and hits[0]["title"] in ("Forecasting playbook",) or "forecast" in hits[0]["text"].lower()
    assert all("chunk_id" in h for h in hits)
    long_para = " ".join(f"Sentence {i} about data platforms." for i in range(120))
    chunks = kb.chunk_text(long_para + "\n\nShort tail.")
    assert len(chunks) > 1 and max(len(c) for c in chunks) < 1500


@pytest.mark.anyio
async def test_rerank_orders_hits_and_falls_back(monkeypatch):
    hits = [{"doc_id": i, "chunk_id": i, "title": f"T{i}", "category": "case_study", "text": f"text {i}"} for i in range(8)]
    _set(kb_rerank=True)

    async def fake_run_agent(**kw):
        return SimpleNamespace(parsed=shared_tools._Ranking(best_first=[6, 2, 99, 2]))
    monkeypatch.setattr(base, "run_agent", fake_run_agent)
    ranked = await shared_tools.rerank("q", hits, k=5)
    assert [h["doc_id"] for h in ranked] == [6, 2]

    async def failing(**kw):
        raise RuntimeError("down")
    monkeypatch.setattr(base, "run_agent", failing)
    assert [h["doc_id"] for h in await shared_tools.rerank("q", hits, k=5)] == [0, 1, 2, 3, 4]


# ------------------------------------------------------------------ fresh research

@pytest.mark.anyio
async def test_stale_brief_and_dossier_are_refreshed_before_writing(seeded, monkeypatch):
    cid = _contact()
    with session_scope() as db:
        c = db.get(Contact, cid)
        db.add(AccountBrief(account_id=c.account_id, summary="old", created_at=datetime.now(UTC) - timedelta(days=45)))
        db.add(PersonProfile(contact_id=cid, summary="old", updated_at=datetime.now(UTC) - timedelta(days=10)))
    calls = []

    async def fake_research(account_id, focus=None, parent_run_id=None):
        calls.append(("brief", account_id))
        return "ok"

    async def fake_person(contact_id, parent_run_id=None):
        calls.append(("dossier", contact_id))
        return {}
    from revenueos.agents import person_researcher
    monkeypatch.setattr(researcher, "research_account", fake_research)
    monkeypatch.setattr(person_researcher, "research_person", fake_person)
    assert await writer.refresh_stale_research(cid) == ["account brief"]  # dossier is only 10 days old
    assert calls[0][0] == "brief"


# ------------------------------------------------------------------ UTM

def test_utm_tags_only_our_own_links():
    _set(utm_enabled=True, utm_domains="criticalriver.com", sender_email="sales@example.com")
    text = ("See https://www.criticalriver.com/case-study, our page https://example.com/a?x=1 and "
            "news https://news.other.com/z. Already tagged: https://criticalriver.com/p?utm_source=li")
    out = utm.tag_links(text, campaign="Q4 Manufacturing", medium="email", content="approval-7")
    assert "https://www.criticalriver.com/case-study?utm_source=revenueos&utm_medium=email&utm_campaign=q4-manufacturing&utm_content=approval-7," in out
    assert "https://example.com/a?x=1&utm_source=revenueos" in out
    assert "https://news.other.com/z." in out and "https://criticalriver.com/p?utm_source=li" in out
    _set(utm_enabled=False)
    assert utm.tag_links(text, campaign="c", medium="email") == text


# ------------------------------------------------------------------ lifecycle + SLA + My day

def test_lifecycle_moves_forward_and_tracks_the_sla(seeded):
    _set(mql_score=60, mql_sla_hours=24)
    pid, mid = _contact(), _contact("marcus.demo@example.com")
    with session_scope() as db:
        p = db.get(Contact, pid)
        p.score = 80
        db.add(Activity(contact_id=pid, type="email_click", detail="clicked"))
        db.get(Contact, mid).score = 90  # high score but never engaged: stays a lead
    with session_scope() as db:
        lifecycle.refresh_all(db)
        assert db.get(Contact, pid).lifecycle == "mql" and db.get(Contact, mid).lifecycle == "lead"
        db.get(Contact, pid).mql_at = datetime.now(UTC) - timedelta(hours=30)
    with session_scope() as db:
        assert [b["contact_id"] for b in lifecycle.sla_breaches(db)] == [pid]
        p = db.get(Contact, pid)
        db.add(Opportunity(account_id=p.account_id, name="Siemens pilot", stage="discovery"))
        db.add(Task(title="Call Priya", contact_id=pid, status="done", completed_at=datetime.now(UTC)))
    with session_scope() as db:
        lifecycle.refresh_all(db)
        p = db.get(Contact, pid)
        assert p.lifecycle == "opportunity" and p.first_touch_at is not None
        assert lifecycle.sla_breaches(db) == []
        f = lifecycle.funnel(db)
        assert f["stages"]["opportunity"] >= 1 and f["sla_met_pct"] == 0.0  # touched, but after 30h


def test_my_day_and_funnel_endpoints(seeded, api):
    cid = _contact()
    with session_scope() as db:
        approvals.queue(db, kind="email", contact=db.get(Contact, cid), subject="hello", body="Hi Priya.",
                        created_by_agent="outreach_writer")
        db.add(Task(title="Call Priya", type="call", contact_id=cid, due_at=datetime.now(UTC) - timedelta(hours=1)))
        db.add(Activity(contact_id=cid, type="reply_interested", detail="Yes, let's talk"))
    day = api.get("/api/my-day").json()
    assert day["summary"]["tasks_due"] == 1 and day["summary"]["approvals_waiting"] == 1
    assert day["hot_replies"][0]["contact_id"] == cid
    assert "stages" in api.get("/api/insights/funnel").json()


@pytest.mark.anyio
async def test_reply_writer_offers_the_booking_link(seeded, monkeypatch):
    from revenueos.agents import replies

    _set(booking_url="https://cal.example.com/priya-team")
    captured = {}

    async def fake_run_agent(**kw):
        captured.update(kw)
        return SimpleNamespace(text="drafted", run_id=kw.get("run_id"))
    monkeypatch.setattr(replies, "run_agent", fake_run_agent)
    t = replies.ReplyTriage(category="interested", sentiment="positive", summary="Wants to talk", urgency="high",
                            needs_response=True, suggested_next_step="Book a call")
    await replies.draft_reply(_contact(), "Sounds good, when?", "idea", t, None)
    assert "https://cal.example.com/priya-team" in captured["prompt"]
