"""Online evaluation: record how real runs go, without touching the run itself.

Disabled by default. When enabled it builds a trace and feedback signals for a finished
run, optionally scores a sample with the judge, and hands the record to a sink. The caller
treats every failure here as non-fatal.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from story_agent.config import ConfigError
from story_agent.evals.app.judge import JudgeScores
from story_agent.evals.online.config import OnlineConfig, parse_online
from story_agent.evals.online.feedback import extract_feedback
from story_agent.evals.online.record import OnlineRecord, OnlineSink
from story_agent.evals.online.sinks import JsonlSink, PendingInputError, pending_sink
from story_agent.evals.online.trace import assert_trace_safe, build_trace, read_events
from story_agent.schema import RunState

JudgeFn = Callable[[RunState], JudgeScores]


class OnlineEvaluator(Protocol):
    """Called once when a run finishes."""

    def on_run_finished(self, state: RunState, runs_dir: Path) -> None:
        """Observe a finished run. Must not change it."""
        ...


class NullEvaluator:
    """Does nothing. The default."""

    def on_run_finished(self, state: RunState, runs_dir: Path) -> None:
        """Ignore the run."""


def sampled(run_id: str, rate: float, salt: str) -> bool:
    """Decide, the same way every time, whether a run is in a sample of ``rate``."""
    if rate <= 0.0:
        return False
    if rate >= 1.0:
        return True
    digest = hashlib.sha256(f"{salt}:{run_id}".encode()).hexdigest()
    return int(digest[:8], 16) / 0x1_0000_0000 < rate


class SamplingEvaluator:
    """Builds a record for sampled runs and sends it to a sink."""

    def __init__(
        self, config: OnlineConfig, sink: OnlineSink, judge: JudgeFn | None = None
    ) -> None:
        """Wire the settings, the sink and an optional judge."""
        self.config = config
        self.sink = sink
        self.judge = judge

    def on_run_finished(self, state: RunState, runs_dir: Path) -> None:
        """Record the run if it is in the trace sample."""
        if not sampled(state.run_id, self.config.trace_sample_rate, "trace"):
            return
        folder = runs_dir / state.run_id
        events = read_events(folder / "trace.jsonl")
        trace = build_trace(state, events)
        assert_trace_safe(trace, _redactions(folder))
        feedback = extract_feedback(state, llm_calls=len(events))
        self.sink.send(OnlineRecord(state.run_id, trace, feedback, self._score(state)))

    def _score(self, state: RunState) -> dict[str, float]:
        judge = self.config.judge
        if self.judge is None or not judge.enabled:
            return {}
        if not sampled(state.run_id, judge.sample_rate, "judge"):
            return {}
        scores = self.judge(state)
        return {
            "judge_groundedness": scores.groundedness,
            "judge_completeness": scores.completeness,
            "judge_testability": scores.testability,
            "judge_unsupported_claims": float(scores.unsupported_claims),
        }


def _redactions(folder: Path) -> dict[str, str]:
    path = folder / "redaction_map.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def build_online_evaluator(
    evals_config: dict[str, Any], runs_dir: Path, judge: JudgeFn | None = None
) -> OnlineEvaluator:
    """Return the evaluator the config asks for. Disabled means ``NullEvaluator``.

    A sink that is still waiting for details is refused here, at start-up, so a run never
    finds out half way.
    """
    config = parse_online(evals_config)
    if not config.enabled:
        return NullEvaluator()
    if config.sink == "jsonl":
        return SamplingEvaluator(config, JsonlSink(runs_dir / config.path), judge)
    try:
        sink = pending_sink(config.sink)
    except KeyError as exc:  # pragma: no cover  (parse_online limits the names)
        raise ConfigError(f"unknown online sink {config.sink}") from exc
    raise PendingInputError(sink.PENDING)
