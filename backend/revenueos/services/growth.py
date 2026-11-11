"""Phase 3 growth: website visits and intent data as buying signals, events and webinars, customer health,
renewals and upsell.

Website visits: a small script on your site (GET /api/public/t.js) reports page views. A visitor who arrived from
one of our emails (utm_content=approval-<id>) is identified as that contact from then on. A known contact viewing
a high-intent page (INTENT_PAGES: pricing, demo, contact...) creates a buying signal and counts as engagement.

Intent data (Bombora, G2, 6sense... via webhook or CSV): topic surges per company become signals; strong ones on
unknown companies can create the account so the Lead Sourcer and Signal Detector can work it.

Events: registrations and attendance per event; after the event, attendees and no-shows go into different
nurture series, and engaged attendees become tasks for their owner.

Customers: contracts with renewal dates. Before a renewal is due (RENEWAL_LEAD_DAYS), a renewal deal and a task
are created. A health score (activity, sentiment, risks, bounces) flags churn risk as a signal; products the
customer doesn't have yet are listed as upsell ideas.
"""
import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (Account, Activity, Approval, Contact, Contract, EmailEvent, Event, EventRegistration,
                      Opportunity, Product, Signal, Task, WebVisit)

UTC = timezone.utc


def _aware(dt):
    return None if dt is None else (dt if dt.tzinfo else dt.replace(tzinfo=UTC))


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _signal_once(db: Session, account_id: int, type_: str, headline: str, **kw) -> Signal | None:
    """One signal per account, type and day (no floods from repeated page views or daily intent feeds)."""
    today = _today()
    exists = db.scalar(select(Signal.id).where(Signal.account_id == account_id, Signal.type == type_,
                                               Signal.event_date == today, Signal.headline == headline))
    if exists:
        return None
    s = Signal(account_id=account_id, type=type_, headline=headline, event_date=today, **kw)
    db.add(s)
    return s


# ------------------------------------------------------------------ website visits

def intent_pages() -> list[str]:
    return [p.strip().lower() for p in (settings.intent_pages or "").split(",") if p.strip()]


def _approval_from_url(url: str) -> int | None:
    q = parse_qs(urlsplit(url).query)
    m = re.match(r"approval-(\d+)$", (q.get("utm_content") or [""])[0])
    return int(m.group(1)) if m else None


def record_visit(db: Session, *, visitor_id: str, url: str, referrer: str | None = None,
                 ip: str | None = None) -> WebVisit:
    """Store a page view; identify the visitor from an email link, or from an earlier identified visit."""
    visitor_id = (visitor_id or "")[:64] or hashlib.sha256((ip or "anon").encode()).hexdigest()[:16]
    parts = urlsplit(url)
    q = parse_qs(parts.query)
    contact_id = None
    approval_id = _approval_from_url(url)
    if approval_id:
        a = db.get(Approval, approval_id)
        contact_id = a.contact_id if a else None
    if contact_id is None:
        contact_id = db.scalar(select(WebVisit.contact_id).where(WebVisit.visitor_id == visitor_id,
                                                                 WebVisit.contact_id.is_not(None)))
    contact = db.get(Contact, contact_id) if contact_id else None
    v = WebVisit(visitor_id=visitor_id, contact_id=contact_id, account_id=contact.account_id if contact else None,
                 url=url[:2000], path=(parts.path or "/")[:500], referrer=(referrer or "")[:1000] or None,
                 utm_source=(q.get("utm_source") or [None])[0], utm_campaign=(q.get("utm_campaign") or [None])[0],
                 approval_id=approval_id)
    db.add(v)
    if contact and any(parts.path.lower().startswith(p) for p in intent_pages()):
        db.add(Activity(contact_id=contact.id, type="web_visit_high", detail=f"Viewed {parts.path}"))
        _signal_once(db, contact.account_id, "website_visit", f"{contact.full_name} viewed {parts.path}",
                     detail=f"Arrived via {v.utm_source or 'direct'}; {url[:300]}", url=url[:1000], strength="high",
                     recommended_action=f"Follow up with {contact.full_name} today while interest is fresh.")
    return v


TRACKER_JS = """(function(){try{var k='rvos_vid',v=localStorage.getItem(k);if(!v){v=Math.random().toString(36).slice(2)+Date.now().toString(36);localStorage.setItem(k,v);}
var d=JSON.stringify({v:v,u:location.href,r:document.referrer});var e='__ENDPOINT__';
if(navigator.sendBeacon){navigator.sendBeacon(e,d);}else{fetch(e,{method:'POST',body:d,keepalive:true,mode:'no-cors'});}}catch(x){}})();"""


def tracker_script(endpoint: str) -> str:
    return TRACKER_JS.replace("__ENDPOINT__", endpoint)


# ------------------------------------------------------------------ intent data

def _domain(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().lower()
    v = urlsplit(v if "//" in v else f"//{v}").netloc or v
    return v.removeprefix("www.") or None


def record_intent(db: Session, *, provider: str, topic: str, score: float, domain: str | None = None,
                  company: str | None = None, detail: str | None = None, url: str | None = None) -> Signal | None:
    """A topic surge for a company (score 0-100). Medium+ becomes a signal; a strong one on an unknown company
    creates the account (source "intent") when INTENT_CREATE_ACCOUNTS is on."""
    d = _domain(domain)
    acc = None
    if d:
        acc = db.scalar(select(Account).where(Account.domain == d))
    if acc is None and company:
        acc = db.scalar(select(Account).where(Account.name.ilike(company.strip())))
    strength = "high" if score >= 70 else "medium" if score >= 40 else "low"
    if strength == "low":
        return None
    if acc is None:
        create = settings.intent_create_accounts
        create = create if isinstance(create, bool) else str(create).lower() in ("1", "true", "yes")
        if not (create and strength == "high" and (company or d)):
            return None
        acc = Account(name=company or d, domain=d, source="intent", fit_reason=f"{provider} intent: {topic} ({score:.0f})")
        db.add(acc)
        db.flush()
    return _signal_once(db, acc.id, "intent", f"Researching {topic} ({provider}, score {score:.0f})",
                        detail=detail, url=url, strength=strength,
                        recommended_action=f"Reach the buying committee at {acc.name} with a {topic} angle.")


def recent_signals(db: Session, account_id: int, days: int = 30) -> list[dict]:
    since = (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
    return [{"type": s.type, "headline": s.headline, "strength": s.strength, "date": s.event_date}
            for s in db.scalars(select(Signal).where(Signal.account_id == account_id, Signal.event_date >= since)
                                .order_by(Signal.id.desc()).limit(8))]


# ------------------------------------------------------------------ events

def register(db: Session, event: Event, contact: Contact, source: str = "registration") -> EventRegistration:
    reg = db.scalar(select(EventRegistration).where(EventRegistration.event_id == event.id,
                                                    EventRegistration.contact_id == contact.id))
    if reg:
        return reg
    reg = EventRegistration(event_id=event.id, contact_id=contact.id, source=source)
    db.add(reg)
    db.add(Activity(contact_id=contact.id, type="event_registered", detail=f"Registered for {event.name}"))
    if event.campaign_id:
        from ..models import CampaignMember

        if not db.scalar(select(CampaignMember.id).where(CampaignMember.campaign_id == event.campaign_id,
                                                         CampaignMember.contact_id == contact.id)):
            db.add(CampaignMember(campaign_id=event.campaign_id, contact_id=contact.id, status="engaged"))
    return reg


def mark_attendance(db: Session, event: Event, attended: dict[str, int | None]) -> dict:
    """attended: email -> minutes attended (None if unknown). Every other registrant becomes a no-show."""
    attended = {e.strip().lower(): m for e, m in attended.items()}
    counts = {"attended": 0, "no_show": 0}
    for reg in event.registrations:
        email = (reg.contact.email or "").lower()
        if email in attended:
            reg.status, reg.attended_minutes = "attended", attended[email]
            counts["attended"] += 1
        else:
            reg.status = "no_show"
            counts["no_show"] += 1
    event.status = "completed"
    return counts


def follow_up(db: Session, event: Event) -> dict:
    """After the event: nurture series for attendees and no-shows; a task for engaged attendees."""
    from . import nurture
    from ..models import NurtureFlow

    out = {"attended_enrolled": 0, "no_show_enrolled": 0, "tasks": 0}
    attended = [r for r in event.registrations if r.status == "attended"]
    no_show = [r for r in event.registrations if r.status == "no_show"]
    for regs, flow_id, key in ((attended, event.attended_flow_id, "attended_enrolled"),
                               (no_show, event.noshow_flow_id, "no_show_enrolled")):
        flow = db.get(NurtureFlow, flow_id) if flow_id else None
        if flow and regs:
            out[key] = nurture.enroll(db, flow, [r.contact_id for r in regs])["enrolled"]
    for r in attended:
        if r.followed_up:
            continue
        db.add(Activity(contact_id=r.contact_id, type="event_attended",
                        detail=f"Attended {event.name}" + (f" ({r.attended_minutes} min)" if r.attended_minutes else "")
                               + (f". Asked: {r.questions}" if r.questions else "")))
        engaged = bool(r.questions) or (r.attended_minutes or 0) >= int(settings.event_engaged_minutes)
        if engaged:
            c = r.contact
            db.add(Task(type="follow_up", title=f"Follow up with {c.full_name} after {event.name}"[:255],
                        contact_id=c.id, owner=c.owner, created_by="events",
                        due_at=datetime.now(UTC) + timedelta(days=1),
                        notes=(f"Asked: {r.questions}\n" if r.questions else "")
                              + (f"Attended {r.attended_minutes} minutes." if r.attended_minutes else "")))
            out["tasks"] += 1
        r.followed_up = True
    return out


# ------------------------------------------------------------------ customers: renewals, health, upsell

def create_due_renewals(db: Session, today: date | None = None) -> int:
    """For contracts ending within RENEWAL_LEAD_DAYS, open a renewal deal (once) and a task for its owner."""
    today = today or datetime.now(UTC).date()
    horizon = (today + timedelta(days=int(settings.renewal_lead_days))).isoformat()
    n = 0
    for k in db.scalars(select(Contract).where(Contract.status == "active", Contract.end_date <= horizon,
                                               Contract.renewal_opportunity_id.is_(None))):
        acc = k.account
        owner = db.scalar(select(Contact.owner).where(Contact.account_id == acc.id, Contact.owner.is_not(None)))
        opp = Opportunity(account_id=acc.id, name=f"Renewal: {k.name}", stage="qualification", amount=k.amount,
                          close_date=k.end_date, owner=owner, deal_type="renewal")
        db.add(opp)
        db.flush()
        k.renewal_opportunity_id = opp.id
        days = (date.fromisoformat(k.end_date) - today).days
        db.add(Task(type="follow_up", title=f"Start the renewal conversation with {acc.name}"[:255], owner=owner,
                    opportunity_id=opp.id, created_by="renewals", due_at=datetime.now(UTC) + timedelta(days=1),
                    notes=f"{k.name} ends on {k.end_date} ({days} days). Value {k.amount or 0:,.0f} {k.currency}."))
        n += 1
    return n


NEGATIVE = ("reply_objection", "reply_not_now", "reply_unsubscribe", "email_complaint", "email_bounce")


def health(db: Session, account: Account, today: date | None = None) -> dict:
    """0-100 (higher is healthier) with the reasons, from activity, sentiment, deal risks, renewal and bounces."""
    today = today or datetime.now(UTC).date()
    now = datetime.now(UTC)
    contact_ids = [c.id for c in account.contacts]
    reasons, score = [], 100
    last = db.scalar(select(Activity.created_at).where(Activity.contact_id.in_(contact_ids))
                     .order_by(Activity.created_at.desc())) if contact_ids else None
    idle = (now - _aware(last)).days if last else None
    if idle is None or idle > 60:
        score -= 30
        reasons.append("no activity in 60+ days" if idle else "no recorded activity")
    elif idle > 30:
        score -= 15
        reasons.append(f"quiet for {idle} days")
    since = now - timedelta(days=90)
    negatives = db.scalars(select(Activity.type).where(Activity.contact_id.in_(contact_ids), Activity.type.in_(NEGATIVE),
                                                       Activity.created_at >= since)).all() if contact_ids else []
    if negatives:
        score -= min(30, 10 * len(negatives))
        reasons.append(f"{len(negatives)} negative signals in 90 days")
    risky = [o for o in db.scalars(select(Opportunity).where(Opportunity.account_id == account.id,
                                                             Opportunity.stage.notin_(["closed_won", "closed_lost"])))
             if o.health == "red"]
    if risky:
        score -= 15
        reasons.append(f"{len(risky)} deal(s) in red health")
    contracts = list(db.scalars(select(Contract).where(Contract.account_id == account.id, Contract.status == "active")))
    soon = [k for k in contracts if k.end_date and (date.fromisoformat(k.end_date) - today).days <= 60]
    if soon and (idle is None or idle > 21):
        score -= 15
        reasons.append("renewal within 60 days and little recent contact")
    if contact_ids and db.scalar(select(EmailEvent.id).where(EmailEvent.contact_id.in_(contact_ids),
                                                             EmailEvent.event == "bounce")):
        score -= 5
        reasons.append("an address bounced (contact may have left)")
    score = max(0, score)
    risk = "high" if score < 50 else "medium" if score < 75 else "low"
    return {"score": score, "churn_risk": risk, "reasons": reasons or ["engaged and on track"]}


def upsell_ideas(db: Session, account: Account) -> list[dict]:
    owned = set()
    for k in db.scalars(select(Contract).where(Contract.account_id == account.id, Contract.status == "active")):
        owned |= {p.lower() for p in (k.products or [])}
    return [{"product": p.name, "unit_price": p.unit_price, "billing": p.billing}
            for p in db.scalars(select(Product).where(Product.active.is_(True))) if p.name.lower() not in owned]


def refresh_health(db: Session) -> int:
    """Daily: a churn-risk signal for every customer whose health turned high-risk."""
    n = 0
    accounts = {k.account_id for k in db.scalars(select(Contract).where(Contract.status == "active"))}
    for acc_id in accounts:
        acc = db.get(Account, acc_id)
        h = health(db, acc)
        if h["churn_risk"] == "high" and _signal_once(
                db, acc.id, "churn_risk", f"Churn risk at {acc.name} (health {h['score']})",
                detail="; ".join(h["reasons"]), strength="high",
                recommended_action="Book a check-in with the sponsor and review open issues before the renewal."):
            n += 1
    return n


def contract_from_won_deal(db: Session, opp: Opportunity, months: int = 12) -> Contract | None:
    """A won deal with recurring line items becomes a contract (start today, MONTHS long)."""
    from .salesmgmt import lines_for

    lines = lines_for(db, opp.id)
    recurring = [line for line in lines if line.get("billing") in ("monthly", "annual")]
    if not recurring or db.scalar(select(Contract.id).where(Contract.opportunity_id == opp.id)):
        return None
    start = datetime.now(UTC).date()
    end = date(start.year + (start.month - 1 + months) // 12, (start.month - 1 + months) % 12 + 1, 1) - timedelta(days=1)
    k = Contract(account_id=opp.account_id, opportunity_id=opp.id, name=opp.name, amount=opp.amount,
                 start_date=start.isoformat(), end_date=end.isoformat(),
                 products=sorted({line["product"] for line in recurring if line.get("product")}))
    db.add(k)
    return k
