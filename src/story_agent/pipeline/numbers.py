"""Number grounding: a quick, deterministic guard against invented limits and amounts."""

from __future__ import annotations

import re
from collections.abc import Iterable

from story_agent.schema import RunState

_NUMBER = re.compile(r"(?<![\w.-])\d[\d,]*(?:\.\d+)?%?(?![\w-])")
_ALWAYS_OK = frozenset({"0", "1"})


def numbers_in(text: str) -> set[str]:
    """Return the numbers in ``text``, without thousands separators."""
    found: set[str] = set()
    for token in _NUMBER.findall(text):
        cleaned = token.replace(",", "")
        if cleaned.endswith(".0"):
            cleaned = cleaned[:-2]
        found.add(cleaned)
    return found


def known_numbers(state: RunState) -> set[str]:
    """Return every number the user has given: scenario, notes, answers, free text, edits."""
    texts: list[str] = [state.redacted_text, state.redacted_notes, *state.review_notes]
    texts.extend(r.free_text_reply for r in state.rounds)
    texts.extend(a.value for a in state.answers)
    texts.extend(r.text for r in state.requirements)
    known: set[str] = set()
    for text in texts:
        known |= numbers_in(text)
    return known


def ungrounded_numbers(texts: Iterable[str], known: set[str]) -> set[str]:
    """Return numbers in ``texts`` that the user never gave. 0 and 1 are always allowed."""
    used: set[str] = set()
    for text in texts:
        used |= numbers_in(text)
    return used - known - _ALWAYS_OK
