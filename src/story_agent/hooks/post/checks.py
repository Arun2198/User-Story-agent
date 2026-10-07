"""Post-hooks that verify component output."""

from __future__ import annotations

from pydantic import BaseModel, ValidationError

from story_agent.guardrails.grounding import GroundingVerifier
from story_agent.hooks.base import HookContext, blocked, passed
from story_agent.schema import HookAction, HookPhase, HookResult

_MIN_LEAK_LEN = 4


class SchemaValidationHook:
    """Re-validates the component output against its schema."""

    name = "schema_validation"
    phase = HookPhase.POST

    def run(self, ctx: HookContext) -> HookResult:
        """Validate ``ctx.data['output']`` against ``ctx.data['output_schema']``."""
        output: BaseModel | None = ctx.data.get("output")
        schema: type[BaseModel] | None = ctx.data.get("output_schema")
        if output is None or schema is None:
            return passed()
        try:
            schema.model_validate(output.model_dump())
        except ValidationError:
            return blocked("SCHEMA_INVALID", "output does not match its schema", ctx.stage)
        return passed()


class GroundingHook:
    """Blocks when any requirement or story is not grounded."""

    name = "grounding"
    phase = HookPhase.POST

    def run(self, ctx: HookContext) -> HookResult:
        """Verify all requirements and stories in run state."""
        state = ctx.state
        if not state.stories and not state.requirements:
            return passed()
        verifier = GroundingVerifier(state, ctx.config.guardrails.grounding)
        report = verifier.verify(state.stories, state.requirements)
        if report.ok:
            return passed()
        return HookResult(action=HookAction.BLOCK, findings=report.findings)


class PiiLeakHook:
    """Blocks output that contains real PII or a value from the redaction map."""

    name = "pii_leak"
    phase = HookPhase.POST

    def run(self, ctx: HookContext) -> HookResult:
        """Scan the serialized output for sensitive values."""
        output: BaseModel | None = ctx.data.get("output")
        if output is None:
            return passed()
        text = output.model_dump_json()
        redactor = ctx.services.redactor
        if redactor.count(text):
            return blocked("PII_LEAK", "output contains sensitive values", ctx.stage)
        lowered = text.casefold()
        for value in redactor.mapping.values():
            if len(value) >= _MIN_LEAK_LEN and value.casefold() in lowered:
                return blocked("PII_LEAK", "output repeats a redacted value", ctx.stage)
        return passed()
