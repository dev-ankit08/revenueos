"""Deliverability Guardian: sending pool, warm-up, gaps, auto-pause on complaints, DNS checks, Gmail sending,
inbox placement tests. Provider HTTP is mocked at integrations.http.request."""
import base64
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from revenueos import seed
from revenueos.config import settings
from revenueos.db import session_scope
from revenueos.integrations import http, oauth
from revenueos.main import app
from revenueos.models import Approval, Connection, Contact, SendingMailbox
from revenueos.services import approvals, deliverability, notify

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


def _contacts(db, n):
    """n contacts at different domains (so the per-company cap doesn't interfere)."""
    out = []
    base = db.scalar(select(Contact).where(Contact.email == "priya.sample@example.com"))
    for i in range(n):
        c = Contact(account_id=base.account_id, first_name=f"P{i}", last_name="X", email=f"p{i}@co{i}.example")
        db.add(c)
        out.append(c)
    db.flush()
    return out


def _draft(db, c):
    return approvals.queue(db, kind="email", contact=c, subject="hi", body=f"Hi {c.first_name}.", created_by_agent="t")


def test_warmup_ramps_from_five_to_the_cap():
    mb = SendingMailbox(address="a@out.example", daily_cap=40, warmup_days=20,
                        warmup_started_at=datetime(2026, 11, 1, tzinfo=UTC))
    assert deliverability.warmup_cap(mb, datetime(2026, 11, 1, 9, tzinfo=UTC)) == 5
    assert deliverability.warmup_cap(mb, datetime(2026, 11, 11, 9, tzinfo=UTC)) == 23  # halfway: 5 + 35 * 10/20 = 22.5
    assert deliverability.warmup_cap(mb, datetime(2026, 11, 25, tzinfo=UTC)) == 40
    assert deliverability.warmup_cap(SendingMailbox(address="b@x", daily_cap=30, warmup_days=0)) == 30


def test_sends_spread_across_the_pool_and_stop_at_capacity(seeded):
    _set(per_domain_daily_cap=50)
    with session_scope() as db:
        db.add_all([SendingMailbox(address="ankit@getcriticalriver.example", daily_cap=2, warmup_days=0, status="active"),
                    SendingMailbox(address="team@getcriticalriver.example", daily_cap=2, warmup_days=0, status="active")])
        ids = [_draft(db, c).id for c in _contacts(db, 5)]
    used = []
    with session_scope() as db:
        for aid in ids[:4]:
            a = approvals.approve_and_send(db, db.get(Approval, aid), "rep")
            used.append(a.mailbox_id)
        assert sorted(used) == [1, 1, 2, 2]
        with pytest.raises(approvals.ApprovalError, match="reached today's limit"):
            approvals.approve_and_send(db, db.get(Approval, ids[4]), "rep")


def test_automated_sends_keep_a_gap_but_people_are_not_delayed(seeded):
    _set(mailbox_min_gap_seconds=600, per_domain_daily_cap=50)
    with session_scope() as db:
        db.add(SendingMailbox(address="ankit@getcriticalriver.example", daily_cap=40, warmup_days=0, status="active"))
        c1, c2 = _contacts(db, 2)
        a1 = _draft(db, c1)
        approvals.approve_and_send(db, a1, "rep")
        with pytest.raises(deliverability.NoSendingCapacity):
            deliverability.pick_mailbox(db, automated=True)  # sent seconds ago
        assert deliverability.pick_mailbox(db, automated=False).address == "ankit@getcriticalriver.example"


def test_all_paused_blocks_sending(seeded):
    with session_scope() as db:
        db.add(SendingMailbox(address="x@getcriticalriver.example", status="paused"))
        (c,) = _contacts(db, 1)
        with pytest.raises(approvals.ApprovalError, match="paused"):
            approvals.approve_and_send(db, _draft(db, c), "rep")


def test_spam_complaints_pause_the_mailbox_and_alert(seeded, api, monkeypatch):
    _set(per_domain_daily_cap=50, webhook_secret=None)
    alerts = []
    monkeypatch.setattr(notify, "alert", lambda s, t: alerts.append(s))
    with session_scope() as db:
        db.add(SendingMailbox(address="ankit@getcriticalriver.example", daily_cap=40, warmup_days=0, status="active"))
        ids = []
        for c in _contacts(db, 2):
            a = _draft(db, c)
            approvals.approve_and_send(db, a, "rep")
            ids.append((a.id, c.email))
    events = [{"event": "spamreport", "email": e, "approval_id": str(aid), "sg_event_id": f"c{aid}"} for aid, e in ids]
    assert api.post("/api/webhooks/email/sendgrid", json=events).json()["processed"] == {"complaint": 2}
    with session_scope() as db:
        mb = db.scalar(select(SendingMailbox))
        assert mb.status == "paused" and "2 spam complaints" in mb.paused_reason
    assert alerts and "paused" in alerts[0]
    assert api.post("/api/deliverability/mailboxes/1/resume").json()["status"] == "active"


def _dns(answers: dict):
    def fake(method, url, params=None, **kw):
        key = (params["name"], params["type"])
        recs = answers.get(key, [])
        return {"Answer": [{"type": 16 if params["type"] == "TXT" else 15, "data": f'"{r}"'} for r in recs]}
    return fake


def test_dns_check_scores_and_explains(monkeypatch):
    _set(primary_domain="criticalriver.example")
    good = {("getcr.example", "TXT"): ["v=spf1 include:_spf.google.com ~all"],
            ("google._domainkey.getcr.example", "TXT"): ["v=DKIM1; k=rsa; p=MIIB"],
            ("_dmarc.getcr.example", "TXT"): ["v=DMARC1; p=none; rua=mailto:d@getcr.example"],
            ("getcr.example", "MX"): ["1 smtp.google.com."]}
    monkeypatch.setattr(http, "request", _dns(good))
    r = deliverability.dns_check("getcr.example")
    assert r["score"] == 95 and r["checks"]["dkim"]["selector"] == "google"
    assert any("monitor-only" in f for f in r["fixes"])

    monkeypatch.setattr(http, "request", _dns({}))
    r = deliverability.dns_check("criticalriver.example")
    assert r["score"] == 0 and r["verdict"] == "not ready"
    assert "main company domain" in r["fixes"][0]
    assert any("SPF" in f for f in r["fixes"]) and any("DMARC" in f for f in r["fixes"])


def test_sends_through_the_reps_own_gmail(seeded, monkeypatch):
    _set(per_domain_daily_cap=50)
    sent = []
    monkeypatch.setattr(oauth, "access_token", lambda db, conn: "gmail-token")
    monkeypatch.setattr(http, "request", lambda method, url, **kw: sent.append((url, kw)) or {"id": "gm-1"})
    with session_scope() as db:
        conn = Connection(provider="google", account_email="ankit@getcriticalriver.example", status="active")
        db.add(conn)
        db.flush()
        db.add(SendingMailbox(address="ankit@getcriticalriver.example", provider="google", connection_id=conn.id,
                              daily_cap=40, warmup_days=0, status="active"))
        (c,) = _contacts(db, 1)
        a = approvals.approve_and_send(db, _draft(db, c), "rep")
        assert a.status == "sent" and a.provider == "gmail" and a.provider_message_id == "gm-1"
    url, kw = sent[0]
    assert url.endswith("/messages/send") and kw["headers"]["Authorization"] == "Bearer gmail-token"
    raw = base64.urlsafe_b64decode(kw["json_body"]["raw"]).decode()
    assert "From: ankit@getcriticalriver.example" in raw and "List-Unsubscribe" in raw


def test_inbox_placement_test(seeded, api, monkeypatch):
    with session_scope() as db:
        db.add(Connection(provider="google", account_email="seed@team.example", status="active"))
        db.add(SendingMailbox(address="ankit@getcriticalriver.example", daily_cap=40, warmup_days=0, status="active"))
    assert "dry_run" in __import__("revenueos.tools.email", fromlist=["x"]).active_provider()
    t = api.post("/api/deliverability/placement-tests").json()
    assert [r["result"] for r in t["results"]] == ["pending"]

    monkeypatch.setattr(oauth, "access_token", lambda db, conn: "tok")

    def fake(method, url, params=None, **kw):
        if url.endswith("/messages"):
            assert t["token"] in params["q"]
            return {"messages": [{"id": "m1"}]}
        return {"labelIds": ["SPAM"]}
    monkeypatch.setattr(http, "request", fake)
    r = api.get(f"/api/deliverability/placement-tests/{t['id']}").json()
    assert r["results"][0]["result"] == "spam" and r["placement_pct"] == 0.0 and r["status"] == "checked"
    assert api.get("/api/deliverability").json()["latest_placement"]["placement_pct"] == 0.0
