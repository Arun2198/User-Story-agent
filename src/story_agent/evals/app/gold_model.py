"""An offline stand-in for the model, driven by an eval case's gold labels.

It behaves like a careful model that finds what the case planted. Offline end-to-end
evals therefore test the plumbing, the guardrails, the simulator and the metrics, not
model quality; model quality needs ``--live``. Degradation modes make it behave badly
on purpose so tests can show the metrics notice:

- ``omit_stated``: miss every second stated item in discovery
- ``hallucinate_number``: put an invented duration in the first story
- ``leak_pii``: copy a planted sensitive value into the first story
- ``noise_seed``: drop a discovered item or a story at random, to exercise stability
"""

from __future__ import annotations

import random
import re
from typing import Any

from story_agent.discovery.packs import PackSet
from story_agent.evals.cases import EvalCase
from story_agent.llm import LLMRequest, RawResponse, Usage

_COMPLIANCE = {
    "aml_sanctions",
    "kyc_cdd",
    "regulatory_reporting",
    "data_privacy",
    "audit_retention",
    "maker_checker",
}
_NFR = {
    "performance_availability",
    "security",
    "audit",
    "audit_retention",
    "business_continuity",
    "data_privacy",
}
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
    "monitor",
    "release",
    "record",
    "protect",
]


_ADJECTIVES = [
    "primary",
    "secondary",
    "standard",
    "special",
    "daily",
    "monthly",
    "manual",
    "automatic",
    "regional",
    "customer-facing",
    "internal",
    "external",
    "urgent",
    "routine",
    "partial",
    "complete",
    "early",
    "late",
    "shared",
    "separate",
    "temporary",
    "permanent",
]


def _block(user: str, kind: str) -> str:
    match = re.search(rf'<untrusted_data kind="{kind}">\n(.*?)\n</untrusted_data>', user, re.S)
    return match.group(1) if match else ""


def _nice(category: str) -> str:
    return category.replace("_", " ")


class GoldModel:
    """Transport that answers every prompt from the case's gold labels."""

    def __init__(
        self,
        case: EvalCase,
        packs: PackSet,
        degrade: frozenset[str] = frozenset(),
        noise_seed: int | None = None,
    ) -> None:
        """Bind to one case. ``noise_seed`` makes behaviour vary between runs."""
        self.case = case
        self.packs = packs
        self.degrade = degrade
        self._rng = random.Random(noise_seed) if noise_seed is not None else None  # noqa: S311  # nosec B311
        self.calls: list[LLMRequest] = []

    def send(self, request: LLMRequest, _json_schema: dict[str, Any]) -> RawResponse:
        """Return the scripted answer for this prompt."""
        self.calls.append(request)
        handler = {
            "scope_check": self._scope,
            "discover": self._discover,
            "clarify": self._clarify,
            "draft": self._draft,
            "criteria": self._criteria,
            "critique": self._critique,
        }[request.prompt_id]
        data = handler(request)
        usage = Usage(len(request.system + request.user) // 4, max(60, len(str(data)) // 4))
        return RawResponse(data, usage)

    # ---- handlers -------------------------------------------------------------

    @staticmethod
    def _scope(_request: LLMRequest) -> dict[str, Any]:
        return {"category": "generate_stories", "reason": "Describes a business scenario."}

    def _discover(self, _request: LLMRequest) -> dict[str, Any]:
        case = self.case
        checklist = self.packs.checklist(case.gold_domain, case.gold_subdomain, case.gold_subpacks)
        items: list[dict[str, str]] = []
        stated_seen = 0
        for e in case.expected_items:
            if e.kind == "stated":
                stated_seen += 1
                if "omit_stated" in self.degrade and stated_seen % 2 == 0:
                    continue
                items.append(
                    {
                        "category_id": e.category,
                        "description": e.description,
                        "status": "stated",
                        "evidence": e.evidence,
                    }
                )
            else:
                items.append(
                    {
                        "category_id": e.category,
                        "description": e.description,
                        "status": "unknown",
                        "evidence": "",
                    }
                )
        for amb in case.ambiguities:
            cat = checklist.get(amb.category)
            if cat is not None and cat.typical_options:
                items.append(
                    {
                        "category_id": cat.id,
                        "description": f"A typical approach may apply: {cat.typical_options[0]}",
                        "status": "inferred",
                        "evidence": "",
                    }
                )
        if self._rng is not None and items and self._rng.random() < 0.5:
            items.pop(self._rng.randrange(len(items)))
        return {
            "domain": case.gold_domain,
            "subdomain": case.gold_subdomain,
            "subpacks": case.gold_subpacks,
            "actors": case.personas,
            "goals": [case.title],
            "business_context": case.title,
            "items": items,
        }

    def _clarify(self, request: LLMRequest) -> dict[str, Any]:
        section = request.user.split("CATEGORIES TO ASK:")[1].split("DISCOVERY SUMMARY:")[0]
        questions: list[dict[str, Any]] = []
        for part in re.split(r"^- id: ", section, flags=re.M)[1:]:
            head, *rest = part.splitlines()
            cat = head.split("|")[0].strip()
            name = head.split("|")[1].strip() if "|" in head else _nice(cat)
            options = [
                m.group(1) for line in rest if (m := re.match(r"\s+typical option: (.*)", line))
            ]
            options = (
                options[:4]
                if len(options) >= 2
                else ["Yes, as described", "No, handle it differently"]
            )
            questions.append(
                {
                    "category_id": cat,
                    "question": f"How should {name.lower()} work in this scenario?",
                    "why_it_matters": f"The answer sets the {name.lower()} rules in the stories.",
                    "options": options,
                }
            )
        return {"questions": questions}

    def _requirements(self, request: LLMRequest) -> list[tuple[str, str, bool, str]]:
        out = []
        for line in _block(request.user, "requirements").splitlines():
            m = re.match(r"(REQ-\d+) \[([a-z_]+)\]( \[ASSUMED\])? (.*)", line)
            if m:
                out.append((m.group(1), m.group(2), bool(m.group(3)), m.group(4)))
        return out

    def _draft(self, request: LLMRequest) -> dict[str, Any]:
        reqs = self._requirements(request)
        persona = self.case.personas[0]
        stories: list[dict[str, Any]] = []
        for n, (rid, cat, assumed, text) in enumerate(reqs):
            verb = _VERBS[n % len(_VERBS)]
            adjective = _ADJECTIVES[(n * 7 + 3) % len(_ADJECTIVES)]
            snippet = " ".join(text.split(":")[-1].split()[:10])
            want = f"to {verb} the {adjective} {_nice(cat)} rule: {snippet}"
            if assumed:
                want = (
                    f"to {verb} the {adjective} {_nice(cat)} behaviour once its value is confirmed"
                )
            stories.append(
                {
                    "title": f"{verb.title()} {_nice(cat)} {rid}",
                    "persona": persona,
                    "want": want,
                    "benefit": f"the {_nice(cat)} outcome is applied consistently",
                    "priority": "must" if n % 2 == 0 else "should",
                    "estimate": 3,
                    "requirement_refs": [rid],
                    "persona_refs": [],
                    "want_refs": [rid],
                    "benefit_refs": [],
                    "depends_on": [],
                }
            )
        if stories and "hallucinate_number" in self.degrade:
            stories[0]["want"] += " within 72 hours"
        if stories and "leak_pii" in self.degrade and self.case.planted_pii:
            stories[0]["want"] += f" for {self.case.planted_pii[0].value}"
        if self._rng is not None and len(stories) > 1 and self._rng.random() < 0.3:
            stories.pop()
        epics: dict[str, list[dict[str, Any]]] = {}
        for n, story in enumerate(stories):
            epics.setdefault(f"Epic {chr(65 + n % 3)}", []).append(story)
        return {"epics": [{"name": k, "features": [], "stories": v} for k, v in epics.items()]}

    def _criteria(self, request: LLMRequest) -> dict[str, Any]:
        out: list[dict[str, Any]] = []
        block = _block(request.user, "stories")
        for part in re.split(r"^(?=[A-Z]{2,6}-\d{4} )", block, flags=re.M):
            m = re.match(r"([A-Z]{2,6}-\d{4}) ", part)
            if not m:
                continue
            sid = m.group(1)
            cats = set(re.findall(r"^\s+REQ-\d+ \[([a-z_]+)\]", part, flags=re.M))
            kinds = ["happy", "edge", "error"]
            if cats & _COMPLIANCE:
                kinds.append("compliance")
            if "  NFR:" in part or cats & _NFR:
                kinds.append("nfr")
            out.append(
                {
                    "story_id": sid,
                    "criteria": [
                        {
                            "given": f"a {k} situation for {sid}",
                            "when": "the customer acts",
                            "then": f"the {k} result is shown",
                            "kind": k,
                        }
                        for k in kinds
                    ],
                }
            )
        return {"stories": out}

    @staticmethod
    def _critique(_request: LLMRequest) -> dict[str, Any]:
        return {"story_notes": [], "duplicates": [], "contradictions": [], "split_suggestions": []}
