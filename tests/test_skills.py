"""Skill-pack structural tests (Phase 3), SKILL_TEST_SCENARIOS.md.

Pure filesystem checks of the skill pack: no LLM calls. Validates pack version,
counts, judgment depth (numeric threshold + recommended action), tool routing,
distinctness, the adversarial guardrail, and OTA/block concentration coverage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"

REQUIRED_TOOLS = {
    "get_otb_summary",
    "get_segment_mix",
    "get_pickup_delta",
    "get_as_of_otb",
    "get_block_vs_transient_mix",
}

# A numeric threshold: comparison + number, a banded range, a bare percentage, or "below/above N".
THRESHOLD_RE = re.compile(
    r"(?:[<>]=?|≥|≤)\s*\d+(?:\.\d+)?%?"
    r"|\d+\s*[–-]\s*\d+\s*%"
    r"|\d+\s*%"
    r"|(?:below|above|under|over)\s+\d+",
    re.IGNORECASE,
)
ACTION_WORDS = (
    "shift", "close", "review", "hold", "raise", "diversify", "stimulate", "cap",
    "pull back", "tighten", "enforce", "reduce", "push direct", "open a", "secure",
    "limit", "protect", "move to",
)


@dataclass
class Skill:
    path: Path
    name: str
    description: str
    body: str

    @property
    def body_word_count(self) -> int:
        return len(self.body.split())

    @property
    def text(self) -> str:
        return f"{self.description}\n{self.body}"


def _parse(path: Path) -> Skill:
    """Parse a SKILL.md into a Skill, splitting YAML frontmatter from the body."""
    raw = path.read_text(encoding="utf-8")
    assert raw.startswith("---"), f"{path}: missing YAML frontmatter"
    _, fm, body = raw.split("---", 2)
    meta: dict[str, str] = {}
    for line in fm.strip().splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip().strip('"').strip("'")
    return Skill(path, meta.get("name", ""), meta.get("description", ""), body.strip())


def _load_all() -> list[Skill]:
    """Parse every ``*/SKILL.md`` in the pack, sorted for deterministic ordering."""
    return [_parse(p) for p in sorted(SKILLS_DIR.glob("*/SKILL.md"))]


def _is_judgment(skill: Skill) -> bool:
    """Whether a skill gives real guidance: a numeric threshold, an action, and depth."""
    has_threshold = bool(THRESHOLD_RE.search(skill.body))
    has_action = any(w in skill.body.lower() for w in ACTION_WORDS)
    return has_threshold and has_action and skill.body_word_count >= 80


# Scenario 1, pack version pin
def test_scenario1_pack_version():
    """The challenge skill pins the expected pack version (otel-rm-v2)."""
    challenge = SKILLS_DIR / "CHALLENGE_SKILL.md"
    text = challenge.read_text(encoding="utf-8")
    assert "otel-rm-v2" in text
    skill = _parse(challenge)
    assert "otel-rm-v2" in skill.description


# Scenario 2, minimum skill count
def test_scenario2_minimum_skill_count():
    """The pack ships at least six skills, each with a name and description."""
    skills = _load_all()
    assert len(skills) >= 6
    for s in skills:
        assert s.name, f"{s.path}: missing name"
        assert s.description, f"{s.path}: missing description"


# Scenario 3, judgment skills
def test_scenario3_judgment_skills():
    """At least three skills give real judgment (threshold + action + depth)."""
    judgment = [s for s in _load_all() if _is_judgment(s)]
    assert len(judgment) >= 3, f"only {len(judgment)} judgment skills"
    for s in judgment:
        assert THRESHOLD_RE.search(s.body), s.name
        assert s.body_word_count >= 80, s.name


# Scenario 4, tool routing declared
def test_scenario4_tool_routing_and_no_sql():
    """Every skill routes to a required tool and never instructs raw SQL."""
    for s in _load_all():
        assert any(t in s.text for t in REQUIRED_TOOLS), f"{s.name}: names no required tool"
        lowered = s.text.lower()
        assert "run_sql" not in lowered
        # No skill should instruct writing SQL or hitting the raw table.
        assert "select " not in lowered or "never" in lowered or "no raw sql" in lowered


# Scenario 5, distinct routing (no clones)
def test_scenario5_distinct_routing():
    """Skills are distinct (no duplicate names/descriptions) and cover key tools."""
    skills = _load_all()
    names = [s.name for s in skills]
    assert len(names) == len(set(names)), "duplicate skill names"
    norm = [re.sub(r"\s+", " ", s.description.strip().lower()) for s in skills]
    assert len(norm) == len(set(norm)), "duplicate descriptions"
    blob = " ".join(s.text.lower() for s in skills)
    assert "get_pickup_delta" in blob  # pickup/pace covered
    assert "get_segment_mix" in blob  # mix/segment covered
    assert "get_otb_summary" in blob  # OTB summary covered


# Scenario 6, adversarial guardrail
def test_scenario6_adversarial_guardrail():
    """The pack encodes the adversarial guardrail vocabulary (grain, filters, dates)."""
    blob = " ".join(s.body.lower() for s in _load_all())
    assert "property_date" in blob and "monthly otb" in blob
    assert "rows" in blob and "reservation" in blob
    assert "cancelled" in blob and "provisional" in blob


# Scenario 7, Tier D/E readiness
def test_scenario7_concentration_judgment_present():
    """OTA-dependency and block-concentration judgment skills exist (Tier D/E ready)."""
    skills = _load_all()
    ota = next((s for s in skills if s.name == "ota-dependency"), None)
    block = next((s for s in skills if s.name == "block-concentration"), None)
    assert ota is not None and _is_judgment(ota)
    assert block is not None and _is_judgment(block)
    assert "share_of_revenue" in ota.text
    assert "block_share_of_revenue" in block.text


@pytest.mark.parametrize("missing", ["", "x"])
def test_every_skill_parses(missing):
    # Smoke: all skill files parse and have non-trivial bodies.
    for s in _load_all():
        assert s.body_word_count > 20, s.path
