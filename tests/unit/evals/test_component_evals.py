from story_agent.config import AppConfig
from story_agent.evals.components import injection, redaction, scope_guard
from story_agent.evals.components.common import check_thresholds, load_cases, ratio, split_cases


def test_redaction_eval_meets_thresholds(app_config: AppConfig) -> None:
    report = redaction.run(app_config.evals)
    assert report.thresholds_met, report.failures


def test_injection_eval_meets_thresholds(app_config: AppConfig) -> None:
    report = injection.run(app_config.evals)
    assert report.thresholds_met, report.failures


def test_scope_guard_eval_meets_thresholds(app_config: AppConfig) -> None:
    report = scope_guard.run(app_config.evals)
    assert report.thresholds_met, report.failures


def test_datasets_have_both_splits_and_both_classes() -> None:
    for name in ("redaction", "injection", "scope_guard"):
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
