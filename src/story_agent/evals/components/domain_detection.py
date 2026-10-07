"""Domain detection eval for the deterministic keyword detector."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from story_agent.discovery.packs import PackSet, load_packs
from story_agent.evals.components.common import Case, load_cases, ratio, run_by_split
from story_agent.schema import EvalReport

NAME = "domain_detection"


def _measure(cases: list[Case], packs: PackSet) -> tuple[dict[str, float], list[str]]:
    domain_ok = sub_ok = sub_total = 0
    sp_tp = sp_fp = sp_fn = 0
    misses: list[str] = []
    for case in cases:
        got = packs.detect(case["text"])
        good = got.domain == case["domain"]
        domain_ok += good
        if case["subdomain"] is not None:
            sub_total += 1
            sub_ok += got.subdomain == case["subdomain"]
            good = good and got.subdomain == case["subdomain"]
        want_sp, got_sp = set(case["subpacks"]), set(got.subpacks)
        sp_tp += len(want_sp & got_sp)
        sp_fp += len(got_sp - want_sp)
        sp_fn += len(want_sp - got_sp)
        good = good and want_sp == got_sp
        if not good:
            misses.append(case["id"])
    metrics = {
        "domain_accuracy": ratio(domain_ok, len(cases)),
        "subdomain_accuracy": ratio(sub_ok, sub_total),
        "subpack_precision": ratio(sp_tp, sp_tp + sp_fp),
        "subpack_recall": ratio(sp_tp, sp_tp + sp_fn),
    }
    return metrics, misses


def run(evals_config: dict[str, Any], config_dir: Path) -> EvalReport:
    """Run the domain detection eval against the packs in ``config_dir``."""
    packs = load_packs(config_dir)
    return run_by_split(
        NAME, load_cases(NAME), lambda subset: _measure(subset, packs), evals_config
    )
