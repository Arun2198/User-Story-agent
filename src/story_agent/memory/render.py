"""Render recalled memory for a prompt: labelled, listed by id, and treated as data."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from story_agent.guardrails.injection import wrap_untrusted
from story_agent.schema import MemoryEntry, utcnow


def render_memory_block(entries: Sequence[MemoryEntry], now: datetime | None = None) -> str:
    """Return the memory block, entries sorted by id. Stale entries are labelled."""
    if not entries:
        return wrap_untrusted("memory", "none")
    current = now or utcnow()
    lines = [
        f"[{e.id}] type={e.type.value} last_confirmed={e.last_confirmed_at.date()}"
        f"{' STALE' if current > e.last_confirmed_at + timedelta(days=e.ttl_days) else ''}: "
        f"{e.content}"
        for e in sorted(entries, key=lambda e: e.id)
    ]
    return wrap_untrusted("memory", "\n".join(lines))


def memory_ids(entries: Sequence[MemoryEntry]) -> tuple[str, ...]:
    """Return the entry ids in sorted order, for the cache key and the trace."""
    return tuple(sorted(e.id for e in entries))
