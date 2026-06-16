"""Subagent definitions.

Segment / block-mix questions are routed to a focused subagent for context
quarantine: mix analysis pulls several segments and macro-group nuances, and
keeping that reasoning out of the main agent's context keeps the GM-facing thread
clean. The subagent is restricted to exactly the two mix tools so it cannot drift
into unrelated work, and it loads the mix-related skills.
"""

from __future__ import annotations

from deepagents import SubAgent

from tools.metrics import get_block_vs_transient_mix, get_segment_mix

# Tools, skills and prompt are pinned here so the subagent's surface is fixed at
# import time, the main agent picks this up via build_agent's subagents list.
SEGMENT_MIX_SUBAGENT: SubAgent = {
    "name": "segment-mix-analyst",
    "description": (
        "Delegate segment-mix and block/transient concentration analysis here. "
        "Use for questions about which segments drive a month, macro-group/segment "
        "shares, OTA dependency, group vs transient balance, and account "
        "concentration. Returns a concise mix read with shares and the key risk."
    ),
    "system_prompt": (
        "You are a segment-mix analyst for a hotel revenue team. Use get_segment_mix "
        "and get_block_vs_transient_mix to break down a stay month: report each "
        "driver's revenue and room-night share, contrast rate-led vs volume-led "
        "segments, and surface OTA or account concentration risk. Follow the "
        "segment-mix, ota-dependency and block-concentration skills. Never write SQL. "
        "Return a tight summary, not raw rows."
    ),
    "tools": [get_segment_mix, get_block_vs_transient_mix],
    "skills": ["skills/segment-mix", "skills/ota-dependency", "skills/block-concentration"],
}
