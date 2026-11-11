"""Marketing agents: Campaign Strategist (Opus), Content Studio (Sonnet), ABM (Sonnet).

All content lands in the approval queue as kind="content"; a person publishes it.
"""
import json
from typing import Literal

from anthropic import beta_async_tool
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..config import settings
from ..db import session_scope
from ..models import Account, Activity, Approval, Campaign, CampaignMember, Contact, SalesDocument
from ..services import approvals, memory, personalisation
from ..tools.crm import get_crm
from . import researcher
from .base import WEB_TOOLS, new_run, run_agent
from .shared_tools import brief_to_dict, get_account, kb_search
from .writer import Claim, make_queue_tool

ContentType = Literal[
    "blog_post", "linkedin_post", "ad", "landing_page", "newsletter", "case_study", "one_pager", "social_post"
]

CONTENT_GUIDE = {
    "blog_post": "800-1200 words, Markdown. A specific, useful point of view with headings; SEO title and meta "
                 "description at the top; one soft CTA at the end.",
    "linkedin_post": "120-220 words. Strong first line (no clickbait), short paragraphs, one idea, a question or "
                     "CTA at the end, 0-3 relevant hashtags.",
    "ad": "Three variants. Each: headline (max 70 chars), primary text (max 150 chars), CTA button label. "
          "Different angle per variant.",
    "landing_page": "Markdown sections: hero headline + subhead + CTA, the problem, how we solve it (3 points), "
                    "proof (KB results only), FAQ (3-4), final CTA.",
    "newsletter": "300-500 words: subject line, preview text, 2-3 short sections with one takeaway each, CTA.",
    "case_study": "Only from a knowledge-base case study. Challenge, approach, results (exact KB figures), quote "
                  "placeholder marked [Customer quote to be approved].",
    "one_pager": "Markdown, fits one page: headline, the problem, our approach, 3 outcomes, proof, next step.",
    "social_post": "Under 60 words, one idea, one CTA.",
}


def make_content_tool(run_id: int, grounding: str, agent_name: str,
                      campaign_id: int | None = None, account_id: int | None = None):
    @beta_async_tool
    async def queue_content(content_type: ContentType, title: str, body: str, rationale: str,
                            claims: list[Claim] | None = None) -> str:
        """Queue one finished piece of marketing content for human review and publishing.

        Args:
            content_type: blog_post, linkedin_post, ad, landing_page, newsletter, case_study, one_pager or social_post.
            title: Internal title for the piece (for ads/posts: a short label).
            body: The complete content, final wording, Markdown allowed.
            rationale: For the reviewer: audience, angle, and which KB facts it relies on.
            claims: Every factual claim with its source (kb:<document title> or a URL).
        """
        with session_scope() as db:
            a = approvals.queue(
                db, kind="content", content_type=content_type, title=title, subject=title, body=body,
                rationale=rationale, created_by_agent=agent_name, run_id=run_id, grounding_extra=grounding,
                campaign_id=campaign_id, account_id=account_id, claims=[c.model_dump() for c in claims or []],
            )
            aid = a.id
        score = await personalisation.grade_approval(aid, grounding, parent_run_id=run_id)
        with session_scope() as db:
            flags = db.get(Approval, aid).guardrail_flags
        return (f"Queued as approval {aid}" + (f" (quality {score}/100)" if score is not None else "")
                + (f" with reviewer notes: {flags}" if flags else "."))

    return queue_content


# --------------------------------------------------------------------------- Content Studio

STUDIO_ROLE = """You are Content Studio, the marketing team's writer. You write in the BRAND VOICE and every
factual claim about us comes from the knowledge base (kb_search). Use web_search only for current market
context, and cite the source in the text when you use an outside fact.

Format guide for this piece:
{guide}

Write the piece, check it against the guide, then call queue_content once. A customer quote you don't have
must be marked as a placeholder in square brackets with "to be approved"; never invent quotes, customers
or numbers. Reply with one line saying what you queued."""


async def create_content(
    content_type: str, brief: str, campaign_id: int | None = None, account_id: int | None = None,
    parent_run_id: int | None = None,
) -> str:
    if content_type not in CONTENT_GUIDE:
        raise ValueError(f"content_type must be one of {list(CONTENT_GUIDE)}")
    context = ""
    with session_scope() as db:
        if campaign_id and (c := db.get(Campaign, campaign_id)):
            plan = c.plan or {}
            context += (f"\nCampaign: {c.name}. Key message: {plan.get('key_message')}. "
                        f"Offer/CTA: {plan.get('offer')}. Audience: {json.dumps(plan.get('audience'))}")
        if account_id and (a := db.get(Account, account_id)):
            context += f"\nTarget account: {a.name}. Brief: {json.dumps(brief_to_dict(a.briefs[0] if a.briefs else None), default=str)}"

    prompt = f"Write a {content_type}.\nBrief: {brief}{context}"
    run_id = new_run("content_studio", settings.model_worker, prompt, parent_run_id)
    result = await run_agent(
        agent="content_studio",
        model=settings.model_worker,
        role_prompt=STUDIO_ROLE.replace("{guide}", CONTENT_GUIDE[content_type]),
        prompt=prompt,
        tools=[*WEB_TOOLS, kb_search, make_content_tool(run_id, context, "content_studio", campaign_id, account_id)],
        effort="medium",
        max_tokens=32000,
        run_id=run_id,
    )
    return result.text


# --------------------------------------------------------------------------- Campaign Strategist

STRATEGIST_ROLE = """You are the Campaign Strategist. Turn a business goal into a campaign plan the marketing
team can execute and sales will thank them for. Work from the ICP and offerings in the company context,
use kb_search for proof points and competitive angles, and size the audience with count_audience
(try a couple of filter combinations; a campaign needs enough targets in our database or a sourcing plan).

Be concrete: realistic KPIs tied to pipeline, a budget split that sums to 100%, a week-by-week timeline,
and a content plan where each item has a distinct job in the funnel. Prefer a few channels done well."""


class Kpi(BaseModel):
    metric: str
    target: str


class Audience(BaseModel):
    industries: list[str]
    title_keywords: list[str] = Field(description="Words matched against job titles, e.g. 'Data', 'CIO'.")
    min_employees: int | None = None
    max_employees: int | None = None
    regions: list[str]


class ChannelMix(BaseModel):
    channel: str
    budget_pct: int = Field(ge=0, le=100)
    rationale: str


class TimelineWeek(BaseModel):
    week: int
    activities: list[str]


class ContentItem(BaseModel):
    content_type: ContentType
    title: str
    angle: str
    channel: str
    week: int


class CampaignPlan(BaseModel):
    name: str
    objective: str
    kpis: list[Kpi]
    audience: Audience
    key_message: str
    value_props: list[str]
    offer: str = Field(description="The call to action / offer.")
    channels: list[ChannelMix]
    timeline: list[TimelineWeek]
    content_plan: list[ContentItem]
    sales_play: str = Field(description="How SDRs/AEs follow up on engaged contacts.")
    risks: list[str]


def _audience_query(industries, title_keywords, min_employees, max_employees):
    stmt = select(Contact).join(Account).where(Contact.status != "disqualified")
    if industries:
        stmt = stmt.where(Account.industry.in_(industries) | Account.industry.is_(None))
    if title_keywords:
        cond = None
        for kw in title_keywords:
            c = Contact.title.ilike(f"%{kw}%")
            cond = c if cond is None else cond | c
        stmt = stmt.where(cond)
    if min_employees:
        stmt = stmt.where(Account.employees >= min_employees)
    if max_employees:
        stmt = stmt.where(Account.employees <= max_employees)
    return stmt


@beta_async_tool
async def count_audience(
    industries: list[str], title_keywords: list[str],
    min_employees: int | None = None, max_employees: int | None = None,
) -> str:
    """Count contacts in our database matching audience filters, with a small sample.

    Args:
        industries: Account industries to include (empty list = any).
        title_keywords: Words to match in job titles (empty list = any).
        min_employees: Optional minimum company size.
        max_employees: Optional maximum company size.
    """
    with session_scope() as db:
        rows = list(db.scalars(_audience_query(industries, title_keywords, min_employees, max_employees)))
        known = sorted({a or "unknown" for a in db.scalars(select(Account.industry).distinct())})
        sample = [f"{c.full_name}, {c.title} @ {c.account.name}" for c in rows[:5]]
    return json.dumps({"matching_contacts": len(rows), "sample": sample, "industries_in_db": known})


def _plan_markdown(p: CampaignPlan) -> str:
    out = [f"# {p.name}", "", f"**Objective:** {p.objective}", f"**Key message:** {p.key_message}",
           f"**Offer:** {p.offer}", "", "## KPIs", *[f"- {k.metric}: {k.target}" for k in p.kpis],
           "", "## Audience", f"- Industries: {', '.join(p.audience.industries) or 'any'}",
           f"- Titles: {', '.join(p.audience.title_keywords) or 'any'}",
           f"- Size: {p.audience.min_employees or '-'} to {p.audience.max_employees or '-'} employees",
           f"- Regions: {', '.join(p.audience.regions) or 'any'}",
           "", "## Value propositions", *[f"- {v}" for v in p.value_props],
           "", "## Channels", *[f"- {c.channel} ({c.budget_pct}%): {c.rationale}" for c in p.channels],
           "", "## Timeline", *[f"- Week {w.week}: {'; '.join(w.activities)}" for w in p.timeline],
           "", "## Content plan", *[f"- W{c.week} {c.content_type} on {c.channel}: {c.title} ({c.angle})" for c in p.content_plan],
           "", "## Sales play", p.sales_play, "", "## Risks", *[f"- {r}" for r in p.risks]]
    return "\n".join(out)


async def plan_campaign(
    goal: str, budget: float | None = None, start_date: str | None = None, end_date: str | None = None,
    parent_run_id: int | None = None,
) -> dict:
    prompt = f"Goal: {goal}"
    if budget:
        prompt += f"\nBudget: {budget:,.0f}"
    if start_date or end_date:
        prompt += f"\nTimeframe: {start_date or '?'} to {end_date or '?'}"

    result = await run_agent(
        agent="campaign_strategist",
        model=settings.model_orchestrator,
        role_prompt=STRATEGIST_ROLE,
        prompt=prompt,
        tools=[kb_search, count_audience],
        effort="high",
        max_tokens=32000,
        output_format=CampaignPlan,
        parent_run_id=parent_run_id,
    )
    p: CampaignPlan = result.parsed

    with session_scope() as db:
        campaign = Campaign(name=p.name, goal=goal, budget=budget, start_date=start_date, end_date=end_date,
                            plan=p.model_dump())
        db.add(campaign)
        db.flush()
        a = p.audience
        members = list(db.scalars(_audience_query(a.industries, a.title_keywords, a.min_employees, a.max_employees)))
        for c in members:
            db.add(CampaignMember(campaign_id=campaign.id, contact_id=c.id))
        db.add(SalesDocument(kind="campaign_plan", title=f"Campaign plan: {p.name}", content=_plan_markdown(p),
                             data=p.model_dump(), run_id=result.run_id))
        get_crm().create_campaign(campaign, members)
        return {"campaign_id": campaign.id, "name": p.name, "members": len(members),
                "content_items": len(p.content_plan)}


async def generate_campaign_content(campaign_id: int, item_indexes: list[int] | None = None,
                                    parent_run_id: int | None = None) -> str:
    """Run Content Studio for content-plan items (all if no indexes given)."""
    with session_scope() as db:
        plan = db.get(Campaign, campaign_id).plan
    items = plan.get("content_plan", [])
    chosen = [items[i] for i in (item_indexes or range(len(items))) if 0 <= i < len(items)]
    for item in chosen:
        await create_content(
            item["content_type"],
            f"{item['title']}. Angle: {item['angle']}. Channel: {item['channel']}. Campaign week {item['week']}.",
            campaign_id=campaign_id, parent_run_id=parent_run_id,
        )
    return f"Drafted {len(chosen)} content pieces for approval."


# --------------------------------------------------------------------------- ABM

ABM_ROLE = """You are the ABM (account-based marketing) agent. Build a personalized asset pack for ONE target
account, grounded in its research brief and our knowledge base.

Produce and queue, in this order:
1. queue_content landing_page: a personalized page for this account (their priorities in their language).
2. queue_content ad: three LinkedIn ad variants aimed at this account's buying group.
3. queue_content one_pager: a one-pager for the champion to forward internally.
4. queue_message: one personalized email (sequence_step=1, send_after_days=0) for EACH contact listed in
   get_account, tailored to that person's role (economic buyer vs. champion vs. technical).
Then reply with a short summary of the account strategy: who to engage first and why.

Never invent facts about the account or results; use the brief and kb_search."""


async def abm_pack(account_id: int, instructions: str | None = None, parent_run_id: int | None = None) -> str:
    with session_scope() as db:
        account = db.get(Account, account_id)
        if not account:
            raise ValueError(f"No account {account_id}")
        has_brief = bool(account.briefs)
    if not has_brief:
        await researcher.research_account(account_id, parent_run_id=parent_run_id)

    with session_scope() as db:
        account = db.get(Account, account_id)
        contact_ids = {c.id for c in account.contacts if c.status != "disqualified"}
        brief = brief_to_dict(account.briefs[0] if account.briefs else None)
        name = account.name
    grounding = json.dumps(brief, default=str)

    prompt = f"Build the ABM pack for account_id={account_id} ({name})."
    if instructions:
        prompt += f"\nInstructions: {instructions}"
    history = memory.context_block(account_id, None)
    if history:
        prompt += "\n\n" + history
    run_id = new_run("abm", settings.model_worker, prompt, parent_run_id)
    result = await run_agent(
        agent="abm",
        model=settings.model_worker,
        role_prompt=ABM_ROLE,
        prompt=prompt,
        tools=[get_account, kb_search,
               make_content_tool(run_id, grounding, "abm", account_id=account_id),
               make_queue_tool(contact_ids, run_id, grounding, agent_name="abm")],
        effort="medium",
        max_tokens=32000,
        run_id=run_id,
    )
    with session_scope() as db:
        db.add(SalesDocument(kind="abm_strategy", title=f"ABM strategy: {name}", content=result.text,
                             account_id=account_id, run_id=run_id))
        for cid in contact_ids:
            db.add(Activity(contact_id=cid, type="abm_pack", detail="Personalized ABM assets drafted"))
    return result.text
