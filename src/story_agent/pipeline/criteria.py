"""Stage 7: Given/When/Then acceptance criteria.

The model writes the criteria. Code cleans them, rejects invented numbers, orders
and caps them, assigns ids, and reports gaps (no criteria, no negative path, no
compliance or NFR criterion where the requirements imply one).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from story_agent.deps import StageDeps
from story_agent.guardrails.injection import wrap_untrusted
from story_agent.llm import LLMRequest, Usage
from story_agent.pipeline.numbers import known_numbers, ungrounded_numbers
from story_agent.pipeline.postprocess import clean_clause, order_and_cap_criteria
from story_agent.prompts import load_prompt
from story_agent.schema import (
    AcceptanceCriterion,
    CriterionKind,
    Finding,
    RunState,
    Severity,
    Story,
)

PROMPT_ID = "criteria"
COMPLIANCE_CATEGORIES = frozenset(
    {
        "aml_sanctions",
        "kyc_cdd",
        "regulatory_reporting",
        "data_privacy",
        "audit_retention",
        "maker_checker",
    }
)
MIN_CLAUSE_CHARS = 3


class CriterionDraft(BaseModel):
    """One criterion as the model returns it. No id."""

    model_config = ConfigDict(extra="forbid")

    given: str = Field(min_length=1, max_length=300)
    when: str = Field(min_length=1, max_length=300)
    then: str = Field(min_length=1, max_length=300)
    kind: Literal["happy", "edge", "error", "compliance", "nfr"]


class StoryCriteria(BaseModel):
    """Criteria for one story."""

    model_config = ConfigDict(extra="forbid")

    story_id: str
    criteria: list[CriterionDraft] = Field(default_factory=list, max_length=14)


class CriteriaOutput(BaseModel):
    """Model output for the criteria stage."""

    model_config = ConfigDict(extra="forbid")

    stories: list[StoryCriteria] = Field(default_factory=list, max_length=20)


@dataclass
class CriteriaResult:
    """Stories with criteria attached, plus findings and call details."""

    stories: list[Story]
    findings: list[Finding] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    requests: list[LLMRequest] = field(default_factory=list)


def _finding(code: str, message: str, story_id: str | None, error: bool = False) -> Finding:
    return Finding(
        code=code,
        message=message,
        severity=Severity.ERROR if error else Severity.WARNING,
        location=story_id,
    )


def max_criteria(deps: StageDeps, state: RunState) -> int:
    """Return the per-story criteria cap from preferences or standards."""
    return state.preferences.max_criteria_per_story or int(
        deps.config.standards.get("max_criteria_per_story", 8)
    )


def render_stories(stories: Sequence[Story], state: RunState) -> str:
    """Render stories with the requirements they cite."""
    by_id = {r.id: r for r in state.requirements}
    blocks: list[str] = []
    for s in stories:
        lines = [
            f"{s.id} [{s.priority.value}] As a {s.persona}, I want {s.want}, so that {s.benefit}."
        ]
        for rid in s.requirement_ids:
            req = by_id.get(rid)
            if req is not None:
                flag = " [ASSUMED]" if req.assumed else ""
                lines.append(f"  {req.id} [{req.category}]{flag} {req.text}")
        lines.extend(f"  NFR: {n}" for n in s.nfrs)
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def _vague(text: str, terms: Sequence[str]) -> list[str]:
    return [t for t in terms if re.search(rf"\b{re.escape(t)}(?:ly|ness)?\b", text, re.I)]


def convert_drafts(
    drafts: Sequence[CriterionDraft], story: Story, known: set[str], vague: Sequence[str]
) -> tuple[list[AcceptanceCriterion], list[Finding]]:
    """Clean model criteria. Return the usable ones and findings for the rest."""
    findings: list[Finding] = []
    kept: list[AcceptanceCriterion] = []
    for d in drafts:
        given, when, then = clean_clause(d.given), clean_clause(d.when), clean_clause(d.then)
        if min(len(given), len(when), len(then)) < MIN_CLAUSE_CHARS:
            findings.append(
                _finding("CRITERION_MALFORMED", "a criterion has an empty clause", story.id)
            )
            continue
        bad = ungrounded_numbers([given, when, then], known)
        if bad:
            findings.append(
                _finding(
                    "CRITERION_UNGROUNDED_NUMBER",
                    f"numbers not given by the user: {sorted(bad)}",
                    story.id,
                    True,
                )
            )
            continue
        hits = _vague(f"{given} {when} {then}", vague)
        if hits:
            findings.append(_finding("CRITERION_VAGUE", f"vague wording: {hits}", story.id))
        kept.append(
            AcceptanceCriterion(
                id="", given=given, when=when, then=then, kind=CriterionKind(d.kind)
            )
        )
    return kept, findings


def _coverage_findings(story: Story, state: RunState) -> list[Finding]:
    kinds = {c.kind for c in story.acceptance_criteria}
    findings: list[Finding] = []
    if not story.acceptance_criteria:
        return [_finding("CRITERIA_MISSING", "story has no acceptance criteria", story.id, True)]
    if CriterionKind.HAPPY not in kinds:
        findings.append(_finding("CRITERIA_NO_HAPPY_PATH", "no happy-path criterion", story.id))
    if not kinds & {CriterionKind.EDGE, CriterionKind.ERROR}:
        findings.append(
            _finding("CRITERIA_NO_NEGATIVE_PATH", "no edge or error criterion", story.id)
        )
    categories = {r.category for r in state.requirements if r.id in story.requirement_ids}
    if categories & COMPLIANCE_CATEGORIES and CriterionKind.COMPLIANCE not in kinds:
        findings.append(
            _finding(
                "CRITERIA_NO_COMPLIANCE",
                "compliance requirement without a compliance criterion",
                story.id,
            )
        )
    if story.nfrs and CriterionKind.NFR not in kinds:
        findings.append(
            _finding("CRITERIA_NO_NFR", "non-functional need without an nfr criterion", story.id)
        )
    return findings


def run_criteria(
    deps: StageDeps, state: RunState, stories: Sequence[Story], only_ids: set[str] | None = None
) -> CriteriaResult:
    """Write criteria for ``only_ids`` (default all). Other stories are returned unchanged."""
    standards = deps.config.standards
    batch_size = int(standards.get("drafting", {}).get("criteria_batch_size", 8))
    vague = list(standards.get("vague_terms", []))
    limit = max_criteria(deps, state)
    targets = [s for s in stories if only_ids is None or s.id in only_ids]
    prompt = load_prompt(deps.prompts_dir, PROMPT_ID)
    known = known_numbers(state)
    updated: dict[str, Story] = {s.id: s for s in stories}
    result = CriteriaResult(list(stories))
    for start in range(0, len(targets), batch_size):
        batch = targets[start : start + batch_size]
        user = "\n".join(
            [
                f"MAX CRITERIA PER STORY: {limit}",
                "STORIES:",
                wrap_untrusted("stories", render_stories(batch, state)),
            ]
        )
        request = LLMRequest(PROMPT_ID, prompt.text, user, deps.config.models.generator)
        reply = deps.client.complete(request, CriteriaOutput)
        result.requests.append(request)
        result.usage = Usage(
            result.usage.input_tokens + reply.usage.input_tokens,
            result.usage.output_tokens + reply.usage.output_tokens,
        )
        result.cost_usd += reply.cost_usd
        by_story = {sc.story_id: sc for sc in reply.value.stories}
        extra = set(by_story) - {s.id for s in batch}
        result.findings.extend(
            _finding("CRITERIA_UNKNOWN_STORY", f"criteria for unknown story {i}", i)
            for i in sorted(extra)
        )
        for story in batch:
            drafts = by_story[story.id].criteria if story.id in by_story else []
            kept, findings = convert_drafts(drafts, story, known, vague)
            result.findings.extend(findings)
            criteria = order_and_cap_criteria(kept, story.id, limit)
            updated[story.id] = story.model_copy(update={"acceptance_criteria": criteria})
    result.stories = [updated[s.id] for s in stories]
    for story in result.stories:
        if only_ids is None or story.id in only_ids:
            result.findings.extend(_coverage_findings(story, state))
    return result
