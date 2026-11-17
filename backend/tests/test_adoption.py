"""Phase A features 2-4: approve from email (signed links), Rep Voice, follow-up value check."""
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from revenueos import seed
from revenueos.agents import base, writer
from revenueos.config import settings
from revenueos.db import session_scope
from revenueos.main import app
from revenueos.models import Approval, Contact, ContextEvent, RepVoice
from revenueos.services import approvals, emailapprove, guardrails, insights, notify, voice

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


def _priya(db) -> Contact:
    return db.scalar(select(Contact).where(Contact.email == "priya.sample@example.com"))


# ------------------------------------------------------------------ approve from email

def _link_parts(url: str):
    u = urlsplit(url)
    return u.path, {k: v[0] for k, v in parse_qs(u.query).items()}


def test_email_link_needs_a_click_then_approves(seeded, api):
    _set(secret_key="k-0123456789abcdef", public_base_url="https://app.example.com")
    with session_scope() as db:
        p = _priya(db)
        p.owner = "anna@us.example"
        aid = approvals.queue(db, kind="email", contact=p, subject="hello", body="Hi Priya, a short note.",
                              created_by_agent="t").id
    url = emailapprove.link(aid, "approve", "anna@us.example")
    path, q = _link_parts(url)
    page = api.get(path, params=q)
    assert "Approve and send?" in page.text and "Hi Priya, a short note." in page.text
    with session_scope() as db:
        assert db.get(Approval, aid).status == "pending"  # opening the link (or a link scanner) changes nothing
    done = api.post(path, params=q)
    assert "Sent." in done.text
    with session_scope() as db:
        a = db.get(Approval, aid)
        assert a.status == "sent" and a.reviewed_by == "anna@us.example (email link)"
    assert "already" in api.get(path, params=q).text


def test_email_links_reject_tampering_and_expiry(seeded, api):
    _set(secret_key="k-0123456789abcdef", public_base_url="https://app.example.com")
    with session_scope() as db:
        aid = approvals.queue(db, kind="email", contact=_priya(db), subject="s", body="Hi Priya.", created_by_agent="t").id
    path, q = _link_parts(emailapprove.link(aid, "approve", "anna@us.example"))
    assert "Link not valid" in api.post(path, params={**q, "r": "mallory@evil.example"}).text
    assert "Link not valid" in api.post(path.replace("/approve", "/reject"), params=q).text  # signed for approve only
    old = emailapprove.link(aid, "reject", "anna@us.example", now=time.time() - 100 * 3600)
    path2, q2 = _link_parts(old)
    assert "expired" in api.get(path2, params=q2).text
    path3, q3 = _link_parts(emailapprove.link(aid, "reject", "anna@us.example"))
    api.post(path3, params=q3, data={"reason": "too_generic"})
    with session_scope() as db:
        a = db.get(Approval, aid)
        assert a.status == "rejected" and a.reject_reason == "too_generic"


def test_morning_digest_carries_approve_links(seeded, monkeypatch):
    _set(secret_key="k-0123456789abcdef", public_base_url="https://app.example.com")
    with session_scope() as db:
        p = _priya(db)
        p.owner = "anna@us.example"
        approvals.queue(db, kind="email", contact=p, subject="forecast idea", body="Hi Priya, " + "word " * 40,
                        created_by_agent="t")
        text = notify.digest_for(db, "anna@us.example")
    assert "forecast idea" in text and "Approve as written: https://app.example.com/api/public/approvals/" in text


# ------------------------------------------------------------------ Rep Voice

def _rep_emails(db, owner, n):
    p = _priya(db)
    for i in range(n):
        db.add(ContextEvent(source="email", source_ref=f"rep-{i}", contact_id=p.id, account_id=p.account_id,
                            author=owner, title=f"t{i}", processed=True,
                            raw_text=f"Hey Priya, quick one on item {i}. Short and to the point as usual. "
                                     "Worth a chat this week? Cheers, Ankit"))


@pytest.mark.anyio
async def test_rep_voice_is_built_from_the_reps_own_emails_and_used_by_the_writer(seeded, monkeypatch):
    with session_scope() as db:
        _rep_emails(db, "ankit@us.example", 4)
    with pytest.raises(ValueError, match="at least 10"):
        await voice.build("ankit@us.example", "ankit@us.example")
    with session_scope() as db:
        _rep_emails(db, "ankit@us.example", 0)
        for i in range(4, 12):
            db.add(ContextEvent(source="email", source_ref=f"rep-{i}", author="ankit@us.example", processed=True,
                                raw_text="Hey there, a short note as usual from me, would it be worth a quick chat this week? Cheers, Ankit"))
    seen = {}

    async def fake_profile(**kw):
        seen["prompt"] = kw["prompt"]
        return SimpleNamespace(parsed=voice.VoiceProfile(
            summary="Short, friendly, direct.", typical_length_words=45, greeting="Hey {first name},",
            sign_off="Cheers, Ankit", formality="casual", sentence_style="short sentences, ends with a question",
            phrases_they_use=["quick one"], phrases_they_avoid=["exclamation marks"]))
    monkeypatch.setattr(base, "run_agent", fake_profile)
    r = await voice.build("Ankit@us.example", "ankit@us.example")
    assert r["samples"] == 12 and "Cheers, Ankit" in seen["prompt"]

    with session_scope() as db:
        p = _priya(db)
        p.owner = "ankit@us.example"
        cid = p.id
    captured = {}

    async def fake_writer(**kw):
        captured.update(kw)
        return SimpleNamespace(text="done", run_id=kw.get("run_id"))
    monkeypatch.setattr(writer, "run_agent", fake_writer)
    await writer.write_sequence(cid)
    assert "WRITE IN ANKIT@US.EXAMPLE'S OWN VOICE" in captured["prompt"] and "Cheers, Ankit" in captured["prompt"]

    with session_scope() as db:
        db.scalar(select(RepVoice)).enabled = False
    captured.clear()
    await writer.write_sequence(cid)
    assert "OWN VOICE" not in captured["prompt"]


def test_rep_voice_permissions(seeded, api):
    api.post("/api/auth/bootstrap", json={"email": "o@us.example", "password": "a-long-password"})
    from revenueos.services import auth, workspaces

    auth.create_user("rep@us.example", "rep-password-1", workspaces.ensure_default().id, role="member")
    rep = api.post("/api/auth/login", json={"email": "rep@us.example", "password": "rep-password-1"}).json()
    H = {"Authorization": f"Bearer {rep['token']}"}
    assert api.post("/api/voice/someone.else@us.example/build", headers=H).status_code == 403
    assert api.post("/api/voice/rep@us.example/build", headers=H).status_code == 200  # own voice: allowed


# ------------------------------------------------------------------ follow-up value check

def test_follow_ups_must_add_something_new(seeded):
    step1 = ("Hi Priya, your point about forecast accuracy as a board metric stuck with me, especially with the plant "
             "data programme. A manufacturer we worked with improved accuracy once plant data was unified.")
    with session_scope() as db:
        p = _priya(db)
        approvals.queue(db, kind="email", contact=p, subject="s1", body=step1, sequence_step=1, created_by_agent="t")
        assert guardrails.followup_problems(db, p, 1, step1) == []
        repeat = "Hi again Priya. " + step1
        assert "repeats" in guardrails.followup_problems(db, p, 2, repeat)[0]
        filler = guardrails.followup_problems(db, p, 2, "Hi Priya, just checking in on my note below. Any thoughts?")
        assert "filler" in filler[0]
        fresh = ("Hi Priya, one more angle: teams that start with two plants on one data model see planners trust the "
                 "forecast sooner. Would a 20-minute walkthrough of that sequencing help?")
        assert guardrails.followup_problems(db, p, 2, fresh) == []


def test_quality_report_splits_by_rep_voice(seeded):
    with session_scope() as db:
        p = _priya(db)
        now = datetime.now(UTC)
        db.add(Approval(kind="email", contact_id=p.id, subject="s", body="b", status="sent", decided_at=now,
                        edit_pct=0, voice_applied=True, created_by_agent="outreach_writer"))
        db.add(Approval(kind="email", contact_id=p.id, subject="s", body="b", status="sent", decided_at=now,
                        edit_pct=40, voice_applied=False, created_by_agent="outreach_writer"))
    with session_scope() as db:
        q = insights.draft_quality(db)["rep_voice"]
    assert q["with_voice"]["approved_unedited_pct"] == 100.0 and q["without_voice"]["approved_unedited_pct"] == 0.0
