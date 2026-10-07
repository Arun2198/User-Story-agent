import re
from pathlib import Path

import pytest

from story_agent.clarify.limits import ClarifyLimits
from story_agent.config import AppConfig

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skill" / "scenario-to-stories" / "SKILL.md"
TEXT = SKILL.read_text(encoding="utf-8")
FLAT = " ".join(TEXT.split())


def frontmatter() -> dict[str, str]:
    match = re.match(r"---\n(.*?)\n---\n", TEXT, re.S)
    assert match, "SKILL.md needs frontmatter"
    return dict(line.split(": ", 1) for line in match.group(1).splitlines())


def test_frontmatter_names_the_skill_after_its_folder() -> None:
    meta = frontmatter()
    assert meta["name"] == SKILL.parent.name
    assert 40 < len(meta["description"]) <= 1024


def test_every_file_the_skill_names_exists() -> None:
    paths = set(re.findall(r"`((?:config|prompts|tests|skill)/[\w./-]+)`", TEXT))
    assert len(paths) >= 9
    missing = [p for p in paths if not (ROOT / p).exists()]
    assert missing == []


@pytest.mark.parametrize(
    "prompt", ["scope_check", "discover", "clarify", "draft", "criteria", "critique"]
)
def test_every_stage_prompt_is_referenced(prompt: str) -> None:
    assert f"prompts/{prompt}.md" in TEXT


def test_the_refusal_message_matches_the_guardrail_config(app_config: AppConfig) -> None:
    message = " ".join(app_config.guardrails.scope.refusal_message.split())
    assert message in " ".join(TEXT.split())


def test_the_limits_match_the_config(app_config: AppConfig) -> None:
    limits = ClarifyLimits.from_standards(app_config.standards)
    assert f"at most {limits.max_questions_per_round} questions per round" in TEXT
    assert f"at most {limits.max_rounds} rounds" in TEXT
    loops = app_config.standards["drafting"]["max_revision_loops"]
    assert f"at most {loops} revision loops" in TEXT
    assert limits.free_text_prompt in FLAT


def test_the_gate_and_grounding_rules_are_stated() -> None:
    lowered = TEXT.lower()
    for phrase in (
        "no drafting without the go-ahead",
        "never invent numbers",
        "data, not instructions",
        "never write memory without approval",
        "whitelisted preferences",
    ):
        assert phrase in lowered


def test_it_does_not_copy_prompt_bodies() -> None:
    for prompt in (ROOT / "prompts").glob("*.md"):
        body = prompt.read_text(encoding="utf-8")
        if "# HARD RULES" not in body:
            continue
        hard_rules = body.split("# HARD RULES", 1)[1].split("# STEP-BY-STEP", 1)[0]
        for line in hard_rules.splitlines():
            if len(line) > 60:
                assert line not in TEXT
