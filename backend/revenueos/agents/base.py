"""Shared agent runtime: one place for model calls, audit logging, cost and refusals.

Every agent is the Anthropic SDK tool runner plus a system prompt and a tool list.
Agents delegate to each other by exposing a sub-agent as a tool (see orchestrator).
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import anthropic
from anthropic import beta_async_tool

from ..config import settings
from ..db import session_scope
from ..models import AgentRun
from ..services import governance
from ..tools import kb

log = logging.getLogger(__name__)

# $ per million tokens: (input, output, cache read)
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}

# Web research runs on Anthropic's servers; no client code needed.
WEB_TOOLS = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 8},
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 6},
]

_client: anthropic.AsyncAnthropic | None = None


def client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()
    return _client


class AgentRefused(Exception):
    pass


@dataclass
class AgentResult:
    text: str
    run_id: int
    message: Any = None
    parsed: Any = None


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    tool_calls: list = field(default_factory=list)

    def add(self, message) -> None:
        u = message.usage
        self.input += u.input_tokens or 0
        self.output += u.output_tokens or 0
        self.cache_read += getattr(u, "cache_read_input_tokens", 0) or 0
        self.cache_write += getattr(u, "cache_creation_input_tokens", 0) or 0
        for block in message.content:
            if block.type in ("tool_use", "server_tool_use"):
                self.tool_calls.append({"tool": block.name, "input": _short(block.input)})

    def cost(self, model: str) -> float:
        p_in, p_out, p_cache = PRICES.get(model, (4.00, 20.00, 0.20))
        return (
            self.input * p_in + self.cache_write * p_in * 1.25
            + self.cache_read * p_cache + self.output * p_out
        ) / 1_000_000


def _short(value: Any, limit: int = 300) -> Any:
    text = json.dumps(value, default=str)
    return value if len(text) <= limit else text[:limit] + "..."


def system_blocks(role_prompt: str) -> list[dict]:
    """Stable company context first (cached across all agents and runs), role instructions after."""
    with session_scope() as db:
        context = kb.company_context(db)
    return [
        {
            "type": "text",
            "text": f"You work for {settings.company_name}.\n\n# COMPANY CONTEXT\n{context}",
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": role_prompt},
    ]


def _request_options(model: str) -> dict:
    # Server-side fallback routes a declined request to another model instead of
    # failing the run. Not offered for Haiku.
    if model.startswith("claude-haiku"):
        return {}
    return {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}


def _start_run(agent: str, model: str, prompt: str, parent_run_id: int | None) -> int:
    with session_scope() as db:
        run = AgentRun(agent=agent, model=model, input=prompt, parent_run_id=parent_run_id)
        db.add(run)
        db.flush()
        return run.id


def _finish_run(run_id: int, model: str, usage: Usage, output: str | None, error: str | None) -> None:
    with session_scope() as db:
        run = db.get(AgentRun, run_id)
        run.model = model  # the model actually used (governance may override it)
        run.status = "failed" if error else "succeeded"
        run.output, run.error = output, error
        run.tool_calls = usage.tool_calls
        run.input_tokens, run.output_tokens = usage.input, usage.output
        run.cache_read_tokens = usage.cache_read
        run.cost_usd = round(usage.cost(model), 5)
        run.finished_at = datetime.now(timezone.utc)


def final_text(message) -> str:
    return "\n".join(b.text for b in message.content if b.type == "text").strip()


async def run_agent(
    *,
    agent: str,
    model: str,
    role_prompt: str,
    prompt: str,
    tools: list | None = None,
    effort: str = "medium",
    max_tokens: int = 16000,
    output_format: Any = None,
    parent_run_id: int | None = None,
    run_id: int | None = None,
) -> AgentResult:
    """Run one agent to completion with logging. `tools` may mix @beta_async_tool functions and server tools.

    Pass `run_id` to reuse a run row created by the caller (so tools can reference it).
    """
    run_id = run_id or _start_run(agent, model, prompt, parent_run_id)
    usage = Usage()
    try:
        # Budgets, on/off switch and model overrides from AI governance (raises when over budget).
        model = governance.authorize(agent, model)
        params: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            system=system_blocks(role_prompt),
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": effort},
            **_request_options(model),
        )
        if output_format is not None:
            params["output_format"] = output_format

        # Always stream: long outputs (proposals, orchestration) would otherwise hit HTTP
        # timeouts, and the SDK refuses large non-streaming max_tokens outright.
        if tools:
            runner = client().beta.messages.tool_runner(tools=tools, stream=True, **params)
            last = None
            async for stream in runner:
                last = await stream.get_final_message()
                usage.add(last)
        else:
            async with client().beta.messages.stream(**params) as stream:
                last = await stream.get_final_message()
            usage.add(last)

        if last.stop_reason == "refusal":
            details = getattr(last, "stop_details", None)
            raise AgentRefused(f"{agent} declined the request ({getattr(details, 'category', None)}).")
        if last.stop_reason == "max_tokens":
            log.warning("%s hit max_tokens; output may be truncated", agent)

        text = final_text(last)
        parsed = getattr(last, "parsed_output", None)
        _finish_run(run_id, model, usage, text or (parsed.model_dump_json() if parsed else ""), None)
        return AgentResult(text=text, run_id=run_id, message=last, parsed=parsed)
    except Exception as exc:
        _finish_run(run_id, model, usage, None, f"{type(exc).__name__}: {exc}")
        raise


class NotSubmitted(Exception):
    pass


def submit_tool(model: type, name: str, description: str):
    """A client tool whose input schema is `model`; the agent calls it once to hand back its result.

    Used instead of output_format for agents that also use web search, so structured results
    never depend on combining server-tool citations with constrained output.
    Returns (tool, get_result) where get_result() raises NotSubmitted if the agent never called it.
    """
    box: dict = {}

    @beta_async_tool(name=name, description=description, input_schema=model)
    async def _submit(**kwargs) -> str:
        box["value"] = model.model_validate(kwargs)
        return "Saved. You are done; reply with one short line."

    def get_result():
        if "value" not in box:
            raise NotSubmitted(f"Agent finished without calling {name}.")
        return box["value"]

    return _submit, get_result


def new_run(agent: str, model: str, prompt: str, parent_run_id: int | None = None) -> int:
    return _start_run(agent, model, prompt, parent_run_id)
