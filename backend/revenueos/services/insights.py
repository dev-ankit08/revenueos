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
        from ..models import CampaignSpend
        from sqlalchemy import func as _f

        spend = float(db.scalar(select(_f.coalesce(_f.sum(CampaignSpend.amount), 0.0))
                                .where(CampaignSpend.campaign_id == camp.id)) or 0.0)
        meetings = sum(1 for c in contacts if c.id in member_ids and c.status == "meeting")
        won = sum(o.amount or 0 for o in new_opps if o.stage == "closed_won")
        pipeline = sum(o.amount or 0 for o in new_opps)
        by_campaign.append({
            "campaign_id": camp.id, "name": camp.name, "members": len(member_ids), **summarize(touches),
            "meetings": meetings, "opportunities": len(new_opps), "pipeline": pipeline, "won": won,
            "spend": round(spend, 2), "cost_per_meeting": round(spend / meetings, 2) if spend and meetings else None,
            "pipeline_per_dollar": round(pipeline / spend, 1) if spend else None,
            "roi": round((won - spend) / spend, 2) if spend else None,
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


def draft_quality(db: Session, days: int = 90) -> dict:
    """Reviewer trust in AI drafts over the last `days`, overall and per agent.

    approved_unedited_pct is the headline accuracy metric: drafts a person sent exactly as written.
    A/B holdout rejections are excluded (they are automatic, not a judgement on quality).
    """
    from .approvals import AB_HOLDOUT_NOTE, REJECT_REASONS

    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = db.scalars(select(Approval).where(
        Approval.decided_at.is_not(None), Approval.decided_at >= since,
        Approval.status.in_(["approved", "sent", "rejected", "failed"]),
    )).all()
    rows = [a for a in rows if not (a.status == "rejected" and a.review_note == AB_HOLDOUT_NOTE)]

    def summarise(items: list[Approval]) -> dict:
        approved = [a for a in items if a.status != "rejected"]
        rejected = [a for a in items if a.status == "rejected"]
        edits = [a.edit_pct for a in approved if a.edit_pct is not None]
        unedited = sum(1 for e in edits if e == 0)
        reasons: dict[str, int] = defaultdict(int)
        for a in rejected:
            reasons[a.reject_reason or "unspecified"] += 1
        return {
            "decided": len(items), "approved": len(approved), "rejected": len(rejected),
            "approved_unedited_pct": _rate(unedited, len(edits)),
            "avg_edit_pct": round(sum(edits) / len(edits), 1) if edits else None,
            "reject_rate": _rate(len(rejected), len(items)),
            "reject_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        }

    by_agent: dict[str, list[Approval]] = defaultdict(list)
    by_prompt: dict[str, list[Approval]] = defaultdict(list)
    for a in rows:
        by_agent[a.created_by_agent].append(a)
        if a.prompt_version:
            by_prompt[f"{a.created_by_agent}@{a.prompt_version}"].append(a)
    return {
        "days": days, "target_unedited_pct": 70, **summarise(rows),
        "by_agent": {agent: summarise(items) for agent, items in sorted(by_agent.items())},
        "by_prompt_version": {k: summarise(v) for k, v in sorted(by_prompt.items())},
        "grader_calibration": grader_calibration(rows),
        "reason_labels": {**REJECT_REASONS, "unspecified": "No reason given"},
    }


def grader_calibration(rows: list[Approval]) -> dict:
    """Does the personalisation score predict what reviewers do? If high-scoring drafts are not sent unedited
    more often than low-scoring ones, the grader (or the threshold) needs attention."""
    buckets = {"under_60": [], "60_79": [], "80_plus": []}
    for a in rows:
        if a.personalisation_score is None:
            continue
        key = "under_60" if a.personalisation_score < 60 else "60_79" if a.personalisation_score < 80 else "80_plus"
        buckets[key].append(a)
    out = {}
    for key, items in buckets.items():
        approved = [a for a in items if a.status != "rejected"]
        unedited = sum(1 for a in approved if a.edit_pct == 0)
        out[key] = {"drafts": len(items), "approved_unedited_pct": _rate(unedited, len(approved)),
                    "reject_rate": _rate(len(items) - len(approved), len(items))}
    lo, hi = out["under_60"]["approved_unedited_pct"], out["80_plus"]["approved_unedited_pct"]
    enough = buckets["under_60"] and buckets["80_plus"] and len(buckets["under_60"]) + len(buckets["80_plus"]) >= 20
    out["verdict"] = ("not enough graded drafts yet" if not enough else
                      "agrees with reviewers" if (hi or 0) > (lo or 0) else
                      "does not predict reviewer decisions: review the grader rubric or threshold")
    return out


def multi_touch_attribution(db: Session) -> dict:
    """Credit each deal's amount to the touches that came before it, under four models:
    first touch, last touch, linear (equal shares) and W-shaped (40% first, 40% the touch just before the deal
    was created, 20% spread over the rest). Touches: sent emails (by assistant or campaign), clicks, replies,
    meetings and inbound form fills, for every contact at the deal's account."""
    from ..models import CalendarEvent, Campaign, EmailEvent, Opportunity

    campaigns = {c.id: c.name for c in db.scalars(select(Campaign))}

    def label_for(a: Approval) -> str:
        if a.campaign_id:
            return f"campaign: {campaigns.get(a.campaign_id, a.campaign_id)}"
        return f"email: {a.created_by_agent}"

    touches_by_account: dict[int, list[tuple[datetime, str]]] = defaultdict(list)
    contacts = {c.id: c for c in db.scalars(select(Contact))}
    for a in db.scalars(select(Approval).where(Approval.status == "sent", Approval.sent_at.is_not(None))):
        if a.contact_id in contacts:
            touches_by_account[contacts[a.contact_id].account_id].append((_aware(a.sent_at), label_for(a)))
    for e in db.scalars(select(EmailEvent).where(EmailEvent.event == "click", EmailEvent.contact_id.is_not(None))):
        if e.contact_id in contacts:
            touches_by_account[contacts[e.contact_id].account_id].append((_aware(e.occurred_at), "click"))
    for act in db.scalars(select(Activity).where(Activity.type.like("reply_%"))):
        if act.contact_id in contacts:
            touches_by_account[contacts[act.contact_id].account_id].append((_aware(act.created_at), "reply"))
    for ev in db.scalars(select(CalendarEvent).where(CalendarEvent.account_id.is_not(None))):
        touches_by_account[ev.account_id].append((_aware(ev.starts_at), "meeting"))
    for c in contacts.values():
        if c.source == "inbound":
            touches_by_account[c.account_id].append((_aware(c.created_at), "inbound form"))

    models = {m: defaultdict(float) for m in ("first_touch", "last_touch", "linear", "w_shaped")}
    deals = covered = 0
    for o in db.scalars(select(Opportunity)):
        deals += 1
        amount = o.amount or 0
        created = _aware(o.created_at)
        touches = sorted(t for t in touches_by_account.get(o.account_id, []) if t[0] and t[0] <= created)
        if not touches or not amount:
            continue
        covered += 1
        labels = [label for _, label in touches]
        models["first_touch"][labels[0]] += amount
        models["last_touch"][labels[-1]] += amount
        for label in labels:
            models["linear"][label] += amount / len(labels)
        if len(labels) == 1:
            models["w_shaped"][labels[0]] += amount
        elif len(labels) == 2:
            models["w_shaped"][labels[0]] += amount / 2
            models["w_shaped"][labels[-1]] += amount / 2
        else:
            models["w_shaped"][labels[0]] += amount * 0.4
            models["w_shaped"][labels[-1]] += amount * 0.4
            for label in labels[1:-1]:
                models["w_shaped"][label] += amount * 0.2 / (len(labels) - 2)
    return {
        "deals": deals, "deals_with_touches": covered,
        "models": {m: dict(sorted(((k, round(v, 2)) for k, v in credit.items()), key=lambda kv: -kv[1]))
                   for m, credit in models.items()},
        "note": "Credit uses deal amounts. A source high in first touch opens deals; high in last touch closes the gap to a deal.",
    }