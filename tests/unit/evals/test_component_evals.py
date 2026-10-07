import pytest

from story_agent.config import AppConfig
from story_agent.evals.components import (
    domain_detection,
    drafting,
    injection,
    redaction,
    scope_guard,
)
from story_agent.evals.components.common import check_thresholds, load_cases, ratio, split_cases
from story_agent.pipeline import critique


def test_redaction_eval_meets_thresholds(app_config: AppConfig) -> None:
    report = redaction.run(app_config.evals)
    assert report.thresholds_met, report.failures


def test_injection_eval_meets_thresholds(app_config: AppConfig) -> None:
    report = injection.run(app_config.evals)
    assert report.thresholds_met, report.failures


def test_scope_guard_eval_meets_thresholds(app_config: AppConfig) -> None:
    report = scope_guard.run(app_config.evals)
    assert report.thresholds_met, report.failures


def test_domain_detection_eval_meets_thresholds(app_config: AppConfig) -> None:
    report = domain_detection.run(app_config.evals, app_config.config_dir)
    assert report.thresholds_met, report.failures
    assert "domain_accuracy" in report.metrics


def test_datasets_have_both_splits_and_both_classes() -> None:
    for name in ("redaction", "injection", "scope_guard", "domain_detection"):
        cases = load_cases(name)
        assert split_cases(cases, "dev")
        assert split_cases(cases, "holdout")
        assert len(split_cases(cases, "all")) == len(cases)
    assert {c["attack"] for c in load_cases("injection")} == {True, False}
    assert any(not c["entities"] for c in load_cases("redaction"))


def test_threshold_failures_are_reported() -> None:
    spec = {"recall": {"min": 0.9}, "fp": {"max": 0.1}, "gone": {"min": 0.5}}
    failures = check_thresholds({"recall": 0.5, "fp": 0.3}, spec)
    assert len(failures) == 3
    assert check_thresholds({"recall": 0.95, "fp": 0.0, "gone": 1}, spec) == []
    assert check_thresholds({"holdout.recall": 0.1}, {"recall": {"min": 0.9}}, "holdout.")


def test_ratio_handles_empty() -> None:
    assert ratio(1, 0) == 1.0
    assert ratio(1, 0, empty=0.0) == 0.0
    assert ratio(1, 4) == 0.25


def test_a_broken_config_fails_the_gate(app_config: AppConfig) -> None:
    strict = {"enforce": ["all"], "thresholds": {"injection": {"catch_rate": {"min": 1.5}}}}
    report = injection.run(strict)
    assert not report.thresholds_met
    assert report.failures


def test_drafting_evals_meet_thresholds(app_config: AppConfig) -> None:
    reports = drafting.run_all(app_config.evals, app_config.config_dir)
    assert [r.name for r in reports] == ["critic_checks", "criteria_checks"]
    for report in reports:
        assert report.thresholds_met, (report.name, report.failures)


def test_the_critic_eval_fails_if_duplicate_detection_is_removed(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(critique, "find_duplicates", lambda *_a, **_k: [])
    report = drafting.critic_checks(app_config, app_config.evals, app_config.config_dir)
    assert not report.thresholds_met
    assert report.metrics["recall"] < 0.9


def test_the_criteria_eval_fails_if_the_vague_list_is_empty(app_config: AppConfig) -> None:
    broken = app_config.model_copy(
        update={"standards": {**app_config.standards, "vague_terms": []}}
    )
    report = drafting.criteria_checks(broken, app_config.evals)
    assert not report.thresholds_met
    assert report.metrics["vague_recall"] == 0
