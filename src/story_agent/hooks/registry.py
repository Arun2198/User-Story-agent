"""Hook registry and the ordered pipeline built from config/hooks.yaml."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from story_agent.config import ConfigError
from story_agent.hooks.base import (
    FailMode,
    Hook,
    HookBlocked,
    HookContext,
)
from story_agent.logging import log_event
from story_agent.schema import Finding, HookAction, HookPhase, HookResult, Severity


class HookSpec(BaseModel):
    """One entry in hooks.yaml."""

    model_config = ConfigDict(extra="forbid")

    name: str
    per: Literal["run", "component"]
    fail_mode: FailMode


class HooksConfig(BaseModel):
    """Contents of hooks.yaml."""

    model_config = ConfigDict(extra="forbid")

    pre: list[HookSpec] = []
    post: list[HookSpec] = []


class HookRegistry:
    """Maps hook names to factories. Create one per application."""

    def __init__(self) -> None:
        """Start empty."""
        self._factories: dict[str, Callable[[], Hook]] = {}

    def register(self, name: str, factory: Callable[[], Hook]) -> None:
        """Add a hook factory. Names must be unique."""
        if name in self._factories:
            raise ValueError(f"hook already registered: {name}")
        self._factories[name] = factory

    def create(self, name: str) -> Hook:
        """Build the hook called ``name``."""
        if name not in self._factories:
            raise ConfigError(f"unknown hook in hooks.yaml: {name}")
        return self._factories[name]()


@dataclass(frozen=True)
class _Entry:
    spec: HookSpec
    hook: Hook


@dataclass(frozen=True)
class PipelineResult:
    """Aggregated outcome of one pipeline run."""

    result: HookResult
    blocked_by: str | None = None


class HookPipeline:
    """Runs hooks in configured order. Guardrails fail closed, observers fail open."""

    def __init__(self, pre: list[_Entry], post: list[_Entry]) -> None:
        """Hold the ordered pre and post hooks."""
        self._entries = {HookPhase.PRE: pre, HookPhase.POST: post}

    def names(self, phase: HookPhase) -> list[str]:
        """Return hook names in run order."""
        return [e.spec.name for e in self._entries[phase]]

    def run(
        self, phase: HookPhase, per: Literal["run", "component"], ctx: HookContext
    ) -> PipelineResult:
        """Run matching hooks. Stops at the first block."""
        findings: list[Finding] = []
        action = HookAction.PASS
        for entry in self._entries[phase]:
            if entry.spec.per != per:
                continue
            result = self._run_one(entry, ctx)
            findings.extend(result.findings)
            log_event(
                ctx.run_id,
                ctx.stage,
                "hook",
                hook=entry.spec.name,
                action=result.action.value,
                codes=[f.code for f in result.findings],
            )
            if result.action is HookAction.BLOCK:
                blocked = HookResult(action=HookAction.BLOCK, findings=findings)
                return PipelineResult(blocked, entry.spec.name)
            if result.action is HookAction.MODIFY:
                action = HookAction.MODIFY
        return PipelineResult(HookResult(action=action, findings=findings))

    def enforce(
        self, phase: HookPhase, per: Literal["run", "component"], ctx: HookContext
    ) -> HookResult:
        """Like ``run`` but raises HookBlocked on a block."""
        outcome = self.run(phase, per, ctx)
        if outcome.blocked_by is not None:
            raise HookBlocked(outcome.blocked_by, outcome.result)
        return outcome.result

    @staticmethod
    def _run_one(entry: _Entry, ctx: HookContext) -> HookResult:
        try:
            return entry.hook.run(ctx)
        except Exception as exc:
            finding = Finding(
                code="HOOK_ERROR",
                message=f"{entry.spec.name} raised {type(exc).__name__}",
                severity=Severity.ERROR
                if entry.spec.fail_mode is FailMode.CLOSED
                else Severity.WARNING,
                location=entry.spec.name,
            )
            if entry.spec.fail_mode is FailMode.CLOSED:
                return HookResult(action=HookAction.BLOCK, findings=[finding])
            return HookResult(action=HookAction.PASS, findings=[finding])


def build_pipeline(hooks_config: dict[str, Any], registry: HookRegistry) -> HookPipeline:
    """Validate hooks.yaml and build the pipeline."""
    try:
        config = HooksConfig.model_validate(hooks_config)
    except ValidationError as exc:
        raise ConfigError(f"invalid hooks.yaml: {exc}") from exc
    pre = [_Entry(spec, registry.create(spec.name)) for spec in config.pre]
    post = [_Entry(spec, registry.create(spec.name)) for spec in config.post]
    for entry, phase in [(e, HookPhase.PRE) for e in pre] + [(e, HookPhase.POST) for e in post]:
        if entry.hook.phase is not phase:
            raise ConfigError(
                f"hook {entry.spec.name} is a {entry.hook.phase} hook, listed under {phase}"
            )
    return HookPipeline(pre, post)
