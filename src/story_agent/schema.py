"""Pydantic models shared across the pipeline.

Every model forbids unknown fields. Models that cross a persistence or LLM
boundary carry ``schema_version`` so old payloads can be detected.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = "1.0"


def utcnow() -> datetime:
    """Return the current UTC time."""
    return datetime.now(UTC)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Intent(StrEnum):
    """Intents the agent is allowed to act on."""

    GENERATE_STORIES = "generate_stories"
    ANSWER_CLARIFICATION = "answer_clarification"
    REFINE_STORIES = "refine_stories"
    EXPORT_STORIES = "export_stories"
    MANAGE_MEMORY = "manage_memory"


class ItemStatus(StrEnum):
    """Lifecycle of a discovery item."""

    STATED = "stated"
    INFERRED = "inferred"
    UNKNOWN = "unknown"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class ProvenanceType(StrEnum):
    """Where a fact in a story or requirement came from."""

    SCENARIO_EXCERPT = "scenario_excerpt"
    CLARIFICATION_ANSWER = "clarification_answer"
    MEMORY_CONFIRMED = "memory_confirmed"
    USER_NOTES = "user_notes"
    DOMAIN_SUGGESTION_ACCEPTED = "domain_suggestion_accepted"


class Priority(StrEnum):
    """MoSCoW priority."""

    MUST = "must"
    SHOULD = "should"
    COULD = "could"
    WONT = "wont"


class StoryStatus(StrEnum):
    """Review state of a story."""

    DRAFT = "draft"
    APPROVED = "approved"
    EDITED = "edited"
    REJECTED = "rejected"


class CriterionKind(StrEnum):
    """Category of an acceptance criterion."""

    HAPPY = "happy"
    EDGE = "edge"
    ERROR = "error"
    COMPLIANCE = "compliance"
    NFR = "nfr"


class MemoryType(StrEnum):
    """Kinds of saved memory entries."""

    CONFIRMED_ANSWER = "confirmed_answer"
    DECISION = "decision"
    GLOSSARY = "glossary"
    NFR_DEFAULT = "nfr_default"
    PREFERENCE = "preference"


class AnswerKind(StrEnum):
    """How a question was answered."""

    OPTION = "option"
    OTHER = "other"
    JUDGMENT = "judgment"
    DEFERRED = "deferred"
    MEMORY_CONFIRMED = "memory_confirmed"


class HookPhase(StrEnum):
    """When a hook runs relative to a component."""

    PRE = "pre"
    POST = "post"


class HookAction(StrEnum):
    """What a hook asks the runner to do."""

    # Enum value, not a credential.
    PASS = "pass"  # noqa: S105  # nosec B105
    MODIFY = "modify"
    BLOCK = "block"


class Severity(StrEnum):
    """Severity of a finding."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Finding(_Model):
    """A single issue reported by a hook, guardrail or critic."""

    code: str
    message: str
    severity: Severity = Severity.WARNING
    location: str | None = None


class HookResult(_Model):
    """Outcome of one hook run."""

    action: HookAction = HookAction.PASS
    findings: list[Finding] = Field(default_factory=list)
    payload: dict[str, Any] | None = None


class Provenance(_Model):
    """Link from a story element back to its source."""

    type: ProvenanceType
    ref: str = Field(description="Excerpt text, or the id of the answer or memory entry")
    element: str = Field(default="story", description="persona, want, benefit, criterion, ...")


class Preferences(_Model):
    """Whitelisted settings that free text is allowed to influence."""

    max_criteria_per_story: int | None = Field(default=None, ge=1, le=20)
    output_format: str | None = None
    estimate_scale: str | None = None

    @field_validator("output_format")
    @classmethod
    def _known_format(cls, value: str | None) -> str | None:
        if value is not None and value not in {"md", "csv", "json"}:
            raise ValueError("output_format must be md, csv or json")
        return value


class Scenario(_Model):
    """The user's scenario and optional notes, as received."""

    schema_version: str = SCHEMA_VERSION
    text: str
    notes: str = ""
    workspace: str = "default"


class DiscoveryItem(_Model):
    """One checklist item in the discovery map."""

    id: str
    category: str
    description: str
    status: ItemStatus
    must_have: bool = False
    provenance: list[Provenance] = Field(default_factory=list)


class DiscoveryMap(_Model):
    """Structured result of the discover stage."""

    schema_version: str = SCHEMA_VERSION
    domain: str
    subdomain: str | None = None
    actors: list[str] = Field(default_factory=list)
    goals: list[str] = Field(default_factory=list)
    business_context: str = ""
    items: list[DiscoveryItem] = Field(default_factory=list)


class MemoryRef(_Model):
    """Pointer to a remembered answer offered as a default."""

    memory_id: str
    value: str
    last_confirmed_at: datetime
    stale: bool = False


class Question(_Model):
    """A clarification question."""

    id: str
    category: str
    question: str
    why_it_matters: str
    options: list[str] = Field(min_length=2, max_length=4)
    remembered_default: MemoryRef | None = None
    item_ids: list[str] = Field(default_factory=list)


class Answer(_Model):
    """The user's answer to one question."""

    question_id: str
    kind: AnswerKind
    value: str = ""
    memory_id: str | None = None


class MemoryEntry(_Model):
    """A saved, user-confirmed memory entry."""

    schema_version: str = SCHEMA_VERSION
    id: str
    workspace: str
    domain: str
    subdomain: str | None = None
    tags: list[str] = Field(default_factory=list)
    type: MemoryType
    content: str
    source_run_id: str
    confirmed: bool = True
    created_at: datetime = Field(default_factory=utcnow)
    last_confirmed_at: datetime = Field(default_factory=utcnow)
    use_count: int = 0
    ttl_days: int = Field(default=180, ge=1)

    @field_validator("confirmed")
    @classmethod
    def _always_confirmed(cls, value: bool) -> bool:
        if not value:
            raise ValueError("memory entries must be confirmed")
        return value


class Requirement(_Model):
    """A confirmed requirement that stories trace back to."""

    id: str
    text: str
    category: str
    provenance: list[Provenance] = Field(min_length=1)


class AcceptanceCriterion(_Model):
    """One Given/When/Then criterion."""

    id: str
    given: str
    when: str
    then: str
    kind: CriterionKind = CriterionKind.HAPPY


class Story(_Model):
    """A user story with criteria and traceability."""

    schema_version: str = SCHEMA_VERSION
    id: str
    epic: str
    feature: str | None = None
    title: str
    persona: str
    want: str
    benefit: str
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    priority: Priority = Priority.SHOULD
    estimate: int | None = None
    nfrs: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)
    clarification_refs: list[str] = Field(default_factory=list)
    provenance: list[Provenance] = Field(min_length=1)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    status: StoryStatus = StoryStatus.DRAFT


class QuestionRound(_Model):
    """Questions asked in one clarification round."""

    number: int = Field(ge=1, le=3)
    questions: list[Question] = Field(max_length=6)
    free_text_reply: str = ""


class RunState(_Model):
    """Everything the graph carries between stages."""

    schema_version: str = SCHEMA_VERSION
    run_id: str
    scenario: Scenario
    redacted_text: str = ""
    redacted_notes: str = ""
    quarantined: list[Finding] = Field(default_factory=list)
    preferences: Preferences = Field(default_factory=Preferences)
    recalled_memory_ids: list[str] = Field(default_factory=list)
    discovery: DiscoveryMap | None = None
    rounds: list[QuestionRound] = Field(default_factory=list)
    answers: list[Answer] = Field(default_factory=list)
    go_ahead: bool = False
    requirements: list[Requirement] = Field(default_factory=list)
    stories: list[Story] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    review_log: list[dict[str, Any]] = Field(default_factory=list)
    stage: str = "intake"
    tokens_used: int = 0
    cost_usd: float = 0.0
    steps: int = 0


class EvalReport(_Model):
    """Result of one eval run."""

    schema_version: str = SCHEMA_VERSION
    name: str
    kind: str
    metrics: dict[str, float] = Field(default_factory=dict)
    thresholds_met: bool = True
    failures: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
