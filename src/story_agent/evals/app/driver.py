"""Drive one eval case end to end through the run flow with a simulated user."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from story_agent.clarify.readiness import Readiness
from story_agent.config import AppConfig
from story_agent.deps import StageDeps
from story_agent.discovery.packs import PackSet
from story_agent.evals.cases import EvalCase
from story_agent.evals.simulator import SimulatedUser
from story_agent.flow import Flow, ScopeRefusalError
from story_agent.hooks import HookBlocked, build_pipeline, default_registry
from story_agent.llm import LLMRequest, RawResponse, StructuredClient, Transport, Usage
from story_agent.memory.conflicts import MemoryConflict, detect_conflicts, resolve_conflict
from story_agent.memory.proposals import ApplyReport, Proposal
from story_agent.memory.store import MemoryStore
from story_agent.pipeline.review import ReviewReport
from story_agent.schema import AnswerKind, DiscoveryMap, ItemStatus, Question, RunState, Scenario

MAX_ROUNDS = 3


class CapturingTransport:
    """Wraps a transport and keeps every request, for leak scanning and cost."""

    def __init__(self, inner: Transport) -> None:
        """Wrap ``inner``."""
        self._inner = inner
        self.requests: list[LLMRequest] = []

    def send(self, request: LLMRequest, json_schema: dict[str, Any]) -> RawResponse:
        """Record the request and forward it."""
        self.requests.append(request)
        return self._inner.send(request, json_schema)


@dataclass
class RunRecord:
    """Everything the metrics need from one run."""

    case: EvalCase
    state: RunState | None = None
    discovery_snapshot: DiscoveryMap | None = None
    questions: list[Question] = field(default_factory=list)
    requests: list[LLMRequest] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    seconds: float = 0.0
    error: str | None = None
    refused: bool = False
    forced_resolutions: int = 0
    conflicts: list[MemoryConflict] = field(default_factory=list)
    proposals: list[Proposal] = field(default_factory=list)
    memory_report: ApplyReport | None = None
    invest: dict[str, float] = field(default_factory=dict)
    loops: int = 0
    user: SimulatedUser | None = None
    review: ReviewReport | None = None
    recalled_ids: list[str] = field(default_factory=list)
    readiness: Readiness | None = None
    rounds_to_ready: int = 0


def build_deps(
    config: AppConfig, packs: PackSet, prompts_dir: Path, transport: Transport
) -> StageDeps:
    """Build stage dependencies around a transport."""
    return StageDeps(StructuredClient(transport, config.models), config, packs, prompts_dir)


def _open_optional(flow: Flow) -> list[str]:
    """Categories that are still open and not must-have."""
    discovery, checklist = flow.state.discovery, flow.checklist
    if discovery is None or checklist is None:
        return []
    return sorted(
        {
            i.category
            for i in discovery.items
            if i.status in {ItemStatus.UNKNOWN, ItemStatus.INFERRED}
            and i.resolved_by is None
            and i.category not in checklist.must_have_ids
        }
    )


def run_case(  # noqa: PLR0913, PLR0917  (an end-to-end driver needs these inputs)
    case: EvalCase,
    config: AppConfig,
    packs: PackSet,
    prompts_dir: Path,
    transport: Transport,
    user: SimulatedUser | None = None,
    store: MemoryStore | None = None,
    run_id: str = "eval-run",
    use_recall: bool = True,
) -> RunRecord:
    """Run a case and return a record. Errors are recorded, not raised."""
    capture = CapturingTransport(transport)
    deps = build_deps(config, packs, prompts_dir, capture)
    pipeline = build_pipeline(config.hooks, default_registry())
    sim = user or SimulatedUser(case)
    record = RunRecord(case, user=sim)
    scenario = Scenario(text=case.scenario, notes=case.notes, workspace=case.workspace)
    flow = Flow.start(deps, pipeline, scenario, run_id, None, store)
    record.state = flow.state
    started = time.monotonic()
    try:
        flow.intake()
        recall = flow.recall() if use_recall else None
        record.recalled_ids = recall.ids if recall else []
        flow.discover()
        record.discovery_snapshot = flow.state.discovery
        for _ in range(MAX_ROUNDS):
            outcome = flow.clarify_round()
            if outcome.result is None:
                break
            questions = outcome.result.round.questions
            record.questions.extend(questions)
            flow.answer(sim.answers_for(questions), sim.free_text())
            for conflict in detect_conflicts(flow.state):
                if conflict.question_id not in flow.state.conflict_resolutions:
                    resolve_conflict(flow.state, conflict.question_id, "replace")
            record.rounds_to_ready = len(flow.state.rounds)
            if flow.readiness().ready and not sim.wants_more(_open_optional(flow)):
                break
        if not flow.readiness().ready:
            record.forced_resolutions = len(flow.resolve_remaining(AnswerKind.JUDGMENT))
        record.readiness = flow.readiness()
        flow.go_ahead("user")
        drafting = flow.draft()
        record.invest, record.loops = drafting.invest, drafting.loops
        record.review = flow.review(sim.review(flow.state.stories))
        record.proposals = flow.memory_proposals()
        record.memory_report = flow.save_memory(
            record.proposals, sim.memory_decisions(record.proposals)
        )
    except ScopeRefusalError:
        record.refused = True
    except HookBlocked as exc:
        if any(f.code == "SCOPE_REFUSED" for f in exc.result.findings):
            record.refused = True
        else:
            record.error = f"blocked: {exc}"
    except Exception as exc:
        record.error = f"{type(exc).__name__}: {str(exc)[:160]}"
    record.seconds = time.monotonic() - started
    record.requests = list(capture.requests)
    record.conflicts = detect_conflicts(flow.state)
    record.usage, record.cost_usd = flow.usage, flow.cost_usd
    return record
