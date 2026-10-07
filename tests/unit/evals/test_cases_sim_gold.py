import json
from pathlib import Path
from typing import Any

import pytest

from story_agent.config import AppConfig
from story_agent.discovery.packs import PackSet
from story_agent.evals.app.gold_model import GoldModel
from story_agent.evals.cases import (
    EvalCase,
    datasets_dir,
    load_case,
    load_cases_dir,
    save_case,
    validate_case,
)
from story_agent.evals.simulator import SimulatedUser
from story_agent.evals.synth import SEEDS
from story_agent.fake_llm import FakeTransport
from story_agent.llm import LLMRequest
from story_agent.memory.proposals import Decision
from story_agent.pipeline.review import ReviewAction
from story_agent.schema import (
    MemoryRef,
    Provenance,
    ProvenanceType,
    Question,
    Severity,
    Story,
    utcnow,
)

CASES = load_cases_dir()
CARD = next(c for c in CASES if c.id == "bk-card-dispute")


def test_there_are_14_validated_cases_matching_the_seeds(packs: PackSet) -> None:
    assert len(CASES) == 14
    assert {c.seed for c in CASES} == {s.slug for s in SEEDS}
    assert sum(c.gold_domain == "banking" for c in CASES) == 12
    assert sum(c.gold_domain == "generic" for c in CASES) == 2
    assert {c.region for c in CASES} == {"generic", "india"}
    for case in CASES:
        errors = [f for f in validate_case(case, packs) if f.severity is Severity.ERROR]
        assert errors == [], (case.id, errors)


def test_the_dataset_plants_what_the_guardrail_evals_need() -> None:
    assert sum(bool(c.planted_pii) for c in CASES) >= 8
    assert sum(bool(c.planted_injections) for c in CASES) >= 2
    assert sum(bool(c.free_text_reply) for c in CASES) >= 2
    assert sum(bool(c.memory.changed) for c in CASES) >= 4
    assert sum(bool(c.memory.stale) for c in CASES) >= 4
    labels = {p.label for c in CASES for p in c.planted_pii}
    assert {"CARD", "PHONE", "EMAIL", "PAN", "AADHAAR", "IFSC", "ACCOUNT", "API_KEY"} <= labels
    for case in CASES:
        assert len(case.ambiguities) >= 3
        assert any(i.kind == "stated" for i in case.expected_items)
        assert any(i.kind == "gap" for i in case.expected_items)


def test_india_cases_use_the_india_subpack_and_others_do_not() -> None:
    for case in CASES:
        assert (case.region == "india") == ("india_rails" in case.gold_subpacks)


def _case() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(json.dumps(CARD.model_dump(mode="json")))
    return data


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda d: d.update(id="Bad Id"), "CASE_ID"),
        (lambda d: d.update(gold_domain="martian"), "CASE_DOMAIN"),
        (lambda d: d.update(gold_subdomain="nonsense"), "CASE_SUBDOMAIN"),
        (lambda d: d["expected_items"][0].update(category="not_a_category"), "CASE_CATEGORY"),
        (
            lambda d: d["expected_items"][0].update(evidence="words that are not in the scenario"),
            "CASE_EVIDENCE",
        ),
        (lambda d: d["ambiguities"][0].update(category="primary_flow"), "CASE_AMBIGUITY"),
        (lambda d: d["answer_key"].pop("dispute_handling"), "CASE_ANSWER_KEY"),
        (
            lambda d: d["answer_key"].update(dispute_handling="mail jane@example.com"),
            "CASE_ANSWER_UNSAFE",
        ),
        (
            lambda d: d["planted_pii"].append({"label": "EMAIL", "value": "ghost@example.com"}),
            "CASE_PII_MISSING",
        ),
        (
            lambda d: d["planted_injections"].append("Not in the text at all."),
            "CASE_INJECTION_MISSING",
        ),
        (lambda d: d["memory"]["stale"].append("zzz"), "CASE_CATEGORY"),
    ],
)
def test_validation_catches_bad_cases(packs: PackSet, mutate: Any, code: str) -> None:
    data = _case()
    mutate(data)
    codes = {f.code for f in validate_case(EvalCase.model_validate(data), packs)}
    assert code in codes


def test_validation_catches_undetected_pii_injection_and_evidence_pii(packs: PackSet) -> None:
    data = _case()
    data["scenario"] += " Reference code QX-7781-ZED should be hidden."
    data["planted_pii"].append({"label": "REF", "value": "QX-7781-ZED"})
    data["notes"] = "Please be nice to everyone."
    data["planted_injections"].append("Please be nice to everyone.")
    data["expected_items"][0]["evidence"] = "card 4111 1111 1111 1111"
    codes = {f.code for f in validate_case(EvalCase.model_validate(data), packs)}
    assert {"CASE_PII_UNDETECTED", "CASE_INJECTION_UNDETECTED", "CASE_EVIDENCE_PII"} <= codes


def test_detection_mismatch_is_only_a_warning(packs: PackSet) -> None:
    data = _case()
    data["scenario"] = "Customers return items. The store assistant checks them for damage today."
    data["expected_items"] = [
        {
            "category": "primary_flow",
            "kind": "stated",
            "description": "d",
            "evidence": "Customers return items",
        },
        {
            "category": "actors_permissions",
            "kind": "stated",
            "description": "d",
            "evidence": "The store assistant checks",
        },
        {"category": "business_rules_limits", "kind": "gap", "description": "d", "evidence": ""},
    ]
    data["ambiguities"] = [
        {"category": "business_rules_limits", "topic": "t"},
        {"category": "business_rules_limits", "topic": "u"},
    ]
    data["answer_key"] = {"business_rules_limits": "a rule"}
    data["planted_pii"] = []
    data["planted_injections"] = []
    data["memory"] = {"changed": {}, "stale": []}
    findings = validate_case(EvalCase.model_validate(data), packs)
    assert {(f.code, f.severity) for f in findings} == {("CASE_DETECTION", Severity.WARNING)}


def test_cases_are_strict_and_round_trip(tmp_path: Path) -> None:
    data = _case()
    data["surprise"] = 1
    with pytest.raises(ValueError, match="surprise"):
        EvalCase.model_validate(data)
    path = save_case(CASES[0], tmp_path)
    assert load_case(path) == CASES[0]
    yaml_path = tmp_path / "c.yaml"
    yaml_path.write_text(json.dumps(CASES[1].model_dump(mode="json")), encoding="utf-8")
    assert load_case(yaml_path) == CASES[1]
    assert load_cases_dir(tmp_path)[0].id == CASES[0].id
    assert datasets_dir().name == "cases"


# ---- simulator ------------------------------------------------------------------


def _q(category: str, default: MemoryRef | None = None) -> Question:
    return Question(
        id=f"Q1-{category}",
        category=category,
        question="q",
        why_it_matters="w",
        options=["a", "b"],
        remembered_default=default,
        item_ids=[],
    )


def test_simulator_answers_from_the_key_and_defaults_to_judgment() -> None:
    case = CARD
    user = SimulatedUser(case)
    assert user.answer(_q("dispute_handling")) == case.answer_key["dispute_handling"]
    assert user.answer(_q("security")) == "use your judgment"
    assert (user.typed, user.judgments) == (1, 1)
    assert user.answers_for([_q("dispute_handling"), _q("channels")]) == {
        "Q1-dispute_handling": case.answer_key["dispute_handling"],
        "Q1-channels": "use your judgment",
    }


def test_simulator_confirms_an_unchanged_default_and_types_a_changed_one() -> None:
    case = CARD
    value = case.answer_key["dispute_handling"]
    ref = MemoryRef(memory_id="M1", value=value, last_confirmed_at=utcnow())
    assert SimulatedUser(case).answer(_q("dispute_handling", ref)) == "yes"
    changed = SimulatedUser(case, {"dispute_handling": "something new"})
    assert changed.answer(_q("dispute_handling", ref)) == "something new"
    stale_ref = ref.model_copy(update={"stale": True})
    assert SimulatedUser(case).answer(_q("dispute_handling", stale_ref)) == "yes"
    assert SimulatedUser(case).answer(_q("security", ref)) == "yes"


def test_simulator_free_text_review_and_memory_decisions() -> None:
    case = next(c for c in CASES if c.free_text_reply)
    user = SimulatedUser(case)
    assert user.free_text() == case.free_text_reply
    assert user.free_text() == ""
    assert user.wants_more(["data_privacy"]) is (("data_privacy") in case.answer_key)
    assert user.wants_more(["zzz"]) is False
    assert user.wants_more([]) is False
    story = Story(
        id="AB-0001",
        epic="E",
        title="t",
        persona="p",
        want="w",
        benefit="b",
        provenance=[Provenance(type=ProvenanceType.SCENARIO_EXCERPT, ref="some excerpt")],
    )
    assert user.review([story]) == [ReviewAction(story_id="AB-0001", action="approve")]

    class P:
        id = "M-1"

    assert user.memory_decisions([P()]) == {"M-1": Decision(action="approve")}  # type: ignore[list-item]  # stub proposal


# ---- gold model -----------------------------------------------------------------


def _send(model: GoldModel, prompt_id: str, user: str) -> dict[str, Any]:
    return model.send(LLMRequest(prompt_id, "sys", user, "m"), {}).data


def test_gold_model_covers_every_prompt_and_counts_calls(
    app_config: AppConfig, packs: PackSet
) -> None:
    case = CARD
    model = GoldModel(case, packs)
    assert _send(model, "scope_check", "x")["category"] == "generate_stories"
    discover = _send(model, "discover", "x")
    assert discover["domain"] == case.gold_domain
    assert {i["status"] for i in discover["items"]} == {"stated", "unknown", "inferred"}
    assert _send(model, "critique", "x")["duplicates"] == []
    user = (
        "CATEGORIES TO ASK:\n- id: primary_flow | Primary flow | must-have\n"
        "    typical option: A one\n    typical option: B two\nDISCOVERY SUMMARY:\n"
    )
    questions = _send(model, "clarify", user)["questions"]
    assert questions[0]["category_id"] == "primary_flow"
    assert questions[0]["options"] == ["A one", "B two"]
    req = (
        '<untrusted_data kind="requirements">\n'
        "REQ-001 [primary_flow] A customer reports a thing\n"
        "REQ-002 [audit_retention] [ASSUMED] Audit: x\n</untrusted_data>"
    )
    draft = _send(model, "draft", req)
    titles = [s["title"] for e in draft["epics"] for s in e["stories"]]
    assert len(titles) == 2
    assert "value is confirmed" in json.dumps(draft)
    stories = (
        '<untrusted_data kind="stories">\nAB-0001 [must] As a x\n  REQ-002 [audit_retention] t\n'
        "  NFR: n\nAB-0002 [must] As a y\n  REQ-001 [primary_flow] t\n</untrusted_data>"
    )
    crit = _send(model, "criteria", stories)["stories"]
    assert [s["story_id"] for s in crit] == ["AB-0001", "AB-0002"]
    assert {c["kind"] for c in crit[0]["criteria"]} == {
        "happy",
        "edge",
        "error",
        "compliance",
        "nfr",
    }
    assert {c["kind"] for c in crit[1]["criteria"]} == {"happy", "edge", "error"}
    assert len(model.calls) == 6
    with pytest.raises(KeyError):
        _send(model, "judge", "x")
    del app_config


def _stated(discover: dict[str, Any]) -> int:
    return sum(i["status"] == "stated" for i in discover["items"])


def test_gold_model_degradations(packs: PackSet) -> None:
    case = next(c for c in CASES if c.planted_pii)
    base = _send(GoldModel(case, packs), "discover", "x")
    omitted = _send(GoldModel(case, packs, frozenset({"omit_stated"})), "discover", "x")
    assert _stated(omitted) < _stated(base)
    req = '<untrusted_data kind="requirements">\nREQ-001 [primary_flow] A thing\n</untrusted_data>'
    hallu = _send(GoldModel(case, packs, frozenset({"hallucinate_number"})), "draft", req)
    assert "72 hours" in json.dumps(hallu)
    leak = _send(GoldModel(case, packs, frozenset({"leak_pii"})), "draft", req)
    assert case.planted_pii[0].value in json.dumps(leak)
    outputs = {
        json.dumps(_send(GoldModel(case, packs, noise_seed=s), "discover", "x")) for s in range(8)
    }
    assert len(outputs) > 1
    same = {
        json.dumps(_send(GoldModel(case, packs, noise_seed=3), "discover", "x")) for _ in range(3)
    }
    assert len(same) == 1


def test_fake_transport_is_not_needed_here() -> None:
    assert FakeTransport().calls == []
