"""CRM Hygiene agent (Haiku): rule-based report + AI enrichment of accounts with missing firmographics."""
import json
from typing import Literal

from pydantic import BaseModel, Field

from ..config import settings
from ..db import session_scope
from ..models import Account
from ..services import hygiene
from .base import run_agent

ROLE = """You fill in missing company firmographics for a CRM. For each account, give its industry and an
employee-count estimate from your own knowledge of the company name and domain.
Use confidence "high" only for well-known companies you are sure about. For anything you don't recognise,
return confidence "low" and null values. Never guess for obscure names."""

INDUSTRIES = (
    "Manufacturing", "Retail & Consumer Goods", "Financial Services", "Healthcare & Life Sciences",
    "Logistics & Transportation", "Technology & Software", "Energy & Utilities", "Telecommunications",
    "Media & Entertainment", "Professional Services", "Public Sector", "Education", "Real Estate",
    "Travel & Hospitality", "Other",
)


class AccountFacts(BaseModel):
    account_id: int
    industry: Literal[INDUSTRIES] | None  # type: ignore[valid-type]
    employees_estimate: int | None = Field(default=None, description="Approximate global headcount.")
    confidence: Literal["high", "medium", "low"]


class Enrichment(BaseModel):
    accounts: list[AccountFacts]


async def run_hygiene(enrich: bool = True, parent_run_id: int | None = None) -> dict:
    """Returns {"document_id", "summary"}. With enrich=True, fills high-confidence gaps via Haiku."""
    applied: list[str] = []
    from ..tools import dataproviders

    if enrich and dataproviders.enrichment_provider():
        # A data provider beats the AI's memory: use it for contacts not yet enriched.
        from ..services import datahealth

        with session_scope() as db:
            n = datahealth.enrich_pending(db, limit=50)
        applied.append(f"{n} contacts enriched from {dataproviders.enrichment_provider()}")
        enrich = False
    if enrich:
        with session_scope() as db:
            todo = [{"account_id": a.id, "name": a.name, "domain": a.domain}
                    for a in db.query(Account).filter((Account.industry.is_(None)) | (Account.employees.is_(None)))
                    .limit(50)]
        if todo:
            result = await run_agent(
                agent="crm_hygiene",
                model=settings.model_fast,
                role_prompt=ROLE,
                prompt=f"Accounts:\n{json.dumps(todo)}",
                effort="low",
                max_tokens=4000,
                output_format=Enrichment,
                parent_run_id=parent_run_id,
            )
            with session_scope() as db:
                for f in result.parsed.accounts:
                    a = db.get(Account, f.account_id)
                    if not a or f.confidence != "high":
                        continue
                    changed = []
                    if not a.industry and f.industry:
                        a.industry = f.industry
                        changed.append(f"industry={f.industry}")
                    if not a.employees and f.employees_estimate:
                        a.employees = f.employees_estimate
                        changed.append(f"employees≈{f.employees_estimate:,}")
                    if changed:
                        a.notes = ((a.notes or "") + "\n[AI-filled: " + ", ".join(changed) + "]").strip()
                        applied.append(f"{a.name}: {', '.join(changed)}")

    with session_scope() as db:
        issues = hygiene.find_issues(db)
        extra = ("\n\n## AI-filled firmographics\n" + "\n".join(f"- {x}" for x in applied)) if applied else ""
        doc = hygiene.save_report(db, issues, extra, run_id=parent_run_id)
        counts = {k: len(v) for k, v in issues.items() if isinstance(v, list)}
        return {"document_id": doc.id, "issue_counts": counts, "ai_filled": applied}
