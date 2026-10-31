"""Microsoft Dynamics 365 Sales adapter (Dataverse Web API v9.2, app registration with client credentials).

Setup: register an Entra app, create an application user in the Dynamics environment with a
sales role, and set DYNAMICS_URL plus DYNAMICS_CLIENT_ID / DYNAMICS_CLIENT_SECRET (or reuse MS_*).
"""
import logging
import time
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import settings
from ...integrations import http
from ...models import Campaign, Contact, Opportunity
from .base import CRMAdapter

log = logging.getLogger(__name__)


class DynamicsCRM(CRMAdapter):
    name = "dynamics"

    # Open-lead status reasons (statuscode with statecode 0). Qualifying/disqualifying a lead needs
    # Dynamics' own QualifyLead process, which reps run there; we only move open leads.
    STATUS_MAP = {"new": "1", "researched": "1", "sequenced": "2", "contacted": "2", "replied": "2"}
    # Written to opportunity.stepname (free text, shown on the record); won/lost use the close actions.
    STAGE_MAP = {
        "discovery": "1-Qualify", "qualification": "1-Qualify", "solution": "2-Develop",
        "proposal": "3-Propose", "negotiation": "4-Close", "closed_won": "Won", "closed_lost": "Lost",
    }

    def __init__(self) -> None:
        super().__init__()
        self._token, self._expires = None, 0.0

    # ----------------------------------------------------------------- transport
    @property
    def api(self) -> str:
        if not settings.dynamics_url:
            raise RuntimeError("DYNAMICS_URL is not set.")
        return settings.dynamics_url.rstrip("/") + "/api/data/v9.2"

    def _auth(self) -> dict:
        if not self._token or time.time() > self._expires:
            tok = http.request("POST", f"https://login.microsoftonline.com/{settings.ms_tenant}/oauth2/v2.0/token", form={
                "grant_type": "client_credentials", "client_id": settings.dynamics_client_id,
                "client_secret": settings.dynamics_client_secret,
                "scope": settings.dynamics_url.rstrip("/") + "/.default"})
            self._token, self._expires = tok["access_token"], time.time() + int(tok.get("expires_in", 3600)) - 60
        return {"Authorization": f"Bearer {self._token}", "OData-Version": "4.0", "Accept": "application/json",
                "Prefer": "return=representation"}

    def _req(self, method: str, path: str, **kw):
        return http.request(method, f"{self.api}/{path}", headers=self._auth(), **kw)

    # ----------------------------------------------------------------- contract
    def ping(self) -> str:
        who = self._req("GET", "WhoAmI")
        return f"Connected as application user {who.get('UserId', '')}"

    def import_open_leads(self, db: Session, limit: int = 200) -> int:
        res = self._req("GET", "leads", params={
            "$select": "leadid,firstname,lastname,emailaddress1,jobtitle,companyname,websiteurl,description",
            "$filter": "statecode eq 0", "$top": min(limit, 500), "$orderby": "createdon desc"})
        return sum(self.local_contact(
            db, ext_id=r["leadid"], first_name=r.get("firstname") or "", last_name=r.get("lastname") or "Unknown",
            company=r.get("companyname") or "Unknown company", email=r.get("emailaddress1"), title=r.get("jobtitle"),
            domain=r.get("websiteurl"), description=r.get("description"),
        ) for r in res.get("value", []))

    def log_activity(self, contact: Contact, subject: str, description: str) -> str | None:
        if not contact.crm_id:
            return None
        res = self._req("POST", "tasks", json_body={
            "subject": subject[:200], "description": description[:100000],
            "regardingobjectid_lead@odata.bind": f"/leads({contact.crm_id})"})
        return res.get("activityid")

    def update_lead_status(self, contact: Contact, status: str, score: float | None) -> None:
        if contact.crm_id and status in self.status_map:
            body = {"statuscode": int(self.status_map[status])}
            if score is not None:
                body["leadqualitycode"] = 1 if score >= 75 else 2 if score >= 50 else 3  # hot / warm / cold
            self._req("PATCH", f"leads({contact.crm_id})", json_body=body)

    def upsert_opportunity(self, opp: Opportunity) -> str | None:
        body = {"name": opp.name[:300], "stepname": self.stage_map.get(opp.stage, ""),
                "description": "\n".join(f"{k}: {v}" for k, v in (opp.qualification or {}).items())[:100000]}
        if opp.amount:
            body["estimatedvalue"] = opp.amount
        if opp.close_date:
            body["estimatedclosedate"] = opp.close_date
        if opp.account.crm_id:
            body["parentaccountid@odata.bind"] = f"/accounts({opp.account.crm_id})"
        if opp.crm_id:
            self._req("PATCH", f"opportunities({opp.crm_id})", json_body=body)
        else:
            self.set_ext_id(opp, self._req("POST", "opportunities", json_body=body)["opportunityid"])
        if opp.stage in ("closed_won", "closed_lost"):
            action = "WinOpportunity" if opp.stage == "closed_won" else "LoseOpportunity"
            self._req("POST", action, json_body={
                "OpportunityClose": {"subject": f"{opp.name}: {opp.stage.replace('_', ' ')}",
                                     "opportunityid@odata.bind": f"/opportunities({opp.crm_id})",
                                     "actualend": date.today().isoformat()},
                "Status": 3 if opp.stage == "closed_won" else 4})
        return opp.crm_id

    def create_campaign(self, campaign: Campaign, members: list[Contact]) -> str | None:
        body = {"name": campaign.name[:128], "description": campaign.goal[:100000]}
        if campaign.budget:
            body["budgetedcost"] = campaign.budget
        if campaign.start_date:
            body["proposedstart"] = campaign.start_date
        if campaign.end_date:
            body["proposedend"] = campaign.end_date
        new_id = self._req("POST", "campaigns", json_body=body)["campaignid"]
        self.set_ext_id(campaign, new_id)
        return new_id

    def pull_deals(self, db: Session, since: datetime) -> int:
        known = {i for i in db.scalars(select(Opportunity.crm_id).where(Opportunity.crm_id.isnot(None)))}
        if not known:
            return 0
        res = self._req("GET", "opportunities", params={
            "$select": "opportunityid,stepname,estimatedvalue,estimatedclosedate,statecode",
            "$filter": f"modifiedon gt {since.strftime('%Y-%m-%dT%H:%M:%SZ')}"})
        changed = 0
        for r in res.get("value", []):
            if r["opportunityid"] not in known:
                continue
            stage = {1: "Won", 2: "Lost"}.get(r.get("statecode"), r.get("stepname"))  # statecode: 0 open, 1 won, 2 lost
            changed += self.apply_deal_update(db, r["opportunityid"], crm_stage=stage,
                                              amount=r.get("estimatedvalue"), close_date=r.get("estimatedclosedate"))
        return changed
