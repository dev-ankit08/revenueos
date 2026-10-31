"""The CRM adapter contract. RevenueOS works on top of any CRM that implements it.

The CRM stays the system of record; RevenueOS keeps a local working copy and syncs both ways:
- out: activities (emails, calls), lead status, deals, campaigns
- in:  new open leads and changes reps make to deals in the CRM (`pull_changes`)

Each adapter maps our pipeline vocabulary onto the CRM's. Mappings can be overridden per
deployment with CRM_FIELD_MAP, e.g. {"deal_stage": {"proposal": "contractsent"}}.
"""
import json
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import settings
from ...models import Account, Campaign, Contact, Opportunity

log = logging.getLogger(__name__)

OUR_STAGES = ("discovery", "qualification", "solution", "proposal", "negotiation", "closed_won", "closed_lost")


class CRMAdapter:
    name = "base"
    id_field = "crm_id"  # which column on our records holds this CRM's id
    STATUS_MAP: dict[str, str] = {}  # our contact status -> CRM lead status
    STAGE_MAP: dict[str, str] = {}  # our deal stage -> CRM deal stage

    def __init__(self) -> None:
        overrides = json.loads(settings.crm_field_map or "{}")
        self.status_map = {**self.STATUS_MAP, **overrides.get("lead_status", {})}
        self.stage_map = {**self.STAGE_MAP, **overrides.get("deal_stage", {})}

    # ----------------------------------------------------------------- ids and mappings
    def ext_id(self, obj) -> str | None:
        return getattr(obj, self.id_field, None)

    def set_ext_id(self, obj, value: str | None) -> None:
        if value:
            setattr(obj, self.id_field, value)

    def stage_from_crm(self, value: str | None) -> str | None:
        """CRM deal stage -> ours (first match in our pipeline order)."""
        for ours in OUR_STAGES:
            if self.stage_map.get(ours) == value:
                return ours
        return None

    # ----------------------------------------------------------------- the contract
    def ping(self) -> str:
        """A cheap read that proves the credentials work (no data is written)."""
        return "no external CRM"

    def import_open_leads(self, db: Session, limit: int = 200) -> int:
        return 0

    def pull_changes(self, db: Session, since: datetime) -> dict:
        """Bring in new open leads and deal changes reps made in the CRM since `since`."""
        return {"leads_imported": self.import_open_leads(db), "deals_updated": self.pull_deals(db, since)}

    def pull_deals(self, db: Session, since: datetime) -> int:
        return 0

    def log_activity(self, contact: Contact, subject: str, description: str) -> str | None:
        return None

    def update_lead_status(self, contact: Contact, status: str, score: float | None) -> None:
        return None

    def upsert_opportunity(self, opp: Opportunity) -> str | None:
        """Create or update the deal in the CRM; stores the CRM id on `opp` and returns it."""
        return None

    def create_campaign(self, campaign: Campaign, members: list[Contact]) -> str | None:
        """Create the campaign in the CRM; stores the CRM id on `campaign` and returns it."""
        return None

    # ----------------------------------------------------------------- shared helpers for adapters
    def local_contact(self, db: Session, *, ext_id: str, last_name: str, company: str, first_name: str = "",
                      email: str | None = None, title: str | None = None, domain: str | None = None,
                      industry: str | None = None, employees: int | None = None, country: str | None = None,
                      description: str | None = None) -> bool:
        """Create the local copy of a CRM lead unless we already have it. Returns True if created."""
        col = getattr(Contact, self.id_field)
        if db.scalar(select(Contact).where(col == ext_id)):
            return False
        if email and (existing := db.scalar(select(Contact).where(Contact.email.ilike(email)))):
            self.set_ext_id(existing, ext_id)  # same person, now linked to the CRM record
            return False
        domain = (domain or "").replace("https://", "").replace("http://", "").removeprefix("www.").strip("/") or None
        account = db.scalar(select(Account).where(Account.name == company))
        if not account:
            account = Account(name=company, domain=domain, industry=industry, employees=employees,
                              country=country, source=self.name)
            db.add(account)
            db.flush()
        c = Contact(account_id=account.id, first_name=first_name or "", last_name=last_name, title=title,
                    email=email, source=self.name, inbound_message=description)
        self.set_ext_id(c, ext_id)
        db.add(c)
        db.flush()
        return True

    def apply_deal_update(self, db: Session, ext_id: str, *, crm_stage: str | None = None,
                          amount: float | None = None, close_date: str | None = None) -> bool:
        """A rep changed a deal in the CRM: mirror it locally. Returns True if anything changed."""
        col = getattr(Opportunity, self.id_field)
        opp = db.scalar(select(Opportunity).where(col == ext_id))
        if not opp:
            return False
        changed = False
        stage = self.stage_from_crm(crm_stage)
        if stage and stage != opp.stage:
            opp.stage, changed = stage, True
        if amount is not None and amount != opp.amount:
            opp.amount, changed = amount, True
        if close_date and close_date[:10] != opp.close_date:
            opp.close_date, changed = close_date[:10], True
        return changed
