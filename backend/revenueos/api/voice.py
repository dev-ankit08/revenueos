"""Rep Voice: build, view and switch off a rep's writing style."""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import jobs
from ..db import get_db
from ..models import RepVoice
from ..services import voice
from ..services.approvals import audit
from .workspace import caller

router = APIRouter(prefix="/api/voice", tags=["rep voice"])


def _me(request: Request) -> tuple[str | None, str]:
    c = caller(request)
    user = c.get("user")
    email = user.get("email") if isinstance(user, dict) else None
    return (email.lower() if email else None), c.get("role", "member")


def _allowed(request: Request, owner: str) -> str:
    """A rep manages their own voice; admins can manage anyone's. Returns who is acting."""
    me, role = _me(request)
    if me == owner.lower() or role in ("admin", "owner") or me is None and role == "admin":
        return me or "admin"
    raise HTTPException(403, "Only the rep themselves (or an admin) can change their voice profile.")


@router.get("")
def list_voices(db: Session = Depends(get_db)):
    return [{"owner": v.owner, "enabled": v.enabled, "samples": v.sample_count, "updated_at": v.updated_at,
             "profile": v.profile, "consented_by": v.consented_by} for v in db.scalars(select(RepVoice))]


@router.post("/{owner}/build")
def build(owner: str, request: Request):
    """Learn the rep's style from emails they wrote. Calling this is the rep's consent."""
    who = _allowed(request, owner)

    async def work(run_id: int) -> str:
        r = await voice.build(owner, who)
        return f"Voice profile for {r['owner']} built from {r['samples']} emails."
    return {"run_id": jobs.start("job:rep_voice", f"Learn {owner}'s writing style", work)}


@router.post("/{owner}/{action}")
def toggle(owner: str, action: str, request: Request, db: Session = Depends(get_db)):
    who = _allowed(request, owner)
    v = db.scalar(select(RepVoice).where(RepVoice.owner == owner.lower()))
    if not v:
        raise HTTPException(404, "No voice profile yet. Build one first.")
    if action not in ("enable", "disable", "delete"):
        raise HTTPException(404, "Unknown action")
    if action == "delete":
        db.delete(v)
    else:
        v.enabled = action == "enable"
    audit(db, who, f"voice.{action}", "rep_voice", owner.lower())
    db.commit()
    return {"owner": owner.lower(), "action": action}
