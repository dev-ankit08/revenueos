"""Phase 4 endpoints: deal reviews, forecast, attribution, A/B results, learning loop."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import jobs
from ..agents import strategy
from ..config import settings
from ..db import get_db
from ..models import KBDocument, Opportunity, SalesDocument
from ..services import governance, insights

router = APIRouter(prefix="/api", tags=["insights"])


def _latest(db: Session, kind: str, **filters) -> dict | None:
    stmt = select(SalesDocument).where(SalesDocument.kind == kind)
    for field, value in filters.items():
        stmt = stmt.where(getattr(SalesDocument, field) == value)
    d = db.scalar(stmt.order_by(SalesDocument.id.desc()))
    return {"id": d.id, "title": d.title, "content": d.content, "data": d.data, "created_at": d.created_at} if d else None


# ------------------------------------------------------------------ deal strategist

@router.post("/deals/{opp_id}/review")
def review_deal(opp_id: int, db: Session = Depends(get_db)):
    if not db.get(Opportunity, opp_id):
        raise HTTPException(404, "Deal not found")

    async def work(run_id: int) -> str:
        r = await strategy.review_deal(opp_id, parent_run_id=run_id)
        return f"{r['health']}, {r['win_probability']}% (document:{r['document_id']})"
    return {"run_id": jobs.start("job:deal_review", f"Review deal {opp_id}", work)}


@router.post("/deals/review-all")
def review_all():
    async def work(run_id: int) -> str:
        return await strategy.review_pipeline(parent_run_id=run_id)
    return {"run_id": jobs.start("job:pipeline_review", "Review all open deals", work)}


@router.get("/deals/{opp_id}/strategy")
def deal_strategy(opp_id: int, db: Session = Depends(get_db)):
    return _latest(db, "deal_strategy", opportunity_id=opp_id) or {}


# ------------------------------------------------------------------ forecast

class ForecastIn(BaseModel):
    target: float | None = None


@router.get("/forecast/numbers")
def forecast_numbers(target: float | None = None, db: Session = Depends(get_db)):
    return insights.forecast_numbers(db, target or settings.quarterly_target or None)


@router.post("/forecast")
def run_forecast(body: ForecastIn):
    async def work(run_id: int) -> str:
        r = await strategy.forecast(body.target, parent_run_id=run_id)
        return f"{r['headline']} (document:{r['document_id']})"
    return {"run_id": jobs.start("job:forecast", "Forecast", work)}


@router.get("/forecast/latest")
def latest_forecast(db: Session = Depends(get_db)):
    return _latest(db, "forecast") or {}


# ------------------------------------------------------------------ attribution, A/B, learning

@router.get("/insights/attribution")
def attribution(db: Session = Depends(get_db)):
    return insights.attribution(db)


@router.get("/insights/ab-tests")
def ab_tests(db: Session = Depends(get_db)):
    return insights.ab_results(db)


@router.post("/insights/learn")
def learn():
    async def work(run_id: int) -> str:
        r = await strategy.learn_from_results(parent_run_id=run_id)
        return (f"Playbook updated from {r['emails_analysed']} emails (confidence {r['confidence']})"
                if r["updated"] else r["reason"])
    return {"run_id": jobs.start("job:learning", "Learn from outreach results", work)}


@router.get("/insights/playbook")
def playbook(db: Session = Depends(get_db)):
    d = db.scalar(select(KBDocument).where(KBDocument.title == strategy.PLAYBOOK_TITLE))
    return {"content": d.content, "updated_at": d.created_at} if d else {}


@router.get("/insights/lift")
def lift(db: Session = Depends(get_db)):
    return insights.lift_report(db)


# ------------------------------------------------------------------ AI governance

@router.get("/governance")
def governance_overview():
    return governance.overview()


class GovernanceIn(BaseModel):
    model: str | None = None
    monthly_budget_usd: float | None = None
    enabled: bool = True


@router.put("/governance/{agent}")
def governance_update(agent: str, body: GovernanceIn):
    """agent "*" sets the total monthly AI budget."""
    try:
        governance.update(agent, model=body.model, monthly_budget_usd=body.monthly_budget_usd, enabled=body.enabled)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return governance.overview()


# ------------------------------------------------------------------ pilot report

def _pilot(db: Session) -> dict:
    from ..models import Evaluation
    from .evaluation import results_for

    return {
        "lift": insights.lift_report(db),
        "roi": insights.roi_ledger(db),
        "ab_tests": insights.ab_results(db),
        "personalisation_vs_replies": insights.personalisation_vs_replies(db),
        "evaluations": [results_for(db, e.id) for e in db.scalars(select(Evaluation).order_by(Evaluation.id))],
    }


@router.get("/insights/roi")
def roi(db: Session = Depends(get_db)):
    return {"roi": insights.roi_ledger(db), "personalisation_vs_replies": insights.personalisation_vs_replies(db)}


@router.get("/pilot/report")
def pilot_report(db: Session = Depends(get_db)):
    """Everything a pilot proves, in one place (also the source for the client-facing brief)."""
    return _pilot(db)


@router.get("/pilot/report.md", response_class=PlainTextResponse)
def pilot_report_markdown(db: Session = Depends(get_db)):
    r = _pilot(db)
    lift, roi_, p = r["lift"], r["roi"], r["personalisation_vs_replies"]
    lines = [f"# {settings.company_name}: RevenueOS pilot report", "",
             "## Does AI outreach add meetings? (random control group)",
             f"- AI outreach: {lift['treatment']['contacts']} contacts, {lift['treatment']['meetings']} meetings "
             f"({lift['treatment']['meeting_rate'] or 0}%)",
             f"- Control (no outreach): {lift['holdout']['contacts']} contacts, {lift['holdout']['meetings']} meetings "
             f"({lift['holdout']['meeting_rate'] or 0}%)",
             f"- **{lift['verdict']}**", "", "## Do people prefer the drafts? (blind test)"]
    lines += [f"- {e['name']}: RevenueOS {e['revenueos_wins']} vs {e['incumbent_label']} {e['incumbent_wins']} "
              f"({e['ties']} ties). **{e['verdict']}**" for e in r["evaluations"]] or ["- No blind evaluations yet."]
    lines += ["", "## Return on AI spend",
              f"- AI spend ${roi_['ai_spend_usd']:,.2f}; {roi_['emails_sent']} emails sent; {roi_['meetings']} meetings",
              f"- AI-influenced deals: {roi_['influenced_opportunities']} · pipeline {roi_['influenced_pipeline']:,.0f} · "
              f"won {roi_['influenced_won']:,.0f}",
              f"- Cost per meeting: {roi_['cost_per_meeting'] if roi_['cost_per_meeting'] is not None else '–'} USD",
              f"- _{roi_['definition']}_", "", "## Does personalisation pay?",
              *[f"- Score {b['score']}: {b['sent']} sent, {b['positive_rate'] if b['positive_rate'] is not None else '–'}% "
                f"positive replies" for b in p], "", "## A/B tests",
              *([f"- {t['agent']} step {t['step']}: {t['verdict']}" for t in r["ab_tests"]] or ["- None yet."])]
    return "\n".join(lines)
