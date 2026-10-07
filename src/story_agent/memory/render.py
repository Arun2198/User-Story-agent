"""Render recalled memory for a prompt: labelled, listed by id, and treated as data."""

from __future__ import annotations

from collections.abc import Sequence

from story_agent.guardrails.injection import wrap_untrusted
from story_agent.schema import MemoryEntry


def render_memory_block(entries: Sequence[MemoryEntry]) -> str:
    """Return the memory block, entries sorted by id. Empty input gives ``none``."""
    if not entries:
        return wrap_untrusted("memory", "none")
    lines = [
        f"[{e.id}] type={e.type.value} last_confirmed={e.last_confirmed_at.date()}: {e.content}"
        for e in sorted(entries, key=lambda e: e.id)
    ]
    return wrap_untrusted("memory", "\n".join(lines))


def memory_ids(entries: Sequence[MemoryEntry]) -> tuple[str, ...]:
    """Return the entry ids in sorted order, for the cache key and the trace."""
    return tuple(sorted(e.id for e in entries))
