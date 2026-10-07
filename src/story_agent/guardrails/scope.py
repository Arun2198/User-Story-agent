"""Scope guard: deterministic refusal of override, prompt-extraction and memory-dump requests.

The model-based intent classifier lives in ``pipeline/scope_check.py``. This guard
runs first, needs no model call and cannot be argued with.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from story_agent.guardrails.injection import InjectionDetector
from story_agent.schema import Intent


class ScopeCategory(StrEnum):
    """What a request is, as far as scope is concerned."""

    GENERATE_STORIES = "generate_stories"
    ANSWER_CLARIFICATION = "answer_clarification"
    REFINE_STORIES = "refine_stories"
    EXPORT_STORIES = "export_stories"
    MANAGE_MEMORY = "manage_memory"
    OUT_OF_SCOPE = "out_of_scope"
    OVERRIDE_ATTEMPT = "override_attempt"
    PROMPT_EXTRACTION = "prompt_extraction"
    MEMORY_EXTRACTION = "memory_extraction"


@dataclass(frozen=True)
class ScopeDecision:
    """Outcome of a scope check."""

    allowed: bool
    category: ScopeCategory
    intent: Intent | None = None
    message: str = ""


_MEMORY_WORDS = re.compile(
    r"\bmemor(?:y|ies)\b|saved\s+(?:entries|answers?)|stored\s+(?:entries|answers?|data)", re.I
)
_MARKER = re.compile(r"\[QUARANTINED #\d+\]")


def category_for(codes: set[str], text: str) -> ScopeCategory:
    """Map injection codes found in a request to a scope category."""
    if "INJ_EXTRACTION" in codes:
        return (
            ScopeCategory.MEMORY_EXTRACTION
            if _MEMORY_WORDS.search(text)
            else ScopeCategory.PROMPT_EXTRACTION
        )
    return ScopeCategory.OVERRIDE_ATTEMPT


class ScopeGuard:
    """Refuses requests that are mostly an attack on the agent's rules."""

    def __init__(self, refusal_message: str, min_remaining_chars: int = 40) -> None:
        """Set the fixed refusal text and how much real content must remain."""
        self._message = refusal_message
        self._min_remaining = min_remaining_chars
        self._detector = InjectionDetector()

    @property
    def refusal_message(self) -> str:
        """The single refusal message used for every refusal."""
        return self._message

    def refuse(self, category: ScopeCategory) -> ScopeDecision:
        """Build a refusal decision."""
        return ScopeDecision(False, category, None, self._message)

    def evaluate(self, text: str) -> ScopeDecision | None:
        """Return a refusal when ``text`` is mainly an attack, else None.

        Attack sentences inside an otherwise real scenario are left to the
        injection scan, which quarantines and reports them.
        """
        result = self._detector.quarantine(text)
        if not result.items:
            return None
        remaining = _MARKER.sub("", result.text).strip()
        if len(remaining) >= self._min_remaining:
            return None
        codes = {code for item in result.items for code in item.codes}
        return self.refuse(category_for(codes, text))
