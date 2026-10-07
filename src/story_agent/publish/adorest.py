"""Azure DevOps REST publisher: the plan and field mapping. No HTTP client ships yet."""

from __future__ import annotations

import copy
from dataclasses import replace
from urllib.parse import quote

from story_agent.publish.adocsv import features_of
from story_agent.publish.base import Operation, Plan, PublishContext, Request, Response
from story_agent.publish.html import criteria_html, description_html
from story_agent.publish.view import PublishView, StoryView

API = "api-version=7.1"
PATCH_TYPE = "application/json-patch+json"


def _field(name: str, value: object) -> dict[str, object]:
    return {"op": "add", "path": f"/fields/{name}", "value": value}


class AdoRestPublisher:
    """Builds Azure DevOps work item requests."""

    name = "ado_rest"

    def plan(self, view: PublishView, ctx: PublishContext) -> Plan:
        """Return a create-or-update operation for every epic, feature and story."""
        ado = ctx.destinations.ado
        names = ado.names
        label = ctx.destinations.labels
        in_field = ado.acceptance_criteria == "field"
        operations: list[Operation] = []
        for epic in view.epics:
            epic_tags = [label.import_, label.key_prefix + epic.key]
            operations.append(
                self._op(
                    "epic",
                    epic.key,
                    None,
                    epic.name,
                    names.epic,
                    ado.project,
                    [
                        _field("System.Title", epic.name),
                        _field("System.Tags", "; ".join(epic_tags)),
                    ],
                    label.key_prefix,
                )
            )
            for feature_name, feature_key, stories in features_of(epic, ado.default_feature):
                feature_tags = [label.import_, label.key_prefix + feature_key]
                operations.append(
                    self._op(
                        "feature",
                        feature_key,
                        epic.key,
                        feature_name,
                        names.feature,
                        ado.project,
                        [
                            _field("System.Title", feature_name),
                            _field("System.Tags", "; ".join(feature_tags)),
                        ],
                        label.key_prefix,
                    )
                )
                operations.extend(
                    self._story_op(story, feature_key, ctx, in_field) for story in stories
                )
        return Plan(self.name, tuple(operations))

    def _story_op(
        self, story: StoryView, parent_key: str, ctx: PublishContext, in_field: bool
    ) -> Operation:
        ado = ctx.destinations.ado
        names = ado.names
        fields = [
            _field("System.Title", story.title),
            _field("System.Description", description_html(story, ctx, with_criteria=not in_field)),
            _field("Microsoft.VSTS.Common.Priority", ado.priority[story.priority]),
            _field("System.Tags", "; ".join(story.labels)),
        ]
        if in_field:
            fields.append(_field(names.criteria_field, criteria_html(story)))
        if story.estimate is not None:
            fields.append(_field(names.points_field, story.estimate))
        return self._op(
            "story",
            story.key,
            parent_key,
            story.title,
            names.story,
            ado.project,
            fields,
            ctx.destinations.labels.key_prefix,
        )

    @staticmethod
    def _op(  # noqa: PLR0913, PLR0917  (an operation needs all of these)
        kind: str,
        key: str,
        parent: str | None,
        title: str,
        item_type: str,
        project: str,
        patch: list[dict[str, object]],
        key_prefix: str,
    ) -> Operation:
        base = f"/{quote(project)}/_apis/wit"
        tag = f"{key_prefix}{key}".replace("'", "''")  # prefix is validated; escape anyway
        wiql = (
            "SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project "  # noqa: S608  # nosec B608
            f"AND [System.Tags] CONTAINS '{tag}'"
        )
        return Operation(
            kind,  # type: ignore[arg-type]
            key,
            parent,
            title,
            Request("POST", f"{base}/wiql?{API}", {"query": wiql}),
            Request("POST", f"{base}/workitems/${quote(item_type)}?{API}", patch, PATCH_TYPE),
            Request("PATCH", f"{base}/workitems/{{id}}?{API}", patch, PATCH_TYPE),
        )

    def found_id(self, response: Response) -> str | None:
        """Return the id of the first work item the query matched."""
        items = response.body.get("workItems", []) if isinstance(response.body, dict) else []
        return str(items[0]["id"]) if items else None

    def created_id(self, response: Response) -> str:
        """Return the id of the created work item."""
        return str(response.body["id"])

    def with_parent(self, request: Request, parent_id: str) -> Request:
        """Add the hierarchy link to a create request."""
        body = copy.deepcopy(request.body)
        body.append(
            {
                "op": "add",
                "path": "/relations/-",
                "value": {
                    "rel": "System.LinkTypes.Hierarchy-Reverse",
                    "url": f"{{organization_url}}/_apis/wit/workitems/{parent_id}",
                },
            }
        )
        return replace(request, body=body)

    def with_id(self, request: Request, item_id: str) -> Request:
        """Address an update request to an existing work item."""
        return replace(request, path=request.path.replace("{id}", item_id))
