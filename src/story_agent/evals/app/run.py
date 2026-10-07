"""Application-level evals: end to end, stability and memory.

All three drive the same run flow with the simulated user. Offline they use the gold
model, so they test plumbing, guardrails and metrics; live they use a real model.
"""

from __future__ import annotations

import statistics
import tempfile
from collections.abc import Callable
from datetime import timedelta
from itertools import combinations
from pathlib import Path
from typing import Any

from story_agent.config import AppConfig
from story_agent.discovery.packs import PackSet
from story_agent.evals.app.driver import RunRecord, run_case
from story_agent.evals.app.gold_model import GoldModel
from story_agent.evals.app.judge import JudgeScores, judge_run
from story_agent.evals.app.metrics import aggregate, case_metrics
from story_agent.evals.cases import EvalCase
from story_agent.evals.components.common import check_thresholds
from story_agent.evals.simulator import SimulatedUser
from story_agent.guardrails.injection import InjectionDetector
from story_agent.guardrails.redaction import Redactor
from story_agent.llm import LLMClient, Transport
from story_agent.memory.conflicts import detect_conflicts
from story_agent.memory.proposals import ProposalAction
from story_agent.memory.store import SqliteMemoryStore
from story_agent.schema import EvalReport, utcnow

TransportFactory = Callable[[EvalCase, int], Transport]


def offline_factory(
    packs: PackSet, degrade: frozenset[str] = frozenset(), noisy: bool = False
) -> TransportFactory:
    """Return a factory of gold-model transports. ``noisy`` varies behaviour per run index."""
    return lambda case, index: GoldModel(case, packs, degrade, index if noisy else None)


def _spec(evals_config: dict[str, Any], name: str, live: bool) -> dict[str, dict[str, Any]]:
    thresholds = evals_config.get("thresholds", {})
    live_spec = thresholds.get(f"{name}_live") if live else None
    spec: dict[str, dict[str, Any]] = live_spec or thresholds.get(name, {})
    return spec


def _report(
    name: str, metrics: dict[str, float], spec: dict[str, dict[str, Any]], details: dict[str, Any]
) -> EvalReport:
    failures = check_thresholds(metrics, spec)
    hard = [
        f for f in failures if any(f.startswith(f"{m}=") and b.get("hard") for m, b in spec.items())
    ]
    details["hard_failures"] = hard
    return EvalReport(
        name=name,
        kind="app",
        metrics=metrics,
        thresholds_met=not failures,
        failures=failures,
        details=details,
    )


def run_app(  # noqa: PLR0913, PLR0917  (an eval run needs these inputs)
    app: AppConfig,
    packs: PackSet,
    prompts_dir: Path,
    cases: list[EvalCase],
    factory: TransportFactory,
    evals_config: dict[str, Any],
    live: bool = False,
    judge_client: LLMClient | None = None,
) -> EvalReport:
    """Run every case once and aggregate metrics. The judge runs only when a client is given."""
    rows: list[dict[str, float]] = []
    per_case: dict[str, dict[str, float]] = {}
    errors: dict[str, str] = {}
    for case in cases:
        record = run_case(case, app, packs, prompts_dir, factory(case, 0))
        scores: JudgeScores | None = None
        if judge_client is not None and record.state is not None and not record.error:
            scores = judge_run(judge_client, app, prompts_dir, record.state)
        metrics = case_metrics(record, app, packs, scores)
        rows.append(metrics)
        per_case[case.id] = metrics
        if record.error:
            errors[case.id] = record.error
        if record.refused:
            errors[case.id] = "refused by scope"
    details = {
        "per_case": per_case,
        "errors": errors,
        "judge": judge_client is not None,
        "cases": len(cases),
    }
    return _report("app", aggregate(rows), _spec(evals_config, "app", live), details)


# ---- stability ----------------------------------------------------------------


def _cv(values: list[float]) -> float:
    mean = statistics.fmean(values)
    return statistics.pstdev(values) / mean if mean else 0.0


def _jaccard(sets: list[frozenset[Any]]) -> float:
    pairs = list(combinations(sets, 2))
    if not pairs:
        return 1.0
    return statistics.fmean(len(a & b) / len(a | b) if a | b else 1.0 for a, b in pairs)


def _story_shapes(record: RunRecord) -> frozenset[frozenset[str]]:
    if record.state is None:
        return frozenset()
    by_id = {r.id: r.category for r in record.state.requirements}
    return frozenset(
        frozenset(by_id[r] for r in s.requirement_ids if r in by_id) for s in record.state.stories
    )


def run_stability(  # noqa: PLR0913, PLR0917  (an eval run needs these inputs)
    app: AppConfig,
    packs: PackSet,
    prompts_dir: Path,
    cases: list[EvalCase],
    factory: TransportFactory,
    evals_config: dict[str, Any],
    runs: int,
    live: bool = False,
) -> EvalReport:
    """Run each case ``runs`` times and report variance in items, questions and stories."""
    if runs < 2:
        raise ValueError("stability needs at least 2 runs")
    rows: list[dict[str, float]] = []
    per_case: dict[str, dict[str, float]] = {}
    for case in cases:
        records = [run_case(case, app, packs, prompts_dir, factory(case, i)) for i in range(runs)]
        ok = [r for r in records if r.state is not None and not r.error]
        stated = [
            float(
                sum(
                    1
                    for i in (r.discovery_snapshot.items if r.discovery_snapshot else [])
                    if i.status.value == "stated"
                )
            )
            for r in ok
        ]
        qcount = [float(len(r.questions)) for r in ok]
        scount = [float(len(r.state.stories)) for r in ok if r.state]
        qsets = [frozenset(q.category for q in r.questions) for r in ok]
        shapes = [_story_shapes(r) for r in ok]
        metrics = {
            "items_cv": _cv(stated) if stated else 0.0,
            "questions_cv": _cv(qcount) if qcount else 0.0,
            "stories_cv": _cv(scount) if scount else 0.0,
            "question_set_jaccard": _jaccard(qsets),
            "story_set_jaccard": _jaccard(shapes),
            "failed_runs": float(len(records) - len(ok)),
        }
        per_case[case.id] = metrics
        rows.append(metrics)
    aggregated = aggregate(rows)
    aggregated["failed_runs"] = sum(r["failed_runs"] for r in rows)
    details = {"per_case": per_case, "runs": runs, "cases": len(cases)}
    return _report("stability", aggregated, _spec(evals_config, "stability", live), details)


# ---- memory (end to end) ------------------------------------------------------


def _age(store: SqliteMemoryStore, category: str) -> None:
    for entry in store.list_entries():
        if f"category:{category}" in entry.tags:
            old = utcnow() - timedelta(days=entry.ttl_days + 30)
            store.put(entry.model_copy(update={"last_confirmed_at": old}))


def _requirement_text(record: RunRecord, category: str) -> str:
    return " ".join(
        r.text
        for r in (record.state.requirements if record.state else [])
        if r.category == category
    )


def run_memory(  # noqa: PLR0913, PLR0917  (an eval run needs these inputs)
    app: AppConfig,
    packs: PackSet,
    prompts_dir: Path,
    cases: list[EvalCase],
    factory: TransportFactory,
    evals_config: dict[str, Any],
    live: bool = False,
) -> EvalReport:
    """Run each case with memory off, seed memory from it, then run again with memory on."""
    rows: list[dict[str, float]] = []
    per_case: dict[str, dict[str, float]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for original in cases:
            # One workspace per case, so cases cannot see each other's memory.
            case = original.model_copy(update={"workspace": f"{original.workspace}-{original.id}"})
            store = SqliteMemoryStore(root, case.workspace, app.memory)
            first = run_case(
                case, app, packs, prompts_dir, factory(case, 0), store=store, use_recall=False
            )
            for category in case.memory.stale:
                _age(store, category)
            key2 = {**case.answer_key, **case.memory.changed}
            second = run_case(
                case,
                app,
                packs,
                prompts_dir,
                factory(case, 1),
                SimulatedUser(case, key2),
                store,
                "eval-run-2",
            )
            other_case = case.model_copy(update={"workspace": "other-" + case.workspace[:50]})
            other = SqliteMemoryStore(root, other_case.workspace, app.memory)
            third = run_case(
                other_case,
                app,
                packs,
                prompts_dir,
                factory(other_case, 2),
                store=other,
                run_id="eval-run-3",
            )
            asked2 = {q.category: q for q in second.questions}
            changed_asked = [
                c for c in case.memory.changed if c in asked2 and asked2[c].remembered_default
            ]
            conflicts = (
                {c.category for c in detect_conflicts(second.state)} if second.state else set()
            )
            updates = {
                p.entry.tags[0].removeprefix("category:")
                for p in second.proposals
                if p.action is ProposalAction.UPDATE and p.entry.tags
            }
            used_new = [
                c
                for c in changed_asked
                if case.memory.changed[c].split()[0].casefold()
                in _requirement_text(second, c).casefold()
                and _requirement_text(second, c)
                .casefold()
                .count(case.answer_key.get(c, "\x00").casefold())
                == 0
            ]
            handled = [
                c for c in changed_asked if c in conflicts and c in updates and c in used_new
            ]
            stale_changed = [c for c in changed_asked if c in case.memory.stale]
            misapplied = [
                c
                for c in stale_changed
                if case.answer_key.get(c, "\x00").casefold()
                in _requirement_text(second, c).casefold()
            ]
            stale_asked = [
                c for c in case.memory.stale if c in asked2 and asked2[c].remembered_default
            ]
            flagged = [
                c
                for c in stale_asked
                if (ref := asked2[c].remembered_default) is not None and ref.stale
            ]
            typed1 = first.user.typed if first.user else 0
            typed2 = second.user.typed if second.user else 0
            stored = "\n".join(f"{e.content} {' '.join(e.tags)}" for e in store.list_entries())
            persisted = Redactor().count(stored) + len(InjectionDetector().scan(stored).hits)
            leaks = len(third.recalled_ids) + sum(
                1 for q in third.questions if q.remembered_default
            )
            leaks += sum(1 for e in other.list_entries() if e.workspace != other.workspace)
            with_default = sum(1 for q in second.questions if q.remembered_default)
            metrics = {
                "question_count_reduction": (typed1 - typed2) / typed1 if typed1 else 0.0,
                "default_offer_rate": with_default / len(second.questions)
                if second.questions
                else 0.0,
                "contradiction_handling_accuracy": len(handled) / len(changed_asked)
                if changed_asked
                else 1.0,
                "false_conflicts": float(len(conflicts - set(case.memory.changed))),
                "stale_misapplication_rate": len(misapplied) / len(stale_changed)
                if stale_changed
                else 0.0,
                "stale_flag_rate": len(flagged) / len(stale_asked) if stale_asked else 1.0,
                "pii_persisted": float(persisted),
                "cross_workspace_leaks": float(leaks),
                "run_failures": float(sum(bool(r.error) for r in (first, second, third))),
            }
            per_case[case.id] = metrics
            rows.append(metrics)
            store.close()
            other.close()
    aggregated = aggregate(rows)
    for count_name in ("pii_persisted", "cross_workspace_leaks", "run_failures", "false_conflicts"):
        aggregated[count_name] = sum(r[count_name] for r in rows)
    return _report(
        "memory",
        aggregated,
        _spec(evals_config, "memory", live),
        {"per_case": per_case, "cases": len(cases)},
    )
