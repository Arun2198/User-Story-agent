"""Parse and apply answers, free text and --answers files.

Answer text is user data. It must pass the component pre-hooks (redaction and
injection scan) before it can be parsed or applied; the ``CleanText`` type makes
that visible to the type checker.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NewType

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from story_agent.config import ConfigError
from story_agent.hooks.base import HookContext
from story_agent.hooks.registry import HookPipeline
from story_agent.intake.preferences import extract_preferences, merge_preferences
from story_agent.schema import (
    Answer,
    AnswerKind,
    DiscoveryItem,
    Finding,
    HookPhase,
    ItemStatus,
    Provenance,
    ProvenanceType,
    Question,
    RunState,
    Severity,
)

CleanText = NewType("CleanText", str)

_JUDGMENT = {
    "use your judgment",
    "use your judgement",
    "your judgment",
    "your judgement",
    "judgment",
    "judgement",
    "up to you",
    "you decide",
}
_DEFER = {"defer", "deferred", "later", "decide later", "skip", "not now"}
_NOT_APPLICABLE = {"n/a", "na", "not applicable", "out of scope", "not needed", "not required"}
_YES = {"yes", "y", "still valid", "keep", "same"}
_NO = {"no", "n", "nope", "not valid"}
_MARKER = re.compile(r"\[QUARANTINED #\d+\]")
FINAL_PREFIX = "Q-final-"


def _norm(text: str) -> str:
    return " ".join(text.casefold().strip().rstrip(".!").split())


def sanitize_texts(
    pipeline: HookPipeline, ctx: HookContext, raw: Mapping[str, str]
) -> dict[str, CleanText]:
    """Run the component pre-hooks over user texts and return the cleaned versions."""
    ctx.stage = "clarify"
    ctx.data["untrusted"] = dict(raw)
    pipeline.enforce(HookPhase.PRE, "component", ctx)
    cleaned: dict[str, str] = ctx.data["untrusted"]
    return {key: CleanText(value) for key, value in cleaned.items()}


_SPECIAL: tuple[tuple[set[str], AnswerKind, str], ...] = (
    (_JUDGMENT, AnswerKind.JUDGMENT, "Agent judgment, approved by the user"),
    (_DEFER, AnswerKind.DEFERRED, "Deferred by the user"),
    (_NOT_APPLICABLE, AnswerKind.NOT_APPLICABLE, "Not applicable or out of scope"),
)


def parse_answer(question: Question, text: CleanText) -> Answer | None:
    """Turn cleaned answer text into an Answer for ``question``.

    Returns None when the user says "no" to a remembered default: the question is
    still open and needs a real answer. A "yes" confirms the remembered value, also
    when it is stale; the prompt shows the stale label, so that is an explicit
    re-confirmation.
    """
    key = _norm(text)
    qid = question.id
    for phrases, kind, wording in _SPECIAL:
        if key in phrases:
            return Answer(question_id=qid, kind=kind, value=wording)
    default = question.remembered_default
    if default is not None and key in _NO:
        return None
    if default is not None and key in _YES:
        return Answer(
            question_id=qid,
            kind=AnswerKind.MEMORY_CONFIRMED,
            value=default.value,
            memory_id=default.memory_id,
        )
    choice = _option_for(question, key)
    if choice is not None:
        return Answer(question_id=qid, kind=AnswerKind.OPTION, value=choice)
    return Answer(question_id=qid, kind=AnswerKind.OTHER, value=text.strip())


def _option_for(question: Question, key: str) -> str | None:
    if key.isdigit() and 1 <= int(key) <= len(question.options):
        return question.options[int(key) - 1]
    return next((o for o in question.options if _norm(o) == key), None)


def _resolve_items(state: RunState, item_ids: list[str], answer: Answer) -> None:
    ids = set(item_ids)
    if state.discovery is None:
        return
    updated: list[DiscoveryItem] = []
    for item in state.discovery.items:
        if item.id not in ids:
            updated.append(item)
            continue
        updated.append(_apply(item, answer))
    state.discovery = state.discovery.model_copy(update={"items": updated})


def _apply(item: DiscoveryItem, answer: Answer) -> DiscoveryItem:
    update: dict[str, object] = {"resolved_by": answer.question_id, "resolution": answer.value}
    if answer.kind is AnswerKind.DEFERRED:
        return item.model_copy(update=update)
    if answer.kind is AnswerKind.NOT_APPLICABLE:
        update["status"] = ItemStatus.REJECTED
        return item.model_copy(update=update)
    if answer.kind is AnswerKind.MEMORY_CONFIRMED and answer.memory_id:
        prov = Provenance(
            type=ProvenanceType.MEMORY_CONFIRMED, ref=answer.memory_id, element="item"
        )
    else:
        prov = Provenance(
            type=ProvenanceType.CLARIFICATION_ANSWER, ref=answer.question_id, element="item"
        )
    update["status"] = ItemStatus.CONFIRMED
    update["provenance"] = [*item.provenance, prov]
    return item.model_copy(update=update)


def answer_questions(state: RunState, raw: Mapping[str, CleanText]) -> list[Finding]:
    """Parse and apply answers keyed by question id. Returns findings for anything skipped."""
    findings: list[Finding] = []
    questions = {q.id: q for r in state.rounds for q in r.questions}
    for qid in sorted(raw):
        question = questions.get(qid)
        if question is None:
            findings.append(
                _finding("ANSWER_UNKNOWN_QUESTION", "answer for an unknown question", qid)
            )
            continue
        text = CleanText(_MARKER.sub("", raw[qid]).strip())
        if not text:
            findings.append(_finding("ANSWER_EMPTY", "answer is empty or was quarantined", qid))
            continue
        answer = parse_answer(question, text)
        if answer is None:
            default = question.remembered_default
            if default is not None and default.memory_id not in state.memory_rejected:
                state.memory_rejected.append(default.memory_id)
            findings.append(
                _finding(
                    "ANSWER_DEFAULT_REJECTED", "remembered answer rejected; please answer", qid
                )
            )
            continue
        state.answers = [a for a in state.answers if a.question_id != qid] + [answer]
        _resolve_items(state, question.item_ids, answer)
    return findings


def submit_free_text(state: RunState, text: CleanText) -> list[Finding]:
    """Store a free-text reply on the latest round and apply any whitelisted preferences.

    The text is scenario data. It can change preferences and nothing else.
    """
    if not state.rounds:
        return [_finding("FREE_TEXT_NO_ROUND", "no round to attach free text to", None)]
    cleaned = _MARKER.sub("", text).strip()
    if not cleaned:
        return []
    last = state.rounds[-1]
    joined = f"{last.free_text_reply}\n{cleaned}".strip()
    state.rounds[-1] = last.model_copy(update={"free_text_reply": joined})
    state.preferences = merge_preferences(state.preferences, extract_preferences(cleaned))
    return []


def resolve_remaining(state: RunState, category_ids: list[str], kind: AnswerKind) -> list[Answer]:
    """Record one explicit user decision (judgment, defer or not applicable) for categories.

    Call this only after the user chose it, for example at the readiness prompt.
    """
    if kind not in {AnswerKind.JUDGMENT, AnswerKind.DEFERRED, AnswerKind.NOT_APPLICABLE}:
        raise ValueError("resolve_remaining only accepts judgment, deferred or not_applicable")
    if state.discovery is None:
        return []
    made: list[Answer] = []
    wording = {
        AnswerKind.JUDGMENT: "Agent judgment, approved by the user",
        AnswerKind.DEFERRED: "Deferred by the user",
        AnswerKind.NOT_APPLICABLE: "Not applicable or out of scope",
    }[kind]
    for category_id in category_ids:
        answer = Answer(question_id=f"{FINAL_PREFIX}{category_id}", kind=kind, value=wording)
        open_ids = [
            i.id
            for i in state.discovery.items
            if i.category == category_id
            and i.status in {ItemStatus.UNKNOWN, ItemStatus.INFERRED}
            and i.resolved_by is None
        ]
        state.answers = [a for a in state.answers if a.question_id != answer.question_id] + [answer]
        _resolve_items(state, open_ids, answer)
        made.append(answer)
    return made


class AnswersFile(BaseModel):
    """Contents of an --answers file."""

    model_config = ConfigDict(extra="forbid")

    answers: dict[str, str] = Field(default_factory=dict)
    free_text: str = ""
    go_ahead: bool = False


def read_answers_data(path: Path) -> dict[str, Any]:
    """Read a YAML or JSON answers file as a mapping."""
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ConfigError(f"invalid answers file {path}: {type(exc).__name__}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"invalid answers file {path}: expected a mapping")
    return data


def load_answers_file(path: Path) -> AnswersFile:
    """Read a YAML or JSON answers file."""
    try:
        return AnswersFile.model_validate(read_answers_data(path))
    except ValidationError as exc:
        raise ConfigError(f"invalid answers file {path}: {type(exc).__name__}") from exc


def resolve_answer_keys(
    state: RunState, answers: Mapping[str, str]
) -> tuple[dict[str, str], list[str]]:
    """Map file keys (question ids or category ids) to question ids of asked questions."""
    by_id: dict[str, Question] = {}
    latest_by_category: dict[str, Question] = {}
    for round_ in state.rounds:
        for question in round_.questions:
            by_id[question.id] = question
            latest_by_category[question.category] = question
    resolved: dict[str, str] = {}
    unmatched: list[str] = []
    for key, value in answers.items():
        if key in by_id:
            resolved[key] = value
        elif key in latest_by_category:
            resolved[latest_by_category[key].id] = value
        else:
            unmatched.append(key)
    return resolved, sorted(unmatched)


def _finding(code: str, message: str, location: str | None) -> Finding:
    return Finding(code=code, message=message, severity=Severity.WARNING, location=location)
