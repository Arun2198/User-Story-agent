"""Jira REST publisher: the plan and field mapping. No HTTP client ships yet.

Jira has no feature level, so a feature becomes a label on its stories. Descriptions use
Jira wiki markup (REST API v2).
"""

from __future__ import annotations

import copy
import re
from dataclasses import replace
from urllib.parse import quote

from story_agent.publish.base import Operation, Plan, PublishContext, Request, Response
from story_agent.publish.html import criteria_wiki, description_wiki, wiki
from story_agent.publish.view import PublishView, StoryView


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")


class JiraRestPublisher:
    """Builds Jira issue requests."""

    name = "jira_rest"

    def __init__(self, epic_link_field: str = "parent") -> None:
        """Set the field that links a story to its epic."""
        self.epic_link_field = epic_link_field

    def plan(self, view: PublishView, ctx: PublishContext) -> Plan:
        """Return a create-or-update operation for every epic and story."""
        jira = ctx.destinations.jira
        prefix = ctx.destinations.labels.key_prefix
        operations: list[Operation] = []
        for epic in view.epics:
            epic_labels = [ctx.destinations.labels.import_, prefix + epic.key]
            fields: dict[str, object] = {"summary": wiki(epic.name), "labels": epic_labels}
            operations.append(
                self._op("epic", epic.key, None, epic.name, jira.issue_types.epic, fields, ctx)
            )
            operations.extend(
                self._story_op(story, feature.name, epic.key, ctx)
                for feature in epic.features
                for story in feature.stories
            )
        return Plan(self.name, tuple(operations))

    def _story_op(
        self, story: StoryView, feature: str | None, epic_key: str, ctx: PublishContext
    ) -> Operation:
        jira = ctx.destinations.jira
        own_field = jira.acceptance_criteria_field
        labels = list(story.labels)
        if feature:
            labels.append(f"feature-{_slug(feature)}")
        fields: dict[str, object] = {
            "summary": wiki(story.title),
            "description": description_wiki(story, ctx, with_criteria=own_field is None),
            "labels": labels,
            "priority": {"name": jira.priority[story.priority]},
        }
        if own_field is not None:
            fields[own_field] = criteria_wiki(story)
        if story.estimate is not None:
            fields[jira.story_points_field] = story.estimate
        return self._op(
            "story", story.key, epic_key, story.title, jira.issue_types.story, fields, ctx
        )

    @staticmethod
    def _op(  # noqa: PLR0913, PLR0917  (an operation needs all of these)
        kind: str,
        key: str,
        parent: str | None,
        title: str,
        issue_type: str,
        fields: dict[str, object],
        ctx: PublishContext,
    ) -> Operation:
        jira = ctx.destinations.jira
        tag = f"{ctx.destinations.labels.key_prefix}{key}"
        jql = f'project = "{jira.project_key}" AND labels = "{tag}"'
        create = {
            "fields": {
                "project": {"key": jira.project_key},
                "issuetype": {"name": issue_type},
                **fields,
            }
        }
        return Operation(
            kind,  # type: ignore[arg-type]
            key,
            parent,
            title,
            Request("GET", f"/rest/api/2/search?jql={quote(jql)}&fields=key&maxResults=1"),
            Request("POST", "/rest/api/2/issue", create),
            Request("PUT", "/rest/api/2/issue/{id}", {"fields": fields}),
        )

    def found_id(self, response: Response) -> str | None:
        """Return the key of the first issue the search matched."""
        issues = response.body.get("issues", []) if isinstance(response.body, dict) else []
        return str(issues[0]["key"]) if issues else None

    def created_id(self, response: Response) -> str:
        """Return the key of the created issue."""
        return str(response.body["key"])

    def with_parent(self, request: Request, parent_id: str) -> Request:
        """Link a create request to its epic. ``parent`` takes a key object, other fields a key."""
        body = copy.deepcopy(request.body)
        field = self.epic_link_field
        body["fields"][field] = {"key": parent_id} if field == "parent" else parent_id
        return replace(request, body=body)

    def with_id(self, request: Request, item_id: str) -> Request:
        """Address an update request to an existing issue."""
        return replace(request, path=request.path.replace("{id}", item_id))
