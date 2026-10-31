import asyncio
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import jobs
from .api import (analytics, approvals, copilot, deals, evaluation, insights, integrations, kb, leads, marketing,
                  onboarding, webhooks, workspace)
from .config import settings
from .db import current_workspace, init_db
from .integrations import slack_bot  # noqa: F401  (registers the Slack approval hook)
from .services import auth, workspaces
from .tools.crm import get_crm

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
workspaces.install()  # `settings` now reads the current workspace's own values first


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    workspaces.ensure_default()
    jobs.bind_loop(asyncio.get_running_loop())
    scheduler = asyncio.create_task(jobs.scheduler_loop())
    yield
    scheduler.cancel()


app = FastAPI(title="RevenueOS", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"],
)


# Endpoints that verify their own signature or secret instead of a sign-in. Webhooks name their
# workspace with ?ws=<slug> (default workspace when omitted).
PUBLIC_PATHS = {
    "/api/health", "/api/integrations/slack/events", "/api/integrations/zoom",
    "/api/integrations/slack/interactions", "/api/integrations/slack/commands",
    "/api/integrations/oauth/google/callback", "/api/integrations/oauth/microsoft/callback",
    "/api/integrations/transcripts", "/api/integrations/calendar-events",
    "/api/auth/login", "/api/auth/bootstrap",
}


@app.middleware("http")
async def workspace_and_auth(request: Request, call_next):
    """Decide who is calling and which workspace (client database) the request runs in.

    - Webhooks and other public paths: the workspace named by ?ws=<slug>.
    - Signed-in users: their own workspace.
    - API_TOKEN (service access, e.g. the MCP server): the default workspace.
    - No users and no API_TOKEN yet: open, as a single-company install.
    """
    path = request.url.path
    ctx, caller = None, {"user": None, "role": "admin", "workspace": None}
    if path.startswith("/api") and request.method != "OPTIONS":
        if path in PUBLIC_PATHS or path.startswith("/api/webhooks"):
            slug = request.query_params.get("ws")
            if slug:
                ws = workspaces.by_slug(slug)
                if not ws:
                    return JSONResponse({"detail": "Unknown workspace"}, status_code=404)
                ctx = workspaces.context_id(ws)
        else:
            header = request.headers.get("authorization", "")
            token = header.removeprefix("Bearer ").strip()
            if settings.api_token and hmac.compare_digest(header, f"Bearer {settings.api_token}"):
                caller = {"user": "service", "role": "owner", "workspace": "default"}
            elif token and (found := auth.resolve(token)):
                user, ws = found
                ctx = workspaces.context_id(ws)
                caller = {"user": {"email": user.email, "name": user.name}, "role": user.role, "workspace": ws.slug}
            elif auth.users_exist() or settings.api_token:
                return JSONResponse({"detail": "Sign in required"}, status_code=401)
    request.state.caller = caller
    reset = current_workspace.set(ctx)
    try:
        return await call_next(request)
    finally:
        current_workspace.reset(reset)


for module in (leads, approvals, copilot, kb, webhooks, analytics, insights, deals, marketing, integrations,
               workspace, onboarding, evaluation):
    app.include_router(module.router)


@app.get("/api/health")
def health():
    return {
        "ok": True, "company": settings.company_name, "crm": get_crm().name,
        "email": "smtp" if settings.smtp_host else "dry_run",
        "models": {"orchestrator": settings.model_orchestrator, "worker": settings.model_worker,
                   "fast": settings.model_fast},
    }
