"""Orchestrator (Opus): the sales copilot reps talk to. It plans, delegates to specialist agents, and reports.

Specialists are exposed as tools, so the orchestrator decides which to call, in what
order, and in parallel when it can (e.g. research five accounts at once).
"""
import json

from anthropic import beta_async_tool
from sqlalchemy import func, select

from ..config import settings
from ..db import session_scope
from ..services import insights
from ..models import Approval, Contact, Opportunity, SalesDocument, Signal
from . import committee, deals, hygiene, intel, marketing, person_researcher, proposals, researcher, scorer, strategy, writer
from .base import run_agent
from .shared_tools import find_leads, get_account, get_contact, kb_search

ROLE = """You are RevenueOS, the AI revenue copilot for the sales and marketing team. You coordinate specialist
agents to turn requests into pipeline:

- score_lead: Lead Scorer (fast). Fit and intent scoring, tiering, routing advice.
- research_account: Account Researcher (web + knowledge base). Produces the brief that outreach relies on.
- write_sequence: Outreach Writer. Drafts a personalized sequence into the approval queue.
- research_person: a sourced dossier of one person's public statements and priorities (deep personalisation).
- committee_outreach: coordinated, role-specific sequences for everyone at one account.
- lift_report: proof that AI outreach works, against a random holdout group.
- Everything you and the agents write also draws on the account's timeline (calls, Slack, email, meetings).
- prep_meeting: Meeting Prep. One-page call brief: people, agenda, discovery questions, proof points.
- analyze_call: Call Analyst. Transcript/notes -> MEDDICC, deal stage and health, CRM update, follow-up draft.
- build_proposal: Pitch & Proposal Builder. Tailored proposal (.docx) and pitch deck (.pptx).
- crm_hygiene: duplicates, missing data, stale leads and deals; fills well-known firmographics.
- list_deals: open opportunities with stage, health, next steps and risks.
- review_deal: Deal Strategist (win probability, stakeholders, next best actions; may draft an email).
- forecast: Forecast Analyst for the quarter. results_report: attribution and A/B test results.
- learn_from_results: refresh the learned outreach playbook from real reply data.
- plan_campaign / create_content: Campaign Strategist and Content Studio (drafts go to Approvals).
- abm_pack: personalized landing page, ads, one-pager and per-person emails for one target account.
- competitor_battlecard: researched battlecard; also updates the knowledge base.
- detect_signals / list_signals: buying triggers (new execs, funding, M&A, hiring) on our accounts.
- source_accounts: find new ICP-fit companies on the web (accounts only; contacts come from CSV/data provider).
- find_leads / get_contact / get_account / kb_search / pipeline_summary: look things up yourself.

How to work:
- Work out the steps first. Do independent steps in parallel (call several tools in one turn).
- Outreach without research is generic: research an account before writing, unless it has a recent brief
  (under 30 days old) or the rep says to skip it.
- Prioritize by score: A and B tier first. Don't spend research on disqualified or D-tier leads unless asked.
- Nothing is ever sent by you or any agent: drafts go to the Approvals inbox for a human.
  Tell the rep how many drafts are waiting there.
- A proposal is far stronger after a call: if the account has no call summary, say so and suggest analysing
  call notes first (but build it anyway if the rep insists).
- If the rep asks for something no tool can do yet (publishing posts, booking meetings, sending calendar invites),
  say so plainly and do the closest useful thing (e.g. draft the content as text in your reply).
- Reply concisely in Markdown: what you did, key findings, and the recommended next action."""


def make_tools(parent_run_id: int):
    @beta_async_tool
    async def score_lead(contact_id: int) -> str:
        """Score one lead for ICP fit and buying intent; updates the lead's score and tier.

        Args:
            contact_id: The lead's contact id.
        """
        result = await scorer.score_contact(contact_id, parent_run_id=parent_run_id)
        return result.model_dump_json()

    @beta_async_tool
    async def research_account(account_id: int, focus: str | None = None) -> str:
        """Research a company on the web and save a sales brief (priorities, pains, news, angle, proof points).

        Takes 1-3 minutes. Call it for several accounts in parallel when needed.

        Args:
            account_id: The account to research.
            focus: Optional thing to dig into, e.g. "their cloud migration" or "new CFO".
        """
        summary = await researcher.research_account(account_id, focus, parent_run_id=parent_run_id)
        return summary or "Brief saved."

    @beta_async_tool
    async def write_sequence(
        contact_id: int, steps: int = 3, instructions: str | None = None, ab_test: bool = False
    ) -> str:
        """Draft a personalized outbound sequence for one contact into the approval queue.

        Args:
            contact_id: The recipient contact id.
            steps: Number of touches, 1-6 (default 3).
            instructions: Optional guidance from the rep: angle, offer, event invite, tone.
            ab_test: If true, also draft a variant B of the first email.
        """
        return await writer.write_sequence(
            contact_id, steps, instructions, ab_test, parent_run_id=parent_run_id
        )

    @beta_async_tool
    async def pipeline_summary() -> str:
        """Counts of leads by status and tier, and how many drafts await approval."""
        with session_scope() as db:
            by_status = dict(db.execute(select(Contact.status, func.count()).group_by(Contact.status)).all())
            by_tier = dict(db.execute(select(Contact.score_tier, func.count()).group_by(Contact.score_tier)).all())
            pending = db.scalar(select(func.count()).select_from(Approval).where(Approval.status == "pending"))
        return json.dumps({"by_status": by_status, "by_tier": {str(k): v for k, v in by_tier.items()},
                           "pending_approvals": pending})

    @beta_async_tool
    async def prep_meeting(contact_id: int, goal: str | None = None, attendees: str | None = None,
                           when: str | None = None) -> str:
        """Write a meeting prep brief for an upcoming call and save it to the lead's documents.

        Args:
            contact_id: The main contact for the meeting.
            goal: What the rep wants from the call.
            attendees: Who will attend, with roles if known.
            when: Meeting date/time as text.
        """
        doc_id = await deals.prep_meeting(contact_id, goal, attendees, when, parent_run_id=parent_run_id)
        with session_scope() as db:
            return db.get(SalesDocument, doc_id).content

    @beta_async_tool
    async def analyze_call(contact_id: int, transcript: str) -> str:
        """Analyse a call transcript or notes: MEDDICC, stage, health, CRM opportunity update, follow-up email draft.

        Args:
            contact_id: The contact the call was with.
            transcript: The full transcript or the rep's notes, verbatim.
        """
        return json.dumps(await deals.analyze_call(contact_id, transcript, parent_run_id=parent_run_id))

    @beta_async_tool
    async def build_proposal(contact_id: int, instructions: str | None = None) -> str:
        """Build a tailored proposal (.docx) and pitch deck (.pptx) for the contact's account. Takes a few minutes.

        Args:
            contact_id: Who the proposal is addressed to.
            instructions: Optional scope, emphasis, or commercial guidance from the rep.
        """
        return json.dumps(await proposals.build_proposal(contact_id, instructions, parent_run_id=parent_run_id))

    @beta_async_tool
    async def crm_hygiene() -> str:
        """Run CRM hygiene: duplicates, missing data, quiet leads, deals needing attention."""
        r = await hygiene.run_hygiene(enrich=True, parent_run_id=parent_run_id)
        with session_scope() as db:
            return db.get(SalesDocument, r["document_id"]).content

    @beta_async_tool
    async def list_deals() -> str:
        """List open deals with stage, amount, health, next steps and risks."""
        with session_scope() as db:
            rows = [{"id": o.id, "name": o.name, "account": o.account.name, "stage": o.stage,
                     "amount": o.amount, "close_date": o.close_date, "health": o.health,
                     "next_steps": o.next_steps, "risks": o.risks}
                    for o in db.scalars(select(Opportunity).where(
                        Opportunity.stage.notin_(["closed_won", "closed_lost"])))]
        return json.dumps(rows) if rows else "No open deals."

    @beta_async_tool
    async def plan_campaign(goal: str, budget: float | None = None, start_date: str | None = None,
                            end_date: str | None = None) -> str:
        """Plan a marketing campaign: audience, message, channels and budget split, timeline, content plan.

        Args:
            goal: Business goal, e.g. "40 meetings with manufacturing CIOs in Q1 for the data assessment".
            budget: Optional total budget.
            start_date: Optional ISO start date.
            end_date: Optional ISO end date.
        """
        return json.dumps(await marketing.plan_campaign(goal, budget, start_date, end_date, parent_run_id=parent_run_id))

    @beta_async_tool
    async def create_content(content_type: str, brief: str, campaign_id: int | None = None,
                             account_id: int | None = None) -> str:
        """Draft one marketing piece into the approval queue.

        Args:
            content_type: blog_post, linkedin_post, ad, landing_page, newsletter, case_study, one_pager or social_post.
            brief: What it should say, for whom, and why.
            campaign_id: Optional campaign it belongs to.
            account_id: Optional target account (for ABM-style pieces).
        """
        return await marketing.create_content(content_type, brief, campaign_id, account_id,
                                              parent_run_id=parent_run_id)

    @beta_async_tool
    async def abm_pack(account_id: int, instructions: str | None = None) -> str:
        """Build a personalized ABM asset pack for one account (researches it first if needed).

        Args:
            account_id: Target account.
            instructions: Optional angle or offer.
        """
        return await marketing.abm_pack(account_id, instructions, parent_run_id=parent_run_id)

    @beta_async_tool
    async def competitor_battlecard(competitor: str) -> str:
        """Research a competitor and produce a battlecard (also stored in the knowledge base).

        Args:
            competitor: Competitor company name.
        """
        r = await intel.competitor_battlecard(competitor, parent_run_id=parent_run_id)
        with session_scope() as db:
            return db.get(SalesDocument, r["document_id"]).content

    @beta_async_tool
    async def detect_signals(account_ids: list[int] | None = None, days: int = 90) -> str:
        """Scan accounts on the web for recent buying triggers. Defaults to the top-scored accounts.

        Args:
            account_ids: Optional specific accounts.
            days: How far back to look (default 90).
        """
        return json.dumps(await intel.detect_signals(account_ids, days, parent_run_id=parent_run_id))

    @beta_async_tool
    async def list_signals() -> str:
        """List new (unactioned) buying signals, strongest first."""
        with session_scope() as db:
            rows = [{"account_id": s.account_id, "account": s.account.name, "type": s.type,
                     "headline": s.headline, "strength": s.strength, "url": s.url,
                     "recommended_action": s.recommended_action}
                    for s in db.scalars(select(Signal).where(Signal.status == "new")
                                        .order_by(Signal.id.desc()).limit(50))]
        return json.dumps(rows) if rows else "No new signals."

    @beta_async_tool
    async def source_accounts(request: str, count: int = 10) -> str:
        """Find new companies matching the ICP and the request, and add them as accounts.

        Args:
            request: What to look for, e.g. "mid-size European logistics firms modernizing their data stack".
            count: How many (max 25).
        """
        return json.dumps(await intel.source_accounts(request, count, parent_run_id=parent_run_id))

    @beta_async_tool
    async def review_deal(opportunity_id: int) -> str:
        """Deal Strategist: honest health, win probability, stakeholder gaps and next best actions for one deal.

        Args:
            opportunity_id: The deal to review.
        """
        r = await strategy.review_deal(opportunity_id, parent_run_id=parent_run_id)
        with session_scope() as db:
            return db.get(SalesDocument, r["document_id"]).content

    @beta_async_tool
    async def forecast(target: float | None = None) -> str:
        """Forecast Analyst: quarter forecast (commit, best case, weighted, coverage) with risks and focus deals.

        Args:
            target: Optional quarterly target; defaults to the configured one.
        """
        r = await strategy.forecast(target, parent_run_id=parent_run_id)
        with session_scope() as db:
            return db.get(SalesDocument, r["document_id"]).content

    @beta_async_tool
    async def results_report() -> str:
        """Attribution (by campaign, source, agent, sequence step) and A/B test results, computed from real outcomes."""
        with session_scope() as db:
            return json.dumps({"attribution": insights.attribution(db), "ab_tests": insights.ab_results(db)},
                              default=str)

    @beta_async_tool
    async def learn_from_results() -> str:
        """Update the outreach playbook from what actually earned replies. Needs enough sent emails to be reliable."""
        return json.dumps(await strategy.learn_from_results(parent_run_id=parent_run_id))

    @beta_async_tool
    async def research_person(contact_id: int) -> str:
        """Person Researcher: a sourced dossier of one person's public, professional footprint (talks, articles,
        interviews, priorities). Use before writing to important contacts.

        Args:
            contact_id: The person to research.
        """
        return json.dumps(await person_researcher.research_person(contact_id, parent_run_id=parent_run_id), default=str)

    @beta_async_tool
    async def committee_outreach(account_id: int, instructions: str | None = None) -> str:
        """Buying-committee outreach: one account narrative, a role-specific staggered sequence for every contact
        at the account (researches people first). Best for important multi-stakeholder accounts.

        Args:
            account_id: The account.
            instructions: Optional angle, offer or event.
        """
        return json.dumps(await committee.committee_outreach(account_id, instructions, parent_run_id=parent_run_id))

    @beta_async_tool
    async def lift_report() -> str:
        """Holdout experiment: meetings and pipeline for AI-contacted leads vs a random control group."""
        with session_scope() as db:
            return json.dumps(insights.lift_report(db), default=str)

    return [score_lead, research_account, research_person, write_sequence, committee_outreach, prep_meeting, analyze_call, build_proposal,
            crm_hygiene, list_deals, review_deal, forecast, results_report, learn_from_results,
            plan_campaign, create_content, abm_pack, competitor_battlecard,
            detect_signals, list_signals, source_accounts,
            lift_report, pipeline_summary, find_leads, get_contact, get_account, kb_search]


async def run_copilot(message: str, run_id: int) -> str:
    result = await run_agent(
        agent="orchestrator",
        model=settings.model_orchestrator,
        role_prompt=ROLE,
        prompt=message,
        tools=make_tools(run_id),
        effort="high",
        max_tokens=32000,
        run_id=run_id,
    )
    return result.text
