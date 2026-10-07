"""Deterministic post-processing of drafted stories.

The model proposes content. This module decides order, ids, estimates, assumptions,
open questions and confidence, so the same stories always get the same ids.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from story_agent.schema import (
    AcceptanceCriterion,
    CriterionKind,
    DiscoveryItem,
    Finding,
    ItemStatus,
    Priority,
    Provenance,
    ProvenanceType,
    Requirement,
    RunState,
    Severity,
    Story,
)

_PRIORITY_RANK = {Priority.MUST: 0, Priority.SHOULD: 1, Priority.COULD: 2, Priority.WONT: 3}
KIND_RANK = {
    CriterionKind.HAPPY: 0,
    CriterionKind.EDGE: 1,
    CriterionKind.ERROR: 2,
    CriterionKind.COMPLIANCE: 3,
    CriterionKind.NFR: 4,
}
ID_RE = re.compile(r"^[A-Z]{2,6}-\d{4}$")
_STOP_WORDS = frozenset({"the", "a", "an", "and", "of", "to", "for", "in", "on", "with", "my"})


@dataclass(frozen=True)
class DraftedStory:
    """A story as drafted, before ids and derived fields. Epic and feature are names."""

    epic: str
    feature: str | None
    title: str
    persona: str
    want: str
    benefit: str
    priority: Priority
    estimate: int | None
    requirement_ids: tuple[str, ...]
    persona_refs: tuple[str, ...]
    want_refs: tuple[str, ...]
    benefit_refs: tuple[str, ...]
    depends_on: tuple[str, ...]


def _norm(text: str) -> str:
    return " ".join(text.casefold().split())


def req_number(req_id: str) -> int:
    """Return the number part of a requirement id, or a large number when malformed."""
    match = re.search(r"(\d+)$", req_id)
    return int(match.group(1)) if match else 10**6


# ---- epics, prefixes, ids -------------------------------------------------


def epic_prefix(name: str, configured: dict[str, str], taken: set[str]) -> str:
    """Return a unique 2 to 6 letter prefix for an epic."""
    if name in configured:
        return configured[name].upper()
    words = [w for w in re.findall(r"[A-Za-z]+", name) if w.casefold() not in _STOP_WORDS]
    base = "".join(w[0] for w in words).upper()[:6]
    if len(base) < 2:
        letters = "".join(words).upper() or "EP"
        base = (letters + "XX")[:3]
    candidate = base
    suffix = ord("A")
    while candidate in taken:
        candidate = f"{base[:5]}{chr(suffix)}"
        suffix += 1
    return candidate


def _epic_order(drafts: Sequence[DraftedStory]) -> list[str]:
    first: dict[str, int] = {}
    for d in drafts:
        low = min((req_number(r) for r in d.requirement_ids), default=10**6)
        first[d.epic] = min(first.get(d.epic, 10**6), low)
    return sorted(first, key=lambda name: (first[name], name.casefold()))


def _sort_key(d: DraftedStory) -> tuple[int, str, int, int, str]:
    low = min((req_number(r) for r in d.requirement_ids), default=10**6)
    return (
        0 if d.feature is None else 1,
        (d.feature or "").casefold(),
        _PRIORITY_RANK[d.priority],
        low,
        d.title.casefold(),
    )


def identity_key(epic: str, requirement_ids: Iterable[str], title: str) -> str:
    """Return the key that says two drafts are the same story across revisions."""
    return f"{_norm(epic)}|{','.join(sorted(requirement_ids))}|{_norm(title)}"


def story_identity(story: Story) -> str:
    """Return the identity key of a built story."""
    return identity_key(story.epic, story.requirement_ids, story.title)


def assign_ids(
    drafts: Sequence[DraftedStory],
    previous: Sequence[Story],
    configured_prefixes: dict[str, str],
) -> list[tuple[str, DraftedStory]]:
    """Return ``(story_id, draft)`` in canonical order.

    A draft that matches a previous story (same epic, requirements and title) keeps
    that story's id. New drafts get the next free number for their epic prefix.
    """
    prefixes: dict[str, str] = {}
    used: dict[str, set[int]] = {}
    previous_by_key = {story_identity(s): s.id for s in previous}
    for story in previous:
        old_prefix, old_number = story.id.split("-")
        prefixes.setdefault(story.epic, old_prefix)
        used.setdefault(old_prefix, set()).add(int(old_number))
    taken = set(prefixes.values())
    for epic in _epic_order(drafts):
        if epic not in prefixes:
            prefixes[epic] = epic_prefix(epic, configured_prefixes, taken)
            taken.add(prefixes[epic])
    result: list[tuple[str, DraftedStory]] = []
    claimed: set[str] = set()
    for epic in _epic_order(drafts):
        for draft in sorted((d for d in drafts if d.epic == epic), key=_sort_key):
            key = identity_key(draft.epic, draft.requirement_ids, draft.title)
            existing = previous_by_key.get(key)
            if existing is not None and existing not in claimed:
                claimed.add(existing)
                result.append((existing, draft))
                continue
            prefix = prefixes[epic]
            number = 1
            while number in used.setdefault(prefix, set()):
                number += 1
            used[prefix].add(number)
            new_id = f"{prefix}-{number:04d}"
            claimed.add(new_id)
            result.append((new_id, draft))
    return result


# ---- derived fields ------------------------------------------------------


def snap_estimate(value: int | None, scale: str, standards: dict[str, Any]) -> int | None:
    """Snap an estimate to the scale's allowed values (or clamp for hours)."""
    if value is None or value < 1:
        return None
    allowed: list[int] = standards.get("estimate_scales", {}).get(scale, [])
    if not allowed:
        return min(value, int(standards.get("max_hours", 80)))
    return min(allowed, key=lambda a: (abs(a - value), a))


def _provenance_for(
    refs: Iterable[str], by_id: dict[str, Requirement], element: str
) -> list[Provenance]:
    seen: set[tuple[ProvenanceType, str]] = set()
    out: list[Provenance] = []
    for ref in refs:
        req = by_id.get(ref)
        if req is None:
            continue
        for prov in req.provenance:
            key = (prov.type, prov.ref)
            if key not in seen:
                seen.add(key)
                out.append(prov.model_copy(update={"element": element}))
    return out


def open_items_for(categories: Iterable[str], state: RunState) -> list[DiscoveryItem]:
    """Return unresolved or deferred items in the given categories."""
    wanted = set(categories)
    if state.discovery is None:
        return []
    return [
        i
        for i in state.discovery.items
        if i.category in wanted
        and (
            (i.status in {ItemStatus.UNKNOWN, ItemStatus.INFERRED})
            or (
                i.resolved_by is not None
                and i.status is not ItemStatus.CONFIRMED
                and i.status is not ItemStatus.REJECTED
            )
        )
    ]


def confidence(story: Story, warning_count: int) -> float:
    """Return a 0..1 confidence from evidence: assumptions, open questions, warnings."""
    score = 1.0 - 0.15 * len(story.assumptions) - 0.05 * len(story.open_questions)
    score -= 0.05 * warning_count
    return round(max(0.1, min(1.0, score)), 2)


def build_story(  # noqa: PLR0913, PLR0917  (explicit inputs keep this pure and testable)
    story_id: str,
    draft: DraftedStory,
    state: RunState,
    id_by_title: dict[str, str],
    scale: str,
    standards: dict[str, Any],
) -> Story:
    """Turn a draft into a Story with provenance, assumptions, NFRs and open questions."""
    by_id = {r.id: r for r in state.requirements}
    refs = [r for r in draft.requirement_ids if r in by_id]
    reqs = [by_id[r] for r in refs]
    nfr_cats = set(standards.get("nfr_categories", []))
    provenance: list[Provenance] = []
    for element, element_refs in (
        ("persona", draft.persona_refs),
        ("want", draft.want_refs),
        ("benefit", draft.benefit_refs),
    ):
        valid = [r for r in element_refs if r in by_id] or refs
        provenance.extend(_provenance_for(valid, by_id, element))
    clarification_refs = sorted(
        {p.ref for r in reqs for p in r.provenance if p.type is ProvenanceType.CLARIFICATION_ANSWER}
    )
    assumptions = [r.text for r in reqs if r.assumed]
    categories = {r.category for r in reqs}
    open_questions = [f"{i.category}: {i.description}" for i in open_items_for(categories, state)]
    story = Story(
        id=story_id,
        epic=draft.epic,
        feature=draft.feature,
        title=draft.title.strip(),
        persona=draft.persona.strip(),
        want=draft.want.strip(),
        benefit=draft.benefit.strip(),
        priority=draft.priority,
        estimate=snap_estimate(draft.estimate, scale, standards),
        nfrs=[r.text for r in reqs if r.category in nfr_cats],
        dependencies=sorted(
            {id_by_title[t] for t in draft.depends_on if t in id_by_title} - {story_id}
        ),
        assumptions=assumptions,
        open_questions=sorted(dict.fromkeys(open_questions)),
        requirement_ids=sorted(refs, key=req_number),
        clarification_refs=[
            c for c in clarification_refs if c in {a.question_id for a in state.answers}
        ],
        provenance=provenance,
    )
    return story.model_copy(update={"confidence": confidence(story, 0)})


def build_stories(
    drafts: Sequence[DraftedStory],
    previous: Sequence[Story],
    state: RunState,
    scale: str,
    standards: dict[str, Any],
) -> list[Story]:
    """Assign ids, resolve dependencies by title, and build every story."""
    assigned = assign_ids(drafts, previous, standards.get("epic_prefixes", {}))
    id_by_title = {_norm(d.title): sid for sid, d in assigned}
    stories: list[Story] = []
    for story_id, draft in assigned:
        resolved = DraftedStory(
            **{**draft.__dict__, "depends_on": tuple(_norm(t) for t in draft.depends_on)}
        )
        stories.append(build_story(story_id, resolved, state, id_by_title, scale, standards))
    return stories


# ---- criteria -------------------------------------------------------------

_LEAD = re.compile(r"^\s*(given|when|then|and)\b[\s,:-]*", re.I)


def clean_clause(text: str) -> str:
    """Strip a leading Given, When, Then or And and surrounding space."""
    return _LEAD.sub("", " ".join(text.split())).strip().rstrip(".")


def order_and_cap_criteria(
    criteria: Sequence[AcceptanceCriterion], story_id: str, limit: int
) -> list[AcceptanceCriterion]:
    """Dedupe, order by kind, keep at least one of each kind when capping, and assign ids."""
    seen: set[str] = set()
    unique: list[AcceptanceCriterion] = []
    for c in sorted(
        criteria, key=lambda c: (KIND_RANK[c.kind], _norm(c.given), _norm(c.when), _norm(c.then))
    ):
        key = _norm(f"{c.given}|{c.when}|{c.then}")
        if key not in seen:
            seen.add(key)
            unique.append(c)
    if len(unique) > limit:
        chosen: list[AcceptanceCriterion] = []
        for kind in KIND_RANK:
            first = next((c for c in unique if c.kind is kind), None)
            if first is not None and len(chosen) < limit:
                chosen.append(first)
        rest = [c for c in unique if c not in chosen]
        chosen.extend(rest[: limit - len(chosen)])
        unique = sorted(chosen, key=lambda c: KIND_RANK[c.kind])
    return [
        c.model_copy(update={"id": f"{story_id}-AC{n:02d}"}) for n, c in enumerate(unique, start=1)
    ]


# ---- checks ---------------------------------------------------------------


def similarity(a: str, b: str) -> float:
    """Return a 0..1 similarity of two texts, ignoring case and spacing."""
    return SequenceMatcher(None, _norm(a), _norm(b), autojunk=False).ratio()


def find_duplicates(stories: Sequence[Story], threshold: float) -> list[Finding]:
    """Return an error finding for each pair of near-duplicate stories."""
    findings: list[Finding] = []
    ordered = sorted(stories, key=lambda s: s.id)
    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            same_reqs = set(a.requirement_ids) == set(b.requirement_ids)
            want = similarity(a.want, b.want)
            title = similarity(a.title, b.title)
            if want >= threshold or (same_reqs and title >= threshold - 0.1):
                findings.append(
                    Finding(
                        code="DUPLICATE_STORY",
                        message=f"{a.id} and {b.id} look like duplicates",
                        severity=Severity.ERROR,
                        location=f"{a.id},{b.id}",
                    )
                )
    return findings


def find_uncovered(stories: Sequence[Story], requirements: Sequence[Requirement]) -> list[Finding]:
    """Return an error finding for each requirement that no story covers."""
    covered = {r for s in stories for r in s.requirement_ids}
    return [
        Finding(
            code="COVERAGE_GAP",
            message=f"{r.id} ({r.category}) is not covered by any story",
            severity=Severity.ERROR,
            location=r.id,
        )
        for r in requirements
        if r.id not in covered
    ]


def check_id_format_and_stability(
    stories: Sequence[Story], previous: Sequence[Story]
) -> list[Finding]:
    """Check ids are well formed and unique, and unchanged stories kept their ids."""
    findings: list[Finding] = []
    ids = [s.id for s in stories]
    findings.extend(
        Finding(code="ID_FORMAT", message=f"bad story id {i}", severity=Severity.ERROR, location=i)
        for i in ids
        if not ID_RE.match(i)
    )
    if len(ids) != len(set(ids)):
        findings.append(
            Finding(
                code="ID_DUPLICATE", message="story ids are not unique", severity=Severity.ERROR
            )
        )
    old = {story_identity(s): s.id for s in previous}
    findings.extend(
        Finding(
            code="ID_UNSTABLE",
            message=f"story {s.id} changed id from {old[story_identity(s)]}",
            severity=Severity.ERROR,
            location=s.id,
        )
        for s in stories
        if story_identity(s) in old and old[story_identity(s)] != s.id
    )
    return findings
