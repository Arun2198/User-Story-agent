from pathlib import Path

import pytest

from story_agent.config import AppConfig, load_config
from story_agent.discovery.packs import PackSet, load_packs

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def app_config() -> AppConfig:
    return load_config(ROOT / "config")


@pytest.fixture(scope="session")
def packs() -> PackSet:
    return load_packs(ROOT / "config")


@pytest.fixture(scope="session")
def prompts_dir() -> Path:
    return ROOT / "prompts"
