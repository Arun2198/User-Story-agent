"""Feedback signals from a finished run: what the person did with the output.

Only counts and rates. The metric names that also exist in the offline app eval use the
same names, so a drift check can compare them directly.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from story_agent.clarify.answers import FINAL_PREFIX
from story_agent.schema import AnswerKind, RunState, StoryStatus


class FeedbackSignals(BaseModel):
    """Signals from one run."""

    model_config = ConfigDict(extra="forbid")

    stories: int
    approved: int
    edited: int
    rejected: int
    questions_asked: int
    answered_other: int
    defaults_confirmed: int
    defaults_rejected: int
    forced_resolutions: int
    rounds: int
    revision_loops: int
    memory_proposed: int
    memory_saved: int
    memory_rejected: int
    mean_edit_distance: float
    tokens: int
    cost_usd: float
    llm_calls: int

    def metrics(self) -> dict[str, float]:
        """Return flat metrics. Shared names match ``baselines/*/app.json``."""
        decided = self.approved + self.edited + self.rejected
        asked = max(1, self.questions_asked)
        return {
            "questions_asked": float(self.questions_asked),
            "rounds_to_readiness": float(self.rounds),
            "other_answer_rate": self.answered_other / asked,
            "forced_resolution_rate": self.forced_resolutions / asked,
            "revision_loops": float(self.revision_loops),
            "stories": float(self.stories),
            "tokens": float(self.tokens),
            "cost_usd": self.cost_usd,
            "llm_calls": float(self.llm_calls),
            "approve_rate": self.approved / decided if decided else 0.0,
            "edit_rate": self.edited / decided if decided else 0.0,
            "reject_rate": self.rejected / decided if decided else 0.0,
            "mean_edit_distance": self.mean_edit_distance,
            "memory_reject_rate": self.memory_rejected / self.memory_proposed
            if self.memory_proposed
            else 0.0,
            "defaults_rejected": float(self.defaults_rejected),
        }


def extract_feedback(state: RunState, llm_calls: int = 0) -> FeedbackSignals:
    """Count what the person decided, answered and approved in one run."""
    count = {s: sum(1 for st in state.stories if st.status is s) for s in StoryStatus}
    distances = [float(e["edit_distance"]) for e in state.review_log if "edit_distance" in e]
    outcome = state.memory_outcome
    saved = len(outcome.get("saved", [])) + len(outcome.get("refreshed", []))
    rejected = len(outcome.get("rejected", []))
    return FeedbackSignals(
        stories=len(state.stories) - count[StoryStatus.REJECTED],
        approved=count[StoryStatus.APPROVED],
        edited=count[StoryStatus.EDITED],
        rejected=count[StoryStatus.REJECTED],
        questions_asked=sum(len(r.questions) for r in state.rounds),
        answered_other=sum(1 for a in state.answers if a.kind is AnswerKind.OTHER),
        defaults_confirmed=sum(1 for a in state.answers if a.kind is AnswerKind.MEMORY_CONFIRMED),
        defaults_rejected=len(state.memory_rejected),
        forced_resolutions=sum(1 for a in state.answers if a.question_id.startswith(FINAL_PREFIX)),
        rounds=len(state.rounds),
        revision_loops=state.critique_loops,
        memory_proposed=saved + rejected,
        memory_saved=saved,
        memory_rejected=rejected,
        mean_edit_distance=round(sum(distances) / len(distances), 4) if distances else 0.0,
        tokens=state.tokens_used,
        cost_usd=round(state.cost_usd, 6),
        llm_calls=llm_calls,
    )
