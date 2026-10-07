"""Derive requirements from the settled discovery map. No model is involved.

Only items the user stated or confirmed become requirements. Inferred and unknown
items never do. An item settled by "use your judgment" becomes a requirement marked
``assumed`` so stories carry it as an assumption, not as a fact.
"""

from __future__ import annotations

from story_agent.discovery.packs import Checklist
from story_agent.schema import (
    AnswerKind,
    DiscoveryItem,
    Finding,
    ItemStatus,
    Requirement,
    RunState,
    Severity,
)

_USABLE = {ItemStatus.STATED, ItemStatus.CONFIRMED}


def requirement_id(number: int) -> str:
    """Return the id of the ``number``-th requirement (from 1)."""
    return f"REQ-{number:03d}"


def _text(item: DiscoveryItem, name: str, assumed: bool) -> str:
    if item.status is ItemStatus.STATED:
        return item.description
    if assumed or not item.resolution:
        return f"{name}: {item.description}"
    return f"{name}: {item.resolution} (about: {item.description})"


def derive_requirements(
    state: RunState, checklist: Checklist
) -> tuple[list[Requirement], list[Finding]]:
    """Return the requirements for the run and findings for items that could not be used."""
    if state.discovery is None:
        return [], []
    names = {c.id: c.name for c in checklist.categories}
    kinds = {a.question_id: a.kind for a in state.answers}
    requirements: list[Requirement] = []
    findings: list[Finding] = []
    for item in state.discovery.items:
        if item.status not in _USABLE:
            continue
        if not item.provenance:
            findings.append(
                Finding(
                    code="REQ_NO_PROVENANCE",
                    message="settled item has no provenance and was not used",
                    severity=Severity.ERROR,
                    location=item.id,
                )
            )
            continue
        assumed = kinds.get(item.resolved_by or "") is AnswerKind.JUDGMENT
        name = names.get(item.category, item.category)
        requirements.append(
            Requirement(
                id=requirement_id(len(requirements) + 1),
                text=_text(item, name, assumed),
                category=item.category,
                assumed=assumed,
                provenance=list(item.provenance),
            )
        )
    return requirements, findings
