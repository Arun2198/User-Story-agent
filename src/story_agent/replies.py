"""What a person (or an answers file) sends back when the run pauses.

Each pause has a ``kind`` in its payload and a matching reply model here. The graph
validates every reply before using it, so a malformed reply asks again.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from story_agent.memory.proposals import Decision
from story_agent.pipeline.review import ReviewAction

PauseKind = Literal["answers", "conflicts", "gate", "review", "memory"]
GateDecision = Literal["go", "more", "judgment", "defer"]


class _Reply(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnswersReply(_Reply):
    """Answers by question id (or category id) and an optional free-text reply."""

    answers: dict[str, str] = Field(default_factory=dict)
    free_text: str = ""


class ConflictsReply(_Reply):
    """For each contradicted remembered answer: replace it, or keep it and note an exception."""

    resolutions: dict[str, Literal["replace", "exception"]] = Field(default_factory=dict)


class GateReply(_Reply):
    """The readiness decision. ``confirmed_by`` says who gave it."""

    decision: GateDecision
    confirmed_by: Literal["user", "answers_file"]


class ReviewReply(_Reply):
    """Approve, edit or reject stories."""

    actions: list[ReviewAction] = Field(default_factory=list)


class MemoryReply(_Reply):
    """A decision per proposed memory entry. Entries left out are rejected."""

    decisions: dict[str, Decision] = Field(default_factory=dict)
