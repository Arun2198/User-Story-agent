"""Stage 9: human review. Approve, edit or reject each story. Every edit is logged.

Edit text is user data. It must pass redaction and the injection scan first (the
``CleanActions`` type marks actions that did). An edit is applied only if the
story still passes grounding afterwards; edited persona, want and benefit carry
``user_notes`` provenance pointing at the recorded edit note.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Literal, NewType

from pydantic import BaseModel, ConfigDict, Field

from story_agent.clarify.answers import CleanText, sanitize_texts
from story_agent.config import AppConfig
from story_agent.guardrails.grounding import GroundingVerifier
from story_agent.hooks.base import HookContext, now_iso
from story_agent.hooks.registry import HookPipeline
from story_agent.pipeline.postprocess import order_and_cap_criteria
from story_agent.schema import (
    AcceptanceCriterion,
    CriterionKind,
    Finding,
    Priority,
    Provenance,
    ProvenanceType,
    RunState,
    Severity,
    Story,
    StoryStatus,
)

_MARKER = re.compile(r"\[QUARANTINED #\d+\]")
TEXT_FIELDS = ("title", "persona", "want", "benefit")
ELEMENT_FIELDS = ("persona", "want", "benefit")


class CriterionEdit(BaseModel):
    """A replacement acceptance criterion."""

    model_config = ConfigDict(extra="forbid")

    given: str
    when: str
    then: str
    kind: CriterionKind = CriterionKind.HAPPY


class StoryEdits(BaseModel):
    """Fields a reviewer may change. Anything left out stays as it is."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    persona: str | None = None
    want: str | None = None
    benefit: str | None = None
    priority: Priority | None = None
    estimate: int | None = Field(default=None, ge=1)
    acceptance_criteria: list[CriterionEdit] | None = None


class ReviewAction(BaseModel):
    """The reviewer's decision on one story."""

    model_config = ConfigDict(extra="forbid")

    story_id: str
    action: Literal["approve", "edit", "reject"]
    edits: StoryEdits | None = None
    reason: str = ""


CleanActions = NewType("CleanActions", list[ReviewAction])


@dataclass
class ReviewReport:
    """What a review pass did."""

    approved: list[str] = field(default_factory=list)
    edited: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    uncovered_requirements: list[str] = field(default_factory=list)


def _warn(code: str, message: str, location: str | None) -> Finding:
    return Finding(code=code, message=message, severity=Severity.WARNING, location=location)


def _err(code: str, message: str, location: str | None) -> Finding:
    return Finding(code=code, message=message, severity=Severity.ERROR, location=location)


def _raw_texts(actions: Sequence[ReviewAction]) -> dict[str, str]:
    raw: dict[str, str] = {}
    for a in actions:
        if a.reason:
            raw[f"{a.story_id}:reason"] = a.reason
        if a.edits is None:
            continue
        for name in TEXT_FIELDS:
            value = getattr(a.edits, name)
            if value is not None:
                raw[f"{a.story_id}:{name}"] = value
        for n, c in enumerate(a.edits.acceptance_criteria or []):
            raw[f"{a.story_id}:ac{n}:given"] = c.given
            raw[f"{a.story_id}:ac{n}:when"] = c.when
            raw[f"{a.story_id}:ac{n}:then"] = c.then
    return raw


def sanitize_actions(
    pipeline: HookPipeline, ctx: HookContext, actions: Sequence[ReviewAction]
) -> tuple[CleanActions, list[Finding]]:
    """Run edit text through redaction and the injection scan. Quarantined fields are dropped."""
    clean = sanitize_texts(pipeline, ctx, _raw_texts(actions))
    findings: list[Finding] = []

    def get(key: str, original: str | None) -> str | None:
        if original is None:
            return None
        text = _MARKER.sub("", clean.get(key, original)).strip()
        if not text:
            findings.append(_warn("EDIT_QUARANTINED", "an edit was empty or quarantined", key))
            return None
        return text

    result: list[ReviewAction] = []
    for a in actions:
        edits = a.edits
        if edits is not None:
            data = edits.model_dump()
            for name in TEXT_FIELDS:
                data[name] = get(f"{a.story_id}:{name}", getattr(edits, name))
            if edits.acceptance_criteria is not None:
                data["acceptance_criteria"] = [
                    {
                        "given": get(f"{a.story_id}:ac{n}:given", c.given) or "",
                        "when": get(f"{a.story_id}:ac{n}:when", c.when) or "",
                        "then": get(f"{a.story_id}:ac{n}:then", c.then) or "",
                        "kind": c.kind,
                    }
                    for n, c in enumerate(edits.acceptance_criteria)
                ]
            edits = StoryEdits.model_validate(data)
        reason = (get(f"{a.story_id}:reason", a.reason) or "") if a.reason else ""
        result.append(a.model_copy(update={"edits": edits, "reason": reason}))
    return CleanActions(result), findings


def _log(state: RunState, entry: dict[str, object]) -> None:
    state.review_log.append({"ts": now_iso(), **entry})


def _ratio(a: str, b: str) -> float:
    return round(SequenceMatcher(None, a, b, autojunk=False).ratio(), 3)


def _open_findings(state: RunState, story_id: str) -> list[str]:
    return sorted(
        {
            f.code
            for f in state.findings
            if f.severity is Severity.ERROR and f.location and story_id in f.location.split(",")
        }
    )


def _apply_edits(
    story: Story, edits: StoryEdits, state: RunState, limit: int
) -> tuple[Story, list[tuple[str, str, str]]]:
    changes: list[tuple[str, str, str]] = []
    update: dict[str, object] = {}
    provenance = list(story.provenance)
    for name in TEXT_FIELDS:
        new = getattr(edits, name)
        old = getattr(story, name)
        if new is None or new == old:
            continue
        update[name] = new
        changes.append((name, old, new))
        if name in ELEMENT_FIELDS:
            note = f"{name}: {new}"
            state.review_notes.append(note)
            provenance = [p for p in provenance if p.element != name]
            provenance.append(Provenance(type=ProvenanceType.USER_NOTES, ref=note, element=name))
    if edits.priority is not None and edits.priority is not story.priority:
        update["priority"] = edits.priority
        changes.append(("priority", story.priority.value, edits.priority.value))
    if edits.estimate is not None and edits.estimate != story.estimate:
        update["estimate"] = edits.estimate
        changes.append(("estimate", str(story.estimate), str(edits.estimate)))
    if edits.acceptance_criteria is not None:
        before = _criteria_text(story.acceptance_criteria)
        criteria = [
            AcceptanceCriterion(id="", given=c.given, when=c.when, then=c.then, kind=c.kind)
            for c in edits.acceptance_criteria
        ]
        for c in criteria:
            state.review_notes.append(f"criterion: Given {c.given} When {c.when} Then {c.then}")
        built = order_and_cap_criteria(criteria, story.id, limit)
        update["acceptance_criteria"] = built
        changes.append(("acceptance_criteria", before, _criteria_text(built)))
    update["provenance"] = provenance
    return story.model_copy(update=update), changes


def _criteria_text(criteria: Sequence[AcceptanceCriterion]) -> str:
    return " | ".join(f"Given {c.given} When {c.when} Then {c.then}" for c in criteria)


def apply_review(
    state: RunState, config: AppConfig, actions: CleanActions, reviewer: str = "user"
) -> ReviewReport:
    """Apply decisions. Unknown stories and invalid edits are reported and skipped."""
    report = ReviewReport()
    limit = state.preferences.max_criteria_per_story or int(
        config.standards.get("max_criteria_per_story", 8)
    )
    index = {s.id: i for i, s in enumerate(state.stories)}
    for action in actions:
        position = index.get(action.story_id)
        if position is None:
            report.findings.append(_warn("REVIEW_UNKNOWN_STORY", "no such story", action.story_id))
            continue
        story = state.stories[position]
        if action.action == "reject":
            state.stories[position] = story.model_copy(update={"status": StoryStatus.REJECTED})
            _log(
                state,
                {
                    "story_id": story.id,
                    "action": "reject",
                    "reviewer": reviewer,
                    "reason": action.reason,
                },
            )
            continue
        if action.action == "approve":
            state.stories[position] = story.model_copy(update={"status": StoryStatus.APPROVED})
            _log(
                state,
                {
                    "story_id": story.id,
                    "action": "approve",
                    "reviewer": reviewer,
                    "open_findings": _open_findings(state, story.id),
                },
            )
            continue
        if action.edits is None:
            report.findings.append(_warn("REVIEW_EMPTY_EDIT", "edit without changes", story.id))
            continue
        notes_before = len(state.review_notes)
        edited, changes = _apply_edits(story, action.edits, state, limit)
        verifier = GroundingVerifier(state, config.guardrails.grounding)
        problems = verifier.check_story(edited)
        invalid = [f for f in problems if f.severity is Severity.ERROR]
        empty_text = not (edited.persona.strip() and edited.want.strip() and edited.benefit.strip())
        empty_clause = any(
            not (c.given.strip() and c.when.strip() and c.then.strip())
            for c in edited.acceptance_criteria
        )
        if invalid or empty_text or empty_clause:
            del state.review_notes[notes_before:]
            report.findings.append(
                _err("REVIEW_EDIT_REJECTED", "edit failed validation and was not applied", story.id)
            )
            report.findings.extend(invalid)
            continue
        if not changes:
            report.findings.append(_warn("REVIEW_NO_CHANGE", "edit changed nothing", story.id))
            continue
        state.stories[position] = edited.model_copy(update={"status": StoryStatus.EDITED})
        for name, old, new in changes:
            _log(
                state,
                {
                    "story_id": story.id,
                    "action": "edit",
                    "field": name,
                    "before": old,
                    "after": new,
                    "edit_distance": round(1 - _ratio(old, new), 3),
                    "reviewer": reviewer,
                },
            )
    _summarise(state, report)
    return report


def _summarise(state: RunState, report: ReviewReport) -> None:
    for story in state.stories:
        bucket = {
            StoryStatus.APPROVED: report.approved,
            StoryStatus.EDITED: report.edited,
            StoryStatus.REJECTED: report.rejected,
            StoryStatus.DRAFT: report.pending,
        }[story.status]
        bucket.append(story.id)
    kept = {
        r for s in state.stories if s.status is not StoryStatus.REJECTED for r in s.requirement_ids
    }
    report.uncovered_requirements = [r.id for r in state.requirements if r.id not in kept]


def all_decided(state: RunState) -> bool:
    """Return True when no story is still waiting for a decision."""
    return bool(state.stories) and all(s.status is not StoryStatus.DRAFT for s in state.stories)


def publishable(state: RunState) -> list[Story]:
    """Return approved and edited stories, in order."""
    return [s for s in state.stories if s.status in {StoryStatus.APPROVED, StoryStatus.EDITED}]


__all__ = [
    "CleanActions",
    "CleanText",
    "CriterionEdit",
    "ReviewAction",
    "ReviewReport",
    "StoryEdits",
    "all_decided",
    "apply_review",
    "publishable",
    "sanitize_actions",
]
