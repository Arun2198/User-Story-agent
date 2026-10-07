"""The ``online`` section of ``config/evals.yaml``."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from story_agent.config import ConfigError

SinkName = Literal["none", "jsonl", "otlp_http", "langfuse", "warehouse"]


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JudgeSampling(_Cfg):
    """Sampled LLM-judge scoring of live runs."""

    enabled: bool = False
    sample_rate: float = Field(default=0.0, ge=0.0, le=1.0)


class Tolerance(_Cfg):
    """How far a metric may move from baseline: an absolute amount or a fraction."""

    abs: float | None = Field(default=None, ge=0.0)
    rel: float | None = Field(default=None, ge=0.0)


class DriftConfig(_Cfg):
    """Drift check against an offline baseline."""

    baseline_mode: Literal["live", "offline"] = "live"
    baseline_name: str = "app"
    min_runs: int = Field(default=20, ge=1)
    tolerances: dict[str, Tolerance] = Field(default_factory=dict)


class OnlineConfig(_Cfg):
    """Online evaluation. Off unless ``enabled`` is true and a sink is chosen."""

    enabled: bool = False
    sink: SinkName = "none"
    trace_sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    path: str = "online/records.jsonl"
    judge: JudgeSampling = Field(default_factory=JudgeSampling)
    drift: DriftConfig = Field(default_factory=DriftConfig)


def parse_online(evals: dict[str, Any]) -> OnlineConfig:
    """Validate the ``online`` section. A missing section means disabled."""
    try:
        config = OnlineConfig.model_validate(evals.get("online", {}))
    except ValidationError as exc:
        fields = ", ".join(".".join(str(p) for p in e["loc"]) for e in exc.errors())
        raise ConfigError(f"invalid online section in evals.yaml: check {fields}") from exc
    if config.enabled and config.sink == "none":
        raise ConfigError("online.enabled is true but online.sink is none")
    return config
