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


class ModelsConfig(_Cfg):
    """Model names and call settings."""

    generator: str
    judge: str
    base_url: str = "https://integrate.api.nvidia.com/v1"
    api_key_env: str = "NVIDIA_API_KEY"
    # A different key for some models, as model id -> environment variable name. A model with no
    # entry, or whose variable is not set, uses the key from api_key_env.
    model_api_key_env: dict[str, str] = Field(default_factory=dict)
    structured_output: Literal["auto", "guided_json", "json_schema", "none"] = "auto"
    extra_body: dict[str, dict[str, Any]] = Field(default_factory=dict)
    temperature: float | None = None
    max_tokens: int | None = None  # None: no per-call limit is sent
    timeout_s: float = 60.0
    max_retries: int = Field(default=3, ge=0)
    backoff_base_s: float = 1.0
    prices: dict[str, Price] = Field(default_factory=dict)


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


def load_config(config_dir: Path | None = None) -> AppConfig:
    """Load and validate every top-level config file."""
    root = config_dir or default_config_dir()
    return AppConfig(
        config_dir=root,
        models=ModelsConfig.model_validate(load_yaml(root / "models.yaml")),
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
