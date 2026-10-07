"""A publisher-ready view of a finished run: approved stories grouped by epic and feature.

Every publisher reads this view, never the run state directly, so they all agree on
what is published, in what order, with which labels and idempotency keys.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from story_agent.clarify.answers import FINAL_PREFIX
from story_agent.pipeline.postprocess import story_identity
from story_agent.pipeline.review import publishable
from story_agent.publish.config import Labels
from story_agent.schema import (
    Answer,
    Provenance,
    ProvenanceType,
    Question,
    RunState,
    Story,
)

EXCERPT_MAX = 240
DEFAULT_TEMPLATE = "As a {persona}, I want {want}, so that {benefit}."


@dataclass(frozen=True)
class CriterionView:
    """One numbered Given/When/Then line."""

    n: int
    given: str
    when: str
    then: str
    kind: str


@dataclass(frozen=True)
class StoryView:
    """A story as published."""

    id: str
    epic: str
    feature: str | None
    title: str
    statement: str
    persona: str
    want: str
    benefit: str
    criteria: tuple[CriterionView, ...]
    priority: str
    estimate: int | None
    nfrs: tuple[str, ...]
    dependencies: tuple[str, ...]
    assumptions: tuple[str, ...]
    open_questions: tuple[str, ...]
    requirement_ids: tuple[str, ...]
    provenance: tuple[str, ...]
    labels: tuple[str, ...]
    key: str
    confidence: float


@dataclass(frozen=True)
class FeatureView:
    """Stories under one feature. ``name`` is None for stories with no feature."""

    name: str | None
    key: str
    stories: tuple[StoryView, ...]


@dataclass(frozen=True)
class EpicView:
    """An epic with its features."""

    name: str
    prefix: str
    key: str
    features: tuple[FeatureView, ...]


@dataclass(frozen=True)
class RequirementView:
    """A requirement the stories trace to."""

    id: str
    text: str
    category: str
    assumed: bool


@dataclass(frozen=True)
class PublishView:
    """Everything a publisher needs."""

    run_id: str
    workspace: str
    epics: tuple[EpicView, ...]
    requirements: tuple[RequirementView, ...]
    skipped: tuple[str, ...]

    @property
    def stories(self) -> tuple[StoryView, ...]:
        """All stories in published order."""
        return tuple(s for e in self.epics for f in e.features for s in f.stories)


def digest(*parts: str) -> str:
    """Return a short stable hash of the parts."""
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]


def _clip(text: str, limit: int = EXCERPT_MAX) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def provenance_lines(state: RunState, items: list[Provenance]) -> tuple[str, ...]:
    """Say, in words, where a story's facts came from. Only clean text is used."""
    questions: dict[str, Question] = {q.id: q for r in state.rounds for q in r.questions}
    answers: dict[str, Answer] = {a.question_id: a for a in state.answers}
    by_memory = {a.memory_id: a for a in state.answers if a.memory_id}
    lines: list[str] = []
    for p in items:
        if p.type is ProvenanceType.SCENARIO_EXCERPT:
            line = f'Scenario: "{_clip(p.ref)}"'
        elif p.type is ProvenanceType.CLARIFICATION_ANSWER:
            line = _answer_line(p.ref, questions, answers)
        elif p.type is ProvenanceType.MEMORY_CONFIRMED:
            found = by_memory.get(p.ref)
            value = f": {_clip(found.value)}" if found else ""
            line = f"Confirmed from saved memory ({p.ref}){value}"
        elif p.type is ProvenanceType.USER_NOTES:
            line = f"Reviewer note: {_clip(p.ref)}"
        else:
            line = f"Accepted suggestion: {_clip(p.ref)}"
        if line not in lines:
            lines.append(line)
    return tuple(lines)


def _answer_line(ref: str, questions: dict[str, Question], answers: dict[str, Answer]) -> str:
    answer = answers.get(ref)
    value = _clip(answer.value) if answer else ""
    question = questions.get(ref)
    if question is not None:
        return f"Question: {_clip(question.question)} Answer: {value}"
    if ref.startswith(FINAL_PREFIX):
        return f"Decision on {ref.removeprefix(FINAL_PREFIX).replace('_', ' ')}: {value}"
    return f"Answer {ref}: {value}"


def _story_view(
    state: RunState, story: Story, labels: Labels, template: str, scenario_hash: str
) -> StoryView:
    key = digest(state.scenario.workspace, scenario_hash, story_identity(story))
    tags = [labels.import_, *story.requirement_ids]
    if story.open_questions:
        tags.append(labels.needs_clarification)
    tags.append(f"{labels.key_prefix}{key}")
    return StoryView(
        id=story.id,
        epic=story.epic,
        feature=story.feature,
        title=story.title,
        statement=template.format(persona=story.persona, want=story.want, benefit=story.benefit),
        persona=story.persona,
        want=story.want,
        benefit=story.benefit,
        criteria=tuple(
            CriterionView(n, c.given, c.when, c.then, c.kind.value)
            for n, c in enumerate(story.acceptance_criteria, 1)
        ),
        priority=story.priority.value,
        estimate=story.estimate,
        nfrs=tuple(story.nfrs),
        dependencies=tuple(story.dependencies),
        assumptions=tuple(story.assumptions),
        open_questions=tuple(story.open_questions),
        requirement_ids=tuple(story.requirement_ids),
        provenance=provenance_lines(state, story.provenance),
        labels=tuple(dict.fromkeys(tags)),
        key=key,
        confidence=story.confidence,
    )


def build_view(state: RunState, standards: dict[str, object], labels: Labels) -> PublishView:
    """Group the approved stories. Drafts and rejected stories are left out and listed."""
    template = str(standards.get("story_template", DEFAULT_TEMPLATE))
    scenario_hash = digest(state.redacted_text, state.redacted_notes)
    approved = publishable(state)
    skipped = tuple(s.id for s in state.stories if s not in approved)
    views = [_story_view(state, s, labels, template, scenario_hash) for s in approved]
    epics: list[EpicView] = []
    for epic_name in dict.fromkeys(v.epic for v in views):
        in_epic = [v for v in views if v.epic == epic_name]
        features = [
            FeatureView(
                name,
                digest(state.scenario.workspace, scenario_hash, "feature", epic_name, name or ""),
                tuple(v for v in in_epic if v.feature == name),
            )
            for name in dict.fromkeys(v.feature for v in in_epic)
        ]
        prefix = in_epic[0].id.rsplit("-", 1)[0]
        key = digest(state.scenario.workspace, scenario_hash, "epic", epic_name)
        epics.append(EpicView(epic_name, prefix, key, tuple(features)))
    cited = {rid for v in views for rid in v.requirement_ids}
    requirements = tuple(
        RequirementView(r.id, r.text, r.category, r.assumed)
        for r in state.requirements
        if r.id in cited
    )
    return PublishView(state.run_id, state.scenario.workspace, tuple(epics), requirements, skipped)
