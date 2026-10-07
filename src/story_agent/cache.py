"""SQLite response cache keyed on prompt, input, memory ids and model."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Protocol


class ResponseCache(Protocol):
    """Stores validated model responses as JSON text."""

    def get(self, key: str) -> str | None:
        """Return the cached JSON for ``key`` or None."""
        ...

    def put(self, key: str, value: str) -> None:
        """Store ``value`` under ``key``."""
        ...


class MemoryCache:
    """In-process cache, used in tests."""

    def __init__(self) -> None:
        """Create an empty cache."""
        self._data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        """Return the cached JSON for ``key`` or None."""
        return self._data.get(key)

    def put(self, key: str, value: str) -> None:
        """Store ``value`` under ``key``."""
        self._data[key] = value


class SqliteCache:
    """Persistent cache in a single SQLite file."""

    def __init__(self, path: Path) -> None:
        """Open or create the cache database at ``path``."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self._conn.commit()

    def get(self, key: str) -> str | None:
        """Return the cached JSON for ``key`` or None."""
        row = self._conn.execute("SELECT value FROM responses WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def put(self, key: str, value: str) -> None:
        """Store ``value`` under ``key``."""
        self._conn.execute(
            "INSERT OR REPLACE INTO responses (key, value) VALUES (?, ?)", (key, value)
        )
        self._conn.commit()

    def close(self) -> None:
        """Close the database."""
        self._conn.close()
