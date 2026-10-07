from pathlib import Path

import pytest

from story_agent.config import AppConfig, load_config

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def app_config() -> AppConfig:
    return load_config(ROOT / "config")
