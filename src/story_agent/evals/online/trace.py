"""An OpenTelemetry-compatible trace of one run.

The shape follows the OTLP trace data model (trace and span ids, nanosecond times, span
kind, status, attributes, events, resource). It holds ids, hashes, counts and numbers only.
It never holds scenario text, prompt text, answers or story text. Attribute names for model
calls follow the ``gen_ai.*`` conventions; everything else sits under ``story_agent.*``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from story_agent import __version__
from story_agent.guardrails.redaction import Redactor
from story_agent.schema import SCHEMA_VERSION, RunState, StoryStatus

SERVICE_NAME = "story-agent"
SCOPE_NAME = "story_agent"
MAX_ATTR_CHARS = 200
AttrValue = str | int | float | bool


class SpanKind(StrEnum):
    """OTLP span kinds used here."""

    INTERNAL = "INTERNAL"
    CLIENT = "CLIENT"


class StatusCode(StrEnum):
    """OTLP status codes."""

    UNSET = "UNSET"
    OK = "OK"
    ERROR = "ERROR"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_attributes(values: Mapping[str, AttrValue]) -> Mapping[str, AttrValue]:
    for key, value in values.items():
        if (
            not key
            or not key[0].islower()
            or not all(c.islower() or c.isdigit() or c in "._" for c in key)
        ):
            raise ValueError(f"bad attribute name {key!r}")
        if isinstance(value, str) and len(value) > MAX_ATTR_CHARS:
            raise ValueError(f"attribute {key} is longer than {MAX_ATTR_CHARS} characters")
    return values


class SpanEvent(_Model):
    """A point in time inside a span."""

    name: str
    time_unix_nano: int = Field(ge=0)
    attributes: dict[str, AttrValue] = Field(default_factory=dict)

    _attrs = field_validator("attributes")(_check_attributes)


class Span(_Model):
    """One span."""

    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    span_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    parent_span_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    name: str
    kind: SpanKind = SpanKind.INTERNAL
    start_time_unix_nano: int = Field(ge=0)
    end_time_unix_nano: int = Field(ge=0)
    status_code: StatusCode = StatusCode.UNSET
    status_message: str = Field(default="", max_length=MAX_ATTR_CHARS)
    attributes: dict[str, AttrValue] = Field(default_factory=dict)
    events: list[SpanEvent] = Field(default_factory=list)

    _attrs = field_validator("attributes")(_check_attributes)

    @model_validator(mode="after")
    def _times_in_order(self) -> Span:
        if self.end_time_unix_nano < self.start_time_unix_nano:
            raise ValueError("a span cannot end before it starts")
        return self


class Trace(_Model):
    """All spans of one run, with the resource that produced them."""

    schema_version: str = SCHEMA_VERSION
    resource: dict[str, AttrValue]
    scope: str = SCOPE_NAME
    spans: list[Span]

    _attrs = field_validator("resource")(_check_attributes)

    def strings(self) -> list[str]:
        """Return every string in the trace, for leak checks."""
        found = [v for v in self.resource.values() if isinstance(v, str)]
        for span in self.spans:
            found.append(span.name)
            found.extend(v for v in span.attributes.values() if isinstance(v, str))
            for event in span.events:
                found.extend(v for v in event.attributes.values() if isinstance(v, str))
        return found


def short_hash(*parts: str, size: int) -> str:
    """Return a stable hex id of ``size`` characters."""
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:size]


def _nanos(stamp: str) -> int:
    return int(datetime.fromisoformat(stamp).timestamp() * 1_000_000_000)


def read_events(path: Path) -> list[dict[str, Any]]:
    """Read ``trace.jsonl``. A missing file is an empty trace; bad lines are skipped."""
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("type") == "call_end":
            events.append(record)
    return events


def build_trace(state: RunState, events: Sequence[Mapping[str, Any]]) -> Trace:
    """Turn a run's state and its ``call_end`` events into a trace."""
    trace_id = short_hash(state.run_id, size=32)
    root_id = short_hash(state.run_id, "root", size=16)
    spans: list[Span] = []
    first = last = 0
    for index, event in enumerate(events):
        usage = event.get("usage") or {}
        call = event.get("call") or {}
        end = _nanos(str(event["ts"]))
        start = max(0, end - int(float(usage.get("latency_s", 0.0)) * 1_000_000_000))
        first = start if index == 0 else min(first, start)
        last = max(last, end)
        attrs: dict[str, AttrValue] = {
            "story_agent.stage": str(event.get("stage", "")),
            "story_agent.cost_usd": float(usage.get("cost_usd", 0.0)),
            "gen_ai.usage.input_tokens": int(usage.get("input_tokens", 0)),
            "gen_ai.usage.output_tokens": int(usage.get("output_tokens", 0)),
        }
        if call.get("model"):
            attrs["gen_ai.system"] = "anthropic"
            attrs["gen_ai.request.model"] = str(call["model"])
            attrs["story_agent.prompt_id"] = str(call.get("prompt_id", ""))
            attrs["story_agent.prompt_hash"] = str(call.get("prompt_hash", ""))
            attrs["story_agent.input_hash"] = str(call.get("input_hash", ""))
            attrs["story_agent.memory_ids"] = ",".join(call.get("memory_ids", []))
        spans.append(
            Span(
                trace_id=trace_id,
                span_id=short_hash(state.run_id, str(index), str(event.get("stage")), size=16),
                parent_span_id=root_id,
                name=f"story_agent.{event.get('stage', 'stage')}",
                kind=SpanKind.CLIENT if call.get("model") else SpanKind.INTERNAL,
                start_time_unix_nano=start,
                end_time_unix_nano=end,
                status_code=StatusCode.OK,
                attributes=attrs,
            )
        )
    stories = [s for s in state.stories if s.status is not StoryStatus.REJECTED]
    root = Span(
        trace_id=trace_id,
        span_id=root_id,
        name="story_agent.run",
        start_time_unix_nano=first,
        end_time_unix_nano=last,
        status_code=StatusCode.OK,
        attributes={
            "story_agent.run_id": state.run_id,
            "story_agent.workspace_hash": short_hash(state.scenario.workspace, size=12),
            "story_agent.go_ahead_by": state.go_ahead_by or "",
            "story_agent.rounds": len(state.rounds),
            "story_agent.questions": sum(len(r.questions) for r in state.rounds),
            "story_agent.stories": len(stories),
            "story_agent.critique_loops": state.critique_loops,
            "story_agent.cost_usd": round(state.cost_usd, 6),
            "gen_ai.usage.total_tokens": state.tokens_used,
        },
        events=[
            SpanEvent(
                name="story_agent.review",
                time_unix_nano=_nanos(str(entry["ts"])) if entry.get("ts") else last,
                attributes={
                    "story_agent.story_id": str(entry.get("story_id", "")),
                    "story_agent.action": str(entry.get("action", "")),
                    "story_agent.edit_distance": float(entry.get("edit_distance", 0.0)),
                },
            )
            for entry in state.review_log
        ],
    )
    resource: dict[str, AttrValue] = {
        "service.name": SERVICE_NAME,
        "service.version": __version__,
    }
    return Trace(resource=resource, spans=[root, *spans])


def assert_trace_safe(trace: Trace, redactions: Mapping[str, str] | None = None) -> None:
    """Raise ValueError if any string in the trace looks like PII or repeats a redacted value."""
    redactor = Redactor(redactions)
    values = {v.casefold() for v in (redactions or {}).values() if len(v) >= 4}
    for text in trace.strings():
        if redactor.count(text):
            raise ValueError("the trace contains a sensitive value")
        if any(v in text.casefold() for v in values):
            raise ValueError("the trace repeats a redacted value")


def _otlp_value(value: AttrValue) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": value}


def _otlp_attrs(values: Mapping[str, AttrValue]) -> list[dict[str, Any]]:
    return [{"key": k, "value": _otlp_value(v)} for k, v in values.items()]


def to_otlp(trace: Trace) -> dict[str, Any]:
    """Return the trace as an OTLP/JSON ``resourceSpans`` document."""
    kinds = {SpanKind.INTERNAL: 1, SpanKind.CLIENT: 3}
    codes = {StatusCode.UNSET: 0, StatusCode.OK: 1, StatusCode.ERROR: 2}
    spans = []
    for s in trace.spans:
        item: dict[str, Any] = {
            "traceId": s.trace_id,
            "spanId": s.span_id,
            "name": s.name,
            "kind": kinds[s.kind],
            "startTimeUnixNano": str(s.start_time_unix_nano),
            "endTimeUnixNano": str(s.end_time_unix_nano),
            "attributes": _otlp_attrs(s.attributes),
            "events": [
                {
                    "name": e.name,
                    "timeUnixNano": str(e.time_unix_nano),
                    "attributes": _otlp_attrs(e.attributes),
                }
                for e in s.events
            ],
            "status": {"code": codes[s.status_code], "message": s.status_message},
        }
        if s.parent_span_id:
            item["parentSpanId"] = s.parent_span_id
        spans.append(item)
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": _otlp_attrs(trace.resource)},
                "scopeSpans": [{"scope": {"name": trace.scope}, "spans": spans}],
            }
        ]
    }
