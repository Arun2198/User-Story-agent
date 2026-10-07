"""Shared helpers for component evals."""

from __future__ import annotations

import json
from collections.abc import Callable
from importlib import resources
from typing import Any

from story_agent.schema import EvalReport

Case = dict[str, Any]
SPLITS = ("all", "dev", "holdout")


def load_cases(name: str) -> list[Case]:
    """Read ``datasets/components/<name>.jsonl`` from the package."""
    path = resources.files("story_agent.evals").joinpath(f"datasets/components/{name}.jsonl")
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def split_cases(cases: list[Case], split: str) -> list[Case]:
    """Return the cases in ``split`` (``all`` keeps everything)."""
    return cases if split == "all" else [c for c in cases if c.get("split") == split]


def ratio(numerator: float, denominator: float, empty: float = 1.0) -> float:
    """Divide, returning ``empty`` when the denominator is zero."""
    return numerator / denominator if denominator else empty


def check_thresholds(
    metrics: dict[str, float], spec: dict[str, dict[str, Any]], prefix: str = ""
) -> list[str]:
    """Return one failure message per metric outside its min or max."""
    failures: list[str] = []
    for metric, bounds in spec.items():
        value = metrics.get(f"{prefix}{metric}")
        if value is None:
            failures.append(f"{prefix}{metric}: missing")
            continue
        if "min" in bounds and value < bounds["min"]:
            failures.append(f"{prefix}{metric}={value:.3f} below min {bounds['min']}")
        if "max" in bounds and value > bounds["max"]:
            failures.append(f"{prefix}{metric}={value:.3f} above max {bounds['max']}")
    return failures


def run_by_split(
    name: str,
    cases: list[Case],
    measure: Callable[[list[Case]], tuple[dict[str, float], list[str]]],
    evals_config: dict[str, Any],
) -> EvalReport:
    """Measure every split and enforce thresholds on the configured ones."""
    metrics: dict[str, float] = {}
    misses: list[str] = []
    for split in SPLITS:
        subset = split_cases(cases, split)
        if not subset:
            continue
        split_metrics, split_misses = measure(subset)
        prefix = "" if split == "all" else f"{split}."
        metrics.update({f"{prefix}{k}": v for k, v in split_metrics.items()})
        if split == "all":
            misses = split_misses
    spec = evals_config.get("thresholds", {}).get(name, {})
    failures: list[str] = []
    for split in evals_config.get("enforce", ["all"]):
        prefix = "" if split == "all" else f"{split}."
        failures.extend(check_thresholds(metrics, spec, prefix))
    return EvalReport(
        name=name,
        kind="component",
        metrics=metrics,
        thresholds_met=not failures,
        failures=failures,
        details={"missed_case_ids": misses},
    )


def finalize(
    name: str,
    metrics: dict[str, float],
    evals_config: dict[str, Any],
    details: dict[str, Any] | None = None,
) -> EvalReport:
    """Build a report for an eval without splits. Metrics marked ``hard`` are listed apart."""
    spec = evals_config.get("thresholds", {}).get(name, {})
    failures = check_thresholds(metrics, spec)
    hard = [
        f for f in failures if any(f.startswith(f"{m}=") and b.get("hard") for m, b in spec.items())
    ]
    out_details = dict(details or {})
    out_details["hard_failures"] = hard
    return EvalReport(
        name=name,
        kind="component",
        metrics=metrics,
        thresholds_met=not failures,
        failures=failures,
        details=out_details,
    )
