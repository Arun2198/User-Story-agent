"""Stage 6: draft epics and stories from the requirements.

The model groups and words the stories. Code validates every reference, rejects
invented numbers, assigns ids and order, and builds provenance from requirements.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from story_agent.deps import StageDeps
from story_agent.discovery.packs import Checklist
from story_agent.guardrails.injection import wrap_untrusted
from story_agent.llm import LLMRequest, Usage
from story_agent.pipeline.numbers import known_numbers, ungrounded_numbers
from story_agent.pipeline.postprocess import DraftedStory, build_stories
from story_agent.prompts import load_prompt
from story_agent.schema import Finding, Priority, Requirement, RunState, Severity, Story

PROMPT_ID = "draft"


class StoryDraft(BaseModel):
    """One story as the model returns it. No ids, no criteria."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    persona: str = Field(min_length=1, max_length=80)
    want: str = Field(min_length=1, max_length=300)
    benefit: str = Field(min_length=1, max_length=300)
    priority: Literal["must", "should", "could", "wont"]
    estimate: int | None = Field(default=None, ge=1)
    requirement_refs: list[str] = Field(min_length=1, max_length=8)
    persona_refs: list[str] = Field(default_factory=list, max_length=8)
    want_refs: list[str] = Field(default_factory=list, max_length=8)
    benefit_refs: list[str] = Field(default_factory=list, max_length=8)
    depends_on: list[str] = Field(default_factory=list, max_length=8)


class FeatureDraft(BaseModel):
    """A feature grouping stories inside an epic."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    stories: list[StoryDraft] = Field(default_factory=list, max_length=20)


class EpicDraft(BaseModel):
    """An epic with optional features."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    features: list[FeatureDraft] = Field(default_factory=list, max_length=10)
    stories: list[StoryDraft] = Field(default_factory=list, max_length=30)


class DraftOutput(BaseModel):
    """Model output for the draft stage."""

    model_config = ConfigDict(extra="forbid")

    epics: list[EpicDraft] = Field(min_length=1, max_length=10)


@dataclass
class DraftResult:
    """Stories built from a draft, plus findings and call details."""

    stories: list[Story]
    findings: list[Finding] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    request: LLMRequest | None = None


def _finding(code: str, message: str, location: str | None, error: bool = True) -> Finding:
    return Finding(
        code=code,
        message=message,
        severity=Severity.ERROR if error else Severity.WARNING,
        location=location,
    )


def render_requirements(requirements: Sequence[Requirement]) -> str:
    """Render requirements, one per line, marking assumptions."""
    return "\n".join(
        f"{r.id} [{r.category}]{' [ASSUMED]' if r.assumed else ''} {r.text}" for r in requirements
    )


def render_standards(deps: StageDeps, state: RunState) -> str:
    """Render the trusted standards block from standards.yaml and preferences."""
    std = deps.config.standards
    scale = state.preferences.estimate_scale or std.get("estimate_scale", "fibonacci")
    allowed = std.get("estimate_scales", {}).get(scale, [])
    drafting = std.get("drafting", {})
    return "\n".join(
        [
            f"story template: {std.get('story_template', '')}",
            f"priority scheme: {std.get('priority_scheme', 'moscow')}",
            f"estimate scale: {scale}; allowed values: {allowed or 'any whole number of hours'}",
            f"max requirements per story: {drafting.get('max_requirements_per_story', 4)}",
            f"max stories: {drafting.get('max_stories', 30)}",
            "definition of ready: " + "; ".join(std.get("definition_of_ready", [])),
        ]
    )


def render_previous(stories: Sequence[Story]) -> str:
    """Render a previous draft for a revision call."""
    return "\n".join(
        f"{s.id} | {s.epic} | {s.title} | {s.persona} | {s.want} | {s.benefit} | "
        f"{','.join(s.requirement_ids)}"
        for s in stories
    )


def build_user_message(
    deps: StageDeps,
    state: RunState,
    previous: Sequence[Story] = (),
    feedback: Sequence[Finding] = (),
) -> str:
    """Assemble the user message. Text derived from user input is wrapped as untrusted."""
    discovery = state.discovery
    actors = ", ".join(discovery.actors) if discovery else ""
    context = (discovery.business_context if discovery else "") or "none"
    goals = "; ".join(discovery.goals) if discovery else ""
    parts = [
        "STANDARDS:",
        render_standards(deps, state),
        "ACTORS:",
        wrap_untrusted("actors", actors or "none"),
        "CONTEXT AND GOALS:",
        wrap_untrusted("context", f"{context}\nGoals: {goals}"),
        "REQUIREMENTS:",
        wrap_untrusted("requirements", render_requirements(state.requirements)),
    ]
    if feedback:
        lines = "\n".join(f"{f.code} {f.location or ''}: {f.message}" for f in feedback)
        parts += [
            "REVISION. PREVIOUS DRAFT:",
            wrap_untrusted("previous_draft", render_previous(previous)),
            "REVISION. FINDINGS TO FIX:",
            wrap_untrusted("review_findings", lines),
        ]
    return "\n".join(parts)


def _flatten(output: DraftOutput) -> list[tuple[str, str | None, StoryDraft]]:
    flat: list[tuple[str, str | None, StoryDraft]] = []
    for epic in output.epics:
        flat.extend((epic.name.strip(), None, s) for s in epic.stories)
        for feature in epic.features:
            flat.extend((epic.name.strip(), feature.name.strip(), s) for s in feature.stories)
    return flat


def _persona_known(persona: str, state: RunState, checklist: Checklist) -> bool:
    names = [a.casefold() for a in (state.discovery.actors if state.discovery else [])]
    names += [a.name.casefold() for a in checklist.actors]
    p = persona.casefold().strip()
    return any(p == n or p in n or n in p for n in names)


def to_drafts(
    output: DraftOutput, state: RunState, checklist: Checklist
) -> tuple[list[DraftedStory], list[Finding]]:
    """Validate references and numbers, and convert model stories to DraftedStory."""
    findings: list[Finding] = []
    valid_ids = {r.id for r in state.requirements}
    known = known_numbers(state)
    drafts: list[DraftedStory] = []
    for epic, feature, raw in _flatten(output):
        refs = tuple(dict.fromkeys(r for r in raw.requirement_refs if r in valid_ids))
        if len(refs) != len(set(raw.requirement_refs)):
            findings.append(
                _finding(
                    "DRAFT_UNKNOWN_REQ", "story cites an unknown requirement id", raw.title, False
                )
            )
        if not refs:
            findings.append(
                _finding("DRAFT_UNGROUNDED_STORY", "story cites no valid requirement", raw.title)
            )
            continue
        bad = ungrounded_numbers([raw.title, raw.want, raw.benefit], known)
        if bad:
            findings.append(
                _finding(
                    "DRAFT_UNGROUNDED_NUMBER",
                    f"numbers not given by the user: {sorted(bad)}",
                    raw.title,
                )
            )
        if not _persona_known(raw.persona, state, checklist):
            findings.append(
                _finding(
                    "DRAFT_PERSONA_UNKNOWN",
                    f"persona '{raw.persona}' is not a listed actor",
                    raw.title,
                    False,
                )
            )
        drafts.append(
            DraftedStory(
                epic=epic,
                feature=feature,
                title=raw.title,
                persona=raw.persona,
                want=raw.want,
                benefit=raw.benefit,
                priority=Priority(raw.priority),
                estimate=raw.estimate,
                requirement_ids=refs,
                persona_refs=tuple(raw.persona_refs),
                want_refs=tuple(raw.want_refs),
                benefit_refs=tuple(raw.benefit_refs),
                depends_on=tuple(raw.depends_on),
            )
        )
    return drafts, findings


def run_draft(
    deps: StageDeps,
    state: RunState,
    checklist: Checklist,
    previous: Sequence[Story] = (),
    feedback: Sequence[Finding] = (),
) -> DraftResult:
    """Call the model once and return built stories. Revisions pass the previous draft."""
    prompt = load_prompt(deps.prompts_dir, PROMPT_ID)
    request = LLMRequest(
        prompt_id=PROMPT_ID,
        system=prompt.text,
        user=build_user_message(deps, state, previous, feedback),
        model=deps.config.models.generator,
    )
    result = deps.client.complete(request, DraftOutput)
    drafts, findings = to_drafts(result.value, state, checklist)
    limit = int(deps.config.standards.get("drafting", {}).get("max_stories", 30))
    if len(drafts) > limit:
        findings.append(
            _finding("DRAFT_TOO_MANY", f"{len(drafts)} stories exceed the limit of {limit}", None)
        )
    scale = state.preferences.estimate_scale or str(
        deps.config.standards.get("estimate_scale", "fibonacci")
    )
    stories = build_stories(drafts, previous, state, scale, deps.config.standards)
    return DraftResult(stories, findings, result.usage, result.cost_usd, request)
