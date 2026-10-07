"""CSV in the Azure DevOps import format.

Hierarchy uses the Title 1, Title 2, Title 3 columns (epic, feature, story). ADO needs
Epic > Feature > Story, so a story with no feature goes under the configured default feature.
"""

from __future__ import annotations

import csv
import io

from story_agent.publish.base import PublishContext
from story_agent.publish.html import criteria_html, description_html
from story_agent.publish.view import EpicView, PublishView, StoryView

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value: str) -> str:
    """Stop spreadsheets from running a cell as a formula."""
    return "'" + value if value.startswith(_FORMULA_START) else value


def features_of(epic: EpicView, default: str) -> list[tuple[str, str, list[StoryView]]]:
    """Return ``(name, key, stories)`` per feature, folding unassigned stories into ``default``."""
    keys: dict[str, str] = {}
    grouped: dict[str, list[StoryView]] = {}
    for feature in epic.features:
        name = feature.name or default
        keys.setdefault(name, feature.key)
        grouped.setdefault(name, []).extend(feature.stories)
    return [(name, keys[name], stories) for name, stories in grouped.items()]


class AdoCsvPublisher:
    """Writes an ADO work item import file."""

    name = "ado_csv"
    extension = "csv"

    def render(self, view: PublishView, ctx: PublishContext) -> str:
        """Return the CSV text."""
        ado = ctx.destinations.ado
        names = ado.names
        label = ctx.destinations.labels
        in_field = ado.acceptance_criteria == "field"
        header = ["Work Item Type", "Title 1", "Title 2", "Title 3", "Description"]
        if in_field:
            header.append(names.criteria_column)
        header += ["Priority", names.points_column, "Tags"]
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(header)

        def row(  # noqa: PLR0913, PLR0917  (one CSV row has these columns)
            kind: str,
            level: int,
            title: str,
            description: str,
            criteria: str,
            priority: str,
            points: str,
            tags: list[str],
        ) -> None:
            titles = ["", "", ""]
            titles[level] = safe_cell(" ".join(title.split()))
            cells = [kind, *titles, safe_cell(description)]
            if in_field:
                cells.append(safe_cell(criteria))
            writer.writerow([*cells, priority, points, "; ".join(tags)])

        for epic in view.epics:
            row(
                names.epic,
                0,
                epic.name,
                "",
                "",
                "",
                "",
                [label.import_, label.key_prefix + epic.key],
            )
            for name, key, stories in features_of(epic, ado.default_feature):
                feature_tags = [label.import_, label.key_prefix + key]
                row(names.feature, 1, name, "", "", "", "", feature_tags)
                for story in stories:
                    row(
                        names.story,
                        2,
                        story.title,
                        description_html(story, ctx, with_criteria=not in_field),
                        criteria_html(story),
                        str(ado.priority[story.priority]),
                        "" if story.estimate is None else str(story.estimate),
                        list(story.labels),
                    )
        return out.getvalue()
