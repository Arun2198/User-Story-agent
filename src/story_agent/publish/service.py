"""Publish a finished run. Every path runs the same checks first."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from story_agent.config import AppConfig
from story_agent.publish.adocsv import AdoCsvPublisher
from story_agent.publish.adorest import AdoRestPublisher
from story_agent.publish.base import (
    ExternalPublisher,
    FilePublisher,
    NotEnabledError,
    Plan,
    PublishContext,
    RestClient,
)
from story_agent.publish.config import parse_destinations
from story_agent.publish.jirarest import JiraRestPublisher
from story_agent.publish.jsonout import JsonPublisher
from story_agent.publish.markdown import MarkdownPublisher
from story_agent.publish.safety import assert_no_pii, check_publishable
from story_agent.publish.view import PublishView, build_view
from story_agent.schema import RunState

FILE_TARGETS: dict[str, FilePublisher] = {
    p.name: p for p in (MarkdownPublisher(), JsonPublisher(), AdoCsvPublisher())
}
EXTERNAL_TARGETS = ("ado_rest", "jira_rest")
TARGETS = (*FILE_TARGETS, *EXTERNAL_TARGETS)
FORMAT_TARGETS = {"md": "md", "json": "json", "csv": "ado_csv"}


class UnknownTargetError(ValueError):
    """The target name is not one of ``TARGETS``."""


@dataclass(frozen=True)
class Rendered:
    """A rendered file."""

    text: str
    extension: str
    stories: int


def load_context(config: AppConfig) -> PublishContext:
    """Validate destinations.yaml and find the templates."""
    return PublishContext(parse_destinations(config.destinations), config.config_dir / "templates")


def prepare(
    state: RunState, config: AppConfig, ctx: PublishContext | None = None
) -> tuple[PublishView, PublishContext]:
    """Check the run is fit to publish, then build the view."""
    check_publishable(state, config)
    context = ctx or load_context(config)
    return build_view(state, config.standards, context.destinations.labels), context


def render_file(
    state: RunState, config: AppConfig, target: str, redactions: Mapping[str, str]
) -> Rendered:
    """Render a file target. Refuses output that leaks redacted or sensitive values."""
    publisher = FILE_TARGETS.get(target)
    if publisher is None:
        raise UnknownTargetError(
            f"{target} is not a file target; choose from {', '.join(FILE_TARGETS)}"
        )
    view, ctx = prepare(state, config)
    text = publisher.render(view, ctx)
    assert_no_pii(text, redactions)
    return Rendered(text, publisher.extension, len(view.stories))


def external_publisher(target: str, ctx: PublishContext) -> ExternalPublisher:
    """Return the publisher for an external target."""
    if target == "ado_rest":
        return AdoRestPublisher()
    if target == "jira_rest":
        return JiraRestPublisher(ctx.destinations.jira.epic_link_field)
    raise UnknownTargetError(
        f"{target} is not an external target; choose from {', '.join(EXTERNAL_TARGETS)}"
    )


def build_plan(
    state: RunState, config: AppConfig, target: str, redactions: Mapping[str, str]
) -> tuple[Plan, PublishContext]:
    """Build the requests for an external target. This sends nothing."""
    view, ctx = prepare(state, config)
    plan = external_publisher(target, ctx).plan(view, ctx)
    assert_no_pii(plan.to_json(), redactions)
    return plan, ctx


def make_rest_client(target: str, ctx: PublishContext) -> RestClient:  # noqa: ARG001  (kept for real clients)
    """Return an HTTP client for the target. None ships in this build."""
    raise NotEnabledError(
        f"writing to {target} is not enabled in this build; use --dry-run to see the exact "
        "payload, or publish the ado_csv file and import it"
    )


def load_run(runs_dir: Path, run_id: str) -> tuple[RunState, dict[str, str]]:
    """Read a finished run and its redaction map."""
    folder = runs_dir / run_id
    saved = folder / "state.json"
    if not saved.exists():
        raise FileNotFoundError(
            f"run {run_id} has not finished; continue it with: story-agent resume {run_id}"
        )
    state = RunState.model_validate(json.loads(saved.read_text(encoding="utf-8")))
    mapping = folder / "redaction_map.json"
    redactions = json.loads(mapping.read_text(encoding="utf-8")) if mapping.exists() else {}
    return state, redactions
