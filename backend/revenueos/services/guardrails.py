"""Deterministic checks on every draft before a human sees it, and again before sending.

Flags have a severity: "block" stops the send outright, "warn" is shown to the
reviewer in the approval inbox.
"""
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Approval, Contact, ContextEvent, KBDocument, Suppression

SPAM_PHRASES = (
    "act now", "100% free", "guaranteed", "risk-free", "no obligation", "click here",
    "limited time", "once in a lifetime", "winner", "urgent",
)
_NUMBER = re.compile(r"(?<![\w.])\$?\d[\d,]*(?:\.\d+)?\s?(?:%|x|k|m|bn|million|billion)?", re.I)
_PLACEHOLDER = re.compile(r"\{\{.*?\}\}|\[(?:first ?name|company|name|insert[^\]]*)\]", re.I)


def is_suppressed(db: Session, email: str | None) -> bool:
    if not email:
        return False
    email = email.lower()
    domain = email.split("@")[-1]
    return db.scalar(
        select(func.count()).select_from(Suppression).where(
            Suppression.email_or_domain.in_([email, domain])
        )
    ) > 0


def _numbers(text: str) -> set[str]:
    return {re.sub(r"[\s,]", "", m.group(0)).lower() for m in _NUMBER.finditer(text)}


def unverified_claims(body: str, grounding: str) -> list[str]:
    """Numbers/percentages in the draft that appear nowhere in the KB or the account brief."""
    known = _numbers(grounding)
    ignore = {str(y) for y in range(2000, 2100)} | {str(n) for n in range(0, 11)}
    return sorted(n for n in _numbers(body) if n not in known and n not in ignore)


SHINGLE = 7  # words: long enough that a shared phrase is a quote, not a coincidence


def _shingles(text: str) -> set[tuple[str, ...]]:
    words = re.findall(r"[a-z0-9']+", text.lower())
    return {tuple(words[i:i + SHINGLE]) for i in range(len(words) - SHINGLE + 1)}


def internal_leak(db: Session, account_id: int, draft: str) -> str | None:
    """A 7-word run shared with an internal-only context event for this account, if any."""
    draft_sh = _shingles(draft)
    if not draft_sh:
        return None
    for raw, summary in db.execute(select(ContextEvent.raw_text, ContextEvent.summary).where(
            ContextEvent.account_id == account_id, ContextEvent.internal.is_(True))):
        hit = draft_sh & _shingles(f"{raw or ''} {summary or ''}")
        if hit:
            return " ".join(sorted(hit)[0])
    return None


# Personal topics outreach must never touch, even if public. Phrased about the RECIPIENT ("your ...")
# so business content (a hospital network, a family-owned company) isn't caught.
SENSITIVE = {
    "family": re.compile(r"\byour (wife|husband|partner|kids|children|son|daughter|family|baby|parents)\b"
                         r"|\b(maternity|paternity) leave\b", re.I),
    "health": re.compile(r"\byour (recent )?(illness|diagnosis|surgery|pregnancy|treatment|recovery|health)\b"
                         r"|\b(you were|you are|you're) (ill|sick|pregnant|in hospital)\b", re.I),
    "home or location": re.compile(r"\byour (home|house|home address|neighbou?rhood)\b|\bwhere you live\b", re.I),
    "beliefs or politics": re.compile(r"\byour (religion|faith|church|mosque|temple|political views|politics|vote)\b", re.I),
    "age": re.compile(r"\byour (age|birthday)\b|\b\d{2} years old\b", re.I),
}


def sensitive_personal(text: str) -> str | None:
    for topic, pattern in SENSITIVE.items():
        if pattern.search(text):
            return topic
    return None


def rules(db: Session) -> dict[str, list[str]]:
    """Rule packs: knowledge-base documents in category "rules", one rule per line:
    BANNED: <phrase>   REQUIRED: <phrase>   TONE: <instruction for the writers>"""
    out: dict[str, list[str]] = {"BANNED": [], "REQUIRED": [], "TONE": []}
    for doc in db.scalars(select(KBDocument).where(KBDocument.category == "rules")):
        for line in doc.content.splitlines():
            key, _, value = line.partition(":")
            if key.strip().upper() in out and value.strip():
                out[key.strip().upper()].append(value.strip())
    return out


def rule_pack_flags(db: Session, kind: str, text: str) -> list[dict]:
    r, lowered, flags = rules(db), text.lower(), []
    banned = [p for p in r["BANNED"] if p.lower() in lowered]
    if banned:
        flags.append({"severity": "block", "code": "banned_phrase",
                      "message": f"Uses phrases your rules ban: {', '.join(banned)}."})
    if kind in ("email", "content"):
        missing = [p for p in r["REQUIRED"] if p.lower() not in lowered]
        if missing:
            flags.append({"severity": "warn", "code": "required_missing",
                          "message": f"Missing required wording: {'; '.join(missing)}."})
    return flags


def check_claims(db: Session, claims: list[dict] | None, body: str, grounding: str) -> list[dict]:
    """Each factual claim should name its source: kb:<document title>, a URL from the brief or dossier,
    or call / reply / history for what the customer told us."""
    if not claims:
        if _numbers(body) - {str(n) for n in range(0, 11)}:
            return [{"severity": "warn", "code": "no_sources", "message": "Draft states figures but lists no sources."}]
        return []
    titles = {t.lower() for t in db.scalars(select(KBDocument.title))}
    unsourced = []
    for c in claims:
        src = (c.get("source") or "").strip()
        ok = (src.lower().startswith("kb:") and src[3:].strip().lower() in titles) \
            or (src.startswith("http") and src in grounding) \
            or src.lower() in ("call", "reply", "history", "brief", "dossier", "meeting")
        if not ok:
            unsourced.append(c.get("text", "")[:80])
    if unsourced:
        return [{"severity": "warn", "code": "unsourced_claim",
                 "message": f"Sources couldn't be verified for: {'; '.join(unsourced)}"}]
    return []


def grounding_text(db: Session, extra: str = "") -> str:
    kb = "\n".join(d.content for d in db.scalars(select(KBDocument)))
    return f"{kb}\n{extra}"


def check_draft(
    db: Session, contact: Contact | None, kind: str, subject: str | None, body: str, grounding: str
) -> list[dict]:
    flags: list[dict] = []
    if kind == "email":
        if not contact or not contact.email:
            flags.append({"severity": "block", "code": "no_email", "message": "Contact has no email address."})
        elif is_suppressed(db, contact.email):
            flags.append({"severity": "block", "code": "suppressed", "message": "Recipient is on the suppression list."})
        if not subject:
            flags.append({"severity": "warn", "code": "no_subject", "message": "Email has no subject line."})
        elif len(subject) > 70:
            flags.append({"severity": "warn", "code": "long_subject", "message": "Subject is over 70 characters."})

    words = len(body.split())
    if kind in ("email", "linkedin_message") and words > 200:
        flags.append({"severity": "warn", "code": "too_long", "message": f"{words} words; cold outreach performs best under 150."})
    if _PLACEHOLDER.search(body) or (subject and _PLACEHOLDER.search(subject)):
        flags.append({"severity": "block", "code": "placeholder", "message": "Unfilled placeholder in draft."})
    if contact and contact.account_id and (leak := internal_leak(db, contact.account_id, f"{subject or ''} {body}")):
        flags.append({"severity": "block", "code": "internal_leak",
                      "message": f"Draft repeats internal team discussion (\"{leak}\"). Rephrase before sending."})
    if kind in ("email", "linkedin_message") and (topic := sensitive_personal(f"{subject or ''} {body}")):
        flags.append({"severity": "block", "code": "sensitive_personal",
                      "message": f"Mentions the person's {topic}. Keep outreach to professional, public topics."})
    flags += rule_pack_flags(db, kind, f"{subject or ''} {body}")
    lowered = f"{subject or ''} {body}".lower()
    spam = [p for p in SPAM_PHRASES if p in lowered]
    if spam:
        flags.append({"severity": "warn", "code": "spam_words", "message": f"Spam-trigger phrases: {', '.join(spam)}"})
    claims = unverified_claims(body, grounding)
    if claims:
        flags.append({
            "severity": "warn", "code": "unverified_claims",
            "message": f"Figures not found in the knowledge base or account brief: {', '.join(claims)}. Verify before sending.",
        })
    return flags


def send_capacity_error(db: Session, email: str) -> str | None:
    """Daily volume caps protect sender reputation. Returns an error message or None."""
    since = datetime.now(timezone.utc) - timedelta(days=1)
    sent_today = db.scalar(
        select(func.count()).select_from(Approval).where(Approval.status == "sent", Approval.sent_at >= since)
    )
    if sent_today >= settings.daily_send_cap:
        return f"Daily send cap of {settings.daily_send_cap} reached."
    domain = email.split("@")[-1].lower()
    same_domain = db.scalar(
        select(func.count()).select_from(Approval).join(Contact, Approval.contact_id == Contact.id).where(
            Approval.status == "sent", Approval.sent_at >= since, Contact.email.ilike(f"%@{domain}")
        )
    )
    if same_domain >= settings.per_domain_daily_cap:
        return f"Already sent {same_domain} emails to {domain} today (cap {settings.per_domain_daily_cap})."
    return None


def compliance_footer() -> str:
    return (
        f"\n\n--\n{settings.company_name} · {settings.physical_address}\n"
        "If you'd rather not hear from me, just reply \"unsubscribe\"."
    )
