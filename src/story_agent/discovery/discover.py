"""Stage 4: discover. Builds the DiscoveryMap from the domain pack checklist.

The model classifies; code decides everything else: which checklist applies,
that every category is covered, which `stated` claims are really grounded, the
order of items and their ids.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from story_agent.deps import StageDeps
from story_agent.discovery.packs import Checklist, Detection, PackSet
from story_agent.guardrails.grounding import GroundingVerifier
from story_agent.guardrails.injection import wrap_untrusted
from story_agent.ids import canonical_items
from story_agent.llm import LLMRequest, Usage
from story_agent.memory.render import memory_ids, render_memory_block
from story_agent.prompts import load_prompt
from story_agent.schema import (
    DiscoveryItem,
    DiscoveryMap,
    Finding,
    ItemStatus,
    MemoryEntry,
    Provenance,
    RunState,
    Severity,
)

PROMPT_ID = "discover"
MAX_ITEMS_PER_CATEGORY = 3


class DiscoveredItem(BaseModel):
    """One item as the model returns it. No ids."""

    model_config = ConfigDict(extra="forbid")

    category_id: str
    description: str = Field(min_length=1, max_length=300)
    status: Literal["stated", "inferred", "unknown"]
    evidence: str = Field(default="", max_length=400)


class DiscoverOutput(BaseModel):
    """Model output for the discover stage."""

    model_config = ConfigDict(extra="forbid")

    domain: str
    subdomain: str | None = None
    subpacks: list[str] = Field(default_factory=list, max_length=6)
    actors: list[str] = Field(default_factory=list, max_length=12)
    goals: list[str] = Field(default_factory=list, max_length=10)
    business_context: str = Field(default="", max_length=600)
    items: list[DiscoveredItem] = Field(default_factory=list, max_length=80)


@dataclass
class DiscoverResult:
    """The map, the checklist it was built against, and anything worth reporting."""

    discovery: DiscoveryMap
    checklist: Checklist
    findings: list[Finding] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    request: LLMRequest | None = None


def render_checklist(checklist: Checklist) -> str:
    """Render the trusted checklist block."""
    lines = []
    for category in checklist.categories:
        marker = "must-have" if category.id in checklist.must_have_ids else "optional"
        lines.append(f"- id: {category.id} | {category.name} | {marker}")
        lines.extend(f"    probe: {probe}" for probe in category.probes)
    return "\n".join(lines)


def build_user_message(
    state: RunState,
    packs: PackSet,
    detection: Detection,
    checklist: Checklist,
    memory: Sequence[MemoryEntry],
) -> str:
    """Assemble the user message. Untrusted text is wrapped; configuration is not."""
    known_subs = packs.subdomain_ids(detection.domain)
    known_subpacks = packs.subpack_ids(detection.domain)
    parts = [
        f"ALLOWED DOMAINS: {', '.join(packs.ids)}",
        f"COMPUTED CANDIDATE: domain={detection.domain} subdomain={detection.subdomain} "
        f"subpacks={detection.subpacks}",
        f"ALLOWED SUBDOMAINS for {detection.domain}: {', '.join(known_subs) or 'none'}",
        f"ALLOWED SUBPACKS for {detection.domain}: {', '.join(known_subpacks) or 'none'}",
        "CHECKLIST:",
        render_checklist(checklist),
        "SCENARIO:",
        wrap_untrusted("scenario", state.redacted_text),
        "NOTES:",
        wrap_untrusted("notes", state.redacted_notes or "none"),
        "REMEMBERED ANSWERS:",
        render_memory_block(memory),
    ]
    return "\n".join(parts)


def run_discover(
    deps: StageDeps, state: RunState, memory: Sequence[MemoryEntry] = ()
) -> DiscoverResult:
    """Detect the domain, call the model once, and build a canonical DiscoveryMap."""
    detection = deps.packs.detect(f"{state.redacted_text}\n{state.redacted_notes}")
    first = deps.packs.checklist(detection.domain, detection.subdomain, detection.subpacks)
    prompt = load_prompt(deps.prompts_dir, PROMPT_ID)
    request = LLMRequest(
        prompt_id=PROMPT_ID,
        system=prompt.text,
        user=build_user_message(state, deps.packs, detection, first, memory),
        model=deps.config.models.generator,
        memory_ids=memory_ids(memory),
    )
    result = deps.client.complete(request, DiscoverOutput)
    discovery, checklist, findings = build_map(result.value, deps, detection, first, state)
    return DiscoverResult(discovery, checklist, findings, result.usage, result.cost_usd, request)


def build_map(
    out: DiscoverOutput, deps: StageDeps, detection: Detection, first: Checklist, state: RunState
) -> tuple[DiscoveryMap, Checklist, list[Finding]]:
    """Validate model output against the packs and turn it into a canonical DiscoveryMap."""
    packs = deps.packs
    findings: list[Finding] = []
    domain = out.domain if out.domain in packs.ids else detection.domain
    if domain != out.domain:
        findings.append(
            _warn("DISCOVER_UNKNOWN_DOMAIN", f"model chose unknown domain {out.domain}")
        )
    subs = packs.subdomain_ids(domain)
    subdomain = out.subdomain if out.subdomain in subs else None
    allowed_subpacks = packs.subpack_ids(domain)
    subpacks = [s for s in dict.fromkeys(out.subpacks) if s in allowed_subpacks]
    if (domain, subdomain, subpacks) != (detection.domain, detection.subdomain, detection.subpacks):
        findings.append(
            Finding(
                code="DISCOVER_DOMAIN_OVERRIDE",
                message=(
                    f"model chose {domain}/{subdomain} "
                    f"over computed {detection.domain}/{detection.subdomain}"
                ),
                severity=Severity.INFO,
            )
        )
        checklist = packs.checklist(domain, subdomain, subpacks)
    else:
        checklist = first
    verifier = GroundingVerifier(state, deps.config.guardrails.grounding)
    items, item_findings = _convert_items(out, checklist, verifier)
    findings.extend(item_findings)
    items = _fill_missing(items, checklist)
    items = canonical_items(items, checklist.ids)
    discovery = DiscoveryMap(
        domain=checklist.domain,
        subdomain=checklist.subdomain,
        subpacks=list(checklist.subpacks),
        actors=sorted({a.strip() for a in out.actors if a.strip()}, key=str.casefold),
        goals=sorted({g.strip() for g in out.goals if g.strip()}, key=str.casefold),
        business_context=out.business_context.strip(),
        items=items,
    )
    return discovery, checklist, findings


def _warn(code: str, message: str, location: str | None = None) -> Finding:
    return Finding(code=code, message=message, severity=Severity.WARNING, location=location)


def _convert_items(
    out: DiscoverOutput, checklist: Checklist, verifier: GroundingVerifier
) -> tuple[list[DiscoveryItem], list[Finding]]:
    findings: list[Finding] = []
    items: list[DiscoveryItem] = []
    seen: set[tuple[str, str]] = set()
    per_category: dict[str, int] = {}
    for raw in out.items:
        category = checklist.get(raw.category_id)
        if category is None:
            findings.append(
                _warn("DISCOVER_UNKNOWN_CATEGORY", f"dropped item for {raw.category_id}")
            )
            continue
        key = (raw.category_id, " ".join(raw.description.casefold().split()))
        if key in seen:
            continue
        seen.add(key)
        if per_category.get(raw.category_id, 0) >= MAX_ITEMS_PER_CATEGORY:
            findings.append(
                _warn("DISCOVER_TOO_MANY_ITEMS", f"trimmed items for {raw.category_id}")
            )
            continue
        per_category[raw.category_id] = per_category.get(raw.category_id, 0) + 1
        status = ItemStatus(raw.status)
        provenance: list[Provenance] = []
        if status is ItemStatus.STATED:
            source = verifier.locate(raw.evidence) if raw.evidence else None
            if source is None:
                status = ItemStatus.INFERRED
                findings.append(
                    _warn(
                        "DISCOVER_STATED_NOT_GROUNDED",
                        "stated item lacks verifiable evidence",
                        raw.category_id,
                    )
                )
            else:
                provenance.append(Provenance(type=source, ref=raw.evidence, element="item"))
        items.append(
            DiscoveryItem(
                id="",
                category=raw.category_id,
                description=raw.description.strip(),
                status=status,
                must_have=raw.category_id in checklist.must_have_ids,
                provenance=provenance,
            )
        )
    return items, findings


def _fill_missing(items: list[DiscoveryItem], checklist: Checklist) -> list[DiscoveryItem]:
    covered = {item.category for item in items}
    gaps = [
        DiscoveryItem(
            id="",
            category=category.id,
            description=f"{category.name} is not described in the scenario.",
            status=ItemStatus.UNKNOWN,
            must_have=category.id in checklist.must_have_ids,
        )
        for category in checklist.categories
        if category.id not in covered
    ]
    return [*items, *gaps]
