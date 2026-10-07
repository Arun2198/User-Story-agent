"""Command line interface. Only the ``memory`` commands exist so far."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from story_agent.config import ConfigError, load_config
from story_agent.memory.recall import is_stale
from story_agent.memory.store import MemoryStoreError, SqliteMemoryStore, UnsafeContentError
from story_agent.schema import MemoryEntry, MemoryType, utcnow

app = typer.Typer(no_args_is_help=True, help="Turn scenarios into user stories.")
memory_app = typer.Typer(no_args_is_help=True, help="Inspect and manage saved memory.")
app.add_typer(memory_app, name="memory")

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
