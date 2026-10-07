"""Memory evals: retrieval, staleness, contradiction handling, safety, isolation, effect.

All of these are deterministic and need no model. Time is fixed so results repeat.
Question-count reduction here is measured at the checklist level; the end-to-end
version (memory off then on, with the user simulator) arrives with the app evals.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from story_agent.clarify.answers import CleanText, parse_answer
from story_agent.clarify.limits import ClarifyLimits
from story_agent.clarify.questions import memory_default
from story_agent.clarify.select import select_for_round
from story_agent.config import AppConfig, load_config
from story_agent.discovery.packs import load_packs
from story_agent.evals.components.common import Case, finalize, load_cases, ratio
from story_agent.guardrails.injection import InjectionDetector
from story_agent.guardrails.redaction import Redactor
from story_agent.ids import item_id
from story_agent.memory.conflicts import detect_conflicts
from story_agent.memory.guard import check_content
from story_agent.memory.recall import recall
from story_agent.memory.store import (
    MemoryStoreError,
    SqliteMemoryStore,
    UnsafeContentError,
    WorkspaceMismatchError,
)
from story_agent.schema import (
    Answer,
    DiscoveryItem,
    DiscoveryMap,
    EvalReport,
    ItemStatus,
    MemoryEntry,
    MemoryRef,
    MemoryType,
    Question,
    QuestionRound,
    RunState,
    Scenario,
)

FIXED_NOW = datetime(2026, 10, 7, tzinfo=UTC)
SCENARIO = (
    "A customer disputes a card transaction and expects a provisional credit while the "
    "bank investigates the claim."
)


def _entry(raw: dict[str, Any], workspace: str = "evalws") -> MemoryEntry:
    return MemoryEntry(
        id=raw["key"],
        workspace=workspace,
        domain=raw["domain"],
        subdomain=raw.get("subdomain"),
        tags=raw["tags"],
        type=MemoryType(raw["type"]),
        content=raw["content"],
        source_run_id="eval",
        created_at=FIXED_NOW - timedelta(days=raw["age_days"]),
        last_confirmed_at=FIXED_NOW - timedelta(days=raw["age_days"]),
        ttl_days=raw["ttl_days"],
    )


def _store(app: AppConfig, root: Path, workspace: str = "evalws") -> SqliteMemoryStore:
    return SqliteMemoryStore(root, workspace, app.memory)


def retrieval(app: AppConfig, evals_config: dict[str, Any]) -> EvalReport:
    """Precision and recall of recall() against labelled cases."""
    tp = fp = fn = 0
    missed: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        for case in load_cases("memory_retrieval"):
            store = _store(app, Path(tmp) / case["id"])
            for raw in case["entries"]:
                store.put(_entry(raw))
            q = case["query"]
            got = set(
                recall(
                    store, app.memory, q["domain"], q["subdomain"], q["tags"], q["text"], FIXED_NOW
                ).ids
            )
            want = set(case["expected"])
            tp += len(got & want)
            fp += len(got - want)
            fn += len(want - got)
            if got != want:
                missed.append(case["id"])
            store.close()
    metrics = {"precision": ratio(tp, tp + fp), "recall": ratio(tp, tp + fn)}
    return finalize("memory_retrieval", metrics, evals_config, {"missed_case_ids": missed})


def _question(default_value: str, options: list[str], stale: bool) -> Question:
    ref = MemoryRef(
        memory_id="M-eval",
        value=default_value,
        last_confirmed_at=FIXED_NOW,
        stale=stale,
    )
    return Question(
        id="Q1-channels",
        category="channels",
        question="q",
        why_it_matters="w",
        options=options,
        remembered_default=ref,
        item_ids=[],
    )


def staleness(_app: AppConfig, evals_config: dict[str, Any]) -> EvalReport:
    """Stale entries are flagged, and never applied without an explicit user answer."""
    flag_ok = kind_ok = misapplied = total = replied = 0
    bad: list[str] = []
    for case in load_cases("memory_staleness"):
        entry = _entry(
            {
                "key": "M-eval",
                "type": "confirmed_answer",
                "domain": "banking",
                "subdomain": None,
                "tags": ["category:channels"],
                "content": "Remembered value",
                "age_days": case["age_days"],
                "ttl_days": case["ttl_days"],
            }
        )
        ref = memory_default("channels", [entry], FIXED_NOW)
        total += 1
        stale_flag = ref is not None and ref.stale == case["expect_stale"]
        flag_ok += stale_flag
        reply = case["reply"]
        kind: str | None = None
        if reply is not None:
            replied += 1
            question = _question(
                "Remembered value", ["Option A", "Option B"], bool(ref and ref.stale)
            )
            answer = parse_answer(question, CleanText(reply))
            kind = None if answer is None else answer.kind.value
            if kind == "memory_confirmed" and reply.strip().casefold() != "yes":
                misapplied += 1
        kind_ok += kind == case["expect_kind"]
        if not stale_flag or kind != case["expect_kind"]:
            bad.append(case["id"])
    metrics = {
        "stale_flag_accuracy": ratio(flag_ok, total),
        "answer_kind_accuracy": ratio(kind_ok, total),
        "stale_misapplication_rate": ratio(misapplied, replied, empty=0.0),
    }
    return finalize("memory_staleness", metrics, evals_config, {"missed_case_ids": bad})


def contradiction(_app: AppConfig, evals_config: dict[str, Any]) -> EvalReport:
    """Check that a conflict is raised only when the answer differs from the default."""
    correct = missed = false = changed = same = 0
    bad: list[str] = []
    for case in load_cases("memory_contradiction"):
        question = _question(case["default"], case["options"], stale=False)
        answer = parse_answer(question, CleanText(case["reply"]))
        answers: list[Answer] = [] if answer is None else [answer]
        state = RunState(
            run_id="eval",
            scenario=Scenario(text="x"),
            rounds=[QuestionRound(number=1, questions=[question])],
            answers=answers,
        )
        raised = bool(detect_conflicts(state))
        expected = bool(case["expect_conflict"])
        correct += raised == expected
        if expected:
            changed += 1
            missed += not raised
        else:
            same += 1
            false += raised
        if raised != expected:
            bad.append(case["id"])
    metrics = {
        "accuracy": ratio(correct, changed + same),
        "missed_conflict_rate": ratio(missed, changed, empty=0.0),
        "false_conflict_rate": ratio(false, same, empty=0.0),
    }
    return finalize("memory_contradiction", metrics, evals_config, {"missed_case_ids": bad})


def safety(app: AppConfig, evals_config: dict[str, Any]) -> EvalReport:
    """Unsafe content is refused; nothing sensitive is persisted."""
    unsafe = rejected = safe = false_reject = code_match = 0
    bad: list[str] = []
    write = app.memory.write
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(app, Path(tmp))
        for n, case in enumerate(load_cases("memory_safety")):
            findings = check_content(
                case["content"], write.max_content_chars, SCENARIO, write.max_scenario_overlap_chars
            )
            entry = _entry(
                {
                    "key": f"S{n}",
                    "type": "confirmed_answer",
                    "domain": "banking",
                    "subdomain": None,
                    "tags": ["category:channels"],
                    "content": case["content"],
                    "age_days": 0,
                    "ttl_days": 180,
                }
            )
            try:
                if findings:
                    raise UnsafeContentError(findings)
                store.put(entry)
                refused = False
            except UnsafeContentError:
                refused = True
            if case["expect"] == "reject":
                unsafe += 1
                rejected += refused
                code_match += refused and case["code"] in {f.code for f in findings}
            else:
                safe += 1
                false_reject += refused
            if refused != (case["expect"] == "reject"):
                bad.append(case["id"])
        stored = store.raw_text()
        pii = Redactor().count(stored) + len(InjectionDetector().scan(stored).hits)
        store.close()
    metrics = {
        "unsafe_rejection_rate": ratio(rejected, unsafe),
        "false_rejection_rate": ratio(false_reject, safe, empty=0.0),
        "code_match_rate": ratio(code_match, unsafe),
        "pii_persisted": float(pii),
    }
    return finalize("memory_safety", metrics, evals_config, {"missed_case_ids": bad})


def isolation(app: AppConfig, evals_config: dict[str, Any]) -> EvalReport:
    """Entries never cross workspaces. Any leak is a hard failure."""
    leaks = blocked = mismatch_cases = traversal_blocked = 0
    traversal = ["../x", "a/b", "..", "x/../y", "/abs", ""]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        stores = {w: _store(app, root, w) for w in ("alpha", "beta")}
        for ws, store in stores.items():
            for n in range(3):
                store.put(
                    _entry(
                        {
                            "key": f"{ws}-{n}",
                            "type": "confirmed_answer",
                            "domain": "banking",
                            "subdomain": None,
                            "tags": ["category:channels", f"term:{ws}marker"],
                            "content": f"{ws}secret value {n} for channels",
                            "age_days": 1,
                            "ttl_days": 180,
                        },
                        ws,
                    )
                )
        for ws, store in stores.items():
            got = recall(
                store,
                app.memory,
                "banking",
                None,
                ["category:channels"],
                "channels value",
                FIXED_NOW,
            )
            leaks += sum(e.workspace != ws or not e.id.startswith(ws) for e in got.entries)
            leaks += len(store.keyword_ids(["alphasecret" if ws == "beta" else "betasecret"]))
            leaks += sum(e.workspace != ws for e in store.list_entries())
        for store in stores.values():
            store.close()
        for ws, other in (("alpha", "beta"), ("beta", "alpha")):
            data = (root / f"{ws}.db").read_bytes()
            leaks += int(f"{other}secret".encode() in data)
        alpha = _store(app, root, "alpha")
        foreign = _entry(
            {
                "key": "x",
                "type": "confirmed_answer",
                "domain": "banking",
                "subdomain": None,
                "tags": ["category:channels"],
                "content": "from beta",
                "age_days": 0,
                "ttl_days": 180,
            },
            "beta",
        )
        mismatch_cases += 1
        try:
            alpha.put(foreign)
        except WorkspaceMismatchError:
            blocked += 1
        alpha.close()
        for name in traversal:
            try:
                _store(app, root, name).close()
            except MemoryStoreError:
                traversal_blocked += 1
    metrics = {
        "cross_workspace_leaks": float(leaks),
        "mismatch_block_rate": ratio(blocked, mismatch_cases),
        "traversal_block_rate": ratio(traversal_blocked, len(traversal)),
    }
    return finalize("memory_isolation", metrics, evals_config)


def effect(app: AppConfig, evals_config: dict[str, Any], config_dir: Path) -> EvalReport:
    """How many first-round questions carry a remembered default, and are stale ones flagged."""
    packs = load_packs(config_dir)
    limits = ClarifyLimits.from_standards(app.standards)
    asked_total = fresh = stored_asked = with_default = stale_asked = stale_flagged = spurious = 0
    for case in load_cases("memory_effect"):
        checklist = packs.checklist("banking", case["subdomain"], case["subpacks"])
        items = [
            DiscoveryItem(
                id=item_id(c.id, 1),
                category=c.id,
                description="gap",
                status=ItemStatus.UNKNOWN,
                must_have=c.id in checklist.must_have_ids,
            )
            for c in checklist.categories
        ]
        discovery = DiscoveryMap(domain="banking", subdomain=case["subdomain"], items=items)
        entries = [
            _entry(
                {
                    "key": f"M-{cid}",
                    "type": "confirmed_answer",
                    "domain": "banking",
                    "subdomain": case["subdomain"],
                    "tags": [f"category:{cid}"],
                    "content": f"remembered {cid}",
                    "age_days": 400 if cid in case["stale"] else 10,
                    "ttl_days": 30 if cid in case["stale"] else 180,
                }
            )
            for cid in case["stored"]
        ]
        for chosen in select_for_round(discovery, checklist, limits):
            cid = chosen.category.id
            ref = memory_default(cid, entries, FIXED_NOW)
            asked_total += 1
            if cid in case["stored"]:
                stored_asked += 1
                with_default += ref is not None
                if cid in case["stale"]:
                    stale_asked += 1
                    stale_flagged += bool(ref and ref.stale)
                fresh += bool(ref and not ref.stale)
            else:
                spurious += ref is not None
    metrics = {
        "question_reduction": ratio(fresh, asked_total, empty=0.0),
        "default_coverage": ratio(with_default, stored_asked),
        "stale_default_flag_rate": ratio(stale_flagged, stale_asked),
        "spurious_defaults": float(spurious),
    }
    return finalize("memory_effect", metrics, evals_config)


def run_all(evals_config: dict[str, Any], config_dir: Path) -> list[EvalReport]:
    """Run every memory eval."""
    app = load_config(config_dir)
    return [
        retrieval(app, evals_config),
        staleness(app, evals_config),
        contradiction(app, evals_config),
        safety(app, evals_config),
        isolation(app, evals_config),
        effect(app, evals_config, config_dir),
    ]


def get_cases(name: str) -> list[Case]:
    """Expose a dataset to tests."""
    return load_cases(name)
