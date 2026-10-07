import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from story_agent import runcmd
from story_agent.cli import app
from story_agent.publish import service
from story_agent.schema import StoryStatus
from tests.unit.publish.fixtures import finished_state
from tests.unit.publish.test_publishers import FakeSystem

ROOT = Path(__file__).resolve().parents[3]
runner = CliRunner()
RUN = "run-fixed-0001"


@pytest.fixture
def runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "config"
    shutil.copytree(ROOT / "config", config)
    text = (config / "destinations.yaml").read_text(encoding="utf-8")
    text = text.replace('project: ""', "project: Bank", 1).replace(
        'project_key: ""', "project_key: BANK"
    )
    (config / "destinations.yaml").write_text(text, encoding="utf-8")
    monkeypatch.setenv("STORY_AGENT_CONFIG_DIR", str(config))
    folder = tmp_path / "runs"
    save(folder, finished_state())
    monkeypatch.setenv("STORY_AGENT_RUNS_DIR", str(folder))
    monkeypatch.setattr(runcmd, "stdin_is_tty", lambda: False)
    return folder


def save(folder: Path, state: Any, mapping: dict[str, str] | None = None) -> None:
    run = folder / state.run_id
    run.mkdir(parents=True, exist_ok=True)
    (run / "state.json").write_text(state.model_dump_json(), encoding="utf-8")
    if mapping:
        (run / "redaction_map.json").write_text(json.dumps(mapping), encoding="utf-8")


def publish(*args: str) -> Any:
    return runner.invoke(app, ["publish", RUN, *args])


def test_markdown_is_written_to_the_run_folder_by_default(runs: Path) -> None:
    result = publish()
    assert result.exit_code == 0, result.output
    path = runs / RUN / "stories.md"
    assert "wrote 3 stories" in result.output
    assert path.read_text(encoding="utf-8").startswith("# User stories")
    assert (path.stat().st_mode & 0o777) == 0o600


@pytest.mark.parametrize(("target", "suffix"), [("json", "json"), ("ado_csv", "csv")])
def test_other_file_targets(runs: Path, target: str, suffix: str) -> None:
    out = runs.parent / f"out.{suffix}"
    result = publish("--target", target, "--out", str(out))
    assert result.exit_code == 0, result.output
    assert out.exists()


def test_out_dash_prints_to_the_terminal(runs: Path) -> None:
    result = publish("--target", "json", "--out", "-")
    assert result.exit_code == 0
    assert json.loads(result.output)["run_id"] == RUN


def test_a_run_that_has_not_finished_says_how_to_continue(runs: Path) -> None:
    result = runner.invoke(app, ["publish", "run-unfinished"])
    assert result.exit_code == 1
    assert "story-agent resume run-unfinished" in result.output


def test_an_unknown_target_is_an_error(runs: Path) -> None:
    result = publish("--target", "pdf")
    assert result.exit_code == 1
    assert "unknown target" in result.output


def test_stories_waiting_for_review_are_not_published(runs: Path) -> None:
    state = finished_state()
    state.stories[0] = state.stories[0].model_copy(update={"status": StoryStatus.DRAFT})
    save(runs, state)
    result = publish()
    assert result.exit_code == 1
    assert "wait for review" in result.output
    assert not (runs / RUN / "stories.md").exists()


def test_a_value_from_the_redaction_map_never_reaches_the_output(runs: Path) -> None:
    save(runs, finished_state(), {"[PERSON_1]": "relationship manager"})
    result = publish()
    assert result.exit_code == 1
    assert "redacted" in result.output


# ---- Azure DevOps and Jira ---------------------------------------------------------------


@pytest.mark.parametrize("target", ["ado_rest", "jira_rest"])
def test_dry_run_prints_the_exact_payload_and_sends_nothing(
    runs: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    def forbidden(*_a: Any) -> None:
        raise AssertionError("dry run must not build a client")

    monkeypatch.setattr(service, "make_rest_client", forbidden)
    result = publish("--target", target, "--dry-run")
    assert result.exit_code == 0, result.output
    plan = json.loads(result.output)
    assert plan["target"] == target
    assert len(plan["operations"]) >= 5


def test_a_real_write_is_not_enabled_in_this_build(runs: Path) -> None:
    result = publish("--target", "ado_rest")
    assert result.exit_code == 1
    assert "not enabled" in result.output
    assert "--dry-run" in result.output


def test_a_real_write_needs_a_person_at_a_terminal(
    runs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system = FakeSystem(jira=False)
    monkeypatch.setattr(service, "make_rest_client", lambda *_a: system)
    result = publish("--target", "ado_rest")
    assert result.exit_code == 2
    assert "approve" in result.output
    assert system.sent == []


def test_declining_the_approval_sends_nothing(runs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    system = FakeSystem(jira=False)
    monkeypatch.setattr(service, "make_rest_client", lambda *_a: system)
    monkeypatch.setattr(runcmd, "stdin_is_tty", lambda: True)
    monkeypatch.setattr(typer, "confirm", lambda *_a, **_k: False)
    result = publish("--target", "ado_rest")
    assert result.exit_code == 3
    assert "Nothing was sent" in result.output
    assert system.sent == []


@pytest.mark.parametrize("target", ["ado_rest", "jira_rest"])
def test_an_approved_write_creates_then_a_second_one_updates(
    runs: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    system = FakeSystem(jira=target == "jira_rest")
    monkeypatch.setattr(service, "make_rest_client", lambda *_a: system)
    monkeypatch.setattr(runcmd, "stdin_is_tty", lambda: True)
    monkeypatch.setattr(typer, "confirm", lambda *_a, **_k: True)
    first = publish("--target", target)
    assert first.exit_code == 0, first.output
    count = 8 if target == "ado_rest" else 5  # Jira has no feature level
    assert f"created {count}, updated 0" in first.output
    second = publish("--target", target)
    assert f"created 0, updated {count}" in second.output
