"""Build the model transport for the provider that config and the environment select."""

from __future__ import annotations

import os
from collections.abc import Mapping

from story_agent.anthropic_transport import AnthropicTransport
from story_agent.config import PROVIDERS, ConfigError, ModelsConfig, get_api_key
from story_agent.nvidia import NvidiaTransport

# Substrings of model ids, strongest first, used by `story-agent models --best`.
PREFERRED: dict[str, tuple[str, ...]] = {
    "anthropic": ("opus", "sonnet", "haiku"),
    "nvidia": (
        "nemotron-3-ultra",
        "deepseek-v4",
        "qwen3.5-397b",
        "glm-5",
        "kimi-k2",
        "qwen3.5-122b",
        "llama-3.1-405b",
        "nemotron-3-super",
    ),
}


def best_models(provider: str, ids: list[str]) -> list[str]:
    """Return the listed ids that match the preferred families, strongest family first."""
    ranked: list[str] = []
    for pattern in PREFERRED.get(provider, ()):
        ranked.extend(i for i in ids if pattern in i.casefold() and i not in ranked)
    return ranked


def transport_for(
    models: ModelsConfig, env: Mapping[str, str] | None = None
) -> AnthropicTransport | NvidiaTransport:
    """Return the transport for ``models.provider``, using the key from the environment."""
    source = os.environ if env is None else env
    try:
        key = get_api_key(models.api_key_env, source)
    except ConfigError as exc:
        other = next(p for p in PROVIDERS if p != models.provider)
        raise ConfigError(
            f"{exc} (provider: {models.provider}). To use {other} instead, set its key and run "
            f"with --provider {other}"
        ) from exc
    if models.provider == "anthropic":
        return AnthropicTransport(key, models)
    return NvidiaTransport(key, models)
