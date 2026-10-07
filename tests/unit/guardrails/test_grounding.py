import pytest

from story_agent.config import AppConfig
from story_agent.guardrails.grounding import GroundingVerifier, normalize
from story_agent.schema import (
    Answer,
    AnswerKind,
    DiscoveryItem,
    DiscoveryMap,
    ItemStatus,
    Provenance,
    ProvenanceType,
    Requirement,
    RunState,
    Scenario,
    Story,
)

SCENARIO = (
    "A customer disputes a card transaction and expects a provisional credit "
    "while the bank investigates."
)


def _state(**kwargs: object) -> RunState:
    base: dict[str, object] = {
        "run_id": "r1",
        "scenario": Scenario(text=SCENARIO, notes="Keep it short for the demo audience."),
        "redacted_text": SCENARIO,
        "redacted_notes": "Keep it short for the demo audience.",
    }
    base.update(kwargs)
    return RunState.model_validate(base)


def _verifier(state: RunState, app_config: AppConfig) -> GroundingVerifier:
    return GroundingVerifier(state, app_config.guardrails.grounding)


def _prov(kind: ProvenanceType, ref: str, element: str = "story") -> Provenance:
    return Provenance(type=kind, ref=ref, element=element)


def _story(provenance: list[Provenance], **kwargs: object) -> Story:
    base: dict[str, object] = {
        "id": "S-0001",
        "epic": "E",
        "title": "t",
        "persona": "customer",
        "want": "w",
        "benefit": "b",
        "provenance": provenance,
    }
    base.update(kwargs)
    return Story.model_validate(base)


def _full(ref: str = "a customer disputes a card transaction") -> list[Provenance]:
    return [_prov(ProvenanceType.SCENARIO_EXCERPT, ref, e) for e in ("persona", "want", "benefit")]


def test_normalize() -> None:
    quote, ldq, rdq, dash = chr(0x2019), chr(0x201C), chr(0x201D), chr(0x2014)
    assert normalize(f"  Hello{quote}s   {ldq}World{rdq}{dash}x ") == 'hello\'s "world"-x'


def test_exact_excerpt(app_config: AppConfig) -> None:
    v = _verifier(_state(), app_config)
    assert v.excerpt_found("provisional credit", normalize(SCENARIO))
    assert v.excerpt_found("PROVISIONAL   credit", normalize(SCENARIO))


def test_strict_fuzzy_accepts_small_differences_only(app_config: AppConfig) -> None:
    v = _verifier(_state(), app_config)
    source = normalize(SCENARIO)
    assert v.excerpt_found("a customer dispute a card transaction and expects", source)
    assert not v.excerpt_found("a customer cancels a loan and expects a visit", source)


@pytest.mark.parametrize("excerpt", ["", "bank", "a b"])
def test_short_excerpts_need_exact_match_and_length(app_config: AppConfig, excerpt: str) -> None:
    assert not _verifier(_state(), app_config).excerpt_found(excerpt, normalize(SCENARIO))


def test_fuzzy_off_for_few_words(app_config: AppConfig) -> None:
    v = _verifier(_state(), app_config)
    assert not v.excerpt_found("provisional credits", normalize(SCENARIO))


def test_grounded_story_passes(app_config: AppConfig) -> None:
    assert _verifier(_state(), app_config).check_story(_story(_full())) == []


def test_missing_excerpt_fails(app_config: AppConfig) -> None:
    story = _story(_full("a customer orders a pizza and a drink"))
    codes = {f.code for f in _verifier(_state(), app_config).check_story(story)}
    assert codes == {"GROUND_EXCERPT_NOT_FOUND"}


def test_uncovered_elements_fail(app_config: AppConfig) -> None:
    story = _story([_prov(ProvenanceType.SCENARIO_EXCERPT, "provisional credit", "want")])
    findings = _verifier(_state(), app_config).check_story(story)
    assert {f.location for f in findings} == {"S-0001:persona", "S-0001:benefit"}


def test_notes_excerpt(app_config: AppConfig) -> None:
    v = _verifier(_state(), app_config)
    ok = _prov(ProvenanceType.USER_NOTES, "keep it short for the demo")
    bad = _prov(ProvenanceType.USER_NOTES, "make it very long and detailed")
    assert v.check_provenance(ok, "x") is None
    assert v.check_provenance(bad, "x") is not None


def test_clarification_answer_must_exist_and_not_be_deferred(app_config: AppConfig) -> None:
    answers = [
        Answer(question_id="Q1", kind=AnswerKind.OPTION, value="24h"),
        Answer(question_id="Q2", kind=AnswerKind.DEFERRED),
    ]
    v = _verifier(_state(answers=answers), app_config)
    assert v.check_provenance(_prov(ProvenanceType.CLARIFICATION_ANSWER, "Q1"), "x") is None
    assert v.check_provenance(_prov(ProvenanceType.CLARIFICATION_ANSWER, "Q2"), "x")
    assert v.check_provenance(_prov(ProvenanceType.CLARIFICATION_ANSWER, "Q9"), "x")


def test_memory_must_be_confirmed_by_the_user_in_this_run(app_config: AppConfig) -> None:
    answers = [Answer(question_id="Q1", kind=AnswerKind.MEMORY_CONFIRMED, memory_id="M1")]
    v = _verifier(_state(answers=answers, recalled_memory_ids=["M1", "M2"]), app_config)
    assert v.check_provenance(_prov(ProvenanceType.MEMORY_CONFIRMED, "M1"), "x") is None
    assert v.check_provenance(_prov(ProvenanceType.MEMORY_CONFIRMED, "M2"), "x")


def test_domain_suggestion_needs_confirmed_item(app_config: AppConfig) -> None:
    items = [
        DiscoveryItem(id="D1", category="c", description="d", status=ItemStatus.CONFIRMED),
        DiscoveryItem(id="D2", category="c", description="d", status=ItemStatus.INFERRED),
    ]
    state = _state(discovery=DiscoveryMap(domain="banking", items=items))
    v = _verifier(state, app_config)
    assert v.check_provenance(_prov(ProvenanceType.DOMAIN_SUGGESTION_ACCEPTED, "D1"), "x") is None
    assert v.check_provenance(_prov(ProvenanceType.DOMAIN_SUGGESTION_ACCEPTED, "D2"), "x")


def test_requirement_and_clarification_links(app_config: AppConfig) -> None:
    req = Requirement(
        id="REQ-1",
        text="t",
        category="c",
        provenance=[_prov(ProvenanceType.SCENARIO_EXCERPT, "provisional credit")],
    )
    state = _state(requirements=[req], answers=[Answer(question_id="Q1", kind=AnswerKind.OPTION)])
    v = _verifier(state, app_config)
    good = _story(_full(), requirement_ids=["REQ-1"], clarification_refs=["Q1"])
    bad = _story(_full(), requirement_ids=["REQ-9"], clarification_refs=["Q7"])
    assert v.check_story(good) == []
    assert {f.code for f in v.check_story(bad)} == {"GROUND_REQ_MISSING", "GROUND_REF_MISSING"}


def test_assumption_needs_a_judgment_answer(app_config: AppConfig) -> None:
    answers = [
        Answer(question_id="Q1", kind=AnswerKind.JUDGMENT),
        Answer(question_id="Q2", kind=AnswerKind.OPTION, value="x"),
    ]
    v = _verifier(_state(answers=answers), app_config)
    linked = _story(_full(), assumptions=["a"], clarification_refs=["Q1"])
    wrong_kind = _story(_full(), assumptions=["a"], clarification_refs=["Q2"])
    none = _story(_full(), assumptions=["a"])
    assert v.check_story(linked) == []
    assert {f.code for f in v.check_story(wrong_kind)} == {"GROUND_ASSUMPTION_UNLINKED"}
    assert {f.code for f in v.check_story(none)} == {"GROUND_ASSUMPTION_UNLINKED"}


def test_empty_provenance_is_rejected_even_if_schema_is_bypassed(app_config: AppConfig) -> None:
    story = _story(_full()).model_copy(update={"provenance": []})
    req = Requirement(
        id="R", text="t", category="c", provenance=[_prov(ProvenanceType.SCENARIO_EXCERPT, "x" * 9)]
    ).model_copy(update={"provenance": []})
    v = _verifier(_state(), app_config)
    assert v.check_story(story)[0].code == "GROUND_NO_PROVENANCE"
    assert v.check_requirement(req)[0].code == "GROUND_NO_PROVENANCE"


def test_report(app_config: AppConfig) -> None:
    good = _story(_full())
    bad = _story(_full("an invented sentence about pizza"), id="S-0002")
    report = _verifier(_state(), app_config).verify([good, bad], [])
    assert not report.ok
    assert report.ungrounded_ids == ["S-0002"]
    assert _verifier(_state(), app_config).verify([good], []).ok
