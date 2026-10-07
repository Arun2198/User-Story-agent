"""Evals for the deterministic checks in drafting: critic checks and criteria checks.

These need no model. They measure the rules that decide whether a story set can go
to review (duplicates, coverage, size, readiness, cycles) and whether a criterion is
well formed, grounded and testable. Model-written drafts and criteria are measured
in the application-level evals.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from story_agent.config import AppConfig, load_config
from story_agent.deps import StageDeps
from story_agent.discovery.packs import load_packs
from story_agent.evals.components.common import Case, load_cases, ratio, run_by_split
from story_agent.llm import LLMError
from story_agent.pipeline.criteria import CriterionDraft, convert_drafts
from story_agent.pipeline.critique import deterministic_findings
from story_agent.schema import (
    AcceptanceCriterion,
    EvalReport,
    Provenance,
    ProvenanceType,
    Requirement,
    RunState,
    Scenario,
    Severity,
    Story,
)

EXCERPT = "excerpt text for the eval scenario"


class _NoClient:
    """A client that must never be called by deterministic checks."""

    def complete(self, request: Any, schema: Any) -> Any:  # noqa: ANN401, ARG002
        """Refuse to run."""
        raise LLMError("deterministic eval must not call a model")


def _provenance() -> list[Provenance]:
    return [
        Provenance(type=ProvenanceType.SCENARIO_EXCERPT, ref=EXCERPT, element=e)
        for e in ("persona", "want", "benefit")
    ]


def _story(raw: dict[str, Any]) -> Story:
    criteria = [
        AcceptanceCriterion(id=f"{raw['id']}-AC{n:02d}", given=f"g{n}", when=f"w{n}", then=f"t{n}")
        for n in range(raw["criteria"])
    ]
    return Story(
        id=raw["id"],
        epic=raw["epic"],
        title=raw["title"],
        persona="Customer",
        want=raw["want"],
        benefit=raw["benefit"],
        estimate=raw["estimate"],
        dependencies=raw["dependencies"],
        requirement_ids=raw["requirement_ids"],
        acceptance_criteria=criteria,
        provenance=_provenance(),
    )


def _measure_critic(
    cases: list[Case], app: AppConfig, deps: StageDeps
) -> tuple[dict[str, float], list[str]]:
    tp = fp = fn = clean = clean_flagged = 0
    misses: list[str] = []
    for case in cases:
        state = RunState(
            run_id="eval",
            scenario=Scenario(text=EXCERPT),
            redacted_text=EXCERPT,
            requirements=[
                Requirement(id=r, text="t", category="c", provenance=_provenance()[:1])
                for r in case["requirements"]
            ],
        )
        stories = [_story(s) for s in case["stories"]]
        findings = deterministic_findings(deps, state, stories, [])
        got = {f.code for f in findings if f.severity is Severity.ERROR}
        want = set(case["expected"])
        tp += len(got & want)
        fp += len(got - want)
        fn += len(want - got)
        if not want:
            clean += 1
            clean_flagged += bool(got)
        if got != want:
            misses.append(case["id"])
    del app
    metrics = {
        "precision": ratio(tp, tp + fp),
        "recall": ratio(tp, tp + fn),
        "clean_false_positive_rate": ratio(clean_flagged, clean, empty=0.0),
    }
    return metrics, misses


def critic_checks(app: AppConfig, evals_config: dict[str, Any], config_dir: Path) -> EvalReport:
    """Measure the deterministic critique checks on labelled story sets."""
    deps = StageDeps(_NoClient(), app, load_packs(config_dir), config_dir.parent / "prompts")
    return run_by_split(
        "critic_checks",
        load_cases("critic_checks"),
        lambda subset: _measure_critic(subset, app, deps),
        evals_config,
    )


def _classify(case: Case, vague: list[str], story: Story) -> str:
    draft = CriterionDraft.model_construct(
        given=case["given"], when=case["when"], then=case["then"], kind="happy"
    )
    _, findings = convert_drafts([draft], story, set(case["known_numbers"]), vague)
    codes = {f.code for f in findings}
    for code, label in (
        ("CRITERION_MALFORMED", "malformed"),
        ("CRITERION_UNGROUNDED_NUMBER", "ungrounded_number"),
        ("CRITERION_VAGUE", "vague"),
    ):
        if code in codes:
            return label
    return "ok"


def _measure_criteria(cases: list[Case], vague: list[str]) -> tuple[dict[str, float], list[str]]:
    story = _story(
        {
            "id": "AA-0001",
            "epic": "A",
            "title": "t",
            "want": "w",
            "benefit": "b",
            "estimate": 3,
            "dependencies": [],
            "requirement_ids": ["REQ-001"],
            "criteria": 0,
        }
    )
    correct = ok_total = ok_flagged = 0
    hits = {"vague": [0, 0], "ungrounded_number": [0, 0], "malformed": [0, 0]}
    misses: list[str] = []
    for case in cases:
        got = _classify(case, vague, story)
        want = case["expected"]
        correct += got == want
        if got != want:
            misses.append(case["id"])
        if want == "ok":
            ok_total += 1
            ok_flagged += got != "ok"
        else:
            hits[want][1] += 1
            hits[want][0] += got == want
    metrics = {
        "accuracy": ratio(correct, len(cases)),
        "vague_recall": ratio(*hits["vague"]),
        "invented_number_recall": ratio(*hits["ungrounded_number"]),
        "malformed_recall": ratio(*hits["malformed"]),
        "false_flag_rate": ratio(ok_flagged, ok_total, empty=0.0),
    }
    return metrics, misses


def criteria_checks(app: AppConfig, evals_config: dict[str, Any]) -> EvalReport:
    """Measure the criteria conformance and testability checks."""
    vague = list(app.standards.get("vague_terms", []))
    return run_by_split(
        "criteria_checks",
        load_cases("criteria_checks"),
        lambda subset: _measure_criteria(subset, vague),
        evals_config,
    )


def run_all(evals_config: dict[str, Any], config_dir: Path) -> list[EvalReport]:
    """Run both drafting evals."""
    app = load_config(config_dir)
    return [critic_checks(app, evals_config, config_dir), criteria_checks(app, evals_config)]
