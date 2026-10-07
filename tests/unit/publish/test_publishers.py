import copy
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest

from story_agent.config import AppConfig, ConfigError
from story_agent.publish import (
    Approval,
    ApprovalRequiredError,
    PublishBlocked,
    apply_plan,
    approve,
)
from story_agent.publish.adocsv import safe_cell
from story_agent.publish.base import NotEnabledError, PublishContext, Request
from story_agent.publish.config import parse_destinations
from story_agent.publish.html import wiki
from story_agent.publish.safety import assert_no_pii, check_publishable
from story_agent.publish.service import (
    FILE_TARGETS,
    build_plan,
    external_publisher,
    load_context,
    make_rest_client,
    prepare,
    render_file,
)
from story_agent.publish.view import build_view
from story_agent.schema import ProvenanceType, StoryStatus
from tests.unit.publish.fixtures import finished_state

ROOT = Path(__file__).resolve().parents[3]
SNAPSHOTS = Path(__file__).parent / "snapshots"


def check_snapshot(name: str, text: str) -> None:
    """Compare with a stored snapshot. Set UPDATE_SNAPSHOTS=1 to rewrite it on purpose."""
    path = SNAPSHOTS / name
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert path.exists(), f"missing snapshot {name}; run with UPDATE_SNAPSHOTS=1"
    assert text == path.read_text(encoding="utf-8"), f"{name} changed; review the diff"


def raw_destinations(config: AppConfig) -> dict[str, Any]:
    raw = copy.deepcopy(config.destinations)
    raw["ado"]["project"] = "Bank"
    raw["jira"]["project_key"] = "BANK"
    return raw


def context(config: AppConfig, **ado: Any) -> PublishContext:
    raw = raw_destinations(config)
    raw["ado"].update(ado)
    return PublishContext(parse_destinations(raw), config.config_dir / "templates")


# ---- what is published --------------------------------------------------------------


def test_the_fixture_is_publishable(app_config: AppConfig) -> None:
    check_publishable(finished_state(), app_config)


def test_the_view_keeps_approved_and_edited_stories_only(app_config: AppConfig) -> None:
    view, _ = prepare(finished_state(), app_config)
    assert [s.id for s in view.stories] == ["DSP-0001", "DSP-0002", "VIS-0001"]
    assert view.skipped == ("VIS-0002",)
    assert [e.name for e in view.epics] == ["Disputes", "Visibility"]
    assert [e.prefix for e in view.epics] == ["DSP", "VIS"]
    assert [f.name for f in view.epics[0].features] == ["Raise a dispute", None]
    assert [r.id for r in view.requirements] == ["REQ-001", "REQ-002", "REQ-003"]


def test_labels_cover_import_requirements_clarification_and_key(app_config: AppConfig) -> None:
    view, _ = prepare(finished_state(), app_config)
    first, second, _third = view.stories
    assert first.labels[0] == "scenario-import"
    assert {"REQ-001", "REQ-002"} <= set(first.labels)
    assert "needs-clarification" not in first.labels
    assert "needs-clarification" in second.labels
    assert first.labels[-1] == f"sa-{first.key}"


def test_provenance_is_written_in_words(app_config: AppConfig) -> None:
    view, _ = prepare(finished_state(), app_config)
    first, second, third = view.stories
    assert first.provenance[0].startswith('Scenario: "expects a temporary credit')
    assert any(
        "Question: How long does the customer" in p and "Answer: 60 days" in p
        for p in first.provenance
    )
    assert any(
        p.startswith("Confirmed from saved memory (M-abc1234567)") for p in second.provenance
    )
    assert any(p.startswith("Decision on audit") for p in second.provenance)
    assert any(p.startswith("Reviewer note") for p in third.provenance)


def test_idempotency_keys_are_stable_across_runs_and_change_with_the_scenario(
    app_config: AppConfig,
) -> None:
    state = finished_state()
    again = state.model_copy(update={"run_id": "run-other-0002"})
    other = state.model_copy(update={"redacted_text": state.redacted_text + " Also fees."})
    keys = [s.key for s in prepare(state, app_config)[0].stories]
    assert keys == [s.key for s in prepare(again, app_config)[0].stories]
    assert keys != [s.key for s in prepare(other, app_config)[0].stories]
    assert len(set(keys)) == 3
    moved = state.model_copy(
        update={"scenario": state.scenario.model_copy(update={"workspace": "beta"})}
    )
    assert keys != [s.key for s in prepare(moved, app_config)[0].stories]


# ---- the checks before publishing ----------------------------------------------------


def test_a_story_waiting_for_review_blocks_publishing(app_config: AppConfig) -> None:
    state = finished_state()
    state.stories[2] = state.stories[2].model_copy(update={"status": StoryStatus.DRAFT})
    with pytest.raises(PublishBlocked, match="wait for review: VIS-0001"):
        check_publishable(state, app_config)


def test_a_run_with_nothing_approved_blocks_publishing(app_config: AppConfig) -> None:
    state = finished_state()
    state.stories = [s.model_copy(update={"status": StoryStatus.REJECTED}) for s in state.stories]
    with pytest.raises(PublishBlocked, match="no approved stories"):
        check_publishable(state, app_config)


def test_an_ungrounded_story_blocks_every_target(app_config: AppConfig) -> None:
    state = finished_state()
    bad = (
        state.stories[0]
        .provenance[0]
        .model_copy(
            update={
                "type": ProvenanceType.SCENARIO_EXCERPT,
                "ref": "the customer wants a pony and a boat",
            }
        )
    )
    state.stories[0] = state.stories[0].model_copy(
        update={"provenance": [bad, *state.stories[0].provenance[1:]]}
    )
    for target in FILE_TARGETS:
        with pytest.raises(PublishBlocked, match="grounding"):
            render_file(state, app_config, target, {})
    with pytest.raises(PublishBlocked, match="grounding"):
        build_plan(state, app_config, "ado_rest", {})


def test_output_that_repeats_a_redacted_value_is_blocked(app_config: AppConfig) -> None:
    state = finished_state()
    with pytest.raises(PublishBlocked, match="redacted"):
        render_file(state, app_config, "md", {"[CARD_1]": "relationship manager"})
    with pytest.raises(PublishBlocked, match="sensitive"):
        assert_no_pii("contact jane.doe@example.com", {})
    assert_no_pii("nothing private here", {"[X]": "ab"})  # too short to count as a leak


def test_a_sensitive_value_in_a_story_is_blocked_in_every_format(app_config: AppConfig) -> None:
    state = finished_state()
    state.stories[0] = state.stories[0].model_copy(
        update={"want": "to email jane.doe@example.com about a dispute"}
    )
    for target in ("md", "json", "ado_csv"):
        with pytest.raises(PublishBlocked, match="sensitive"):
            render_file(state, app_config, target, {})
    with pytest.raises(PublishBlocked, match="sensitive"):
        build_plan(state, app_config, "jira_rest", {})


# ---- snapshots -------------------------------------------------------------------------


@pytest.mark.parametrize(("target", "name"), [("md", "stories.md"), ("json", "stories.json")])
def test_file_snapshots(app_config: AppConfig, target: str, name: str) -> None:
    rendered = render_file(finished_state(), app_config, target, {})
    assert rendered.stories == 3
    check_snapshot(name, rendered.text)


def test_json_is_valid_and_complete(app_config: AppConfig) -> None:
    data = json.loads(render_file(finished_state(), app_config, "json", {}).text)
    assert data["not_published"] == ["VIS-0002"]
    stories = [s for e in data["epics"] for f in e["features"] for s in f["stories"]]
    assert [s["id"] for s in stories] == ["DSP-0001", "DSP-0002", "VIS-0001"]
    assert stories[0]["acceptance_criteria"][1]["kind"] == "error"
    assert all(s["idempotency_key"] and s["provenance"] for s in stories)


def test_ado_csv_snapshots(app_config: AppConfig) -> None:
    state = finished_state()
    view, _ = prepare(state, app_config)
    default = FILE_TARGETS["ado_csv"].render(view, context(app_config))
    check_snapshot("ado_agile.csv", default)
    scrum = FILE_TARGETS["ado_csv"].render(
        view, context(app_config, process="scrum", acceptance_criteria="description")
    )
    check_snapshot("ado_scrum_description.csv", scrum)
    assert "Product Backlog Item" in scrum
    assert "Effort" in scrum.splitlines()[0]
    assert "Acceptance Criteria" not in scrum.splitlines()[0]


def test_ado_csv_puts_stories_without_a_feature_under_the_default_feature(
    app_config: AppConfig,
) -> None:
    rows = render_file(finished_state(), app_config, "ado_csv", {}).text.splitlines()
    assert any(r.startswith("Feature,,General,") for r in rows)
    assert sum(r.startswith("Feature,") for r in rows) == 3
    assert sum(r.startswith("Epic,") for r in rows) == 2


def test_spreadsheet_formulas_are_neutralised() -> None:
    assert safe_cell("=SUM(A1)") == "'=SUM(A1)"
    assert safe_cell("-5% fee") == "'-5% fee"
    assert safe_cell("@cmd") == "'@cmd"
    assert safe_cell("Plain") == "Plain"


def test_a_formula_in_a_title_is_neutralised_in_the_csv(app_config: AppConfig) -> None:
    state = finished_state()
    state.stories[0] = state.stories[0].model_copy(update={"title": "=HYPERLINK(evil)"})
    text = render_file(state, app_config, "ado_csv", {}).text
    assert "'=HYPERLINK(evil)" in text
    assert ",=HYPERLINK" not in text


def test_html_and_wiki_escape_story_text(app_config: AppConfig) -> None:
    state = finished_state()
    state.stories[0] = state.stories[0].model_copy(
        update={"want": "to <script>x</script> {code} [a|b]"}
    )
    view, _ = prepare(state, app_config)
    csv_text = FILE_TARGETS["ado_csv"].render(view, context(app_config))
    assert "<script>" not in csv_text
    assert "&lt;script&gt;" in csv_text
    plan = external_publisher("jira_rest", context(app_config)).plan(view, context(app_config))
    body = plan.to_json()
    assert "{code}" not in body.replace("\\\\{", "")
    assert wiki("{code} [x|y]") == "\\{code\\} \\[x\\|y\\]"


def test_ado_rest_plan_snapshot(app_config: AppConfig) -> None:
    view, _ = prepare(finished_state(), app_config)
    ctx = context(app_config)
    plan = external_publisher("ado_rest", ctx).plan(view, ctx)
    check_snapshot("ado_rest_plan.json", plan.to_json() + "\n")
    kinds = [(o.kind, o.parent_key is None) for o in plan.operations]
    assert kinds[0] == ("epic", True)
    assert all(not root for kind, root in kinds if kind != "epic")


def test_jira_rest_plan_snapshot(app_config: AppConfig) -> None:
    view, _ = prepare(finished_state(), app_config)
    ctx = context(app_config)
    plan = external_publisher("jira_rest", ctx).plan(view, ctx)
    check_snapshot("jira_rest_plan.json", plan.to_json() + "\n")
    assert [o.kind for o in plan.operations].count("story") == 3
    assert "feature-raise-a-dispute" in plan.to_json()


def test_jira_can_use_its_own_criteria_field_and_epic_link_field(app_config: AppConfig) -> None:
    raw = raw_destinations(app_config)
    raw["jira"]["acceptance_criteria_field"] = "customfield_10100"
    raw["jira"]["epic_link_field"] = "customfield_10014"
    ctx = PublishContext(parse_destinations(raw), app_config.config_dir / "templates")
    view, _ = prepare(finished_state(), app_config)
    publisher = external_publisher("jira_rest", ctx)
    plan = publisher.plan(view, ctx)
    story = next(o for o in plan.operations if o.kind == "story")
    fields = story.create.body["fields"]
    assert "customfield_10100" in fields
    assert "Acceptance criteria" not in fields["description"]
    linked = publisher.with_parent(story.create, "BANK-1")
    assert linked.body["fields"]["customfield_10014"] == "BANK-1"


# ---- external writes: approval and idempotency ---------------------------------------------


class Reply:
    def __init__(self, body: Any, status: int = 200) -> None:
        self.status = status
        self.body = body


class FakeSystem:
    """Stands in for ADO or Jira: finds items by tag, creates and updates them."""

    def __init__(self, jira: bool) -> None:
        self.jira = jira
        self.items: dict[str, str] = {}
        self.sent: list[Request] = []

    @staticmethod
    def tag_of(text: str) -> str:
        found = re.findall(r"sa-[0-9a-f]{12}", unquote(text))
        return str(found[-1])

    def send(self, request: Request) -> Reply:
        self.sent.append(request)
        blob = json.dumps(request.body) + request.path
        if "wiql" in request.path or request.path.startswith("/rest/api/2/search"):
            tag = self.tag_of(blob)
            found = self.items.get(tag)
            if self.jira:
                return Reply({"issues": [{"key": found}] if found else []})
            return Reply({"workItems": [{"id": int(found)}] if found else []})
        if request.method == "POST":
            tag = self.tag_of(blob)
            number = len(self.items) + 1
            ident = f"BANK-{number}" if self.jira else str(number)
            self.items[tag] = ident
            return Reply({"key": ident} if self.jira else {"id": number})
        return Reply({})


def plan_for(app_config: AppConfig, target: str) -> Any:
    ctx = context(app_config)
    publisher = external_publisher(target, ctx)
    view, _ = prepare(finished_state(), app_config)
    return publisher, publisher.plan(view, ctx)


@pytest.mark.parametrize("target", ["ado_rest", "jira_rest"])
def test_nothing_is_sent_without_an_approval_for_this_exact_plan(
    app_config: AppConfig, target: str
) -> None:
    publisher, plan = plan_for(app_config, target)
    system = FakeSystem(jira=target == "jira_rest")
    with pytest.raises(ApprovalRequiredError):
        apply_plan(publisher, plan, system, None)
    other = plan.__class__(plan.target, plan.operations[:-1])
    with pytest.raises(ApprovalRequiredError):
        apply_plan(publisher, plan, system, approve(other, "arun"))
    with pytest.raises(ApprovalRequiredError):
        apply_plan(publisher, plan, system, Approval("0" * 64, "arun"))
    assert system.sent == []


@pytest.mark.parametrize("target", ["ado_rest", "jira_rest"])
def test_a_re_run_updates_instead_of_duplicating(app_config: AppConfig, target: str) -> None:
    publisher, plan = plan_for(app_config, target)
    system = FakeSystem(jira=target == "jira_rest")
    approval = approve(plan, "arun")
    first = apply_plan(publisher, plan, system, approval)
    assert len(first.created) == len(plan.operations)
    assert first.updated == []
    count = len(system.items)
    second = apply_plan(publisher, plan, system, approval)
    assert second.created == []
    assert len(second.updated) == len(plan.operations)
    assert len(system.items) == count
    updates = [r for r in system.sent if r.method in {"PATCH", "PUT"}]
    assert len(updates) == len(plan.operations)
    assert all("{id}" not in r.path for r in updates)


def test_children_are_linked_to_the_parents_that_were_just_created(app_config: AppConfig) -> None:
    publisher, plan = plan_for(app_config, "ado_rest")
    system = FakeSystem(jira=False)
    apply_plan(publisher, plan, system, approve(plan, "arun"))
    creates = [r for r in system.sent if r.method == "POST" and "wiql" not in r.path]
    epic, feature = creates[0], creates[1]
    assert not any(p["path"] == "/relations/-" for p in epic.body)
    link = next(p for p in feature.body if p["path"] == "/relations/-")
    assert link["value"]["url"].endswith("/workitems/1")
    # updates keep the parent they were created with
    apply_plan(publisher, plan, system, approve(plan, "arun"))
    assert not any(
        p["path"] == "/relations/-" for r in system.sent if r.method == "PATCH" for p in r.body
    )


def test_jira_stories_are_linked_to_their_epic_on_create(app_config: AppConfig) -> None:
    publisher, plan = plan_for(app_config, "jira_rest")
    system = FakeSystem(jira=True)
    apply_plan(publisher, plan, system, approve(plan, "arun"))
    creates = [r for r in system.sent if r.method == "POST"]
    assert "parent" not in creates[0].body["fields"]
    assert creates[1].body["fields"]["parent"] == {"key": "BANK-1"}


def test_no_http_client_ships_yet(app_config: AppConfig) -> None:
    with pytest.raises(NotEnabledError, match="not enabled"):
        make_rest_client("ado_rest", context(app_config))


def test_unknown_targets_are_reported(app_config: AppConfig) -> None:
    ctx = load_context(app_config)
    with pytest.raises(ValueError, match="not an external target"):
        external_publisher("md", ctx)
    with pytest.raises(ValueError, match="not a file target"):
        render_file(finished_state(), app_config, "ado_rest", {})


# ---- destinations.yaml ---------------------------------------------------------------------


def test_the_shipped_destinations_file_is_valid(app_config: AppConfig) -> None:
    config = parse_destinations(app_config.destinations)
    assert config.ado.names.story == "User Story"
    assert set(config.ado.processes) == {"agile", "scrum", "cmmi"}
    assert config.labels.import_ == "scenario-import"


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda r: r["ado"].update(process="waterfall"), "process"),
        (lambda r: r["ado"].update(surprise=1), "surprise"),
        (lambda r: r["ado"]["priority"].pop("wont"), "priority"),
        (lambda r: r["jira"]["priority"].pop("must"), "priority"),
        (lambda r: r["labels"].pop("import"), "labels"),
        (lambda r: r["labels"].update(key_prefix="x' OR '1'='1"), "key_prefix"),
    ],
)
def test_a_bad_destinations_file_is_a_clean_error(
    app_config: AppConfig, edit: Any, message: str
) -> None:
    raw = copy.deepcopy(app_config.destinations)
    edit(raw)
    with pytest.raises(ConfigError, match=message):
        parse_destinations(raw)


def test_build_view_is_pure(app_config: AppConfig) -> None:
    state = finished_state()
    before = state.model_dump_json()
    build_view(state, app_config.standards, load_context(app_config).destinations.labels)
    assert state.model_dump_json() == before
