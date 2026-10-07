"""The pieces together: discover, clarify rounds, answers file, readiness, gate."""

from pathlib import Path

import pytest

from story_agent.clarify.answers import (
    CleanText,
    answer_questions,
    load_answers_file,
    resolve_answer_keys,
    submit_free_text,
)
from story_agent.clarify.readiness import GateError, assess, grant_go_ahead, require_go_ahead
from story_agent.clarify.rounds import next_round
from story_agent.config import AppConfig
from story_agent.discovery.packs import PackSet
from story_agent.schema import AnswerKind
from tests.helpers import discovered


def test_noninteractive_run_reaches_readiness_within_three_rounds(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path, tmp_path: Path
) -> None:
    deps, fake, state, checklist = discovered(app_config, packs, prompts_dir)
    path = tmp_path / "answers.yaml"
    path.write_text("free_text: Please use at most 4 criteria.\ngo_ahead: true\n")
    file = load_answers_file(path)
    asked: list[str] = []
    for _ in range(3):
        fake._queued["clarify"].append({"questions": []})
        outcome = next_round(deps, state, checklist)
        if outcome.result is None:
            break
        questions = outcome.result.round.questions
        asked.extend(q.category for q in questions)
        by_category = {q.category: "use your judgment" for q in questions}
        resolved, unmatched = resolve_answer_keys(state, by_category)
        assert unmatched == []
        assert answer_questions(state, {k: CleanText(v) for k, v in resolved.items()}) == []
        if assess(state, checklist).ready:
            break
    submit_free_text(state, CleanText(file.free_text))
    assert len(state.rounds) <= 3
    assert len(asked) == len(set(asked)), "a category was asked twice"
    assert {a.kind for a in state.answers} == {AnswerKind.JUDGMENT}
    assert state.preferences.max_criteria_per_story == 4
    assert assess(state, checklist).ready
    assert file.go_ahead
    grant_go_ahead(state, checklist, "answers_file")
    require_go_ahead(state)
    assert state.go_ahead_by == "answers_file"


def test_a_run_never_drafts_without_the_gate(
    app_config: AppConfig, packs: PackSet, prompts_dir: Path
) -> None:
    deps, fake, state, checklist = discovered(app_config, packs, prompts_dir)
    with pytest.raises(GateError):
        require_go_ahead(state)
    fake._queued["clarify"].append({"questions": []})
    next_round(deps, state, checklist)
    with pytest.raises(GateError):
        require_go_ahead(state)
    with pytest.raises(GateError):
        grant_go_ahead(state, checklist, "user")
