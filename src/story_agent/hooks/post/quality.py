"""Post-hooks that check the story set: duplicates, id stability, coverage.

While drafting, the critique loop handles these findings, so the hooks only report.
At review and publish they block, so a set with duplicates, unstable ids or
uncovered requirements cannot go out.
"""

from __future__ import annotations

from story_agent.hooks.base import HookContext, passed
from story_agent.pipeline.postprocess import (
    check_id_format_and_stability,
    find_duplicates,
    find_uncovered,
)
from story_agent.schema import (
    Finding,
    HookAction,
    HookPhase,
    HookResult,
    Severity,
    Story,
    StoryStatus,
)

STRICT_STAGES = frozenset({"human_review", "publish"})


def _result(ctx: HookContext, findings: list[Finding]) -> HookResult:
    errors = [f for f in findings if f.severity is Severity.ERROR]
    if errors and ctx.stage in STRICT_STAGES:
        return HookResult(action=HookAction.BLOCK, findings=findings)
    return HookResult(action=HookAction.PASS, findings=findings)


def _live(ctx: HookContext) -> list[Story]:
    return [s for s in ctx.state.stories if s.status is not StoryStatus.REJECTED]


class DeduplicationHook:
    """Reports near-duplicate stories."""

    name = "deduplication"
    phase = HookPhase.POST

    def run(self, ctx: HookContext) -> HookResult:
        """Compare stories pairwise using the configured similarity."""
        stories = _live(ctx)
        if not stories:
            return passed()
        threshold = float(ctx.config.standards.get("drafting", {}).get("duplicate_similarity", 0.9))
        return _result(ctx, find_duplicates(stories, threshold))


class IdStabilityHook:
    """Reports malformed, duplicate or unstable story ids."""

    name = "id_stability"
    phase = HookPhase.POST

    def run(self, ctx: HookContext) -> HookResult:
        """Check ids against ``ctx.data['previous_stories']`` when present."""
        stories = ctx.state.stories
        if not stories:
            return passed()
        return _result(
            ctx, check_id_format_and_stability(stories, ctx.data.get("previous_stories", []))
        )


class CoverageHook:
    """Reports requirements that no story covers (discovery-map coverage)."""

    name = "coverage"
    phase = HookPhase.POST

    def run(self, ctx: HookContext) -> HookResult:
        """Check every requirement is covered by a story, or left out by a rejected one."""
        if not ctx.state.requirements:
            return passed()
        # A rejected story is an explicit decision to leave its requirements out.
        return _result(ctx, find_uncovered(ctx.state.stories, ctx.state.requirements))
