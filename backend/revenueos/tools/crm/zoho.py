"""Zoho CRM adapter (REST API v2, self-client OAuth refresh token).

Setup: in the Zoho API console create a Self Client with scopes ZohoCRM.modules.ALL and
ZohoCRM.org.READ, generate a refresh token, and set ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET /
ZOHO_REFRESH_TOKEN (plus ZOHO_ACCOUNTS_URL / ZOHO_API_DOMAIN outside the US data centre).
"""
import logging
import time
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import settings
from ...integrations import http
from ...models import Campaign, Contact, Opportunity
from .base import CRMAdapter

log = logging.getLogger(__name__)


class ZohoCRM(CRMAdapter):
    name = "zoho"

    STATUS_MAP = {"new": "Not Contacted", "researched": "Not Contacted", "sequenced": "Attempted to Contact",
                  "contacted": "Attempted to Contact", "replied": "Contacted", "meeting": "Pre-Qualified",
                  "disqualified": "Not Qualified"}
    STAGE_MAP = {"discovery": "Qualification", "qualification": "Needs Analysis", "solution": "Value Proposition",
                 "proposal": "Proposal/Price Quote", "negotiation": "Negotiation/Review",
                 "closed_won": "Closed Won", "closed_lost": "Closed Lost"}

    def __init__(self) -> None:
        super().__init__()
        self._token, self._expires = None, 0.0

    def _auth(self) -> dict:
        if not self._token or time.time() > self._expires:
            if not settings.zoho_refresh_token:
                raise RuntimeError("ZOHO_REFRESH_TOKEN is not set.")
            tok = http.request("POST", f"{settings.zoho_accounts_url.rstrip('/')}/oauth/v2/token", form={
                "refresh_token": settings.zoho_refresh_token, "client_id": settings.zoho_client_id,
                "client_secret": settings.zoho_client_secret, "grant_type": "refresh_token"})
            if "access_token" not in tok:
                raise RuntimeError(f"Zoho token refresh failed: {tok.get('error', tok)}")
            self._token, self._expires = tok["access_token"], time.time() + int(tok.get("expires_in", 3600)) - 60
        return {"Authorization": f"Zoho-oauthtoken {self._token}"}

    def _req(self, method: str, path: str, **kw):
        return http.request(method, f"{settings.zoho_api_domain.rstrip('/')}/crm/v2/{path}", headers=self._auth(), **kw)

    def ping(self) -> str:
        org = self._req("GET", "org").get("org", [{}])[0]
        return f"Connected to {org.get('company_name', 'Zoho CRM')}"

    def import_open_leads(self, db: Session, limit: int = 200) -> int:
        res = self._req("GET", "Leads/search", params={
            "criteria": "((Lead_Status:equals:Not Contacted)or(Lead_Status:equals:Attempted to Contact))",
            "per_page": min(limit, 200)})
        return sum(self.local_contact(
            db, ext_id=str(r["id"]), first_name=r.get("First_Name") or "", last_name=r.get("Last_Name") or "Unknown",
            company=r.get("Company") or "Unknown company", email=r.get("Email"), title=r.get("Designation"),
            domain=r.get("Website"), industry=r.get("Industry"), employees=r.get("No_of_Employees"),
            country=r.get("Country"), description=r.get("Description"),
        ) for r in (res or {}).get("data", []))

    def log_activity(self, contact: Contact, subject: str, description: str) -> str | None:
        if not contact.crm_id:
            return None
        res = self._req("POST", "Notes", json_body={"data": [{
            "Note_Title": subject[:120], "Note_Content": description[:30000],
            "Parent_Id": contact.crm_id, "se_module": "Leads"}]})
        return res["data"][0]["details"]["id"]

    def update_lead_status(self, contact: Contact, status: str, score: float | None) -> None:
        if contact.crm_id and status in self.status_map:
            self._req("PUT", f"Leads/{contact.crm_id}", json_body={"data": [{"Lead_Status": self.status_map[status]}]})

    def upsert_opportunity(self, opp: Opportunity) -> str | None:
        record = {"Deal_Name": opp.name[:120], "Stage": self.stage_map.get(opp.stage, "Qualification"),
                  "Description": "\n".join(f"{k}: {v}" for k, v in (opp.qualification or {}).items())[:30000]}
        if opp.amount:
            record["Amount"] = opp.amount
        if opp.close_date:
            record["Closing_Date"] = opp.close_date
        if opp.crm_id:
            self._req("PUT", f"Deals/{opp.crm_id}", json_body={"data": [record]})
            return opp.crm_id
        res = self._req("POST", "Deals", json_body={"data": [record]})
        new_id = str(res["data"][0]["details"]["id"])
        self.set_ext_id(opp, new_id)
        return new_id

    def create_campaign(self, campaign: Campaign, members: list[Contact]) -> str | None:
        record = {"Campaign_Name": campaign.name[:120], "Description": campaign.goal[:30000], "Status": "Planning"}
        if campaign.start_date:
            record["Start_Date"] = campaign.start_date
        if campaign.end_date:
            record["End_Date"] = campaign.end_date
        if campaign.budget:
            record["Budgeted_Cost"] = campaign.budget
        new_id = str(self._req("POST", "Campaigns", json_body={"data": [record]})["data"][0]["details"]["id"])
        self.set_ext_id(campaign, new_id)
        return new_id

    def pull_deals(self, db: Session, since: datetime) -> int:
        known = {i for i in db.scalars(select(Opportunity.crm_id).where(Opportunity.crm_id.isnot(None)))}
        if not known:
            return 0
        res = http.request(  # only deals changed since the last pull
            "GET", f"{settings.zoho_api_domain.rstrip('/')}/crm/v2/Deals",
            headers={**self._auth(), "If-Modified-Since": since.strftime("%Y-%m-%dT%H:%M:%S+00:00")},
            params={"fields": "Stage,Amount,Closing_Date", "per_page": 200})
        changed = 0
        for r in (res or {}).get("data", []):
            if str(r["id"]) in known:
                changed += self.apply_deal_update(db, str(r["id"]), crm_stage=r.get("Stage"),
                                                  amount=r.get("Amount"), close_date=r.get("Closing_Date"))
        return changed
