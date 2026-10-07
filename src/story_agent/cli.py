"""Command line interface: run, resume, memory and evals."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from story_agent import runcmd
from story_agent.config import ConfigError, default_config_dir, load_config
from story_agent.discovery.packs import load_packs
from story_agent.evals.cases import datasets_dir, load_case, save_case, validate_case
from story_agent.evals.online.config import parse_online
from story_agent.evals.online.drift import check_drift
from story_agent.evals.online.feedback import extract_feedback
from story_agent.evals.online.trace import assert_trace_safe, build_trace, read_events, to_otlp
from story_agent.evals.report import to_json, to_markdown
from story_agent.evals.runner import (
    COMPONENTS,
    PACKAGE_BASELINES,
    SuiteOptions,
    load_baseline,
    run_suite,
)
from story_agent.memory.recall import is_stale
from story_agent.memory.store import MemoryStoreError, SqliteMemoryStore, UnsafeContentError
from story_agent.publish import PublishBlocked, apply_plan, approve, service
from story_agent.publish.base import NotEnabledError
from story_agent.publish.service import (
    EXTERNAL_TARGETS,
    FILE_TARGETS,
    FORMAT_TARGETS,
    TARGETS,
    UnknownTargetError,
    build_plan,
    external_publisher,
    load_run,
    render_file,
)
from story_agent.runcmd import (
    EXIT_ERROR,
    EXIT_NO_TERMINAL,
    EXIT_OK,
    EXIT_PAUSED,
    Chosen,
    NoTerminalError,
    build_runtime,
    choose_responder,
    default_memory_dir,
    default_runs_dir,
    summarize,
    write_private,
)
from story_agent.schema import MemoryEntry, MemoryType, Scenario, utcnow
from story_agent.session import RunOutcome, Session, SessionError

app = typer.Typer(no_args_is_help=True, help="Turn scenarios into user stories.")
memory_app = typer.Typer(no_args_is_help=True, help="Inspect and manage saved memory.")
evals_app = typer.Typer(no_args_is_help=True, help="Run and extend the evals.")
online_app = typer.Typer(no_args_is_help=True, help="Traces, feedback and drift for finished runs.")
app.add_typer(memory_app, name="memory")
app.add_typer(evals_app, name="evals")
app.add_typer(online_app, name="online")

Workspace = Annotated[str, typer.Option("--workspace", "-w", help="Workspace name.")]
MemoryDir = Annotated[
    Path | None,
    typer.Option("--memory-dir", help="Directory for memory databases (STORY_AGENT_MEMORY_DIR)."),
]


def _open(workspace: str, memory_dir: Path | None) -> SqliteMemoryStore:
    root = memory_dir or Path(os.environ.get("STORY_AGENT_MEMORY_DIR", "memory"))
    try:
        return SqliteMemoryStore(root, workspace, load_config().memory)
    except (MemoryStoreError, ConfigError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


def _state(entry: MemoryEntry, now: datetime) -> str:
    return "STALE" if is_stale(entry, now) else "fresh"


@memory_app.command("list")
def list_cmd(
    workspace: Workspace = "default",
    memory_dir: MemoryDir = None,
    entry_type: Annotated[MemoryType | None, typer.Option("--type", help="Filter by type.")] = None,
    domain: Annotated[str | None, typer.Option(help="Filter by domain.")] = None,
) -> None:
    """List saved entries in a workspace."""
    store = _open(workspace, memory_dir)
    now = utcnow()
    entries = store.list_entries(entry_type, domain)
    for e in entries:
        text = e.content if len(e.content) <= 60 else e.content[:57] + "..."
        typer.echo(
            f"{e.id}  {e.type.value:<16} {e.domain:<10} {_state(e, now):<5} "
            f"used={e.use_count:<3} {e.last_confirmed_at.date()}  {text}"
        )
    typer.echo(f"{len(entries)} entries in workspace '{workspace}'")
    store.close()


@memory_app.command("show")
def show_cmd(entry_id: str, workspace: Workspace = "default", memory_dir: MemoryDir = None) -> None:
    """Show one entry in full."""
    store = _open(workspace, memory_dir)
    entry = store.get(entry_id)
    store.close()
    if entry is None:
        typer.echo(f"error: no entry {entry_id}", err=True)
        raise typer.Exit(1)
    typer.echo(json.dumps(entry.model_dump(mode="json"), indent=2, sort_keys=True))


@memory_app.command("edit")
def edit_cmd(  # noqa: PLR0913, PLR0917  (CLI options)
    entry_id: str,
    content: Annotated[str | None, typer.Option(help="New content.")] = None,
    ttl_days: Annotated[int | None, typer.Option(min=1, help="New TTL in days.")] = None,
    tags: Annotated[str | None, typer.Option(help="Comma-separated tags.")] = None,
    workspace: Workspace = "default",
    memory_dir: MemoryDir = None,
) -> None:
    """Edit an entry. The new content goes through the same safety checks as a write."""
    store = _open(workspace, memory_dir)
    entry = store.get(entry_id)
    if entry is None:
        store.close()
        typer.echo(f"error: no entry {entry_id}", err=True)
        raise typer.Exit(1)
    update: dict[str, object] = {"last_confirmed_at": utcnow()}
    if content is not None:
        update["content"] = content.strip()
    if ttl_days is not None:
        update["ttl_days"] = ttl_days
    if tags is not None:
        update["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    try:
        store.put(entry.model_copy(update=update))
    except UnsafeContentError as exc:
        typer.echo(f"error: refused ({exc})", err=True)
        raise typer.Exit(1) from exc
    finally:
        store.close()
    typer.echo(f"updated {entry_id}")


@memory_app.command("delete")
def delete_cmd(
    entry_id: str, workspace: Workspace = "default", memory_dir: MemoryDir = None
) -> None:
    """Delete one entry."""
    store = _open(workspace, memory_dir)
    removed = store.delete(entry_id)
    store.close()
    if not removed:
        typer.echo(f"error: no entry {entry_id}", err=True)
        raise typer.Exit(1)
    typer.echo(f"deleted {entry_id}")


@memory_app.command("export")
def export_cmd(
    workspace: Workspace = "default",
    memory_dir: MemoryDir = None,
    out: Annotated[Path | None, typer.Option(help="Write to this file instead of stdout.")] = None,
) -> None:
    """Export every entry of one workspace as JSON."""
    store = _open(workspace, memory_dir)
    entries = [e.model_dump(mode="json") for e in store.list_entries()]
    store.close()
    text = json.dumps({"workspace": workspace, "entries": entries}, indent=2, sort_keys=True)
    if out is None:
        typer.echo(text)
    else:
        out.write_text(text, encoding="utf-8")
        typer.echo(f"wrote {len(entries)} entries to {out}")


@memory_app.command("clear")
def clear_cmd(
    workspace: Workspace,
    memory_dir: MemoryDir = None,
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask for confirmation.")] = False,
) -> None:
    """Delete every entry in a workspace."""
    store = _open(workspace, memory_dir)
    if not yes and not typer.confirm(f"Delete all memory in workspace '{workspace}'?"):
        store.close()
        raise typer.Exit(1)
    count = store.clear()
    store.close()
    typer.echo(f"cleared {count} entries from workspace '{workspace}'")


# ---- run and resume --------------------------------------------------------------

Answers = Annotated[
    Path | None,
    typer.Option(
        "--answers",
        help="YAML or JSON file that answers every question, to run without a terminal.",
    ),
]
RunsDir = Annotated[
    Path | None, typer.Option("--runs-dir", help="Where runs are saved (STORY_AGENT_RUNS_DIR).")
]


def _finish(outcome: RunOutcome, chosen: Chosen, runs_dir: Path) -> None:
    lines, code = summarize(outcome, chosen.stopped, runs_dir)
    for line in lines:
        typer.echo(line, err=code != EXIT_OK)
    raise typer.Exit(code)


@app.command("run")
def run_cmd(  # noqa: PLR0913, PLR0917  (CLI options)
    scenario: Annotated[str, typer.Argument(help="The scenario, in plain language.")],
    notes: Annotated[str, typer.Option(help="Extra notes about the scenario.")] = "",
    workspace: Workspace = "default",
    answers: Answers = None,
    runs_dir: RunsDir = None,
    memory_dir: MemoryDir = None,
    no_memory: Annotated[bool, typer.Option("--no-memory", help="Do not use memory.")] = False,
    out: Annotated[
        Path | None, typer.Option("--out", help="Also write the stories here when the run ends.")
    ] = None,
    fmt: Annotated[
        str | None, typer.Option("--format", help="md, csv (Azure DevOps import) or json.")
    ] = None,
) -> None:
    """Turn a scenario into reviewed user stories."""
    root = default_runs_dir(runs_dir)
    try:
        chosen = choose_responder(answers)
        scenario_model = Scenario(text=scenario, notes=notes, workspace=workspace)
        session = Session(
            build_runtime(default_config_dir(), root, default_memory_dir(memory_dir), not no_memory)
        )
    except NoTerminalError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_NO_TERMINAL) from exc
    except (ConfigError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_ERROR) from exc
    try:
        outcome = session.start(scenario_model, chosen.responder)
    finally:
        session.rt.close()
    if outcome.status == "done" and (out is not None or fmt is not None):
        _export_after_run(outcome, fmt, out, root)
    _finish(outcome, chosen, root)


def _export_after_run(outcome: RunOutcome, fmt: str | None, out: Path | None, root: Path) -> None:
    """Write the finished run in the asked format. A problem here does not undo the run."""
    state = outcome.state
    wanted = fmt or (state.preferences.output_format if state else None) or "md"
    target = FORMAT_TARGETS.get(wanted)
    if target is None:
        typer.echo(f"error: unknown format {wanted}; choose md, csv or json", err=True)
        raise typer.Exit(EXIT_ERROR)
    _publish_file(outcome.run_id, target, out, root)


@app.command("resume")
def resume_cmd(
    run_id: Annotated[str, typer.Argument(help="The id printed when the run paused.")],
    answers: Answers = None,
    runs_dir: RunsDir = None,
    memory_dir: MemoryDir = None,
    no_memory: Annotated[bool, typer.Option("--no-memory", help="Do not use memory.")] = False,
) -> None:
    """Continue a paused run from its checkpoint."""
    root = default_runs_dir(runs_dir)
    try:
        chosen = choose_responder(answers)
        session = Session(
            build_runtime(default_config_dir(), root, default_memory_dir(memory_dir), not no_memory)
        )
        try:
            outcome = session.resume(run_id, chosen.responder)
        finally:
            session.rt.close()
    except NoTerminalError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_NO_TERMINAL) from exc
    except (ConfigError, SessionError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_ERROR) from exc
    _finish(outcome, chosen, root)


# ---- publish ---------------------------------------------------------------------


def _publish_file(run_id: str, target: str, out: Path | None, root: Path) -> None:
    try:
        config = load_config(default_config_dir())
        state, redactions = load_run(root, run_id)
        rendered = render_file(state, config, target, redactions)
    except (ConfigError, FileNotFoundError, PublishBlocked, UnknownTargetError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_ERROR) from exc
    if str(out) == "-":
        typer.echo(rendered.text, nl=False)
        return
    path = out or root / run_id / f"stories.{rendered.extension}"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_private(path, rendered.text)
    typer.echo(f"wrote {rendered.stories} stories to {path}")


def _publish_external(run_id: str, target: str, dry_run: bool, root: Path) -> None:
    try:
        config = load_config(default_config_dir())
        state, redactions = load_run(root, run_id)
        plan, ctx = build_plan(state, config, target, redactions)
    except (ConfigError, FileNotFoundError, PublishBlocked, UnknownTargetError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_ERROR) from exc
    if dry_run:
        typer.echo(plan.to_json())
        return
    try:
        client = service.make_rest_client(target, ctx)
    except NotEnabledError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_ERROR) from exc
    if not runcmd.stdin_is_tty():
        typer.echo(
            "error: writing to an external system needs a person to approve it at a terminal. "
            "Use --dry-run to see the payload.",
            err=True,
        )
        raise typer.Exit(EXIT_NO_TERMINAL)
    typer.echo(plan.to_json())
    if not typer.confirm(f"Send these {len(plan.operations)} items to {target}?", default=False):
        typer.echo("Nothing was sent.")
        raise typer.Exit(EXIT_PAUSED)
    publisher = external_publisher(target, ctx)
    report = apply_plan(publisher, plan, client, approve(plan, os.environ.get("USER", "user")))
    typer.echo(f"created {len(report.created)}, updated {len(report.updated)}")


@app.command("publish")
def publish_cmd(
    run_id: Annotated[str, typer.Argument(help="A finished run.")],
    target: Annotated[str, typer.Option("--target", help=f"One of: {', '.join(TARGETS)}.")] = "md",
    out: Annotated[
        Path | None, typer.Option("--out", help="Output file, or - for the terminal.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the exact payload for ado_rest or jira_rest.")
    ] = False,
    runs_dir: RunsDir = None,
) -> None:
    """Publish a finished run as a file, or prepare a write to Azure DevOps or Jira."""
    root = default_runs_dir(runs_dir)
    if target in FILE_TARGETS:
        _publish_file(run_id, target, out, root)
    elif target in EXTERNAL_TARGETS:
        _publish_external(run_id, target, dry_run, root)
    else:
        typer.echo(f"error: unknown target {target}; choose from {', '.join(TARGETS)}", err=True)
        raise typer.Exit(EXIT_ERROR)


# ---- online -----------------------------------------------------------------------


@online_app.command("trace")
def online_trace(
    run_id: str,
    otlp: Annotated[bool, typer.Option("--otlp", help="Print OTLP/JSON instead.")] = False,
    runs_dir: RunsDir = None,
) -> None:
    """Print the OpenTelemetry-compatible trace of a finished run."""
    root = default_runs_dir(runs_dir)
    try:
        state, redactions = load_run(root, run_id)
        trace = build_trace(state, read_events(root / run_id / "trace.jsonl"))
        assert_trace_safe(trace, redactions)
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_ERROR) from exc
    data = to_otlp(trace) if otlp else trace.model_dump(mode="json")
    typer.echo(json.dumps(data, indent=2))


@online_app.command("feedback")
def online_feedback(run_id: str, runs_dir: RunsDir = None) -> None:
    """Print the feedback signals of a finished run."""
    root = default_runs_dir(runs_dir)
    try:
        state, _ = load_run(root, run_id)
    except FileNotFoundError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_ERROR) from exc
    calls = len(read_events(root / run_id / "trace.jsonl"))
    typer.echo(extract_feedback(state, calls).model_dump_json(indent=2))


@online_app.command("drift")
def online_drift(
    runs_dir: RunsDir = None,
    baseline_dir: Annotated[Path | None, typer.Option(help="Baseline directory.")] = None,
) -> None:
    """Compare recorded online runs with the offline baseline. Exit 1 on drift."""
    root = default_runs_dir(runs_dir)
    try:
        config = load_config(default_config_dir())
        online = parse_online(config.evals)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_ERROR) from exc
    path = root / online.path
    rows: list[dict[str, float]] = []
    if path.exists():
        rows = [
            json.loads(line)["metrics"] for line in path.read_text(encoding="utf-8").splitlines()
        ]
    drift = online.drift
    baseline = load_baseline(
        baseline_dir or PACKAGE_BASELINES, drift.baseline_mode, drift.baseline_name
    )
    if baseline is None:
        typer.echo(
            f"no {drift.baseline_mode} baseline for {drift.baseline_name}; "
            "run the evals with --live --update-baseline first"
        )
        raise typer.Exit(EXIT_OK)
    report = check_drift(rows, baseline, drift)
    if report.skipped:
        typer.echo(f"not checked: {report.skipped}")
        raise typer.Exit(EXIT_OK)
    typer.echo(f"{report.runs} runs, {len(report.checked)} metrics checked")
    for item in report.drifted:
        typer.echo(f"DRIFT {item}")
    raise typer.Exit(EXIT_ERROR if report.drifted else EXIT_OK)


# ---- evals ---------------------------------------------------------------------


@evals_app.command("run")
def evals_run(  # noqa: PLR0913, PLR0917  (CLI options)
    component: Annotated[
        str | None, typer.Option(help=f"One of: {', '.join(COMPONENTS)}, all.")
    ] = None,
    app_eval: Annotated[
        bool, typer.Option("--app", help="End-to-end evals with the simulated user.")
    ] = False,
    stability: Annotated[int | None, typer.Option(min=2, help="Repeat each case N times.")] = None,
    memory: Annotated[bool, typer.Option("--memory", help="Memory off then on, per case.")] = False,
    live: Annotated[
        bool, typer.Option(help="Use the real model (needs ANTHROPIC_API_KEY).")
    ] = False,
    cases: Annotated[str | None, typer.Option(help="Comma-separated case ids.")] = None,
    cases_dir: Annotated[Path | None, typer.Option(help="Directory of case files.")] = None,
    out: Annotated[Path, typer.Option(help="Where to write the reports.")] = Path("eval-reports"),
    baseline_dir: Annotated[Path | None, typer.Option(help="Baseline directory.")] = None,
    update_baseline: Annotated[
        bool, typer.Option(help="Save this run as the new baseline.")
    ] = False,
) -> None:
    """Run evals, write JSON and markdown reports, and exit non-zero on a regression."""
    options = SuiteOptions(
        component=component,
        app=app_eval,
        stability=stability,
        memory=memory,
        live=live,
        case_ids=[c.strip() for c in cases.split(",")] if cases else None,
        cases_dir=cases_dir,
        baseline_dir=baseline_dir,
        update_baseline=update_baseline,
        config_dir=default_config_dir(),
    )
    try:
        result = run_suite(options)
    except (ValueError, ConfigError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    out.mkdir(parents=True, exist_ok=True)
    (out / "eval-report.json").write_text(to_json(result), encoding="utf-8")
    (out / "eval-report.md").write_text(to_markdown(result), encoding="utf-8")
    typer.echo(to_markdown(result))
    raise typer.Exit(result.exit_code)


@evals_app.command("add-case")
def evals_add_case(
    file: Path,
    cases_dir: Annotated[Path | None, typer.Option(help="Where to store the case.")] = None,
    force: Annotated[bool, typer.Option(help="Replace a case with the same id.")] = False,
) -> None:
    """Validate a case file and add it to the dataset (use --force to attach gold stories later)."""
    config_dir = default_config_dir()
    try:
        case = load_case(file)
        packs = load_packs(config_dir)
    except (ValueError, OSError, ConfigError) as exc:
        typer.echo(f"error: cannot read the case: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(1) from exc
    findings = validate_case(case, packs)
    for f in findings:
        typer.echo(f"{f.severity.value}: {f.code}: {f.message}", err=f.severity.value == "error")
    if any(f.severity.value == "error" for f in findings):
        raise typer.Exit(1)
    target = (cases_dir or datasets_dir()) / f"{case.id}.json"
    if target.exists() and not force:
        typer.echo(f"error: {target.name} exists; use --force to replace it", err=True)
        raise typer.Exit(1)
    path = save_case(case, cases_dir)
    typer.echo(f"added {case.id} ({len(case.gold_stories)} gold stories) at {path}")
