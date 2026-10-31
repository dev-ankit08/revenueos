"""Blind evaluation kit: do reps prefer RevenueOS drafts over the current approach, for the same leads?

Each item shows two emails as "A" and "B" in random order; raters never see which is which.
Results reveal the sides and apply a two-sided sign test (ties excluded).
"""
import math
import random

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Approval, Contact, EvalItem, EvalRating, Evaluation

router = APIRouter(prefix="/api/evaluations", tags=["evaluation"])


class IncumbentEmail(BaseModel):
    contact_id: int
    subject: str | None = None
    body: str = Field(min_length=10)


class EvaluationIn(BaseModel):
    name: str = Field(min_length=2)
    incumbent_label: str = "Current approach"
    items: list[IncumbentEmail] = Field(min_length=1)


def _revenueos_draft(db: Session, contact_id: int) -> Approval | None:
    """The first email RevenueOS wrote for this person (step 1, variant actually used)."""
    return db.scalar(select(Approval).where(
        Approval.contact_id == contact_id, Approval.kind == "email", Approval.sequence_step == 1,
        Approval.status.in_(["pending", "approved", "sent"])).order_by(Approval.id))


@router.post("")
def create(body: EvaluationIn, db: Session = Depends(get_db)):
    ev = Evaluation(name=body.name, incumbent_label=body.incumbent_label)
    db.add(ev)
    db.flush()
    added, skipped = 0, []
    for it in body.items:
        c = db.get(Contact, it.contact_id)
        draft = _revenueos_draft(db, it.contact_id)
        if not c or not draft:
            skipped.append(it.contact_id)  # write a RevenueOS sequence for them first
            continue
        db.add(EvalItem(evaluation_id=ev.id, contact_id=it.contact_id, revenueos_subject=draft.subject,
                        revenueos_body=draft.body, incumbent_subject=it.subject, incumbent_body=it.body,
                        revenueos_is_a=random.random() < 0.5))
        added += 1
    if not added:
        db.rollback()
        raise HTTPException(422, "None of these contacts has a RevenueOS draft yet. Write sequences for them first.")
    db.commit()
    return {"id": ev.id, "items": added, "skipped_contacts_without_draft": skipped}


@router.get("")
def list_evaluations(db: Session = Depends(get_db)):
    out = []
    for ev in db.scalars(select(Evaluation).order_by(Evaluation.id.desc())):
        items = list(db.scalars(select(EvalItem.id).where(EvalItem.evaluation_id == ev.id)))
        ratings = db.scalars(select(EvalRating).where(EvalRating.item_id.in_(items))).all() if items else []
        out.append({"id": ev.id, "name": ev.name, "incumbent_label": ev.incumbent_label, "items": len(items),
                    "ratings": len(ratings), "raters": len({r.rater for r in ratings}), "created_at": ev.created_at})
    return out


@router.get("/{eval_id}")
def blind_items(eval_id: int, rater: str | None = None, db: Session = Depends(get_db)):
    """Items for rating: A and B only, no sources. `rater` marks which they already rated."""
    if not db.get(Evaluation, eval_id):
        raise HTTPException(404, "Evaluation not found")
    out = []
    for it in db.scalars(select(EvalItem).where(EvalItem.evaluation_id == eval_id).order_by(EvalItem.id)):
        c = db.get(Contact, it.contact_id)
        a = (it.revenueos_subject, it.revenueos_body) if it.revenueos_is_a else (it.incumbent_subject, it.incumbent_body)
        b = (it.incumbent_subject, it.incumbent_body) if it.revenueos_is_a else (it.revenueos_subject, it.revenueos_body)
        mine = db.scalar(select(EvalRating).where(EvalRating.item_id == it.id, EvalRating.rater == rater)) if rater else None
        out.append({"item_id": it.id, "recipient": f"{c.full_name}, {c.title or ''} at {c.account.name}",
                    "a": {"subject": a[0], "body": a[1]}, "b": {"subject": b[0], "body": b[1]},
                    "your_choice": mine.choice if mine else None})
    return out


class RatingIn(BaseModel):
    rater: str = Field(min_length=2)
    choice: str = Field(pattern="^(A|B|tie)$")
    comment: str | None = None


@router.post("/{eval_id}/items/{item_id}/rate")
def rate(eval_id: int, item_id: int, body: RatingIn, db: Session = Depends(get_db)):
    it = db.get(EvalItem, item_id)
    if not it or it.evaluation_id != eval_id:
        raise HTTPException(404, "Item not found")
    r = db.scalar(select(EvalRating).where(EvalRating.item_id == item_id, EvalRating.rater == body.rater)) \
        or EvalRating(item_id=item_id, rater=body.rater)
    r.choice, r.comment = body.choice, body.comment
    db.add(r)
    db.commit()
    return {"ok": True}


def sign_test_p(wins: int, losses: int) -> float | None:
    """Two-sided exact binomial (sign) test, p = 0.5."""
    n = wins + losses
    if n == 0:
        return None
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def results_for(db: Session, eval_id: int) -> dict:
    ev = db.get(Evaluation, eval_id)
    if not ev:
        raise HTTPException(404, "Evaluation not found")
    items = {it.id: it for it in db.scalars(select(EvalItem).where(EvalItem.evaluation_id == eval_id))}
    wins = losses = ties = 0
    comments = []
    for r in db.scalars(select(EvalRating).where(EvalRating.item_id.in_(items))) if items else []:
        it = items[r.item_id]
        if r.choice == "tie":
            ties += 1
        elif (r.choice == "A") == it.revenueos_is_a:
            wins += 1
        else:
            losses += 1
        if r.comment:
            comments.append({"rater": r.rater, "preferred": "tie" if r.choice == "tie" else
                             ("RevenueOS" if (r.choice == "A") == it.revenueos_is_a else ev.incumbent_label),
                             "comment": r.comment})
    decided = wins + losses
    p = sign_test_p(wins, losses)
    win_rate = round(100 * wins / decided, 1) if decided else None
    if decided < 10:
        verdict = "Not enough ratings yet (aim for 30+ decided comparisons)."
    elif p is not None and p < 0.05:
        verdict = (f"Raters prefer {'RevenueOS' if wins > losses else ev.incumbent_label} "
                   f"({win_rate}% RevenueOS win rate, p={p:.3f}).")
    else:
        verdict = f"No clear preference yet ({win_rate}% RevenueOS win rate, p={p:.2f})."
    return {"id": ev.id, "name": ev.name, "incumbent_label": ev.incumbent_label, "revenueos_wins": wins,
            "incumbent_wins": losses, "ties": ties, "win_rate": win_rate, "p_value": p, "verdict": verdict,
            "comments": comments[:50]}


@router.get("/{eval_id}/results")
def results(eval_id: int, db: Session = Depends(get_db)):
    return results_for(db, eval_id)
