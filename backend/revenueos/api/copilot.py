from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import jobs
from ..agents.orchestrator import run_copilot
from ..config import settings
from ..db import get_db
from ..models import AgentRun

router = APIRouter(prefix="/api", tags=["copilot"])


class CopilotIn(BaseModel):
    message: str = Field(min_length=2, max_length=8000)


@router.post("/copilot")
def copilot(body: CopilotIn):
    async def work(run_id: int) -> str:
        return await run_copilot(body.message, run_id)
    return {"run_id": jobs.start("orchestrator", body.message, work, model=settings.model_orchestrator)}


def run_to_dict(r: AgentRun) -> dict:
    return {
        "id": r.id, "agent": r.agent, "status": r.status, "input": r.input, "output": r.output,
        "error": r.error, "tool_calls": r.tool_calls, "model": r.model, "cost_usd": r.cost_usd,
        "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
        "started_at": r.started_at, "finished_at": r.finished_at, "parent_run_id": r.parent_run_id,
    }


@router.get("/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(AgentRun, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    children = list(db.scalars(select(AgentRun).where(AgentRun.parent_run_id == run_id).order_by(AgentRun.id)))
    return run_to_dict(run) | {"children": [run_to_dict(c) for c in children]}


@router.get("/runs")
def list_runs(limit: int = 50, db: Session = Depends(get_db)):
    stmt = select(AgentRun).where(AgentRun.parent_run_id.is_(None)).order_by(AgentRun.id.desc()).limit(min(limit, 200))
    return [run_to_dict(r) for r in db.scalars(stmt)]
