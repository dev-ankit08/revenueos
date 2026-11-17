"""Knowledge-base builder: onboarding in a day.

Reads the company's own website (and any public PDFs it links to, through web fetch) and drafts the knowledge-base
documents the assistants need most: what you sell, your ideal customers, buyer personas, brand voice and case studies.
Every draft cites the pages it came from and waits for a person to approve it; nothing goes live on its own.
"""
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..config import settings
from ..db import session_scope
from ..models import KBDraft
from .base import WEB_TOOLS, run_agent, submit_tool

ROLE = """You set up a sales and marketing knowledge base from a company's own website.
1. Fetch the home page, then the pages about products/services, industries, customers/case studies and about-us
   (follow links on the site; up to about 12 pages). Public PDFs linked from the site are fine.
2. Draft these documents, each from what the site actually says:
   - offering: what they sell and the outcomes it delivers (one document per major offering, max 3)
   - icp: the kinds of companies they serve (industry, size, region, signs of need)
   - persona: the buyers they speak to and what each cares about
   - brand_voice: how the site writes (tone, sentence style, words they use and avoid)
   - case_study: one per customer story on the site, with ONLY the figures the page states
3. Never invent customers, figures or claims. If the site doesn't say it, leave it out.
4. Call submit_drafts once with every document and the URLs each one came from."""


class Draft(BaseModel):
    category: str = Field(description="offering | icp | persona | brand_voice | case_study")
    title: str
    content: str = Field(description="Plain text, 120-400 words, facts from the site only.")
    source_urls: list[str]


class Drafts(BaseModel):
    documents: list[Draft]


async def build_from_website(url: str, parent_run_id: int | None = None) -> dict:
    submit, get_result = submit_tool(Drafts, "submit_drafts", "Submit every drafted knowledge-base document (once).")
    await run_agent(
        agent="kb_builder", model=settings.model_worker, role_prompt=ROLE, effort="medium", max_tokens=16000,
        prompt=f"Company website: {url}",
        tools=[{**WEB_TOOLS[0], "max_uses": 6}, {**WEB_TOOLS[1], "max_uses": 14}, submit],
        parent_run_id=parent_run_id)
    drafts: Drafts = get_result()
    allowed = {"offering", "icp", "persona", "brand_voice", "case_study"}
    created = 0
    with session_scope() as db:
        for d in drafts.documents:
            if d.category not in allowed or not d.content.strip():
                continue
            if db.scalar(select(KBDraft.id).where(KBDraft.title == d.title, KBDraft.status == "pending")):
                continue
            db.add(KBDraft(category=d.category, title=d.title[:255], content=d.content,
                           source_urls=d.source_urls[:10], website=url))
            created += 1
    return {"drafts": created}
