"""Deterministic ids. The model never invents ids; these functions do."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from story_agent.schema import DiscoveryItem, ItemStatus

_STATUS_ORDER = {
    ItemStatus.STATED: 0,
    ItemStatus.CONFIRMED: 1,
    ItemStatus.INFERRED: 2,
    ItemStatus.UNKNOWN: 3,
    ItemStatus.REJECTED: 4,
}


def question_id(round_number: int, category_id: str) -> str:
    """Return the id of the question about ``category_id`` in a round."""
    return f"Q{round_number}-{category_id}"


def item_id(category_id: str, number: int) -> str:
    """Return the id of the ``number``-th item (from 1) in a category."""
    return f"D-{category_id}-{number:02d}"


def canonical_items(
    items: Sequence[DiscoveryItem], category_order: Sequence[str]
) -> list[DiscoveryItem]:
    """Sort items by checklist order, status, then text, and assign ids."""
    position = {cid: i for i, cid in enumerate(category_order)}
    ordered = sorted(
        items,
        key=lambda it: (
            position.get(it.category, len(position)),
            _STATUS_ORDER[it.status],
            it.description.casefold(),
        ),
    )
    counts: dict[str, int] = {}
    result: list[DiscoveryItem] = []
    for item in ordered:
        counts[item.category] = counts.get(item.category, 0) + 1
        result.append(item.model_copy(update={"id": item_id(item.category, counts[item.category])}))
    return result


def memory_id(entry_type: str, domain: str, subdomain: str | None, key_tag: str) -> str:
    """Return a stable id for a memory entry from what it is about, not from its text.

    The same kind of fact in the same domain always gets the same id, so a changed
    answer updates the entry instead of adding a second one.
    """
    raw = "|".join([entry_type, domain, subdomain or "", key_tag])
    return "M-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
