import io
import json
import logging
from pathlib import Path

from story_agent.logging import configure_logging, stage_logger
from story_agent.prompts import load_prompt


def test_prompt_version_and_hash(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("---\nversion: 3\n---\nBody\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("No front matter\n", encoding="utf-8")
    a = load_prompt(tmp_path, "a")
    assert (a.version, a.text) == ("3", "Body")
    assert load_prompt(tmp_path, "b").version == "0"
    assert a.hash != load_prompt(tmp_path, "b").hash


def test_json_logging_has_run_and_stage() -> None:
    stream = io.StringIO()
    configure_logging(stream, logging.INFO)
    stage_logger("run1", "discover").info("done")
    record = json.loads(stream.getvalue())
    assert record["run_id"] == "run1"
    assert record["stage"] == "discover"
    assert record["msg"] == "done"
