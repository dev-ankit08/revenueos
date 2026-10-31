"""Deterministic revenue analytics: forecast math, last-touch attribution, A/B statistics.

Agents (Forecast Analyst, Learning) interpret these numbers; they never compute them.
"""
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Activity, Approval, Campaign, CampaignMember, Contact, Opportunity

OPEN_STAGES = ("discovery", "qualification", "solution", "proposal", "negotiation")
# Default win probability by stage when the Deal Strategist hasn't scored a deal yet.
STAGE_PROBABILITY = {"discovery": 10, "qualification": 20, "solution": 40, "proposal": 60, "negotiation": 80}
POSITIVE_REPLIES = {"reply_interested", "reply_question", "reply_referral"}
NEGATIVE_REPLIES = {"reply_unsubscribe", "reply_wrong_person"}


def _aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def quarter_bounds(now: datetime) -> tuple[datetime, datetime]:
    q_start_month = 3 * ((now.month - 1) // 3) + 1
    start = now.replace(month=q_start_month, day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start.replace(year=start.year + 1, month=1) if q_start_month == 10
           else start.replace(month=q_start_month + 3))
    return start, end


def deal_probability(o: Opportunity) -> float:
    return o.win_probability if o.win_probability is not None else STAGE_PROBABILITY.get(o.stage, 0)


def deal_category(o: Opportunity) -> str:
    if o.forecast_category:
        return o.forecast_category
    p = deal_probability(o)
    return "commit" if p >= 75 else "best_case" if p >= 40 else "pipeline"


# --------------------------------------------------------------------------- forecast

def forecast_numbers(db: Session, target: float | None = None) -> dict:
    now = datetime.now(timezone.utc)
    q_start, q_end = quarter_bounds(now)
    today = now.date().isoformat()
    q_end_iso = (q_end - timedelta(days=1)).date().isoformat()

    opps = list(db.scalars(select(Opportunity)))
    open_opps = [o for o in opps if o.stage in OPEN_STAGES]
    won = [o for o in opps if o.stage == "closed_won"]
    lost = [o for o in opps if o.stage == "closed_lost"]
    won_this_q = sum(o.amount or 0 for o in won if _aware(o.closed_at) and q_start <= _aware(o.closed_at) < q_end)

    deals, totals = [], defaultdict(float)
    for o in open_opps:
        p, cat, amt = deal_probability(o), deal_category(o), o.amount or 0
        in_quarter = bool(o.close_date) and o.close_date <= q_end_iso
        if in_quarter:
            totals[cat] += amt
            totals["weighted"] += amt * p / 100
        totals["open_pipeline"] += amt
        deals.append({
            "opportunity_id": o.id, "name": o.name, "account": o.account.name, "stage": o.stage, "amount": o.amount,
            "close_date": o.close_date, "probability": p, "category": cat, "health": o.health,
            "closes_this_quarter": in_quarter, "slipped": bool(o.close_date and o.close_date < today),
            "days_since_activity": (now - _aware(o.last_activity_at)).days, "risks": (o.risks or [])[:3],
        })

    commit = totals["commit"]
    best_case = commit + totals["best_case"]
    remaining = (target - won_this_q) if target else None
    qtr_pipeline = sum(d["amount"] or 0 for d in deals if d["closes_this_quarter"])
    decided = len(won) + len(lost)
    return {
        "quarter": f"{q_start:%Y}-Q{(q_start.month - 1) // 3 + 1}",
        "target": target,
        "closed_won_this_quarter": won_this_q,
        "commit": commit,
        "best_case": best_case,
        "weighted": round(totals["weighted"], 2),
        "quarter_pipeline": qtr_pipeline,
        "open_pipeline": totals["open_pipeline"],
        "projected_attainment": round((won_this_q + totals["weighted"]) / target * 100, 1) if target else None,
        "coverage_ratio": round(qtr_pipeline / remaining, 2) if remaining and remaining > 0 else None,
        "historical_win_rate": round(len(won) / decided * 100, 1) if decided else None,
        "deals_missing_amount": sum(1 for d in deals if not d["amount"]),
        "deals_missing_close_date": sum(1 for d in deals if not d["close_date"]),
        "slipped_deals": [d["name"] for d in deals if d["slipped"]],
        "avg_days_in_stage": _avg_days_in_stage(open_opps + won + lost),
        "deals": sorted(deals, key=lambda d: -(d["amount"] or 0)),
    }


def _avg_days_in_stage(opps: list[Opportunity]) -> dict:
    spans = defaultdict(list)
    for o in opps:
        changes = o.stage_changes
        for cur, nxt in zip(changes, changes[1:]):
            spans[cur.to_stage].append((_aware(nxt.changed_at) - _aware(cur.changed_at)).days)
    return {stage: round(sum(v) / len(v), 1) for stage, v in spans.items() if v}


# --------------------------------------------------------------------------- attribution

def credited_replies(db: Session) -> dict[int, str]:
    """Last-touch: each reply is credited to the latest email sent to that contact before it.

    Returns {approval_id: reply_activity_type}.
    """
    sent_by_contact = defaultdict(list)
    for a in db.scalars(select(Approval).where(Approval.status == "sent", Approval.contact_id.isnot(None))):
        sent_by_contact[a.contact_id].append(a)
    credited: dict[int, str] = {}
    replies = db.scalars(select(Activity).where(Activity.type.like("reply%")).order_by(Activity.created_at))
    for r in replies:
        prior = [a for a in sent_by_contact.get(r.contact_id, [])
                 if _aware(a.sent_at) and _aware(a.sent_at) <= _aware(r.created_at)]
        if prior:
            last = max(prior, key=lambda a: _aware(a.sent_at))
            # A classified reply (reply_interested...) overrides the bare "reply" marker.
            if last.id not in credited or credited[last.id] == "reply":
                credited[last.id] = r.type
    return credited


def _rate(n: int, d: int) -> float | None:
    return round(n / d * 100, 1) if d else None


def attribution(db: Session) -> dict:
    credited = credited_replies(db)
    sent = list(db.scalars(select(Approval).where(Approval.status == "sent")))
    opps = list(db.scalars(select(Opportunity)))
    opp_by_account = defaultdict(list)
    for o in opps:
        opp_by_account[o.account_id].append(o)

    def summarize(rows: list[Approval]) -> dict:
        positive = sum(1 for a in rows if credited.get(a.id) in POSITIVE_REPLIES)
        replied = sum(1 for a in rows if a.id in credited)
        return {"sent": len(rows), "replies": replied, "positive_replies": positive,
                "reply_rate": _rate(replied, len(rows)), "positive_rate": _rate(positive, len(rows))}

    by_agent, by_step = defaultdict(list), defaultdict(list)
    for a in sent:
        by_agent[a.created_by_agent].append(a)
        by_step[a.sequence_step].append(a)

    by_source = []
    contacts = list(db.scalars(select(Contact)))
    for source in sorted({c.source for c in contacts}):
        group = [c for c in contacts if c.source == source]
        accounts = {c.account_id for c in group}
        group_opps = [o for acc in accounts for o in opp_by_account[acc]]
        by_source.append({
            "source": source, "contacts": len(group),
            "meetings": sum(1 for c in group if c.status == "meeting"),
            "opportunities": len(group_opps),
            "pipeline": sum(o.amount or 0 for o in group_opps if o.stage in OPEN_STAGES),
            "won": sum(o.amount or 0 for o in group_opps if o.stage == "closed_won"),
        })

    by_campaign = []
    for camp in db.scalars(select(Campaign)):
        member_ids = set(db.scalars(select(CampaignMember.contact_id).where(CampaignMember.campaign_id == camp.id)))
        started = _aware(camp.created_at)
        touches = [a for a in sent if a.contact_id in member_ids and _aware(a.sent_at) and _aware(a.sent_at) >= started]
        accounts = {c.account_id for c in contacts if c.id in member_ids}
        new_opps = [o for acc in accounts for o in opp_by_account[acc] if _aware(o.created_at) >= started]
        by_campaign.append({
            "campaign_id": camp.id, "name": camp.name, "members": len(member_ids), **summarize(touches),
            "meetings": sum(1 for c in contacts if c.id in member_ids and c.status == "meeting"),
            "opportunities": len(new_opps), "pipeline": sum(o.amount or 0 for o in new_opps),
            "won": sum(o.amount or 0 for o in new_opps if o.stage == "closed_won"),
        })

    return {
        "model": "last-touch (a reply is credited to the latest email sent before it)",
        "overall": summarize(sent),
        "by_agent": [{"agent": k, **summarize(v)} for k, v in sorted(by_agent.items())],
        "by_sequence_step": [{"step": k, **summarize(v)} for k, v in sorted(by_step.items())],
        "by_source": by_source,
        "by_campaign": by_campaign,
    }


# --------------------------------------------------------------------------- A/B tests

def two_proportion_p_value(x1: int, n1: int, x2: int, n2: int) -> float | None:
    """Two-sided z-test for a difference in proportions."""
    if not n1 or not n2:
        return None
    p = (x1 + x2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 1.0
    z = abs(x1 / n1 - x2 / n2) / se
    return math.erfc(z / math.sqrt(2))


MIN_SAMPLE_PER_VARIANT = 30


def ab_results(db: Session) -> list[dict]:
    """Experiments = (agent, step) groups where both variants were sent. Outcome = positive reply."""
    credited = credited_replies(db)
    groups = defaultdict(lambda: {"A": [], "B": []})
    for a in db.scalars(select(Approval).where(Approval.status == "sent", Approval.kind == "email")):
        if a.variant in ("A", "B"):
            groups[(a.created_by_agent, a.sequence_step)][a.variant].append(a)

    out = []
    for (agent, step), v in sorted(groups.items()):
        if not v["A"] or not v["B"]:
            continue
        stats = {}
        for name, rows in v.items():
            wins = sum(1 for a in rows if credited.get(a.id) in POSITIVE_REPLIES)
            stats[name] = {"sent": len(rows), "positive_replies": wins, "rate": _rate(wins, len(rows)),
                           "example_subject": rows[-1].subject}
        a, b = stats["A"], stats["B"]
        p = two_proportion_p_value(a["positive_replies"], a["sent"], b["positive_replies"], b["sent"])
        enough = min(a["sent"], b["sent"]) >= MIN_SAMPLE_PER_VARIANT
        winner = None
        if enough and p is not None and p < 0.05:
            winner = "A" if (a["rate"] or 0) > (b["rate"] or 0) else "B"
        verdict = (f"Variant {winner} wins (p={p:.3f})" if winner else
                   f"Not enough data yet (need {MIN_SAMPLE_PER_VARIANT}+ sends per variant)" if not enough else
                   f"No significant difference (p={p:.2f})")
        out.append({"agent": agent, "step": step, "A": a, "B": b, "p_value": p, "winner": winner, "verdict": verdict})
    return out


# --------------------------------------------------------------------------- holdout lift

MIN_SAMPLE_PER_GROUP = 30


def lift_report(db: Session) -> dict:
    """Treatment (AI outreach) vs randomly held-out contacts: meeting rate, deals and pipeline."""
    contacts = list(db.scalars(select(Contact).where(Contact.experiment_group.in_(["treatment", "holdout"]))))
    opps_by_account = defaultdict(list)
    for o in db.scalars(select(Opportunity)):
        opps_by_account[o.account_id].append(o)
    replied_ids = set(db.scalars(select(Activity.contact_id).where(Activity.type.like("reply%"))))

    groups = {}
    for g in ("treatment", "holdout"):
        rows = [c for c in contacts if c.experiment_group == g]
        meetings = [c for c in rows if c.status == "meeting" or any(
            o.primary_contact_id == c.id for o in opps_by_account[c.account_id])]
        opps = {o.id: o for c in rows for o in opps_by_account[c.account_id]
                if c.experiment_assigned_at is None or _aware(o.created_at) >= _aware(c.experiment_assigned_at)}
        groups[g] = {
            "contacts": len(rows), "meetings": len(meetings), "meeting_rate": _rate(len(meetings), len(rows)),
            "replies": sum(1 for c in rows if c.id in replied_ids), "opportunities": len(opps),
            "pipeline": sum(o.amount or 0 for o in opps.values() if o.stage in OPEN_STAGES),
            "won": sum(o.amount or 0 for o in opps.values() if o.stage == "closed_won"),
        }
    t, h = groups["treatment"], groups["holdout"]
    p = two_proportion_p_value(t["meetings"], t["contacts"], h["meetings"], h["contacts"])
    enough = min(t["contacts"], h["contacts"]) >= MIN_SAMPLE_PER_GROUP
    lift = (round((t["meeting_rate"] or 0) - (h["meeting_rate"] or 0), 1)
            if t["contacts"] and h["contacts"] else None)
    if not enough:
        verdict = f"Collecting data: need {MIN_SAMPLE_PER_GROUP}+ contacts in each group."
    elif p is not None and p < 0.05:
        verdict = f"AI outreach changes the meeting rate by {lift:+} points (p={p:.3f}): a real effect."
    else:
        verdict = f"No significant difference yet (p={p:.2f}); keep collecting."
    return {"treatment": t, "holdout": h, "meeting_rate_lift_points": lift, "p_value": p, "verdict": verdict}


# --------------------------------------------------------------------------- ROI ledger and personalisation

def personalisation_vs_replies(db: Session) -> list[dict]:
    """Do more personal emails earn more positive replies? Sent emails bucketed by their score."""
    credited = credited_replies(db)
    buckets = {"under 60": [], "60-79": [], "80+": []}
    for a in db.scalars(select(Approval).where(Approval.status == "sent", Approval.kind == "email",
                                               Approval.personalisation_score.isnot(None))):
        key = "80+" if a.personalisation_score >= 80 else "60-79" if a.personalisation_score >= 60 else "under 60"
        buckets[key].append(a)
    out = []
    for name, rows in buckets.items():
        positive = sum(1 for a in rows if credited.get(a.id) in POSITIVE_REPLIES)
        out.append({"score": name, "sent": len(rows), "positive_replies": positive, "positive_rate": _rate(positive, len(rows))})
    return out


def roi_ledger(db: Session) -> dict:
    """What the AI cost vs what it influenced. A deal counts as AI-influenced when RevenueOS emailed
    someone at the account before the deal was created."""
    from sqlalchemy import func as sqlfunc

    from ..models import AgentRun

    spend = float(db.scalar(select(sqlfunc.coalesce(sqlfunc.sum(AgentRun.cost_usd), 0.0))) or 0.0)
    first_touch: dict[int, datetime] = {}
    for a in db.scalars(select(Approval).where(Approval.status == "sent", Approval.contact_id.isnot(None))):
        acc = a.contact.account_id
        when = _aware(a.sent_at)
        if when and (acc not in first_touch or when < first_touch[acc]):
            first_touch[acc] = when
    influenced = [o for o in db.scalars(select(Opportunity))
                  if o.account_id in first_touch and _aware(o.created_at) >= first_touch[o.account_id]]
    meetings = db.scalar(select(sqlfunc.count()).select_from(Contact).where(Contact.status == "meeting")) or 0
    sent = db.scalar(select(sqlfunc.count()).select_from(Approval).where(Approval.status == "sent")) or 0
    pipeline = sum(o.amount or 0 for o in influenced if o.stage in OPEN_STAGES)
    won = sum(o.amount or 0 for o in influenced if o.stage == "closed_won")
    return {
        "ai_spend_usd": round(spend, 2), "emails_sent": sent, "meetings": meetings,
        "influenced_opportunities": len(influenced), "influenced_pipeline": pipeline, "influenced_won": won,
        "cost_per_meeting": round(spend / meetings, 2) if meetings else None,
        "cost_per_opportunity": round(spend / len(influenced), 2) if influenced else None,
        "won_per_dollar_of_ai": round(won / spend, 1) if spend and won else None,
        "definition": "AI-influenced = deal created after RevenueOS first emailed someone at that account.",
    }
