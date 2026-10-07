"""Stage 5a: write the questions for one round.

Code picks the categories and builds ids, options hygiene and remembered defaults.
The model only phrases the question text, the reason and the suggested options.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from story_agent.clarify.select import OpenCategory
from story_agent.deps import StageDeps
from story_agent.guardrails.injection import wrap_untrusted
from story_agent.ids import question_id
from story_agent.llm import LLMRequest, Usage
from story_agent.memory.render import memory_ids, render_memory_block
from story_agent.prompts import load_prompt
from story_agent.schema import (
    Finding,
    MemoryEntry,
    MemoryRef,
    MemoryType,
    Question,
    QuestionRound,
    RunState,
    Severity,
    utcnow,
)

PROMPT_ID = "clarify"
_FORBIDDEN_OPTIONS = {
    "other",
    "others",
    "none of the above",
    "use your judgment",
    "use your judgement",
}
MAX_OPTIONS = 4


class QuestionDraft(BaseModel):
    """One question as the model returns it. No ids."""

    model_config = ConfigDict(extra="forbid")

    category_id: str
    question: str = Field(min_length=1, max_length=400)
    why_it_matters: str = Field(min_length=1, max_length=300)
    options: list[str] = Field(min_length=2, max_length=6)


class ClarifyOutput(BaseModel):
    """Model output for the clarify stage."""

    model_config = ConfigDict(extra="forbid")

    questions: list[QuestionDraft] = Field(default_factory=list, max_length=6)


@dataclass
class RoundResult:
    """A built round plus findings and usage."""

    round: QuestionRound
    findings: list[Finding] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0


def memory_default(
    category_id: str, entries: Sequence[MemoryEntry], now: datetime | None = None
) -> MemoryRef | None:
    """Return the newest confirmed answer tagged for this category, marked stale past its TTL."""
    current = now or utcnow()
    tag = f"category:{category_id}"
    matches = [e for e in entries if e.type is MemoryType.CONFIRMED_ANSWER and tag in e.tags]
    if not matches:
        return None
    best = sorted(matches, key=lambda e: (e.last_confirmed_at, e.id), reverse=True)[0]
    stale = current > best.last_confirmed_at + timedelta(days=best.ttl_days)
    return MemoryRef(
        memory_id=best.id,
        value=best.content,
        last_confirmed_at=best.last_confirmed_at,
        stale=stale,
    )


def clean_options(draft: list[str], typical: Sequence[str]) -> list[str]:
    """Trim, dedupe, drop forbidden options, cap at four, and top up from typical options."""
    seen: set[str] = set()
    options: list[str] = []
    for source in (draft, typical):
        for raw in source:
            text = " ".join(raw.split())
            key = text.casefold().rstrip(".")
            if not text or key in _FORBIDDEN_OPTIONS or key in seen:
                continue
            seen.add(key)
            options.append(text)
            if len(options) == MAX_OPTIONS:
                return options
        if len(options) >= 2:
            break
    return options if len(options) >= 2 else ["Yes", "No"]


def _render_categories(selected: Sequence[OpenCategory], must_have_ids: frozenset[str]) -> str:
    lines: list[str] = []
    for open_cat in selected:
        cat = open_cat.category
        marker = "must-have" if cat.id in must_have_ids else "optional"
        lines.append(f"- id: {cat.id} | {cat.name} | {marker}")
        lines.extend(f"    probe: {p}" for p in cat.probes)
        lines.extend(f"    typical option: {o}" for o in cat.typical_options)
    return "\n".join(lines)


def _render_open_items(selected: Sequence[OpenCategory]) -> str:
    return "\n".join(
        f"{open_cat.category.id}: [{item.id}] {item.status.value}: {item.description}"
        for open_cat in selected
        for item in open_cat.items
    )


def _render_history(state: RunState) -> str:
    lines = [f"{a.question_id} ({a.kind.value}): {a.value}" for a in state.answers if a.value]
    lines.extend(
        f"free text round {r.number}: {r.free_text_reply}"
        for r in state.rounds
        if r.free_text_reply
    )
    return "\n".join(lines) or "none"


def build_user_message(
    state: RunState,
    selected: Sequence[OpenCategory],
    must_have_ids: frozenset[str],
    max_rounds: int,
    memory: Sequence[MemoryEntry],
) -> str:
    """Assemble the user message. Text derived from user input is wrapped as untrusted."""
    discovery = state.discovery
    summary = ""
    if discovery is not None:
        summary = (
            f"domain: {discovery.domain}/{discovery.subdomain}\n"
            f"actors: {', '.join(discovery.actors)}\n"
            f"goals: {'; '.join(discovery.goals)}\n"
            f"context: {discovery.business_context}"
        )
    return "\n".join(
        [
            f"ROUND: {len(state.rounds) + 1} of {max_rounds}",
            "CATEGORIES TO ASK:",
            _render_categories(selected, must_have_ids),
            "DISCOVERY SUMMARY:",
            wrap_untrusted("discovery_summary", summary or "none"),
            "OPEN ITEMS:",
            wrap_untrusted("open_items", _render_open_items(selected)),
            "EARLIER ANSWERS AND FREE TEXT:",
            wrap_untrusted("answers", _render_history(state)),
            "REMEMBERED ANSWERS:",
            render_memory_block(memory),
        ]
    )


def build_round(
    deps: StageDeps,
    state: RunState,
    selected: Sequence[OpenCategory],
    must_have_ids: frozenset[str],
    memory: Sequence[MemoryEntry] = (),
) -> RoundResult:
    """Ask the model to phrase the questions, then enforce one clean question per category."""
    limits_rounds = len(state.rounds) + 1
    prompt = load_prompt(deps.prompts_dir, PROMPT_ID)
    max_rounds = deps.config.standards.get("clarification", {}).get("max_rounds", 3)
    request = LLMRequest(
        prompt_id=PROMPT_ID,
        system=prompt.text,
        user=build_user_message(state, selected, must_have_ids, max_rounds, memory),
        model=deps.config.models.generator,
        memory_ids=memory_ids(memory),
    )
    result = deps.client.complete(request, ClarifyOutput)
    drafts = {d.category_id: d for d in result.value.questions}
    findings: list[Finding] = []
    questions: list[Question] = []
    for open_cat in selected:
        cat = open_cat.category
        draft = drafts.get(cat.id)
        if draft is None:
            findings.append(
                Finding(
                    code="CLARIFY_FALLBACK",
                    message=f"no question from the model for {cat.id}; used the pack probe",
                    severity=Severity.WARNING,
                    location=cat.id,
                )
            )
            text, why, options = (
                cat.probes[0],
                f"{cat.name} affects the stories and has no answer yet.",
                [],
            )
        else:
            text, why, options = draft.question.strip(), draft.why_it_matters.strip(), draft.options
        questions.append(
            Question(
                id=question_id(limits_rounds, cat.id),
                category=cat.id,
                question=text,
                why_it_matters=why,
                options=clean_options(options, cat.typical_options),
                remembered_default=memory_default(cat.id, memory),
                item_ids=[i.id for i in open_cat.items],
            )
        )
    round_ = QuestionRound(number=limits_rounds, questions=questions)
    return RoundResult(round_, findings, result.usage, result.cost_usd)
