"""Phase 3 batch 1: two-factor sign-in, lockout, SSO token validation, key rotation, bulk approvals,
notifications and alerts, lawful basis, one-click unsubscribe, export/erase, retention."""
import base64
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes
from fastapi.testclient import TestClient
from sqlalchemy import select

from revenueos import seed
from revenueos.config import settings
from revenueos.db import session_scope
from revenueos.main import app
from revenueos.models import Activity, AgentRun, Approval, Contact, Suppression
from revenueos.services import approvals, notify, privacy, secrets, security

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


def _cid(email="priya.sample@example.com") -> int:
    with session_scope() as db:
        return db.scalar(select(Contact.id).where(Contact.email == email))


# ------------------------------------------------------------------ sign-in security

def test_totp_matches_rfc_6238_vector():
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert security.totp_now(secret, at=59) == "287082"  # RFC 6238 SHA-1 vector, truncated to 6 digits
    assert security.verify_totp(secret, "287082", at=59) and not security.verify_totp(secret, "000000", at=59)


def test_two_factor_sign_in_and_lockout(seeded, api):
    _set(secret_key="test-secret-key-for-encryption")
    owner = api.post("/api/auth/bootstrap", json={"email": "o@us.example", "password": "a-long-password"}).json()
    H = {"Authorization": f"Bearer {owner['token']}"}
    setup = api.post("/api/auth/mfa/setup", headers=H).json()
    assert setup["otpauth_uri"].startswith("otpauth://totp/")
    assert api.post("/api/auth/mfa/enable", json={"code": "000000"}, headers=H).status_code == 422
    assert api.post("/api/auth/mfa/enable", json={"code": security.totp_now(setup["secret"])}, headers=H).json()["mfa_enabled"]

    r = api.post("/api/auth/login", json={"email": "o@us.example", "password": "a-long-password"})
    assert r.status_code == 401 and r.json()["detail"]["mfa_required"]
    r = api.post("/api/auth/login", json={"email": "o@us.example", "password": "a-long-password",
                                          "code": security.totp_now(setup["secret"])})
    assert r.status_code == 200 and r.json()["token"]

    for _ in range(5):
        api.post("/api/auth/login", json={"email": "o@us.example", "password": "wrong-password"})
    r = api.post("/api/auth/login", json={"email": "o@us.example", "password": "a-long-password",
                                          "code": security.totp_now(setup["secret"])})
    assert r.status_code == 423  # locked for 15 minutes


def _jwt(key, kid, claims) -> str:
    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode().rstrip("=")
    head = b64(json.dumps({"alg": "RS256", "kid": kid}).encode())
    body = b64(json.dumps(claims).encode())
    sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{b64(sig)}"


def test_oidc_id_token_validation():
    _set(oidc_issuer="https://login.example.com", oidc_client_id="revenueos")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nums = key.public_key().public_numbers()

    def b64int(n: int) -> str:
        return base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).decode().rstrip("=")
    jwks = {"keys": [{"kty": "RSA", "kid": "k1", "n": b64int(nums.n), "e": b64int(nums.e)}]}
    good = {"iss": "https://login.example.com", "aud": "revenueos", "exp": time.time() + 300, "nonce": "n1",
            "email": "rep@us.example"}
    assert security.verify_id_token(_jwt(key, "k1", good), jwks, "n1")["email"] == "rep@us.example"
    for bad, msg in (({**good, "aud": "someone-else"}, "audience"), ({**good, "nonce": "x"}, "nonce"),
                     ({**good, "exp": time.time() - 3600}, "expired")):
        with pytest.raises(ValueError, match=msg):
            security.verify_id_token(_jwt(key, "k1", bad), jwks, "n1")
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(ValueError, match="signature"):
        security.verify_id_token(_jwt(other, "k1", good), jwks, "n1")


def test_key_rotation_decrypts_old_values():
    _set(secret_key="old-key-0123456789", secret_key_old=None)
    stored = secrets.encrypt("refresh-token-value")
    _set(secret_key="new-key-9876543210", secret_key_old="old-key-0123456789")
    assert secrets.decrypt(stored) == "refresh-token-value"
    rotated = secrets.rotate(stored)
    _set(secret_key_old=None)
    assert secrets.decrypt(rotated) == "refresh-token-value"


# ------------------------------------------------------------------ approvals at scale

def test_bulk_approve_skips_blocked_and_lists_oldest_first(seeded, api):
    pid, mid = _cid(), _cid("marcus.demo@example.com")
    with session_scope() as db:
        good = approvals.queue(db, kind="email", contact=db.get(Contact, pid), subject="hi", body="Hi Priya.",
                               created_by_agent="t").id
        bad = approvals.queue(db, kind="email", contact=db.get(Contact, mid), subject="hi", body="Hi [First Name].",
                              created_by_agent="t").id
        db.get(Approval, good).created_at = datetime.now(UTC) - timedelta(hours=5)
    oldest = api.get("/api/approvals", params={"order": "oldest"}).json()
    assert oldest[0]["id"] == good and oldest[0]["waiting_hours"] >= 5
    r = api.post("/api/approvals/bulk", json={"ids": [good, bad, 999], "action": "approve"}).json()
    assert [d["id"] for d in r["done"]] == [good]
    assert {s["id"] for s in r["skipped"]} == {bad, 999}


# ------------------------------------------------------------------ notifications

def test_hot_reply_notifies_owner_and_failures_alert(seeded, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "tell", lambda person, subject, text: sent.append((person, subject)) or "email")
    with session_scope() as db:
        c = db.get(Contact, _cid())
        c.owner = "anna@us.example"
        notify.hot_reply(c, "interested", "Wants a call")
    assert sent == [("anna@us.example", "Priya Sample replied: interested")]

    alerts = []
    monkeypatch.setattr(notify, "alert", lambda subject, text: alerts.append(subject))
    with session_scope() as db:
        db.add(AgentRun(agent="outreach_writer", model="m", input="i", status="failed", error="boom",
                        finished_at=datetime.now(UTC)))
    with session_scope() as db:
        assert notify.alert_failures(db, datetime.now(UTC) - timedelta(minutes=5)) == 1
    assert alerts and "1 failed jobs" in alerts[0]


def test_daily_digest_only_for_reps_with_work(seeded, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "tell", lambda person, subject, text: sent.append((person, text)) or "email")
    with session_scope() as db:
        c = db.get(Contact, _cid())
        c.owner = "anna@us.example"
        db.get(Contact, _cid("marcus.demo@example.com")).owner = "ben@us.example"
        approvals.queue(db, kind="email", contact=c, subject="hi", body="Hi Priya.", created_by_agent="t")
    with session_scope() as db:
        assert notify.send_digests(db) == 1
    assert sent[0][0] == "anna@us.example" and "1 draft(s) to review" in sent[0][1]
    _set(digest_hour=8, default_timezone="UTC")
    morning = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)
    assert notify.due_for_digest(None, morning) and not notify.due_for_digest("2026-11-09", morning)
    assert not notify.due_for_digest(None, datetime(2026, 11, 9, 6, 0, tzinfo=UTC))  # before 08:00
    _set(digest_hour=-1)
    assert not notify.due_for_digest(None, morning)  # switched off


# ------------------------------------------------------------------ privacy

def test_lawful_basis_required_in_listed_countries(seeded, api):
    _set(consent_required_countries="Germany")
    pid = _cid()
    with session_scope() as db:
        db.get(Contact, pid).account.country = "Germany"
        a = approvals.queue(db, kind="email", contact=db.get(Contact, pid), subject="hi", body="Hi Priya.",
                            created_by_agent="t")
        assert "no_lawful_basis" in {f["code"] for f in a.guardrail_flags}
    assert api.put(f"/api/leads/{pid}/lawful-basis", json={"lawful_basis": "legitimate_interest",
                                                            "source": "LIA 2026-11"}).status_code == 200
    with session_scope() as db:
        a = approvals.queue(db, kind="email", contact=db.get(Contact, pid), subject="hi", body="Hi Priya.",
                            created_by_agent="t")
        assert "no_lawful_basis" not in {f["code"] for f in a.guardrail_flags}


def test_one_click_unsubscribe_link(seeded, api):
    _set(public_base_url="https://revenueos.example.com", secret_key="k-0123456789")
    footer = __import__("revenueos.services.guardrails", fromlist=["x"]).compliance_footer("priya.sample@example.com")
    assert "https://revenueos.example.com/api/public/unsubscribe?t=" in footer
    token = privacy.unsubscribe_token("priya.sample@example.com")
    assert privacy.read_unsubscribe_token(token + "x") is None
    assert "Stop all emails" in api.get("/api/public/unsubscribe", params={"t": token}).text
    assert "unsubscribed" in api.post("/api/public/unsubscribe", data={"t": token}).text
    with session_scope() as db:
        assert db.scalar(select(Suppression).where(Suppression.email_or_domain == "priya.sample@example.com"))
        assert db.get(Contact, _cid()).status == "disqualified"


def test_export_and_erase(seeded, api):
    pid = _cid()
    with session_scope() as db:
        approvals.queue(db, kind="email", contact=db.get(Contact, pid), subject="hi", body="Hi Priya.", created_by_agent="t")
        db.add(Activity(contact_id=pid, type="note", detail="likes short emails"))
    out = api.post("/api/privacy/export", json={"email": "Priya.Sample@example.com"}).json()
    assert out["found"] and out["contacts"][0]["messages"][0]["subject"] == "hi"
    assert api.post("/api/privacy/erase", json={"email": "priya.sample@example.com"}).json()["erased_contacts"] == 1
    with session_scope() as db:
        assert db.get(Contact, pid) is None
        assert db.scalar(select(Suppression).where(Suppression.email_or_domain == "priya.sample@example.com"))


def test_retention_purges_old_run_logs_and_rejected_drafts(seeded):
    old = datetime.now(UTC) - timedelta(days=400)
    with session_scope() as db:
        db.add(AgentRun(agent="x", model="m", input="secret prompt", output="o", started_at=old))
        db.add(Approval(kind="email", contact_id=_cid(), subject="s", body="old draft", status="rejected",
                        created_by_agent="t", created_at=old))
    with session_scope() as db:
        r = privacy.purge_old(db)
    assert r["runs_purged"] >= 1 and r["drafts_purged"] == 1
    with session_scope() as db:
        assert db.scalar(select(AgentRun).where(AgentRun.agent == "x")).input == "[purged]"
