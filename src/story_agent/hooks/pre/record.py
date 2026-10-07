"""Pre-hook that records the prompt hash and memory ids of each call."""

from __future__ import annotations

from story_agent.hooks.base import HookContext, now_iso, passed, write_json_line
from story_agent.schema import HookPhase, HookResult


class RecordPromptHook:
    """Writes a ``call_start`` event to the run trace. Observability only."""

    name = "record_prompt"
    phase = HookPhase.PRE

    def run(self, ctx: HookContext) -> HookResult:
        """Append prompt id, prompt hash, input hash, memory ids and model."""
        path = ctx.run_path
        call = ctx.data.get("call")
        if path is not None and call:
            event = {"ts": now_iso(), "type": "call_start", "stage": ctx.stage, **call}
            write_json_line(path / "trace.jsonl", event)
        return passed()
