"""AI governance: monthly budgets (total and per agent), per-agent model choice, agents on/off.

Checked by agents/base.run_agent before every model call, so spend can't run past a budget.
"""
import contextvars
import logging
import threading
from datetime import datetime, timezone

from sqlalchemy import func, select

from ..db import session_scope
from ..models import AgentRun, AgentSetting

log = logging.getLogger(__name__)
TOTAL = "*"
ALERT_AT = 0.8

MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1")
# Agents that use web search/fetch: those server tools need Opus/Sonnet-class models.
WEB_AGENTS = {"account_researcher", "meeting_prep", "content_studio", "competitor_intel", "signal_detector",
              "lead_sourcer", "person_researcher"}
AGENTS = {
    "orchestrator": "Copilot (plans and delegates)", "lead_scorer": "Lead Scorer", "account_researcher": "Account Researcher",
    "person_researcher": "Person Researcher", "outreach_writer": "Outreach Writer", "personalisation_grader": "Personalisation grader",
    "reply_triage": "Reply Triage", "reply_writer": "Reply drafts", "meeting_prep": "Meeting Prep", "call_analyst": "Call Analyst",
    "proposal_builder": "Proposal Builder", "deal_strategist": "Deal Strategist", "forecast_analyst": "Forecast Analyst",
    "learning": "Learning loop", "committee_planner": "Committee Planner", "campaign_strategist": "Campaign Strategist",
    "content_studio": "Content Studio", "abm": "ABM", "competitor_intel": "Competitor Intel", "signal_detector": "Signal Detector",
    "lead_sourcer": "Lead Sourcer", "crm_hygiene": "CRM Hygiene", "context_summariser": "Slack summariser",
}


class BudgetExceeded(RuntimeError):
    pass


class AgentDisabled(RuntimeError):
    pass


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def month_spend(db, agent: str | None = None) -> float:
    stmt = select(func.coalesce(func.sum(AgentRun.cost_usd), 0.0)).where(AgentRun.started_at >= month_start())
    if agent and agent != TOTAL:
        stmt = stmt.where(AgentRun.agent == agent)
    return float(db.scalar(stmt) or 0.0)


def authorize(agent: str, default_model: str) -> str:
    """Raise if this agent may not run now; otherwise return the model to use."""
    with session_scope() as db:
        rows = {s.agent: s for s in db.scalars(select(AgentSetting).where(AgentSetting.agent.in_([agent, TOTAL])))}
        own, total = rows.get(agent), rows.get(TOTAL)
        if own and not own.enabled:
            raise AgentDisabled(f"The {AGENTS.get(agent, agent)} agent is switched off in AI governance.")
        for setting, scope in ((total, None), (own, agent)):
            if setting and setting.monthly_budget_usd is not None:
                spent = month_spend(db, scope)
                if spent >= setting.monthly_budget_usd:
                    who = "AI" if scope is None else AGENTS.get(agent, agent)
                    raise BudgetExceeded(f"Monthly {who} budget of ${setting.monthly_budget_usd:,.2f} reached "
                                         f"(${spent:,.2f} spent). Raise it under AI governance to continue.")
                _maybe_alert(setting, spent)
        return own.model if own and own.model else default_model


def _maybe_alert(setting: AgentSetting, spent: float) -> None:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    if spent < ALERT_AT * setting.monthly_budget_usd or setting.alerted_month == month:
        return
    setting.alerted_month = month
    who = "Total AI" if setting.agent == TOTAL else AGENTS.get(setting.agent, setting.agent)
    text = f"⚠️ {who} spend is at ${spent:,.2f} of its ${setting.monthly_budget_usd:,.2f} monthly budget."
    log.warning(text)
    from ..integrations import slack_bot

    if slack_bot.enabled():
        from ..config import settings

        ctx = contextvars.copy_context()
        threading.Thread(target=ctx.run, args=(_post, slack_bot, settings.slack_approvals_channel, text),
                         daemon=True).start()


def _post(slack_bot, channel: str, text: str) -> None:
    try:
        slack_bot.slack_call("chat.postMessage", {"channel": channel, "text": text})
    except Exception as exc:
        log.warning("budget alert to Slack failed: %s", exc)


def overview() -> dict:
    with session_scope() as db:
        rows = {s.agent: s for s in db.scalars(select(AgentSetting))}
        spend = dict(db.execute(select(AgentRun.agent, func.sum(AgentRun.cost_usd))
                                .where(AgentRun.started_at >= month_start()).group_by(AgentRun.agent)).all())
        total = rows.get(TOTAL)
        return {
            "month": datetime.now(timezone.utc).strftime("%Y-%m"),
            "models": MODELS,
            "total": {"budget": total.monthly_budget_usd if total else None,
                      "spent": round(sum(v or 0 for k, v in spend.items() if not k.startswith("job:")), 4)},
            "agents": [{"agent": a, "label": label, "model": rows[a].model if a in rows else None,
                        "budget": rows[a].monthly_budget_usd if a in rows else None,
                        "enabled": rows[a].enabled if a in rows else True,
                        "spent": round(spend.get(a) or 0, 4), "uses_web": a in WEB_AGENTS}
                       for a, label in AGENTS.items()],
        }


def update(agent: str, *, model: str | None, monthly_budget_usd: float | None, enabled: bool = True) -> None:
    if agent != TOTAL and agent not in AGENTS:
        raise ValueError(f"Unknown agent {agent!r}")
    if model and model not in MODELS:
        raise ValueError(f"Model must be one of {MODELS}")
    if model == "claude-haiku-5-5" and agent in WEB_AGENTS:
        raise ValueError("This agent searches the web, which needs an Opus or Sonnet model.")
    if monthly_budget_usd is not None and monthly_budget_usd < 0:
        raise ValueError("Budget can't be negative.")
    with session_scope() as db:
        s = db.scalar(select(AgentSetting).where(AgentSetting.agent == agent)) or AgentSetting(agent=agent)
        s.model, s.monthly_budget_usd, s.enabled = model, monthly_budget_usd, enabled
        s.updated_at = datetime.now(timezone.utc)
        db.add(s)
