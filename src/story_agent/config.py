"""Configuration loading. Secrets come from the environment, never from files."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class ConfigError(RuntimeError):
    """Raised when configuration is missing or invalid."""


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Price(_Cfg):
    """Token prices in USD per million tokens."""

    input: float
    output: float


Provider = Literal["anthropic", "nvidia"]
PROVIDERS: tuple[Provider, ...] = ("anthropic", "nvidia")
PROVIDER_ENV = "STORY_AGENT_PROVIDER"


class ModelsConfig(_Cfg):
    """The models and call settings for the provider in use."""

    provider: Provider
    generator: str
    judge: str
    api_key_env: str
    base_url: str = ""
    structured_output: Literal["auto", "guided_json", "json_schema", "none"] = "auto"
    extra_body: dict[str, dict[str, Any]] = Field(default_factory=dict)
    temperature: float | None = None
    max_tokens: int | None = None  # None: no per-call limit is sent (where the provider allows it)
    timeout_s: float = 600.0
    max_retries: int = Field(default=3, ge=0)
    backoff_base_s: float = 1.0
    prices: dict[str, Price] = Field(default_factory=dict)


class ProviderBlock(_Cfg):
    """One provider's section of models.yaml. Unset call settings use the shared ones."""

    api_key_env: str
    generator: str
    judge: str
    base_url: str = ""
    structured_output: Literal["auto", "guided_json", "json_schema", "none"] = "auto"
    extra_body: dict[str, dict[str, Any]] = Field(default_factory=dict)
    temperature: float | None = None
    max_tokens: int | None = None
    timeout_s: float | None = None
    prices: dict[str, Price] = Field(default_factory=dict)


class ModelsFile(_Cfg):
    """models.yaml: shared call settings and one block per provider."""

    provider: Literal["auto", "anthropic", "nvidia"] = "auto"
    timeout_s: float = 600.0
    max_retries: int = Field(default=3, ge=0)
    backoff_base_s: float = 1.0
    max_tokens: int | None = None
    providers: dict[Provider, ProviderBlock]


def resolve_models(raw: dict[str, Any], env: Mapping[str, str] | None = None) -> ModelsConfig:
    """Pick the provider and merge its block with the shared settings.

    The provider is, in order: ``STORY_AGENT_PROVIDER`` in the environment, then ``provider`` in
    models.yaml. ``auto`` means the first provider whose API key variable is set, and the first
    one listed when none is, so offline work needs no key.
    """
    source = os.environ if env is None else env
    try:
        file = ModelsFile.model_validate(raw)
    except ValueError as exc:
        raise ConfigError(f"invalid models.yaml: {type(exc).__name__}") from exc
    wanted = source.get(PROVIDER_ENV, "").strip().casefold() or file.provider
    if wanted not in {"auto", *PROVIDERS}:
        raise ConfigError(f"{PROVIDER_ENV} must be auto, anthropic or nvidia, not {wanted!r}")
    if wanted == "auto":
        keyed = [
            p
            for p in PROVIDERS
            if p in file.providers and source.get(file.providers[p].api_key_env, "").strip()
        ]
        wanted = keyed[0] if keyed else next(p for p in PROVIDERS if p in file.providers)
    if wanted not in file.providers:
        raise ConfigError(f"models.yaml has no section for provider {wanted}")
    block = file.providers[wanted]
    if wanted == "nvidia" and not block.base_url:
        raise ConfigError("models.yaml: the nvidia section needs base_url")
    return ModelsConfig(
        provider=wanted,
        generator=block.generator,
        judge=block.judge,
        api_key_env=block.api_key_env,
        base_url=block.base_url,
        structured_output=block.structured_output,
        extra_body=block.extra_body,
        temperature=block.temperature,
        max_tokens=block.max_tokens if block.max_tokens is not None else file.max_tokens,
        timeout_s=block.timeout_s if block.timeout_s is not None else file.timeout_s,
        max_retries=file.max_retries,
        backoff_base_s=file.backoff_base_s,
        prices=block.prices,
    )


class BudgetConfig(_Cfg):
    """Per-run limits."""

    max_tokens_per_run: int | None = None  # None: no token cap; cost and steps still apply
    max_cost_usd_per_run: float
    max_steps_per_run: int


class LimitsConfig(_Cfg):
    """Input size limits."""

    max_scenario_chars: int
    max_notes_chars: int


class GroundingConfig(_Cfg):
    """Grounding verifier settings."""

    fuzzy_min_ratio: float = Field(ge=0.0, le=1.0)
    min_excerpt_chars: int = Field(default=8, ge=1)
    fuzzy_min_words: int = Field(default=4, ge=1)


class ScopeConfig(_Cfg):
    """Scope guard settings."""

    refusal_message: str


class RedactionConfig(_Cfg):
    """Redaction settings."""

    restore_in_outputs: bool = False


class GuardrailsConfig(_Cfg):
    """Guardrail settings."""

    budget: BudgetConfig
    limits: LimitsConfig
    grounding: GroundingConfig
    scope: ScopeConfig
    redaction: RedactionConfig = Field(default_factory=RedactionConfig)


class RecallConfig(_Cfg):
    """Memory recall settings."""

    max_entries: int = Field(default=12, ge=1)
    min_matched_terms: int = Field(default=2, ge=1)
    max_query_terms: int = Field(default=30, ge=1)


class WriteConfig(_Cfg):
    """Memory write settings."""

    max_content_chars: int = Field(default=300, ge=1)
    max_scenario_overlap_chars: int = Field(default=60, ge=10)


class MemoryConfig(_Cfg):
    """Memory settings."""

    recall: RecallConfig = Field(default_factory=RecallConfig)
    write: WriteConfig = Field(default_factory=WriteConfig)
    ttl_days: dict[str, int] = Field(default_factory=dict)
    nfr_categories: list[str] = Field(default_factory=list)


class AppConfig(_Cfg):
    """All configuration the app needs, loaded once and passed in."""

    config_dir: Path
    models: ModelsConfig
    guardrails: GuardrailsConfig
    memory: MemoryConfig
    standards: dict[str, Any]
    hooks: dict[str, Any]
    destinations: dict[str, Any]
    evals: dict[str, Any]


def default_config_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return the config directory from the environment or ./config."""
    env = os.environ if env is None else env
    return Path(env.get("STORY_AGENT_CONFIG_DIR", "config"))


def load_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML mapping from ``path``."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"missing config file: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping")
    return data


def load_config(config_dir: Path | None = None, env: Mapping[str, str] | None = None) -> AppConfig:
    """Load and validate every top-level config file."""
    root = config_dir or default_config_dir()
    return AppConfig(
        config_dir=root,
        models=resolve_models(load_yaml(root / "models.yaml"), env),
        guardrails=GuardrailsConfig.model_validate(load_yaml(root / "guardrails.yaml")),
        memory=MemoryConfig.model_validate(load_yaml(root / "memory.yaml")),
        standards=load_yaml(root / "standards.yaml"),
        hooks=load_yaml(root / "hooks.yaml"),
        destinations=load_yaml(root / "destinations.yaml"),
        evals=load_yaml(root / "evals.yaml"),
    )


def get_api_key(name: str, env: Mapping[str, str] | None = None) -> str:
    """Return the API key held in the environment variable ``name``, or raise."""
    env = os.environ if env is None else env
    key = env.get(name, "").strip()
    if not key:
        raise ConfigError(f"{name} is not set")
    return key
