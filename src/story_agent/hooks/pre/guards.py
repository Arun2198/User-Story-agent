"""Pre-hooks: input size, schema version, scope guard, budget."""

from __future__ import annotations

from story_agent.hooks.base import HookContext, blocked, passed
from story_agent.schema import SCHEMA_VERSION, HookPhase, HookResult


class InputSizeHook:
    """Blocks empty or oversized scenario and notes."""

    name = "input_size"
    phase = HookPhase.PRE

    def run(self, ctx: HookContext) -> HookResult:
        """Check lengths against guardrails.yaml limits."""
        limits = ctx.config.guardrails.limits
        scenario = ctx.state.scenario
        if not scenario.text.strip():
            return blocked("INPUT_EMPTY", "scenario is empty", "scenario")
        if len(scenario.text) > limits.max_scenario_chars:
            return blocked("INPUT_TOO_LARGE", "scenario is too long", "scenario")
        if len(scenario.notes) > limits.max_notes_chars:
            return blocked("INPUT_TOO_LARGE", "notes are too long", "notes")
        return passed()


class SchemaVersionHook:
    """Blocks state or memory written under a different schema version."""

    name = "schema_version"
    phase = HookPhase.PRE

    def run(self, ctx: HookContext) -> HookResult:
        """Compare versions on run state, scenario and any recalled memory."""
        if ctx.state.schema_version != SCHEMA_VERSION:
            return blocked("SCHEMA_VERSION_MISMATCH", "run state version differs", "state")
        if ctx.state.scenario.schema_version != SCHEMA_VERSION:
            return blocked("SCHEMA_VERSION_MISMATCH", "scenario version differs", "scenario")
        for entry in ctx.data.get("memory_entries", []):
            if entry.schema_version != SCHEMA_VERSION:
                return blocked("SCHEMA_VERSION_MISMATCH", "memory entry version differs", entry.id)
        return passed()


class ScopeGuardHook:
    """Refuses a run whose request is mainly an attack on the agent's rules."""

    name = "scope_guard"
    phase = HookPhase.PRE

    def run(self, ctx: HookContext) -> HookResult:
        """Run the deterministic scope guard on the scenario text."""
        decision = ctx.services.scope_guard.evaluate(ctx.state.scenario.text)
        if decision is None:
            return passed()
        ctx.data["refusal"] = decision.message
        return blocked("SCOPE_REFUSED", decision.category.value, "scenario")


class BudgetHook:
    """Blocks a component when the run has used up its token, cost or step budget."""

    name = "budget"
    phase = HookPhase.PRE

    def run(self, ctx: HookContext) -> HookResult:
        """Compare run totals with the configured budget."""
        budget = ctx.config.guardrails.budget
        state = ctx.state
        if state.tokens_used >= budget.max_tokens_per_run:
            return blocked("BUDGET_EXCEEDED", "token budget used up", "tokens")
        if state.cost_usd >= budget.max_cost_usd_per_run:
            return blocked("BUDGET_EXCEEDED", "cost budget used up", "cost")
        if state.steps >= budget.max_steps_per_run:
            return blocked("BUDGET_EXCEEDED", "step budget used up", "steps")
        return passed()
