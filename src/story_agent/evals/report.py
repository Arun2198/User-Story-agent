"""JSON and markdown reports for an eval suite."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from story_agent.schema import EvalReport


@dataclass
class SuiteResult:
    """Everything one ``evals run`` produced."""

    reports: list[EvalReport]
    mode: str
    regressions: list[str] = field(default_factory=list)
    hard_failures: list[str] = field(default_factory=list)
    baseline_updated: bool = False
    cases: int = 0

    @property
    def threshold_failures(self) -> list[str]:
        """Threshold failures across reports, prefixed with the report name."""
        return [f"{r.name}: {f}" for r in self.reports for f in r.failures]

    @property
    def exit_code(self) -> int:
        """2 for a hard failure, 1 for any other failure or regression, else 0."""
        if self.hard_failures:
            return 2
        return 1 if self.threshold_failures or self.regressions else 0


MODE_NOTE = {
    "offline": (
        "OFFLINE: the model is a scripted stand-in driven by the cases' gold labels. "
        "These results test plumbing, guardrails, the simulator and the metrics. "
        "They say nothing about model quality; run with --live for that."
    ),
    "live": "LIVE: a real model produced these results.",
}


def to_json(result: SuiteResult) -> str:
    """Return the suite as JSON. Reports hold metrics only; no scenario text."""
    body: dict[str, Any] = {
        "mode": result.mode,
        "cases": result.cases,
        "exit_code": result.exit_code,
        "regressions": result.regressions,
        "hard_failures": result.hard_failures,
        "threshold_failures": result.threshold_failures,
        "reports": [r.model_dump(mode="json", exclude={"created_at"}) for r in result.reports],
    }
    return json.dumps(body, indent=2, sort_keys=True)


def _fmt(value: float) -> str:
    return f"{value:.4g}"


def to_markdown(result: SuiteResult) -> str:
    """Return the suite as a markdown report."""
    lines = ["# Eval report", "", f"Mode: **{result.mode}**. {MODE_NOTE.get(result.mode, '')}", ""]
    status = "PASS" if result.exit_code == 0 else ("HARD FAIL" if result.exit_code == 2 else "FAIL")
    lines += [f"Result: **{status}** (exit code {result.exit_code}), {result.cases} cases", ""]
    if result.hard_failures:
        lines += ["## Hard failures", *[f"- {f}" for f in result.hard_failures], ""]
    if result.regressions:
        lines += ["## Regressions against baseline", *[f"- {r}" for r in result.regressions], ""]
    if result.threshold_failures:
        lines += ["## Threshold failures", *[f"- {f}" for f in result.threshold_failures], ""]
    for report in result.reports:
        mark = "pass" if report.thresholds_met else "FAIL"
        lines += [
            f"## {report.name} ({report.kind}): {mark}",
            "",
            "| metric | value |",
            "|---|---|",
        ]
        for name, value in sorted(report.metrics.items()):
            if "." not in name:
                lines.append(f"| {name} | {_fmt(value)} |")
        errors = report.details.get("errors")
        if errors:
            lines += [
                "",
                "Run errors:",
                *[f"- {cid}: {msg}" for cid, msg in sorted(errors.items())],
            ]
        missed = report.details.get("missed_case_ids")
        if missed:
            lines += ["", f"Cases not matching their labels: {', '.join(missed)}"]
        lines.append("")
    if result.baseline_updated:
        lines.append("Baselines were updated from this run.")
    return "\n".join(lines) + "\n"
