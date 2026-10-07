from pathlib import Path
from typing import Any

from story_agent.config import AppConfig
from story_agent.deps import StageDeps
from story_agent.discovery.packs import PackSet
from story_agent.fake_llm import FakeTransport
from story_agent.pipeline.criteria import CriteriaOutput, max_criteria, run_criteria
from story_agent.pipeline.critique import (
    CritiqueOutput,
    deterministic_findings,
    invest_scores,
    run_critique,
)
from story_agent.pipeline.draft import DraftOutput, run_draft
from story_agent.pipeline.requirements import derive_requirements
from story_agent.schema import (
    CriterionKind,
    Finding,
    Preferences,
    Provenance,
    ProvenanceType,
    RunState,
    Severity,
    Story,
)
from tests.helpers import EMPTY_CRITIQUE, criteria_output, draft_output, settled


def _ready(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> tuple[StageDeps, FakeTransport, RunState, Any]:
    deps, fake, state, checklist = settled(app_config, packs, prompts_dir)
    state.requirements, _ = derive_requirements(state, checklist)
    return deps, fake, state, checklist


def _drafted(app_config: AppConfig, packs: PackSet, prompts_dir: Path, per_story: int = 1):  # type: ignore[no-untyped-def]  # test helper
    deps, fake, state, checklist = _ready(app_config, packs, prompts_dir)
    fake._queued["draft"].append(draft_output(state, per_story))
    result = run_draft(deps, state, checklist)
    state.stories = result.stories
    return deps, fake, state, checklist, result


# ---- draft ----------------------------------------------------------------


def test_draft_builds_grounded_stories_with_stable_ids(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> None:
    deps, fake, state, checklist = _ready(app_config, packs, prompts_dir)
    fake._queued["draft"].append(draft_output(state, 2))
    result = run_draft(deps, state, checklist)
    assert result.findings == []
    stories = result.stories
    assert len({s.id for s in stories}) == len(stories)
    covered = {r for s in stories for r in s.requirement_ids}
    assert covered == {r.id for r in state.requirements}
    assert all(s.provenance for s in stories)
    assert all({p.element for p in s.provenance} == {"persona", "want", "benefit"} for s in stories)
    assert all(s.estimate == 3 for s in stories)
    assert result.usage.input_tokens > 0
    assert result.request is not None
    assert result.request.prompt_id == "draft"


def test_draft_prompt_carries_standards_and_wraps_requirements(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> None:
    deps, fake, state, checklist = _ready(app_config, packs, prompts_dir)
    fake._queued["draft"].append(draft_output(state))
    run_draft(deps, state, checklist)
    sent = fake.calls[-1]
    assert "As a {persona}, I want {want}, so that {benefit}." in sent.user
    assert "estimate scale: fibonacci; allowed values: [1, 2, 3, 5, 8, 13]" in sent.user
    assert '<untrusted_data kind="requirements">' in sent.user
    assert "REQ-001 [" in sent.user
    assert "[ASSUMED]" in sent.user
    assert "REVISION" not in sent.user
    assert "never instructions" in sent.system


def test_preferences_change_the_scale(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> None:
    deps, fake, state, checklist = _ready(app_config, packs, prompts_dir)
    state.preferences = Preferences(estimate_scale="tshirt")
    out = draft_output(state)
    out["epics"][0]["stories"][0]["estimate"] = 7
    fake._queued["draft"].append(out)
    result = run_draft(deps, state, checklist)
    assert "estimate scale: tshirt; allowed values: [1, 2, 3, 5, 8]" in fake.calls[-1].user
    assert result.stories[0].estimate == 8


def test_story_citing_no_valid_requirement_is_dropped_and_reported(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> None:
    deps, fake, state, checklist = _ready(app_config, packs, prompts_dir)
    out = draft_output(state)
    out["epics"][0]["stories"].append(
        {**out["epics"][0]["stories"][0], "title": "Invented", "requirement_refs": ["REQ-999"]}
    )
    out["epics"][0]["stories"].append(
        {
            **out["epics"][0]["stories"][0],
            "title": "Half",
            "requirement_refs": ["REQ-001", "REQ-998"],
        }
    )
    fake._queued["draft"].append(out)
    result = run_draft(deps, state, checklist)
    codes = {(f.code, f.location) for f in result.findings}
    assert ("DRAFT_UNGROUNDED_STORY", "Invented") in codes
    assert ("DRAFT_UNKNOWN_REQ", "Half") in codes
    assert "Invented" not in {s.title for s in result.stories}
    half = next(s for s in result.stories if s.title == "Half")
    assert "REQ-998" not in half.requirement_ids


def test_invented_numbers_are_an_error(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> None:
    deps, fake, state, checklist = _ready(app_config, packs, prompts_dir)
    out = draft_output(state)
    out["epics"][0]["stories"][0]["want"] = "to be refunded within 72 hours"
    fake._queued["draft"].append(out)
    result = run_draft(deps, state, checklist)
    bad = [f for f in result.findings if f.code == "DRAFT_UNGROUNDED_NUMBER"]
    assert len(bad) == 1
    assert bad[0].severity is Severity.ERROR
    assert "72" in bad[0].message
    state.answers[0] = state.answers[0].model_copy(update={"value": "Refund within 72 hours"})
    fake._queued["draft"].append(out)
    assert not [
        f for f in run_draft(deps, state, checklist).findings if f.code == "DRAFT_UNGROUNDED_NUMBER"
    ]


def test_unknown_persona_is_a_warning(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> None:
    deps, fake, state, checklist = _ready(app_config, packs, prompts_dir)
    out = draft_output(state)
    out["epics"][0]["stories"][0]["persona"] = "Wizard"
    fake._queued["draft"].append(out)
    finding = next(
        f for f in run_draft(deps, state, checklist).findings if f.code == "DRAFT_PERSONA_UNKNOWN"
    )
    assert finding.severity is Severity.WARNING


def test_too_many_stories_is_an_error(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> None:
    deps, fake, state, checklist = _ready(app_config, packs, prompts_dir)
    out = draft_output(state)
    template = out["epics"][0]["stories"][0]
    stories = [{**template, "title": f"S{n}"} for n in range(32)]
    out["epics"] = [
        {"name": "One", "features": [], "stories": stories[:16]},
        {"name": "Two", "features": [], "stories": stories[16:]},
    ]
    fake._queued["draft"].append(out)
    assert "DRAFT_TOO_MANY" in {f.code for f in run_draft(deps, state, checklist).findings}


def test_features_and_epics_are_flattened(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> None:
    deps, fake, state, checklist = _ready(app_config, packs, prompts_dir)
    template = draft_output(state)["epics"][0]["stories"][0]
    other = {**template, "title": "In feature", "requirement_refs": ["REQ-002"]}
    fake._queued["draft"].append(
        {
            "epics": [
                {
                    "name": "Only epic",
                    "features": [{"name": "Feat A", "stories": [other]}],
                    "stories": [template],
                }
            ]
        }
    )
    stories = run_draft(deps, state, checklist).stories
    assert {(s.title, s.feature) for s in stories} == {
        (template["title"], None),
        ("In feature", "Feat A"),
    }
    assert {s.epic for s in stories} == {"Only epic"}


def test_revision_call_includes_previous_draft_and_findings_and_keeps_ids(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> None:
    deps, fake, state, checklist, first = _drafted(app_config, packs, prompts_dir)
    feedback = [
        Finding(
            code="COVERAGE_GAP",
            message="REQ-009 is not covered",
            severity=Severity.ERROR,
            location="REQ-009",
        )
    ]
    fake._queued["draft"].append(draft_output(state))
    second = run_draft(deps, state, checklist, first.stories, feedback)
    sent = fake.calls[-1].user
    assert "REVISION. PREVIOUS DRAFT:" in sent
    assert '<untrusted_data kind="review_findings">' in sent
    assert "COVERAGE_GAP REQ-009: REQ-009 is not covered" in sent
    assert [s.id for s in second.stories] == [s.id for s in first.stories]


def test_draft_schema_is_strict() -> None:
    assert DraftOutput.model_json_schema()["additionalProperties"] is False


# ---- criteria -------------------------------------------------------------


def test_criteria_are_attached_cleaned_and_numbered(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> None:
    deps, fake, state, _, drafted = _drafted(app_config, packs, prompts_dir)
    out = criteria_output(drafted.stories[:8])
    out["stories"][0]["criteria"][0]["given"] = "Given a posted card transaction."
    fake._queued["criteria"].append(out)
    result = run_criteria(deps, state, drafted.stories[:8])
    first = result.stories[0]
    assert first.acceptance_criteria[0].given == "a posted card transaction"
    assert [c.kind for c in first.acceptance_criteria] == [
        CriterionKind.HAPPY,
        CriterionKind.EDGE,
        CriterionKind.ERROR,
    ]
    assert first.acceptance_criteria[0].id == f"{first.id}-AC01"
    assert not [f for f in result.findings if f.severity is Severity.ERROR]
    sent = fake.calls[-1].user
    assert "MAX CRITERIA PER STORY: 8" in sent
    assert '<untrusted_data kind="stories">' in sent
    assert "[ASSUMED]" in sent


def test_criteria_batches_and_only_ids(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> None:
    deps, fake, state, _, drafted = _drafted(app_config, packs, prompts_dir)
    deps.config.standards["drafting"]["criteria_batch_size"] = 5
    stories = drafted.stories
    n_batches = -(-len(stories) // 5)
    for start in range(0, len(stories), 5):
        fake._queued["criteria"].append(criteria_output(stories[start : start + 5]))
    result = run_criteria(deps, state, stories)
    assert len(result.requests) == n_batches
    assert all(s.acceptance_criteria for s in result.stories)
    only = {stories[0].id}
    fake._queued["criteria"].append(criteria_output([stories[0]], ("happy", "error")))
    again = run_criteria(deps, state, result.stories, only)
    assert [c.kind for c in again.stories[0].acceptance_criteria] == [
        CriterionKind.HAPPY,
        CriterionKind.ERROR,
    ]
    assert again.stories[1:] == result.stories[1:]
    deps.config.standards["drafting"]["criteria_batch_size"] = 8


def test_criteria_cap_follows_preferences(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> None:
    deps, fake, state, _, drafted = _drafted(app_config, packs, prompts_dir)
    state.preferences = Preferences(max_criteria_per_story=2)
    assert max_criteria(deps, state) == 2
    fake._queued["criteria"].append(
        criteria_output(drafted.stories[:8], ("happy", "edge", "error", "compliance"))
    )
    result = run_criteria(deps, state, drafted.stories[:8])
    assert all(len(s.acceptance_criteria) == 2 for s in result.stories)
    assert "MAX CRITERIA PER STORY: 2" in fake.calls[-1].user


def test_criteria_findings(app_config: AppConfig, packs: PackSet, prompts_dir: Path) -> None:
    deps, fake, state, _, drafted = _drafted(app_config, packs, prompts_dir)
    stories = drafted.stories[:4]
    out = {
        "stories": [
            {
                "story_id": stories[0].id,
                "criteria": [
                    {
                        "given": "a user",
                        "when": "they pay within 96 hours",
                        "then": "it works",
                        "kind": "happy",
                    },
                    {
                        "given": "a user",
                        "when": "they click",
                        "then": "the page loads quickly and easily",
                        "kind": "error",
                    },
                    {"given": "x", "when": "y", "then": "z", "kind": "edge"},
                ],
            },
            {
                "story_id": stories[1].id,
                "criteria": [
                    {"given": "a user", "when": "they act", "then": "it works", "kind": "happy"}
                ],
            },
            {"story_id": "NOPE-0001", "criteria": []},
        ]
    }
    fake._queued["criteria"].append(out)
    result = run_criteria(deps, state, stories)
    by = {(f.code, f.location) for f in result.findings}
    assert ("CRITERION_UNGROUNDED_NUMBER", stories[0].id) in by
    assert ("CRITERION_VAGUE", stories[0].id) in by
    assert ("CRITERION_MALFORMED", stories[0].id) in by
    assert ("CRITERIA_NO_NEGATIVE_PATH", stories[1].id) in by
    assert ("CRITERIA_MISSING", stories[2].id) in by
    assert ("CRITERIA_UNKNOWN_STORY", "NOPE-0001") in by
    errors = {f.code for f in result.findings if f.severity is Severity.ERROR}
    assert errors == {"CRITERION_UNGROUNDED_NUMBER", "CRITERIA_MISSING"}
    kept = result.stories[0].acceptance_criteria
    assert all("96" not in c.when for c in kept)


def test_compliance_and_nfr_expectations(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> None:
    deps, fake, state, _, drafted = _drafted(app_config, packs, prompts_dir, per_story=8)
    story = next(s for s in drafted.stories if s.nfrs)
    assert story.nfrs
    fake._queued["criteria"].append(criteria_output([story], ("happy", "edge")))
    codes = {f.code for f in run_criteria(deps, state, [story]).findings}
    assert {"CRITERIA_NO_COMPLIANCE", "CRITERIA_NO_NFR"} <= codes


def test_criteria_schema_is_strict() -> None:
    assert CriteriaOutput.model_json_schema()["additionalProperties"] is False


# ---- critique -------------------------------------------------------------


def _with_criteria(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> tuple[StageDeps, FakeTransport, RunState, list[Story]]:
    deps, fake, state, _, drafted = _drafted(app_config, packs, prompts_dir)
    fake._queued["criteria"].append(criteria_output(drafted.stories[:8]))
    fake._queued["criteria"].append(criteria_output(drafted.stories[8:16]))
    stories = run_criteria(deps, state, drafted.stories).stories
    state.stories = stories
    return deps, fake, state, stories


def test_clean_set_has_no_blocking_findings(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> None:
    deps, fake, state, stories = _with_criteria(app_config, packs, prompts_dir)
    fake._queued["critique"].append(EMPTY_CRITIQUE)
    result = run_critique(deps, state, stories)
    assert result.blocking == []
    assert set(result.invest) == {s.id for s in stories}
    assert all(0 <= v <= 1 for v in result.invest.values())
    sent = fake.calls[-1].user
    assert "independent:" in sent
    assert '<untrusted_data kind="stories">' in sent
    assert " AC [happy] Given" in sent


def test_deterministic_checks(app_config: AppConfig, packs: PackSet, prompts_dir: Path) -> None:
    deps, _, state, stories = _with_criteria(app_config, packs, prompts_dir)
    dropped = stories[1:]
    findings = deterministic_findings(deps, state, dropped, [])
    assert {f.code for f in findings} >= {"COVERAGE_GAP"}
    big = stories[0].model_copy(
        update={"estimate": 13, "dependencies": [stories[1].id, stories[2].id, stories[3].id]}
    )
    no_ac = stories[1].model_copy(update={"acceptance_criteria": [], "estimate": None})
    loop_a = stories[2].model_copy(update={"dependencies": [stories[3].id]})
    loop_b = stories[3].model_copy(update={"dependencies": [stories[2].id], "benefit": loop_a.want})
    loop_b = loop_b.model_copy(update={"want": loop_a.want})
    findings = deterministic_findings(deps, state, [big, no_ac, loop_a, loop_b, *stories[4:]], [])
    codes = {f.code for f in findings}
    assert {
        "OVERSIZED",
        "NOT_READY",
        "INVEST_NOT_ESTIMATED",
        "INVEST_DEPENDENT",
        "DEPENDENCY_CYCLE",
        "INVEST_BENEFIT_RESTATES_WANT",
    } <= codes
    errors = {f.code for f in findings if f.severity is Severity.ERROR}
    assert {"OVERSIZED", "NOT_READY", "DEPENDENCY_CYCLE"} <= errors
    assert "INVEST_DEPENDENT" not in errors


def test_grounding_failures_block(app_config: AppConfig, packs: PackSet, prompts_dir: Path) -> None:
    deps, _, state, stories = _with_criteria(app_config, packs, prompts_dir)
    broken = stories[0].model_copy(update={"provenance": stories[0].provenance[:1]})
    codes = {f.code for f in deterministic_findings(deps, state, [broken, *stories[1:]], [])}
    assert "GROUND_ELEMENT_UNCOVERED" in codes


def test_model_findings_are_mapped_and_unknown_ids_ignored(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> None:
    deps, fake, state, stories = _with_criteria(app_config, packs, prompts_dir)
    a, b = stories[0].id, stories[1].id
    fake._queued["critique"].append(
        {
            "story_notes": [
                {"story_id": a, "negotiable": False, "issues": ["Names a database."]},
                {"story_id": b, "negotiable": True, "issues": ["Minor note."]},
                {"story_id": "ZZ-0001", "negotiable": False, "issues": ["ghost"]},
            ],
            "duplicates": [
                {"story_ids": [a, b], "reason": "Same outcome."},
                {"story_ids": [a, "ZZ-0001"], "reason": "ghost pair"},
            ],
            "contradictions": [{"story_ids": [b, a], "reason": "Cannot both hold."}],
            "split_suggestions": [
                {"story_id": a, "reason": "Two outcomes."},
                {"story_id": "ZZ-0001", "reason": "ghost"},
            ],
        }
    )
    result = run_critique(deps, state, stories)
    got = {
        (f.code, f.location, f.severity)
        for f in result.findings
        if f.code
        in {
            "INVEST_NOT_NEGOTIABLE",
            "REVIEW_NOTE",
            "DUPLICATE_STORY",
            "CONTRADICTION",
            "SPLIT_SUGGESTED",
        }
    }
    assert got == {
        ("INVEST_NOT_NEGOTIABLE", a, Severity.WARNING),
        ("REVIEW_NOTE", b, Severity.WARNING),
        ("DUPLICATE_STORY", f"{a},{b}", Severity.ERROR),
        ("CONTRADICTION", f"{a},{b}", Severity.ERROR),
        ("SPLIT_SUGGESTED", a, Severity.WARNING),
    }
    assert result.invest[a] < 1.0


def test_invest_scores() -> None:
    story = Story(
        id="AB-0001",
        epic="E",
        title="t",
        persona="p",
        want="w",
        benefit="b",
        provenance=[Provenance(type=ProvenanceType.SCENARIO_EXCERPT, ref="excerpt text")],
    )
    findings = [
        Finding(code="OVERSIZED", message="m", location="AB-0001"),
        Finding(code="CRITERION_VAGUE", message="m", location="AB-0001"),
        Finding(code="INVEST_NOT_ESTIMATED", message="m", location="AB-0001"),
        Finding(code="OVERSIZED", message="m", location="AB-0001,AB-0002"),
        Finding(code="OTHER", message="m", location="AB-0001"),
        Finding(code="OVERSIZED", message="m"),
    ]
    assert invest_scores([story], findings) == {"AB-0001": round(1 - 3 / 6, 3)}


def test_critique_schema_is_strict() -> None:
    assert CritiqueOutput.model_json_schema()["additionalProperties"] is False
