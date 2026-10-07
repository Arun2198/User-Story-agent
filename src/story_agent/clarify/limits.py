"""Clarification limits from config/standards.yaml."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

HARD_MAX_QUESTIONS = 6
HARD_MAX_ROUNDS = 3


class ClarifyLimits(BaseModel):
    """How many questions and rounds are allowed. Config may lower the hard caps, not raise them."""

    model_config = ConfigDict(extra="forbid")

    max_questions_per_round: int = Field(default=HARD_MAX_QUESTIONS, ge=1, le=HARD_MAX_QUESTIONS)
    max_rounds: int = Field(default=HARD_MAX_ROUNDS, ge=1, le=HARD_MAX_ROUNDS)
    free_text_prompt: str = "Anything else you want to add or change?"

    @classmethod
    def from_standards(cls, standards: dict[str, Any]) -> ClarifyLimits:
        """Read the ``clarification`` section of standards.yaml."""
        return cls.model_validate(standards.get("clarification", {}))
