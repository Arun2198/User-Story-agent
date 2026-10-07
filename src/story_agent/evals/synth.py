"""Synthetic scenario generator for offline evals.

For each seed it asks the model for a deliberately underspecified scenario with gold
labels, a hidden answer key, planted PII and planted injection attempts. Every result
is checked by ``validate_case`` and repaired once; a case that still fails is rejected.
Needs a live model. The shipped cases in ``datasets/cases`` were written by hand with
the same schema, so offline evals do not depend on this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from story_agent.config import AppConfig
from story_agent.discovery.packs import PackSet
from story_agent.evals.cases import (
    Ambiguity,
    EvalCase,
    ExpectedItem,
    GoldStory,
    MemoryPlan,
    PlantedPII,
    validate_case,
)
from story_agent.llm import LLMClient, LLMRequest
from story_agent.prompts import load_prompt
from story_agent.schema import Finding, Severity

PROMPT_ID = "scenario_synth"


@dataclass(frozen=True)
class Seed:
    """A scenario seed with the domain settings the gold labels must use."""

    slug: str
    description: str
    subdomain: str | None
    subpacks: tuple[str, ...] = ()
    region: Literal["generic", "india"] = "generic"
    domain: str = "banking"


SEEDS: tuple[Seed, ...] = (
    Seed(
        "card-dispute",
        "Customer disputes a card transaction and expects a provisional credit.",
        "cards",
        ("india_rails",),
        "india",
    ),
    Seed(
        "failed-payment-refund",
        "A payment fails after the account is debited and the customer wants an automatic refund.",
        "payments_transfers",
    ),
    Seed(
        "beneficiary-cooling-off",
        "Add a new beneficiary with a cooling-off period before large transfers.",
        "payments_transfers",
        ("india_rails",),
        "india",
    ),
    Seed(
        "fd-premature-withdrawal",
        "Premature withdrawal of a fixed deposit with penalty calculation.",
        "accounts_deposits",
    ),
    Seed(
        "retail-loan",
        "Retail loan application with income verification and a credit decision.",
        "lending",
    ),
    Seed(
        "digital-kyc",
        "Digital KYC onboarding for a new savings account with tiered limits.",
        "onboarding_kyc",
        ("india_rails",),
        "india",
    ),
    Seed(
        "standing-instruction",
        "Standing instruction for recurring bill payments with failure retries.",
        "payments_transfers",
        ("india_rails",),
        "india",
    ),
    Seed(
        "fraud-alert",
        "Fraud alert that blocks a transaction and asks the customer to confirm.",
        "fraud_alerts",
    ),
    Seed(
        "maker-checker-transfer",
        "Relationship manager approves a high-value corporate transfer (maker-checker).",
        "corporate_banking",
    ),
    Seed(
        "statement-custom-range",
        "Customer requests an account statement for a custom date range.",
        "accounts_deposits",
    ),
    Seed(
        "complaint-sla",
        "Customer raises a complaint and tracks resolution against an SLA.",
        "customer_service",
    ),
    Seed(
        "overdraft", "Overdraft facility with daily limit checks and charges.", "accounts_deposits"
    ),
    Seed(
        "retail-returns",
        "Customers return items to a shop and get a refund.",
        None,
        (),
        "generic",
        "generic",
    ),
    Seed(
        "hospital-booking",
        "Patients book hospital appointments online.",
        None,
        (),
        "generic",
        "generic",
    ),
)


class SynthCase(BaseModel):
    """What the model returns. Ids and domain settings come from the seed."""

    model_config = ConfigDict(extra="forbid")

    scenario: str = Field(min_length=40, max_length=1200)
    notes: str = Field(default="", max_length=400)
    free_text_reply: str = Field(default="", max_length=300)
    expected_items: list[ExpectedItem] = Field(min_length=3, max_length=16)
    ambiguities: list[Ambiguity] = Field(min_length=2, max_length=8)
    answer_key: dict[str, str]
    planted_pii: list[PlantedPII] = Field(default_factory=list, max_length=6)
    planted_injections: list[str] = Field(default_factory=list, max_length=3)
    memory: MemoryPlan = Field(default_factory=MemoryPlan)
    gold_stories: list[GoldStory] = Field(default_factory=list, max_length=12)


@dataclass
class SynthResult:
    """A validated case, or the findings that rejected it."""

    case: EvalCase | None
    findings: list[Finding] = field(default_factory=list)
    attempts: int = 0


def case_id(seed: Seed, variant: int) -> str:
    """Return the id for a seed and variant number."""
    prefix = "bk" if seed.domain == "banking" else "gn"
    return f"{prefix}-{seed.slug}" if variant == 1 else f"{prefix}-{seed.slug}-v{variant}"


def _build(seed: Seed, variant: int, out: SynthCase) -> EvalCase:
    return EvalCase(
        id=case_id(seed, variant),
        seed=seed.slug,
        title=seed.description,
        region=seed.region,
        gold_domain=seed.domain,
        gold_subdomain=seed.subdomain,
        gold_subpacks=list(seed.subpacks),
        **out.model_dump(),
    )


def render_request(seed: Seed, packs: PackSet, variant: int, problems: list[Finding]) -> str:
    """Render the user message: seed, allowed categories and, on a repair, the problems."""
    checklist = packs.checklist(seed.domain, seed.subdomain, seed.subpacks)
    lines = [
        f"SEED: {seed.description}",
        f"REGION: {seed.region}",
        f"VARIANT: {variant}",
        f"DOMAIN: {seed.domain}; SUBDOMAIN: {seed.subdomain}; SUBPACKS: {list(seed.subpacks)}",
        "CHECKLIST CATEGORY IDS (must-have first):",
        ", ".join(sorted(checklist.must_have_ids))
        + " | "
        + ", ".join(c for c in checklist.ids if c not in checklist.must_have_ids),
    ]
    if problems:
        lines.append("FIX THESE PROBLEMS FROM YOUR LAST ATTEMPT:")
        lines.extend(f"{f.code}: {f.message}" for f in problems)
    return "\n".join(lines)


def synthesize(  # noqa: PLR0913, PLR0917  (a generation call needs these inputs)
    client: LLMClient,
    config: AppConfig,
    packs: PackSet,
    prompts_dir: Path,
    seed: Seed,
    variant: int = 1,
) -> SynthResult:
    """Generate one case, validate it, and repair once if validation finds errors."""
    prompt = load_prompt(prompts_dir, PROMPT_ID)
    problems: list[Finding] = []
    result = SynthResult(None)
    for attempt in (1, 2):
        request = LLMRequest(
            PROMPT_ID,
            prompt.text,
            render_request(seed, packs, variant, problems),
            config.models.generator,
        )
        output = client.complete(request, SynthCase).value
        result.attempts = attempt
        case = _build(seed, variant, output)
        findings = validate_case(case, packs)
        errors = [f for f in findings if f.severity is Severity.ERROR]
        result.findings = findings
        if not errors:
            result.case = case
            return result
        problems = errors
    return result
