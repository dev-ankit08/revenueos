"""ROI Statement: one board-ready page that answers "is this worth it?", monthly by email and as a Word document.

Hours saved are estimated from what the assistants actually did, using editable minutes-per-task assumptions
(ROI_MINUTES, per workspace). Everything else is measured: meetings, pipeline and wins at accounts RevenueOS worked,
AI spend, the control-group lift, draft quality, forecast accuracy and deliverability.
"""
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import AgentRun, Approval, CalendarEvent, Opportunity, PlacementTest

UTC = timezone.utc

# Minutes a person would have spent doing what each assistant did once (edit with ROI_MINUTES).
DEFAULT_MINUTES = {
    "account_researcher": 30, "person_researcher": 20, "outreach_writer": 25, "reply_writer": 8,
    "call_analyst": 20, "meeting_prep": 25, "proposal_builder": 120, "deal_strategist": 30, "lead_scorer": 3,
    "crm_hygiene": 15, "content_studio": 60, "campaign_strategist": 120, "competitor_intel": 90,
    "forecast_analyst": 60, "committee_planner": 45, "kb_builder": 120,
}


def minutes_table() -> dict:
    table = dict(DEFAULT_MINUTES)
    try:
        table.update(json.loads(settings.roi_minutes or "{}"))
    except ValueError:
        pass
    return table


def build(db: Session, days: int = 30, now: datetime | None = None) -> dict:
    from . import insights
    from .pipeline_intel import accuracy

    now = now or datetime.now(UTC)
    since = now - timedelta(days=days)
    runs = db.execute(select(AgentRun.agent, func.count()).where(AgentRun.status == "succeeded", AgentRun.started_at >= since,
                                                                 AgentRun.parent_run_id.is_(None) | AgentRun.agent.in_(list(DEFAULT_MINUTES)))
                      .group_by(AgentRun.agent)).all()
    minutes = minutes_table()
    saved = {agent: n * minutes[agent] for agent, n in runs if agent in minutes}
    hours = round(sum(saved.values()) / 60, 1)
    spend = float(db.scalar(select(func.coalesce(func.sum(AgentRun.cost_usd), 0.0)).where(AgentRun.started_at >= since)) or 0)
    sent = db.scalar(select(func.count()).select_from(Approval).where(Approval.status == "sent", Approval.sent_at >= since)) or 0
    meetings = db.scalar(select(func.count()).select_from(CalendarEvent).where(
        CalendarEvent.contact_id.is_not(None), CalendarEvent.starts_at >= since, CalendarEvent.starts_at <= now)) or 0
    new_opps = list(db.scalars(select(Opportunity).where(Opportunity.created_at >= since)))
    won = [o for o in db.scalars(select(Opportunity).where(Opportunity.stage == "closed_won", Opportunity.closed_at >= since))]
    roi = insights.roi_ledger(db)
    lift = insights.lift_report(db)
    quality = insights.draft_quality(db, days=days)
    placement = db.scalar(select(PlacementTest).where(PlacementTest.placement_pct.is_not(None)).order_by(PlacementTest.id.desc()))
    last_q = None
    q = f"{now:%Y}-Q{(now.month - 1) // 3 + 1}"
    year, qn = int(q[:4]), int(q[-1])
    prev = f"{year - 1}-Q4" if qn == 1 else f"{year}-Q{qn - 1}"
    acc = accuracy(db, prev)
    if acc:
        last_q = {"period": prev, **acc["mid_quarter_accuracy"]}
    loaded_rate = float(settings.roi_hourly_cost or 0)
    return {
        "period_days": days, "from": since.date().isoformat(), "to": now.date().isoformat(),
        "company": settings.company_name,
        "outcomes": {"emails_sent": sent, "customer_meetings": meetings, "new_opportunities": len(new_opps),
                     "new_pipeline": round(sum(o.amount or 0 for o in new_opps), 2),
                     "won_deals": len(won), "won_revenue": round(sum(o.amount or 0 for o in won), 2),
                     "ai_influenced_pipeline_all_time": roi["influenced_pipeline"],
                     "ai_influenced_won_all_time": roi["influenced_won"]},
        "efficiency": {"hours_saved": hours, "by_assistant_hours": {k: round(v / 60, 1) for k, v in sorted(saved.items(), key=lambda kv: -kv[1])},
                       "value_of_time_saved": round(hours * loaded_rate, 2) if loaded_rate else None,
                       "ai_spend_usd": round(spend, 2),
                       "cost_per_meeting": round(spend / meetings, 2) if meetings else None,
                       "assumptions_minutes": minutes},
        "proof": {"holdout": {"verdict": lift.get("verdict"), "meeting_rate_lift_pts": lift.get("meeting_rate_lift_points"),
                              "treatment_contacts": lift["treatment"]["contacts"], "holdout_contacts": lift["holdout"]["contacts"]}},
        "quality": {"approved_unedited_pct": quality["approved_unedited_pct"], "reject_rate": quality["reject_rate"],
                    "forecast_accuracy_last_quarter": last_q},
        "deliverability": {"inbox_placement_pct": placement.placement_pct if placement else None},
    }


def _money(x) -> str:
    return f"{x:,.0f}" if x else "0"


def to_markdown(r: dict) -> str:
    o, e, p, q = r["outcomes"], r["efficiency"], r["proof"]["holdout"], r["quality"]
    lines = [f"# {r['company']}: RevenueOS ROI statement", f"{r['from']} to {r['to']} ({r['period_days']} days)", "",
             "## Results",
             f"- {o['customer_meetings']} customer meetings; {o['new_opportunities']} new opportunities worth {_money(o['new_pipeline'])}",
             f"- {o['won_deals']} deals won worth {_money(o['won_revenue'])}",
             f"- Since the start: {_money(o['ai_influenced_pipeline_all_time'])} open pipeline and {_money(o['ai_influenced_won_all_time'])} won "
             "at accounts RevenueOS contacted before the deal began", "",
             "## Time and cost",
             f"- About {e['hours_saved']} hours of research, writing and admin done by the assistants"
             + (f" (worth about {_money(e['value_of_time_saved'])})" if e["value_of_time_saved"] else ""),
             f"- AI cost: ${e['ai_spend_usd']:,.2f}" + (f"; ${e['cost_per_meeting']:,.2f} per meeting" if e["cost_per_meeting"] else ""),
             "", "## Proof",
             f"- Control group: {p['verdict'] or 'not enough data yet'}"
             + (f" ({p['meeting_rate_lift_pts']:+} points meeting rate)" if p.get("meeting_rate_lift_pts") is not None else ""),
             f"- Drafts sent without edits: {q['approved_unedited_pct'] if q['approved_unedited_pct'] is not None else '–'}%",
             ]
    if q["forecast_accuracy_last_quarter"]:
        f = q["forecast_accuracy_last_quarter"]
        lines.append(f"- Forecast accuracy {f['period']} (mid-quarter call): weighted {f['weighted']}%, commit {f['commit']}%")
    if r["deliverability"]["inbox_placement_pct"] is not None:
        lines.append(f"- Inbox placement: {r['deliverability']['inbox_placement_pct']}%")
    lines += ["", "_Hours saved use the per-task assumptions in Settings (ROI_MINUTES); everything else is measured._"]
    return "\n".join(lines)


def to_docx(r: dict):
    from ..tools.docgen import build_proposal_docx, safe_filename

    sections = []
    for block in to_markdown(r).split("\n## ")[1:]:
        heading, *rest = block.split("\n")
        sections.append({"heading": heading, "bullets": [l[2:] for l in rest if l.startswith("- ")],
                         "paragraphs": [l.strip("_") for l in rest if l.startswith("_")]})
    name = safe_filename(f"roi-statement-{r['to']}") + ".docx"
    path = settings.generated_dir / name
    build_proposal_docx(path, f"{r['company']}: RevenueOS ROI statement", f"{r['from']} to {r['to']}", sections)
    return path


def recipients() -> list[str]:
    return [x.strip() for x in (settings.roi_recipients or "").split(",") if "@" in x]


def send_monthly(db: Session) -> int:
    from ..tools.email import send_internal

    to = recipients()
    if not to:
        return 0
    text = to_markdown(build(db, days=30))
    for person in to:
        send_internal(person, f"{settings.company_name}: RevenueOS ROI statement", text)
    return len(to)
