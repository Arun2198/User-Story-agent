"""Hook protocol, context and shared helpers.

``ctx.data`` carries the component-specific values hooks read and write:

- ``untrusted``: dict of text bound for a model prompt (scenario, notes, answers)
- ``call``: dict with prompt_id, prompt_hash, input_hash, memory_ids, model
- ``output`` and ``output_schema``: the validated model output and its class
- ``usage``: dict with input_tokens, output_tokens, cost_usd, latency_s, cached
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from story_agent.config import AppConfig
from story_agent.guardrails.injection import InjectionDetector
from story_agent.guardrails.redaction import Redactor
from story_agent.guardrails.scope import ScopeGuard
from story_agent.schema import Finding, HookAction, HookPhase, HookResult, RunState, Severity


class FailMode(StrEnum):
    """What the runner does when a hook raises."""

    CLOSED = "closed"
    OPEN = "open"


@dataclass
class HookServices:
    """Run-scoped helpers shared by hooks. Built once per run, passed in."""

    redactor: Redactor
    injection: InjectionDetector
    scope_guard: ScopeGuard
    runs_dir: Path | None = None


@dataclass
class HookContext:
    """Everything a hook may read or change."""

    run_id: str
    stage: str
    state: RunState
    config: AppConfig
    services: HookServices
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def run_path(self) -> Path | None:
        """Directory for this run's files, or None when persistence is off."""
        if self.services.runs_dir is None:
            return None
        path = self.services.runs_dir / self.run_id
        path.mkdir(parents=True, exist_ok=True)
        return path


class Hook(Protocol):
    """A guardrail or observability step run before or after a component."""

    name: str
    phase: HookPhase

    def run(self, ctx: HookContext) -> HookResult:
        """Inspect or modify ``ctx`` and report what happened."""
        ...


class HookBlocked(RuntimeError):  # noqa: N818  (reads better than HookBlockedError)
    """Raised when a hook blocks the run."""

    def __init__(self, hook: str, result: HookResult) -> None:
        """Keep the blocking hook name and its result."""
        codes = ", ".join(f.code for f in result.findings) or "no findings"
        super().__init__(f"blocked by {hook}: {codes}")
        self.hook = hook
        self.result = result


def passed() -> HookResult:
    """Return a result that changes nothing."""
    return HookResult(action=HookAction.PASS)


def blocked(code: str, message: str, location: str | None = None) -> HookResult:
    """Return a blocking result with one error finding."""
    finding = Finding(code=code, message=message, severity=Severity.ERROR, location=location)
    return HookResult(action=HookAction.BLOCK, findings=[finding])


def modified(findings: list[Finding] | None = None) -> HookResult:
    """Return a result saying the hook changed something."""
    return HookResult(action=HookAction.MODIFY, findings=findings or [])


def write_json_line(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON object to ``path``."""
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")


def now_iso() -> str:
    """Return the current UTC time as ISO text."""
    return datetime.now(UTC).isoformat()


def sync_state_text(ctx: HookContext, key: str, text: str) -> None:
    """Mirror an untrusted text back into run state when it is the scenario or notes."""
    if key == "scenario":
        ctx.state.redacted_text = text
    elif key == "notes":
        ctx.state.redacted_notes = text
