"""Typed view of ``config/destinations.yaml``."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from story_agent.config import ConfigError

Priorities = dict[Literal["must", "should", "could", "wont"], int | str]


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Labels(_Cfg):
    """Labels added to every published story."""

    import_: str = Field(alias="import")
    needs_clarification: str
    key_prefix: str = Field(default="sa-", pattern=r"^[A-Za-z0-9_-]{1,16}$")


class Templates(_Cfg):
    """File names under ``config/templates/``."""

    markdown: str
    ado: str
    jira: str


class AdoProcess(_Cfg):
    """Work item names and field names for one ADO process template."""

    epic: str
    feature: str
    story: str
    points_field: str
    points_column: str
    criteria_field: str
    criteria_column: str


class AdoConfig(_Cfg):
    """Azure DevOps settings."""

    process: Literal["agile", "scrum", "cmmi"]
    organization_url: str = ""
    project: str = ""
    token_env: str
    acceptance_criteria: Literal["field", "description"]
    default_feature: str
    priority: dict[str, int]
    processes: dict[str, AdoProcess]

    @property
    def names(self) -> AdoProcess:
        """Return the settings of the chosen process."""
        return self.processes[self.process]


class IssueTypes(_Cfg):
    """Jira issue type names."""

    epic: str
    story: str


class JiraConfig(_Cfg):
    """Jira settings."""

    base_url: str = ""
    project_key: str = ""
    email_env: str
    token_env: str
    issue_types: IssueTypes
    priority: dict[str, str]
    story_points_field: str
    epic_link_field: str
    acceptance_criteria_field: str | None = None


class DestinationsConfig(_Cfg):
    """All destinations."""

    labels: Labels
    templates: Templates
    ado: AdoConfig
    jira: JiraConfig


def parse_destinations(raw: dict[str, Any]) -> DestinationsConfig:
    """Validate the raw destinations mapping."""
    try:
        config = DestinationsConfig.model_validate(raw)
    except ValidationError as exc:
        fields = ", ".join(".".join(str(p) for p in e["loc"]) for e in exc.errors())
        raise ConfigError(f"invalid destinations.yaml: check {fields}") from exc
    if config.ado.process not in config.ado.processes:
        raise ConfigError(f"destinations.yaml: ado.process {config.ado.process} is not defined")
    for name in ("must", "should", "could", "wont"):
        if name not in config.ado.priority or name not in config.jira.priority:
            raise ConfigError(f"destinations.yaml: priority map is missing {name}")
    return config
