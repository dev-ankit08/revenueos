"""RevenueOS as an MCP server, so other AI assistants can use its skills.

Claude (Desktop / Code), Microsoft Copilot Studio, Salesforce Agentforce, HubSpot or any MCP
client can call these tools. Writing tools still only DRAFT: everything customer-facing goes
to the RevenueOS approval queue.

Run:
  python -m revenueos.mcp_server                  # stdio (Claude Desktop / Claude Code)
  python -m revenueos.mcp_server --http 8001      # streamable HTTP at /mcp; requires API_TOKEN
"""
import argparse
import json

from mcp.server.mcpserver import MCPServer
from sqlalchemy import func, select

from .agents import committee, deals, intel, person_researcher, researcher, strategy, writer
from .config import settings
from .db import init_db, session_scope
from .models import Account, Approval, Contact, Opportunity, SalesDocument
from .services import insights, memory
from .tools import kb

server = MCPServer(
    name="revenueos",
    instructions=("RevenueOS sales and marketing agents. Lookups are instant; research and writing tools take "
                  "1-3 minutes. Writing tools only queue drafts for human approval in RevenueOS; nothing is sent."),
)


def _contact_row(c: Contact) -> dict:
    return {"contact_id": c.id, "name": c.full_name, "title": c.title, "email": c.email, "company": c.account.name,
            "account_id": c.account_id, "status": c.status, "score": c.score, "tier": c.score_tier}


@server.tool()
def find_leads(search: str | None = None, status: str | None = None, min_score: float | None = None,
               limit: int = 20) -> str:
    """Find leads by name, title or company, best scored first."""
    with session_scope() as db:
        stmt = select(Contact).join(Account)
        if search:
            like = f"%{search}%"
            stmt = stmt.where(Contact.first_name.ilike(like) | Contact.last_name.ilike(like)
                              | Contact.title.ilike(like) | Account.name.ilike(like))
        if status:
            stmt = stmt.where(Contact.status == status)
        if min_score is not None:
            stmt = stmt.where(Contact.score >= min_score)
        rows = [_contact_row(c) for c in db.scalars(stmt.order_by(Contact.score.desc().nullslast()).limit(min(limit, 100)))]
    return json.dumps(rows)


@server.tool()
def account_timeline(account_id: int) -> str:
    """Everything the team knows about an account: calls, emails, Slack digests, meetings, notes (newest first)."""
    return memory.context_block(account_id) or "No history yet."


@server.tool()
def knowledge_search(query: str) -> str:
    """Search the company knowledge base (offerings, case studies, pricing guidance, competitors, objections)."""
    with session_scope() as db:
        return json.dumps(kb.search(db, query, k=5))


@server.tool()
def pipeline_status() -> str:
    """Leads by status, open deals, pending approvals and the quarter's forecast numbers."""
    with session_scope() as db:
        by_status = dict(db.execute(select(Contact.status, func.count()).group_by(Contact.status)).all())
        pending = db.scalar(select(func.count()).select_from(Approval).where(Approval.status == "pending"))
        numbers = insights.forecast_numbers(db, settings.quarterly_target or None)
        numbers.pop("deals", None)
    return json.dumps({"leads_by_status": by_status, "pending_approvals": pending, "forecast": numbers}, default=str)


@server.tool()
async def research_account(account_id: int, focus: str | None = None) -> str:
    """Research a company on the web and save a sales brief (priorities, pains, news, best angle)."""
    return await researcher.research_account(account_id, focus) or "Brief saved."


@server.tool()
async def research_person(contact_id: int) -> str:
    """Build a sourced dossier of one person's public, professional footprint."""
    return json.dumps(await person_researcher.research_person(contact_id), default=str)


@server.tool()
async def draft_sequence(contact_id: int, steps: int = 3, instructions: str | None = None) -> str:
    """Draft a personalised outreach sequence into RevenueOS's approval queue (nothing is sent)."""
    return await writer.write_sequence(contact_id, steps, instructions)


@server.tool()
async def draft_committee_outreach(account_id: int, instructions: str | None = None) -> str:
    """Role-specific, staggered sequences for everyone at an account, around one narrative (drafts only)."""
    return json.dumps(await committee.committee_outreach(account_id, instructions))


@server.tool()
async def analyse_call(contact_id: int, transcript: str) -> str:
    """Turn a call transcript or notes into MEDDICC, deal stage and health; updates the deal and drafts the follow-up."""
    return json.dumps(await deals.analyze_call(contact_id, transcript), default=str)


@server.tool()
async def meeting_prep(contact_id: int, goal: str | None = None, attendees: str | None = None) -> str:
    """One-page prep brief for an upcoming call."""
    doc_id = await deals.prep_meeting(contact_id, goal, attendees)
    with session_scope() as db:
        return db.get(SalesDocument, doc_id).content


@server.tool()
async def review_deal(opportunity_id: int) -> str:
    """Honest deal review: win probability, stakeholders, risks, next best actions."""
    r = await strategy.review_deal(opportunity_id)
    with session_scope() as db:
        return db.get(SalesDocument, r["document_id"]).content


@server.tool()
async def competitor_battlecard(competitor: str) -> str:
    """Research a competitor and produce a sourced battlecard."""
    r = await intel.competitor_battlecard(competitor)
    with session_scope() as db:
        return db.get(SalesDocument, r["document_id"]).content


@server.tool()
def open_deals() -> str:
    """Open deals with stage, amount, health, win probability and next steps."""
    with session_scope() as db:
        rows = [{"opportunity_id": o.id, "name": o.name, "account": o.account.name, "stage": o.stage,
                 "amount": o.amount, "close_date": o.close_date, "health": o.health,
                 "win_probability": o.win_probability, "next_steps": o.next_steps}
                for o in db.scalars(select(Opportunity).where(Opportunity.stage.notin_(["closed_won", "closed_lost"])))]
    return json.dumps(rows)


class BearerAuth:
    """ASGI wrapper: the HTTP transport requires `Authorization: Bearer <API_TOKEN>`."""

    def __init__(self, app, token: str):
        self.app, self.expected = app, f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            auth = dict(scope.get("headers") or []).get(b"authorization", b"")
            import hmac

            if not hmac.compare_digest(auth, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"detail":"Unauthorized"}'})
                return
        await self.app(scope, receive, send)


def main() -> None:
    parser = argparse.ArgumentParser(description="RevenueOS MCP server")
    parser.add_argument("--http", type=int, metavar="PORT", help="serve streamable HTTP on this port instead of stdio")
    args = parser.parse_args()
    init_db()
    if args.http:
        if not settings.api_token:
            raise SystemExit("Set API_TOKEN before exposing the MCP server over HTTP.")
        import uvicorn

        uvicorn.run(BearerAuth(server.streamable_http_app(), settings.api_token), host="0.0.0.0", port=args.http)
    else:
        server.run("stdio")


if __name__ == "__main__":
    main()
