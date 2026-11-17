"""Buyer Room and buying-group coverage.

Buyer Room: one private page per deal, opened through a secret link (no login). It holds a short summary written for
the buyer, the proposal and deck, matching case studies, and a mutual action plan (steps with owners on both sides and
dates; the buyer can tick steps and comment). Buyers can ask questions: an assistant answers ONLY from the knowledge
base and the room's documents, under the same rules as every draft (banned phrases, no invented figures, no pricing
unless the rep allows it). Anything it can't answer confidently becomes a task for the rep instead.

Every visit (who, which section, how long) is recorded: it shows who on the buying committee is engaged and who
has never looked, and it feeds deal health.

Coverage: for each account, which buying roles are engaged (replies, meetings, events, room visits) against the roles a
deal needs. "Single-threaded" (one engaged person) is the most common reason deals stall.
"""
import secrets as pysecrets
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (Activity, BuyerRoom, Contact, KBDocument, Opportunity, RoomQuestion, RoomStep, RoomView,
                      SalesDocument, Task)

UTC = timezone.utc

DEFAULT_PLAN = [
    ("Discovery call: goals and success criteria", "both", -0.9),
    ("Solution walkthrough with the wider team", "us", -0.7),
    ("Technical and security review", "them", -0.55),
    ("Proposal and commercials reviewed", "them", -0.4),
    ("Legal and procurement sign-off", "them", -0.2),
    ("Contract signed", "them", 0.0),
    ("Kick-off meeting", "both", 0.1),
]


def _aware(dt):
    return None if dt is None else (dt if dt.tzinfo else dt.replace(tzinfo=UTC))


# ------------------------------------------------------------------ create

class _Step(BaseModel):
    title: str
    owner_side: str = Field(description="us | them | both")
    due_in_days: int


class _RoomDraft(BaseModel):
    summary: str = Field(description="4-6 sentences written TO the buyer: their goals, what we propose, the outcome.")
    steps: list[_Step] = Field(description="5-8 mutual action plan steps from now to signature and kick-off.")


ROOM_ROLE = """You prepare a shared deal room for a B2B buyer. Write the summary to the buyer in plain words: what they
told us they want to achieve, what we propose, and the outcome. Use only facts in the deal context; never invent figures,
names or dates. Then propose a realistic mutual action plan from today to signature and kick-off."""


async def create_room(opportunity_id: int, created_by: str, generate: bool = True,
                      parent_run_id: int | None = None) -> int:
    """A room for the deal, with a summary and plan written from the deal (or a standard plan when generate=False)."""
    with session_scope_() as db:
        o = db.get(Opportunity, opportunity_id)
        if not o:
            raise ValueError("Deal not found")
        existing = db.scalar(select(BuyerRoom).where(BuyerRoom.opportunity_id == o.id, BuyerRoom.status == "active"))
        if existing:
            return existing.id
        docs = list(db.scalars(select(SalesDocument).where(SalesDocument.account_id == o.account_id,
                                                          SalesDocument.kind.in_(["proposal", "deck"]))
                               .order_by(SalesDocument.id.desc()).limit(4)))
        cases = list(db.scalars(select(KBDocument).where(KBDocument.category == "case_study").limit(3)))
        close = date.fromisoformat(o.close_date) if o.close_date else date.today() + timedelta(days=45)
        context = {"deal": o.name, "company": o.account.name, "stage": o.stage, "next_steps": o.next_steps,
                   "meddicc": o.qualification, "close_date": close.isoformat(), "today": date.today().isoformat()}
        doc_ids, case_ids = [d.id for d in docs], [c.id for c in cases]
        name, account_id = o.account.name, o.account_id
    days_left = max(14, (close - date.today()).days)
    summary = f"This room brings together everything for {name}: the proposal, examples from similar companies and our shared plan."
    steps = [(t, side, round(days_left * (1 + f))) for t, side, f in DEFAULT_PLAN]
    if generate:
        from ..agents.base import run_agent
        import json

        r = await run_agent(agent="room_builder", model=settings.model_worker, role_prompt=ROOM_ROLE, effort="low",
                            max_tokens=3000, prompt=json.dumps(context, default=str), output_format=_RoomDraft,
                            parent_run_id=parent_run_id)
        summary = r.parsed.summary
        steps = [(s.title, s.owner_side if s.owner_side in ("us", "them", "both") else "both", max(0, s.due_in_days))
                 for s in r.parsed.steps][:10]
    with session_scope_() as db:
        room = BuyerRoom(token=pysecrets.token_urlsafe(24), opportunity_id=opportunity_id, account_id=account_id,
                         title=f"{name} × {settings.company_name}", summary=summary, documents=doc_ids,
                         case_studies=case_ids, created_by=created_by)
        db.add(room)
        db.flush()
        for i, (title, side, due) in enumerate(steps, 1):
            db.add(RoomStep(room_id=room.id, position=i, title=title[:255], owner_side=side,
                            due_date=(date.today() + timedelta(days=due)).isoformat()))
        return room.id


def session_scope_():
    from ..db import session_scope

    return session_scope()


def room_url(room: BuyerRoom) -> str:
    return f"{settings.app_base_url.rstrip('/')}/room/{room.token}"


# ------------------------------------------------------------------ buyer side

def public_view(db: Session, room: BuyerRoom) -> dict:
    docs = [db.get(SalesDocument, i) for i in room.documents or []]
    cases = [db.get(KBDocument, i) for i in room.case_studies or []]
    return {
        "title": room.title, "summary": room.summary, "welcome_note": room.welcome_note,
        "company": settings.company_name, "brand_color": settings.brand_color,
        "documents": [{"id": d.id, "title": d.title, "kind": d.kind, "has_file": bool(d.file_name)} for d in docs if d],
        "case_studies": [{"id": c.id, "title": c.title, "content": c.content[:3000]} for c in cases if c],
        "steps": [{"id": s.id, "position": s.position, "title": s.title, "owner_side": s.owner_side,
                   "owner_name": s.owner_name, "due_date": s.due_date, "status": s.status, "comment": s.comment,
                   "done_by": s.done_by} for s in sorted(room.steps, key=lambda s: s.position)],
        "questions": [{"question": q.question, "answer": q.answer, "pending": q.answer is None, "at": q.created_at}
                      for q in sorted(room.questions, key=lambda q: q.id)],
    }


def identify(db: Session, room: BuyerRoom, visitor: str | None) -> Contact | None:
    """A visitor who typed their work email is matched to a contact at the account (or added as one)."""
    v = (visitor or "").strip().lower()
    if "@" not in v:
        return None
    c = db.scalar(select(Contact).where(Contact.email.ilike(v)))
    if c:
        return c
    known = db.scalar(select(Contact).where(Contact.account_id == room.account_id, Contact.email.is_not(None)))
    if known and known.email.split("@")[-1].lower() == v.split("@")[-1]:
        first = v.split("@")[0].split(".")[0].title()
        c = Contact(account_id=room.account_id, first_name=first, last_name="", email=v, source="buyer room",
                    owner=known.owner)
        db.add(c)
        db.flush()
        return c
    return None


def record_view(db: Session, room: BuyerRoom, visitor: str | None, section: str, seconds: int) -> None:
    c = identify(db, room, visitor)
    db.add(RoomView(room_id=room.id, visitor=(visitor or "anonymous")[:255], contact_id=c.id if c else None,
                    section=section[:40], seconds=max(0, min(seconds, 3600))))
    room.last_viewed_at = datetime.now(UTC)
    if c and not db.scalar(select(Activity.id).where(Activity.contact_id == c.id, Activity.type == "room_view",
                                                     Activity.created_at >= datetime.now(UTC) - timedelta(hours=12))):
        db.add(Activity(contact_id=c.id, type="room_view", detail=f"Opened the buyer room ({section})"))
    from .pipeline_intel import touch_deal

    touch_deal(db, room.account_id)


def update_step(db: Session, room: BuyerRoom, step_id: int, visitor: str | None, done: bool | None,
                comment: str | None) -> RoomStep:
    s = db.get(RoomStep, step_id)
    if not s or s.room_id != room.id:
        raise ValueError("Step not found")
    if done is not None:
        s.status = "done" if done else "open"
        s.done_by = (visitor or "buyer")[:255] if done else None
        s.done_at = datetime.now(UTC) if done else None
    if comment:
        s.comment = comment[:2000]
        o = db.get(Opportunity, room.opportunity_id)
        db.add(Task(type="follow_up", title=f"Buyer commented on \"{s.title}\""[:255], opportunity_id=o.id,
                    contact_id=o.primary_contact_id, owner=o.owner, created_by="buyer_room",
                    due_at=datetime.now(UTC) + timedelta(hours=4), notes=f"{visitor or 'Buyer'}: {comment}"))
    return s


class _Answer(BaseModel):
    answer: str
    confident: bool = Field(description="True only if the sources fully answer the question.")
    is_pricing_question: bool
    sources: list[str] = Field(description="Titles of the documents the answer used.")


ANSWER_ROLE = """You answer a buyer's question inside a deal room, on behalf of the sales team. Use ONLY the sources
provided (our knowledge base and this deal's documents). If they don't fully answer it, say briefly that the team will
follow up, and set confident=false. Never invent figures, customer names, dates or commitments. Plain, friendly, short."""


async def answer(room_id: int, question: str, visitor: str | None, parent_run_id: int | None = None) -> dict:
    """Answer from the knowledge base and room documents, under the rule packs; otherwise hand it to the rep."""
    from . import guardrails
    from ..agents.base import run_agent
    from ..tools import kb

    with session_scope_() as db:
        room = db.get(BuyerRoom, room_id)
        hits = kb.search(db, question, k=6)
        docs = [db.get(SalesDocument, i) for i in room.documents or []]
        sources = [f"### {h['title']}\n{h['text']}" for h in hits]
        sources += [f"### {d.title}\n{(d.content or '')[:4000]}" for d in docs if d]
        allow_pricing = room.allow_pricing
    r = await run_agent(agent="room_assistant", model=settings.model_worker, role_prompt=ANSWER_ROLE, effort="low",
                        max_tokens=1500, output_format=_Answer, parent_run_id=parent_run_id,
                        prompt=f"Question: {question}\n\nSources:\n" + "\n\n".join(sources or ["(none)"]))
    a: _Answer = r.parsed
    with session_scope_() as db:
        room = db.get(BuyerRoom, room_id)
        grounding = "\n".join(sources)
        problems = [f for f in guardrails.check_draft(db, None, "room_answer", None, a.answer, grounding)
                    if f["severity"] == "block"]
        unverified = guardrails.unverified_claims(a.answer, guardrails.grounding_text(db, grounding))
        withheld = (a.is_pricing_question and not allow_pricing) or not a.confident or problems or unverified
        q = RoomQuestion(room_id=room.id, visitor=(visitor or "buyer")[:255], question=question[:2000],
                         answer=None if withheld else a.answer, answered_by=None if withheld else "assistant",
                         sources=a.sources)
        db.add(q)
        o = db.get(Opportunity, room.opportunity_id)
        if withheld:
            why = ("pricing questions go to the rep" if a.is_pricing_question and not allow_pricing
                   else "a rule check stopped the answer" if problems or unverified else "the sources didn't fully answer it")
            db.add(Task(type="follow_up", title=f"Answer a buyer question in the {room.title} room"[:255],
                        opportunity_id=o.id, contact_id=o.primary_contact_id, owner=o.owner, created_by="buyer_room",
                        due_at=datetime.now(UTC) + timedelta(hours=4),
                        notes=f"{visitor or 'Buyer'} asked: {question}\n(Not answered automatically: {why}.)"
                              + (f"\nDraft answer: {a.answer}" if not a.is_pricing_question else "")))
        db.flush()
        return {"answer": q.answer, "pending": q.answer is None,
                "message": None if q.answer else "Thanks, the team will answer this shortly."}


# ------------------------------------------------------------------ rep side

def engagement(db: Session, room: BuyerRoom) -> dict:
    by_person: dict[str, dict] = defaultdict(lambda: {"visits": 0, "seconds": 0, "sections": set(), "last": None})
    for v in room.views:
        p = by_person[v.visitor]
        p["visits"] += 1
        p["seconds"] += v.seconds or 0
        p["sections"].add(v.section)
        when = _aware(v.created_at)
        if when and (p["last"] is None or when > p["last"]):
            p["last"] = when
    steps = room.steps
    return {
        "url": room_url(room), "status": room.status, "allow_pricing": room.allow_pricing,
        "visitors": [{"visitor": k, "visits": p["visits"], "minutes": round(p["seconds"] / 60, 1),
                      "sections": sorted(p["sections"]), "last": p["last"]} for k, p in by_person.items()],
        "plan_progress": {"done": sum(1 for s in steps if s.status == "done"), "total": len(steps)},
        "open_questions": sum(1 for q in room.questions if q.answer is None),
        "last_viewed_at": room.last_viewed_at,
    }


# ------------------------------------------------------------------ buying-group coverage

ROLE_KEYWORDS = {
    "economic_buyer": ("ceo", "chief executive", "president", "owner", "managing director", "general manager", "coo",
                       "chief operating", "founder", "svp", "evp", "vp "),
    "finance": ("cfo", "finance", "financial", "procurement", "purchasing", "controller", "treasur"),
    "technical": ("cto", "cio", "chief technology", "chief information", "it ", "architect", "engineer", "security",
                  "infrastructure", "platform", "developer", "data"),
    "user": ("manager", "director", "head of", "lead", "specialist", "analyst", "operations"),
}
REQUIRED = ("champion", "economic_buyer", "technical", "finance")
ENGAGED_ACTIVITY = ("reply", "reply_interested", "reply_question", "reply_objection", "reply_referral", "meeting",
                    "call_analyzed", "event_attended", "room_view", "web_visit_high")


def roles_for(title: str | None) -> set[str]:
    t = f" {(title or '').lower()} "
    return {role for role, words in ROLE_KEYWORDS.items() if any(w in t for w in words)}


def coverage(db: Session, account_id: int) -> dict:
    contacts = list(db.scalars(select(Contact).where(Contact.account_id == account_id, Contact.status != "disqualified")))
    engagement_count: dict[int, int] = defaultdict(int)
    for a in db.scalars(select(Activity).where(Activity.contact_id.in_([c.id for c in contacts] or [0]),
                                               Activity.type.in_(ENGAGED_ACTIVITY))):
        engagement_count[a.contact_id] += 1
    engaged = [c for c in contacts if engagement_count[c.id]]
    champion = max(engaged, key=lambda c: engagement_count[c.id], default=None)
    roles: dict[str, list[str]] = defaultdict(list)
    for c in engaged:
        for r in roles_for(c.title):
            roles[r].append(c.full_name)
    if champion:
        roles["champion"].append(champion.full_name)
    plan = db.scalar(select(SalesDocument).where(SalesDocument.account_id == account_id,
                                                 SalesDocument.kind == "committee_plan").order_by(SalesDocument.id.desc()))
    missing_from_plan = (plan.data or {}).get("missing_roles", []) if plan else []
    covered = [r for r in REQUIRED if roles.get(r)]
    return {
        "score": round(len(covered) / len(REQUIRED) * 100),
        "engaged_people": len(engaged), "known_people": len(contacts),
        "single_threaded": len(engaged) <= 1,
        "roles": {r: roles.get(r, []) for r in REQUIRED},
        "missing_roles": [r for r in REQUIRED if not roles.get(r)],
        "never_engaged": [c.full_name for c in contacts if not engagement_count[c.id]][:10],
        "roles_to_find": missing_from_plan,
    }
