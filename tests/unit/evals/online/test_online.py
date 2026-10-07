import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from story_agent.cli import app
from story_agent.config import AppConfig, ConfigError
from story_agent.discovery.packs import PackSet
from story_agent.evals.app.driver import build_deps
from story_agent.evals.app.gold_model import GoldModel
from story_agent.evals.app.judge import JudgeScores
from story_agent.evals.cases import load_cases_dir
from story_agent.evals.online import (
    NullEvaluator,
    SamplingEvaluator,
    build_online_evaluator,
    build_trace,
    extract_feedback,
    sampled,
    to_otlp,
)
from story_agent.evals.online.config import DriftConfig, Tolerance, parse_online
from story_agent.evals.online.drift import check_drift, mean_metrics
from story_agent.evals.online.sinks import JsonlSink, PendingInputError
from story_agent.evals.online.trace import Span, assert_trace_safe, read_events
from story_agent.evals.runner import load_baseline
from story_agent.evals.simulator import SimulatedResponder, SimulatedUser
from story_agent.graph import Runtime
from story_agent.hooks import build_pipeline, default_registry
from story_agent.schema import RunState, Scenario
from story_agent.session import Session
from tests.unit.publish.fixtures import finished_state

ROOT = Path(__file__).resolve().parents[4]
PROMPTS = ROOT / "prompts"
CASE = {c.id: c for c in load_cases_dir()}["bk-card-dispute"]
runner = CliRunner()
OPEN: list[Runtime] = []


@pytest.fixture(autouse=True)
def _close() -> Iterator[None]:
    yield
    while OPEN:
        OPEN.pop().close()


def run_once(
    config: AppConfig,
    packs: PackSet,
    tmp: Path,
    online: Any = None,
    *,
    run_id: str = "run-online-1",
    memory: bool = False,
) -> RunState:
    deps = build_deps(config, packs, PROMPTS, GoldModel(CASE, packs))
    runtime = Runtime(
        deps,
        build_pipeline(config.hooks, default_registry()),
        tmp / "runs",
        tmp / "memory" if memory else None,
        online or NullEvaluator(),
    )
    OPEN.append(runtime)
    scenario = Scenario(text=CASE.scenario, notes=CASE.notes, workspace=CASE.workspace)
    outcome = Session(runtime).start(scenario, SimulatedResponder(SimulatedUser(CASE)), run_id)
    assert outcome.status == "done"
    assert outcome.state is not None
    return outcome.state


# ---- the trace -------------------------------------------------------------------------


def test_a_real_run_becomes_a_valid_trace(
    app_config: AppConfig, packs: PackSet, tmp_path: Path
) -> None:
    state = run_once(app_config, packs, tmp_path)
    events = read_events(tmp_path / "runs" / state.run_id / "trace.jsonl")
    assert len(events) >= 5
    trace = build_trace(state, events)
    root, *calls = trace.spans
    assert root.name == "story_agent.run"
    assert root.parent_span_id is None
    assert len(calls) == len(events)
    assert {c.parent_span_id for c in calls} == {root.span_id}
    assert len({s.trace_id for s in trace.spans}) == 1
    assert len({s.span_id for s in trace.spans}) == len(trace.spans)
    model_calls = [c for c in calls if c.kind.value == "CLIENT"]
    assert model_calls
    first = model_calls[0].attributes
    assert first["gen_ai.system"] == "anthropic"
    assert first["gen_ai.request.model"] == app_config.models.generator
    assert first["story_agent.prompt_hash"]
    assert root.attributes["gen_ai.usage.total_tokens"] == state.tokens_used
    assert root.start_time_unix_nano <= min(c.start_time_unix_nano for c in calls)
    assert any(e.name == "story_agent.review" for e in root.events)
    assert build_trace(state, events) == trace  # same input, same trace


def test_the_trace_holds_no_scenario_text_or_planted_values(
    app_config: AppConfig, packs: PackSet, tmp_path: Path
) -> None:
    state = run_once(app_config, packs, tmp_path)
    folder = tmp_path / "runs" / state.run_id
    trace = build_trace(state, read_events(folder / "trace.jsonl"))
    blob = json.dumps(trace.model_dump(mode="json")) + json.dumps(to_otlp(trace))
    assert CASE.planted_pii
    for planted in CASE.planted_pii:
        assert planted.value not in blob
    for sentence in CASE.scenario.split("."):
        if len(sentence.strip()) > 25:
            assert sentence.strip() not in blob
    assert CASE.workspace not in blob  # only a hash of the workspace
    mapping = json.loads((folder / "redaction_map.json").read_text())
    assert_trace_safe(trace, mapping)


def test_a_trace_with_a_sensitive_value_is_refused(
    app_config: AppConfig, packs: PackSet, tmp_path: Path
) -> None:
    state = run_once(app_config, packs, tmp_path)
    trace = build_trace(state, [])
    root = trace.spans[0].model_copy(
        update={
            "attributes": {**trace.spans[0].attributes, "story_agent.note": "jane.doe@example.com"}
        }
    )
    with pytest.raises(ValueError, match="sensitive"):
        assert_trace_safe(trace.model_copy(update={"spans": [root]}))
    leaked = trace.spans[0].model_copy(
        update={"attributes": {**trace.spans[0].attributes, "story_agent.note": "my secret value"}}
    )
    with pytest.raises(ValueError, match="redacted"):
        assert_trace_safe(trace.model_copy(update={"spans": [leaked]}), {"[X_1]": "secret value"})


def test_the_otlp_document_has_the_expected_shape(
    app_config: AppConfig, packs: PackSet, tmp_path: Path
) -> None:
    state = run_once(app_config, packs, tmp_path)
    trace = build_trace(state, read_events(tmp_path / "runs" / state.run_id / "trace.jsonl"))
    doc = to_otlp(trace)
    resource = doc["resourceSpans"][0]
    assert {"key": "service.name", "value": {"stringValue": "story-agent"}} in resource["resource"][
        "attributes"
    ]
    spans = resource["scopeSpans"][0]["spans"]
    assert len(spans) == len(trace.spans)
    assert "parentSpanId" not in spans[0]
    assert spans[1]["parentSpanId"] == spans[0]["spanId"]
    assert spans[1]["kind"] in {1, 3}
    assert spans[0]["startTimeUnixNano"].isdigit()
    values = {a["key"]: a["value"] for a in spans[0]["attributes"]}
    assert "intValue" in values["story_agent.rounds"]
    assert "doubleValue" in values["story_agent.cost_usd"]
    assert "stringValue" in values["story_agent.run_id"]


def test_span_rules_reject_bad_data() -> None:
    base: dict[str, Any] = {
        "trace_id": "a" * 32,
        "span_id": "b" * 16,
        "name": "x",
        "start_time_unix_nano": 5,
        "end_time_unix_nano": 9,
    }
    Span.model_validate(base)
    with pytest.raises(ValidationError, match="before it starts"):
        Span.model_validate({**base, "end_time_unix_nano": 1})
    with pytest.raises(ValidationError, match="bad attribute name"):
        Span.model_validate({**base, "attributes": {"Bad Name": 1}})
    with pytest.raises(ValidationError, match="longer than"):
        Span.model_validate({**base, "attributes": {"story_agent.x": "y" * 201}})
    with pytest.raises(ValidationError):
        Span.model_validate({**base, "trace_id": "short"})
    with pytest.raises(ValidationError):
        Span.model_validate({**base, "surprise": 1})


def test_read_events_skips_bad_lines(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    assert read_events(path) == []
    path.write_text(
        'not json\n{"type": "other"}\n{"type": "call_end", "ts": "x"}\n', encoding="utf-8"
    )
    assert [e["type"] for e in read_events(path)] == ["call_end"]


# ---- feedback --------------------------------------------------------------------------


def test_feedback_counts_what_the_person_did() -> None:
    state = finished_state()
    state.review_log = [
        {"action": "edit", "story_id": "DSP-0002", "edit_distance": 0.2},
        {"action": "edit", "story_id": "DSP-0002", "edit_distance": 0.4},
        {"action": "approve", "story_id": "DSP-0001"},
    ]
    state.memory_rejected = ["M-1"]
    state.memory_outcome = {"saved": ["M-2"], "refreshed": [], "rejected": ["M-3", "M-4"]}
    state.tokens_used, state.cost_usd, state.critique_loops = 1200, 0.05, 1
    signals = extract_feedback(state, llm_calls=7)
    assert (signals.approved, signals.edited, signals.rejected) == (2, 1, 1)
    assert signals.stories == 3
    assert signals.questions_asked == 2
    assert signals.defaults_confirmed == 1
    assert signals.forced_resolutions == 1
    assert signals.defaults_rejected == 1
    assert (signals.memory_proposed, signals.memory_saved, signals.memory_rejected) == (3, 1, 2)
    assert signals.mean_edit_distance == 0.3
    metrics = signals.metrics()
    assert metrics["approve_rate"] == 0.5
    assert metrics["reject_rate"] == 0.25
    assert metrics["memory_reject_rate"] == 2 / 3
    assert metrics["forced_resolution_rate"] == 0.5
    assert metrics["llm_calls"] == 7.0


def test_feedback_metric_names_match_the_offline_baseline() -> None:
    baseline = load_baseline(ROOT / "src" / "story_agent" / "evals" / "baselines", "offline", "app")
    assert baseline is not None
    shared = extract_feedback(finished_state()).metrics().keys() & baseline.keys()
    assert {
        "questions_asked",
        "rounds_to_readiness",
        "other_answer_rate",
        "tokens",
        "cost_usd",
    } <= shared


def test_a_run_with_no_decisions_has_zero_rates() -> None:
    state = RunState(run_id="r", scenario=Scenario(text="x" * 30))
    metrics = extract_feedback(state).metrics()
    assert metrics["approve_rate"] == 0.0
    assert metrics["memory_reject_rate"] == 0.0
    assert metrics["other_answer_rate"] == 0.0


def test_memory_outcome_is_kept_in_run_state(
    app_config: AppConfig, packs: PackSet, tmp_path: Path
) -> None:
    state = run_once(app_config, packs, tmp_path, memory=True)
    assert state.memory_outcome["saved"]
    assert extract_feedback(state).memory_saved == len(state.memory_outcome["saved"])


# ---- configuration -------------------------------------------------------------------


def test_online_is_off_in_the_shipped_config(app_config: AppConfig) -> None:
    config = parse_online(app_config.evals)
    assert not config.enabled
    assert config.sink == "none"
    assert isinstance(build_online_evaluator(app_config.evals, Path("runs")), NullEvaluator)


def test_online_config_errors_are_clean() -> None:
    assert not parse_online({}).enabled
    with pytest.raises(ConfigError, match="sink is none"):
        parse_online({"online": {"enabled": True}})
    with pytest.raises(ConfigError, match="sink"):
        parse_online({"online": {"sink": "carrier-pigeon"}})
    with pytest.raises(ConfigError, match="surprise"):
        parse_online({"online": {"surprise": 1}})
    with pytest.raises(ConfigError, match="trace_sample_rate"):
        parse_online({"online": {"trace_sample_rate": 2}})


@pytest.mark.parametrize("sink", ["otlp_http", "langfuse", "warehouse"])
def test_stub_sinks_refuse_at_start_up_and_say_what_is_needed(
    app_config: AppConfig, sink: str, tmp_path: Path
) -> None:
    evals = {**app_config.evals, "online": {"enabled": True, "sink": sink}}
    with pytest.raises(PendingInputError, match="pending input"):
        build_online_evaluator(evals, tmp_path)


def test_the_jsonl_sink_builds_an_evaluator(app_config: AppConfig, tmp_path: Path) -> None:
    evals = {**app_config.evals, "online": {"enabled": True, "sink": "jsonl"}}
    assert isinstance(build_online_evaluator(evals, tmp_path), SamplingEvaluator)


# ---- sampling ----------------------------------------------------------------------------


def test_sampling_is_deterministic_and_close_to_the_rate() -> None:
    assert sampled("r", 0.0, "x") is False
    assert sampled("r", 1.0, "x") is True
    assert sampled("run-1", 0.5, "trace") == sampled("run-1", 0.5, "trace")
    hits = sum(sampled(f"run-{i}", 0.25, "judge") for i in range(2000))
    assert 380 < hits < 620
    assert [sampled(f"run-{i}", 0.5, "a") for i in range(50)] != [
        sampled(f"run-{i}", 0.5, "b") for i in range(50)
    ]


# ---- the evaluator in a real run --------------------------------------------------------


def enabled(tmp: Path, judge_fn: Any = None, **overrides: Any) -> SamplingEvaluator:
    raw: dict[str, Any] = {
        "enabled": True,
        "sink": "jsonl",
        "judge": {"enabled": True, "sample_rate": 1.0},
    }
    raw.update(overrides)
    return SamplingEvaluator(
        parse_online({"online": raw}), JsonlSink(tmp / "runs" / "rec.jsonl"), judge_fn
    )


def test_a_finished_run_is_recorded_once(
    app_config: AppConfig, packs: PackSet, tmp_path: Path
) -> None:
    evaluator = enabled(tmp_path)
    state = run_once(app_config, packs, tmp_path, evaluator)
    path = tmp_path / "runs" / "rec.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["run_id"] == state.run_id
    assert record["metrics"]["approve_rate"] == 1.0
    assert record["trace"]["spans"][0]["name"] == "story_agent.run"
    assert (path.stat().st_mode & 0o777) == 0o600
    evaluator.on_run_finished(state, tmp_path / "runs")  # a repeat adds nothing
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_a_run_outside_the_trace_sample_is_not_recorded(
    app_config: AppConfig, packs: PackSet, tmp_path: Path
) -> None:
    evaluator = enabled(tmp_path, trace_sample_rate=0.0)
    run_once(app_config, packs, tmp_path, evaluator)
    assert not (tmp_path / "runs" / "rec.jsonl").exists()


def test_the_judge_scores_only_sampled_runs_and_only_when_enabled(
    app_config: AppConfig, packs: PackSet, tmp_path: Path
) -> None:
    calls: list[str] = []

    def judge(state: RunState) -> JudgeScores:
        calls.append(state.run_id)
        return JudgeScores(0.75, 0.5, 1.0, 2)

    run_once(app_config, packs, tmp_path, enabled(tmp_path, judge), run_id="run-j1")
    record = json.loads((tmp_path / "runs" / "rec.jsonl").read_text().splitlines()[0])
    assert record["metrics"]["judge_groundedness"] == 0.75
    assert record["metrics"]["judge_unsupported_claims"] == 2.0
    assert calls == ["run-j1"]
    off = enabled(tmp_path / "off", judge, judge={"enabled": False, "sample_rate": 1.0})
    run_once(app_config, packs, tmp_path / "off", off, run_id="run-j2")
    none = enabled(tmp_path / "none", judge, judge={"enabled": True, "sample_rate": 0.0})
    run_once(app_config, packs, tmp_path / "none", none, run_id="run-j3")
    assert calls == ["run-j1"]
    nojudge = enabled(tmp_path / "nj", None)
    run_once(app_config, packs, tmp_path / "nj", nojudge, run_id="run-j4")
    plain = json.loads((tmp_path / "nj" / "runs" / "rec.jsonl").read_text().splitlines()[0])
    assert "judge_groundedness" not in plain["metrics"]


def test_an_evaluator_that_fails_never_fails_the_run(
    app_config: AppConfig, packs: PackSet, tmp_path: Path
) -> None:
    class Broken:
        def on_run_finished(self, state: RunState, runs_dir: Path) -> None:
            raise RuntimeError("the collector is down")

    state = run_once(app_config, packs, tmp_path, Broken())
    assert state.stories
    assert (tmp_path / "runs" / state.run_id / "state.json").exists()


def test_a_paused_run_is_not_recorded(
    app_config: AppConfig, packs: PackSet, tmp_path: Path
) -> None:
    deps = build_deps(app_config, packs, PROMPTS, GoldModel(CASE, packs))
    evaluator = enabled(tmp_path)
    runtime = Runtime(
        deps,
        build_pipeline(app_config.hooks, default_registry()),
        tmp_path / "runs",
        None,
        evaluator,
    )
    OPEN.append(runtime)

    class Nobody:
        def respond(self, payload: dict[str, Any]) -> None:
            return None

    scenario = Scenario(text=CASE.scenario, workspace=CASE.workspace)
    assert Session(runtime).start(scenario, Nobody(), "run-p").status == "paused"
    assert not (tmp_path / "runs" / "rec.jsonl").exists()


# ---- drift ------------------------------------------------------------------------------

BASELINE = {
    "questions_asked": 14.0,
    "other_answer_rate": 0.4,
    "judge_groundedness": 0.9,
    "cost_usd": 0.1,
}
CONFIG = DriftConfig(
    min_runs=3,
    tolerances={
        "questions_asked": Tolerance(rel=0.25),
        "other_answer_rate": Tolerance(abs=0.1),
        "judge_groundedness": Tolerance(abs=0.05),
        "tokens": Tolerance(rel=0.5),
    },
)


def test_runs_close_to_the_baseline_do_not_drift() -> None:
    rows = [{"questions_asked": 15.0, "other_answer_rate": 0.45, "judge_groundedness": 0.88}] * 3
    report = check_drift(rows, BASELINE, CONFIG)
    assert report.ok
    assert set(report.checked) == {"questions_asked", "other_answer_rate", "judge_groundedness"}


def test_runs_far_from_the_baseline_drift() -> None:
    rows = [{"questions_asked": 20.0, "other_answer_rate": 0.7, "judge_groundedness": 0.9}] * 3
    report = check_drift(rows, BASELINE, CONFIG)
    assert [d.metric for d in report.drifted] == ["questions_asked", "other_answer_rate"]
    assert "online 20" in str(report.drifted[0])


def test_too_few_runs_are_not_judged() -> None:
    report = check_drift([{"questions_asked": 99.0}], BASELINE, CONFIG)
    assert report.ok
    assert "at least 3" in report.skipped


def test_the_mean_uses_only_runs_that_report_the_metric() -> None:
    assert mean_metrics([{"a": 1.0}, {"a": 3.0, "b": 5.0}]) == {"a": 2.0, "b": 5.0}


# ---- the commands ------------------------------------------------------------------------


@pytest.fixture
def finished_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("STORY_AGENT_CONFIG_DIR", str(ROOT / "config"))
    folder = tmp_path / "runs"
    state = finished_state()
    (folder / state.run_id).mkdir(parents=True)
    (folder / state.run_id / "state.json").write_text(state.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("STORY_AGENT_RUNS_DIR", str(folder))
    return folder


def test_the_trace_and_feedback_commands(finished_run: Path) -> None:
    trace = runner.invoke(app, ["online", "trace", "run-fixed-0001"])
    assert trace.exit_code == 0, trace.output
    assert json.loads(trace.output)["spans"][0]["name"] == "story_agent.run"
    otlp = runner.invoke(app, ["online", "trace", "run-fixed-0001", "--otlp"])
    assert "resourceSpans" in json.loads(otlp.output)
    feedback = runner.invoke(app, ["online", "feedback", "run-fixed-0001"])
    assert json.loads(feedback.output)["approved"] == 2
    missing = runner.invoke(app, ["online", "feedback", "run-nope"])
    assert missing.exit_code == 1


def test_a_trace_command_refuses_a_run_that_leaks(finished_run: Path) -> None:
    mapping = finished_run / "run-fixed-0001" / "redaction_map.json"
    mapping.write_text(json.dumps({"[P_1]": "story_agent.run"}), encoding="utf-8")
    result = runner.invoke(app, ["online", "trace", "run-fixed-0001"])
    assert result.exit_code == 1
    assert "redacted" in result.output


def test_the_drift_command(finished_run: Path, tmp_path: Path) -> None:
    baselines = tmp_path / "baselines"
    (baselines / "live").mkdir(parents=True)
    result = runner.invoke(app, ["online", "drift", "--baseline-dir", str(baselines)])
    assert result.exit_code == 0
    assert "no live baseline" in result.output
    (baselines / "live" / "app.json").write_text(
        json.dumps({"name": "app", "metrics": {"questions_asked": 14.0}}), encoding="utf-8"
    )
    result = runner.invoke(app, ["online", "drift", "--baseline-dir", str(baselines)])
    assert result.exit_code == 0
    assert "not checked" in result.output
    records = finished_run / "online" / "records.jsonl"
    records.parent.mkdir()
    rows = [json.dumps({"metrics": {"questions_asked": 30.0}}) for _ in range(20)]
    records.write_text("\n".join(rows) + "\n", encoding="utf-8")
    result = runner.invoke(app, ["online", "drift", "--baseline-dir", str(baselines)])
    assert result.exit_code == 1
    assert "DRIFT questions_asked" in result.output
    rows = [json.dumps({"metrics": {"questions_asked": 14.0}}) for _ in range(20)]
    records.write_text("\n".join(rows) + "\n", encoding="utf-8")
    ok = runner.invoke(app, ["online", "drift", "--baseline-dir", str(baselines)])
    assert ok.exit_code == 0
    assert "1 metrics checked" in ok.output
