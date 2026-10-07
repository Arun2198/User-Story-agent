"""Redaction eval: entity-level precision and recall on seeded PII and secrets."""

from __future__ import annotations

from collections import Counter
from typing import Any

from story_agent.evals.components.common import Case, load_cases, ratio, run_by_split
from story_agent.guardrails.redaction import find_spans
from story_agent.schema import EvalReport

NAME = "redaction"


def _measure(cases: list[Case]) -> tuple[dict[str, float], list[str]]:
    tp = fp = fn = 0
    clean = clean_hit = 0
    misses: list[str] = []
    for case in cases:
        text = case["text"]
        predicted = Counter((s.label, text[s.start : s.end]) for s in find_spans(text))
        gold = Counter((e["label"], e["value"]) for e in case["entities"])
        hit = sum((predicted & gold).values())
        tp += hit
        fp += sum(predicted.values()) - hit
        fn += sum(gold.values()) - hit
        if not gold:
            clean += 1
            clean_hit += 1 if predicted else 0
        if predicted != gold:
            misses.append(case["id"])
    metrics = {
        "precision": ratio(tp, tp + fp),
        "recall": ratio(tp, tp + fn),
        "clean_false_positive_rate": ratio(clean_hit, clean, empty=0.0),
    }
    return metrics, misses


def run(evals_config: dict[str, Any]) -> EvalReport:
    """Run the redaction eval."""
    return run_by_split(NAME, load_cases(NAME), _measure, evals_config)
