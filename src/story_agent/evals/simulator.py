"""A simulated user that answers from an eval case's hidden answer key.

It is deterministic. It never sees the gold labels, only the answer key, the default
reply and the free-text reply. A remembered default is confirmed with "yes" only when
the key still agrees with it; otherwise the user types the key's answer, which raises a
memory conflict, as a real user would.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from story_agent.evals.cases import EvalCase
from story_agent.memory.proposals import Decision, Proposal
from story_agent.pipeline.review import ReviewAction
from story_agent.schema import Question, Story


def _norm(text: str) -> str:
    return " ".join(text.casefold().split()).rstrip(".!")


@dataclass
class SimulatedUser:
    """Answers questions for one case."""

    case: EvalCase
    answer_key: dict[str, str] = field(default_factory=dict)
    typed: int = 0
    judgments: int = 0
    confirmed_defaults: int = 0
    free_text_sent: bool = False

    def __post_init__(self) -> None:
        """Default to the case's own answer key."""
        if not self.answer_key:
            self.answer_key = dict(self.case.answer_key)

    def answer(self, question: Question) -> str:
        """Return the reply to one question."""
        wanted = self.answer_key.get(question.category)
        default = question.remembered_default
        if default is not None and (wanted is None or _norm(wanted) == _norm(default.value)):
            self.confirmed_defaults += 1
            return "yes"
        if wanted is not None:
            self.typed += 1
            return wanted
        self.judgments += 1
        return self.case.default_reply

    def answers_for(self, questions: Sequence[Question]) -> dict[str, str]:
        """Return replies for a whole round."""
        return {q.id: self.answer(q) for q in questions}

    def wants_more(self, open_optional: Sequence[str]) -> bool:
        """Say yes to another round when optional topics remain that this user knows about."""
        return any(category in self.answer_key for category in open_optional)

    def free_text(self) -> str:
        """Return the "anything else" reply once, then nothing."""
        if self.free_text_sent:
            return ""
        self.free_text_sent = True
        return self.case.free_text_reply

    def review(self, stories: Sequence[Story]) -> list[ReviewAction]:
        """Approve every story."""
        return [ReviewAction(story_id=s.id, action="approve") for s in stories]

    def memory_decisions(self, proposals: Sequence[Proposal]) -> dict[str, Decision]:
        """Approve every proposal."""
        return {p.id: Decision(action="approve") for p in proposals}


class SimulatedResponder:
    """Lets a ``SimulatedUser`` answer the run graph's pauses, like a person would."""

    def __init__(self, user: SimulatedUser) -> None:
        """Wrap the simulated user."""
        self.user = user

    def respond(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Answer one pause."""
        kind = payload["kind"]
        if kind == "answers":
            questions = [Question.model_validate(q) for q in payload["questions"]]
            return {
                "answers": self.user.answers_for(questions),
                "free_text": self.user.free_text(),
            }
        if kind == "conflicts":
            return {"resolutions": {c["question_id"]: "replace" for c in payload["conflicts"]}}
        if kind == "gate":
            more = payload["rounds_used"] < payload["rounds_max"] and self.user.wants_more(
                payload["open_optional_categories"]
            )
            if payload["ready"] and more:
                decision = "more"
            else:
                decision = "go" if payload["ready"] else "judgment"
            return {"decision": decision, "confirmed_by": "user"}
        if kind == "review":
            stories = [Story.model_validate(s) for s in payload["stories"]]
            return {"actions": [a.model_dump(mode="json") for a in self.user.review(stories)]}
        decisions = {p["id"]: {"action": "approve"} for p in payload["proposals"]}
        return {"decisions": decisions}
