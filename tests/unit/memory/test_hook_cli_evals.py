import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from story_agent.cli import app
from story_agent.config import AppConfig
from story_agent.evals.components import memory as memory_evals
from story_agent.evals.components.common import finalize
from story_agent.hooks.post.memory import MemoryProposalHook
from story_agent.memory.store import SqliteMemoryStore
from story_agent.schema import Answer, AnswerKind, DiscoveryMap, HookAction, Question, QuestionRound
from tests.unit.hooks.test_hooks import make_ctx
from tests.unit.memory.helpers import entry, store_for

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[3]


def _args(tmp: Path, *args: str, ws: str = "w1") -> list[str]:
    return ["memory", *args, "--workspace", ws, "--memory-dir", str(tmp)]


@pytest.fixture(autouse=True)
def _config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORY_AGENT_CONFIG_DIR", str(ROOT / "config"))


# ---- hook -------------------------------------------------------------------


def _ctx_with_answer(app_config: AppConfig, tmp_path: Path, value: str, with_store: bool = True):  # type: ignore[no-untyped-def]  # test helper
    ctx = make_ctx(app_config, runs_dir=tmp_path / "runs")
    ctx.state.scenario = ctx.state.scenario.model_copy(update={"workspace": "w1"})
    ctx.state.discovery = DiscoveryMap(domain="banking", subdomain="cards")
    question = Question(
        id="Q1-channels", category="channels", question="q", why_it_matters="w", options=["a", "b"]
    )
    ctx.state.rounds = [QuestionRound(number=1, questions=[question])]
    ctx.state.answers = [Answer(question_id="Q1-channels", kind=AnswerKind.OTHER, value=value)]
    if with_store:
        ctx.services.memory = store_for(app_config, tmp_path / "mem")
    return ctx


def test_hook_builds_proposals_but_saves_nothing(app_config: AppConfig, tmp_path: Path) -> None:
    ctx = _ctx_with_answer(app_config, tmp_path, "Mobile only")
    result = MemoryProposalHook().run(ctx)
    assert result.action is HookAction.PASS
    assert [p.entry.content for p in ctx.data["memory_proposals"]] == ["Mobile only"]
    assert ctx.services.memory.list_entries() == []
    ctx.services.memory.close()
    record = json.loads((tmp_path / "runs" / "r1" / "memory_proposals.json").read_text())
    assert record["proposals"][0]["action"] == "create"
    assert "Mobile only" not in json.dumps(record)


def test_hook_reports_refused_candidates_as_warnings(app_config: AppConfig, tmp_path: Path) -> None:
    ctx = _ctx_with_answer(app_config, tmp_path, "mail a@b.co")
    result = MemoryProposalHook().run(ctx)
    assert [f.code for f in result.findings] == ["MEMORY_PII"]
    assert ctx.data["memory_proposals"] == []
    ctx.services.memory.close()


def test_hook_does_nothing_without_a_store(app_config: AppConfig, tmp_path: Path) -> None:
    ctx = _ctx_with_answer(app_config, tmp_path, "Mobile only", with_store=False)
    assert MemoryProposalHook().run(ctx).action is HookAction.PASS
    assert "memory_proposals" not in ctx.data


# ---- cli --------------------------------------------------------------------


def test_cli_list_show_edit_delete_export_clear(app_config: AppConfig, tmp_path: Path) -> None:
    out = runner.invoke(app, _args(tmp_path, "list"))
    assert out.exit_code == 0
    assert "0 entries" in out.output
    store = store_for(app_config, tmp_path)
    store.put(entry("M-1", age_days=400, ttl_days=30))
    store.put(entry("M-2", tags=["category:fees_charges"], content="Flat fee of 5"))
    store.close()
    listing = runner.invoke(app, _args(tmp_path, "list")).output
    assert "M-1" in listing
    assert "STALE" in listing
    assert "fresh" in listing
    assert "2 entries" in listing
    assert "M-2" not in runner.invoke(app, _args(tmp_path, "list", "--type", "preference")).output
    assert "M-2" in runner.invoke(app, _args(tmp_path, "list", "--domain", "banking")).output
    shown = json.loads(runner.invoke(app, _args(tmp_path, "show", "M-2")).output)
    assert shown["content"] == "Flat fee of 5"
    assert shown["confirmed"] is True
    edited = runner.invoke(
        app,
        _args(
            tmp_path,
            "edit",
            "M-2",
            "--content",
            "Flat fee of 6",
            "--ttl-days",
            "10",
            "--tags",
            "category:fees_charges, x",
        ),
    )
    assert edited.exit_code == 0
    again = json.loads(runner.invoke(app, _args(tmp_path, "show", "M-2")).output)
    assert (again["content"], again["ttl_days"], again["tags"]) == (
        "Flat fee of 6",
        10,
        ["category:fees_charges", "x"],
    )
    exported = json.loads(runner.invoke(app, _args(tmp_path, "export")).output)
    assert exported["workspace"] == "w1"
    assert [e["id"] for e in exported["entries"]] == ["M-1", "M-2"]
    target = tmp_path / "out.json"
    assert runner.invoke(app, _args(tmp_path, "export", "--out", str(target))).exit_code == 0
    assert len(json.loads(target.read_text())["entries"]) == 2
    assert runner.invoke(app, _args(tmp_path, "delete", "M-1")).exit_code == 0
    assert runner.invoke(app, _args(tmp_path, "delete", "M-1")).exit_code == 1
    declined = runner.invoke(
        app, ["memory", "clear", "--workspace", "w1", "--memory-dir", str(tmp_path)], input="n\n"
    )
    assert declined.exit_code == 1
    assert "M-2" in runner.invoke(app, _args(tmp_path, "list")).output
    cleared = runner.invoke(
        app, ["memory", "clear", "--workspace", "w1", "--memory-dir", str(tmp_path), "--yes"]
    )
    assert cleared.exit_code == 0
    assert "cleared 1 entries" in cleared.output


def test_cli_errors(app_config: AppConfig, tmp_path: Path) -> None:
    store = store_for(app_config, tmp_path)
    store.put(entry("M-1"))
    store.close()
    assert runner.invoke(app, _args(tmp_path, "show", "nope")).exit_code == 1
    assert runner.invoke(app, _args(tmp_path, "edit", "nope", "--content", "x")).exit_code == 1
    unsafe = runner.invoke(app, _args(tmp_path, "edit", "M-1", "--content", "mail a@b.co"))
    assert unsafe.exit_code == 1
    assert "refused" in unsafe.output
    still = json.loads(runner.invoke(app, _args(tmp_path, "show", "M-1")).output)
    assert still["content"] == "Mobile and web only"
    bad = runner.invoke(app, _args(tmp_path, "list", ws="../evil"))
    assert bad.exit_code == 1
    assert not (tmp_path.parent / "evil.db").exists()


def test_cli_workspaces_are_isolated(app_config: AppConfig, tmp_path: Path) -> None:
    store = store_for(app_config, tmp_path, "alpha")
    store.put(entry("M-1", workspace="alpha", content="alpha secret"))
    store.close()
    assert "alpha secret" in runner.invoke(app, _args(tmp_path, "list", ws="alpha")).output
    other = runner.invoke(app, _args(tmp_path, "list", ws="beta"))
    assert "alpha secret" not in other.output
    assert "0 entries" in other.output
    assert runner.invoke(app, _args(tmp_path, "show", "M-1", ws="beta")).exit_code == 1


def test_cli_without_a_command_shows_help() -> None:
    assert runner.invoke(app, []).exit_code in (0, 2)
    assert "memory" in runner.invoke(app, ["--help"]).output


# ---- evals ------------------------------------------------------------------


def test_memory_evals_meet_thresholds(app_config: AppConfig) -> None:
    reports = memory_evals.run_all(app_config.evals, ROOT / "config")
    assert [r.name for r in reports] == [
        "memory_retrieval",
        "memory_staleness",
        "memory_contradiction",
        "memory_safety",
        "memory_isolation",
        "memory_effect",
    ]
    for report in reports:
        assert report.thresholds_met, (report.name, report.failures)
        assert report.details["hard_failures"] == [] if "hard_failures" in report.details else True


def test_effect_eval_reports_a_real_reduction(app_config: AppConfig) -> None:
    report = memory_evals.effect(app_config, app_config.evals, ROOT / "config")
    assert 0 < report.metrics["question_reduction"] < 1
    assert report.metrics["spurious_defaults"] == 0


def test_the_retrieval_eval_fails_if_recall_returns_everything(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    class All:
        def __init__(self, ids: list[str]) -> None:
            self.ids = ids

    def everything(store, *_a, **_k):  # type: ignore[no-untyped-def]  # stub
        return All([e.id for e in store.list_entries()])

    monkeypatch.setattr(memory_evals, "recall", everything)
    report = memory_evals.retrieval(app_config, app_config.evals)
    assert not report.thresholds_met
    assert report.metrics["precision"] < 0.9


def test_the_safety_eval_hard_fails_if_the_guard_is_removed(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(memory_evals, "check_content", lambda *_a, **_k: [])
    monkeypatch.setattr("story_agent.memory.store.check_content", lambda *_a, **_k: [])
    report = memory_evals.safety(app_config, app_config.evals)
    assert report.metrics["pii_persisted"] > 0
    assert report.details["hard_failures"]
    assert not report.thresholds_met


def test_the_isolation_eval_hard_fails_if_the_workspace_check_is_removed(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = SqliteMemoryStore.put

    def lax_put(self, e):  # type: ignore[no-untyped-def]  # stub
        return original(self, e.model_copy(update={"workspace": self.workspace}))

    monkeypatch.setattr(SqliteMemoryStore, "put", lax_put)
    report = memory_evals.isolation(app_config, app_config.evals)
    assert report.metrics["mismatch_block_rate"] == 0
    assert report.details["hard_failures"]


def test_the_staleness_eval_hard_fails_if_stale_defaults_are_auto_confirmed(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def always_confirm(question, text):  # type: ignore[no-untyped-def]  # stub
        d = question.remembered_default
        return Answer(
            question_id=question.id,
            kind=AnswerKind.MEMORY_CONFIRMED,
            value=d.value,
            memory_id=d.memory_id,
        )

    monkeypatch.setattr(memory_evals, "parse_answer", always_confirm)
    report = memory_evals.staleness(app_config, app_config.evals)
    assert report.metrics["stale_misapplication_rate"] > 0
    assert report.details["hard_failures"]


def test_finalize_marks_only_hard_metrics() -> None:
    cfg = {"thresholds": {"x": {"a": {"min": 1.0}, "b": {"max": 0, "hard": True}}}}
    report = finalize("x", {"a": 0.5, "b": 2}, cfg)
    assert len(report.failures) == 2
    assert len(report.details["hard_failures"]) == 1
    assert report.details["hard_failures"][0].startswith("b=")
    assert finalize("x", {"a": 1.0, "b": 0}, cfg).thresholds_met
