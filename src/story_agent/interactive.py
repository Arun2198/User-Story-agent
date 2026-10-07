"""Answers the run's pauses by asking a person in the terminal."""

from __future__ import annotations

from typing import Any

import typer
from pydantic import BaseModel

from story_agent.replies import AnswersReply, ConflictsReply, GateReply, MemoryReply, ReviewReply

ANSWER_HINT = "Type a number, your own answer, 'use your judgment', 'defer' or 'n/a'."


def _ask(text: str, default: str | None = None, choices: list[str] | None = None) -> str:
    label = f"{text} [{'/'.join(choices)}]" if choices else text
    while True:
        value = str(typer.prompt(label, default=default, show_default=default is not None))
        value = value.strip()
        if choices is None or value in choices:
            return value
        typer.echo(f"Please choose one of: {', '.join(choices)}")


def _remembered(default: dict[str, Any]) -> str:
    stale = " (STALE, please re-confirm)" if default["stale"] else ""
    return (
        f"  Previously you said: {default['value']} (confirmed {default['last_confirmed_at']})"
        f"{stale}\n  Still valid? yes / no, or type a new answer."
    )


def render_story(story: dict[str, Any]) -> str:
    """Return a story as plain text for review."""
    lines = [
        f"{story['id']}  {story['title']}  [{story['priority']}]",
        f"  As a {story['persona']}, I want {story['want']}, so that {story['benefit']}.",
    ]
    for n, c in enumerate(story["acceptance_criteria"], 1):
        lines.append(
            f"  AC{n} ({c['kind']}): Given {c['given']}; When {c['when']}; Then {c['then']}"
        )
    if story["assumptions"]:
        lines.append("  Assumptions: " + "; ".join(story["assumptions"]))
    if story["open_questions"]:
        lines.append("  Open questions: " + "; ".join(story["open_questions"]))
    lines.append(f"  Requirements: {', '.join(story['requirement_ids'])}")
    return "\n".join(lines)


class PromptResponder:
    """Asks the person at the terminal. ``stopped`` is set if they walk away."""

    def __init__(self) -> None:
        """Start with nothing stopped."""
        self.stopped = ""

    def respond(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Ask one pause. Ctrl-C leaves the run paused and saved."""
        try:
            reply: BaseModel = getattr(self, f"_{payload['kind']}")(payload)
            return reply.model_dump(mode="json")
        except (typer.Abort, EOFError):
            self.stopped = "stopped by the user"
            return None

    def _note(self, payload: dict[str, Any]) -> None:
        if payload.get("note"):
            typer.echo(f"\n{payload['note']}")

    def _answers(self, payload: dict[str, Any]) -> AnswersReply:
        self._note(payload)
        typer.echo(f"\nClarifying questions, round {payload['round']} of {payload['rounds_max']}.")
        typer.echo(ANSWER_HINT)
        answers: dict[str, str] = {}
        for n, q in enumerate(payload["questions"], 1):
            typer.echo(f"\n{n}. {q['question']}")
            typer.echo(f"   Why it matters: {q['why_it_matters']}")
            for i, option in enumerate(q["options"], 1):
                typer.echo(f"   {i}) {option}")
            if q["remembered_default"] is not None:
                typer.echo(_remembered(q["remembered_default"]))
            answers[q["id"]] = _ask("Your answer").strip()
        extra = _ask(f"\n{payload['free_text_prompt']} (Enter to skip)", default="")
        return AnswersReply(answers=answers, free_text=extra.strip())

    def _conflicts(self, payload: dict[str, Any]) -> ConflictsReply:
        resolutions: dict[str, Any] = {}
        for c in payload["conflicts"]:
            typer.echo(
                f"\nYou answered '{c['new']}' for {c['category']}, but memory says "
                f"'{c['remembered']}'."
            )
            resolutions[c["question_id"]] = _ask(
                "Replace the saved answer, or keep it and treat this run as an exception?",
                default="exception",
                choices=["replace", "exception"],
            )
        return ConflictsReply.model_validate({"resolutions": resolutions})

    def _gate(self, payload: dict[str, Any]) -> GateReply:
        self._note(payload)
        typer.echo(f"\n{payload['summary']}")
        left = payload["rounds_used"] < payload["rounds_max"]
        choices = ["go"] if payload["ready"] else []
        if left:
            choices.append("more")
        choices += ["judgment", "defer"]
        typer.echo(
            "\ngo = start drafting; more = another round of questions; "
            "judgment = let me assume the open items; defer = leave them as open questions"
        )
        decision = _ask("Your decision", default=choices[0], choices=choices)
        return GateReply.model_validate({"decision": decision, "confirmed_by": "user"})

    def _review(self, payload: dict[str, Any]) -> ReviewReply:
        self._note(payload)
        errors = [f for f in payload["findings"] if f["severity"] == "error"]
        if errors:
            typer.echo("\nOpen problems the critic found:")
            for f in errors:
                typer.echo(f"  {f['code']} {f['location']}: {f['message']}")
        actions: list[dict[str, Any]] = []
        for story in payload["stories"]:
            typer.echo(f"\n{render_story(story)}")
            choice = _ask("approve, edit or reject", default="approve", choices=_ACTIONS)
            action: dict[str, Any] = {"story_id": story["id"], "action": choice}
            if choice == "edit":
                edits = {
                    name: _ask(f"{name}", default=story[name])
                    for name in ("title", "want", "benefit")
                }
                action["edits"] = {k: v for k, v in edits.items() if v != story[k]}
            elif choice == "reject":
                action["reason"] = _ask("Reason (Enter to skip)", default="")
            actions.append(action)
        return ReviewReply.model_validate({"actions": actions})

    def _memory(self, payload: dict[str, Any]) -> MemoryReply:
        typer.echo("\nI can remember these for next time. Nothing is saved unless you approve it.")
        decisions: dict[str, Any] = {}
        for p in payload["proposals"]:
            typer.echo(f"\n[{p['type']}] {p['content']}  ({p['action']}: {p['reason']})")
            if p["replaces"]:
                typer.echo(f"  This replaces: {p['replaces']}")
            choice = _ask("approve, edit or reject", default="reject", choices=_ACTIONS)
            decision: dict[str, Any] = {"action": choice}
            if choice == "edit":
                decision["content"] = _ask("New wording", default=p["content"])
            decisions[p["id"]] = decision
        return MemoryReply.model_validate({"decisions": decisions})


_ACTIONS = ["approve", "edit", "reject"]
