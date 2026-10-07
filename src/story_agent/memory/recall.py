"""Deterministic recall of saved entries.

Order of operations: restrict to the workspace (the store), the domain and sub-domain,
then select by tag and by full-text keyword match, then sort with a fixed key. Nothing
here calls a model. Recalled entries are still data, never instructions.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from story_agent.config import MemoryConfig
from story_agent.discovery.packs import GENERIC, PackSet
from story_agent.memory.store import MemoryStore, entry_terms, search_terms
from story_agent.schema import MemoryEntry, MemoryType, RunState, utcnow

_STOP = (
    "the and for that with this from are was were has have had not but can will should would "
    "when then than into over under after before about they them their there what which who "
    "how why all any each per via its our out use used using also may must"
)
STOPWORDS = frozenset(_STOP.split())
TYPE_RANK = {
    MemoryType.CONFIRMED_ANSWER: 0,
    MemoryType.NFR_DEFAULT: 1,
    MemoryType.DECISION: 2,
    MemoryType.PREFERENCE: 3,
    MemoryType.GLOSSARY: 4,
}
ALWAYS_TAGS = frozenset(
    {
        "pref:max_criteria_per_story",
        "pref:output_format",
        "pref:estimate_scale",
        "decision:story_template",
        "decision:priority_scheme",
    }
)


@dataclass(frozen=True)
class Recalled:
    """One recalled entry with why it matched."""

    entry: MemoryEntry
    stale: bool
    matched_tags: tuple[str, ...]
    matched_terms: int


@dataclass(frozen=True)
class RecallResult:
    """Recalled entries in their fixed order."""

    items: tuple[Recalled, ...]

    @property
    def entries(self) -> list[MemoryEntry]:
        """The recalled entries."""
        return [i.entry for i in self.items]

    @property
    def ids(self) -> list[str]:
        """Entry ids in recall order."""
        return [i.entry.id for i in self.items]


def is_stale(entry: MemoryEntry, now: datetime | None = None) -> bool:
    """Return True when the entry is past its TTL and needs re-confirmation."""
    current = now or utcnow()
    return current > entry.last_confirmed_at + timedelta(days=entry.ttl_days)


def recall(  # noqa: PLR0913, PLR0917  (query fields kept explicit and keyword-friendly)
    store: MemoryStore,
    config: MemoryConfig,
    domain: str,
    subdomain: str | None,
    tags: Iterable[str],
    text: str,
    now: datetime | None = None,
) -> RecallResult:
    """Return the entries relevant to a domain, tags and scenario text."""
    domains = {domain, GENERIC}
    query_tags = set(tags) | ALWAYS_TAGS
    terms = search_terms(text, STOPWORDS, config.recall.max_query_terms)
    keyword_hits = store.keyword_ids(terms)
    query_terms = set(terms)
    kept: list[tuple[tuple[int, int, int, float, str], Recalled]] = []
    for entry in store.candidates(domains):
        if entry.subdomain is not None and entry.subdomain != subdomain:
            continue
        matched_tags = tuple(sorted(set(entry.tags) & query_tags))
        matched = len(entry_terms(entry) & query_terms) if entry.id in keyword_hits else 0
        if not matched_tags and matched < config.recall.min_matched_terms:
            continue
        item = Recalled(entry, is_stale(entry, now), matched_tags, matched)
        key = (
            -len(matched_tags),
            TYPE_RANK[entry.type],
            -matched,
            -entry.last_confirmed_at.timestamp(),
            entry.id,
        )
        kept.append((key, item))
    kept.sort(key=lambda pair: pair[0])
    return RecallResult(tuple(item for _, item in kept[: config.recall.max_entries]))


def memory_recall(
    store: MemoryStore,
    packs: PackSet,
    config: MemoryConfig,
    state: RunState,
    now: datetime | None = None,
) -> RecallResult:
    """Stage 3. Recall entries for a run and record their ids in run state.

    Uses the same deterministic detection as discover, so the tags match the
    checklist that discover will use.
    """
    text = f"{state.redacted_text}\n{state.redacted_notes}"
    detection = packs.detect(text)
    checklist = packs.checklist(detection.domain, detection.subdomain, detection.subpacks)
    tags = [f"category:{cid}" for cid in checklist.ids]
    result = recall(store, config, detection.domain, detection.subdomain, tags, text, now)
    state.recalled_memory_ids = result.ids
    return result
