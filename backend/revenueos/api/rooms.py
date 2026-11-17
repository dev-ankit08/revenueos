"""Phase C endpoints: Buyer Rooms (rep side and the public buyer page), buying-group coverage, the ROI Statement,
the knowledge base built from the website, and imported sequences."""
import csv
import io

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import jobs
from ..config import settings
from ..db import get_db
from ..models import BuyerRoom, KBDraft, Opportunity, RoomQuestion, SalesDocument
from ..services import buyer_rooms as rooms_svc
from ..services import roi_statement
from ..services.approvals import audit
from .workspace import caller, require_admin

router = APIRouter(prefix="/api", tags=["buyer rooms"])


def _actor(request: Request) -> str:
    user = caller(request).get("user")
    return (user.get("name") or user.get("email")) if isinstance(user, dict) else str(user or "anonymous")


# ------------------------------------------------------------------ rep side

class RoomIn(BaseModel):
    generate: bool = True  # write the summary and plan with AI from the deal; False = standard plan


@router.post("/deals/{opp_id}/room")
def create_room(opp_id: int, body: RoomIn, request: Request, db: Session = Depends(get_db)):
    if not db.get(Opportunity, opp_id):
        raise HTTPException(404, "Deal not found")
    who = _actor(request)

    async def work(run_id: int) -> str:
        rid = await rooms_svc.create_room(opp_id, who, generate=body.generate, parent_run_id=run_id)
        return f"room:{rid}"
    return {"run_id": jobs.start("job:buyer_room", f"Buyer room for deal {opp_id}", work)}


def _room(db: Session, room_id: int) -> BuyerRoom:
    r = db.get(BuyerRoom, room_id)
    if not r:
        raise HTTPException(404, "Room not found")
    return r


@router.get("/deals/{opp_id}/room")
def get_deal_room(opp_id: int, db: Session = Depends(get_db)):
    r = db.scalar(select(BuyerRoom).where(BuyerRoom.opportunity_id == opp_id, BuyerRoom.status == "active"))
    if not r:
        return None
    pending = [{"id": q.id, "question": q.question, "visitor": q.visitor, "at": q.created_at}
               for q in r.questions if q.answer is None]
    return {"id": r.id, **rooms_svc.engagement(db, r), **rooms_svc.public_view(db, r), "pending_questions": pending}


class RoomPatch(BaseModel):
    summary: str | None = None
    welcome_note: str | None = None
    allow_pricing: bool | None = None
    status: str | None = Field(default=None, pattern="^(active|archived)$")


@router.patch("/rooms/{room_id}")
def update_room(room_id: int, body: RoomPatch, request: Request, db: Session = Depends(get_db)):
    r = _room(db, room_id)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(r, k, v)
    audit(db, _actor(request), "room.update", "room", r.id, **body.model_dump(exclude_none=True))
    db.commit()
    return {"id": r.id, **rooms_svc.engagement(db, r)}


class AnswerIn(BaseModel):
    answer: str


@router.post("/rooms/{room_id}/questions/{qid}/answer")
def answer_question(room_id: int, qid: int, body: AnswerIn, request: Request, db: Session = Depends(get_db)):
    q = db.get(RoomQuestion, qid)
    if not q or q.room_id != room_id:
        raise HTTPException(404, "Question not found")
    q.answer, q.answered_by = body.answer, _actor(request)
    db.commit()
    return {"id": q.id, "answer": q.answer}


@router.get("/accounts/{account_id}/coverage")
def account_coverage(account_id: int, db: Session = Depends(get_db)):
    return rooms_svc.coverage(db, account_id)


# ------------------------------------------------------------------ the buyer's page (public: the secret link is the key)

def _public_room(db: Session, token: str) -> BuyerRoom:
    r = db.scalar(select(BuyerRoom).where(BuyerRoom.token == token, BuyerRoom.status == "active"))
    if not r:
        raise HTTPException(404, "This room isn't available.")
    return r


@router.get("/public/rooms/{token}")
def public_room(token: str, db: Session = Depends(get_db)):
    return rooms_svc.public_view(db, _public_room(db, token))


class ViewIn(BaseModel):
    visitor: str | None = None
    section: str = "overview"
    seconds: int = 0


@router.post("/public/rooms/{token}/view")
def public_view_event(token: str, body: ViewIn, db: Session = Depends(get_db)):
    rooms_svc.record_view(db, _public_room(db, token), body.visitor, body.section, body.seconds)
    db.commit()
    return {"ok": True}


class StepIn(BaseModel):
    visitor: str | None = None
    done: bool | None = None
    comment: str | None = Field(default=None, max_length=2000)


@router.post("/public/rooms/{token}/steps/{step_id}")
def public_step(token: str, step_id: int, body: StepIn, db: Session = Depends(get_db)):
    room = _public_room(db, token)
    try:
        s = rooms_svc.update_step(db, room, step_id, body.visitor, body.done, body.comment)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    db.commit()
    return {"id": s.id, "status": s.status}


class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    visitor: str | None = None


@router.post("/public/rooms/{token}/ask")
async def public_ask(token: str, body: AskIn, db: Session = Depends(get_db)):
    room = _public_room(db, token)
    if sum(1 for q in room.questions if q.visitor == (body.visitor or "buyer")) >= 30:
        raise HTTPException(429, "Thanks for all the questions; the team will pick these up with you directly.")
    return await rooms_svc.answer(room.id, body.question, body.visitor)


@router.get("/public/rooms/{token}/documents/{doc_id}")
def public_document(token: str, doc_id: int, db: Session = Depends(get_db)):
    room = _public_room(db, token)
    if doc_id not in (room.documents or []):
        raise HTTPException(404, "Not in this room")
    d = db.get(SalesDocument, doc_id)
    if not d or not d.file_name:
        raise HTTPException(404, "No file")
    path = (settings.generated_dir / d.file_name).resolve()
    if settings.generated_dir.resolve() not in path.parents or not path.exists():
        raise HTTPException(404, "File missing")
    rooms_svc.record_view(db, room, None, "documents", 0)
    db.commit()
    return FileResponse(path, filename=d.file_name)


# ------------------------------------------------------------------ ROI statement

@router.get("/insights/roi-statement")
def roi_json(days: int = 30, db: Session = Depends(get_db)):
    r = roi_statement.build(db, days=days)
    return {**r, "markdown": roi_statement.to_markdown(r)}


@router.get("/insights/roi-statement.docx")
def roi_docx(days: int = 30, db: Session = Depends(get_db)):
    path = roi_statement.to_docx(roi_statement.build(db, days=days))
    return FileResponse(path, filename=path.name)


@router.post("/insights/roi-statement/send", dependencies=[Depends(require_admin)])
def roi_send(db: Session = Depends(get_db)):
    n = roi_statement.send_monthly(db)
    if not n:
        raise HTTPException(409, "Add recipients in Settings (ROI statement recipients) first.")
    return {"sent_to": n}


# ------------------------------------------------------------------ onboarding: KB from the website, imports

class SiteIn(BaseModel):
    url: str = Field(min_length=4)


@router.post("/onboarding/kb-from-website", dependencies=[Depends(require_admin)])
def kb_from_website(body: SiteIn):
    from ..agents import kb_builder

    url = body.url if body.url.startswith("http") else f"https://{body.url}"

    async def work(run_id: int) -> str:
        r = await kb_builder.build_from_website(url, parent_run_id=run_id)
        return f"{r['drafts']} knowledge-base drafts ready for review"
    return {"run_id": jobs.start("job:kb_builder", f"Build knowledge base from {url}", work)}


@router.get("/kb/drafts")
def kb_drafts(status: str = "pending", db: Session = Depends(get_db)):
    return [{"id": d.id, "category": d.category, "title": d.title, "content": d.content, "source_urls": d.source_urls,
             "status": d.status, "created_at": d.created_at}
            for d in db.scalars(select(KBDraft).where(KBDraft.status == status).order_by(KBDraft.category, KBDraft.id))]


class DraftDecision(BaseModel):
    content: str | None = None  # edited text, if changed


@router.post("/kb/drafts/{draft_id}/{action}", dependencies=[Depends(require_admin)])
def kb_draft_action(draft_id: int, action: str, body: DraftDecision, request: Request, db: Session = Depends(get_db)):
    from ..tools import kb

    d = db.get(KBDraft, draft_id)
    if not d or d.status != "pending":
        raise HTTPException(404, "Draft not found")
    if action == "reject":
        d.status = "rejected"
    elif action == "approve":
        content = body.content or d.content
        if d.source_urls:
            content += "\n\nSources: " + ", ".join(d.source_urls)
        doc = kb.add_document(db, d.title, d.category, content)
        d.status = "approved"
        audit(db, _actor(request), "kb.approve_draft", "kb_document", doc.id, title=d.title)
    else:
        raise HTTPException(404, "Unknown action")
    db.commit()
    return {"id": d.id, "status": d.status}


@router.post("/onboarding/import-sequences", dependencies=[Depends(require_admin)])
async def import_sequences(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Your existing sequences (Outreach, Salesloft or HubSpot export: step/day/subject/body columns) become a playbook
    document, so the writers learn what your team already sends."""
    from ..tools import kb

    text = (await file.read()).decode("utf-8-sig", errors="replace")
    rows, out = 0, []
    for row in csv.DictReader(io.StringIO(text)):
        r = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        body = r.get("body") or r.get("email body") or r.get("template body") or r.get("content")
        if not body:
            continue
        rows += 1
        seq = r.get("sequence") or r.get("sequence name") or r.get("cadence") or "Sequence"
        step = r.get("step") or r.get("step number") or r.get("position") or str(rows)
        day = r.get("day") or r.get("delay") or r.get("wait days") or ""
        out.append(f"### {seq}, step {step}" + (f" (day {day})" if day else "")
                   + f"\nSubject: {r.get('subject') or r.get('email subject') or ''}\n{body[:2000]}")
    if not rows:
        raise HTTPException(422, "No rows with a body/content column found.")
    doc = kb.add_document(db, "Our existing sequences (imported)", "playbook",
                          "What our team already sends. Keep what works; improve on it.\n\n" + "\n\n".join(out))
    db.commit()
    return {"steps_imported": rows, "document_id": doc.id}
