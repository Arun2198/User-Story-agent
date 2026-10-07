"""Memory write proposals and the approval step.

Proposals are built from what the user confirmed in a run, with no model call.
Nothing is saved without an explicit decision. Content is checked by the write
guard before it is proposed and again before it is saved.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from story_agent.config import MemoryConfig
from story_agent.ids import memory_id
from story_agent.memory.guard import check_content
from story_agent.memory.store import MemoryStore, UnsafeContentError
from story_agent.schema import (
    AnswerKind,
    Finding,
    MemoryEntry,
    MemoryType,
    RunState,
    utcnow,
)

NFR_TYPES = {MemoryType.NFR_DEFAULT}


class ProposalAction(StrEnum):
    """What approving a proposal does."""

    CREATE = "create"
    UPDATE = "update"
    REFRESH = "refresh"


class Decision(BaseModel):
    """The user's decision on one proposal."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "edit", "reject"]
    content: str | None = None


@dataclass(frozen=True)
class Proposal:
    """One entry the agent suggests saving."""

    entry: MemoryEntry
    action: ProposalAction
    reason: str
    conflict_with: MemoryEntry | None = None

    @property
    def id(self) -> str:
        """The id of the entry this proposal would write."""
        return self.entry.id


@dataclass
class ProposalSet:
    """Proposals plus candidates the guard refused."""

    proposals: list[Proposal] = field(default_factory=list)
    refused: list[Finding] = field(default_factory=list)


@dataclass
class ApplyReport:
    """What the approval step did."""

    saved: list[str] = field(default_factory=list)
    refreshed: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    refused: list[Finding] = field(default_factory=list)


def _norm(text: str) -> str:
    return " ".join(text.casefold().split()).rstrip(".!")


def _make_entry(  # noqa: PLR0913, PLR0917  (explicit fields read better than a bag)
    state: RunState,
    config: MemoryConfig,
    entry_type: MemoryType,
    domain: str,
    subdomain: str | None,
    key_tag: str,
    content: str,
    now: datetime,
) -> MemoryEntry:
    return MemoryEntry(
        id=memory_id(entry_type.value, domain, subdomain, key_tag),
        workspace=state.scenario.workspace,
        domain=domain,
        subdomain=subdomain,
        tags=[key_tag],
        type=entry_type,
        content=content.strip(),
        source_run_id=state.run_id,
        created_at=now,
        last_confirmed_at=now,
        ttl_days=config.ttl_days.get(entry_type.value, 180),
    )


def _classify(
    store: MemoryStore, entry: MemoryEntry
) -> tuple[ProposalAction, str, MemoryEntry | None]:
    existing = store.get(entry.id)
    if existing is None:
        return ProposalAction.CREATE, "new entry", None
    if _norm(existing.content) == _norm(entry.content):
        return ProposalAction.REFRESH, "same answer, refresh the last-confirmed date", existing
    return ProposalAction.UPDATE, "changes a saved answer", existing


def _candidates(state: RunState, config: MemoryConfig, now: datetime) -> list[MemoryEntry]:
    discovery = state.discovery
    if discovery is None:
        return []
    questions = {q.id: q for r in state.rounds for q in r.questions}
    out: list[MemoryEntry] = []
    for answer in state.answers:
        question = questions.get(answer.question_id)
        if question is None or answer.kind not in {AnswerKind.OPTION, AnswerKind.OTHER}:
            continue
        if state.conflict_resolutions.get(question.id) == "exception":
            continue
        is_nfr = question.category in config.nfr_categories
        entry_type = MemoryType.NFR_DEFAULT if is_nfr else MemoryType.CONFIRMED_ANSWER
        subdomain = None if is_nfr else discovery.subdomain
        tag = f"category:{question.category}"
        out.append(
            _make_entry(
                state, config, entry_type, discovery.domain, subdomain, tag, answer.value, now
            )
        )
    prefs = state.preferences.model_dump(exclude_none=True)
    for name, value in sorted(prefs.items()):
        out.append(
            _make_entry(
                state,
                config,
                MemoryType.PREFERENCE,
                "generic",
                None,
                f"pref:{name}",
                f"{name}={value}",
                now,
            )
        )
    return out


def _refresh_candidates(state: RunState, store: MemoryStore) -> list[Proposal]:
    proposals: list[Proposal] = []
    for answer in state.answers:
        if answer.kind is AnswerKind.MEMORY_CONFIRMED and answer.memory_id:
            existing = store.get(answer.memory_id)
            if existing is not None:
                proposals.append(
                    Proposal(
                        existing, ProposalAction.REFRESH, "you re-confirmed this answer", existing
                    )
                )
    return proposals


def build_proposals(
    state: RunState, store: MemoryStore, config: MemoryConfig, now: datetime | None = None
) -> ProposalSet:
    """Build proposals from the run. Content failing the write guard is refused, not proposed."""
    current = now or utcnow()
    result = ProposalSet()
    seen: set[str] = set()
    for entry in _candidates(state, config, current):
        findings = check_content(
            entry.content,
            config.write.max_content_chars,
            state.scenario.text,
            config.write.max_scenario_overlap_chars,
        )
        if findings:
            result.refused.extend(
                f.model_copy(update={"location": entry.tags[0]}) for f in findings
            )
            continue
        action, reason, existing = _classify(store, entry)
        final = entry
        if existing is not None:
            final = entry.model_copy(
                update={"created_at": existing.created_at, "use_count": existing.use_count}
            )
        if final.id not in seen:
            seen.add(final.id)
            result.proposals.append(Proposal(final, action, reason, existing))
    for refresh in _refresh_candidates(state, store):
        if refresh.id not in seen:
            seen.add(refresh.id)
            result.proposals.append(refresh)
    result.proposals.sort(key=lambda p: p.id)
    return result


def propose_entry(  # noqa: PLR0913, PLR0917  (explicit fields read better than a bag)
    state: RunState,
    store: MemoryStore,
    config: MemoryConfig,
    entry_type: MemoryType,
    domain: str,
    key_tag: str,
    content: str,
    now: datetime | None = None,
) -> Proposal | Finding:
    """Propose one decision or glossary entry. Returns a refusal Finding when unsafe."""
    entry = _make_entry(state, config, entry_type, domain, None, key_tag, content, now or utcnow())
    findings = check_content(
        entry.content,
        config.write.max_content_chars,
        state.scenario.text,
        config.write.max_scenario_overlap_chars,
    )
    if findings:
        return findings[0].model_copy(update={"location": key_tag})
    action, reason, existing = _classify(store, entry)
    return Proposal(entry, action, reason, existing)


def apply_decisions(  # noqa: PLR0913, PLR0917  (explicit arguments keep the approval gate visible)
    store: MemoryStore,
    config: MemoryConfig,
    proposals: list[Proposal],
    decisions: Mapping[str, Decision],
    scenario_text: str = "",
    now: datetime | None = None,
) -> ApplyReport:
    """Save only what was approved or edited. A proposal with no decision is rejected."""
    current = now or utcnow()
    report = ApplyReport()
    for proposal in proposals:
        decision = decisions.get(proposal.id)
        if decision is None or decision.action == "reject":
            report.rejected.append(proposal.id)
            continue
        entry = proposal.entry
        if decision.action == "edit":
            if not decision.content:
                report.rejected.append(proposal.id)
                continue
            entry = entry.model_copy(update={"content": decision.content.strip()})
            findings = check_content(
                entry.content,
                config.write.max_content_chars,
                scenario_text,
                config.write.max_scenario_overlap_chars,
            )
            if findings:
                report.refused.extend(
                    f.model_copy(update={"location": proposal.id}) for f in findings
                )
                continue
        if proposal.action is ProposalAction.REFRESH and decision.action == "approve":
            if store.touch(proposal.id, current):
                report.refreshed.append(proposal.id)
            continue
        entry = entry.model_copy(update={"last_confirmed_at": current})
        try:
            store.put(entry)
        except UnsafeContentError as exc:
            report.refused.extend(exc.findings)
            continue
        report.saved.append(proposal.id)
    return report
