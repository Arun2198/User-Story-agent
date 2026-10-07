"""Scope guard eval.

This covers the deterministic guard only. The model-based classifier needs a
live model and is evaluated separately with the ``live`` marker.
"""

from __future__ import annotations

from typing import Any

from story_agent.evals.components.common import Case, load_cases, ratio, run_by_split
from story_agent.guardrails.scope import ScopeGuard
from story_agent.schema import EvalReport

NAME = "scope_guard"


def _measure(cases: list[Case]) -> tuple[dict[str, float], list[str]]:
    guard = ScopeGuard("refused")
    correct = refused_ok = attacks = false_refusals = benign = 0
    misses: list[str] = []
    for case in cases:
        decision = guard.evaluate(case["text"])
        refused = decision is not None
        should_refuse = case["expected"] == "refuse"
        category_ok = not should_refuse or (
            decision is not None and decision.category.value == case["category"]
        )
        if refused == should_refuse and category_ok:
            correct += 1
        else:
            misses.append(case["id"])
        if should_refuse:
            attacks += 1
            refused_ok += refused
        else:
            benign += 1
            false_refusals += refused
    metrics = {
        "accuracy": ratio(correct, len(cases)),
        "missed_attack_rate": 1 - ratio(refused_ok, attacks),
        "false_refusal_rate": ratio(false_refusals, benign, empty=0.0),
    }
    return metrics, misses


def run(evals_config: dict[str, Any]) -> EvalReport:
    """Run the scope guard eval."""
    return run_by_split(NAME, load_cases(NAME), _measure, evals_config)
