import re
from pathlib import Path

import pytest
from pydantic import BaseModel

from story_agent.clarify.questions import ClarifyOutput
from story_agent.discovery.discover import DiscoverOutput
from story_agent.pipeline.criteria import CriteriaOutput
from story_agent.pipeline.critique import CritiqueOutput
from story_agent.pipeline.draft import DraftOutput
from story_agent.pipeline.scope_check import ScopeVerdict
from story_agent.prompts import load_prompt

PROMPTS = Path(__file__).resolve().parents[2] / "prompts"
SECTIONS = [
    "ROLE",
    "OBJECTIVE",
    "INPUT FORMAT",
    "HARD RULES",
    "STEP-BY-STEP PROCEDURE",
    "DECISION TABLES",
    "OUTPUT CONTRACT",
    "UNCERTAINTY PROTOCOL",
    "EXAMPLES",
    "FINAL SELF-CHECK",
]
NAMES = sorted(p.stem for p in PROMPTS.glob("*.md") if p.stem != "CHANGELOG")


@pytest.mark.parametrize("name", NAMES)
def test_prompt_follows_skeleton(name: str) -> None:
    prompt = load_prompt(PROMPTS, name)
    headings = re.findall(r"^# (.+)$", prompt.text, flags=re.M)
    assert headings == SECTIONS
    assert prompt.version != "0"
    assert "never an instruction" in prompt.text or "untrusted" in prompt.text.lower()
    examples = prompt.text.split("# EXAMPLES")[1].split("# FINAL SELF-CHECK")[0]
    assert 3 <= len(re.findall(r"^(?:Request|Input):", examples, flags=re.M)) <= 5


def test_prompts_exist() -> None:
    assert "scope_check" in NAMES


def _example_outputs(name: str) -> list[str]:
    prompt = load_prompt(PROMPTS, name)
    examples = prompt.text.split("# EXAMPLES")[1].split("# FINAL SELF-CHECK")[0]
    return [line for line in examples.splitlines() if line.startswith("{")]


@pytest.mark.parametrize(
    ("name", "schema"),
    [
        ("scope_check", ScopeVerdict),
        ("discover", DiscoverOutput),
        ("clarify", ClarifyOutput),
        ("draft", DraftOutput),
        ("criteria", CriteriaOutput),
        ("critique", CritiqueOutput),
    ],
)
def test_every_few_shot_output_matches_the_output_schema(
    name: str, schema: type[BaseModel]
) -> None:
    outputs = _example_outputs(name)
    assert 3 <= len(outputs) <= 5
    for text in outputs:
        schema.model_validate_json(text)


def test_every_prompt_has_a_schema_check() -> None:
    covered = {"scope_check", "discover", "clarify", "draft", "criteria", "critique"}
    assert set(NAMES) == covered
