"""Detect answers that contradict a remembered default."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from story_agent.schema import AnswerKind, RunState

Resolution = Literal["replace", "exception"]
_CHECKED = {AnswerKind.OPTION, AnswerKind.OTHER}


@dataclass(frozen=True)
class MemoryConflict:
    """The user answered differently from what memory says."""

    question_id: str
    category: str
    memory_id: str
    remembered: str
    new: str
    last_confirmed_at: datetime
    stale: bool


def _norm(text: str) -> str:
    return " ".join(text.casefold().split()).rstrip(".!")


def detect_conflicts(state: RunState) -> list[MemoryConflict]:
    """Return one conflict per answer that differs from its question's remembered default.

    Confirming the default, deferring, judgment and not-applicable are not conflicts.
    """
    answers = {a.question_id: a for a in state.answers}
    found: list[MemoryConflict] = []
    for round_ in state.rounds:
        for question in round_.questions:
            default = question.remembered_default
            answer = answers.get(question.id)
            if default is None or answer is None or answer.kind not in _CHECKED:
                continue
            if _norm(answer.value) == _norm(default.value):
                continue
            found.append(
                MemoryConflict(
                    question.id,
                    question.category,
                    default.memory_id,
                    default.value,
                    answer.value,
                    default.last_confirmed_at,
                    default.stale,
                )
            )
    return found


def resolve_conflict(state: RunState, question_id: str, resolution: Resolution) -> None:
    """Record the choice: ``replace`` the saved answer, or a one-off ``exception``."""
    if resolution not in ("replace", "exception"):
        raise ValueError("resolution must be replace or exception")
    if question_id not in {c.question_id for c in detect_conflicts(state)}:
        raise ValueError(f"no conflict for {question_id}")
    state.conflict_resolutions[question_id] = resolution


def unresolved(state: RunState) -> list[MemoryConflict]:
    """Return conflicts the user has not decided yet."""
    return [c for c in detect_conflicts(state) if c.question_id not in state.conflict_resolutions]
