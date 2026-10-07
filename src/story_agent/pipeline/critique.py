"""Stage 8: critique. Deterministic checks first, then a model review for meaning.

Blocking findings have severity ERROR and trigger a revision. Warnings are reported
to the reviewer. Coverage is checked against the confirmed requirements, which come
from the discovery map.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from story_agent.deps import StageDeps
from story_agent.guardrails.grounding import GroundingVerifier
from story_agent.guardrails.injection import wrap_untrusted
from story_agent.llm import LLMRequest, Usage
from story_agent.pipeline.postprocess import (
    check_id_format_and_stability,
    find_duplicates,
    find_uncovered,
    similarity,
)
from story_agent.prompts import load_prompt
from story_agent.schema import Finding, RunState, Severity, Story

PROMPT_ID = "critique"
INVEST_CHECKS = ("independent", "negotiable", "valuable", "estimable", "small", "testable")


class StoryNote(BaseModel):
    """A per-story note from the reviewer."""

    model_config = ConfigDict(extra="forbid")

    story_id: str
    negotiable: bool = True
    issues: list[str] = Field(default_factory=list, max_length=5)


class StoryPair(BaseModel):
    """Two or more stories that conflict or repeat each other."""

    model_config = ConfigDict(extra="forbid")

    story_ids: list[str] = Field(min_length=2, max_length=4)
    reason: str = Field(min_length=1, max_length=300)


class SplitSuggestion(BaseModel):
    """A story that should be split."""

    model_config = ConfigDict(extra="forbid")

    story_id: str
    reason: str = Field(min_length=1, max_length=300)


class CritiqueOutput(BaseModel):
    """Model output for the critique stage."""

    model_config = ConfigDict(extra="forbid")

    story_notes: list[StoryNote] = Field(default_factory=list, max_length=40)
    duplicates: list[StoryPair] = Field(default_factory=list, max_length=20)
    contradictions: list[StoryPair] = Field(default_factory=list, max_length=20)
    split_suggestions: list[SplitSuggestion] = Field(default_factory=list, max_length=20)


@dataclass
class CritiqueResult:
    """Findings, per-story INVEST scores and call details."""

    findings: list[Finding] = field(default_factory=list)
    invest: dict[str, float] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    request: LLMRequest | None = None

    @property
    def blocking(self) -> list[Finding]:
        """Findings that require a revision."""
        return [f for f in self.findings if f.severity is Severity.ERROR]


def _f(code: str, message: str, location: str | None, error: bool) -> Finding:
    return Finding(
        code=code,
        message=message,
        severity=Severity.ERROR if error else Severity.WARNING,
        location=location,
    )


def _has_cycle(stories: Sequence[Story]) -> bool:
    graph = {s.id: set(s.dependencies) for s in stories}
    state: dict[str, int] = {}

    def visit(node: str) -> bool:
        if state.get(node) == 1:
            return True
        if state.get(node) == 2:
            return False
        state[node] = 1
        found = any(visit(n) for n in graph.get(node, ()) if n in graph)
        state[node] = 2
        return found

    return any(visit(n) for n in graph)


def deterministic_findings(
    deps: StageDeps, state: RunState, stories: Sequence[Story], previous: Sequence[Story]
) -> list[Finding]:
    """Run the checks that need no model."""
    std = deps.config.standards
    drafting = std.get("drafting", {})
    cap = state.preferences.max_criteria_per_story or int(std.get("max_criteria_per_story", 8))
    max_reqs = int(drafting.get("max_requirements_per_story", 4))
    max_estimate = int(drafting.get("max_estimate_before_split", 8))
    findings: list[Finding] = []
    findings.extend(find_uncovered(stories, state.requirements))
    findings.extend(find_duplicates(stories, float(drafting.get("duplicate_similarity", 0.9))))
    findings.extend(check_id_format_and_stability(stories, previous))
    verifier = GroundingVerifier(state, deps.config.guardrails.grounding)
    for story in stories:
        findings.extend(verifier.check_story(story))
        if not story.acceptance_criteria:
            findings.append(_f("NOT_READY", "no acceptance criteria", story.id, True))
        too_big = (
            len(story.acceptance_criteria) > cap
            or len(story.requirement_ids) > max_reqs
            or (story.estimate or 0) > max_estimate
        )
        if too_big:
            findings.append(_f("OVERSIZED", "story is too large; split it", story.id, True))
        if len(story.dependencies) > 2:
            findings.append(_f("INVEST_DEPENDENT", "story depends on many others", story.id, False))
        if similarity(story.want, story.benefit) >= 0.8:
            findings.append(
                _f("INVEST_BENEFIT_RESTATES_WANT", "benefit repeats the want", story.id, False)
            )
        if story.estimate is None:
            findings.append(_f("INVEST_NOT_ESTIMATED", "story has no estimate", story.id, False))
    if _has_cycle(stories):
        findings.append(_f("DEPENDENCY_CYCLE", "story dependencies form a cycle", None, True))
    return findings


def render_for_review(stories: Sequence[Story], state: RunState) -> str:
    """Render stories with criteria for the reviewer."""
    by_id = {r.id: r for r in state.requirements}
    blocks: list[str] = []
    for s in stories:
        lines = [f"{s.id} {s.title} ({s.persona}): I want {s.want}, so that {s.benefit}."]
        lines.extend(f"  {by_id[r].id}: {by_id[r].text}" for r in s.requirement_ids if r in by_id)
        lines.extend(
            f"  AC [{c.kind.value}] Given {c.given}; When {c.when}; Then {c.then}"
            for c in s.acceptance_criteria
        )
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def invest_scores(stories: Sequence[Story], findings: Sequence[Finding]) -> dict[str, float]:
    """Return the share of the six INVEST checks each story passes, from the findings."""
    failing: dict[str, set[str]] = {s.id: set() for s in stories}
    mapping = {
        "INVEST_DEPENDENT": "independent",
        "DEPENDENCY_CYCLE": "independent",
        "INVEST_NOT_NEGOTIABLE": "negotiable",
        "INVEST_BENEFIT_RESTATES_WANT": "valuable",
        "INVEST_NOT_ESTIMATED": "estimable",
        "OVERSIZED": "small",
        "SPLIT_SUGGESTED": "small",
        "NOT_READY": "testable",
        "CRITERIA_MISSING": "testable",
        "CRITERION_VAGUE": "testable",
    }
    for finding in findings:
        check = mapping.get(finding.code)
        if check is None or not finding.location:
            continue
        for story_id in finding.location.split(","):
            if story_id in failing:
                failing[story_id].add(check)
    return {sid: round(1 - len(bad) / len(INVEST_CHECKS), 3) for sid, bad in failing.items()}


def _model_findings(output: CritiqueOutput, valid: set[str]) -> list[Finding]:
    findings: list[Finding] = []
    for note in output.story_notes:
        if note.story_id not in valid:
            continue
        if not note.negotiable:
            findings.append(
                _f(
                    "INVEST_NOT_NEGOTIABLE",
                    "; ".join(note.issues) or "dictates a solution",
                    note.story_id,
                    False,
                )
            )
        findings.extend(
            _f("REVIEW_NOTE", issue, note.story_id, False)
            for issue in note.issues
            if note.negotiable
        )
    for code, pairs in (
        ("DUPLICATE_STORY", output.duplicates),
        ("CONTRADICTION", output.contradictions),
    ):
        for pair in pairs:
            ids = [i for i in pair.story_ids if i in valid]
            if len(ids) >= 2:
                findings.append(_f(code, pair.reason, ",".join(sorted(ids)), True))
    findings.extend(
        _f("SPLIT_SUGGESTED", s.reason, s.story_id, False)
        for s in output.split_suggestions
        if s.story_id in valid
    )
    return findings


def _dedupe(findings: Sequence[Finding]) -> list[Finding]:
    seen: set[tuple[str, str | None]] = set()
    out: list[Finding] = []
    for f in findings:
        key = (f.code, f.location)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def run_critique(
    deps: StageDeps, state: RunState, stories: Sequence[Story], previous: Sequence[Story] = ()
) -> CritiqueResult:
    """Run deterministic checks and one model review. Findings are deduplicated."""
    findings = deterministic_findings(deps, state, stories, previous)
    prompt = load_prompt(deps.prompts_dir, PROMPT_ID)
    std = deps.config.standards
    invest = "\n".join(f"{k}: {v}" for k, v in std.get("invest", {}).items())
    user = "\n".join(
        [
            "INVEST RULES:",
            invest,
            "STORIES:",
            wrap_untrusted("stories", render_for_review(stories, state)),
        ]
    )
    request = LLMRequest(PROMPT_ID, prompt.text, user, deps.config.models.generator)
    reply = deps.client.complete(request, CritiqueOutput)
    findings.extend(_model_findings(reply.value, {s.id for s in stories}))
    findings = _dedupe(findings)
    return CritiqueResult(
        findings, invest_scores(stories, findings), reply.usage, reply.cost_usd, request
    )
