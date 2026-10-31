"""The numbers that show whether the platform is earning its keep."""
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AccountBrief, Activity, AgentRun, Approval, Contact
from ..services.approvals import AB_HOLDOUT_NOTE

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

# Rough manual effort each agent output replaces, in minutes. Tune to your team.
MINUTES_SAVED = {"account_brief": 30, "draft": 10, "score": 3}


@router.get("")
def summary(db: Session = Depends(get_db)):
    def count(model, *where):
        return db.scalar(select(func.count()).select_from(model).where(*where)) or 0

    by_status = dict(db.execute(select(Contact.status, func.count()).group_by(Contact.status)).all())
    by_tier = {str(k): v for k, v in db.execute(select(Contact.score_tier, func.count()).group_by(Contact.score_tier)).all()}
    # A/B holdouts are auto-rejected by design; they say nothing about draft quality.
    approvals = dict(db.execute(
        select(Approval.status, func.count())
        .where((Approval.review_note.is_(None)) | (Approval.review_note != AB_HOLDOUT_NOTE))
        .group_by(Approval.status)).all())
    sent = approvals.get("sent", 0)
    replied = count(Activity, Activity.type == "reply")
    decided = sum(approvals.get(s, 0) for s in ("sent", "approved", "rejected"))
    approved_like = sum(approvals.get(s, 0) for s in ("sent", "approved"))

    agent_cost = [
        {"agent": a, "runs": n, "cost_usd": round(c or 0, 4)}
        for a, n, c in db.execute(
            select(AgentRun.agent, func.count(), func.sum(AgentRun.cost_usd))
            .where(~AgentRun.agent.startswith("job:")).group_by(AgentRun.agent)
        ).all()
    ]
    total_cost = sum(r["cost_usd"] for r in agent_cost)
    briefs, drafts, scores = count(AccountBrief), count(Approval), count(Contact, Contact.score.isnot(None))
    hours_saved = (briefs * MINUTES_SAVED["account_brief"] + drafts * MINUTES_SAVED["draft"]
                   + scores * MINUTES_SAVED["score"]) / 60

    return {
        "leads": {"total": sum(by_status.values()), "by_status": by_status, "by_tier": by_tier},
        "outreach": {
            "drafts": drafts, "pending_review": approvals.get("pending", 0), "sent": sent,
            "approval_rate": round(approved_like / decided, 3) if decided else None,
            "reply_rate": round(replied / sent, 3) if sent else None,
            "meetings": by_status.get("meeting", 0),
        },
        "ai": {
            "briefs": briefs, "total_cost_usd": round(total_cost, 2), "by_agent": agent_cost,
            "est_hours_saved": round(hours_saved, 1),
            "cost_per_sent_email": round(total_cost / sent, 3) if sent else None,
        },
    }
