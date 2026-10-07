from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from story_agent.config import AppConfig
from story_agent.discovery.packs import PackSet
from story_agent.pipeline.numbers import known_numbers, numbers_in, ungrounded_numbers
from story_agent.pipeline.postprocess import (
    DraftedStory,
    assign_ids,
    build_stories,
    check_id_format_and_stability,
    clean_clause,
    epic_prefix,
    find_duplicates,
    find_uncovered,
    identity_key,
    order_and_cap_criteria,
    req_number,
    similarity,
    snap_estimate,
)
from story_agent.pipeline.requirements import derive_requirements, requirement_id
from story_agent.schema import (
    AcceptanceCriterion,
    AnswerKind,
    CriterionKind,
    ItemStatus,
    Priority,
    Provenance,
    ProvenanceType,
    Requirement,
    Story,
)
from tests.helpers import settled


def _settled(app_config: AppConfig, packs: PackSet, prompts_dir: Path):  # type: ignore[no-untyped-def]  # test helper
    return settled(app_config, packs, prompts_dir)


# ---- requirements ---------------------------------------------------------


def test_only_stated_and_confirmed_items_become_requirements(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> None:
    _, _, state, checklist = _settled(app_config, packs, prompts_dir)
    reqs, findings = derive_requirements(state, checklist)
    assert findings == []
    assert state.discovery is not None
    usable = [
        i for i in state.discovery.items if i.status in {ItemStatus.STATED, ItemStatus.CONFIRMED}
    ]
    assert len(reqs) == len(usable)
    assert [r.id for r in reqs] == [requirement_id(n) for n in range(1, len(reqs) + 1)]
    assert all(r.provenance for r in reqs)
    open_items = [
        i for i in state.discovery.items if i.status in {ItemStatus.UNKNOWN, ItemStatus.INFERRED}
    ]
    assert open_items, "optional categories stay open"
    texts = " ".join(r.text for r in reqs)
    for item in open_items:
        assert item.description not in texts


def test_judgment_items_are_marked_assumed_and_answers_are_not(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> None:
    _, _, state, checklist = _settled(app_config, packs, prompts_dir)
    reqs, _ = derive_requirements(state, checklist)
    judged = {a.question_id for a in state.answers if a.kind is AnswerKind.JUDGMENT}
    assert judged
    assumed = [r for r in reqs if r.assumed]
    assert assumed
    for r in assumed:
        assert any(
            p.ref in judged for p in r.provenance if p.type is ProvenanceType.CLARIFICATION_ANSWER
        )
    stated = [
        r for r in reqs if any(p.type is ProvenanceType.SCENARIO_EXCERPT for p in r.provenance)
    ]
    assert stated
    assert not any(r.assumed for r in stated)
    confirmed = [r for r in reqs if not r.assumed and r not in stated]
    assert confirmed
    assert all("(about:" in r.text for r in confirmed)


def test_an_item_without_provenance_is_reported_not_used(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> None:
    _, _, state, checklist = _settled(app_config, packs, prompts_dir)
    assert state.discovery is not None
    before, _ = derive_requirements(state, checklist)
    victim = next(i.id for i in state.discovery.items if i.status is ItemStatus.CONFIRMED)
    items = [
        i.model_copy(update={"provenance": []}) if i.id == victim else i
        for i in state.discovery.items
    ]
    state.discovery = state.discovery.model_copy(update={"items": items})
    reqs, findings = derive_requirements(state, checklist)
    assert [(f.code, f.location) for f in findings] == [("REQ_NO_PROVENANCE", victim)]
    assert len(reqs) == len(before) - 1


def test_no_discovery_means_no_requirements(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> None:
    _, _, state, checklist = _settled(app_config, packs, prompts_dir)
    state.discovery = None
    assert derive_requirements(state, checklist) == ([], [])


# ---- numbers --------------------------------------------------------------


def test_numbers() -> None:
    text = "within 24 hours, up to 5,000 or 12.50 and 7% but not T-1 or abc123 or v2.0"
    assert numbers_in(text) == {"24", "5000", "12.50", "7%"}
    assert numbers_in("30.0 days") == {"30"}
    assert ungrounded_numbers(["wait 24 hours then 3 retries and 1 try, 0 left"], {"24"}) == {"3"}
    assert ungrounded_numbers(["nothing numeric here"], set()) == set()


def test_known_numbers_cover_everything_the_user_said(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> None:
    _, _, state, _ = _settled(app_config, packs, prompts_dir)
    state.redacted_text += " Limit 500."
    state.redacted_notes = "Retries: 4"
    state.review_notes.append("want: 9 items")
    assert {"500", "4", "9"} <= known_numbers(state)


@given(st.lists(st.integers(min_value=2, max_value=10**6), max_size=6))
def test_property_numbers_in_the_known_set_are_never_flagged(values: list[int]) -> None:
    text = " and ".join(f"{v:,} units" for v in values)
    assert ungrounded_numbers([text], {str(v) for v in values}) == set()


# ---- epics, ids, ordering ---------------------------------------------------


def test_epic_prefixes() -> None:
    assert epic_prefix("Card disputes", {}, set()) == "CD"
    assert epic_prefix("Card disputes", {"Card disputes": "dsp"}, set()) == "DSP"
    assert epic_prefix("Card disputes", {}, {"CD"}) == "CDA"
    assert epic_prefix("Payments", {}, set()) == "PAY"
    assert epic_prefix("The Big Payment Safety Net Programme Now", {}, set()) == "BPSNPN"
    assert epic_prefix("!!", {}, set()) == "EPX"
    taken: set[str] = set()
    names = ["Card disputes", "Customer data", "Cash deposits"]
    out = []
    for n in names:
        p = epic_prefix(n, {}, taken)
        taken.add(p)
        out.append(p)
    assert len(set(out)) == 3


def _d(
    epic: str = "E1",
    title: str = "t",
    reqs: tuple[str, ...] = ("REQ-001",),
    priority: Priority = Priority.MUST,
    *,
    feature: str | None = None,
    depends: tuple[str, ...] = (),
) -> DraftedStory:
    return DraftedStory(
        epic, feature, title, "Customer", "w", "b", priority, 3, reqs, (), (), (), depends
    )


def test_ids_follow_epic_order_priority_and_requirements() -> None:
    drafts = [
        _d("Beta", "b1", ("REQ-005",)),
        _d("Alpha", "a2", ("REQ-003",), Priority.SHOULD),
        _d("Alpha", "a1", ("REQ-004",), Priority.MUST),
        _d("Alpha", "af", ("REQ-009",), Priority.MUST, feature="Feat"),
    ]
    result = assign_ids(drafts, [], {})
    assert [(i, d.title) for i, d in result] == [
        ("ALP-0001", "a1"),
        ("ALP-0002", "a2"),
        ("ALP-0003", "af"),
        ("BET-0001", "b1"),
    ]


def test_story_identity_is_stable_and_new_stories_take_the_next_number() -> None:
    prev_drafts = [_d("Alpha", "a1", ("REQ-001",)), _d("Alpha", "a2", ("REQ-002",))]
    previous = [_story(sid, d) for sid, d in assign_ids(prev_drafts, [], {})]
    revised = [
        _d("Alpha", "a0 new", ("REQ-000",)),
        _d("Alpha", "a2", ("REQ-002",)),
        _d("Alpha", "a1", ("REQ-001",)),
    ]
    result = {d.title: i for i, d in assign_ids(revised, previous, {})}
    assert result["a1"] == previous[0].id
    assert result["a2"] == previous[1].id
    assert result["a0 new"] == "ALP-0003"


def _story(sid: str, d: DraftedStory) -> Story:
    return Story(
        id=sid,
        epic=d.epic,
        title=d.title,
        persona=d.persona,
        want=d.want,
        benefit=d.benefit,
        requirement_ids=list(d.requirement_ids),
        provenance=[Provenance(type=ProvenanceType.SCENARIO_EXCERPT, ref="some excerpt text")],
    )


@given(st.permutations(range(6)))
def test_property_ids_do_not_depend_on_draft_order(order: list[int]) -> None:
    base = [
        _d("Alpha", "one", ("REQ-001",)),
        _d("Alpha", "two", ("REQ-002",), Priority.SHOULD),
        _d("Beta", "three", ("REQ-004",)),
        _d("Beta", "four", ("REQ-003",), Priority.COULD),
        _d("Gamma", "five", ("REQ-006",)),
        _d("Gamma", "six", ("REQ-005",)),
    ]
    shuffled = [base[i] for i in order]
    assert assign_ids(shuffled, [], {}) == assign_ids(base, [], {})


def test_identity_key_ignores_case_order_and_spacing() -> None:
    a = identity_key("Epic  One", ["REQ-2", "REQ-1"], "My  Title")
    assert a == identity_key("epic one", ["REQ-1", "REQ-2"], "my title")


def test_req_number() -> None:
    assert req_number("REQ-012") == 12
    assert req_number("junk") == 10**6


def test_snap_estimate(app_config: AppConfig) -> None:
    std = app_config.standards
    assert snap_estimate(4, "fibonacci", std) == 3
    assert snap_estimate(6, "fibonacci", std) == 5
    assert snap_estimate(100, "fibonacci", std) == 13
    assert snap_estimate(7, "tshirt", std) == 8
    assert snap_estimate(500, "hours", std) == 80
    assert snap_estimate(7, "hours", std) == 7
    assert snap_estimate(None, "fibonacci", std) is None
    assert snap_estimate(0, "fibonacci", std) is None


def test_build_stories_provenance_assumptions_nfrs_and_open_questions(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> None:
    _, _, state, checklist = _settled(app_config, packs, prompts_dir)
    reqs, _ = derive_requirements(state, checklist)
    state.requirements = reqs
    stated = next(
        r for r in reqs if any(p.type is ProvenanceType.SCENARIO_EXCERPT for p in r.provenance)
    )
    assumed = next(r for r in reqs if r.assumed)
    nfr_req = next((r for r in reqs if r.category in app_config.standards["nfr_categories"]), None)
    refs = tuple(r.id for r in (stated, assumed) + ((nfr_req,) if nfr_req else ()))
    draft = DraftedStory(
        "Disputes",
        None,
        "Raise dispute",
        "Customer",
        "to dispute",
        "I am heard",
        Priority.MUST,
        4,
        refs,
        (stated.id,),
        (stated.id,),
        (),
        ("other",),
    )
    other = _d("Disputes", "other", (stated.id,))
    stories = build_stories([draft, other], [], state, "fibonacci", app_config.standards)
    story = next(s for s in stories if s.title == "Raise dispute")
    assert story.estimate == 3
    assert story.requirement_ids == sorted(refs, key=req_number)
    assert story.assumptions == [r.text for r in reqs if r.id in refs and r.assumed]
    assert assumed.text in story.assumptions
    assert story.dependencies == [next(s.id for s in stories if s.title == "other")]
    assert {p.element for p in story.provenance} == {"persona", "want", "benefit"}
    persona_refs = [p for p in story.provenance if p.element == "persona"]
    assert {(p.type, p.ref) for p in persona_refs} == {(p.type, p.ref) for p in stated.provenance}
    benefit = [p for p in story.provenance if p.element == "benefit"]
    assert len(benefit) >= len(persona_refs)
    assert set(story.clarification_refs) <= {a.question_id for a in state.answers}
    assert story.clarification_refs
    if nfr_req:
        assert story.nfrs == [nfr_req.text]
    assert 0.1 <= story.confidence < 1.0


# ---- criteria helpers -----------------------------------------------------


def _ac(kind: CriterionKind, n: int = 0) -> AcceptanceCriterion:
    return AcceptanceCriterion(id="", given=f"g{n}", when=f"w{n}", then=f"t{n}", kind=kind)


def test_clean_clause() -> None:
    assert clean_clause("Given a posted transaction.") == "a posted transaction"
    assert clean_clause("  WHEN  the user acts ") == "the user acts"
    assert clean_clause("and then it works") == "then it works"
    assert clean_clause("Thenceforth nothing") == "Thenceforth nothing"


def test_criteria_are_ordered_deduped_and_numbered() -> None:
    items = [
        _ac(CriterionKind.ERROR, 1),
        _ac(CriterionKind.HAPPY, 2),
        _ac(CriterionKind.HAPPY, 2),
        _ac(CriterionKind.EDGE, 3),
    ]
    out = order_and_cap_criteria(items, "S-0001", 10)
    assert [c.kind for c in out] == [CriterionKind.HAPPY, CriterionKind.EDGE, CriterionKind.ERROR]
    assert [c.id for c in out] == ["S-0001-AC01", "S-0001-AC02", "S-0001-AC03"]


def test_capping_keeps_one_of_each_kind() -> None:
    items = [_ac(CriterionKind.HAPPY, n) for n in range(5)] + [
        _ac(CriterionKind.EDGE, 10),
        _ac(CriterionKind.ERROR, 11),
        _ac(CriterionKind.COMPLIANCE, 12),
        _ac(CriterionKind.NFR, 13),
    ]
    out = order_and_cap_criteria(items, "S-1", 5)
    assert len(out) == 5
    assert {c.kind for c in out} == set(CriterionKind)


@given(
    st.lists(st.sampled_from(list(CriterionKind)), min_size=0, max_size=14),
    st.integers(min_value=1, max_value=6),
)
def test_property_cap_is_respected_and_order_is_by_kind(
    kinds: list[CriterionKind], limit: int
) -> None:
    out = order_and_cap_criteria([_ac(k, n) for n, k in enumerate(kinds)], "S-1", limit)
    assert len(out) <= limit
    ranks = [list(CriterionKind).index(c.kind) for c in out]
    assert ranks == sorted(ranks)
    assert len({c.id for c in out}) == len(out)


# ---- checks ---------------------------------------------------------------


def test_duplicate_detection() -> None:
    a = _story("A-0001", _d("A", "Raise a card dispute", ("REQ-001",))).model_copy(
        update={"want": "to raise a card dispute"}
    )
    b = a.model_copy(
        update={"id": "A-0002", "title": "Different", "want": "To raise a card dispute"}
    )
    c = a.model_copy(
        update={
            "id": "A-0003",
            "title": "Unrelated",
            "want": "see my statement",
            "requirement_ids": ["REQ-9"],
        }
    )
    found = find_duplicates([a, b, c], 0.9)
    assert [f.location for f in found] == ["A-0001,A-0002"]
    same_reqs = a.model_copy(
        update={"id": "A-0004", "want": "something else entirely", "title": "Raise a card dispute!"}
    )
    assert find_duplicates([a, same_reqs], 0.9)
    assert similarity("Same  Text", "same text") == 1.0


def test_coverage_gaps() -> None:
    req = Requirement(
        id="REQ-001",
        text="t",
        category="c",
        provenance=[Provenance(type=ProvenanceType.SCENARIO_EXCERPT, ref="excerpt text")],
    )
    req2 = req.model_copy(update={"id": "REQ-002"})
    story = _story("A-0001", _d("A", "x", ("REQ-001",)))
    gaps = find_uncovered([story], [req, req2])
    assert [g.location for g in gaps] == ["REQ-002"]
    assert gaps[0].code == "COVERAGE_GAP"


def test_id_format_and_stability_checks() -> None:
    good = _story("AB-0001", _d("A", "x"))
    assert check_id_format_and_stability([good], []) == []
    bad = good.model_copy(update={"id": "ab-1"})
    assert {f.code for f in check_id_format_and_stability([bad], [])} == {"ID_FORMAT"}
    dup = good.model_copy()
    assert "ID_DUPLICATE" in {f.code for f in check_id_format_and_stability([good, dup], [])}
    moved = good.model_copy(update={"id": "AB-0007"})
    assert [f.code for f in check_id_format_and_stability([moved], [good])] == ["ID_UNSTABLE"]


@pytest.mark.parametrize("n", [1, 7, 12])
def test_requirement_id_format(n: int) -> None:
    assert requirement_id(n) == f"REQ-{n:03d}"
