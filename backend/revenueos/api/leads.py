import csv
import io

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import jobs
from ..agents import researcher, scorer, workflows, writer
from ..agents.person_researcher import get_dossier, research_person
from ..agents.shared_tools import brief_to_dict, contact_to_dict
from ..db import get_db
from ..models import Account, Activity, Approval, Contact
from ..tools.crm import get_crm
from .approvals import approval_to_dict

router = APIRouter(prefix="/api", tags=["leads"])


class LeadIn(BaseModel):
    first_name: str
    last_name: str
    company: str
    title: str | None = None
    email: str | None = None
    linkedin_url: str | None = None
    domain: str | None = None
    industry: str | None = None
    employees: int | None = None
    country: str | None = None
    notes: str | None = None
    source: str = "manual"
    inbound_message: str | None = None


def upsert_lead(db: Session, lead: LeadIn) -> Contact:
    account = db.scalar(select(Account).where(
        (Account.domain == lead.domain) if lead.domain else (Account.name == lead.company)
    ))
    if not account:
        account = Account(name=lead.company, domain=lead.domain, industry=lead.industry,
                          employees=lead.employees, country=lead.country, notes=lead.notes, source=lead.source)
        db.add(account)
        db.flush()
    contact = db.scalar(select(Contact).where(Contact.email == lead.email)) if lead.email else None
    if contact:
        if lead.inbound_message:
            contact.inbound_message = lead.inbound_message
            contact.source = lead.source
        return contact
    contact = Contact(
        account_id=account.id, first_name=lead.first_name, last_name=lead.last_name, title=lead.title,
        email=lead.email, linkedin_url=lead.linkedin_url, source=lead.source,
        inbound_message=lead.inbound_message,
    )
    db.add(contact)
    db.flush()
    from ..services import salesmgmt

    salesmgmt.assign_owner(db, contact)
    return contact


@router.get("/leads")
def list_leads(status: str | None = None, tier: str | None = None, db: Session = Depends(get_db)):
    stmt = select(Contact).join(Account)
    if status:
        stmt = stmt.where(Contact.status == status)
    if tier:
        stmt = stmt.where(Contact.score_tier == tier)
    stmt = stmt.order_by(Contact.score.desc().nullslast(), Contact.id.desc()).limit(500)
    return [contact_to_dict(c) | {"has_brief": bool(c.account.briefs)} for c in db.scalars(stmt)]


@router.post("/leads")
def create_lead(lead: LeadIn, db: Session = Depends(get_db)):
    contact = upsert_lead(db, lead)
    db.commit()
    return contact_to_dict(contact)


@router.get("/leads/lookup")
def lookup(crm: str | None = None, id: str | None = None, email: str | None = None, db: Session = Depends(get_db)):
    """Find our contact for a CRM record (used by the side panel embedded in Salesforce, HubSpot or Dynamics)."""
    c = None
    if id and crm == "salesforce":
        c = db.scalar(select(Contact).where(Contact.sf_id == id))
    elif id and crm in ("hubspot", "dynamics"):
        c = db.scalar(select(Contact).where(Contact.crm_id == id))
    if not c and email:
        c = db.scalar(select(Contact).where(Contact.email.ilike(email)))
    if not c:
        raise HTTPException(404, "This record isn't in RevenueOS yet. Import open leads from the CRM, or add it.")
    return {"contact_id": c.id, "account_id": c.account_id}


@router.get("/leads/{contact_id}")
def lead_detail(contact_id: int, db: Session = Depends(get_db)):
    c = db.get(Contact, contact_id)
    if not c:
        raise HTTPException(404, "Lead not found")
    acts = db.scalars(select(Activity).where(Activity.contact_id == c.id).order_by(Activity.created_at.desc()))
    apps = db.scalars(select(Approval).where(Approval.contact_id == c.id).order_by(Approval.id))
    a = c.account
    return contact_to_dict(c) | {
        "score_reason": c.score_reason, "inbound_message": c.inbound_message, "linkedin_url": c.linkedin_url,
        "experiment_group": c.experiment_group, "timezone": c.timezone, "owner": c.owner,
        "lifecycle": c.lifecycle, "email_status": c.email_status, "lawful_basis": c.lawful_basis,
        "consent_source": c.consent_source, "consent_at": c.consent_at, "dossier": get_dossier(c.id),
        "account_detail": {"id": a.id, "name": a.name, "domain": a.domain, "industry": a.industry,
                           "employees": a.employees, "country": a.country, "notes": a.notes},
        "brief": brief_to_dict(a.briefs[0] if a.briefs else None),
        "activities": [{"type": x.type, "detail": x.detail, "at": x.created_at} for x in acts],
        "approvals": [approval_to_dict(x) for x in apps],
    }


class SequenceIn(BaseModel):
    steps: int = 3
    instructions: str | None = None
    ab_test: bool = False


@router.post("/leads/{contact_id}/score")
def score(contact_id: int):
    async def work(run_id: int) -> str:
        return (await scorer.score_contact(contact_id, parent_run_id=run_id)).model_dump_json()
    return {"run_id": jobs.start("job:score", f"Score contact {contact_id}", work)}


@router.post("/leads/{contact_id}/research")
def research(contact_id: int, db: Session = Depends(get_db)):
    c = db.get(Contact, contact_id)
    if not c:
        raise HTTPException(404, "Lead not found")
    account_id = c.account_id

    async def work(run_id: int) -> str:
        return await researcher.research_account(account_id, parent_run_id=run_id)
    return {"run_id": jobs.start("job:research", f"Research account {account_id}", work)}


@router.post("/leads/{contact_id}/sequence")
def sequence(contact_id: int, body: SequenceIn):
    async def work(run_id: int) -> str:
        return await writer.write_sequence(
            contact_id, body.steps, body.instructions, body.ab_test, parent_run_id=run_id
        )
    return {"run_id": jobs.start("job:sequence", f"Write sequence for contact {contact_id}", work)}


class BatchIn(BaseModel):
    contact_ids: list[int]
    steps: int = 3


@router.post("/leads/batch-outbound")
def batch_outbound(body: BatchIn):
    async def work(run_id: int) -> str:
        return await workflows.outbound_batch(body.contact_ids, body.steps, parent_run_id=run_id)
    return {"run_id": jobs.start("job:outbound_batch", f"Outbound batch of {len(body.contact_ids)}", work)}


@router.post("/crm/sync")
async def crm_sync():
    """Pull new open leads and deal changes from the CRM now."""
    import asyncio

    from ..integrations.sync import run_crm_pull
    return {"crm": get_crm().name, **(await asyncio.to_thread(run_crm_pull))}


@router.post("/crm/import")
def crm_import(db: Session = Depends(get_db)):
    crm = get_crm()
    n = crm.import_open_leads(db)
    db.commit()
    return {"crm": crm.name, "imported": n}


# Common header spellings from Apollo / ZoomInfo / Sales Navigator / Salesforce exports.
CSV_ALIASES = {
    "first_name": ("first_name", "first name", "firstname"),
    "last_name": ("last_name", "last name", "lastname", "surname"),
    "email": ("email", "email address", "work email", "business email"),
    "title": ("title", "job title", "position"),
    "company": ("company", "company name", "account name", "organization"),
    "domain": ("domain", "website", "company website", "company domain"),
    "industry": ("industry",),
    "employees": ("employees", "# employees", "employee count", "company size", "number of employees"),
    "country": ("country", "company country", "location country"),
    "linkedin_url": ("linkedin_url", "linkedin", "person linkedin url", "linkedin profile"),
}


@router.post("/leads/import-csv")
async def import_csv(file: UploadFile, db: Session = Depends(get_db)):
    """Import contacts from a data-provider or CRM export. Rows without name + company are skipped."""
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    headers = {h.strip().lower(): h for h in (reader.fieldnames or [])}
    mapping = {field: next((headers[a] for a in aliases if a in headers), None)
               for field, aliases in CSV_ALIASES.items()}
    if not (mapping["last_name"] and mapping["company"]):
        raise HTTPException(422, "CSV needs at least last name and company columns.")

    imported, skipped = 0, 0
    for row in reader:
        get = lambda f: (row.get(mapping[f]) or "").strip() or None if mapping[f] else None  # noqa: E731
        if not (get("last_name") and get("company")):
            skipped += 1
            continue
        employees = get("employees")
        try:
            employees = int(float(employees.replace(",", ""))) if employees else None
        except ValueError:
            employees = None
        domain = get("domain")
        if domain:
            domain = domain.lower().replace("https://", "").replace("http://", "").removeprefix("www.").strip("/")
        upsert_lead(db, LeadIn(
            first_name=get("first_name") or "", last_name=get("last_name"), company=get("company"),
            title=get("title"), email=get("email"), linkedin_url=get("linkedin_url"), domain=domain,
            industry=get("industry"), employees=employees, country=get("country"), source="import",
        ))
        imported += 1
    db.commit()
    return {"imported": imported, "skipped": skipped, "columns_used": {k: v for k, v in mapping.items() if v}}


@router.post("/leads/{contact_id}/dossier")
def dossier(contact_id: int, db: Session = Depends(get_db)):
    if not db.get(Contact, contact_id):
        raise HTTPException(404, "Lead not found")

    async def work(run_id: int) -> str:
        d = await research_person(contact_id, parent_run_id=run_id)
        return f"{len(d['public_statements'])} sourced statements"
    return {"run_id": jobs.start("job:dossier", f"Person dossier for contact {contact_id}", work)}


@router.post("/leads/{contact_id}/release-holdout")
def release_holdout(contact_id: int, db: Session = Depends(get_db)):
    """Take a contact out of the control group. They are then excluded from lift statistics."""
    c = db.get(Contact, contact_id)
    if not c:
        raise HTTPException(404, "Lead not found")
    if c.experiment_group == "holdout":
        c.experiment_group = "released"
        db.commit()
    return {"contact_id": c.id, "experiment_group": c.experiment_group}
