"""Market intelligence agents (Sonnet + web): Competitor Intel, Signal Detector, Lead Sourcer."""
import json
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from ..config import settings
from ..db import session_scope
from ..models import Account, Activity, Contact, KBDocument, SalesDocument, Signal
from ..tools import kb
from .base import WEB_TOOLS, run_agent, submit_tool
from .shared_tools import kb_search

# --------------------------------------------------------------------------- Competitor Intel

INTEL_ROLE = """You are Competitor Intel. Research one competitor on the web and produce a battlecard reps can
use in a live deal. Use primary sources (their site, pricing pages, press releases) and reputable press from
the last 12 months. Use kb_search for OUR positioning and existing competitive notes.

Be fair and factual: reps lose credibility with exaggerated claims. Every "recent move" needs a source URL.
Landmines are questions our reps can ask that expose a real weakness, not cheap shots."""


class Battlecard(BaseModel):
    competitor: str
    overview: str = Field(description="Who they are and how they position themselves, 2-3 sentences.")
    target_customers: str
    strengths: list[str]
    weaknesses: list[str]
    recent_moves: list[str] = Field(description="Each: 'YYYY-MM - what happened - source URL'.")
    pricing_notes: str
    how_we_win: list[str]
    landmine_questions: list[str]
    objection_responses: list[str] = Field(description="Each: 'They say X' -> 'We say Y'.")
    sources: list[str]


def _battlecard_md(b: Battlecard) -> str:
    sec = lambda t, xs: [f"## {t}", *[f"- {x}" for x in xs], ""]  # noqa: E731
    return "\n".join([
        f"# Battlecard: {b.competitor}", "", b.overview, "", f"**Who they sell to:** {b.target_customers}", "",
        *sec("Their strengths", b.strengths), *sec("Their weaknesses", b.weaknesses),
        *sec("Recent moves", b.recent_moves), f"**Pricing:** {b.pricing_notes}", "",
        *sec("How we win", b.how_we_win), *sec("Landmine questions", b.landmine_questions),
        *sec("Objection responses", b.objection_responses), *sec("Sources", b.sources),
    ])


async def competitor_battlecard(competitor: str, parent_run_id: int | None = None) -> dict:
    submit, get_card = submit_tool(Battlecard, "submit_battlecard", "Submit the finished battlecard (call once).")
    result = await run_agent(
        agent="competitor_intel",
        model=settings.model_worker,
        role_prompt=INTEL_ROLE,
        prompt=f"Build a battlecard for: {competitor}. Finish by calling submit_battlecard.",
        tools=[*WEB_TOOLS, kb_search, submit],
        effort="medium",
        max_tokens=32000,
        parent_run_id=parent_run_id,
    )
    b: Battlecard = get_card()
    md = _battlecard_md(b)
    title = f"Battlecard: {b.competitor}"
    with session_scope() as db:
        doc = SalesDocument(kind="battlecard", title=title, content=md, data=b.model_dump(), run_id=result.run_id)
        db.add(doc)
        # Keep one live copy in the knowledge base so every agent (writer, objection handling) uses it.
        for old in db.scalars(select(KBDocument).where(KBDocument.title == title)):
            db.delete(old)
        db.flush()
        kb.add_document(db, title, "competitor", md)
        db.flush()
        return {"document_id": doc.id, "competitor": b.competitor}


# --------------------------------------------------------------------------- Signal Detector

SIGNAL_ROLE = """You are the Signal Detector. For each account, search the web for BUYING TRIGGERS from the
last {days} days that make our offering timely (see the ICP triggers in the company context): new executive
in a relevant role, funding, M&A, restructuring, relevant hiring, technology change, expansion, earnings
commentary about relevant priorities, or a stated initiative.

Only report real events you found, each with its source URL and date. Ignore generic news. Strength:
high = directly creates need/budget for what we sell; medium = plausible link; low = weak link.
recommended_action must be specific (who to contact, with what angle). Return an empty list for an account
with nothing relevant; that's a valid answer."""


class FoundSignal(BaseModel):
    account_id: int
    type: Literal["new_executive", "funding", "m_and_a", "hiring", "technology_change", "expansion",
                  "restructuring", "initiative", "earnings", "other"]
    headline: str
    detail: str
    url: str
    event_date: str | None = Field(default=None, description="ISO date if known.")
    strength: Literal["high", "medium", "low"]
    recommended_action: str


class SignalScan(BaseModel):
    signals: list[FoundSignal]


async def detect_signals(account_ids: list[int] | None = None, days: int = 90, limit: int = 10,
                         parent_run_id: int | None = None) -> dict:
    """Scan accounts for buying triggers. Defaults to the best-scored active accounts."""
    with session_scope() as db:
        if account_ids:
            accounts = [db.get(Account, i) for i in account_ids]
        else:
            # Accounts with the highest-scored live contacts first.
            rows = db.execute(
                select(Account, Contact.score).join(Contact)
                .where(Contact.status.notin_(["disqualified"]))
                .order_by(Contact.score.desc().nullslast())
            ).all()
            seen, accounts = set(), []
            for a, _ in rows:
                if a.id not in seen:
                    seen.add(a.id)
                    accounts.append(a)
        targets = [{"account_id": a.id, "name": a.name, "domain": a.domain, "industry": a.industry}
                   for a in accounts if a][:limit]
    if not targets:
        return {"signals": 0}

    submit, get_scan = submit_tool(SignalScan, "submit_signals",
                                   "Submit all signals found across the accounts (call once; an empty list is fine).")
    await run_agent(
        agent="signal_detector",
        model=settings.model_worker,
        role_prompt=SIGNAL_ROLE.replace("{days}", str(days)),
        prompt=(f"Today is {date.today().isoformat()}. Accounts:\n{json.dumps(targets)}\n"
                "Finish by calling submit_signals."),
        tools=[{**WEB_TOOLS[0], "max_uses": 4 * len(targets)}, WEB_TOOLS[1], submit],
        effort="medium",
        max_tokens=32000,
        parent_run_id=parent_run_id,
    )
    scan: SignalScan = get_scan()
    valid_ids = {t["account_id"] for t in targets}
    created = 0
    with session_scope() as db:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        for s in scan.signals:
            if s.account_id not in valid_ids:
                continue
            if s.event_date and s.event_date < cutoff:
                continue
            if db.scalar(select(Signal).where(Signal.account_id == s.account_id, Signal.url == s.url)):
                continue  # already known
            db.add(Signal(account_id=s.account_id, type=s.type, headline=s.headline, detail=s.detail,
                          url=s.url, event_date=s.event_date, strength=s.strength,
                          recommended_action=s.recommended_action))
            created += 1
            if s.strength == "high":
                for c in db.scalars(select(Contact).where(Contact.account_id == s.account_id)):
                    db.add(Activity(contact_id=c.id, type="signal", detail=f"{s.type}: {s.headline}"))
    return {"accounts_scanned": len(targets), "signals": created}


# --------------------------------------------------------------------------- Lead Sourcer

SOURCER_ROLE = """You are the Lead Sourcer. Find real companies that match our Ideal Customer Profile (in the
company context) and the rep's request, using web search. For each, explain the fit in one or two sentences
with evidence (a trigger, a stated initiative, size/industry match) and give its website domain.

Rules:
- Real, currently operating companies only, verified on the web. No duplicates of the existing accounts listed.
- Prefer companies showing a buying trigger right now.
- Suggest the personas to contact (titles), but do NOT invent people's names or email addresses; contacts
  come from the team's data provider or a CSV import."""


class SourcedAccount(BaseModel):
    name: str
    domain: str
    industry: str
    employees_estimate: int | None = None
    country: str | None = None
    fit_reason: str
    evidence_url: str
    target_titles: list[str]


class SourcingResult(BaseModel):
    accounts: list[SourcedAccount]


def _norm_domain(d: str) -> str:
    return d.lower().replace("https://", "").replace("http://", "").removeprefix("www.").strip("/")


async def source_accounts(request: str, count: int = 10, parent_run_id: int | None = None) -> dict:
    count = max(1, min(count, 25))
    with session_scope() as db:
        existing = [d for d in db.scalars(select(Account.domain)) if d]
    submit, get_found = submit_tool(SourcingResult, "submit_accounts", "Submit the companies found (call once).")
    await run_agent(
        agent="lead_sourcer",
        model=settings.model_worker,
        role_prompt=SOURCER_ROLE,
        prompt=(f"Find {count} companies. Request: {request}\n"
                f"Existing account domains to exclude: {json.dumps(existing[:500])}\n"
                "Finish by calling submit_accounts."),
        tools=[{**WEB_TOOLS[0], "max_uses": 15}, WEB_TOOLS[1], submit],
        effort="medium",
        max_tokens=32000,
        parent_run_id=parent_run_id,
    )
    found: SourcingResult = get_found()
    added, skipped = [], 0
    with session_scope() as db:
        known = {_norm_domain(d) for d in existing}
        for s in found.accounts[:count]:
            dom = _norm_domain(s.domain)
            if not dom or dom in known:
                skipped += 1
                continue
            known.add(dom)
            db.add(Account(
                name=s.name, domain=dom, industry=s.industry, employees=s.employees_estimate, country=s.country,
                source="sourced", fit_reason=f"{s.fit_reason} ({s.evidence_url})",
                notes=f"Target titles: {', '.join(s.target_titles)}",
            ))
            added.append(s.name)
    return {"added": added, "skipped_duplicates": skipped}
