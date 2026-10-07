"""The run flow: pipeline stages wrapped in hooks, as plain steps.

Each step runs the component pre-hooks, the stage, then the component post-hooks.
The evals drive these steps with a simulated user; the graph and CLI drive the same
steps with a person. Nothing here talks to a terminal.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import BaseModel

from story_agent.clarify.answers import (
    CleanText,
    answer_questions,
    resolve_remaining,
    sanitize_texts,
    submit_free_text,
)
from story_agent.clarify.readiness import (
    Readiness,
    assess,
    grant_go_ahead,
    must_have_open_categories,
)
from story_agent.clarify.rounds import RoundOutcome, next_round
from story_agent.deps import StageDeps
from story_agent.discovery.discover import DiscoverResult, run_discover
from story_agent.discovery.packs import Checklist
from story_agent.guardrails.injection import InjectionDetector
from story_agent.guardrails.redaction import Redactor
from story_agent.guardrails.scope import ScopeDecision, ScopeGuard
from story_agent.hooks.base import HookContext, HookServices
from story_agent.hooks.registry import HookPipeline
from story_agent.intake.preferences import extract_preferences, merge_preferences
from story_agent.llm import LLMRequest, Usage
from story_agent.memory.proposals import (
    ApplyReport,
    Decision,
    Proposal,
    apply_decisions,
    build_proposals,
)
from story_agent.memory.recall import RecallResult, memory_recall
from story_agent.memory.store import MemoryStore
from story_agent.pipeline.drafting import DraftingResult, run_drafting
from story_agent.pipeline.review import (
    CleanActions,
    ReviewAction,
    ReviewReport,
    apply_review,
    sanitize_actions,
)
from story_agent.pipeline.scope_check import check_scope
from story_agent.schema import (
    AnswerKind,
    Finding,
    HookPhase,
    ItemStatus,
    MemoryEntry,
    RunState,
    Scenario,
)

T = TypeVar("T")


class ScopeRefusalError(RuntimeError):
    """The request was refused by scope. ``message`` is the fixed refusal text."""

    def __init__(self, message: str) -> None:
        """Keep the fixed refusal message."""
        super().__init__(message)
        self.message = message


class Payload(BaseModel):
    """Serialised stage output, so output hooks can scan it."""

    content: str


def call_info(request: LLMRequest | None) -> dict[str, object]:
    """Return the prompt hash, input hash, memory ids and model of a call."""
    if request is None:
        return {}
    return {
        "prompt_id": request.prompt_id,
        "prompt_hash": request.prompt_hash,
        "input_hash": request.input_hash,
        "memory_ids": list(request.memory_ids),
        "model": request.model,
    }


@dataclass
class Flow:
    """One run. Create with ``Flow.start`` and call the steps in order."""

    deps: StageDeps
    pipeline: HookPipeline
    services: HookServices
    state: RunState
    memory: MemoryStore | None = None
    checklist: Checklist | None = None
    memory_entries: list[MemoryEntry] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    requests: list[LLMRequest] = field(default_factory=list)

    @classmethod
    def start(  # noqa: PLR0913, PLR0917  (a run needs these inputs)
        cls,
        deps: StageDeps,
        pipeline: HookPipeline,
        scenario: Scenario,
        run_id: str,
        runs_dir: Path | None = None,
        memory: MemoryStore | None = None,
        redactions: Mapping[str, str] | None = None,
    ) -> Flow:
        """Create a run. ``redactions`` continues numbering when a run is resumed."""
        services = HookServices(
            redactor=Redactor(redactions),
            injection=InjectionDetector(),
            scope_guard=ScopeGuard(deps.config.guardrails.scope.refusal_message),
            runs_dir=runs_dir,
            memory=memory,
        )
        state = RunState(run_id=run_id, scenario=scenario)
        return cls(deps, pipeline, services, state, memory)

    @classmethod
    def restore(
        cls,
        deps: StageDeps,
        pipeline: HookPipeline,
        state: RunState,
        runs_dir: Path | None = None,
        memory: MemoryStore | None = None,
    ) -> Flow:
        """Rebuild a flow from saved run state, for a resumed or checkpointed run."""
        mapping: dict[str, str] = {}
        saved = runs_dir / state.run_id / "redaction_map.json" if runs_dir else None
        if saved is not None and saved.exists():
            mapping = json.loads(saved.read_text(encoding="utf-8"))
        flow = cls.start(deps, pipeline, state.scenario, state.run_id, runs_dir, memory, mapping)
        flow.state = state
        if state.discovery is not None:
            found = state.discovery
            flow.checklist = deps.packs.checklist(found.domain, found.subdomain, found.subpacks)
        if memory is not None:
            loaded = (memory.get(i) for i in state.recalled_memory_ids)
            flow.memory_entries = [e for e in loaded if e is not None]
        return flow

    # ---- plumbing -----------------------------------------------------------

    def _ctx(self, stage: str) -> HookContext:
        self.state.stage = stage
        return HookContext(
            self.state.run_id, stage, self.state, self.deps.config, self.services, {}
        )

    def _around(
        self,
        stage: str,
        run: Callable[[], T],
        requests: Callable[[T], Sequence[LLMRequest]],
        usage: Callable[[T], tuple[Usage, float]],
        payload: Callable[[T], str],
    ) -> T:
        ctx = self._ctx(stage)
        self.pipeline.enforce(HookPhase.PRE, "component", ctx)
        started = time.monotonic()
        result = run()
        latency = round(time.monotonic() - started, 3)
        used, cost = usage(result)
        reqs = list(requests(result))
        self.usage = Usage(
            self.usage.input_tokens + used.input_tokens,
            self.usage.output_tokens + used.output_tokens,
        )
        self.cost_usd += cost
        self.requests.extend(reqs)
        ctx.data["call"] = call_info(reqs[-1] if reqs else None)
        ctx.data["calls"] = [call_info(r) for r in reqs]
        ctx.data["usage"] = {
            "input_tokens": used.input_tokens,
            "output_tokens": used.output_tokens,
            "cost_usd": cost,
            "latency_s": latency,
        }
        ctx.data["output"] = Payload(content=payload(result))
        ctx.data["output_schema"] = Payload
        self.pipeline.enforce(HookPhase.POST, "component", ctx)
        return result

    # ---- steps --------------------------------------------------------------

    def intake(self) -> ScopeDecision:
        """Run-level guards, redaction and injection scan, preferences, then scope check.

        Raises HookBlocked for a guardrail block and ScopeRefusalError for a refusal.
        """
        ctx = self._ctx("intake")
        self.pipeline.enforce(HookPhase.PRE, "run", ctx)
        scenario = self.state.scenario
        ctx.data["untrusted"] = {"scenario": scenario.text, "notes": scenario.notes}
        self.pipeline.enforce(HookPhase.PRE, "component", ctx)
        self.state.preferences = merge_preferences(
            self.state.preferences, extract_preferences(self.state.redacted_notes)
        )
        decision = self._around(
            "scope_check",
            lambda: check_scope(
                self.deps.client, self.deps.config, self.deps.prompts_dir, self.state.redacted_text
            ),
            lambda _d: [],
            lambda _d: (Usage(), 0.0),
            lambda d: d.category.value,
        )
        if not decision.allowed:
            raise ScopeRefusalError(decision.message)
        return decision

    def recall(self) -> RecallResult | None:
        """Stage 3: recall saved entries for this run. Does nothing without a store."""
        if self.memory is None:
            return None
        result = memory_recall(self.memory, self.deps.packs, self.deps.config.memory, self.state)
        self.memory_entries = result.entries
        ctx = self._ctx("memory_recall")
        ctx.data["memory_entries"] = self.memory_entries
        self.pipeline.enforce(HookPhase.PRE, "run", ctx)
        return result

    def discover(self) -> DiscoverResult:
        """Stage 4: build the discovery map."""

        def run() -> DiscoverResult:
            return run_discover(self.deps, self.state, self.memory_entries)

        result = self._around(
            "discover",
            run,
            lambda r: [r.request] if r.request else [],
            lambda r: (r.usage, r.cost_usd),
            lambda r: r.discovery.model_dump_json(),
        )
        self.state.discovery = result.discovery
        self.checklist = result.checklist
        self.findings.extend(result.findings)
        return result

    def clarify_round(self) -> RoundOutcome:
        """Stage 5: ask the next round, or report that none is needed."""
        checklist = self._need_checklist()
        outcome_box: list[RoundOutcome] = []

        def run() -> RoundOutcome:
            outcome = next_round(self.deps, self.state, checklist, self.memory_entries)
            outcome_box.append(outcome)
            return outcome

        outcome = self._around(
            "clarify",
            run,
            lambda o: [o.result.request] if o.result and o.result.request else [],
            lambda o: (o.result.usage, o.result.cost_usd) if o.result else (Usage(), 0.0),
            lambda o: o.result.round.model_dump_json() if o.result else "{}",
        )
        if outcome.result is not None:
            self.findings.extend(outcome.result.findings)
        return outcome

    def answer(self, answers: Mapping[str, str], free_text: str = "") -> list[Finding]:
        """Apply answers keyed by question id, and an optional free-text reply."""
        ctx = self._ctx("clarify")
        raw = dict(answers)
        if free_text:
            raw["__free_text__"] = free_text
        clean = sanitize_texts(self.pipeline, ctx, raw)
        text = clean.pop("__free_text__", None)
        findings = answer_questions(self.state, clean)
        if text:
            findings += submit_free_text(self.state, CleanText(text))
        self.findings.extend(findings)
        return findings

    def readiness(self) -> Readiness:
        """Return the readiness summary."""
        return assess(self.state, self._need_checklist())

    def open_optional_categories(self) -> list[str]:
        """Categories still open that are not must-have. A user may choose to cover them."""
        checklist = self._need_checklist()
        if self.state.discovery is None:
            return []
        return sorted(
            {
                i.category
                for i in self.state.discovery.items
                if i.status in {ItemStatus.UNKNOWN, ItemStatus.INFERRED}
                and i.resolved_by is None
                and i.category not in checklist.must_have_ids
            }
        )

    def resolve_remaining(self, kind: AnswerKind) -> list[str]:
        """Record one explicit decision for every unresolved must-have category."""
        checklist = self._need_checklist()
        categories = must_have_open_categories(self.state, checklist)
        resolve_remaining(self.state, categories, kind)
        return categories

    def go_ahead(self, confirmed_by: Literal["user", "answers_file"]) -> None:
        """Open the gate. Only call this after the user (or an answers file) said go."""
        grant_go_ahead(self.state, self._need_checklist(), confirmed_by)

    def draft(self) -> DraftingResult:
        """Stages 6 to 8: draft, criteria and critique with revisions."""
        checklist = self._need_checklist()
        result = self._around(
            "draft",
            lambda: run_drafting(self.deps, self.state, checklist),
            lambda r: r.requests,
            lambda r: (r.usage, r.cost_usd),
            lambda r: json.dumps([s.model_dump(mode="json") for s in r.stories]),
        )
        self.findings.extend(result.findings)
        return result

    def review(self, actions: Sequence[ReviewAction], reviewer: str = "user") -> ReviewReport:
        """Stage 9: sanitise and apply review actions, then run the strict review hooks."""
        ctx = self._ctx("human_review")
        clean, findings = sanitize_actions(self.pipeline, ctx, actions)
        report = apply_review(self.state, self.deps.config, CleanActions(list(clean)), reviewer)
        report.findings.extend(findings)
        ctx = self._ctx("human_review")
        self.pipeline.enforce(HookPhase.POST, "component", ctx)
        return report

    def memory_proposals(self) -> list[Proposal]:
        """Stage 10: propose memory entries. Nothing is saved here."""
        ctx = self._ctx("memory_write_proposal")
        self.pipeline.run(HookPhase.POST, "run", ctx)
        if self.memory is None:
            return []
        result = build_proposals(self.state, self.memory, self.deps.config.memory)
        self.findings.extend(result.refused)
        return result.proposals

    def save_memory(
        self, proposals: Sequence[Proposal], decisions: Mapping[str, Decision]
    ) -> ApplyReport:
        """Save only the approved or edited proposals."""
        if self.memory is None:
            return ApplyReport()
        report = apply_decisions(
            self.memory,
            self.deps.config.memory,
            list(proposals),
            decisions,
            self.state.scenario.text,
        )
        self.state.memory_outcome = {
            "saved": list(report.saved),
            "refreshed": list(report.refreshed),
            "rejected": list(report.rejected),
        }
        return report

    def _need_checklist(self) -> Checklist:
        if self.checklist is None:
            raise RuntimeError("discover must run first")
        return self.checklist
