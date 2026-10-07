"""Checks that run before anything is published."""

from __future__ import annotations

from collections.abc import Mapping

from story_agent.config import AppConfig
from story_agent.guardrails.grounding import GroundingVerifier
from story_agent.guardrails.redaction import Redactor
from story_agent.pipeline.review import publishable
from story_agent.schema import RunState, StoryStatus

MIN_LEAK_LEN = 4


class PublishBlocked(RuntimeError):  # noqa: N818  (reads better than PublishBlockedError)
    """The run is not fit to publish. The message says why."""


def check_publishable(state: RunState, config: AppConfig) -> None:
    """Refuse to publish unless every story was decided and every approved story is grounded."""
    waiting = [s.id for s in state.stories if s.status is StoryStatus.DRAFT]
    if waiting:
        raise PublishBlocked(f"{len(waiting)} stories still wait for review: {', '.join(waiting)}")
    stories = publishable(state)
    if not stories:
        raise PublishBlocked("there are no approved stories to publish")
    report = GroundingVerifier(state, config.guardrails.grounding).verify(
        stories, state.requirements
    )
    errors = [f for f in report.findings if f.severity.value == "error"]
    if errors:
        codes = ", ".join(sorted({f.code for f in errors}))
        raise PublishBlocked(f"approved stories fail the grounding check ({codes})")


def assert_no_pii(text: str, redactions: Mapping[str, str]) -> None:
    """Refuse output that holds sensitive values, or any value that was redacted from the input."""
    if Redactor(redactions).count(text):
        raise PublishBlocked("the output contains sensitive values")
    lowered = text.casefold()
    for value in redactions.values():
        if len(value) >= MIN_LEAK_LEN and value.casefold() in lowered:
            raise PublishBlocked("the output repeats a value that was redacted from the input")
