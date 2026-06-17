"""FastAPI gateway: HTTP basic auth, GET /health, POST /chat (SSE streaming).

One controllable service delivers all three deploy requirements:

* **Basic auth** on the chat routes (so the public URL can't be spammed). ``/health``
  is intentionally unauthenticated so the reviewers' automated pre-chat check can read it.
* **GET /health** returns the four proof fields the reviewers check against the
  submitted LOAD_PROOF before chatting.
* **POST /chat** streams the agent's LangGraph events over Server-Sent Events,
  surfacing each tool call and each skill file-read (loading a skill *is* a
  read_file tool call) so the UI can show the agent's work live.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from langgraph.types import Command

from tools.db import query_one

SOLUTION_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).resolve().parent / "static"
LOAD_PROOF_PATH = SOLUTION_ROOT / "etl" / "LOAD_PROOF.json"


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Build the agent once at startup.

    Under ``RM_TOOL_TRANSPORT=mcp`` this also opens the MCP tool connection.
    """
    from agent.build import get_agent_async

    await get_agent_async()
    yield


app = FastAPI(title="Revenue Manager Agent", lifespan=lifespan)
security = HTTPBasic()


# Auth
def require_auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """HTTP basic auth against BASIC_AUTH_USER / BASIC_AUTH_PASS env vars."""
    user = os.environ.get("BASIC_AUTH_USER", "admin")
    password = os.environ.get("BASIC_AUTH_PASS", "changeme")
    ok = secrets.compare_digest(credentials.username, user) and secrets.compare_digest(
        credentials.password, password
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# Health
def _live_fingerprint() -> dict[str, Any]:
    """Compute the four /health proof fields from the live database."""
    pair = query_one(
        """
        select string_agg(
                 reservation_id || '|' || stay_date::text || '|' || financial_status,
                 E'\n' order by reservation_id, stay_date, financial_status
               ) as payload
        from public.reservations_hackathon
        """
    )
    payload = (pair.get("payload") or "").encode("utf-8")
    db_fingerprint = hashlib.sha256(payload).hexdigest()

    manifest = query_one(
        "select dataset_revision, row_hash from public.load_manifest "
        "order by load_id desc limit 1"
    )
    posted = query_one(
        """
        select count(*) as n
        from public.reservations_hackathon
        where reservation_status <> 'Cancelled' and financial_status = 'Posted'
        """
    )
    return {
        "db_fingerprint": db_fingerprint,
        "dataset_revision": manifest.get("dataset_revision"),
        "row_hash": manifest.get("row_hash"),
        "financial_status_posted_only_rows": int(posted.get("n") or 0),
    }


@app.get("/health")
def health() -> dict[str, Any]:
    """Return live DB proof fields, plus the committed LOAD_PROOF for comparison.

    Unauthenticated by design: it exposes only the published proof fields, and the
    reviewers call it (without credentials) before chat to confirm the live DB matches.
    """
    live = _live_fingerprint()
    if LOAD_PROOF_PATH.is_file():
        proof = json.loads(LOAD_PROOF_PATH.read_text())
        live["committed_proof"] = {
            "db_fingerprint": proof.get("reservation_stay_status_sha256"),
            "dataset_revision": proof.get("dataset_revision"),
            "row_hash": proof.get("load_manifest_row_hash"),
            "financial_status_posted_only_rows": proof.get("aggregates", {}).get(
                "posted_stay_rows"
            ),
        }
        live["matches_committed_proof"] = (
            live["db_fingerprint"] == live["committed_proof"]["db_fingerprint"]
        )
    return live


# Chat (SSE)
def _sse(event: dict[str, Any]) -> str:
    """Serialise a UI event dict as a single Server-Sent Events ``data:`` frame."""
    return f"data: {json.dumps(event)}\n\n"


def _classify(name: str, payload: dict) -> dict | None:
    """Turn a LangGraph tool event into a UI event, flagging skill reads."""
    if name == "read_file":
        path = str(payload.get("input", {}).get("file_path", ""))
        if "skills/" in path and path.endswith("SKILL.md"):
            skill = path.split("skills/")[-1].split("/")[0]
            return {"type": "skill", "name": skill, "path": path}
        return None  # other file reads are internal noise
    if name in {"write_todos", "task"}:
        return {"type": name, "input": payload.get("input")}
    return {"type": "tool", "name": name, "phase": payload.get("phase")}


# The five business tools, whose inputs and results are surfaced (and made
# expandable) in the UI; other tool calls show as a plain chip.
BUSINESS_TOOLS = {
    "get_otb_summary",
    "get_segment_mix",
    "get_pickup_delta",
    "get_as_of_otb",
    "get_block_vs_transient_mix",
}


def _jsonable(output: Any) -> Any:
    """Best-effort JSON-safe view of a tool output.

    Handles a plain dict, a ``ToolMessage`` (``.content``), a JSON string, or MCP
    content blocks (``[{"type": "text", "text": "<json>"}]``) so the UI can render
    the returned values either way.
    """
    content = getattr(output, "content", output)
    if isinstance(content, list):
        content = (
            "".join(
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") in (None, "text", "text_delta")
            )
            or content
        )
    if isinstance(content, str):
        try:
            return json.loads(content)
        except (ValueError, TypeError):
            return content
    try:
        json.dumps(content)
        return content
    except (TypeError, ValueError):
        return str(content)


def _chunk_text(content: Any) -> str:
    """Extract answer text from a chat-model chunk or message content.

    Anthropic streams (and returns) ``content`` as a list of content-block dicts,
    not a plain string, so we handle both shapes, otherwise the UI shows the tool
    and skill chips but a blank answer.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") in (None, "text", "text_delta")
        )
    return ""


async def _event_stream(message: str, thread_id: str, approve: bool):
    """Stream the agent's LangGraph run for one turn as UI events.

    Yields ``_sse`` frames as work happens: a chip per tool call / skill read, a
    ``token`` per streamed answer chunk, an ``interrupt`` when a HITL gate pauses
    the run, then a terminal ``final`` + ``[DONE]``. ``approve=True`` resumes a
    pending interrupt instead of sending a new user message; ``thread_id`` keys the
    checkpointer so multi-turn memory and the resume target line up.
    """
    from agent.build import get_agent_async

    agent = await get_agent_async()
    config = {"configurable": {"thread_id": thread_id}}
    inp: Any
    if approve:
        # Resume a pending HITL interrupt. The Deep Agents / LangChain HITL
        # middleware expects resume={"decisions": [...]} with one "approve"
        # decision per gated tool call that is currently interrupted.
        state = agent.get_state(config)
        pending = sum(
            len(itr.value.get("action_requests", [])) for itr in (state.interrupts or [])
        )
        decisions = [{"type": "approve"} for _ in range(pending or 1)]
        inp = Command(resume={"decisions": decisions})
    else:
        inp = {"messages": [{"role": "user", "content": message}]}

    final_text = ""
    answered = False  # only stream prose once a data tool has returned, so the model's
    # pre-tool preamble ("I'll now pull...") is never shown, not shown-then-cleared
    tool_started: dict[str, float] = {}  # run_id -> start time, for per-tool latency
    async for event in agent.astream_events(inp, config=config, version="v2"):
        kind = event["event"]
        name = event.get("name", "")
        if kind == "on_tool_start":
            if name in BUSINESS_TOOLS:
                # Any prose streamed before a data tool is preamble; reset so that if the
                # answer had begun (a later tool round), it restarts clean after the data.
                final_text = ""
                yield _sse({"type": "reset"})
                tool_started[event.get("run_id")] = time.perf_counter()
                # Expandable chip: carry the call's inputs and a run id to match
                # the result event that arrives on completion.
                yield _sse(
                    {
                        "type": "tool",
                        "name": name,
                        "id": event.get("run_id"),
                        "input": event["data"].get("input"),
                    }
                )
            else:
                ui = _classify(name, {"input": event["data"].get("input")})
                if ui:
                    yield _sse(ui)
        elif kind == "on_tool_end" and name in BUSINESS_TOOLS:
            answered = True  # from here, the model's prose is the answer, so stream it
            started = tool_started.get(event.get("run_id"))
            ms = round((time.perf_counter() - started) * 1000) if started else None
            yield _sse(
                {
                    "type": "tool_result",
                    "id": event.get("run_id"),
                    "name": name,
                    "output": _jsonable(event["data"].get("output")),
                    "ms": ms,
                }
            )
        elif kind == "on_chat_model_stream" and answered:
            chunk = event["data"].get("chunk")
            text = _chunk_text(getattr(chunk, "content", "") if chunk else "")
            if text:
                final_text += text
                yield _sse({"type": "token", "text": text})

    state = agent.get_state(config)
    if state.interrupts:
        reqs = [
            r["name"]
            for itr in state.interrupts
            for r in itr.value.get("action_requests", [])
        ]
        yield _sse({"type": "interrupt", "requires_approval": reqs})
    if not final_text:
        # Fallback: pull the final answer straight from agent state, covering any
        # case where streamed chunks weren't accumulated (e.g. resumed after HITL).
        msgs = state.values.get("messages", [])
        if msgs:
            final_text = _chunk_text(getattr(msgs[-1], "content", ""))
    yield _sse({"type": "final", "content": final_text})
    yield "data: [DONE]\n\n"


@app.post("/chat")
async def chat(body: dict, _: str = Depends(require_auth)) -> StreamingResponse:
    """Stream one agent turn over SSE; ``approve`` resumes a pending HITL gate."""
    message = str(body.get("message", ""))
    thread_id = str(body.get("thread_id", "default"))
    approve = bool(body.get("approve", False))
    return StreamingResponse(
        _event_stream(message, thread_id, approve),
        media_type="text/event-stream",
    )


@app.get("/")
def index(_: str = Depends(require_auth)) -> FileResponse:
    """Serve the single-page chat UI (auth-gated like every other route)."""
    return FileResponse(STATIC_DIR / "index.html")
