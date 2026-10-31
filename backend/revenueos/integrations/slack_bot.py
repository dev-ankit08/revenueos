"""RevenueOS inside Slack: approve drafts with a button, and ask the Copilot with /revenueos.

Setup (same Slack app as event ingestion): add bot scope `chat:write` and a slash command
`/revenueos` → /api/integrations/slack/commands; turn on Interactivity → /api/integrations/slack/interactions;
set SLACK_BOT_TOKEN and SLACK_APPROVALS_CHANNEL. Anyone in that channel can approve, so keep it
to the sales team.
"""
import contextvars
import logging
import threading

from sqlalchemy import event
from sqlalchemy.orm import Session

from ..config import settings
from ..db import session_scope
from ..models import Approval
from . import http

log = logging.getLogger(__name__)
SLACK_API = "https://slack.com/api"


def enabled() -> bool:
    return bool(settings.slack_bot_token and settings.slack_approvals_channel)


def slack_call(method: str, payload: dict) -> dict:
    res = http.request("POST", f"{SLACK_API}/{method}", json_body=payload,
                       headers={"Authorization": f"Bearer {settings.slack_bot_token}"})
    if not res.get("ok"):
        raise RuntimeError(f"Slack {method} failed: {res.get('error')}")
    return res


def approval_blocks(a: Approval) -> list[dict]:
    c = a.contact
    who = f"{c.full_name} ({c.title or 'unknown title'}, {c.account.name})" if c else (a.title or "content")
    meta = [f"{a.kind.replace('_', ' ')}", f"step {a.sequence_step}", f"by {a.created_by_agent}"]
    if a.personalisation_score is not None:
        meta.append(f"personalisation {a.personalisation_score}/100")
    flags = "\n".join(f"{'⛔' if f['severity'] == 'block' else '⚠️'} {f['message']}" for f in a.guardrail_flags)
    body = a.body if len(a.body) < 2600 else a.body[:2600] + "…"
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Draft for {who}*\n{' · '.join(meta)}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": (f"*{a.subject}*\n" if a.subject else "") + body}},
    ]
    if a.rationale:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"Why: {a.rationale[:300]}"}]})
    if flags:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": flags[:2900]}})
    blocks.append({"type": "actions", "elements": [
        {"type": "button", "style": "primary", "action_id": "approve", "value": str(a.id),
         "text": {"type": "plain_text", "text": "Approve & send" if a.kind == "email" else "Approve"}},
        {"type": "button", "style": "danger", "action_id": "reject", "value": str(a.id),
         "text": {"type": "plain_text", "text": "Reject"}},
        {"type": "button", "action_id": "open", "url": f"{settings.app_base_url}/approvals",
         "text": {"type": "plain_text", "text": "Edit in RevenueOS"}},
    ]})
    return blocks


def post_approvals(ids: list[int]) -> None:
    for approval_id in ids:
        try:
            with session_scope() as db:
                a = db.get(Approval, approval_id)
                if not a or a.status != "pending":
                    continue  # e.g. the A/B holdout variant
                slack_call("chat.postMessage", {"channel": settings.slack_approvals_channel,
                                                "text": f"New draft for review: {a.subject or a.title or a.kind}",
                                                "blocks": approval_blocks(a)})
        except Exception as exc:
            log.warning("could not post approval %s to Slack: %s", approval_id, exc)


@event.listens_for(Session, "after_commit")
def _announce_new_approvals(session) -> None:
    """approvals.queue() notes new drafts on the session; once they are committed, post them to Slack."""
    ids = session.info.pop("new_approvals", [])
    if ids and enabled():
        ctx = contextvars.copy_context()  # post from the same workspace
        threading.Thread(target=ctx.run, args=(post_approvals, ids), daemon=True).start()


@event.listens_for(Session, "after_rollback")
def _forget_rolled_back(session) -> None:
    session.info.pop("new_approvals", None)


def respond(response_url: str, text: str, replace: bool = False) -> None:
    """Reply through Slack's response_url (valid 30 minutes; needs no token)."""
    payload = {"text": text[:3900], "response_type": "ephemeral"}
    if replace:
        payload = {"text": text[:3900], "replace_original": True}
    http.request("POST", response_url, json_body=payload)
