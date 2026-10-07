"""Hook framework and the default hook set."""

from __future__ import annotations

from story_agent.hooks.base import FailMode, Hook, HookBlocked, HookContext, HookServices
from story_agent.hooks.post.checks import GroundingHook, PiiLeakHook, SchemaValidationHook
from story_agent.hooks.post.memory import MemoryProposalHook
from story_agent.hooks.post.observe import MetricsHook, TraceHook, UsageAccountingHook
from story_agent.hooks.pre.guards import (
    BudgetHook,
    InputSizeHook,
    SchemaVersionHook,
    ScopeGuardHook,
)
from story_agent.hooks.pre.record import RecordPromptHook
from story_agent.hooks.pre.untrusted import InjectionScanHook, RedactHook
from story_agent.hooks.registry import HookPipeline, HookRegistry, build_pipeline

__all__ = [
    "FailMode",
    "Hook",
    "HookBlocked",
    "HookContext",
    "HookPipeline",
    "HookRegistry",
    "HookServices",
    "build_pipeline",
    "default_registry",
]


def default_registry() -> HookRegistry:
    """Return a registry with every built-in hook."""
    registry = HookRegistry()
    for factory in (
        InputSizeHook,
        SchemaVersionHook,
        ScopeGuardHook,
        RedactHook,
        InjectionScanHook,
        BudgetHook,
        RecordPromptHook,
        SchemaValidationHook,
        GroundingHook,
        PiiLeakHook,
        UsageAccountingHook,
        MetricsHook,
        TraceHook,
        MemoryProposalHook,
    ):
        registry.register(factory.name, factory)
    return registry
