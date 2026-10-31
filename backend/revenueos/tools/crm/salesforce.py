"""Salesforce adapter (simple-salesforce)."""
import logging
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import settings
from ...models import Campaign, Contact, Opportunity
from .base import CRMAdapter

log = logging.getLogger(__name__)


class SalesforceCRM(CRMAdapter):
    name = "salesforce"
    id_field = "sf_id"

    # Standard Lead.Status picklist values.
    STATUS_MAP = {
        "new": "Open - Not Contacted",
        "researched": "Open - Not Contacted",
        "sequenced": "Working - Contacted",
        "contacted": "Working - Contacted",
        "replied": "Working - Contacted",
        "meeting": "Closed - Converted",
        "disqualified": "Closed - Not Converted",
    }
    # Standard Opportunity.StageName picklist values.
    STAGE_MAP = {
        "discovery": "Prospecting",
        "qualification": "Qualification",
        "solution": "Needs Analysis",
        "proposal": "Proposal/Price Quote",
        "negotiation": "Negotiation/Review",
        "closed_won": "Closed Won",
        "closed_lost": "Closed Lost",
    }

    def __init__(self, client=None) -> None:
        super().__init__()
        if client is None:
            from simple_salesforce import Salesforce

            client = Salesforce(username=settings.sf_username, password=settings.sf_password,
                                security_token=settings.sf_security_token or "", domain=settings.sf_domain)
        self.sf = client

    def ping(self) -> str:
        n = self.sf.query("SELECT COUNT() FROM Lead").get("totalSize")
        return f"Connected: {n} leads visible"

    def log_activity(self, contact: Contact, subject: str, description: str) -> str | None:
        if not contact.sf_id:
            return None
        result = self.sf.Task.create({
            "WhoId": contact.sf_id, "Subject": subject[:255], "Description": description[:32000],
            "Status": "Completed", "ActivityDate": date.today().isoformat(), "TaskSubtype": "Email",
        })
        return result.get("id")

    def update_lead_status(self, contact: Contact, status: str, score: float | None) -> None:
        if not contact.sf_id or not contact.sf_id.startswith("00Q"):  # Lead ids only
            return
        fields = {"Status": self.status_map.get(status, "Working - Contacted")}
        if score is not None:
            fields["Rating"] = "Hot" if score >= 75 else "Warm" if score >= 50 else "Cold"
        self.sf.Lead.update(contact.sf_id, fields)

    def upsert_opportunity(self, opp: Opportunity) -> str | None:
        """Needs the Account to exist in Salesforce; otherwise the deal stays local."""
        account_sf_id = opp.account.sf_id
        if not account_sf_id:
            log.info("account %s has no Salesforce id; opportunity kept local", opp.account.name)
            return None
        q = opp.qualification or {}
        fields = {
            "Name": opp.name[:120],
            "StageName": self.stage_map.get(opp.stage, "Prospecting"),
            "CloseDate": opp.close_date or date.today().replace(day=28).isoformat(),
            "NextStep": "; ".join(opp.next_steps)[:255] or None,
            "Description": "\n".join(f"{k}: {v}" for k, v in q.items())[:32000],
        }
        if opp.amount:
            fields["Amount"] = opp.amount
        if opp.sf_id:
            self.sf.Opportunity.update(opp.sf_id, fields)
            return opp.sf_id
        new_id = self.sf.Opportunity.create(fields | {"AccountId": account_sf_id}).get("id")
        self.set_ext_id(opp, new_id)
        return new_id

    def create_campaign(self, campaign: Campaign, members: list[Contact]) -> str | None:
        fields = {"Name": campaign.name[:80], "Status": "Planned", "IsActive": False,
                  "Description": campaign.goal[:32000]}
        if campaign.start_date:
            fields["StartDate"] = campaign.start_date
        if campaign.end_date:
            fields["EndDate"] = campaign.end_date
        if campaign.budget:
            fields["BudgetedCost"] = campaign.budget
        sf_campaign_id = self.sf.Campaign.create(fields).get("id")
        self.set_ext_id(campaign, sf_campaign_id)
        for c in members:
            if not c.sf_id:
                continue
            key = "LeadId" if c.sf_id.startswith("00Q") else "ContactId"
            try:
                self.sf.CampaignMember.create({"CampaignId": sf_campaign_id, key: c.sf_id, "Status": "Sent"})
            except Exception:  # duplicate membership etc. shouldn't fail the plan
                log.warning("could not add %s to campaign", c.sf_id, exc_info=True)
        return sf_campaign_id

    def import_open_leads(self, db: Session, limit: int = 200) -> int:
        soql = ("SELECT Id, FirstName, LastName, Title, Email, Company, Website, Industry, "
                "NumberOfEmployees, Country, LeadSource, Description FROM Lead "
                f"WHERE IsConverted = false AND Status LIKE 'Open%' ORDER BY CreatedDate DESC LIMIT {int(limit)}")
        return sum(self.local_contact(
            db, ext_id=r["Id"], first_name=r.get("FirstName") or "", last_name=r["LastName"], company=r["Company"],
            email=r.get("Email"), title=r.get("Title"), domain=r.get("Website"), industry=r.get("Industry"),
            employees=r.get("NumberOfEmployees"), country=r.get("Country"), description=r.get("Description"),
        ) for r in self.sf.query_all(soql)["records"])

    def pull_deals(self, db: Session, since: datetime) -> int:
        ids = [i for i in db.scalars(select(Opportunity.sf_id).where(Opportunity.sf_id.isnot(None)))]
        if not ids:
            return 0
        changed = 0
        for chunk in (ids[i:i + 200] for i in range(0, len(ids), 200)):
            quoted = ",".join(f"'{i}'" for i in chunk if i.isalnum())  # ids are alphanumeric; nothing else is sent
            soql = ("SELECT Id, StageName, Amount, CloseDate FROM Opportunity "
                    f"WHERE Id IN ({quoted}) AND LastModifiedDate > {since.strftime('%Y-%m-%dT%H:%M:%SZ')}")
            for r in self.sf.query_all(soql)["records"]:
                changed += self.apply_deal_update(db, r["Id"], crm_stage=r.get("StageName"),
                                                  amount=r.get("Amount"), close_date=r.get("CloseDate"))
        return changed
