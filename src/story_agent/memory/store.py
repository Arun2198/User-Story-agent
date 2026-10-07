"""Workspace-scoped memory store on SQLite with FTS5.

One database file per workspace (``<memory_dir>/<workspace>.db``), so one client's
entries are never in the same file as another's. Entries are always confirmed by
the user; nothing here decides what to save.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from story_agent.config import MemoryConfig
from story_agent.memory.guard import check_content
from story_agent.schema import SCHEMA_VERSION, Finding, MemoryEntry, MemoryType

DB_VERSION = 1
_WORKSPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TERM_RE = re.compile(r"[a-z0-9]{3,}")


class MemoryStoreError(RuntimeError):
    """Base class for memory store errors."""


class WorkspaceMismatchError(MemoryStoreError):
    """An entry belongs to a different workspace than the store."""


class UnsafeContentError(MemoryStoreError):
    """Content failed the write guard."""

    def __init__(self, findings: Sequence[Finding]) -> None:
        """Keep the findings that caused the refusal."""
        super().__init__(", ".join(f.code for f in findings))
        self.findings = list(findings)


class MemoryStore(Protocol):
    """What the rest of the app needs from a memory store."""

    workspace: str

    def get(self, entry_id: str) -> MemoryEntry | None:
        """Return one entry or None."""
        ...

    def put(self, entry: MemoryEntry) -> None:
        """Insert or replace an entry after the write guard passes."""
        ...

    def delete(self, entry_id: str) -> bool:
        """Delete an entry. Return True when it existed."""
        ...

    def list_entries(
        self, entry_type: MemoryType | None = None, domain: str | None = None
    ) -> list[MemoryEntry]:
        """List entries in a fixed order."""
        ...

    def candidates(self, domains: Iterable[str]) -> list[MemoryEntry]:
        """Return entries whose domain is in ``domains``."""
        ...

    def keyword_ids(self, terms: Sequence[str]) -> set[str]:
        """Return ids of entries matching any of ``terms`` through full-text search."""
        ...

    def touch(self, entry_id: str, now: datetime | None = None) -> bool:
        """Record a re-confirmation: bump use_count and last_confirmed_at."""
        ...

    def clear(self) -> int:
        """Delete every entry in the workspace. Return how many were removed."""
        ...


def search_terms(text: str, stopwords: frozenset[str], limit: int) -> list[str]:
    """Return up to ``limit`` distinct lowercase keywords. Only [a-z0-9] terms survive."""
    seen: dict[str, None] = {}
    for term in _TERM_RE.findall(text.casefold()):
        if term not in stopwords:
            seen.setdefault(term, None)
    return sorted(seen)[:limit]


def entry_terms(entry: MemoryEntry) -> set[str]:
    """Return the keywords of an entry, as used for full-text matching."""
    return set(_TERM_RE.findall(_body(entry).casefold()))


def _body(entry: MemoryEntry) -> str:
    return f"{entry.content} {' '.join(entry.tags)}"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _row_to_entry(row: sqlite3.Row) -> MemoryEntry:
    return MemoryEntry(
        schema_version=row["schema_version"],
        id=row["id"],
        workspace=row["workspace"],
        domain=row["domain"],
        subdomain=row["subdomain"],
        tags=json.loads(row["tags"]),
        type=MemoryType(row["type"]),
        content=row["content"],
        source_run_id=row["source_run_id"],
        confirmed=bool(row["confirmed"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        last_confirmed_at=datetime.fromisoformat(row["last_confirmed_at"]),
        use_count=row["use_count"],
        ttl_days=row["ttl_days"],
    )


class SqliteMemoryStore:
    """SQLite implementation of MemoryStore."""

    def __init__(self, memory_dir: Path, workspace: str, config: MemoryConfig) -> None:
        """Open or create ``<memory_dir>/<workspace>.db``."""
        if not _WORKSPACE_RE.fullmatch(workspace):
            raise MemoryStoreError("invalid workspace name")
        root = memory_dir.resolve()
        root.mkdir(parents=True, exist_ok=True)
        path = (root / f"{workspace}.db").resolve()
        if path.parent != root:
            raise MemoryStoreError("workspace path escapes the memory directory")
        self.workspace = workspace
        self.path = path
        self._config = config
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        try:
            self._init_schema()
        except Exception:
            self._conn.close()
            raise

    def _init_schema(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, DB_VERSION):
            raise MemoryStoreError(f"unsupported memory database version {version}")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS entries (
                id TEXT PRIMARY KEY,
                workspace TEXT NOT NULL,
                domain TEXT NOT NULL,
                subdomain TEXT,
                tags TEXT NOT NULL,
                type TEXT NOT NULL,
                content TEXT NOT NULL,
                source_run_id TEXT NOT NULL,
                confirmed INTEGER NOT NULL CHECK (confirmed = 1),
                created_at TEXT NOT NULL,
                last_confirmed_at TEXT NOT NULL,
                use_count INTEGER NOT NULL,
                ttl_days INTEGER NOT NULL,
                schema_version TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(id UNINDEXED, body);
            """
        )
        self._conn.execute(f"PRAGMA user_version = {DB_VERSION}")
        self._conn.commit()

    def close(self) -> None:
        """Close the database."""
        self._conn.close()

    def __enter__(self) -> SqliteMemoryStore:
        """Use the store in a ``with`` block."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Close on leaving the block."""
        self.close()

    def get(self, entry_id: str) -> MemoryEntry | None:
        """Return one entry or None."""
        row = self._conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
        return None if row is None else _row_to_entry(row)

    def put(self, entry: MemoryEntry) -> None:
        """Insert or replace an entry after checking workspace, version and content."""
        if entry.workspace != self.workspace:
            raise WorkspaceMismatchError("entry workspace differs from the store workspace")
        if entry.schema_version != SCHEMA_VERSION:
            raise MemoryStoreError("entry schema version is not supported")
        findings = check_content(entry.content, self._config.write.max_content_chars)
        for tag in entry.tags:
            findings.extend(check_content(tag, self._config.write.max_content_chars))
        if findings:
            raise UnsafeContentError(findings)
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entry.id,
                    entry.workspace,
                    entry.domain,
                    entry.subdomain,
                    json.dumps(sorted(entry.tags)),
                    entry.type.value,
                    entry.content,
                    entry.source_run_id,
                    1,
                    _iso(entry.created_at),
                    _iso(entry.last_confirmed_at),
                    entry.use_count,
                    entry.ttl_days,
                    entry.schema_version,
                ),
            )
            self._conn.execute("DELETE FROM entries_fts WHERE id = ?", (entry.id,))
            self._conn.execute(
                "INSERT INTO entries_fts (id, body) VALUES (?, ?)", (entry.id, _body(entry))
            )

    def delete(self, entry_id: str) -> bool:
        """Delete an entry. Return True when it existed."""
        with self._conn:
            cur = self._conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
            self._conn.execute("DELETE FROM entries_fts WHERE id = ?", (entry_id,))
        return cur.rowcount > 0

    def list_entries(
        self, entry_type: MemoryType | None = None, domain: str | None = None
    ) -> list[MemoryEntry]:
        """List entries ordered by type, domain, then id."""
        sql = "SELECT * FROM entries WHERE 1=1"
        args: list[str] = []
        if entry_type is not None:
            sql += " AND type = ?"
            args.append(entry_type.value)
        if domain is not None:
            sql += " AND domain = ?"
            args.append(domain)
        rows = self._conn.execute(sql + " ORDER BY type, domain, id", args).fetchall()
        return [_row_to_entry(r) for r in rows]

    def candidates(self, domains: Iterable[str]) -> list[MemoryEntry]:
        """Return entries in the given domains, ordered by id."""
        wanted = sorted(set(domains))
        if not wanted:
            return []
        marks = ",".join("?" for _ in wanted)
        rows = self._conn.execute(
            f"SELECT * FROM entries WHERE domain IN ({marks}) ORDER BY id",  # noqa: S608  # nosec B608 - placeholders only
            wanted,
        ).fetchall()
        return [_row_to_entry(r) for r in rows]

    def keyword_ids(self, terms: Sequence[str]) -> set[str]:
        """Return ids of entries matching any term. Terms are re-validated and quoted."""
        safe = [t for t in terms if _TERM_RE.fullmatch(t)]
        if not safe:
            return set()
        query = " OR ".join(f'"{t}"' for t in safe)
        rows = self._conn.execute(
            "SELECT id FROM entries_fts WHERE entries_fts MATCH ?", (query,)
        ).fetchall()
        return {r["id"] for r in rows}

    def touch(self, entry_id: str, now: datetime | None = None) -> bool:
        """Record a re-confirmation. Return True when the entry exists."""
        when = now or datetime.now(UTC)
        with self._conn:
            cur = self._conn.execute(
                "UPDATE entries SET use_count = use_count + 1, last_confirmed_at = ? WHERE id = ?",
                (_iso(when), entry_id),
            )
        return cur.rowcount > 0

    def clear(self) -> int:
        """Delete every entry. Return how many were removed."""
        with self._conn:
            cur = self._conn.execute("DELETE FROM entries")
            self._conn.execute("DELETE FROM entries_fts")
        return cur.rowcount

    def raw_text(self) -> str:
        """Return all stored text, for PII audits in tests and evals."""
        rows = self._conn.execute("SELECT content, tags FROM entries").fetchall()
        return "\n".join(f"{r['content']} {r['tags']}" for r in rows)
