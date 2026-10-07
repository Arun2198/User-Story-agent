"""Shared builders for tests."""

import re
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from story_agent.clarify.answers import (
    CleanText,
    answer_questions,
    resolve_remaining,
)
from story_agent.clarify.readiness import grant_go_ahead, must_have_open_categories
from story_agent.clarify.rounds import next_round
from story_agent.config import AppConfig
from story_agent.deps import StageDeps
from story_agent.discovery.discover import run_discover
from story_agent.discovery.packs import PackSet
from story_agent.fake_llm import FakeTransport
from story_agent.llm import RawResponse, StructuredClient
from story_agent.schema import AnswerKind, Requirement, RunState, Scenario

DISPUTE = (
    "A customer disputes a card transaction and expects a provisional credit "
    "while the bank investigates. The relationship manager can see the case."
)


def make_state(text: str = DISPUTE, notes: str = "", run_id: str = "r1") -> RunState:
    return RunState(
        run_id=run_id,
        scenario=Scenario(text=text, notes=notes),
        redacted_text=text,
        redacted_notes=notes,
    )


def make_deps(
    config: AppConfig,
    packs: PackSet,
    prompts_dir: Path,
    queued: dict[str, list[Any]] | None = None,
) -> tuple[StageDeps, FakeTransport]:
    fake = FakeTransport(queued or {})
    client = StructuredClient(fake, config.models)
    return StageDeps(client, config, packs, prompts_dir), fake


def discover_output(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "domain": "banking",
        "subdomain": "cards",
        "subpacks": [],
        "actors": ["Customer", "relationship manager", "Customer"],
        "goals": ["Dispute a transaction"],
        "business_context": "Card dispute handling.",
        "items": [
            {
                "category_id": "dispute_handling",
                "description": "Customer expects a provisional credit during the investigation.",
                "status": "stated",
                "evidence": "expects a provisional credit while the bank investigates",
            },
            {
                "category_id": "dispute_handling",
                "description": "Time limits and evidence rules are not given.",
                "status": "unknown",
                "evidence": "",
            },
            {
                "category_id": "limits_velocity",
                "description": "A claim limit may exist.",
                "status": "inferred",
                "evidence": "",
            },
        ],
    }
    base.update(overrides)
    return base


def clarify_output(state: RunState, categories: list[str]) -> dict[str, Any]:
    return {
        "questions": [
            {
                "category_id": c,
                "question": f"Question about {c}?",
                "why_it_matters": f"It shapes the {c} stories.",
                "options": ["Option A", "Option B", "Other", "option a"],
            }
            for c in categories
        ]
    }


def discovered(
    config: AppConfig, packs: PackSet, prompts: Path, queued: dict[str, list[Any]] | None = None
) -> tuple[StageDeps, FakeTransport, RunState, Any]:
    """Run discover with the fake client and return deps, fake, state and checklist."""
    queued = dict(queued or {})
    queued.setdefault("discover", [discover_output()])
    deps, fake = make_deps(config, packs, prompts, queued)
    state = make_state()
    result = run_discover(deps, state)
    state.discovery = result.discovery
    return deps, fake, state, result.checklist


def settled(
    config: AppConfig, packs: PackSet, prompts: Path, queued: dict[str, list[Any]] | None = None
) -> tuple[StageDeps, FakeTransport, RunState, Any]:
    """Discovery and clarification finished, the gate open, requirements not yet derived."""

    deps, fake, state, checklist = discovered(config, packs, prompts, queued)
    fake._queued["clarify"].append({"questions": []})
    outcome = next_round(deps, state, checklist)
    assert outcome.result is not None
    questions = outcome.result.round.questions
    answer_questions(state, {q.id: CleanText("1") for q in questions[:3]})
    resolve_remaining(state, must_have_open_categories(state, checklist), AnswerKind.JUDGMENT)
    grant_go_ahead(state, checklist, "user")
    return deps, fake, state, checklist


_VERBS = [
    "review",
    "confirm",
    "track",
    "limit",
    "approve",
    "report",
    "verify",
    "audit",
    "schedule",
    "cancel",
    "retry",
    "notify",
    "export",
    "reconcile",
    "escalate",
    "archive",
]


def _nice(category: str) -> str:
    return category.replace("_", " ")


def draft_output(
    source: RunState | Sequence[Requirement], per_story: int = 1, epic_by_category: bool = True
) -> dict[str, Any]:
    """A valid draft that covers every requirement, ``per_story`` requirements per story."""
    reqs = source.requirements if isinstance(source, RunState) else list(source)
    epics: dict[str, list[dict[str, Any]]] = {}
    for n in range(0, len(reqs), per_story):
        group = reqs[n : n + per_story]
        epic = f"Epic {chr(65 + (n // per_story) % 3)}" if epic_by_category else "Disputes"
        verb = _VERBS[(n // per_story) % len(_VERBS)]
        topic = _nice(group[0].category)
        epics.setdefault(epic, []).append(
            {
                "title": f"{verb.title()} {topic} {group[0].id}",
                "persona": "Customer",
                "want": f"to {verb} the {topic}",
                "benefit": f"the {_nice(group[0].category)} result is clear to me",
                "priority": "must" if n % 2 == 0 else "should",
                "estimate": 3,
                "requirement_refs": [r.id for r in group],
                "persona_refs": [],
                "want_refs": [group[0].id],
                "benefit_refs": [],
                "depends_on": [],
            }
        )
    return {
        "epics": [
            {"name": name, "features": [], "stories": stories} for name, stories in epics.items()
        ]
    }


def criteria_output(
    stories: list[Any], kinds: tuple[str, ...] = ("happy", "edge", "error")
) -> dict[str, Any]:
    return {
        "stories": [
            {
                "story_id": s.id,
                "criteria": [
                    {
                        "given": f"a {k} situation for {s.id}",
                        "when": "the customer acts",
                        "then": f"the {k} result is shown",
                        "kind": k,
                    }
                    for k in kinds
                ],
            }
            for s in stories
        ]
    }


EMPTY_CRITIQUE: dict[str, Any] = {
    "story_notes": [],
    "duplicates": [],
    "contradictions": [],
    "split_suggestions": [],
}


class AutoTransport(FakeTransport):
    """Fake transport that answers criteria and critique calls from the request itself."""

    def send(self, request, _json_schema):  # type: ignore[no-untyped-def]  # test helper
        queue = self._queued[request.prompt_id]
        if not queue and request.prompt_id == "criteria":
            ids = re.findall(r"^([A-Z]{2,6}-\d{4}) ", request.user, flags=re.M)
            self.calls.append(request)
            data = {
                "stories": [
                    {
                        "story_id": sid,
                        "criteria": [
                            {
                                "given": f"a {k} situation for {sid}",
                                "when": "the customer acts",
                                "then": f"the {k} result is shown",
                                "kind": k,
                            }
                            for k in ("happy", "edge", "error")
                        ],
                    }
                    for sid in ids
                ]
            }
            return RawResponse(data, self._usage)
        if not queue and request.prompt_id == "critique":
            self.calls.append(request)
            return RawResponse(EMPTY_CRITIQUE, self._usage)
        return super().send(request, _json_schema)


def auto_deps(deps: StageDeps, fake: FakeTransport) -> tuple[StageDeps, AutoTransport]:
    """Return deps whose client auto-answers criteria and critique, keeping queued drafts."""
    auto = AutoTransport({k: list(v) for k, v in fake._queued.items()})
    return replace(deps, client=StructuredClient(auto, deps.config.models)), auto
