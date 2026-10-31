"""CRM hygiene checks. Pure rules (no AI cost), so they can run daily."""
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Account, Activity, Approval, Contact, Opportunity, SalesDocument

STALE_CONTACT_DAYS = 14
STALE_DEAL_DAYS = 14
_SUFFIX = re.compile(r"\b(inc|ltd|llc|plc|gmbh|corp|corporation|co|limited|ag|sa|bv|pvt)\b\.?", re.I)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def normalize_company(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _SUFFIX.sub("", name.lower()))


def find_issues(db: Session) -> dict:
    now = datetime.now(timezone.utc)
    contacts = list(db.scalars(select(Contact)))
    accounts = list(db.scalars(select(Account)))

    # Duplicates
    by_email, by_name = defaultdict(list), defaultdict(list)
    for c in contacts:
        if c.email:
            by_email[c.email.lower()].append(c)
        by_name[(c.first_name.lower(), c.last_name.lower(), c.account_id)].append(c)
    dup_contacts = []
    seen = set()
    for group in [*by_email.values(), *by_name.values()]:
        ids = tuple(sorted(c.id for c in group))
        if len(group) > 1 and ids not in seen:
            seen.add(ids)
            dup_contacts.append({"keep_id": ids[0], "merge_ids": list(ids[1:]),
                                 "name": group[0].full_name, "email": group[0].email})

    by_company = defaultdict(list)
    for a in accounts:
        by_company[a.domain.lower() if a.domain else normalize_company(a.name)].append(a)
    dup_accounts = [{"ids": sorted(a.id for a in g), "names": [a.name for a in g]}
                    for g in by_company.values() if len(g) > 1]

    # Missing data
    missing_contact = [{"contact_id": c.id, "name": c.full_name,
                        "missing": [f for f in ("email", "title") if not getattr(c, f)]}
                       for c in contacts if c.status != "disqualified" and (not c.email or not c.title)]
    missing_account = [{"account_id": a.id, "name": a.name,
                        "missing": [f for f in ("industry", "employees", "domain") if not getattr(a, f)]}
                       for a in accounts if not (a.industry and a.employees and a.domain)]
    unscored = [c.id for c in contacts if c.score is None and c.status != "disqualified"]

    # Stale contacts: contacted, no reply, nothing pending, quiet for N days
    last_act = dict(db.execute(select(Activity.contact_id, func.max(Activity.created_at))
                               .group_by(Activity.contact_id)).all())
    pending = set(db.scalars(select(Approval.contact_id).where(Approval.status.in_(["pending", "approved"]))))
    stale_contacts = []
    for c in contacts:
        last = last_act.get(c.id)
        if c.status == "contacted" and c.id not in pending and last and now - _aware(last) > timedelta(days=STALE_CONTACT_DAYS):
            stale_contacts.append({"contact_id": c.id, "name": c.full_name, "account": c.account.name,
                                   "days_quiet": (now - _aware(last)).days})

    # Deals
    deals = []
    for o in db.scalars(select(Opportunity).where(Opportunity.stage.notin_(["closed_won", "closed_lost"]))):
        problems = []
        quiet = (now - _aware(o.last_activity_at)).days
        if quiet > STALE_DEAL_DAYS:
            problems.append(f"no activity for {quiet} days")
        if not o.amount:
            problems.append("no amount")
        if not o.close_date:
            problems.append("no close date")
        elif o.close_date < now.date().isoformat():
            problems.append(f"close date {o.close_date} is in the past")
        if not o.next_steps:
            problems.append("no next step")
        if problems:
            deals.append({"opportunity_id": o.id, "name": o.name, "stage": o.stage, "problems": problems})

    return {
        "generated_at": now.isoformat(),
        "duplicate_contacts": dup_contacts,
        "duplicate_accounts": dup_accounts,
        "contacts_missing_data": missing_contact,
        "accounts_missing_data": missing_account,
        "unscored_leads": unscored,
        "stale_contacts": stale_contacts,
        "deal_issues": deals,
    }


def to_markdown(issues: dict) -> str:
    def section(title, rows, fmt):
        return [f"## {title} ({len(rows)})", *([f"- {fmt(r)}" for r in rows[:50]] or ["- None"]), ""]

    return "\n".join([
        *section("Duplicate contacts", issues["duplicate_contacts"],
                 lambda r: f"{r['name']} ({r['email']}): keep #{r['keep_id']}, merge {r['merge_ids']}"),
        *section("Duplicate accounts", issues["duplicate_accounts"], lambda r: " / ".join(r["names"])),
        *section("Deals needing attention", issues["deal_issues"],
                 lambda r: f"{r['name']} ({r['stage']}): {', '.join(r['problems'])}"),
        *section("Contacted leads gone quiet", issues["stale_contacts"],
                 lambda r: f"{r['name']} at {r['account']}: {r['days_quiet']} days, consider a new angle or close out"),
        *section("Contacts missing data", issues["contacts_missing_data"],
                 lambda r: f"{r['name']}: missing {', '.join(r['missing'])}"),
        *section("Accounts missing data", issues["accounts_missing_data"],
                 lambda r: f"{r['name']}: missing {', '.join(r['missing'])}"),
        f"**Unscored leads:** {len(issues['unscored_leads'])}",
    ])


def save_report(db: Session, issues: dict, extra_markdown: str = "", run_id: int | None = None) -> SalesDocument:
    doc = SalesDocument(kind="hygiene_report", title=f"CRM hygiene report {issues['generated_at'][:10]}",
                        content=to_markdown(issues) + extra_markdown, data=issues, run_id=run_id)
    db.add(doc)
    db.flush()
    return doc


def merge_contacts(db: Session, keep_id: int, merge_id: int) -> Contact:
    """Fold `merge_id` into `keep_id`: move history, fill blanks, delete the duplicate."""
    keep, dup = db.get(Contact, keep_id), db.get(Contact, merge_id)
    if not keep or not dup or keep_id == merge_id:
        raise ValueError("Both contacts must exist and differ.")
    for field in ("email", "title", "linkedin_url", "sf_id", "crm_id", "inbound_message", "owner"):
        if not getattr(keep, field) and getattr(dup, field):
            setattr(keep, field, getattr(dup, field))
    if dup.score is not None and (keep.score is None or dup.score > keep.score):
        keep.score, keep.score_tier, keep.score_reason = dup.score, dup.score_tier, dup.score_reason
    for model in (Activity, Approval, SalesDocument):
        for row in db.scalars(select(model).where(model.contact_id == merge_id)):
            row.contact_id = keep_id
    for o in db.scalars(select(Opportunity).where(Opportunity.primary_contact_id == merge_id)):
        o.primary_contact_id = keep_id
    db.add(Activity(contact_id=keep_id, type="merged", detail=f"Merged duplicate contact #{merge_id}"))
    db.flush()
    db.delete(dup)
    return keep
