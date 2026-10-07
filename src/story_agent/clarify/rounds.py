"""Run one clarification round, or report that none is needed."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from story_agent.clarify.limits import ClarifyLimits
from story_agent.clarify.questions import RoundResult, build_round
from story_agent.clarify.select import select_for_round
from story_agent.deps import StageDeps
from story_agent.discovery.packs import Checklist
from story_agent.schema import MemoryEntry, RunState


@dataclass
class RoundOutcome:
    """Either a new round, or the reason there is none."""

    result: RoundResult | None
    reason: str = ""
    free_text_prompt: str = ""
    notes: list[str] = field(default_factory=list)


def next_round(
    deps: StageDeps,
    state: RunState,
    checklist: Checklist,
    memory: Sequence[MemoryEntry] = (),
) -> RoundOutcome:
    """Ask the next round, append it to state, and return it.

    Returns no round when everything is settled or the round limit is reached. In
    both cases the caller shows the readiness summary and waits for the user.
    """
    limits = ClarifyLimits.from_standards(deps.config.standards)
    if state.discovery is None:
        raise ValueError("discover must run before clarify")
    if len(state.rounds) >= limits.max_rounds:
        return RoundOutcome(None, "round limit reached")
    selected = select_for_round(state.discovery, checklist, limits)
    if not selected:
        return RoundOutcome(None, "no open items")
    result = build_round(deps, state, selected, checklist.must_have_ids, memory)
    state.rounds.append(result.round)
    return RoundOutcome(result, "", limits.free_text_prompt)
