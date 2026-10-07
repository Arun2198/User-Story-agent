import re
from pathlib import Path

import pytest

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
