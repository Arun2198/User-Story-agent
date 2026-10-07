"""LLM judge for end-to-end runs: a fixed rubric, scored by a different model.

The judge reads the scenario, the questions and answers, the requirements and the
stories, and scores groundedness, completeness and testability from 1 to 5. It also
lists claims no source supports. It needs a live model; offline runs skip it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from story_agent.config import AppConfig
from story_agent.guardrails.injection import wrap_untrusted
from story_agent.llm import LLMClient, LLMRequest
from story_agent.prompts import load_prompt
from story_agent.schema import RunState, StoryStatus

PROMPT_ID = "judge"


class JudgeOutput(BaseModel):
    """Rubric scores from the judge."""

    model_config = ConfigDict(extra="forbid")

    groundedness: int = Field(ge=1, le=5)
    completeness: int = Field(ge=1, le=5)
    testability: int = Field(ge=1, le=5)
    unsupported_claims: list[str] = Field(default_factory=list, max_length=10)
    rationale: str = Field(max_length=400)


@dataclass(frozen=True)
class JudgeScores:
    """Scores scaled to 0..1, plus the unsupported claims."""

    groundedness: float
    completeness: float
    testability: float
    unsupported_claims: int


def render_run(state: RunState) -> str:
    """Render everything the judge needs, as untrusted data blocks."""
    qa = [f"{q.id} [{q.category}] {q.question}" for r in state.rounds for q in r.questions]
    answers = [f"{a.question_id} ({a.kind.value}): {a.value}" for a in state.answers]
    requirements = [
        f"{r.id}{' [ASSUMED]' if r.assumed else ''} {r.text}" for r in state.requirements
    ]
    stories = []
    for s in state.stories:
        if s.status is StoryStatus.REJECTED:
            continue
        refs = ", ".join(s.requirement_ids)
        head = f"{s.id} As a {s.persona}, I want {s.want}, so that {s.benefit}."
        lines = [f"{head} Requirements: {refs}"]
        lines.extend(
            f"  Given {c.given}; When {c.when}; Then {c.then}" for c in s.acceptance_criteria
        )
        stories.append("\n".join(lines))
    return "\n".join(
        [
            "SCENARIO:",
            wrap_untrusted("scenario", f"{state.redacted_text}\n{state.redacted_notes}".strip()),
            "QUESTIONS AND ANSWERS:",
            wrap_untrusted("questions", "\n".join(qa) + "\n" + "\n".join(answers)),
            "REQUIREMENTS:",
            wrap_untrusted("requirements", "\n".join(requirements)),
            "STORIES:",
            wrap_untrusted("stories", "\n".join(stories)),
        ]
    )


def judge_run(
    client: LLMClient, config: AppConfig, prompts_dir: Path, state: RunState
) -> JudgeScores:
    """Score one run with the judge model."""
    prompt = load_prompt(prompts_dir, PROMPT_ID)
    request = LLMRequest(PROMPT_ID, prompt.text, render_run(state), config.models.judge)
    out = client.complete(request, JudgeOutput).value
    return JudgeScores(
        _scale(out.groundedness),
        _scale(out.completeness),
        _scale(out.testability),
        len(out.unsupported_claims),
    )


def _scale(value: int) -> float:
    return round((value - 1) / 4, 3)
