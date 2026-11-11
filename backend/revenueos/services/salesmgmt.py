"""Sales management: lead routing by territory, quotas and attainment per rep, win/loss analysis,
and products / line items that make deal amounts and proposals use real prices."""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Contact, Opportunity, OpportunityLine, Quota, Territory

CLOSE_REASONS = {
    "won": ["price_value", "product_fit", "relationship", "speed_to_value", "proof_case_study", "partner_referral", "other"],
    "lost": ["price", "lost_to_competitor", "no_decision", "timing", "no_budget", "product_gap", "champion_left",
             "went_in_house", "other"],
}


# ------------------------------------------------------------------ routing

def _matches(t: Territory, contact: Contact) -> bool:
    acc = contact.account
    if t.countries and (acc.country or "").strip().lower() not in {c.strip().lower() for c in t.countries}:
        return False
    if t.industries and not any(i.strip().lower() in (acc.industry or "").lower() for i in t.industries):
        return False
    if t.min_employees is not None and (acc.employees is None or acc.employees < t.min_employees):
        return False
    if t.max_employees is not None and (acc.employees is None or acc.employees > t.max_employees):
        return False
    return True


def territory_for(db: Session, contact: Contact) -> Territory | None:
    for t in db.scalars(select(Territory).where(Territory.active.is_(True)).order_by(Territory.priority, Territory.id)):
        if t.members and _matches(t, contact):
            return t
    return None


def assign_owner(db: Session, contact: Contact, force: bool = False) -> str | None:
    """Give an unowned contact an owner: the account's existing owner (one rep per account), else the next
    member of the matching territory in turn. Returns the owner (unchanged when none applies)."""
    if contact.owner and not force:
        return contact.owner
    colleague = db.scalar(select(Contact.owner).where(Contact.account_id == contact.account_id,
                                                      Contact.id != contact.id, Contact.owner.is_not(None)))
    if colleague and not force:
        contact.owner = colleague
        return colleague
    t = territory_for(db, contact)
    if not t:
        return contact.owner
    members = list(t.members)
    contact.owner = members[t.next_index % len(members)]
    t.next_index = (t.next_index + 1) % len(members)
    return contact.owner


def route_unowned(db: Session) -> int:
    n = 0
    for c in db.scalars(select(Contact).where(Contact.owner.is_(None))):
        if assign_owner(db, c):
            n += 1
    return n


# ------------------------------------------------------------------ quotas

def current_period(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{now.year}-Q{(now.month - 1) // 3 + 1}"


def _period_bounds(period: str) -> tuple[datetime, datetime]:
    year, q = int(period[:4]), int(period[-1])
    start = datetime(year, 3 * (q - 1) + 1, 1, tzinfo=timezone.utc)
    end = datetime(year + (q == 4), (3 * q) % 12 + 1, 1, tzinfo=timezone.utc)
    return start, end


def _aware(dt):
    return None if dt is None else (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))


def rep_performance(db: Session, period: str | None = None) -> list[dict]:
    """Per rep: quota, won this period, attainment, open pipeline closing this period, and coverage."""
    period = period or current_period()
    start, end = _period_bounds(period)
    quotas = {q.owner: q.amount for q in db.scalars(select(Quota).where(Quota.period == period))}
    reps: dict[str, dict] = {}
    for o in db.scalars(select(Opportunity).where(Opportunity.owner.is_not(None))):
        r = reps.setdefault(o.owner, {"won": 0.0, "won_deals": 0, "pipeline": 0.0, "open_deals": 0, "lost_deals": 0})
        closed = _aware(o.closed_at)
        if o.stage == "closed_won" and closed and start <= closed < end:
            r["won"] += o.amount or 0
            r["won_deals"] += 1
        elif o.stage == "closed_lost" and closed and start <= closed < end:
            r["lost_deals"] += 1
        elif o.stage not in ("closed_won", "closed_lost") and o.close_date and start.date().isoformat() <= o.close_date < end.date().isoformat():
            r["pipeline"] += o.amount or 0
            r["open_deals"] += 1
    out = []
    for owner in sorted(set(reps) | set(quotas)):
        r = reps.get(owner, {"won": 0.0, "won_deals": 0, "pipeline": 0.0, "open_deals": 0, "lost_deals": 0})
        quota = quotas.get(owner)
        gap = (quota - r["won"]) if quota else None
        out.append({"owner": owner, "period": period, "quota": quota, **r,
                    "attainment_pct": round(r["won"] / quota * 100, 1) if quota else None,
                    "coverage": round(r["pipeline"] / gap, 2) if gap and gap > 0 else None,
                    "win_rate_pct": round(r["won_deals"] / (r["won_deals"] + r["lost_deals"]) * 100, 1)
                    if r["won_deals"] + r["lost_deals"] else None})
    return sorted(out, key=lambda x: -(x["attainment_pct"] or 0))


# ------------------------------------------------------------------ win / loss

def win_loss(db: Session) -> dict:
    won = list(db.scalars(select(Opportunity).where(Opportunity.stage == "closed_won")))
    lost = list(db.scalars(select(Opportunity).where(Opportunity.stage == "closed_lost")))

    def count(items, key):
        out: dict[str, int] = {}
        for o in items:
            k = getattr(o, key) or "unspecified"
            out[k] = out.get(k, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    competitors = sorted({c for o in won + lost for c in (o.competitors or [])})
    return {
        "won": len(won), "lost": len(lost),
        "win_rate_pct": round(len(won) / (len(won) + len(lost)) * 100, 1) if won or lost else None,
        "won_reasons": count(won, "close_reason"), "lost_reasons": count(lost, "close_reason"),
        "by_competitor": {c: record_against(db, c) for c in competitors},
        "labels": CLOSE_REASONS,
    }


def record_against(db: Session, competitor: str) -> dict:
    name = competitor.strip().lower()
    won = lost = 0
    reasons: dict[str, int] = {}
    for o in db.scalars(select(Opportunity).where(Opportunity.stage.in_(["closed_won", "closed_lost"]))):
        if name not in {c.strip().lower() for c in o.competitors or []}:
            continue
        if o.stage == "closed_won":
            won += 1
        else:
            lost += 1
            reasons[o.close_reason or "unspecified"] = reasons.get(o.close_reason or "unspecified", 0) + 1
    return {"won": won, "lost": lost, "win_rate_pct": round(won / (won + lost) * 100, 1) if won + lost else None,
            "why_we_lose": reasons}


# ------------------------------------------------------------------ products and line items

def lines_for(db: Session, opp_id: int) -> list[dict]:
    return [{"id": line.id, "product_id": line.product_id,
             "product": line.product.name if line.product else None, "description": line.description,
             "quantity": line.quantity, "unit_price": line.unit_price, "discount_pct": line.discount_pct,
             "total": line.total, "billing": line.product.billing if line.product else None}
            for line in db.scalars(select(OpportunityLine).where(OpportunityLine.opportunity_id == opp_id)
                                   .order_by(OpportunityLine.id))]


def recompute_amount(db: Session, opp: Opportunity) -> float | None:
    """With line items, the deal amount is their sum (so forecasts and proposals agree)."""
    lines = list(db.scalars(select(OpportunityLine).where(OpportunityLine.opportunity_id == opp.id)))
    if lines:
        opp.amount = round(sum(line.total for line in lines), 2)
    return opp.amount
