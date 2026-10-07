"""Eval cases: a scenario, its gold labels and the hidden answer key.

A case is data. The simulator answers questions from ``answer_key``; the metrics
compare the run with the gold labels. ``validate_case`` checks a case against the
domain packs and the guardrails so a bad label cannot silently skew results.
"""

from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from story_agent.discovery.packs import PackSet
from story_agent.guardrails.grounding import normalize
from story_agent.guardrails.injection import InjectionDetector
from story_agent.guardrails.redaction import Redactor
from story_agent.memory.guard import check_content
from story_agent.schema import SCHEMA_VERSION, Finding, Severity

CASE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")


class _Case(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExpectedItem(_Case):
    """A planted consideration the agent should discover."""

    category: str
    kind: Literal["stated", "gap"]
    description: str
    evidence: str = ""


class Ambiguity(_Case):
    """A planted ambiguity and the question topic (a checklist category) it should trigger."""

    category: str
    topic: str


class PlantedPII(_Case):
    """A sensitive value planted in the scenario or notes."""

    label: str
    value: str


class MemoryPlan(_Case):
    """How the memory evals perturb the second run."""

    changed: dict[str, str] = Field(default_factory=dict)
    stale: list[str] = Field(default_factory=list)


class GoldStory(_Case):
    """An optional gold story, attached later with ``evals add-case``."""

    title: str
    persona: str
    want: str
    requirement_categories: list[str] = Field(default_factory=list)


class EvalCase(_Case):
    """One end-to-end eval case."""

    schema_version: str = SCHEMA_VERSION
    id: str
    seed: str
    title: str
    region: Literal["generic", "india"] = "generic"
    scenario: str
    notes: str = ""
    workspace: str = "evalws"
    personas: list[str] = Field(default_factory=lambda: ["Customer"])
    free_text_reply: str = ""
    gold_domain: str
    gold_subdomain: str | None = None
    gold_subpacks: list[str] = Field(default_factory=list)
    expected_items: list[ExpectedItem] = Field(min_length=3)
    ambiguities: list[Ambiguity] = Field(min_length=2)
    answer_key: dict[str, str]
    default_reply: str = "use your judgment"
    planted_pii: list[PlantedPII] = Field(default_factory=list)
    planted_injections: list[str] = Field(default_factory=list)
    memory: MemoryPlan = Field(default_factory=MemoryPlan)
    gold_stories: list[GoldStory] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


def _err(code: str, message: str, location: str) -> Finding:
    return Finding(code=code, message=message, severity=Severity.ERROR, location=location)


def _warn(code: str, message: str, location: str) -> Finding:
    return Finding(code=code, message=message, severity=Severity.WARNING, location=location)


def validate_case(case: EvalCase, packs: PackSet) -> list[Finding]:  # noqa: PLR0912
    """Return findings for anything wrong with a case. Errors mean the case is unusable."""
    findings: list[Finding] = []
    where = case.id
    if not CASE_ID_RE.match(case.id):
        findings.append(_err("CASE_ID", "id must be lowercase letters, digits and dashes", where))
    if case.gold_domain not in packs.ids:
        return [*findings, _err("CASE_DOMAIN", f"unknown domain {case.gold_domain}", where)]
    checklist = packs.checklist(case.gold_domain, case.gold_subdomain, case.gold_subpacks)
    if case.gold_subdomain and checklist.subdomain != case.gold_subdomain:
        findings.append(_err("CASE_SUBDOMAIN", f"unknown sub-domain {case.gold_subdomain}", where))
    known = set(checklist.ids)
    text = normalize(f"{case.scenario}\n{case.notes}")
    redacted = Redactor().redact(f"{case.scenario}\n{case.notes}")
    gaps = {i.category for i in case.expected_items if i.kind == "gap"}
    for item in case.expected_items:
        if item.category not in known:
            findings.append(
                _err("CASE_CATEGORY", f"{item.category} is not in the checklist", where)
            )
        if item.kind == "stated":
            if not item.evidence or normalize(item.evidence) not in text:
                findings.append(
                    _err("CASE_EVIDENCE", f"evidence for {item.category} not found", where)
                )
            elif normalize(item.evidence) not in normalize(redacted):
                findings.append(
                    _err("CASE_EVIDENCE_PII", f"evidence for {item.category} contains PII", where)
                )
    findings.extend(
        _err("CASE_AMBIGUITY", f"{a.category} must be a gap item", where)
        for a in case.ambiguities
        if a.category not in gaps
    )
    findings.extend(
        _err("CASE_ANSWER_KEY", f"no answer for {a.category}", where)
        for a in case.ambiguities
        if a.category not in case.answer_key
    )
    referenced = {*case.answer_key, *case.memory.changed, *case.memory.stale}
    findings.extend(
        _err("CASE_CATEGORY", f"{c} is not in the checklist", where)
        for c in sorted(referenced - known)
    )
    for answer in [*case.answer_key.values(), *case.memory.changed.values()]:
        bad = check_content(answer, 300)
        if bad:
            findings.append(
                _err("CASE_ANSWER_UNSAFE", f"answer fails the memory guard: {bad[0].code}", where)
            )
    for planted in case.planted_pii:
        if normalize(planted.value) not in text:
            findings.append(
                _err("CASE_PII_MISSING", f"planted {planted.label} not in the text", where)
            )
        elif planted.value in redacted:
            findings.append(
                _err("CASE_PII_UNDETECTED", f"planted {planted.label} is not redacted", where)
            )
    detector = InjectionDetector()
    for attack in case.planted_injections:
        if normalize(attack) not in text:
            findings.append(
                _err("CASE_INJECTION_MISSING", "planted injection not in the text", where)
            )
        elif not detector.scan(attack).flagged:
            findings.append(
                _err("CASE_INJECTION_UNDETECTED", "planted injection is not detected", where)
            )
    detected = packs.detect(redacted)
    if detected.domain != case.gold_domain:
        findings.append(_warn("CASE_DETECTION", f"keyword detection says {detected.domain}", where))
    return findings


def datasets_dir() -> Path:
    """Return the directory of shipped eval cases."""
    return Path(str(resources.files("story_agent.evals").joinpath("datasets/cases")))


def load_case(path: Path) -> EvalCase:
    """Read one case from a JSON or YAML file."""
    text = path.read_text(encoding="utf-8")
    if path.suffix in {".yaml", ".yml"}:
        return EvalCase.model_validate(yaml.safe_load(text))
    return EvalCase.model_validate_json(text)


def load_cases_dir(directory: Path | None = None) -> list[EvalCase]:
    """Load every ``*.json`` case in ``directory`` (default: the shipped cases), sorted by id."""
    root = directory or datasets_dir()
    cases = [load_case(p) for p in sorted(root.glob("*.json"))]
    return sorted(cases, key=lambda c: c.id)


def save_case(case: EvalCase, directory: Path | None = None) -> Path:
    """Write a case as ``<id>.json`` and return the path."""
    root = directory or datasets_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{case.id}.json"
    path.write_text(
        json.dumps(case.model_dump(mode="json"), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path
