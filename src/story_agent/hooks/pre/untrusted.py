"""Pre-hooks that clean untrusted text before it reaches a prompt."""

from __future__ import annotations

import json
import os

from story_agent.guardrails.injection import QuarantinedItem
from story_agent.hooks.base import (
    HookContext,
    modified,
    passed,
    sync_state_text,
    write_json_line,
)
from story_agent.schema import Finding, HookPhase, HookResult, Severity


class RedactHook:
    """Replaces PII and secrets in every untrusted text with placeholders."""

    name = "redact"
    phase = HookPhase.PRE

    def run(self, ctx: HookContext) -> HookResult:
        """Redact ``ctx.data['untrusted']`` in place and save the local mapping."""
        redactor = ctx.services.redactor
        untrusted: dict[str, str] = ctx.data.get("untrusted", {})
        changed = 0
        for key, text in untrusted.items():
            clean = redactor.redact(text)
            if clean != text:
                untrusted[key] = clean
                changed += 1
            sync_state_text(ctx, key, untrusted[key])
        if not changed:
            return passed()
        self._save_mapping(ctx)
        info = Finding(
            code="PII_REDACTED",
            message=f"redacted sensitive values in {changed} text(s)",
            severity=Severity.INFO,
        )
        return modified([info])

    @staticmethod
    def _save_mapping(ctx: HookContext) -> None:
        path = ctx.run_path
        if path is None:
            return
        target = path / "redaction_map.json"
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(ctx.services.redactor.mapping, handle, sort_keys=True)


class InjectionScanHook:
    """Quarantines instruction-like, hidden or encoded text and reports it."""

    name = "injection_scan"
    phase = HookPhase.PRE

    def run(self, ctx: HookContext) -> HookResult:
        """Scan every untrusted text, replacing flagged sentences with a marker."""
        untrusted: dict[str, str] = ctx.data.get("untrusted", {})
        findings: list[Finding] = []
        for key, text in untrusted.items():
            result = ctx.services.injection.quarantine(text)
            if result.text == text and not result.findings:
                continue
            untrusted[key] = result.text
            sync_state_text(ctx, key, result.text)
            for finding in result.findings:
                located = finding.model_copy(
                    update={"location": f"{key}:{finding.location or finding.code}"}
                )
                findings.append(located)
            self._persist(ctx, key, result.items)
        if not findings:
            return passed()
        known = {(f.code, f.location) for f in ctx.state.quarantined}
        ctx.state.quarantined.extend(f for f in findings if (f.code, f.location) not in known)
        return modified(findings)

    @staticmethod
    def _persist(ctx: HookContext, key: str, items: list[QuarantinedItem]) -> None:
        path = ctx.run_path
        if path is None:
            return
        for item in items:
            write_json_line(
                path / "quarantine.jsonl",
                {"source": key, "index": item.index, "codes": list(item.codes), "text": item.text},
            )
