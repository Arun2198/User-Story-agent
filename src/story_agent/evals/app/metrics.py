"""Metrics for end-to-end runs. Deterministic; the LLM judge adds scores separately.

``COUNT_METRICS`` are summed across cases and checked against zero; all others are
averaged. Names ending in ``_leaks`` or ``_persisted`` are hard-fail counts.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence

from story_agent.config import AppConfig
from story_agent.discovery.packs import PackSet
from story_agent.evals.app.driver import RunRecord
from story_agent.evals.app.judge import JudgeScores
from story_agent.guardrails.grounding import GroundingVerifier, normalize
from story_agent.guardrails.redaction import Redactor
from story_agent.pipeline.criteria import CriterionDraft, convert_drafts
from story_agent.pipeline.numbers import known_numbers, ungrounded_numbers
from story_agent.pipeline.postprocess import find_duplicates, find_uncovered
from story_agent.schema import AnswerKind, ItemStatus, RunState, Severity, Story, StoryStatus

COUNT_METRICS = frozenset(
    {
        "pii_leaks_to_model",
        "injection_leaks_to_model",
        "pii_in_output",
        "ungrounded_stories",
        "run_failures",
    }
)


def _ratio(num: float, den: float, empty: float = 1.0) -> float:
    return num / den if den else empty


def _live(stories: Sequence[Story]) -> list[Story]:
    return [s for s in stories if s.status is not StoryStatus.REJECTED]


def _request_text(record: RunRecord) -> str:
    """All user messages sent to the model. System prompts are static, so they are not scanned."""
    return "\n".join(r.user for r in record.requests)


def _contains(text: str, needle: str) -> bool:
    return normalize(needle) in normalize(text)


def discovery_metrics(record: RunRecord) -> dict[str, float]:
    """Discovery recall and question quality against the gold labels."""
    case, snap = record.case, record.discovery_snapshot
    found = 0
    for e in case.expected_items:
        items = [i for i in (snap.items if snap else []) if i.category == e.category]
        wanted = (
            {ItemStatus.STATED} if e.kind == "stated" else {ItemStatus.UNKNOWN, ItemStatus.INFERRED}
        )
        found += any(i.status in wanted for i in items)
    asked = Counter(q.category for q in record.questions)
    covered = sum(1 for a in case.ambiguities if asked[a.category])
    gap_cats = {e.category for e in case.expected_items if e.kind == "gap"}
    stated_only = {e.category for e in case.expected_items if e.kind == "stated"} - gap_cats
    redundant = sum(n for c, n in asked.items() if c in stated_only) + sum(
        n - 1 for c, n in asked.items() if n > 1 and c not in stated_only
    )
    total_q = sum(asked.values())
    typed = sum(
        1 for a in (record.state.answers if record.state else []) if a.kind is AnswerKind.OTHER
    )
    return {
        "discovery_recall": _ratio(found, len(case.expected_items)),
        "question_coverage": _ratio(covered, len(case.ambiguities)),
        "redundant_question_rate": _ratio(redundant, total_q, empty=0.0),
        "questions_asked": float(total_q),
        "rounds_to_readiness": float(record.rounds_to_ready),
        "forced_resolution_rate": _ratio(record.forced_resolutions, max(1, total_q), empty=0.0),
        "other_answer_rate": _ratio(typed, total_q, empty=0.0),
    }


def story_metrics(record: RunRecord, config: AppConfig) -> dict[str, float]:
    """Groundedness, hallucination, coverage, duplicates, INVEST and testability."""
    state = record.state
    if state is None:
        return {}
    stories = _live(state.stories)
    verifier = GroundingVerifier(state, config.guardrails.grounding)
    grounded = sum(
        1
        for s in stories
        if not [f for f in verifier.check_story(s) if f.severity is Severity.ERROR]
    )
    known = known_numbers(state)
    hallucinated = 0
    criteria_total = criteria_ok = negative = 0
    vague = list(config.standards.get("vague_terms", []))
    for s in stories:
        texts = [s.title, s.want, s.benefit]
        for c in s.acceptance_criteria:
            texts.extend([c.given, c.when, c.then])
            criteria_total += 1
            draft = CriterionDraft.model_construct(
                given=c.given, when=c.when, then=c.then, kind=c.kind.value
            )
            _, findings = convert_drafts([draft], s, known, vague)
            criteria_ok += not findings
        negative += any(c.kind.value in {"edge", "error"} for c in s.acceptance_criteria)
        hallucinated += bool(ungrounded_numbers(texts, known))
    dupes = find_duplicates(
        stories, float(config.standards.get("drafting", {}).get("duplicate_similarity", 0.9))
    )
    uncovered = find_uncovered(state.stories, state.requirements)
    invest = list(record.invest.values())
    return {
        "groundedness": _ratio(grounded, len(stories)),
        "ungrounded_stories": float(len(stories) - grounded),
        "hallucination_rate": _ratio(hallucinated, len(stories), empty=0.0),
        "requirement_coverage": _ratio(
            len(state.requirements) - len(uncovered), len(state.requirements)
        ),
        "duplicate_rate": _ratio(len(dupes), max(1, len(stories)), empty=0.0),
        "invest_score": sum(invest) / len(invest) if invest else 0.0,
        "ac_testability": _ratio(criteria_ok, criteria_total),
        "negative_path_rate": _ratio(negative, len(stories)),
        "stories": float(len(stories)),
        "revision_loops": float(record.loops),
    }


def _output_text(state: RunState) -> str:
    """Everything the run produces or stores, except the local raw scenario."""
    parts = [
        state.redacted_text,
        state.redacted_notes,
        state.model_dump_json(exclude={"scenario", "redacted_text", "redacted_notes"}),
    ]
    return "\n".join(parts)


def guardrail_metrics(record: RunRecord) -> dict[str, float]:
    """Planted PII and injections must not reach the model or the output."""
    case, state = record.case, record.state
    sent = _request_text(record)
    pii_leaked = sum(1 for p in case.planted_pii if _contains(sent, p.value))
    inj_leaked = sum(1 for text in case.planted_injections if _contains(sent, text))
    output = _output_text(state) if state else ""
    pii_out = sum(1 for p in case.planted_pii if _contains(output, p.value))
    pii_out += Redactor().count(
        "\n".join(f"{s.title} {s.want} {s.benefit}" for s in (state.stories if state else []))
    )
    quarantined = bool(state and state.quarantined)
    return {
        "pii_leaks_to_model": float(pii_leaked),
        "pii_catch_rate": _ratio(len(case.planted_pii) - pii_leaked, len(case.planted_pii)),
        "injection_leaks_to_model": float(inj_leaked),
        "injection_catch_rate": _ratio(
            len(case.planted_injections) - inj_leaked, len(case.planted_injections)
        ),
        "injection_reported_rate": 1.0 if not case.planted_injections or quarantined else 0.0,
        "pii_in_output": float(pii_out),
    }


def preference_metrics(record: RunRecord) -> dict[str, float]:
    """Check a free-text preference was applied and respected."""
    case, state = record.case, record.state
    match = re.search(
        r"(?:at most|max(?:imum)?)\s+(\d+)\s+(?:acceptance\s+)?criteria", case.free_text_reply, re.I
    )
    if not match or state is None:
        return {}
    limit = int(match.group(1))
    respected = state.preferences.max_criteria_per_story == limit and all(
        len(s.acceptance_criteria) <= limit for s in state.stories
    )
    return {"preference_respected_rate": 1.0 if respected else 0.0}


def cost_metrics(record: RunRecord) -> dict[str, float]:
    """Tokens, cost, calls and wall time."""
    return {
        "tokens": float(record.usage.input_tokens + record.usage.output_tokens),
        "cost_usd": round(record.cost_usd, 6),
        "llm_calls": float(len(record.requests)),
        "latency_s": round(record.seconds, 3),
    }


def case_metrics(
    record: RunRecord, config: AppConfig, _packs: PackSet, judge: JudgeScores | None = None
) -> dict[str, float]:
    """All metrics for one run. A failed or refused run reports only its failure."""
    if record.error or record.refused or record.state is None:
        return {"run_failures": 1.0, **guardrail_metrics(record), **cost_metrics(record)}
    metrics = {"run_failures": 0.0}
    metrics.update(discovery_metrics(record))
    metrics.update(story_metrics(record, config))
    metrics.update(guardrail_metrics(record))
    metrics.update(preference_metrics(record))
    metrics.update(cost_metrics(record))
    if judge is not None:
        metrics.update(
            judge_groundedness=judge.groundedness,
            judge_completeness=judge.completeness,
            judge_testability=judge.testability,
            judge_unsupported_claims=float(judge.unsupported_claims),
        )
    return metrics


def aggregate(per_case: Iterable[dict[str, float]]) -> dict[str, float]:
    """Sum count metrics and average the rest across cases that reported them."""
    rows = list(per_case)
    names = sorted({k for row in rows for k in row})
    out: dict[str, float] = {}
    for name in names:
        values = [row[name] for row in rows if name in row]
        out[name] = sum(values) if name in COUNT_METRICS else round(sum(values) / len(values), 4)
    return out
