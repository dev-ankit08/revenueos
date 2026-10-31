"""Pipedrive adapter (API v1, personal or company API token in PIPEDRIVE_TOKEN).

Pipedrive leads have no status field and deal stages are numeric ids per pipeline, so:
- lead status is not written back (notes and deals still are);
- set CRM_FIELD_MAP {"deal_stage": {"proposal": "12", ...}} with your stage ids to move deal stages.
Won / lost always sync, through the deal's status.
"""
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import settings
from ...integrations import http
from ...models import Contact, Opportunity
from .base import CRMAdapter

log = logging.getLogger(__name__)


class PipedriveCRM(CRMAdapter):
    name = "pipedrive"
    STAGE_MAP: dict[str, str] = {}  # stage ids are account-specific: provide via CRM_FIELD_MAP

    def _req(self, method: str, path: str, params: dict | None = None, **kw):
        if not settings.pipedrive_token:
            raise RuntimeError("PIPEDRIVE_TOKEN is not set.")
        res = http.request(method, f"https://{settings.pipedrive_domain}/v1/{path}",
                           params={**(params or {}), "api_token": settings.pipedrive_token}, **kw)
        if isinstance(res, dict) and res.get("success") is False:
            raise RuntimeError(f"Pipedrive: {res.get('error')}")
        return res

    def ping(self) -> str:
        me = self._req("GET", "users/me").get("data", {})
        return f"Connected as {me.get('name', 'Pipedrive user')} ({me.get('company_name', '')})"

    def import_open_leads(self, db: Session, limit: int = 200) -> int:
        """Leads Inbox items that are not archived, with their person and organisation."""
        created = 0
        for lead in self._req("GET", "leads", {"archived_status": "not_archived", "limit": min(limit, 500)}).get("data") or []:
            if not lead.get("person_id"):
                continue
            person = self._req("GET", f"persons/{lead['person_id']}").get("data") or {}
            org = (self._req("GET", f"organizations/{lead['organization_id']}").get("data") or {}) \
                if lead.get("organization_id") else {}
            email = next((e.get("value") for e in person.get("email") or [] if e.get("value")), None)
            name = (person.get("name") or "Unknown").split(" ", 1)
            created += self.local_contact(
                db, ext_id=str(person["id"]), first_name=name[0] if len(name) > 1 else "", last_name=name[-1],
                company=org.get("name") or "Unknown company", email=email, description=lead.get("title"))
        return created

    def log_activity(self, contact: Contact, subject: str, description: str) -> str | None:
        if not contact.crm_id:
            return None
        res = self._req("POST", "notes", json_body={"content": f"<b>{subject}</b><br>{description[:60000]}",
                                                    "person_id": int(contact.crm_id)})
        return str((res.get("data") or {}).get("id"))

    def update_lead_status(self, contact: Contact, status: str, score: float | None) -> None:
        return None  # Pipedrive leads have no status field

    def upsert_opportunity(self, opp: Opportunity) -> str | None:
        body = {"title": opp.name[:255]}
        if opp.amount:
            body["value"] = opp.amount
        if opp.close_date:
            body["expected_close_date"] = opp.close_date
        if opp.stage == "closed_won":
            body["status"] = "won"
        elif opp.stage == "closed_lost":
            body["status"] = "lost"
        elif opp.stage in self.stage_map:
            body["stage_id"] = int(self.stage_map[opp.stage])
        if opp.primary_contact and opp.primary_contact.crm_id:
            body["person_id"] = int(opp.primary_contact.crm_id)
        if opp.crm_id:
            self._req("PUT", f"deals/{opp.crm_id}", json_body=body)
            return opp.crm_id
        new_id = str(self._req("POST", "deals", json_body=body)["data"]["id"])
        self.set_ext_id(opp, new_id)
        return new_id

    def pull_deals(self, db: Session, since: datetime) -> int:
        known = {i for i in db.scalars(select(Opportunity.crm_id).where(Opportunity.crm_id.isnot(None)))}
        if not known:
            return 0
        res = self._req("GET", "recents", {"since_timestamp": since.strftime("%Y-%m-%d %H:%M:%S"), "items": "deal"})
        changed = 0
        for item in res.get("data") or []:
            d = item.get("data") or {}
            if str(d.get("id")) not in known:
                continue
            stage = {"won": self.stage_map.get("closed_won", "won"), "lost": self.stage_map.get("closed_lost", "lost")}.get(
                d.get("status"), str(d.get("stage_id")) if d.get("stage_id") is not None else None)
            changed += self.apply_deal_update(db, str(d["id"]), crm_stage=stage, amount=d.get("value"),
                                              close_date=d.get("expected_close_date"))
        return changed

    def stage_from_crm(self, value: str | None) -> str | None:
        if value == "won":
            return "closed_won"
        if value == "lost":
            return "closed_lost"
        return super().stage_from_crm(value)
