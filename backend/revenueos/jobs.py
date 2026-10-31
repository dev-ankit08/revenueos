"""Background jobs: long agent work runs off the request path; the UI polls the run row."""
import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from .db import current_workspace, session_scope, use_workspace
from .models import AgentRun

log = logging.getLogger(__name__)
_tasks: set[asyncio.Task] = set()
_loop: asyncio.AbstractEventLoop | None = None


def start(
    name: str, description: str, work: Callable[[int], Awaitable[str | None]], model: str = "-"
) -> int:
    """Create a run row, then run `work(run_id)` in the background. Returns the run id to poll.

    Agents started inside `work` should pass `parent_run_id=run_id` so the UI can show the tree.
    """
    ws = current_workspace.get()  # the job runs in the workspace that started it
    with session_scope() as db:
        run = AgentRun(agent=name, model=model, input=description)
        db.add(run)
        db.flush()
        run_id = run.id

    async def _wrapper() -> None:
        current_workspace.set(ws)
        output, error = None, None
        try:
            output = await work(run_id)
        except Exception as exc:
            log.exception("job %s failed", run_id)
            error = f"{type(exc).__name__}: {exc}"
        with session_scope() as db:
            run = db.get(AgentRun, run_id)
            run.status = "failed" if error else "succeeded"
            run.output = output if output is not None else run.output
            run.error = error or run.error
            run.finished_at = run.finished_at or datetime.now(timezone.utc)

    try:
        asyncio.get_running_loop()
        task = asyncio.create_task(_wrapper())
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    except RuntimeError:
        # Called from a sync endpoint's worker thread: hand the job to the server's loop.
        if _loop is None:
            raise RuntimeError("jobs.bind_loop() was not called at startup")
        asyncio.run_coroutine_threadsafe(_wrapper(), _loop)
    return run_id


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


async def scheduler_loop(interval_seconds: int = 900) -> None:
    """Every tick, for every workspace: release due follow-ups, process context, daily hygiene,
    and the opt-in scheduled scans/reviews."""
    from .services import workspaces

    state: dict[int | None, dict] = {}
    while True:
        try:
            ids = workspaces.all_context_ids()
        except Exception:
            log.exception("could not list workspaces")
            ids = [None]
        for ws in ids:
            with use_workspace(ws):
                await _tick(state.setdefault(ws, {}))
        await asyncio.sleep(interval_seconds)


async def _tick(st: dict) -> None:
    from .config import settings
    from .services import hygiene, memory
    from .services.approvals import release_due_followups

    try:
        with session_scope() as db:
            sent = release_due_followups(db)
        if sent:
            log.info("released %d follow-up emails", sent)
    except Exception:
        log.exception("follow-up scheduler failed")
    try:
        await _context_tick(st)
    except Exception:
        log.exception("context processing failed")
    now = datetime.now(timezone.utc)
    try:
        if st.get("hygiene_day") != now.date():
            with session_scope() as db:
                hygiene.save_report(db, hygiene.find_issues(db))
                memory.purge_raw_text(db)
            st["hygiene_day"] = now.date()
    except Exception:
        log.exception("hygiene report failed")
    for key, interval, name, desc, fn in (
        ("signals", settings.signal_scan_interval_days, "job:signals", "Scheduled signal scan", _scheduled_scan),
        ("review", settings.review_interval_days, "job:revenue_review", "Scheduled revenue review", _scheduled_review),
    ):
        try:
            if interval and (st.get(key) is None or (now - st[key]).days >= interval):
                st[key] = now
                start(name, desc, fn)
        except Exception:
            log.exception("%s failed", name)


async def _scheduled_scan(run_id: int) -> str:
    from .agents.intel import detect_signals

    r = await detect_signals(parent_run_id=run_id)
    return f"{r.get('signals', 0)} new signals across {r.get('accounts_scanned', 0)} accounts"


async def _scheduled_review(run_id: int) -> str:
    """Deal reviews, then a forecast built on them, then the learning loop."""
    from .agents import strategy

    deals = await strategy.review_pipeline(parent_run_id=run_id)
    fc = await strategy.forecast(parent_run_id=run_id)
    learned = await strategy.learn_from_results(parent_run_id=run_id)
    return f"{deals} Forecast: {fc['headline']} Playbook: {'updated' if learned['updated'] else learned['reason']}"


async def _context_tick(st: dict) -> None:
    """Start background jobs only when there is work: Slack digests and due meeting preps."""
    from sqlalchemy import select

    from .integrations import calendar
    from .models import ContextEvent

    with session_scope() as db:
        slack_waiting = db.scalar(select(ContextEvent.id).where(
            ContextEvent.source == "slack", ContextEvent.processed.is_(False), ContextEvent.account_id.isnot(None)))
        preps_due = calendar.due_for_prep(db)
    if slack_waiting:
        start("job:slack_digest", "Summarise new Slack messages", _slack_digest)
    if not st.get("sync_running") and _has_connections():
        st["sync_running"] = True

        async def _sync(run_id: int, _st=st) -> str:
            try:
                return await _scheduled_sync(run_id)
            finally:
                _st["sync_running"] = False
        start("job:sync", "Sync mail, calendars and call recordings", _sync)
    if preps_due:
        start("job:meeting_prep_auto", f"Prep {len(preps_due)} upcoming customer meetings", calendar.run_due_preps)


async def _slack_digest(run_id: int) -> str:
    from .agents.context import digest_slack

    r = await digest_slack(parent_run_id=run_id)
    return f"{r['digests']} account digests"



def _has_connections() -> bool:
    from sqlalchemy import select

    from .config import settings
    from .models import Connection

    if (settings.gong_access_key and settings.gong_access_secret) or settings.crm != "local":
        return True
    with session_scope() as db:
        return db.scalar(select(Connection.id).where(Connection.provider.in_(["google", "microsoft"]),
                                                     Connection.status != "disabled")) is not None


async def _scheduled_sync(run_id: int) -> str:
    from .integrations.sync import run_sync

    return await run_sync(parent_run_id=run_id)
