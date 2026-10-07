"""Publisher interfaces.

A file publisher renders a ``PublishView`` to text. An external publisher builds a plan of
requests for another system. Nothing is sent without an ``Approval`` for exactly that plan.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from string import Template
from typing import Any, Literal, Protocol

from story_agent.publish.config import DestinationsConfig
from story_agent.publish.view import PublishView


@dataclass(frozen=True)
class PublishContext:
    """Settings and templates a publisher reads."""

    destinations: DestinationsConfig
    templates_dir: Path

    def template(self, name: str) -> Template:
        """Load a ``string.Template`` from ``config/templates``."""
        return Template((self.templates_dir / name).read_text(encoding="utf-8"))


class FilePublisher(Protocol):
    """Renders a run to a file's text."""

    name: str
    extension: str

    def render(self, view: PublishView, ctx: PublishContext) -> str:
        """Return the file content."""
        ...


# ---- external systems -----------------------------------------------------------------


@dataclass(frozen=True)
class Request:
    """One HTTP request, as it would be sent."""

    method: Literal["GET", "POST", "PATCH", "PUT"]
    path: str
    body: Any = None
    content_type: str = "application/json"


@dataclass(frozen=True)
class Operation:
    """Create or update one item, found again by its idempotency tag."""

    kind: Literal["epic", "feature", "story"]
    key: str
    parent_key: str | None
    title: str
    lookup: Request
    create: Request
    update: Request


@dataclass(frozen=True)
class Plan:
    """Everything an external publish would do, in order."""

    target: str
    operations: tuple[Operation, ...]

    def to_json(self) -> str:
        """Return the exact payload for ``--dry-run`` and for the approval summary."""
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)

    @property
    def digest(self) -> str:
        """A hash of the plan; an approval is valid for one digest only."""
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Approval:
    """A person's go-ahead for one plan. Create it only with ``approve``."""

    plan_digest: str
    approved_by: str


class ApprovalRequiredError(RuntimeError):
    """An external write was attempted without a matching approval."""


class NotEnabledError(RuntimeError):
    """The target has a plan but no HTTP client in this build."""


def approve(plan: Plan, approved_by: str) -> Approval:
    """Record that a person approved this exact plan."""
    return Approval(plan.digest, approved_by)


class Response(Protocol):
    """What a REST call returns."""

    status: int
    body: Any


class RestClient(Protocol):
    """Sends requests to the external system. No implementation ships in this build."""

    def send(self, request: Request) -> Response:
        """Send one request."""
        ...


class ExternalPublisher(Protocol):
    """Builds a plan for an external system and reads its answers."""

    name: str

    def plan(self, view: PublishView, ctx: PublishContext) -> Plan:
        """Return the requests for every epic, feature and story."""
        ...

    def found_id(self, response: Response) -> str | None:
        """Return the id of the existing item in a lookup response, if any."""
        ...

    def created_id(self, response: Response) -> str:
        """Return the id of a newly created item."""
        ...

    def with_parent(self, request: Request, parent_id: str) -> Request:
        """Return the request with the parent link filled in."""
        ...

    def with_id(self, request: Request, item_id: str) -> Request:
        """Return an update request addressed to an existing item."""
        ...


@dataclass
class ApplyReport:
    """What an approved publish did."""

    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)


def apply_plan(
    publisher: ExternalPublisher, plan: Plan, client: RestClient, approval: Approval | None
) -> ApplyReport:
    """Create or update every item. A re-run finds items by key and updates them."""
    if approval is None or approval.plan_digest != plan.digest:
        raise ApprovalRequiredError("a person must approve this exact payload before it is sent")
    ids: dict[str, str] = {}
    report = ApplyReport()
    for op in plan.operations:
        existing = publisher.found_id(client.send(op.lookup))
        parent = ids.get(op.parent_key) if op.parent_key else None
        if existing is not None:
            request = publisher.with_id(op.update, existing)
            ids[op.key] = existing
            report.updated.append(op.key)
        else:
            request = op.create
            report.created.append(op.key)
        if parent is not None and existing is None:  # an item keeps the parent it was created with
            request = publisher.with_parent(request, parent)
        response = client.send(request)
        if existing is None:
            ids[op.key] = publisher.created_id(response)
    return report
