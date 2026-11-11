"""Account Researcher (Sonnet + web search): builds the one-page brief all outreach is grounded in."""
from anthropic import beta_async_tool

from ..config import settings
from ..db import session_scope
from ..models import Account, AccountBrief
from .base import WEB_TOOLS, run_agent
from .shared_tools import get_account, kb_search

ROLE = """You are the Account Researcher on the sales team. Produce a sharp, factual account brief that a rep
can use to write a relevant first message and run a good discovery call.

Process:
1. Call get_account to see what we already know.
2. Research the company on the web: its business, strategy, recent news (last ~12 months), leadership
   statements, hiring patterns, funding, acquisitions, and technology choices. Prefer primary sources
   (the company site, press releases, filings) and reputable press.
3. Use kb_search to find which of OUR offerings and case studies are most relevant to what you found.
4. Call save_account_brief exactly once with your findings, then reply with a 3-sentence summary.

Rules:
- Only state facts you found. Every news item needs its source URL. If something is uncertain, say so.
- Pain points are hypotheses; phrase them as such ("likely", "probably") unless the company stated them.
- relevant_proof_points must name real knowledge base entries; never invent customers or numbers.
- Keep it concise: a rep should absorb the brief in two minutes."""


def make_save_tool(account_id: int):
    @beta_async_tool
    async def save_account_brief(
        summary: str,
        priorities: list[str],
        pain_points: list[str],
        recent_news: list[str],
        recommended_angle: str,
        relevant_proof_points: list[str],
        likely_objections: list[str],
        sources: list[str],
    ) -> str:
        """Save the finished account brief. Call this exactly once at the end of your research.

        Args:
            summary: 3-5 sentences: who they are, what matters to them now, and why we are relevant.
            priorities: The company's stated or evident strategic priorities.
            pain_points: Likely pains our offering addresses (hypotheses unless stated by the company).
            recent_news: Recent events, each formatted "YYYY-MM - headline - source URL".
            recommended_angle: The single best angle for a first outreach and why.
            relevant_proof_points: Titles of our knowledge-base case studies/offerings that fit, with one line each on why.
            likely_objections: Objections we should expect, each with a one-line response.
            sources: URLs used.
        """
        with session_scope() as db:
            brief = AccountBrief(
                account_id=account_id, summary=summary, priorities=priorities, pain_points=pain_points,
                recent_news=recent_news, recommended_angle=recommended_angle,
                relevant_proof_points=relevant_proof_points, likely_objections=likely_objections,
                sources=sources,
            )
            db.add(brief)
            account = db.get(Account, account_id)
            for c in account.contacts:
                if c.status == "new":
                    c.status = "researched"
            db.flush()
            return f"Saved brief {brief.id}."

    return save_account_brief


async def research_account(account_id: int, focus: str | None = None, parent_run_id: int | None = None) -> str:
    with session_scope() as db:
        account = db.get(Account, account_id)
        if not account:
            raise ValueError(f"No account {account_id}")
        label = f"{account.name} ({account.domain or 'domain unknown'})"

    prompt = f"Research account_id={account_id}: {label}."
    if focus:
        prompt += f"\nThe rep asked you to focus on: {focus}"

    result = await run_agent(
        agent="account_researcher",
        model=settings.model_worker,
        role_prompt=ROLE,
        prompt=prompt,
        tools=[*WEB_TOOLS, get_account, kb_search, make_save_tool(account_id)],
        effort="medium",
        parent_run_id=parent_run_id,
    )
    note = verify_brief_sources(account_id, result.run_id)
    return result.text + (f"\n\n{note}" if note else "")


def verify_brief_sources(account_id: int, run_id: int) -> str | None:
    """Remove news items and sources the researcher cited but never found or opened in this run."""
    from sqlalchemy import select

    from ..models import AgentRun
    from ..services import sources

    with session_scope() as db:
        run = db.get(AgentRun, run_id)
        if not run or not run.source_urls:
            return None  # no record of what was read (e.g. no web tools ran): nothing to compare against
        brief = db.scalar(select(AccountBrief).where(AccountBrief.account_id == account_id)
                          .order_by(AccountBrief.id.desc()))
        if not brief or (run.started_at and brief.created_at and brief.created_at < run.started_at):
            return None
        cited = list(brief.sources or []) + [u for item in brief.recent_news or [] for u in sources.urls_in_text(item)]
        bad = sources.unverified(db, cited)
        if not bad:
            brief.unverified_sources = []
            return None
        bad_keys = {sources.normalise(u) for u in bad}
        brief.sources = [u for u in brief.sources or [] if sources.normalise(u) not in bad_keys]
        brief.recent_news = [n for n in brief.recent_news or []
                             if not any(sources.normalise(u) in bad_keys for u in sources.urls_in_text(n))]
        brief.unverified_sources = bad
        return f"Removed {len(bad)} cited source(s) the researcher never actually opened."
