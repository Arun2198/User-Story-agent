import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from story_agent.cli import app
from story_agent.config import AppConfig, ConfigError
from story_agent.discovery.packs import PackSet
from story_agent.evals.app.judge import JudgeOutput, judge_run, render_run
from story_agent.evals.cases import EvalCase, GoldStory, load_cases_dir
from story_agent.evals.report import MODE_NOTE, SuiteResult, to_json, to_markdown
from story_agent.evals.runner import (
    COMPONENTS,
    SuiteOptions,
    compare,
    load_baseline,
    run_components,
    run_suite,
    save_baseline,
    select_cases,
)
from story_agent.evals.synth import SEEDS, SynthResult, case_id, render_request, synthesize
from story_agent.fake_llm import FakeTransport
from story_agent.llm import StructuredClient
from story_agent.schema import EvalReport, Finding, RunState, Scenario, Severity

ROOT = Path(__file__).resolve().parents[3]
CASES = load_cases_dir()
runner = CliRunner()


@pytest.fixture(autouse=True)
def _config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORY_AGENT_CONFIG_DIR", str(ROOT / "config"))


def _options(tmp: Path, **kw: Any) -> SuiteOptions:
    return SuiteOptions(config_dir=ROOT / "config", baseline_dir=tmp / "baselines", **kw)


# ---- runner -------------------------------------------------------------------------


def test_components_can_be_selected_and_unknown_ones_are_refused(app_config: AppConfig) -> None:
    assert [r.name for r in run_components(app_config.evals, ROOT / "config", "redaction")] == [
        "redaction"
    ]
    assert {r.name for r in run_components(app_config.evals, ROOT / "config", "memory")} >= {
        "memory_isolation"
    }
    assert len(COMPONENTS) == 6
    with pytest.raises(ValueError, match="unknown component"):
        run_components(app_config.evals, ROOT / "config", "bogus")


def test_cases_filter() -> None:
    chosen = select_cases(SuiteOptions(case_ids=["bk-overdraft", "gn-retail-returns"]))
    assert [c.id for c in chosen] == ["bk-overdraft", "gn-retail-returns"]
    with pytest.raises(ValueError, match="unknown case ids: nope"):
        select_cases(SuiteOptions(case_ids=["nope"]))
    assert len(select_cases(SuiteOptions())) == 14


def test_component_run_exit_codes_and_baselines(tmp_path: Path) -> None:
    first = run_suite(_options(tmp_path, component="injection", update_baseline=True))
    assert first.exit_code == 0
    assert first.baseline_updated
    assert (tmp_path / "baselines" / "offline" / "injection.json").exists()
    assert first.mode == "offline"
    again = run_suite(_options(tmp_path, component="injection"))
    assert again.exit_code == 0
    assert again.regressions == []
    path = tmp_path / "baselines" / "offline" / "injection.json"
    body = json.loads(path.read_text())
    body["metrics"][
        "false_positive_rate"
    ] = -1.0  # a better baseline than reality is impossible: use catch_rate
    body["metrics"]["false_positive_rate"] = 0.0
    body["metrics"]["catch_rate"] = 1.5
    path.write_text(json.dumps(body))
    worse = run_suite(_options(tmp_path, component="injection"))
    assert worse.exit_code == 1
    assert any("catch_rate" in r for r in worse.regressions)


def test_a_threshold_failure_is_exit_1_and_a_hard_failure_is_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("story_agent.guardrails.redaction.Redactor.redact", lambda _s, t: t)
    result = run_suite(_options(tmp_path, app=True, case_ids=["bk-card-dispute"]))
    assert result.exit_code == 2
    assert any("pii_leaks_to_model" in f for f in result.hard_failures)
    soft = SuiteResult(
        [
            EvalReport(
                name="x", kind="component", thresholds_met=False, failures=["m=0 below min 1"]
            )
        ],
        "offline",
    )
    assert soft.exit_code == 1
    assert SuiteResult([], "offline", regressions=["x"]).exit_code == 1
    assert SuiteResult([], "offline").exit_code == 0


def test_app_memory_and_stability_selection(tmp_path: Path) -> None:
    result = run_suite(_options(tmp_path, memory=True, case_ids=["gn-retail-returns"]))
    assert [r.name for r in result.reports] == ["memory"]
    result = run_suite(_options(tmp_path, stability=2, case_ids=["gn-retail-returns"]))
    assert [r.name for r in result.reports] == ["stability"]
    assert result.cases == 1


def test_baseline_helpers(tmp_path: Path) -> None:
    report = EvalReport(name="r", kind="app", metrics={"a": 0.5, "latency_s": 9.0, "b": 2.0})
    assert load_baseline(tmp_path, "offline", "r") is None
    save_baseline(tmp_path, "offline", report)
    assert load_baseline(tmp_path, "offline", "r") == {"a": 0.5, "b": 2.0}
    spec: dict[str, dict[str, Any]] = {
        "a": {"min": 0.1},
        "b": {"max": 5},
        "missing": {"min": 1},
        "unspecced_ok": {},
    }
    better = report.model_copy(update={"metrics": {"a": 0.9, "b": 1.0}})
    assert compare(better, {"a": 0.5, "b": 2.0}, spec, 0.02) == []
    worse = report.model_copy(update={"metrics": {"a": 0.4, "b": 3.0}})
    messages = compare(worse, {"a": 0.5, "b": 2.0}, spec, 0.02)
    assert len(messages) == 2
    assert compare(worse, {"a": 0.5, "b": 2.0}, spec, 1.2) == []


def test_live_mode_needs_a_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        run_suite(_options(tmp_path, app=True, live=True, case_ids=["bk-overdraft"]))


# ---- reports ---------------------------------------------------------------------------


def test_reports_hold_metrics_not_scenario_text(tmp_path: Path) -> None:
    result = run_suite(_options(tmp_path, app=True, case_ids=["bk-card-dispute"]))
    text = to_json(result)
    body = json.loads(text)
    assert body["mode"] == "offline"
    assert body["exit_code"] == result.exit_code
    assert "created_at" not in text
    for planted in CASES[1].planted_pii:
        assert planted.value not in text
    assert "4111" not in text
    markdown = to_markdown(result)
    assert MODE_NOTE["offline"] in markdown
    assert "## app (app)" in markdown
    assert "Result: **PASS**" in markdown


def test_markdown_lists_failures_and_regressions() -> None:
    bad = EvalReport(
        name="x",
        kind="component",
        metrics={"a": 0.1},
        thresholds_met=False,
        failures=["a=0.1 below min 1"],
        details={"missed_case_ids": ["c1"], "errors": {"c9": "boom"}},
    )
    result = SuiteResult([bad], "live", regressions=["x.a: worse"], hard_failures=["x: a=0.1"])
    text = to_markdown(result)
    for needle in (
        "HARD FAIL",
        "Hard failures",
        "Regressions against baseline",
        "Threshold failures",
        "c1",
        "c9: boom",
        "LIVE",
    ):
        assert needle in text
    assert "Baselines were updated" not in text
    result.baseline_updated = True
    assert "Baselines were updated" in to_markdown(result)


# ---- cli -------------------------------------------------------------------------------


def test_cli_evals_run_writes_reports_and_sets_the_exit_code(tmp_path: Path) -> None:
    out = tmp_path / "rep"
    ok = runner.invoke(
        app,
        [
            "evals",
            "run",
            "--component",
            "scope_guard",
            "--out",
            str(out),
            "--baseline-dir",
            str(tmp_path / "b"),
        ],
    )
    assert ok.exit_code == 0
    assert json.loads((out / "eval-report.json").read_text())["exit_code"] == 0
    assert "scope_guard" in (out / "eval-report.md").read_text()
    bad = runner.invoke(app, ["evals", "run", "--component", "bogus", "--out", str(out)])
    assert bad.exit_code == 1
    assert "unknown component" in bad.output
    unknown_case = runner.invoke(
        app, ["evals", "run", "--app", "--cases", "nope", "--out", str(out)]
    )
    assert unknown_case.exit_code == 1
    few = runner.invoke(app, ["evals", "run", "--stability", "1"])
    assert few.exit_code == 2


def test_cli_add_case(tmp_path: Path) -> None:
    source = CASES[1].model_copy(update={"id": "my-own-case"})
    file = tmp_path / "case.json"
    file.write_text(json.dumps(source.model_dump(mode="json")), encoding="utf-8")
    target = tmp_path / "cases"
    added = runner.invoke(app, ["evals", "add-case", str(file), "--cases-dir", str(target)])
    assert added.exit_code == 0, added.output
    assert (target / "my-own-case.json").exists()
    assert "0 gold stories" in added.output
    again = runner.invoke(app, ["evals", "add-case", str(file), "--cases-dir", str(target)])
    assert again.exit_code == 1
    assert "--force" in again.output
    with_gold = source.model_copy(
        update={
            "gold_stories": [
                GoldStory(title="t", persona="Customer", want="w", requirement_categories=[])
            ]
        }
    )
    file.write_text(json.dumps(with_gold.model_dump(mode="json")), encoding="utf-8")
    forced = runner.invoke(
        app, ["evals", "add-case", str(file), "--cases-dir", str(target), "--force"]
    )
    assert forced.exit_code == 0
    assert "1 gold stories" in forced.output
    assert load_cases_dir(target)[0].gold_stories[0].title == "t"


def test_cli_add_case_rejects_bad_files(tmp_path: Path) -> None:
    broken = CASES[1].model_dump(mode="json")
    broken["id"] = "bad-case"
    broken["expected_items"][0]["evidence"] = "text that is not in the scenario"
    file = tmp_path / "bad.json"
    file.write_text(json.dumps(broken), encoding="utf-8")
    result = runner.invoke(
        app, ["evals", "add-case", str(file), "--cases-dir", str(tmp_path / "c")]
    )
    assert result.exit_code == 1
    assert "CASE_EVIDENCE" in result.output
    assert not (tmp_path / "c" / "bad-case.json").exists()
    unreadable = runner.invoke(app, ["evals", "add-case", str(tmp_path / "missing.json")])
    assert unreadable.exit_code == 1
    junk = tmp_path / "junk.json"
    junk.write_text("{not json", encoding="utf-8")
    assert runner.invoke(app, ["evals", "add-case", str(junk)]).exit_code == 1


# ---- synth ------------------------------------------------------------------------------


def _synth_payload(case: EvalCase) -> dict[str, Any]:
    data = case.model_dump(mode="json")
    for key in (
        "schema_version",
        "id",
        "seed",
        "title",
        "region",
        "workspace",
        "personas",
        "gold_domain",
        "gold_subdomain",
        "gold_subpacks",
        "default_reply",
        "tags",
    ):
        data.pop(key)
    return data


def test_synthesize_builds_a_validated_case(app_config: AppConfig, packs: PackSet) -> None:
    seed = next(s for s in SEEDS if s.slug == "card-dispute")
    fake = FakeTransport({"scenario_synth": [_synth_payload(CASES[1])]})
    result = synthesize(
        StructuredClient(fake, app_config.models), app_config, packs, ROOT / "prompts", seed
    )
    assert result.case is not None
    assert result.attempts == 1
    assert (
        result.case.id,
        result.case.gold_subdomain,
        result.case.gold_subpacks,
        result.case.region,
    ) == ("bk-card-dispute", "cards", ["india_rails"], "india")
    sent = fake.calls[0].user
    assert "SEED: Customer disputes a card transaction" in sent
    assert "dispute_handling" in sent
    assert "FIX THESE" not in sent


def test_synthesize_repairs_once_then_gives_up(app_config: AppConfig, packs: PackSet) -> None:
    seed = next(s for s in SEEDS if s.slug == "card-dispute")
    bad = _synth_payload(CASES[1])
    bad["expected_items"][0]["evidence"] = "not in the scenario at all"
    good = _synth_payload(CASES[1])
    fake = FakeTransport({"scenario_synth": [bad, good]})
    client = StructuredClient(fake, app_config.models)
    repaired = synthesize(client, app_config, packs, ROOT / "prompts", seed)
    assert repaired.case is not None
    assert repaired.attempts == 2
    assert "FIX THESE PROBLEMS" in fake.calls[1].user
    assert "CASE_EVIDENCE" in fake.calls[1].user
    fake2 = FakeTransport({"scenario_synth": [bad, bad]})
    failed = synthesize(
        StructuredClient(fake2, app_config.models), app_config, packs, ROOT / "prompts", seed
    )
    assert failed.case is None
    assert failed.attempts == 2
    assert any(f.severity is Severity.ERROR for f in failed.findings)
    assert SynthResult(None).attempts == 0


def test_seeds_and_ids() -> None:
    assert len(SEEDS) == 14
    assert sum(s.domain == "banking" for s in SEEDS) == 12
    assert case_id(SEEDS[0], 1) == "bk-card-dispute"
    assert case_id(SEEDS[0], 3) == "bk-card-dispute-v3"
    assert case_id(SEEDS[-1], 1) == "gn-hospital-booking"
    packs_text = render_request(
        SEEDS[0],
        __import__("story_agent.discovery.packs", fromlist=["load_packs"]).load_packs(
            ROOT / "config"
        ),
        2,
        [Finding(code="X", message="fix me", severity=Severity.ERROR)],
    )
    assert "VARIANT: 2" in packs_text
    assert "X: fix me" in packs_text


# ---- judge ------------------------------------------------------------------------------


def test_judge_uses_the_judge_model_and_scales_scores(app_config: AppConfig) -> None:
    state = RunState(
        run_id="r",
        scenario=Scenario(text="Customers return items."),
        redacted_text="Customers return items.",
    )
    fake = FakeTransport(
        {
            "judge": [
                {
                    "groundedness": 5,
                    "completeness": 3,
                    "testability": 1,
                    "unsupported_claims": ["a", "b"],
                    "rationale": "ok",
                }
            ]
        }
    )
    scores = judge_run(
        StructuredClient(fake, app_config.models), app_config, ROOT / "prompts", state
    )
    assert (
        scores.groundedness,
        scores.completeness,
        scores.testability,
        scores.unsupported_claims,
    ) == (1.0, 0.5, 0.0, 2)
    assert fake.calls[0].model == app_config.models.judge
    assert app_config.models.judge != app_config.models.generator
    for kind in ("scenario", "questions", "requirements", "stories"):
        assert f'<untrusted_data kind="{kind}">' in fake.calls[0].user
    assert JudgeOutput.model_json_schema()["additionalProperties"] is False
    assert "Customers return items." in render_run(state)
