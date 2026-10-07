from pathlib import Path

import pytest

from story_agent.config import (
    AppConfig,
    ConfigError,
    default_config_dir,
    get_api_key,
    load_config,
    load_yaml,
)


def test_loads_repo_config(app_config: AppConfig) -> None:
    assert app_config.models.generator
    assert app_config.models.judge != app_config.models.generator
    assert app_config.guardrails.budget.max_steps_per_run > 0


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_bad_yaml(tmp_path: Path) -> None:
    bad = tmp_path / "x.yaml"
    bad.write_text("a: [1,", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_yaml(bad)


def test_non_mapping(tmp_path: Path) -> None:
    f = tmp_path / "x.yaml"
    f.write_text("- 1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_yaml(f)


def test_api_key_from_env_only() -> None:
    assert get_api_key("NVIDIA_API_KEY", {"NVIDIA_API_KEY": "k"}) == "k"
    with pytest.raises(ConfigError):
        get_api_key("NVIDIA_API_KEY", {})


def test_default_config_dir() -> None:
    assert default_config_dir({"STORY_AGENT_CONFIG_DIR": "/x"}) == Path("/x")
    assert default_config_dir({}) == Path("config")
