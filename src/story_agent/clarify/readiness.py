"""The readiness summary and the go-ahead gate.

Drafting may start only after ``grant_go_ahead`` succeeds, and that needs an
explicit confirmation from the user (interactive) or from an --answers file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from story_agent.clarify.select import is_open
from story_agent.discovery.packs import Checklist
from story_agent.schema import AnswerKind, DiscoveryItem, ItemStatus, RunState


class GateError(RuntimeError):
    """Raised when drafting is attempted before the gate is open."""


@dataclass
class Readiness:
    """What is settled, what is not, and whether drafting may start."""

    ready: bool
    unresolved_must_have: list[str] = field(default_factory=list)
    open_optional: list[str] = field(default_factory=list)
    confirmed: list[str] = field(default_factory=list)
    assumed: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    not_applicable: list[str] = field(default_factory=list)
    relied_on: list[str] = field(default_factory=list)
    rounds_used: int = 0
    no_questions_needed: bool = False

    def render(self) -> str:
        """Return the readiness summary as text for the user."""
        lines = ["Readiness summary", "================="]
        if self.no_questions_needed:
            lines.append("No clarification questions are needed. I relied on the following.")
        lines.append(f"Rounds used: {self.rounds_used}")
        sections = [
            ("Stated in your scenario", self.relied_on),
            ("Confirmed by your answers", self.confirmed),
            ("Assumed by your judgment (user approved)", self.assumed),
            ("Deferred by you", self.deferred),
            ("Marked not applicable or out of scope", self.not_applicable),
            ("Still unknown, optional (will appear as open questions)", self.open_optional),
            ("MUST-HAVE, still unresolved", self.unresolved_must_have),
        ]
        for title, entries in sections:
            if entries:
                lines.append(f"\n{title}:")
                lines.extend(f"  - {entry}" for entry in entries)
        verdict = (
            "Ready to draft." if self.ready else "Not ready: resolve the must-have items above."
        )
        lines.append(f"\n{verdict}")
        return "\n".join(lines)


def _label(item: DiscoveryItem, category_name: str) -> str:
    if item.resolution:
        return f"{category_name}: {item.resolution}"
    return f"{category_name}: {item.description}"


def assess(state: RunState, checklist: Checklist) -> Readiness:
    """Compute readiness from the discovery map and answers."""
    discovery = state.discovery
    if discovery is None:
        return Readiness(ready=False, unresolved_must_have=["discovery has not run"])
    names = {c.id: c.name for c in checklist.categories}
    kinds = {a.question_id: a.kind for a in state.answers}
    report = Readiness(ready=True, rounds_used=len(state.rounds))
    unresolved: dict[str, None] = {}
    optional: dict[str, None] = {}
    for item in discovery.items:
        name = names.get(item.category, item.category)
        kind = kinds.get(item.resolved_by or "")
        if is_open(item):
            target = unresolved if item.category in checklist.must_have_ids else optional
            target[f"{name}: {item.description}"] = None
        elif item.status is ItemStatus.STATED:
            report.relied_on.append(_label(item, name))
        elif item.status is ItemStatus.REJECTED:
            report.not_applicable.append(f"{name}")
        elif kind is AnswerKind.DEFERRED:
            report.deferred.append(name)
        elif kind is AnswerKind.JUDGMENT:
            report.assumed.append(name)
        elif item.status is ItemStatus.CONFIRMED:
            report.confirmed.append(_label(item, name))
    report.unresolved_must_have = list(unresolved)
    report.open_optional = list(optional)
    report.ready = not report.unresolved_must_have
    report.no_questions_needed = not state.rounds and not optional and report.ready
    for attr in ("relied_on", "confirmed", "assumed", "deferred", "not_applicable"):
        setattr(report, attr, list(dict.fromkeys(getattr(report, attr))))
    return report


def must_have_open_categories(state: RunState, checklist: Checklist) -> list[str]:
    """Return ids of must-have categories that still have open items."""
    if state.discovery is None:
        return []
    ids = {i.category for i in state.discovery.items if is_open(i)}
    return [c.id for c in checklist.categories if c.id in ids and c.id in checklist.must_have_ids]


def grant_go_ahead(
    state: RunState,
    checklist: Checklist,
    confirmed_by: Literal["user", "answers_file"],
) -> None:
    """Open the gate. Raises GateError unless every must-have category is settled."""
    if not assess(state, checklist).ready:
        raise GateError("must-have categories are still unresolved")
    state.go_ahead = True
    state.go_ahead_by = confirmed_by


def require_go_ahead(state: RunState) -> None:
    """Raise GateError unless the user has opened the gate. Drafting calls this first."""
    if not state.go_ahead or state.go_ahead_by is None:
        raise GateError("drafting needs the user's go-ahead after the readiness summary")
