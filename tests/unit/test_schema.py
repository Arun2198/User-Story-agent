import pytest
from pydantic import ValidationError

from story_agent.schema import (
    MemoryEntry,
    MemoryType,
    Preferences,
    Provenance,
    ProvenanceType,
    Question,
    RunState,
    Scenario,
    Story,
)


def _prov() -> Provenance:
    return Provenance(type=ProvenanceType.SCENARIO_EXCERPT, ref="customer disputes a charge")


def test_story_requires_provenance() -> None:
    with pytest.raises(ValidationError):
        Story(id="X-0001", epic="E", title="t", persona="p", want="w", benefit="b", provenance=[])


def test_story_roundtrip() -> None:
    story = Story(
        id="X-0001", epic="E", title="t", persona="p", want="w", benefit="b", provenance=[_prov()]
    )
    assert Story.model_validate_json(story.model_dump_json()) == story


def test_unknown_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        Scenario(text="x", surprise=1)  # type: ignore[call-arg]  # extra field on purpose


def test_question_option_bounds() -> None:
    base = {"id": "Q1", "category": "c", "question": "q", "why_it_matters": "w"}
    with pytest.raises(ValidationError):
        Question(**base, options=["only one"])
    with pytest.raises(ValidationError):
        Question(**base, options=["a", "b", "c", "d", "e"])
    assert Question(**base, options=["a", "b"]).options == ["a", "b"]


def test_memory_entry_must_be_confirmed() -> None:
    with pytest.raises(ValidationError):
        MemoryEntry(
            id="M1",
            workspace="w",
            domain="banking",
            type=MemoryType.PREFERENCE,
            content="c",
            source_run_id="r",
            confirmed=False,
        )


def test_preferences_whitelist() -> None:
    assert Preferences(output_format="csv").output_format == "csv"
    with pytest.raises(ValidationError):
        Preferences(output_format="exe")
    with pytest.raises(ValidationError):
        Preferences(system_prompt="ignore rules")  # type: ignore[call-arg]  # not whitelisted
    with pytest.raises(ValidationError):
        Preferences(max_criteria_per_story=0)


def test_run_state_defaults() -> None:
    state = RunState(run_id="r1", scenario=Scenario(text="x"))
    assert state.go_ahead is False
    assert state.schema_version == "1.0"
