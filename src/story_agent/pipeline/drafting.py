"""Stages 6 to 8 together: draft, criteria and critique with at most two revisions.

The clarification gate is checked first. Requirements come from the settled
discovery map, never from the model.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from story_agent.clarify.readiness import require_go_ahead
from story_agent.deps import StageDeps
from story_agent.discovery.packs import Checklist
from story_agent.llm import LLMRequest, Usage
from story_agent.pipeline.criteria import run_criteria
from story_agent.pipeline.critique import CritiqueResult, run_critique
from story_agent.pipeline.draft import run_draft
from story_agent.pipeline.postprocess import confidence, story_identity
from story_agent.pipeline.requirements import derive_requirements
from story_agent.schema import Finding, RunState, Severity, Story


class NothingToDraftError(RuntimeError):
    """There are no confirmed requirements to write stories from."""


@dataclass
class DraftingResult:
    """Final stories, the last findings and what the loop cost."""

    stories: list[Story]
    findings: list[Finding]
    invest: dict[str, float]
    loops: int
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    requests: list[LLMRequest] = field(default_factory=list)


def _same_text(a: Story, b: Story) -> bool:
    return (a.title, a.persona, a.want, a.benefit, a.requirement_ids) == (
        b.title,
        b.persona,
        b.want,
        b.benefit,
        b.requirement_ids,
    )


def _carry_criteria(new: Sequence[Story], old: Sequence[Story]) -> tuple[list[Story], set[str]]:
    """Reuse criteria for unchanged stories. Return stories and the ids needing criteria."""
    previous = {story_identity(s): s for s in old}
    carried: list[Story] = []
    needs: set[str] = set()
    for story in new:
        before = previous.get(story_identity(story))
        if before is not None and before.id == story.id and _same_text(before, story):
            carried.append(
                story.model_copy(update={"acceptance_criteria": before.acceptance_criteria})
            )
        else:
            carried.append(story)
            needs.add(story.id)
    return carried, needs


def _tally(out: DraftingResult, usage: Usage, cost: float, *requests: LLMRequest | None) -> None:
    out.usage = Usage(
        out.usage.input_tokens + usage.input_tokens, out.usage.output_tokens + usage.output_tokens
    )
    out.cost_usd += cost
    out.requests.extend(r for r in requests if r is not None)


def _final_confidence(stories: Sequence[Story], findings: Sequence[Finding]) -> list[Story]:
    warnings: dict[str, int] = {}
    for f in findings:
        if f.severity is Severity.WARNING and f.location:
            for sid in f.location.split(","):
                warnings[sid] = warnings.get(sid, 0) + 1
    return [
        s.model_copy(update={"confidence": confidence(s, warnings.get(s.id, 0))}) for s in stories
    ]


def run_drafting(deps: StageDeps, state: RunState, checklist: Checklist) -> DraftingResult:
    """Draft, write criteria, critique, and revise up to the configured limit.

    Stories left with blocking findings after the last revision are returned with
    those findings so the reviewer sees them. Nothing is silently dropped.
    """
    require_go_ahead(state)
    requirements, req_findings = derive_requirements(state, checklist)
    if not requirements:
        raise NothingToDraftError("no stated or confirmed requirements")
    state.requirements = requirements
    max_loops = int(deps.config.standards.get("drafting", {}).get("max_revision_loops", 2))
    out = DraftingResult([], [], {}, 0)
    draft = run_draft(deps, state, checklist)
    _tally(out, draft.usage, draft.cost_usd, draft.request)
    criteria = run_criteria(deps, state, draft.stories)
    _tally(out, criteria.usage, criteria.cost_usd, *criteria.requests)
    stories = criteria.stories
    stage_findings = [*draft.findings, *criteria.findings]
    critique = run_critique(deps, state, stories, [])
    _tally(out, critique.usage, critique.cost_usd, critique.request)
    blocking = _blocking(stage_findings, critique)
    while blocking and out.loops < max_loops:
        out.loops += 1
        previous = stories
        draft = run_draft(deps, state, checklist, previous, blocking)
        _tally(out, draft.usage, draft.cost_usd, draft.request)
        stories, needs = _carry_criteria(draft.stories, previous)
        stage_findings = list(draft.findings)
        if needs:
            criteria = run_criteria(deps, state, stories, needs)
            _tally(out, criteria.usage, criteria.cost_usd, *criteria.requests)
            stories = criteria.stories
            stage_findings += criteria.findings
        critique = run_critique(deps, state, stories, previous)
        _tally(out, critique.usage, critique.cost_usd, critique.request)
        blocking = _blocking(stage_findings, critique)
    findings = _merge(req_findings + stage_findings, critique.findings)
    out.stories = _final_confidence(stories, findings)
    out.findings = findings
    out.invest = critique.invest
    state.stories = out.stories
    state.findings.extend(findings)
    state.critique_loops = out.loops
    return out


def _blocking(stage_findings: Sequence[Finding], critique: CritiqueResult) -> list[Finding]:
    errors = [f for f in stage_findings if f.severity is Severity.ERROR]
    return _merge(errors, critique.blocking)


def _merge(*groups: Sequence[Finding]) -> list[Finding]:
    seen: set[tuple[str, str | None, str]] = set()
    merged: list[Finding] = []
    for group in groups:
        for f in group:
            key = (f.code, f.location, f.message)
            if key not in seen:
                seen.add(key)
                merged.append(f)
    return merged
