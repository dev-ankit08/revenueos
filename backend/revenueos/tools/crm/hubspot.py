"""HubSpot adapter (CRM v3 API, private-app token in HUBSPOT_TOKEN).

Scopes the private app needs: crm.objects.contacts.read/write, crm.objects.deals.read/write,
crm.objects.companies.read. Campaign creation needs Marketing Hub and is not done here.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import settings
from ...integrations import http
from ...models import Campaign, Contact, Opportunity
from .base import CRMAdapter

log = logging.getLogger(__name__)
API = "https://api.hubapi.com"
NOTE_TO_CONTACT = 202  # HubSpot-defined association type ids
DEAL_TO_CONTACT = 3


class HubSpotCRM(CRMAdapter):
    name = "hubspot"

    # hs_lead_status values
    STATUS_MAP = {
        "new": "NEW", "researched": "NEW", "sequenced": "ATTEMPTED_TO_CONTACT", "contacted": "ATTEMPTED_TO_CONTACT",
        "replied": "CONNECTED", "meeting": "OPEN_DEAL", "disqualified": "UNQUALIFIED",
    }
    # Default sales pipeline stage ids
    STAGE_MAP = {
        "discovery": "appointmentscheduled", "qualification": "qualifiedtobuy", "solution": "presentationscheduled",
        "proposal": "decisionmakerboughtin", "negotiation": "contractsent",
        "closed_won": "closedwon", "closed_lost": "closedlost",
    }

    def _req(self, method: str, path: str, **kw):
        if not settings.hubspot_token:
            raise RuntimeError("HUBSPOT_TOKEN is not set.")
        return http.request(method, f"{API}{path}", headers={"Authorization": f"Bearer {settings.hubspot_token}"}, **kw)

    def ping(self) -> str:
        self._req("GET", "/crm/v3/objects/contacts", params={"limit": 1})
        return "Connected to HubSpot"

    def import_open_leads(self, db: Session, limit: int = 200) -> int:
        res = self._req("POST", "/crm/v3/objects/contacts/search", json_body={
            "filterGroups": [{"filters": [{"propertyName": "hs_lead_status", "operator": "IN", "values": ["NEW", "OPEN"]}]}],
            "properties": ["firstname", "lastname", "email", "jobtitle", "company", "website", "industry", "country"],
            "limit": min(limit, 100)})
        created = 0
        for r in res.get("results", []):
            p = r.get("properties", {})
            if not (p.get("lastname") or p.get("email")):
                continue
            created += self.local_contact(
                db, ext_id=str(r["id"]), first_name=p.get("firstname") or "", last_name=p.get("lastname") or p["email"],
                company=p.get("company") or (p.get("email") or "@unknown").split("@")[-1], email=p.get("email"),
                title=p.get("jobtitle"), domain=p.get("website"), industry=p.get("industry"), country=p.get("country"))
        return created

    def log_activity(self, contact: Contact, subject: str, description: str) -> str | None:
        if not contact.crm_id:
            return None
        res = self._req("POST", "/crm/v3/objects/notes", json_body={
            "properties": {"hs_timestamp": datetime.now(timezone.utc).isoformat(),
                           "hs_note_body": f"<b>{subject}</b><br>{description[:60000]}"},
            "associations": [{"to": {"id": contact.crm_id},
                              "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": NOTE_TO_CONTACT}]}]})
        return res.get("id")

    def update_lead_status(self, contact: Contact, status: str, score: float | None) -> None:
        if contact.crm_id and status in self.status_map:
            self._req("PATCH", f"/crm/v3/objects/contacts/{contact.crm_id}",
                      json_body={"properties": {"hs_lead_status": self.status_map[status]}})

    def upsert_opportunity(self, opp: Opportunity) -> str | None:
        props = {"dealname": opp.name[:200], "dealstage": self.stage_map.get(opp.stage, "appointmentscheduled"),
                 "pipeline": "default",
                 "description": "\n".join(f"{k}: {v}" for k, v in (opp.qualification or {}).items())[:60000]}
        if opp.amount:
            props["amount"] = str(opp.amount)
        if opp.close_date:
            props["closedate"] = opp.close_date
        if opp.crm_id:
            self._req("PATCH", f"/crm/v3/objects/deals/{opp.crm_id}", json_body={"properties": props})
            return opp.crm_id
        body = {"properties": props}
        if opp.primary_contact and opp.primary_contact.crm_id:
            body["associations"] = [{"to": {"id": opp.primary_contact.crm_id},
                                     "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": DEAL_TO_CONTACT}]}]
        new_id = str(self._req("POST", "/crm/v3/objects/deals", json_body=body)["id"])
        self.set_ext_id(opp, new_id)
        return new_id

    def create_campaign(self, campaign: Campaign, members: list[Contact]) -> str | None:
        log.info("HubSpot campaigns need Marketing Hub; campaign %r kept in RevenueOS only", campaign.name)
        return None

    def pull_deals(self, db: Session, since: datetime) -> int:
        known = {i for i in db.scalars(select(Opportunity.crm_id).where(Opportunity.crm_id.isnot(None)))}
        if not known:
            return 0
        res = self._req("POST", "/crm/v3/objects/deals/search", json_body={
            "filterGroups": [{"filters": [{"propertyName": "hs_lastmodifieddate", "operator": "GT",
                                           "value": str(int(since.timestamp() * 1000))}]}],
            "properties": ["dealstage", "amount", "closedate"], "limit": 100})
        changed = 0
        for r in res.get("results", []):
            if str(r["id"]) not in known:
                continue
            p = r.get("properties", {})
            amount = float(p["amount"]) if p.get("amount") else None
            changed += self.apply_deal_update(db, str(r["id"]), crm_stage=p.get("dealstage"), amount=amount,
                                              close_date=p.get("closedate"))
        return changed
