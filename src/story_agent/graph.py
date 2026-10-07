"""The run graph. LangGraph runs the stages in order and pauses where a person decides.

Each node rebuilds a ``Flow`` from the saved run state, does one step and saves the
state back, so a checkpoint is enough to resume in another process. Four nodes pause
with ``interrupt``: clarify answers, memory conflicts, the readiness gate and review
(and the memory approval). A node that calls the model never pauses, so a resume does
not repeat a model call.

The gate is the only place ``go_ahead`` is set, and only from a validated reply.
"""

from __future__ import annotations

import functools
import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, TypedDict, TypeVar, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, ValidationError

from story_agent.clarify.answers import resolve_answer_keys
from story_agent.clarify.limits import ClarifyLimits
from story_agent.clarify.readiness import GateError
from story_agent.deps import StageDeps
from story_agent.flow import Flow, ScopeRefusalError
from story_agent.hooks import HookBlocked
from story_agent.hooks.registry import HookPipeline
from story_agent.memory.conflicts import resolve_conflict, unresolved
from story_agent.memory.proposals import build_proposals
from story_agent.memory.store import MemoryStore, SqliteMemoryStore
from story_agent.pipeline.drafting import NothingToDraftError
from story_agent.replies import AnswersReply, ConflictsReply, GateReply, MemoryReply, ReviewReply
from story_agent.schema import AnswerKind, Finding, RunState, Severity, StoryStatus

Status = Literal["running", "done", "refused", "blocked"]
STOPPED = frozenset({"refused", "blocked"})
M = TypeVar("M", bound=BaseModel)


class GraphState(TypedDict, total=False):
    """What the checkpoint stores. ``run`` is the serialised ``RunState``."""

    run: dict[str, Any]
    status: Status
    message: str
    note: str
    route: str
    memory_report: dict[str, list[str]]


@dataclass
class Runtime:
    """Things the nodes need that do not belong in a checkpoint."""

    deps: StageDeps
    pipeline: HookPipeline
    runs_dir: Path
    memory_dir: Path | None = None
    _stores: dict[str, SqliteMemoryStore] = field(default_factory=dict)

    def memory_for(self, workspace: str) -> MemoryStore | None:
        """Return the memory store of a workspace, or None when memory is off."""
        if self.memory_dir is None:
            return None
        if workspace not in self._stores:
            self._stores[workspace] = SqliteMemoryStore(
                self.memory_dir, workspace, self.deps.config.memory
            )
        return self._stores[workspace]

    def close(self) -> None:
        """Close every open memory store."""
        for store in self._stores.values():
            store.close()
        self._stores.clear()


class InterruptView(Protocol):
    """A pause waiting for a reply."""

    value: object


class TaskView(Protocol):
    """A graph task and its pauses."""

    interrupts: Sequence[InterruptView]


class SnapshotView(Protocol):
    """The saved state of a run."""

    values: dict[str, Any]
    tasks: Sequence[TaskView]


class CompiledRunGraph(Protocol):
    """The part of a compiled graph this project uses."""

    def invoke(self, graph_input: object, config: dict[str, Any]) -> object:
        """Run until the graph ends or pauses."""
        ...

    def get_state(self, config: dict[str, Any]) -> SnapshotView:
        """Return the latest checkpoint."""
        ...


def initial_state(state: RunState) -> GraphState:
    """Return the graph input for a new run."""
    return {"run": state.model_dump(mode="json"), "status": "running", "note": "", "route": ""}


def _parse(model: type[M], raw: object) -> M | None:
    try:
        return model.model_validate(raw)
    except ValidationError:
        return None


def _question_payload(flow: Flow) -> dict[str, Any]:
    state = flow.state
    questions = []
    for q in state.rounds[-1].questions:
        default = q.remembered_default
        questions.append(
            {
                "id": q.id,
                "category": q.category,
                "question": q.question,
                "why_it_matters": q.why_it_matters,
                "options": q.options,
                "remembered_default": None
                if default is None
                else {
                    "memory_id": default.memory_id,
                    "value": default.value,
                    "last_confirmed_at": default.last_confirmed_at.date().isoformat(),
                    "stale": default.stale,
                },
            }
        )
    limits = ClarifyLimits.from_standards(flow.deps.config.standards)
    return {
        "kind": "answers",
        "round": len(state.rounds),
        "rounds_max": limits.max_rounds,
        "questions": questions,
        "free_text_prompt": limits.free_text_prompt,
    }


class RunGraph:
    """Builds the graph for one runtime."""

    def __init__(self, runtime: Runtime) -> None:
        """Keep the runtime the nodes read from."""
        self.rt = runtime

    # ---- plumbing -------------------------------------------------------------

    def _flow(self, gs: GraphState) -> Flow:
        state = RunState.model_validate(gs["run"])
        memory = self.rt.memory_for(state.scenario.workspace)
        return Flow.restore(self.rt.deps, self.rt.pipeline, state, self.rt.runs_dir, memory)

    @staticmethod
    def _save(
        flow: Flow,
        route: str = "",
        note: str = "",
        memory_report: dict[str, list[str]] | None = None,
    ) -> GraphState:
        out: GraphState = {
            "run": flow.state.model_dump(mode="json"),
            "route": route,
            "note": note,
        }
        if memory_report is not None:
            out["memory_report"] = memory_report
        return out

    def _guarded(
        self, fn: Callable[[GraphState], GraphState]
    ) -> Callable[[GraphState], GraphState]:
        """Turn the expected stops (refusal, a blocking hook, nothing to draft) into a status."""

        @functools.wraps(fn)
        def wrapper(gs: GraphState) -> GraphState:
            try:
                return fn(gs)
            except ScopeRefusalError as exc:
                return {"status": "refused", "message": exc.message, "route": ""}
            except HookBlocked as exc:
                if any(f.code == "SCOPE_REFUSED" for f in exc.result.findings):
                    message = self.rt.deps.config.guardrails.scope.refusal_message
                    return {"status": "refused", "message": message, "route": ""}
                return {"status": "blocked", "message": str(exc), "route": ""}
            except (NothingToDraftError, GateError) as exc:
                return {"status": "blocked", "message": str(exc), "route": ""}

        return wrapper

    # ---- nodes ----------------------------------------------------------------

    def intake(self, gs: GraphState) -> GraphState:
        """Redact, scan, check scope."""
        flow = self._flow(gs)
        flow.intake()
        return self._save(flow)

    def recall(self, gs: GraphState) -> GraphState:
        """Recall memory for this workspace."""
        flow = self._flow(gs)
        flow.recall()
        return self._save(flow)

    def discover(self, gs: GraphState) -> GraphState:
        """Build the discovery map."""
        flow = self._flow(gs)
        flow.discover()
        return self._save(flow)

    def clarify_ask(self, gs: GraphState) -> GraphState:
        """Ask the next round, or go to the gate when there is nothing to ask."""
        flow = self._flow(gs)
        outcome = flow.clarify_round()
        if outcome.result is None:
            return self._save(flow, "gate", outcome.reason)
        return self._save(flow, "clarify_collect")

    def clarify_collect(self, gs: GraphState) -> GraphState:
        """Pause for the answers to the latest round, then apply them."""
        flow = self._flow(gs)
        raw = interrupt(_question_payload(flow) | {"note": gs.get("note", "")})
        reply = _parse(AnswersReply, raw)
        if reply is None:
            return self._save(flow, "clarify_collect", "That reply was not understood.")
        resolved, unmatched = resolve_answer_keys(flow.state, reply.answers)
        flow.answer(resolved, reply.free_text)
        note = (
            f"Ignored answers for unknown questions: {', '.join(unmatched)}." if unmatched else ""
        )
        return self._save(flow, "conflicts", note)

    def conflicts(self, gs: GraphState) -> GraphState:
        """Ask which answer wins when a typed answer contradicts memory."""
        flow = self._flow(gs)
        open_ = unresolved(flow.state)
        if not open_:
            return self._save(flow, self._after_round(flow))
        payload = {
            "kind": "conflicts",
            "conflicts": [
                {
                    "question_id": c.question_id,
                    "category": c.category,
                    "remembered": c.remembered,
                    "new": c.new,
                    "stale": c.stale,
                    "last_confirmed_at": c.last_confirmed_at.date().isoformat(),
                }
                for c in open_
            ],
        }
        reply = _parse(ConflictsReply, interrupt(payload))
        if reply is not None:
            known = {c.question_id for c in open_}
            for qid, resolution in reply.resolutions.items():
                if qid in known:
                    resolve_conflict(flow.state, qid, resolution)
        return self._save(flow, "conflicts")

    @staticmethod
    def _after_round(flow: Flow) -> str:
        """Ask again only while must-haves are open and rounds remain. Otherwise the gate."""
        limits = ClarifyLimits.from_standards(flow.deps.config.standards)
        if not flow.readiness().ready and len(flow.state.rounds) < limits.max_rounds:
            return "clarify_ask"
        return "gate"

    def gate(self, gs: GraphState) -> GraphState:
        """Show the readiness summary and wait for the user's decision."""
        flow = self._flow(gs)
        readiness = flow.readiness()
        limits = ClarifyLimits.from_standards(flow.deps.config.standards)
        payload = {
            "kind": "gate",
            "summary": readiness.render(),
            "ready": readiness.ready,
            "rounds_used": len(flow.state.rounds),
            "rounds_max": limits.max_rounds,
            "unresolved_must_have": readiness.unresolved_must_have,
            "open_optional": readiness.open_optional,
            "open_optional_categories": flow.open_optional_categories(),
            "note": gs.get("note", ""),
        }
        reply = _parse(GateReply, interrupt(payload))
        if reply is None:
            return self._save(flow, "gate", "That reply was not understood.")
        if reply.decision == "more":
            if len(flow.state.rounds) >= limits.max_rounds:
                return self._save(flow, "gate", "The round limit is reached.")
            return self._save(flow, "clarify_ask")
        if reply.decision == "go" and not readiness.ready:
            return self._save(flow, "gate", "Must-have items are still unresolved.")
        if reply.decision == "judgment":
            flow.resolve_remaining(AnswerKind.JUDGMENT)
        elif reply.decision == "defer":
            flow.resolve_remaining(AnswerKind.DEFERRED)
        flow.go_ahead(reply.confirmed_by)
        return self._save(flow, "draft")

    def draft(self, gs: GraphState) -> GraphState:
        """Draft, write criteria and critique."""
        flow = self._flow(gs)
        flow.draft()
        return self._save(flow, "review")

    def review(self, gs: GraphState) -> GraphState:
        """Pause for review until every story is approved or rejected."""
        flow = self._flow(gs)
        pending = [s for s in flow.state.stories if s.status is StoryStatus.DRAFT]
        if not pending:
            return self._save(flow, "memory_propose")
        payload = {
            "kind": "review",
            "stories": [s.model_dump(mode="json") for s in pending],
            "findings": [_finding_view(f) for f in flow.state.findings],
            "note": gs.get("note", ""),
        }
        reply = _parse(ReviewReply, interrupt(payload))
        if reply is None:
            return self._save(flow, "review", "That reply was not understood.")
        report = flow.review(reply.actions)
        note = "; ".join(f.message for f in report.findings if f.severity is Severity.WARNING)
        return self._save(flow, "review", note)

    def memory_propose(self, gs: GraphState) -> GraphState:
        """Work out which entries to suggest saving."""
        flow = self._flow(gs)
        proposals = flow.memory_proposals()
        return self._save(flow, "memory_decide" if proposals else "publish")

    def memory_decide(self, gs: GraphState) -> GraphState:
        """Pause for approval of each proposed entry. Nothing is saved without it."""
        flow = self._flow(gs)
        store = flow.memory
        if store is None:
            return self._save(flow, "publish")
        proposals = build_proposals(flow.state, store, self.rt.deps.config.memory).proposals
        payload = {
            "kind": "memory",
            "proposals": [
                {
                    "id": p.id,
                    "type": p.entry.type.value,
                    "content": p.entry.content,
                    "action": p.action.value,
                    "reason": p.reason,
                    "replaces": p.conflict_with.content if p.conflict_with else None,
                }
                for p in proposals
            ],
        }
        reply = _parse(MemoryReply, interrupt(payload))
        decisions = reply.decisions if reply is not None else {}
        report = flow.save_memory(proposals, decisions)
        summary = {
            "saved": report.saved,
            "refreshed": report.refreshed,
            "rejected": report.rejected,
        }
        return self._save(flow, "publish", memory_report=summary)

    def publish(self, gs: GraphState) -> GraphState:
        """Write the finished run. Publishers to other systems attach here later."""
        flow = self._flow(gs)
        folder = self.rt.runs_dir / flow.state.run_id
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "state.json"
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(flow.state.model_dump(mode="json"), handle, indent=2, sort_keys=True)
        out = self._save(flow)
        out["status"] = "done"
        return out

    # ---- wiring ---------------------------------------------------------------

    def build(self, checkpointer: BaseCheckpointSaver[str]) -> CompiledRunGraph:
        """Compile the graph with a checkpointer."""
        graph = StateGraph(GraphState)
        nodes = (
            "intake",
            "recall",
            "discover",
            "clarify_ask",
            "clarify_collect",
            "conflicts",
            "gate",
            "draft",
            "review",
            "memory_propose",
            "memory_decide",
            "publish",
        )
        for name in nodes:
            graph.add_node(name, self._guarded(getattr(self, name)))  # type: ignore[call-overload]
        graph.add_edge(START, "intake")
        for first, second in (("intake", "recall"), ("recall", "discover")):
            graph.add_conditional_edges(first, _stop_or(second), {second: second, END: END})
        graph.add_conditional_edges(
            "discover", _stop_or("clarify_ask"), {"clarify_ask": "clarify_ask", END: END}
        )
        routes: dict[str, tuple[str, ...]] = {
            "clarify_ask": ("gate", "clarify_collect"),
            "clarify_collect": ("conflicts", "clarify_collect"),
            "conflicts": ("conflicts", "clarify_ask", "gate"),
            "gate": ("gate", "clarify_ask", "draft"),
            "draft": ("review",),
            "review": ("review", "memory_propose"),
            "memory_propose": ("memory_decide", "publish"),
            "memory_decide": ("publish",),
        }
        for source, targets in routes.items():
            graph.add_conditional_edges(
                source, _by_route(targets), {**{t: t for t in targets}, END: END}
            )
        graph.add_edge("publish", END)
        return cast(CompiledRunGraph, graph.compile(checkpointer=checkpointer))


def _stop_or(next_node: str) -> Callable[[GraphState], str]:
    def route(gs: GraphState) -> str:
        return END if gs.get("status") in STOPPED else next_node

    return route


def _by_route(targets: tuple[str, ...]) -> Callable[[GraphState], str]:
    def route(gs: GraphState) -> str:
        if gs.get("status") in STOPPED:
            return END
        wanted = gs.get("route", "")
        if wanted not in targets:
            raise RuntimeError(f"unexpected route {wanted!r}")
        return wanted

    return route


def _finding_view(f: Finding) -> dict[str, str]:
    return {
        "code": f.code,
        "severity": f.severity.value,
        "message": f.message,
        "location": f.location or "",
    }
