"""Run eval suites, compare with baselines, enforce thresholds and set the exit code."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from story_agent.config import AppConfig, get_api_key, load_config
from story_agent.discovery.packs import load_packs
from story_agent.evals.app.run import (
    TransportFactory,
    offline_factory,
    run_app,
    run_memory,
    run_stability,
)
from story_agent.evals.cases import EvalCase, load_cases_dir
from story_agent.evals.components import (
    domain_detection,
    drafting,
    injection,
    redaction,
    scope_guard,
)
from story_agent.evals.components import memory as memory_components
from story_agent.evals.report import SuiteResult
from story_agent.llm import LLMClient, StructuredClient
from story_agent.nvidia import NvidiaTransport
from story_agent.schema import EvalReport

COMPONENTS = ("redaction", "injection", "scope_guard", "domain_detection", "memory", "drafting")
PACKAGE_BASELINES = Path(__file__).parent / "baselines"
VOLATILE_METRICS = frozenset({"latency_s"})  # wall-clock time differs on every run


@dataclass
class SuiteOptions:
    """What to run."""

    component: str | None = None
    app: bool = False
    stability: int | None = None
    memory: bool = False
    live: bool = False
    case_ids: list[str] | None = None
    cases_dir: Path | None = None
    baseline_dir: Path | None = None
    update_baseline: bool = False
    config_dir: Path = field(default_factory=lambda: Path("config"))
    prompts_dir: Path | None = None


def run_components(evals_config: dict[str, Any], config_dir: Path, which: str) -> list[EvalReport]:
    """Run one component eval, or all of them with ``which == 'all'``."""
    if which not in {*COMPONENTS, "all"}:
        raise ValueError(f"unknown component {which}; choose from {', '.join(COMPONENTS)} or all")
    chosen = COMPONENTS if which == "all" else (which,)
    reports: list[EvalReport] = []
    for name in chosen:
        if name == "redaction":
            reports.append(redaction.run(evals_config))
        elif name == "injection":
            reports.append(injection.run(evals_config))
        elif name == "scope_guard":
            reports.append(scope_guard.run(evals_config))
        elif name == "domain_detection":
            reports.append(domain_detection.run(evals_config, config_dir))
        elif name == "memory":
            reports.extend(memory_components.run_all(evals_config, config_dir))
        else:
            reports.extend(drafting.run_all(evals_config, config_dir))
    return reports


def select_cases(options: SuiteOptions) -> list[EvalCase]:
    """Load cases and apply the ``--cases`` filter."""
    cases = load_cases_dir(options.cases_dir)
    if options.case_ids:
        wanted = set(options.case_ids)
        unknown = wanted - {c.id for c in cases}
        if unknown:
            raise ValueError(f"unknown case ids: {', '.join(sorted(unknown))}")
        cases = [c for c in cases if c.id in wanted]
    return cases


def _live_parts(app: AppConfig) -> tuple[TransportFactory, LLMClient]:
    transport = NvidiaTransport(get_api_key(app.models.api_key_env), app.models)
    return (lambda _case, _index: transport), StructuredClient(transport, app.models)


# ---- baselines ---------------------------------------------------------------


def baseline_path(directory: Path, mode: str, name: str) -> Path:
    """Return the baseline file for a report."""
    return directory / mode / f"{name}.json"


def load_baseline(directory: Path, mode: str, name: str) -> dict[str, float] | None:
    """Read a baseline's metrics, or None when there is none."""
    path = baseline_path(directory, mode, name)
    if not path.exists():
        return None
    return {k: float(v) for k, v in json.loads(path.read_text(encoding="utf-8"))["metrics"].items()}


def save_baseline(directory: Path, mode: str, report: EvalReport) -> None:
    """Write a report's metrics as its baseline."""
    path = baseline_path(directory, mode, report.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    metrics = {
        k: round(v, 4) for k, v in sorted(report.metrics.items()) if k not in VOLATILE_METRICS
    }
    path.write_text(
        json.dumps({"name": report.name, "metrics": metrics}, indent=2) + "\n", encoding="utf-8"
    )


def compare(
    report: EvalReport,
    baseline: dict[str, float],
    spec: dict[str, dict[str, Any]],
    tolerance: float,
) -> list[str]:
    """Return a message for each thresholded metric worse than baseline by more than tolerance."""
    messages: list[str] = []
    for metric, bounds in spec.items():
        if metric not in report.metrics or metric not in baseline:
            continue
        now, before = report.metrics[metric], baseline[metric]
        if "min" in bounds and now < before - tolerance:
            messages.append(f"{report.name}.{metric}: {now:.4g} is below baseline {before:.4g}")
        if "max" in bounds and now > before + tolerance:
            messages.append(f"{report.name}.{metric}: {now:.4g} is above baseline {before:.4g}")
    return messages


def _spec_for(
    evals_config: dict[str, Any], report: EvalReport, live: bool
) -> dict[str, dict[str, Any]]:
    thresholds = evals_config.get("thresholds", {})
    if live and f"{report.name}_live" in thresholds:
        return dict(thresholds[f"{report.name}_live"])
    return dict(thresholds.get(report.name, {}))


# ---- the suite ---------------------------------------------------------------


def run_suite(options: SuiteOptions) -> SuiteResult:
    """Run what ``options`` asks for and build the result. With no flags, run everything offline."""
    app = load_config(options.config_dir)
    evals_config = app.evals
    prompts_dir = options.prompts_dir or options.config_dir.parent / "prompts"
    packs = load_packs(options.config_dir)
    mode = "live" if options.live else "offline"
    nothing_chosen = not (options.component or options.app or options.stability or options.memory)
    reports: list[EvalReport] = []
    cases: list[EvalCase] = []
    if options.component or nothing_chosen:
        reports.extend(run_components(evals_config, options.config_dir, options.component or "all"))
    if options.app or options.stability or options.memory or nothing_chosen:
        cases = select_cases(options)
        judge: LLMClient | None = None
        if options.live:
            factory, judge = _live_parts(app)
        else:
            factory = offline_factory(packs)
        if options.app or nothing_chosen:
            reports.append(
                run_app(app, packs, prompts_dir, cases, factory, evals_config, options.live, judge)
            )
        if options.memory or nothing_chosen:
            reports.append(
                run_memory(app, packs, prompts_dir, cases, factory, evals_config, options.live)
            )
        if options.stability:
            reports.append(
                run_stability(
                    app,
                    packs,
                    prompts_dir,
                    cases,
                    factory,
                    evals_config,
                    options.stability,
                    options.live,
                )
            )
    result = SuiteResult(reports, mode, cases=len(cases))
    baseline_dir = options.baseline_dir or PACKAGE_BASELINES
    tolerance = float(evals_config.get("baseline", {}).get("tolerance", 0.02))
    for report in reports:
        result.hard_failures.extend(
            f"{report.name}: {f}" for f in report.details.get("hard_failures", [])
        )
        baseline = load_baseline(baseline_dir, mode, report.name)
        if baseline is not None:
            result.regressions.extend(
                compare(report, baseline, _spec_for(evals_config, report, options.live), tolerance)
            )
    if options.update_baseline:
        for report in reports:
            save_baseline(baseline_dir, mode, report)
        result.baseline_updated = True
    return result
