"""The record kept about one finished run, and the sink interface."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from story_agent.evals.online.feedback import FeedbackSignals
from story_agent.evals.online.trace import Trace
from story_agent.schema import SCHEMA_VERSION


@dataclass(frozen=True)
class OnlineRecord:
    """What is kept about one finished run."""

    run_id: str
    trace: Trace
    feedback: FeedbackSignals
    judge: Mapping[str, float] = field(default_factory=dict)

    def metrics(self) -> dict[str, float]:
        """Return feedback metrics plus judge scores, for drift checks."""
        return {**self.feedback.metrics(), **self.judge}

    def to_json_line(self) -> str:
        """Return the record as one JSON line."""
        return json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "run_id": self.run_id,
                "trace": self.trace.model_dump(mode="json"),
                "feedback": self.feedback.model_dump(mode="json"),
                "metrics": self.metrics(),
            },
            sort_keys=True,
        )


class OnlineSink(Protocol):
    """Where records go."""

    def send(self, record: OnlineRecord) -> None:
        """Store or forward one record."""
        ...
