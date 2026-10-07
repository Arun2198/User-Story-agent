from pathlib import Path
from typing import Any

import pytest
import yaml

from story_agent.anthropic_transport import AnthropicTransport
from story_agent.config import ConfigError, load_config, resolve_models
from story_agent.evals.online.trace import system_for
from story_agent.nvidia import NvidiaTransport
from story_agent.providers import best_models, transport_for

ROOT = Path(__file__).resolve().parents[2]
RAW: dict[str, Any] = yaml.safe_load((ROOT / "config" / "models.yaml").read_text(encoding="utf-8"))


def test_auto_picks_the_first_provider_with_a_key() -> None:
    assert resolve_models(RAW, {"NVIDIA_API_KEY": "x"}).provider == "nvidia"
    assert resolve_models(RAW, {"ANTHROPIC_API_KEY": "x", "NVIDIA_API_KEY": "x"}).provider == (
        "anthropic"
    )


def test_auto_with_no_key_still_resolves_so_offline_work_needs_none() -> None:
    assert resolve_models(RAW, {}).provider == "anthropic"


def test_the_environment_overrides_the_file() -> None:
    models = resolve_models(RAW, {"STORY_AGENT_PROVIDER": "nvidia", "ANTHROPIC_API_KEY": "x"})
    assert models.provider == "nvidia"
    assert models.api_key_env == "NVIDIA_API_KEY"


def test_an_unknown_provider_is_refused() -> None:
    with pytest.raises(ConfigError):
        resolve_models(RAW, {"STORY_AGENT_PROVIDER": "other"})


def test_a_missing_section_is_refused() -> None:
    raw = {**RAW, "provider": "nvidia", "providers": {"anthropic": RAW["providers"]["anthropic"]}}
    with pytest.raises(ConfigError):
        resolve_models(raw, {})


def test_invalid_models_file_is_refused() -> None:
    with pytest.raises(ConfigError):
        resolve_models({"providers": 3}, {})


def test_shared_and_block_settings_merge() -> None:
    anthropic = resolve_models(RAW, {"STORY_AGENT_PROVIDER": "anthropic"})
    nvidia = resolve_models(RAW, {"STORY_AGENT_PROVIDER": "nvidia"})
    assert anthropic.generator.startswith("claude-sonnet")
    assert anthropic.judge.startswith("claude-opus")
    assert anthropic.max_tokens == 16000
    assert nvidia.max_tokens is None
    assert nvidia.extra_body == {}
    assert anthropic.timeout_s == nvidia.timeout_s


def test_transport_matches_the_provider() -> None:
    a = resolve_models(RAW, {"STORY_AGENT_PROVIDER": "anthropic"})
    n = resolve_models(RAW, {"STORY_AGENT_PROVIDER": "nvidia"})
    assert isinstance(transport_for(a, {"ANTHROPIC_API_KEY": "k" * 30}), AnthropicTransport)
    assert isinstance(transport_for(n, {"NVIDIA_API_KEY": "k" * 30}), NvidiaTransport)


def test_a_missing_key_names_the_other_provider() -> None:
    a = resolve_models(RAW, {"STORY_AGENT_PROVIDER": "anthropic"})
    with pytest.raises(ConfigError) as info:
        transport_for(a, {})
    assert "ANTHROPIC_API_KEY" in str(info.value)
    assert "--provider nvidia" in str(info.value)


def test_best_models_ranks_strongest_family_first() -> None:
    ids = ["a/qwen3.5-397b", "nvidia/nemotron-3-ultra-550b", "x/unrelated"]
    assert best_models("nvidia", ids) == ["nvidia/nemotron-3-ultra-550b", "a/qwen3.5-397b"]
    assert best_models("anthropic", ["claude-sonnet-5-5", "claude-opus-5-5"])[0].endswith(
        "opus-5-5"
    )


def test_trace_system_follows_the_model() -> None:
    assert system_for("claude-opus-5-5") == "anthropic"
    assert system_for("nvidia/nemotron-3-ultra-550b-a55b") == "nvidia"


def test_load_config_uses_the_environment() -> None:
    cfg = load_config(ROOT / "config", env={"STORY_AGENT_PROVIDER": "anthropic"})
    assert cfg.models.provider == "anthropic"
