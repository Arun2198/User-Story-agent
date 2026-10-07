from pathlib import Path

import pytest

from story_agent.config import AppConfig, load_config
from story_agent.discovery.packs import PackSet, load_packs

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def app_config() -> AppConfig:
    """The shipped config with the nvidia provider selected, so tests do not depend on keys."""
    return load_config(ROOT / "config", env={"STORY_AGENT_PROVIDER": "nvidia"})


@pytest.fixture(scope="session")
def anthropic_config() -> AppConfig:
    """The shipped config with the anthropic provider selected."""
    return load_config(ROOT / "config", env={"STORY_AGENT_PROVIDER": "anthropic"})


@pytest.fixture(scope="session")
def packs() -> PackSet:
    return load_packs(ROOT / "config")


@pytest.fixture(scope="session")
def prompts_dir() -> Path:
    return ROOT / "prompts"
