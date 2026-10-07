"""Pick which checklist categories to ask about, ranked by impact. Pure code, no model."""

from __future__ import annotations

from dataclasses import dataclass

from story_agent.clarify.limits import ClarifyLimits
from story_agent.discovery.packs import Category, Checklist
from story_agent.schema import DiscoveryItem, DiscoveryMap, ItemStatus

_OPEN = {ItemStatus.UNKNOWN, ItemStatus.INFERRED}
MUST_HAVE_BONUS = 25
UNKNOWN_POINTS = 8
INFERRED_POINTS = 4
MAX_COUNTED = 3


@dataclass(frozen=True)
class OpenCategory:
    """A category that still has unanswered items, with its rank score."""

    category: Category
    items: tuple[DiscoveryItem, ...]
    score: int


def is_open(item: DiscoveryItem) -> bool:
    """Return True when an item still needs the user's answer."""
    return item.status in _OPEN and item.resolved_by is None


def score(category: Category, items: list[DiscoveryItem], must_have: bool) -> int:
    """Impact score: category weight, must-have bonus, and how many gaps are open."""
    unknown = min(sum(i.status is ItemStatus.UNKNOWN for i in items), MAX_COUNTED)
    inferred = min(sum(i.status is ItemStatus.INFERRED for i in items), MAX_COUNTED)
    return (
        category.weight * 10
        + (MUST_HAVE_BONUS if must_have else 0)
        + unknown * UNKNOWN_POINTS
        + inferred * INFERRED_POINTS
    )


def open_categories(discovery: DiscoveryMap, checklist: Checklist) -> list[OpenCategory]:
    """Return categories with open items, highest score first, ties in checklist order."""
    result: list[OpenCategory] = []
    for category in checklist.categories:
        items = [i for i in discovery.items if i.category == category.id and is_open(i)]
        if not items:
            continue
        must = category.id in checklist.must_have_ids
        result.append(OpenCategory(category, tuple(items), score(category, items, must)))
    order = {c.id: i for i, c in enumerate(checklist.categories)}
    return sorted(result, key=lambda o: (-o.score, order[o.category.id]))


def select_for_round(
    discovery: DiscoveryMap, checklist: Checklist, limits: ClarifyLimits
) -> list[OpenCategory]:
    """Return the top categories to ask about in the next round."""
    return open_categories(discovery, checklist)[: limits.max_questions_per_round]
