"""Injection eval: catch rate on planted attacks and false positives on benign text."""

from __future__ import annotations

from typing import Any

from story_agent.evals.components.common import Case, load_cases, ratio, run_by_split
from story_agent.guardrails.injection import InjectionDetector
from story_agent.schema import EvalReport

NAME = "injection"


def _measure(cases: list[Case]) -> tuple[dict[str, float], list[str]]:
    detector = InjectionDetector()
    caught = attacks = flagged_benign = benign = 0
    misses: list[str] = []
    for case in cases:
        flagged = detector.scan(case["text"]).flagged
        if case["attack"]:
            attacks += 1
            caught += flagged
        else:
            benign += 1
            flagged_benign += flagged
        if flagged != case["attack"]:
            misses.append(case["id"])
    metrics = {
        "catch_rate": ratio(caught, attacks),
        "false_positive_rate": ratio(flagged_benign, benign, empty=0.0),
    }
    return metrics, misses


def run(evals_config: dict[str, Any]) -> EvalReport:
    """Run the injection eval."""
    return run_by_split(NAME, load_cases(NAME), _measure, evals_config)
