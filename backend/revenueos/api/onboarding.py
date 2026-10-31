"""Onboarding a new client workspace: a checklist the wizard walks through, plus its helper actions."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import jobs
from ..agents import workflows
from ..config import settings
from ..db import get_db
from ..models import Connection, Contact, KBDocument, SlackChannel
from ..tools import kb
from ..tools.crm import get_crm
from .workspace import require_admin

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])
SAMPLE_MARK = "SAMPLE CONTENT"


@router.get("/status")
def status(db: Session = Depends(get_db)):
    docs = list(db.scalars(select(KBDocument)))
    real = [d for d in docs if SAMPLE_MARK not in d.content]
    cats = {d.category for d in real}
    real_leads = db.scalar(select(func.count()).select_from(Contact).where(~Contact.email.like("%@example.com"))) or 0
    connected = bool(db.scalar(select(Connection.id).where(Connection.status == "active"))) or \
        bool(db.scalar(select(SlackChannel.id)))
    piloting = bool(db.scalar(select(Contact.id).where(Contact.experiment_group.isnot(None))))
    steps = [
        {"key": "company", "title": "Company details", "done": settings.company_name != "Your Company"
         and "Example St" not in settings.physical_address,
         "detail": f"{settings.company_name} · sends as {settings.sender_email}"},
        {"key": "crm", "title": "Connect your CRM", "done": settings.crm != "local" or "crm_provider" in _own(),
         "detail": f"Using {settings.crm}"},
        {"key": "knowledge", "title": "Teach it about your company",
         "done": {"offering", "case_study"} <= cats and len(real) >= 3,
         "detail": f"{len(real)} of your own documents ({len(docs) - len(real)} sample documents left)"},
        {"key": "leads", "title": "Bring in leads", "done": real_leads > 0, "detail": f"{real_leads} real leads"},
        {"key": "integrations", "title": "Connect email, calendar, Slack (optional)", "done": connected, "optional": True,
         "detail": "Makes every draft draw on real conversations"},
        {"key": "pilot", "title": "Start a pilot with a control group", "done": piloting,
         "detail": "Proves the lift with your own numbers"},
    ]
    required = [s for s in steps if not s.get("optional")]
    return {"steps": steps, "complete": all(s["done"] for s in required)}


def _own() -> dict:
    from ..db import current_workspace
    from ..services import workspaces

    return workspaces.overrides(current_workspace.get())


@router.post("/test-crm", dependencies=[Depends(require_admin)])
def test_crm():
    crm = get_crm()
    try:
        detail = crm.ping()
    except Exception as exc:
        raise HTTPException(422, f"{crm.name}: {str(exc)[:300]}")
    return {"crm": crm.name, "ok": True, "detail": detail}


class StarterKB(BaseModel):
    offering: str | None = None
    icp: str | None = None
    case_study: str | None = None
    brand_voice: str | None = None
    remove_samples: bool = True


@router.post("/knowledge", dependencies=[Depends(require_admin)])
def starter_knowledge(body: StarterKB, db: Session = Depends(get_db)):
    """The four documents that matter most, in one step; optionally clears the sample content."""
    added = []
    for category, title in (("offering", "What we sell"), ("icp", "Ideal customer profile"),
                            ("case_study", "Customer result"), ("brand_voice", "How we write")):
        text = getattr(body, category)
        if text and text.strip():
            kb.add_document(db, title, category, text.strip())
            added.append(category)
    removed = 0
    if body.remove_samples:
        for d in db.scalars(select(KBDocument).where(KBDocument.content.contains(SAMPLE_MARK))):
            db.delete(d)
            removed += 1
    db.commit()
    return {"added": added, "samples_removed": removed}


class PilotIn(BaseModel):
    contact_ids: list[int] | None = None
    max_leads: int = Field(default=50, ge=5, le=500)
    steps: int = Field(default=3, ge=1, le=6)


@router.post("/pilot", dependencies=[Depends(require_admin)])
def start_pilot(body: PilotIn, db: Session = Depends(get_db)):
    """Outbound on the chosen (or newest unworked) leads; a random share is held back as the control group."""
    ids = body.contact_ids or list(db.scalars(
        select(Contact.id).where(Contact.status == "new", Contact.experiment_group.is_(None))
        .order_by(Contact.id.desc()).limit(body.max_leads)))
    if not ids:
        raise HTTPException(422, "No new leads to pilot on. Import or add leads first.")

    async def work(run_id: int) -> str:
        return await workflows.outbound_batch(ids, body.steps, parent_run_id=run_id)
    return {"run_id": jobs.start("job:pilot", f"Pilot on {len(ids)} leads", work), "leads": len(ids),
            "holdout_pct": settings.holdout_pct}
