"""Assemble the Revenue Manager deep agent from the framework building blocks.

A single ``create_deep_agent(...)`` call wires together every required Deep Agents
capability, each chosen deliberately (see ARCHITECTURE.md):

* **model + system_prompt** - Claude (env ``MODEL_ID``) with the RM persona.
* **tools** - exactly the five named tools; no run_sql.
* **skills** - filesystem-backed SKILL.md pack, progressive disclosure.
* **subagents** - segment/block-mix work quarantined to a focused analyst.
* **interrupt_on** - human approval gate on the expensive ``get_as_of_otb``.
* **checkpointer + store** - multi-turn memory (checkpointer is also required for HITL).
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from agent.prompt import dated_system_prompt
from agent.subagents import SEGMENT_MIX_SUBAGENT
from tools.metrics import ALL_TOOLS


def _today() -> str:
    """Current date (Europe/London, matching the pickup-window time zone)."""
    return datetime.now(ZoneInfo("Europe/London")).date().isoformat()

SOLUTION_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_ID = "anthropic:claude-sonnet-4-6"

# The single tool gated behind human approval (point-in-time rebuild).
HITL_TOOL = "get_as_of_otb"
INTERRUPT_ON = {HITL_TOOL: True}

# Skill sources, relative to the filesystem backend root (SOLUTION_ROOT).
SKILL_SOURCES = ["skills"]


def build_model(model_id: str | None = None):
    """Resolve the chat model from MODEL_ID (provider-agnostic, Claude by default)."""
    from langchain.chat_models import init_chat_model

    return init_chat_model(model_id or os.environ.get("MODEL_ID", DEFAULT_MODEL_ID))


def build_agent(
    *,
    model=None,
    model_id: str | None = None,
    checkpointer=None,
    store=None,
):
    """Create the compiled Revenue Manager deep agent graph.

    ``model`` may be injected (e.g. a fake chat model in tests) to introspect the
    graph without a live LLM; otherwise it is resolved from ``MODEL_ID``.

    Args:
      model: a pre-built chat model to use directly; takes precedence over ``model_id``.
      model_id: model id to resolve when ``model`` is not given (else ``MODEL_ID``).
      checkpointer: conversation checkpointer; defaults to an in-memory saver
        (required for HITL, so a default is always wired in).
      store: long-term memory store; defaults to an in-memory store.
    """
    backend = FilesystemBackend(root_dir=str(SOLUTION_ROOT), virtual_mode=False)
    return create_deep_agent(
        model=model if model is not None else build_model(model_id),
        system_prompt=dated_system_prompt(_today()),
        tools=ALL_TOOLS,
        skills=SKILL_SOURCES,
        subagents=[SEGMENT_MIX_SUBAGENT],
        interrupt_on=INTERRUPT_ON,
        backend=backend,
        checkpointer=checkpointer or InMemorySaver(),
        store=store or InMemoryStore(),
    )


_AGENT = None
_AGENT_DATE = None


def get_agent():
    """Return a process-wide singleton agent, rebuilt when the date rolls over.

    The system prompt embeds today's date, so a long-running server stays correct
    across midnight without a manual restart.
    """
    global _AGENT, _AGENT_DATE
    today = _today()
    if _AGENT is None or _AGENT_DATE != today:
        _AGENT = build_agent()
        _AGENT_DATE = today
    return _AGENT
