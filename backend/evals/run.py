"""Quality evals: run the real assistants on golden cases, score them, and compare with the baseline.

    cd backend
    python -m evals.run                         # all suites, compare with evals/baseline.json
    python -m evals.run --suites triage,grader  # some suites
    python -m evals.run --save-baseline         # accept the current scores as the new baseline

Uses a throwaway database (your real data is never touched) and never sends anything: drafts are only
queued, email is forced to dry-run and the CRM to local. Needs ANTHROPIC_API_KEY. Exit code 1 when any
suite scores more than TOLERANCE below its baseline, so CI can block a prompt or model change that makes
an assistant worse. Each run costs a little AI spend (printed at the end).
"""
import argparse
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASELINE = HERE / "baseline.json"
RESULTS = HERE / "results"
TOLERANCE = 0.05
SUITES = ("scorer", "triage", "grader", "writer")


def _isolate() -> None:
    """Point the app at a temporary database and switch off every outbound channel. Call before importing."""
    tmp = Path(tempfile.mkdtemp(prefix="revenueos-evals-"))
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp / 'evals.db'}"
    for key in ("SMTP_HOST", "SENDGRID_API_KEY", "POSTMARK_SERVER_TOKEN", "SF_USERNAME", "SF_PASSWORD",
                "HUBSPOT_TOKEN", "DYNAMICS_URL", "ZOHO_REFRESH_TOKEN", "PIPEDRIVE_TOKEN", "SLACK_BOT_TOKEN"):
        os.environ[key] = ""
    os.environ["EMAIL_PROVIDER"] = "smtp"
    os.environ["CRM_PROVIDER"] = "local"
    os.environ["KB_RERANK"] = os.environ.get("KB_RERANK", "true")
    sys.path.insert(0, str(HERE.parent))


def compare(scores: dict, baseline: dict, tolerance: float = TOLERANCE) -> list[str]:
    """Regressions: suites whose score fell more than `tolerance` below the baseline."""
    out = []
    for suite, result in scores.items():
        base = (baseline.get(suite) or {}).get("score")
        if base is not None and result["score"] < base - tolerance:
            out.append(f"{suite}: {result['score']:.2f} < baseline {base:.2f}")
    return out


def summarise(suite: str, results: list[dict]) -> dict:
    passed = sum(1 for r in results if r["passed"])
    score = sum(r.get("score", 1.0 if r["passed"] else 0.0) for r in results) / len(results) if results else 0.0
    return {"suite": suite, "score": round(score, 3), "passed": passed, "cases": len(results),
            "failures": [r for r in results if not r["passed"]]}


# ------------------------------------------------------------------ suites

async def suite_scorer() -> list[dict]:
    from sqlalchemy import select

    from revenueos.agents import scorer
    from revenueos.api.leads import LeadIn, upsert_lead
    from revenueos.db import session_scope
    from revenueos.models import Contact

    from .cases import SCORER

    out = []
    for case in SCORER:
        with session_scope() as db:
            if "new_lead" in case:
                lead = dict(case["new_lead"])
                if "message" in lead:
                    lead["inbound_message"] = lead.pop("message")
                c = upsert_lead(db, LeadIn(**lead))
                db.flush()
                cid = c.id
            else:
                cid = db.scalar(select(Contact.id).where(Contact.email == case["email"]))
        r = await scorer.score_contact(cid)
        ok = r.tier in case["expect_tiers"]
        out.append({"id": case["id"], "passed": ok, "got": r.tier, "expected": case["expect_tiers"],
                    "why": r.reasons[:3]})
    return out


async def suite_triage() -> list[dict]:
    from sqlalchemy import select

    from revenueos.agents import replies
    from revenueos.db import session_scope
    from revenueos.models import Contact

    from .cases import REPLIES

    with session_scope() as db:
        cid = db.scalar(select(Contact.id).where(Contact.email == "priya.sample@example.com"))
    out = []
    for case_id, text, expected in REPLIES:
        t, _ = await replies.classify_reply(cid, text, "forecast accuracy")
        out.append({"id": case_id, "passed": t.category == expected, "got": t.category, "expected": expected})
    return out


async def suite_grader() -> list[dict]:
    from revenueos.services import personalisation

    from .cases import GRADER

    out = []
    for case in GRADER:
        hi = await personalisation.grade(subject=None, body=case["specific"], recipient=case["recipient"],
                                         evidence=case["evidence"])
        lo = await personalisation.grade(subject=None, body=case["generic"], recipient=case["recipient"],
                                         evidence=case["evidence"])
        gap = hi.score - lo.score
        out.append({"id": case["id"], "passed": gap >= case["min_gap"], "specific": hi.score, "generic": lo.score,
                    "gap": gap})
    return out


async def suite_writer() -> list[dict]:
    from pydantic import BaseModel, Field
    from sqlalchemy import select

    from revenueos.agents import writer
    from revenueos.agents.base import run_agent
    from revenueos.config import settings
    from revenueos.db import session_scope
    from revenueos.models import AccountBrief, Approval, Contact
    from revenueos.services import sources

    from .cases import WRITER

    class Judgement(BaseModel):
        relevance: int = Field(ge=1, le=5, description="Does it address this person's priorities from the brief?")
        specificity: int = Field(ge=1, le=5, description="Could it only have been sent to this person?")
        credibility: int = Field(ge=1, le=5, description="Are all claims supported by the brief/knowledge base?")
        brevity_and_tone: int = Field(ge=1, le=5, description="Short, plain, respectful, a low-friction ask?")
        reasons: list[str]

    out = []
    for case in WRITER:
        with session_scope() as db:
            c = db.scalar(select(Contact).where(Contact.email == case["email"]))
            db.add(AccountBrief(account_id=c.account_id, **case["brief"]))
            sources.record(db, case["brief"]["sources"], agent="evals")  # the brief's pages count as read
            cid = c.id
        await writer.write_sequence(cid, steps=case["steps"])
        with session_scope() as db:
            drafts = list(db.scalars(select(Approval).where(Approval.contact_id == cid, Approval.kind == "email")
                                     .order_by(Approval.sequence_step)))
            checks = {
                "queued_all_steps": len({a.sequence_step for a in drafts}) >= case["steps"],
                "nothing_blocked": all(not any(f["severity"] == "block" for f in a.guardrail_flags) for a in drafts),
                "no_invented_figures": all(not any(f["code"] in ("unverified_claims", "claim_not_in_source")
                                                   for f in a.guardrail_flags) for a in drafts),
                "first_email_short": bool(drafts) and len(drafts[0].body.split()) <= 150,
                "personal_enough": all((a.personalisation_score or 0) >= int(settings.personalisation_threshold)
                                       for a in drafts),
            }
            sequence = "\n\n---\n\n".join(f"Step {a.sequence_step}\nSubject: {a.subject}\n{a.body}" for a in drafts)
        judge = await run_agent(
            agent="eval_judge", model=settings.model_orchestrator, effort="medium", max_tokens=2000,
            role_prompt="You are a demanding VP of Sales reviewing an SDR's outreach sequence. Score strictly.",
            prompt=f"Brief:\n{json.dumps(case['brief'])}\n\nSequence:\n{sequence}", output_format=Judgement)
        j = judge.parsed
        judged = (j.relevance + j.specificity + j.credibility + j.brevity_and_tone) / 20
        rules = sum(checks.values()) / len(checks)
        score = round(0.5 * rules + 0.5 * judged, 3)
        out.append({"id": case["id"], "passed": all(checks.values()) and judged >= 0.7, "score": score,
                    "checks": checks, "judge": j.model_dump()})
    return out


# ------------------------------------------------------------------ runner

async def run(suites: list[str]) -> dict:
    from sqlalchemy import func, select

    from revenueos import seed
    from revenueos.db import init_db, session_scope
    from revenueos.models import AgentRun

    init_db()
    seed.main()
    funcs = {"scorer": suite_scorer, "triage": suite_triage, "grader": suite_grader, "writer": suite_writer}
    scores = {}
    for name in suites:
        try:
            results = await funcs[name]()
        except Exception as exc:  # a crashing suite scores zero rather than hiding
            results = [{"id": "suite", "passed": False, "score": 0.0, "error": f"{type(exc).__name__}: {exc}"}]
        scores[name] = summarise(name, results)
        print(f"{name:8s} {scores[name]['score']:.2f}  ({scores[name]['passed']}/{scores[name]['cases']} passed)")
    with session_scope() as db:
        cost = db.scalar(select(func.coalesce(func.sum(AgentRun.cost_usd), 0.0)))
    return {"run_at": datetime.now(timezone.utc).isoformat(), "cost_usd": round(cost, 4), "suites": scores}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--suites", default=",".join(SUITES))
    parser.add_argument("--save-baseline", action="store_true")
    args = parser.parse_args(argv)
    suites = [s.strip() for s in args.suites.split(",") if s.strip() in SUITES]

    _isolate()
    report = asyncio.run(run(suites))
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"AI spend: ${report['cost_usd']}  ·  report: {out}")

    if args.save_baseline:
        baseline = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
        baseline.update({k: {"score": v["score"], "saved_at": report["run_at"]} for k, v in report["suites"].items()})
        BASELINE.write_text(json.dumps(baseline, indent=2), encoding="utf-8")
        print(f"Baseline saved: {BASELINE}")
        return 0
    if not BASELINE.exists():
        print("No baseline yet. Review the report, then run with --save-baseline.")
        return 0
    regressions = compare(report["suites"], json.loads(BASELINE.read_text(encoding="utf-8")))
    for r in regressions:
        print(f"REGRESSION  {r}")
    return 1 if regressions else 0


if __name__ == "__main__":
    sys.exit(main())
