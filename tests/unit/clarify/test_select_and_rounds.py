from pathlib import Path

import pytest

from story_agent.clarify.limits import ClarifyLimits
from story_agent.clarify.rounds import next_round
from story_agent.clarify.select import open_categories, score, select_for_round
from story_agent.config import AppConfig
from story_agent.discovery.packs import PackSet
from story_agent.schema import ItemStatus
from tests.helpers import clarify_output, discovered


def test_limits_come_from_standards_and_cannot_exceed_hard_caps(app_config: AppConfig) -> None:
    limits = ClarifyLimits.from_standards(app_config.standards)
    assert (limits.max_questions_per_round, limits.max_rounds) == (6, 3)
    assert limits.free_text_prompt == "Anything else you want to add or change?"
    assert ClarifyLimits.from_standards({}).max_rounds == 3
    with pytest.raises(ValueError, match="less than or equal"):
        ClarifyLimits.model_validate({"max_questions_per_round": 7})


def test_ranking_is_by_impact_and_deterministic(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> None:
    _, _, state, checklist = discovered(app_config, packs, prompts_dir)
    assert state.discovery is not None
    ranked = open_categories(state.discovery, checklist)
    assert [o.score for o in ranked] == sorted((o.score for o in ranked), reverse=True)
    assert ranked == open_categories(state.discovery, checklist)
    assert all(o.category.id in checklist.must_have_ids for o in ranked[:6])
    optional = [o for o in ranked if o.category.id not in checklist.must_have_ids]
    assert ranked.index(optional[0]) >= 6
    stated_only = next(o for o in ranked if o.category.id == "dispute_handling")
    assert any(i.status is ItemStatus.UNKNOWN for i in stated_only.items)


def test_score_formula(packs: PackSet) -> None:
    category = packs.checklist("generic").get("primary_flow")
    assert category is not None
    assert score(category, [], must_have=False) == 50
    assert score(category, [], must_have=True) == 75


def test_selection_is_capped_at_six(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> None:
    _, _, state, checklist = discovered(app_config, packs, prompts_dir)
    assert state.discovery is not None
    limits = ClarifyLimits.from_standards(app_config.standards)
    assert len(select_for_round(state.discovery, checklist, limits)) == 6


def test_round_asks_one_question_per_selected_category(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> None:
    deps, fake, state, checklist = discovered(app_config, packs, prompts_dir)
    assert state.discovery is not None
    limits = ClarifyLimits.from_standards(app_config.standards)
    wanted = [o.category.id for o in select_for_round(state.discovery, checklist, limits)]
    fake._queued["clarify"].append(clarify_output(state, wanted))
    outcome = next_round(deps, state, checklist)
    assert outcome.result is not None
    assert outcome.free_text_prompt == "Anything else you want to add or change?"
    questions = outcome.result.round.questions
    assert [q.category for q in questions] == wanted
    assert [q.id for q in questions] == [f"Q1-{c}" for c in wanted]
    assert state.rounds == [outcome.result.round]
    assert all(2 <= len(q.options) <= 4 for q in questions)
    assert all("other" not in {o.casefold() for o in q.options} for q in questions)
    assert questions[0].options.count("Option A") == 1


def test_missing_question_uses_the_pack_probe_and_reports_it(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> None:
    deps, fake, state, checklist = discovered(app_config, packs, prompts_dir)
    fake._queued["clarify"].append({"questions": []})
    outcome = next_round(deps, state, checklist)
    assert outcome.result is not None
    assert len(outcome.result.round.questions) == 6
    assert {f.code for f in outcome.result.findings} == {"CLARIFY_FALLBACK"}
    assert all(len(q.options) >= 2 for q in outcome.result.round.questions)


def test_prompt_marks_derived_text_as_untrusted(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> None:
    deps, fake, state, checklist = discovered(app_config, packs, prompts_dir)
    fake._queued["clarify"].append({"questions": []})
    next_round(deps, state, checklist)
    sent = fake.calls[-1]
    for kind in ("discovery_summary", "open_items", "answers", "memory"):
        assert f'<untrusted_data kind="{kind}">' in sent.user
    assert "ROUND: 1 of 3" in sent.user


def test_no_round_when_nothing_is_open(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> None:
    deps, _, state, checklist = discovered(app_config, packs, prompts_dir)
    assert state.discovery is not None
    items = [i.model_copy(update={"status": ItemStatus.CONFIRMED}) for i in state.discovery.items]
    state.discovery = state.discovery.model_copy(update={"items": items})
    outcome = next_round(deps, state, checklist)
    assert outcome.result is None
    assert outcome.reason == "no open items"


def test_round_limit(app_config: AppConfig, packs: PackSet, prompts_dir: Path) -> None:
    deps, fake, state, checklist = discovered(app_config, packs, prompts_dir)
    for _ in range(3):
        fake._queued["clarify"].append({"questions": []})
        assert next_round(deps, state, checklist).result is not None
    assert [r.number for r in state.rounds] == [1, 2, 3]
    outcome = next_round(deps, state, checklist)
    assert outcome.result is None
    assert outcome.reason == "round limit reached"


def test_clarify_needs_discovery(app_config: AppConfig, packs: PackSet, prompts_dir: Path) -> None:
    deps, _, state, checklist = discovered(app_config, packs, prompts_dir)
    state.discovery = None
    with pytest.raises(ValueError, match="discover must run"):
        next_round(deps, state, checklist)
