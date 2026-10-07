"""Post-hook that proposes memory entries at the end of a run."""

from __future__ import annotations

import json
from pathlib import Path

from story_agent.hooks.base import HookContext, now_iso, passed
from story_agent.memory.proposals import Proposal, build_proposals
from story_agent.schema import Finding, HookPhase, HookResult, Severity


class MemoryProposalHook:
    """Builds write proposals into ``ctx.data['memory_proposals']``. It never saves anything."""

    name = "memory_proposal"
    phase = HookPhase.POST

    def run(self, ctx: HookContext) -> HookResult:
        """Propose entries from the confirmed answers and preferences of the run."""
        store = ctx.services.memory
        if store is None:
            return passed()
        result = build_proposals(ctx.state, store, ctx.config.memory)
        ctx.data["memory_proposals"] = result.proposals
        findings = [
            Finding(code=f.code, message=f.message, severity=Severity.WARNING, location=f.location)
            for f in result.refused
        ]
        path = ctx.run_path
        if path is not None:
            self._write(path, ctx, result.proposals)
        return HookResult(findings=findings)

    @staticmethod
    def _write(path: Path, ctx: HookContext, proposals: list[Proposal]) -> None:
        records = [
            {"id": p.id, "action": p.action.value, "type": p.entry.type.value, "reason": p.reason}
            for p in proposals
        ]
        body = {"ts": now_iso(), "run_id": ctx.run_id, "proposals": records}
        (path / "memory_proposals.json").write_text(
            json.dumps(body, sort_keys=True), encoding="utf-8"
        )
