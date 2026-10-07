"""Responders that answer the run's pauses without a person at the keyboard.

``AnswersFileResponder`` is the only way to run without a terminal. It answers only
what the file says. It never fills a gap on its own: an unanswered question, an
unset go-ahead or an undecided review leaves the run paused.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from story_agent.clarify.answers import AnswersFile, read_answers_data
from story_agent.config import ConfigError
from story_agent.memory.proposals import Decision
from story_agent.pipeline.review import ReviewAction
from story_agent.replies import AnswersReply, ConflictsReply, GateReply, MemoryReply, ReviewReply

Reply = AnswersReply | ConflictsReply | GateReply | ReviewReply | MemoryReply
Policy = Literal["stop", "judgment", "defer"]


class RunAnswers(AnswersFile):
    """An --answers file for a whole run.

    ``answers`` maps a question id or a category id to the reply, exactly as it would
    be typed ("yes" confirms a remembered answer). Everything the file leaves out stops
    the run, unless ``unanswered`` says to record a judgment or a deferral.
    """

    unanswered: Policy = "stop"
    on_conflict: Literal["stop", "replace", "exception"] = "stop"
    review: Literal["none", "approve_all"] | list[ReviewAction] = "none"
    memory: Literal["none", "approve_all"] = "none"


def load_run_answers(path: Path) -> RunAnswers:
    """Read and check an --answers file."""
    try:
        return RunAnswers.model_validate(read_answers_data(path))
    except ValidationError as exc:
        fields = ", ".join(".".join(str(p) for p in e["loc"]) for e in exc.errors())
        raise ConfigError(f"invalid answers file {path}: check {fields}") from exc


_WORDING = {"judgment": "use your judgment", "defer": "defer"}


class AnswersFileResponder:
    """Answers pauses from a ``RunAnswers`` file. ``stopped`` says why it gave up."""

    def __init__(self, answers: RunAnswers) -> None:
        """Keep the file contents."""
        self.file = answers
        self.stopped = ""
        self._free_text_sent = False
        self._review_sent = False

    def respond(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Answer one pause, or return None and set ``stopped``."""
        handler = getattr(self, f"_{payload['kind']}", None)
        if handler is None:
            self.stopped = f"unknown pause {payload['kind']}"
            return None
        reply: Reply | None = handler(payload)
        return None if reply is None else reply.model_dump(mode="json")

    def _answers(self, payload: dict[str, Any]) -> AnswersReply | None:
        replies: dict[str, str] = {}
        missing: list[str] = []
        for q in payload["questions"]:
            text = self.file.answers.get(q["id"]) or self.file.answers.get(q["category"])
            if text:
                replies[q["id"]] = text
            elif self.file.unanswered == "stop":
                missing.append(q["category"])
            else:
                replies[q["id"]] = _WORDING[self.file.unanswered]
        if missing:
            self.stopped = f"the answers file has no answer for: {', '.join(missing)}"
            return None
        free_text = "" if self._free_text_sent else self.file.free_text
        self._free_text_sent = True
        return AnswersReply(answers=replies, free_text=free_text)

    def _conflicts(self, payload: dict[str, Any]) -> ConflictsReply | None:
        if self.file.on_conflict == "stop":
            ids = ", ".join(c["category"] for c in payload["conflicts"])
            self.stopped = f"an answer contradicts memory ({ids}); set on_conflict in the file"
            return None
        choice = self.file.on_conflict
        return ConflictsReply(resolutions={c["question_id"]: choice for c in payload["conflicts"]})

    def _gate(self, payload: dict[str, Any]) -> GateReply | None:
        if not self.file.go_ahead:
            self.stopped = "the answers file does not set go_ahead: true"
            return None
        if payload["ready"]:
            return GateReply(decision="go", confirmed_by="answers_file")
        if self.file.unanswered == "stop":
            items = "; ".join(payload["unresolved_must_have"])
            self.stopped = f"must-have items are unresolved: {items}"
            return None
        return GateReply(decision=self.file.unanswered, confirmed_by="answers_file")

    def _review(self, payload: dict[str, Any]) -> ReviewReply | None:
        if self.file.review == "none" or self._review_sent:
            self.stopped = (
                "stories are waiting for review"
                if self.file.review == "none"
                else "some stories could not be approved; review them with resume"
            )
            return None
        self._review_sent = True
        if self.file.review == "approve_all":
            actions = [ReviewAction(story_id=s["id"], action="approve") for s in payload["stories"]]
            return ReviewReply(actions=actions)
        return ReviewReply(actions=list(self.file.review))

    def _memory(self, payload: dict[str, Any]) -> MemoryReply:
        if self.file.memory == "approve_all":
            return MemoryReply(
                decisions={p["id"]: Decision(action="approve") for p in payload["proposals"]}
            )
        return MemoryReply()
