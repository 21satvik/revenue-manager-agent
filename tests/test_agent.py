"""Agent wiring tests (Phase 3), AGENT_TEST_SCENARIOS.md.

Graph introspection + a fake tool-calling model + a recorded trace fixture. No
live LLM API calls (a fake model is injected), so these run in CI without keys.
"""

from __future__ import annotations

import json
from pathlib import Path

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from agent.build import INTERRUPT_ON, SKILL_SOURCES, build_agent
from agent.subagents import SEGMENT_MIX_SUBAGENT
from tools.metrics import ALL_TOOLS

REQUIRED_TOOL_NAMES = {
    "get_otb_summary",
    "get_segment_mix",
    "get_pickup_delta",
    "get_as_of_otb",
    "get_block_vs_transient_mix",
}
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class FakeToolModel(GenericFakeChatModel):
    """A fake chat model that supports bind_tools so we can drive the graph."""

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, D102
        return self


def _agent_with(tool_calls):
    """Build an agent whose fake model emits exactly ``tool_calls`` on first turn."""
    ai = AIMessage(content="", tool_calls=tool_calls)
    return build_agent(model=FakeToolModel(messages=iter([ai])))


def _registered_tool_names(agent) -> set[str]:
    """Return the names of the tools registered on the agent's tools node."""
    node = agent.nodes.get("tools")
    return set(getattr(getattr(node, "bound", node), "tools_by_name", {}).keys())


# Scenario 1, tool surface is fixed
def test_scenario1_exactly_five_business_tools():
    """The agent registers exactly the five business tools and no SQL tool."""
    # Our deliberately designed tool surface is exactly the five (no run_sql).
    assert {t.name for t in ALL_TOOLS} == REQUIRED_TOOL_NAMES
    agent = build_agent(model=FakeToolModel(messages=iter([AIMessage(content="hi")])))
    registered = _registered_tool_names(agent)
    assert REQUIRED_TOOL_NAMES <= registered
    assert not any("sql" in name.lower() for name in registered)


# Scenario 2, get_as_of_otb is human-gated
def test_scenario2_as_of_otb_interrupts():
    """Calling get_as_of_otb pauses the run for human approval before it executes."""
    assert INTERRUPT_ON.get("get_as_of_otb") is True  # declared HITL target
    agent = _agent_with(
        [{"name": "get_as_of_otb",
          "args": {"stay_month": "2025-08", "as_of_utc": "2025-05-01T00:00:00Z"},
          "id": "c1"}]
    )
    cfg = {"configurable": {"thread_id": "hitl"}}
    result = agent.invoke({"messages": [{"role": "user", "content": "as of May 1?"}]}, config=cfg)
    # Execution pauses for approval before the gated tool runs.
    assert "__interrupt__" in result
    state = agent.get_state(cfg)
    names = [
        req["name"]
        for itr in state.interrupts
        for req in itr.value.get("action_requests", [])
    ]
    assert "get_as_of_otb" in names


# Scenario 3, segment work is isolated
def test_scenario3_segment_work_isolated_via_subagent():
    """Segment work is isolated in a subagent restricted to the two mix tools."""
    # Chosen pattern: a dedicated subagent restricted to the two mix tools.
    sub = SEGMENT_MIX_SUBAGENT
    assert sub["name"] == "segment-mix-analyst"
    sub_tool_names = {t.name for t in sub["tools"]}
    assert sub_tool_names == {"get_segment_mix", "get_block_vs_transient_mix"}
    # The subagent is reachable via the built-in task tool on the main agent.
    agent = build_agent(model=FakeToolModel(messages=iter([AIMessage(content="hi")])))
    assert "task" in _registered_tool_names(agent)


# Scenario 4, multi-tool decomposition (recorded trace)
def test_scenario4_multi_tool_decomposition():
    """A composite question decomposes into multiple business-tool calls (recorded trace)."""
    trace = json.loads((FIXTURES / "composite_trace.json").read_text())
    used = {c["name"] for c in trace["tool_calls"]}
    assert len(used & REQUIRED_TOOL_NAMES) >= 2
    assert "get_otb_summary" in used and "get_pickup_delta" in used


# Scenario 5, skill loading is on-demand
def test_scenario5_skills_filesystem_backed():
    """Skills load on demand from disk via SkillsMiddleware, not a monolith prompt."""
    assert SKILL_SOURCES == ["skills"]
    agent = build_agent(model=FakeToolModel(messages=iter([AIMessage(content="hi")])))
    # SkillsMiddleware is wired (progressive disclosure), not a monolith prompt.
    assert any("SkillsMiddleware" in node for node in agent.nodes)
    # Skill files exist on disk for the backend to read.
    assert (Path(__file__).resolve().parents[1] / "skills" / "otb-summary" / "SKILL.md").is_file()


# Scenario 6, memory / filesystem used
def test_scenario6_memory_configured():
    """The agent wires a checkpointer, a store, and the virtual-filesystem tools."""
    agent = build_agent(model=FakeToolModel(messages=iter([AIMessage(content="hi")])))
    assert agent.checkpointer is not None  # multi-turn memory + HITL support
    assert agent.store is not None
    # Virtual filesystem is wired (its tools are registered).
    assert {"read_file", "write_file", "ls"} <= _registered_tool_names(agent)


# Scenario 7, refusal policy encoded (bonus)
def test_scenario7_refusal_policy_in_config():
    """The default-filter policy and refusal stance are encoded in skills + prompt."""
    from agent.prompt import SYSTEM_PROMPT

    guardrail = (
        Path(__file__).resolve().parents[1] / "skills" / "grain-and-filters" / "SKILL.md"
    ).read_text()
    # The default filter policy + "don't silently comply" are encoded for the agent.
    assert "do not silently comply" in guardrail.lower()
    assert "posted" in SYSTEM_PROMPT.lower() and "non-cancelled" in SYSTEM_PROMPT.lower()
