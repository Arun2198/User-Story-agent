"""Deterministic grounding verification.

Every requirement and every story element must trace to something the user said
or confirmed. Scenario and notes excerpts are checked against the normalised
text (exact match, then a strict fuzzy fallback). Clarification, memory and
discovery references must exist in run state. Nothing here calls a model.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from story_agent.config import GroundingConfig
from story_agent.schema import (
    AnswerKind,
    Finding,
    ItemStatus,
    Provenance,
    ProvenanceType,
    Requirement,
    RunState,
    Severity,
    Story,
)

_QUOTES = {0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"', 0x2013: "-", 0x2014: "-"}
STORY_ELEMENTS = ("persona", "want", "benefit")


def normalize(text: str) -> str:
    """NFKC, straight quotes and dashes, casefold, single spaces."""
    folded = unicodedata.normalize("NFKC", text).translate(_QUOTES).casefold()
    return re.sub(r"\s+", " ", folded).strip()


def _error(code: str, message: str, location: str) -> Finding:
    return Finding(code=code, message=message, severity=Severity.ERROR, location=location)


@dataclass
class GroundingReport:
    """Findings from verifying a story or requirement set."""

    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing failed."""
        return not any(f.severity is Severity.ERROR for f in self.findings)

    @property
    def ungrounded_ids(self) -> list[str]:
        """Ids of the items that failed, in first-seen order."""
        seen: dict[str, None] = {}
        for finding in self.findings:
            if finding.location:
                seen.setdefault(finding.location.split(":")[0], None)
        return list(seen)


class GroundingVerifier:
    """Checks provenance against a run's state."""

    def __init__(self, state: RunState, config: GroundingConfig) -> None:
        """Bind the verifier to the redacted scenario, notes and run state."""
        self._state = state
        self._config = config
        self._scenario = normalize(state.redacted_text or state.scenario.text)
        notes = [state.redacted_notes or state.scenario.notes]
        notes.extend(r.free_text_reply for r in state.rounds if r.free_text_reply)
        notes.extend(state.review_notes)
        self._notes = normalize("\n".join(notes))
        self._answers = {a.question_id: a for a in state.answers}
        self._memory_confirmed = {
            a.memory_id
            for a in state.answers
            if a.kind is AnswerKind.MEMORY_CONFIRMED and a.memory_id
        }
        self._confirmed_items = {
            item.id
            for item in (state.discovery.items if state.discovery else [])
            if item.status is ItemStatus.CONFIRMED
        }

    def excerpt_found(self, excerpt: str, source: str) -> bool:
        """Return True when ``excerpt`` appears in ``source``, exactly or nearly."""
        needle = normalize(excerpt)
        if len(needle) < self._config.min_excerpt_chars or not source:
            return False
        if needle in source:
            return True
        words = source.split()
        n = len(needle.split())
        if n < self._config.fuzzy_min_words:
            return False
        ratio = self._config.fuzzy_min_ratio
        for size in (n - 1, n, n + 1):
            for i in range(max(len(words) - size + 1, 0)):
                window = " ".join(words[i : i + size])
                matcher = SequenceMatcher(None, needle, window, autojunk=False)
                if matcher.real_quick_ratio() >= ratio and matcher.ratio() >= ratio:
                    return True
        return False

    def locate(self, excerpt: str) -> ProvenanceType | None:
        """Return which source contains ``excerpt``: the scenario, or the notes and replies."""
        if self.excerpt_found(excerpt, self._scenario):
            return ProvenanceType.SCENARIO_EXCERPT
        if self.excerpt_found(excerpt, self._notes):
            return ProvenanceType.USER_NOTES
        return None

    def check_provenance(self, prov: Provenance, where: str) -> Finding | None:
        """Return a finding when ``prov`` cannot be verified."""
        kind = prov.type
        if kind is ProvenanceType.SCENARIO_EXCERPT:
            if not self.excerpt_found(prov.ref, self._scenario):
                return _error("GROUND_EXCERPT_NOT_FOUND", "scenario excerpt not found", where)
        elif kind is ProvenanceType.USER_NOTES:
            if not self.excerpt_found(prov.ref, self._notes):
                return _error("GROUND_EXCERPT_NOT_FOUND", "notes excerpt not found", where)
        elif kind is ProvenanceType.CLARIFICATION_ANSWER:
            answer = self._answers.get(prov.ref)
            if answer is None or answer.kind is AnswerKind.DEFERRED:
                return _error("GROUND_REF_MISSING", f"no usable answer {prov.ref}", where)
        elif kind is ProvenanceType.MEMORY_CONFIRMED:
            if prov.ref not in self._memory_confirmed:
                return _error("GROUND_REF_MISSING", f"memory {prov.ref} not confirmed", where)
        elif prov.ref not in self._confirmed_items:
            return _error("GROUND_REF_MISSING", f"item {prov.ref} not confirmed", where)
        return None

    def check_requirement(self, req: Requirement) -> list[Finding]:
        """Verify every provenance entry of a requirement."""
        if not req.provenance:
            return [_error("GROUND_NO_PROVENANCE", "no provenance", req.id)]
        found = (self.check_provenance(p, f"{req.id}:provenance") for p in req.provenance)
        return [f for f in found if f]

    def check_story(self, story: Story) -> list[Finding]:
        """Verify provenance, element coverage, assumptions and requirement links."""
        findings: list[Finding] = []
        if not story.provenance:
            return [_error("GROUND_NO_PROVENANCE", "no provenance", story.id)]
        checked = (self.check_provenance(p, f"{story.id}:provenance") for p in story.provenance)
        findings.extend(f for f in checked if f)
        covered = {p.element for p in story.provenance}
        findings.extend(
            _error("GROUND_ELEMENT_UNCOVERED", f"{e} has no provenance", f"{story.id}:{e}")
            for e in STORY_ELEMENTS
            if e not in covered
        )
        known_requirements = {r.id for r in self._state.requirements}
        findings.extend(
            _error("GROUND_REQ_MISSING", f"unknown requirement {r}", story.id)
            for r in story.requirement_ids
            if r not in known_requirements
        )
        findings.extend(
            _error("GROUND_REF_MISSING", f"unknown clarification {r}", story.id)
            for r in story.clarification_refs
            if r not in self._answers
        )
        if story.assumptions and not self._has_judgment_ref(story):
            findings.append(
                _error(
                    "GROUND_ASSUMPTION_UNLINKED", "assumption without a judgment answer", story.id
                )
            )
        return findings

    def verify(self, stories: list[Story], requirements: list[Requirement]) -> GroundingReport:
        """Verify all requirements and stories."""
        report = GroundingReport()
        for req in requirements:
            report.findings.extend(self.check_requirement(req))
        for story in stories:
            report.findings.extend(self.check_story(story))
        return report

    def _has_judgment_ref(self, story: Story) -> bool:
        return any(
            ref in self._answers and self._answers[ref].kind is AnswerKind.JUDGMENT
            for ref in story.clarification_refs
        )
