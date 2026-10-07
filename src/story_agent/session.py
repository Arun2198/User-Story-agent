"""Start and resume runs. This is the only place that talks to the checkpointer.

A responder answers each pause. When it cannot (no terminal, or the answers file
does not cover it) it returns None and the run stays paused, saved in its checkpoint,
until ``resume``.
"""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from story_agent.graph import CompiledRunGraph, GraphState, RunGraph, Runtime, initial_state
from story_agent.schema import RunState, Scenario

MAX_PAUSES = 200  # a safety stop for a responder that keeps giving bad replies
RECURSION_LIMIT = 400
CHECKPOINT_FILE = "checkpoint.sqlite"


class SessionError(RuntimeError):
    """The run cannot be started or resumed."""


class Responder(Protocol):
    """Answers a pause. ``payload['kind']`` is answers, conflicts, gate, review or memory."""

    def respond(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Return a reply, or None when this responder cannot answer."""
        ...


@dataclass
class RunOutcome:
    """Where a run ended up."""

    run_id: str
    status: Literal["done", "paused", "refused", "blocked"]
    state: RunState | None = None
    pending: dict[str, Any] | None = None
    message: str = ""
    memory_report: dict[str, list[str]] | None = None


def new_run_id(now: datetime | None = None) -> str:
    """Return a readable, unique run id."""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    return f"run-{stamp}-{secrets.token_hex(2)}"


class Session:
    """Runs scenarios through the graph for one runtime."""

    def __init__(self, runtime: Runtime) -> None:
        """Keep the runtime used by every run."""
        self.rt = runtime

    def run_dir(self, run_id: str) -> Path:
        """Return the directory of a run."""
        return self.rt.runs_dir / run_id

    def exists(self, run_id: str) -> bool:
        """Say whether a run has a checkpoint."""
        return (self.run_dir(run_id) / CHECKPOINT_FILE).exists()

    @contextmanager
    def _graph(self, run_id: str) -> Iterator[CompiledRunGraph]:
        folder = self.run_dir(run_id)
        folder.mkdir(parents=True, exist_ok=True)
        folder.chmod(0o700)
        path = folder / CHECKPOINT_FILE
        conn = sqlite3.connect(str(path), check_same_thread=False)
        try:
            saver = SqliteSaver(conn)
            yield RunGraph(self.rt).build(saver)
        finally:
            conn.close()
            if path.exists():
                path.chmod(0o600)

    def start(
        self, scenario: Scenario, responder: Responder, run_id: str | None = None
    ) -> RunOutcome:
        """Start a run and drive it until it ends or the responder cannot answer."""
        rid = run_id or new_run_id()
        if self.exists(rid):
            raise SessionError(f"run {rid} already exists")
        state = RunState(run_id=rid, scenario=scenario)
        with self._graph(rid) as graph:
            return self._drive(graph, rid, initial_state(state), responder)

    def resume(self, run_id: str, responder: Responder) -> RunOutcome:
        """Continue a saved run from its checkpoint."""
        if not self.exists(run_id):
            raise SessionError(f"no saved run {run_id}")
        with self._graph(run_id) as graph:
            return self._drive(graph, run_id, None, responder)

    def _drive(
        self, graph: CompiledRunGraph, run_id: str, first: GraphState | None, responder: Responder
    ) -> RunOutcome:
        config = {"configurable": {"thread_id": run_id}, "recursion_limit": RECURSION_LIMIT}
        graph.invoke(first, config)  # None continues from the checkpoint
        for _ in range(MAX_PAUSES):
            pending = _pending(graph, config)
            if pending is None:
                return self._outcome(graph, run_id, config, None)
            reply = responder.respond(pending)
            if reply is None:
                return self._outcome(graph, run_id, config, pending)
            graph.invoke(Command(resume=reply), config)
        raise SessionError("too many pauses; the responder keeps sending replies that are refused")

    @staticmethod
    def _outcome(
        graph: CompiledRunGraph, run_id: str, config: dict[str, Any], pending: dict[str, Any] | None
    ) -> RunOutcome:
        values: dict[str, Any] = graph.get_state(config).values
        state = RunState.model_validate(values["run"]) if "run" in values else None
        status = values.get("status", "running")
        if pending is not None:
            return RunOutcome(run_id, "paused", state, pending, values.get("note", ""))
        if status in {"refused", "blocked"}:
            return RunOutcome(run_id, status, state, None, values.get("message", ""))
        return RunOutcome(run_id, "done", state, None, "", values.get("memory_report"))


def _pending(graph: CompiledRunGraph, config: dict[str, Any]) -> dict[str, Any] | None:
    snapshot = graph.get_state(config)
    for task in snapshot.tasks:
        for item in task.interrupts:
            value = item.value
            if isinstance(value, dict):
                return value
    return None
