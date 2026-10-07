"""Post-hooks for accounting and observability."""

from __future__ import annotations

from story_agent.hooks.base import HookContext, now_iso, passed, write_json_line
from story_agent.logging import log_event
from story_agent.schema import HookPhase, HookResult


class UsageAccountingHook:
    """Adds a component's tokens, cost and a step to the run totals.

    This feeds the budget hook, so it fails closed.
    """

    name = "usage_accounting"
    phase = HookPhase.POST

    def run(self, ctx: HookContext) -> HookResult:
        """Update run state from ``ctx.data['usage']``."""
        usage = ctx.data.get("usage")
        if not usage:
            return passed()
        ctx.state.tokens_used += int(usage.get("input_tokens", 0)) + int(
            usage.get("output_tokens", 0)
        )
        ctx.state.cost_usd += float(usage.get("cost_usd", 0.0))
        ctx.state.steps += 1
        return passed()


class MetricsHook:
    """Logs cost, latency and token counts. Fails open."""

    name = "metrics"
    phase = HookPhase.POST

    def run(self, ctx: HookContext) -> HookResult:
        """Emit one structured log record for the component."""
        usage = ctx.data.get("usage") or {}
        log_event(ctx.run_id, ctx.stage, "component_done", **usage)
        return passed()


class TraceHook:
    """Appends a ``call_end`` event to runs/<run_id>/trace.jsonl. Fails open."""

    name = "trace"
    phase = HookPhase.POST

    def run(self, ctx: HookContext) -> HookResult:
        """Persist stage, call metadata and usage. No prompt or scenario text."""
        path = ctx.run_path
        if path is None:
            return passed()
        event = {
            "ts": now_iso(),
            "type": "call_end",
            "stage": ctx.stage,
            "call": ctx.data.get("call", {}),
            "usage": ctx.data.get("usage", {}),
            "tokens_total": ctx.state.tokens_used,
            "cost_total_usd": ctx.state.cost_usd,
        }
        write_json_line(path / "trace.jsonl", event)
        return passed()
