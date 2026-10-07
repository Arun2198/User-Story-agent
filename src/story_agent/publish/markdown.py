"""Markdown publisher."""

from __future__ import annotations

from story_agent.publish.base import PublishContext
from story_agent.publish.view import PublishView, StoryView


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _section(title: str, items: tuple[str, ...]) -> str:
    if not items:
        return ""
    return f"\n{title}:\n" + "\n".join(f"- {_one_line(i)}" for i in items) + "\n"


def render_story(story: StoryView, ctx: PublishContext) -> str:
    """Return one story as a markdown block."""
    meta = [f"Priority: {story.priority}"]
    if story.estimate is not None:
        meta.append(f"Estimate: {story.estimate}")
    meta.append(f"Confidence: {story.confidence:.2f}")
    criteria = ""
    if story.criteria:
        lines = (f"{c.n}. Given {c.given}, when {c.when}, then {c.then}" for c in story.criteria)
        criteria = "\nAcceptance criteria:\n" + "\n".join(_one_line(x) for x in lines) + "\n"
    notes = (
        _section("Non-functional requirements", story.nfrs)
        + _section("Dependencies", story.dependencies)
        + _section("Assumptions", story.assumptions)
        + _section("Open questions", story.open_questions)
    )
    body = ctx.template(ctx.destinations.templates.markdown).substitute(
        statement=_one_line(story.statement),
        meta=" | ".join(meta),
        criteria=criteria,
        notes=notes,
        provenance="\n".join(f"- {_one_line(p)}" for p in story.provenance),
        requirements=", ".join(story.requirement_ids),
    )
    return f"#### {story.id}: {_one_line(story.title)}\n\n{body.rstrip()}\n"


class MarkdownPublisher:
    """Writes the run as one markdown document."""

    name = "md"
    extension = "md"

    def render(self, view: PublishView, ctx: PublishContext) -> str:
        """Return the document."""
        out = [
            "# User stories",
            "",
            f"Run: {view.run_id} | Workspace: {view.workspace} | Stories: {len(view.stories)}",
            "",
        ]
        for epic in view.epics:
            out += [f"## Epic: {_one_line(epic.name)} ({epic.prefix})", ""]
            for feature in epic.features:
                if feature.name:
                    out += [f"### Feature: {_one_line(feature.name)}", ""]
                for story in feature.stories:
                    out += [render_story(story, ctx)]
        out += ["## Requirements", ""]
        out += ["| ID | Requirement | Basis |", "|---|---|---|"]
        for r in view.requirements:
            basis = "assumed (your judgment)" if r.assumed else "stated or confirmed"
            out.append(f"| {r.id} | {_one_line(r.text).replace('|', '/')} | {basis} |")
        questions = [(s.id, q) for s in view.stories for q in s.open_questions]
        if questions:
            out += ["", "## Open questions", ""]
            out += [f"- {sid}: {_one_line(q)}" for sid, q in questions]
        if view.skipped:
            out += ["", f"Not published (rejected): {', '.join(view.skipped)}"]
        return "\n".join(out).rstrip() + "\n"
