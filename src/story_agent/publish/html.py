"""Rich-text fragments for destinations: HTML for Azure DevOps, wiki markup for Jira."""

from __future__ import annotations

from html import escape

from story_agent.publish.base import PublishContext
from story_agent.publish.view import StoryView


def _line(text: str) -> str:
    return " ".join(text.split())


def criteria_html(story: StoryView) -> str:
    """Return the numbered criteria as an HTML list, or an empty string."""
    if not story.criteria:
        return ""
    items = "".join(
        f"<li>Given {escape(_line(c.given))}, when {escape(_line(c.when))}, "
        f"then {escape(_line(c.then))}</li>"
        for c in story.criteria
    )
    return f"<ol>{items}</ol>"


def description_html(story: StoryView, ctx: PublishContext, with_criteria: bool) -> str:
    """Return the ADO description: statement, criteria (optional), source, requirements."""
    criteria = (
        f"<h4>Acceptance criteria</h4>{criteria_html(story)}\n"
        if (with_criteria and story.criteria)
        else ""
    )
    source = "<ul>" + "".join(f"<li>{escape(_line(p))}</li>" for p in story.provenance) + "</ul>"
    return (
        ctx.template(ctx.destinations.templates.ado)
        .substitute(
            statement=escape(_line(story.statement)),
            criteria=criteria,
            provenance=source,
            requirements=escape(", ".join(story.requirement_ids)),
        )
        .strip()
    )


_WIKI_SPECIAL = str.maketrans({c: f"\\{c}" for c in "{}[]|"})


def wiki(text: str) -> str:
    """Escape characters that Jira wiki markup would treat as markup."""
    return _line(text).translate(_WIKI_SPECIAL)


def criteria_wiki(story: StoryView) -> str:
    """Return the numbered criteria as a Jira wiki list, or an empty string."""
    return "\n".join(
        f"# Given {wiki(c.given)}, when {wiki(c.when)}, then {wiki(c.then)}" for c in story.criteria
    )


def description_wiki(story: StoryView, ctx: PublishContext, with_criteria: bool) -> str:
    """Return the Jira description in wiki markup."""
    criteria = ""
    if with_criteria and story.criteria:
        criteria = f"h4. Acceptance criteria\n{criteria_wiki(story)}\n"
    source = "\n".join(f"* {wiki(p)}" for p in story.provenance)
    return (
        ctx.template(ctx.destinations.templates.jira)
        .substitute(
            statement=wiki(story.statement),
            criteria=criteria,
            provenance=source,
            requirements=", ".join(story.requirement_ids),
        )
        .strip()
    )
