"""Domain packs: YAML data that defines what to discover for an industry.

A pack is data only. Adding an industry means adding a YAML file under
``config/domains/``. A pack may extend another (usually ``generic``), and may carry
optional sub-packs that switch on extra categories, such as regional payment rails.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from story_agent.config import ConfigError, load_yaml
from story_agent.schema import SCHEMA_VERSION

GENERIC = "generic"
DEFAULT_MIN_SCORE = 2


class _PackModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Hint(_PackModel):
    """A keyword that suggests a domain, sub-domain or sub-pack."""

    term: str = Field(min_length=1)
    weight: int = Field(default=1, ge=1, le=5)


def _hints(value: object) -> object:
    if not isinstance(value, list):
        return value
    return [{"term": item} if isinstance(item, str) else item for item in value]


class Actor(_PackModel):
    """An entry in the actor catalogue."""

    id: str
    name: str
    description: str = ""


class Category(_PackModel):
    """One discovery checklist category."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    must_have: bool = False
    weight: int = Field(default=3, ge=1, le=5)
    applies_to: list[str] = Field(default_factory=list)
    must_have_in: list[str] = Field(default_factory=list)
    probes: list[str] = Field(min_length=1)
    typical_options: list[str] = Field(default_factory=list, max_length=4)

    def applies(self, subdomain: str | None) -> bool:
        """Return True when this category belongs on the checklist for ``subdomain``."""
        return not self.applies_to or (subdomain is not None and subdomain in self.applies_to)

    def is_must_have(self, subdomain: str | None) -> bool:
        """Return True when this category must be settled before drafting."""
        return self.must_have or (subdomain is not None and subdomain in self.must_have_in)


class SubDomain(_PackModel):
    """A sub-domain with its own detection hints."""

    id: str
    name: str
    hints: list[Hint] = Field(default_factory=list)

    _coerce = field_validator("hints", mode="before")(_hints)


class SubPack(_PackModel):
    """Optional extra categories that switch on when its hints match."""

    id: str
    name: str
    min_score: int = Field(default=DEFAULT_MIN_SCORE, ge=1)
    hints: list[Hint] = Field(default_factory=list)
    categories: list[Category] = Field(default_factory=list)

    _coerce = field_validator("hints", mode="before")(_hints)


class Pack(_PackModel):
    """A domain pack as written in YAML."""

    schema_version: str = SCHEMA_VERSION
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    extends: str | None = None
    min_score: int = Field(default=DEFAULT_MIN_SCORE, ge=1)
    hints: list[Hint] = Field(default_factory=list)
    subdomains: list[SubDomain] = Field(default_factory=list)
    sub_packs: list[SubPack] = Field(default_factory=list)
    actors: list[Actor] = Field(default_factory=list)
    categories: list[Category] = Field(default_factory=list)

    _coerce = field_validator("hints", mode="before")(_hints)


@dataclass(frozen=True)
class Detection:
    """Result of the deterministic domain detector."""

    domain: str
    subdomain: str | None
    subpacks: list[str]
    scores: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Checklist:
    """The ordered categories to discover for one domain, sub-domain and sub-packs."""

    domain: str
    subdomain: str | None
    subpacks: tuple[str, ...]
    categories: tuple[Category, ...]
    actors: tuple[Actor, ...]
    must_have_ids: frozenset[str]

    def get(self, category_id: str) -> Category | None:
        """Return a category by id."""
        return next((c for c in self.categories if c.id == category_id), None)

    @property
    def ids(self) -> list[str]:
        """Category ids in checklist order."""
        return [c.id for c in self.categories]


def _matches(text: str, term: str) -> bool:
    pattern = r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?:s|es)?(?![A-Za-z0-9])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _score(text: str, hints: Iterable[Hint]) -> int:
    return sum(h.weight for h in hints if _matches(text, h.term))


class PackSet:
    """All loaded packs, with checklist resolution and domain detection."""

    def __init__(self, packs: dict[str, Pack]) -> None:
        """Validate cross-references and keep the packs."""
        if GENERIC not in packs:
            raise ConfigError("the generic pack is required")
        self._packs = packs
        for pack in packs.values():
            self._validate(pack)

    @property
    def ids(self) -> list[str]:
        """Pack ids, sorted."""
        return sorted(self._packs)

    def get(self, pack_id: str) -> Pack:
        """Return a pack, or raise ConfigError."""
        if pack_id not in self._packs:
            raise ConfigError(f"unknown domain pack: {pack_id}")
        return self._packs[pack_id]

    def lineage(self, pack_id: str) -> list[Pack]:
        """Return the pack and its ancestors, root first."""
        chain: list[Pack] = []
        seen: set[str] = set()
        current: str | None = pack_id
        while current is not None:
            if current in seen:
                raise ConfigError(f"pack extends cycle at {current}")
            seen.add(current)
            pack = self.get(current)
            chain.append(pack)
            current = pack.extends
        return list(reversed(chain))

    def subdomain_ids(self, pack_id: str) -> list[str]:
        """Sub-domain ids declared by the pack and its ancestors."""
        return [s.id for p in self.lineage(pack_id) for s in p.subdomains]

    def subpack_ids(self, pack_id: str) -> list[str]:
        """Sub-pack ids declared by the pack and its ancestors."""
        return [s.id for p in self.lineage(pack_id) for s in p.sub_packs]

    def checklist(
        self, domain: str, subdomain: str | None = None, subpacks: Iterable[str] = ()
    ) -> Checklist:
        """Resolve the checklist. Child categories override parents by id."""
        chain = self.lineage(domain)
        known_subs = self.subdomain_ids(domain)
        sub = subdomain if subdomain in known_subs else None
        active = [s for s in dict.fromkeys(subpacks) if s in self.subpack_ids(domain)]
        merged: dict[str, Category] = {}
        for pack in chain:
            for category in pack.categories:
                merged[category.id] = category
        for pack in chain:
            for sub_pack in pack.sub_packs:
                if sub_pack.id in active:
                    for category in sub_pack.categories:
                        merged[category.id] = category
        categories = tuple(c for c in merged.values() if c.applies(sub))
        actors = {a.id: a for pack in chain for a in pack.actors}
        return Checklist(
            domain=domain,
            subdomain=sub,
            subpacks=tuple(active),
            categories=categories,
            actors=tuple(actors.values()),
            must_have_ids=frozenset(c.id for c in categories if c.is_must_have(sub)),
        )

    def detect(self, text: str) -> Detection:
        """Pick a domain, sub-domain and sub-packs from keyword hints. Pure and repeatable."""
        scores = {
            pack.id: _score(text, pack.hints)
            for pack in self._packs.values()
            if pack.id != GENERIC and pack.hints
        }
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        if not ranked or ranked[0][1] < self._packs[ranked[0][0]].min_score:
            return Detection(GENERIC, None, [], scores)
        domain = ranked[0][0]
        sub_scores = {
            s.id: _score(text, s.hints) for p in self.lineage(domain) for s in p.subdomains
        }
        best = max(sub_scores.items(), key=lambda kv: kv[1], default=(None, 0))
        subdomain = best[0] if best[1] > 0 else None
        if subdomain is not None:  # first declared wins a tie
            top = best[1]
            subdomain = next(sid for sid, sc in sub_scores.items() if sc == top)
        subpacks = [
            sp.id
            for p in self.lineage(domain)
            for sp in p.sub_packs
            if _score(text, sp.hints) >= sp.min_score
        ]
        return Detection(domain, subdomain, subpacks, scores)

    def _validate(self, pack: Pack) -> None:
        if pack.schema_version != SCHEMA_VERSION:
            raise ConfigError(f"pack {pack.id}: schema_version {pack.schema_version} unsupported")
        self.lineage(pack.id)
        ids = [c.id for c in pack.categories]
        for sp in pack.sub_packs:
            ids.extend(c.id for c in sp.categories)
        if len(ids) != len(set(ids)):
            raise ConfigError(f"pack {pack.id}: duplicate category ids")
        known_subs = set(self.subdomain_ids(pack.id))
        if len(known_subs) != len([s for p in self.lineage(pack.id) for s in p.subdomains]):
            raise ConfigError(f"pack {pack.id}: duplicate sub-domain ids")
        every = list(pack.categories) + [c for sp in pack.sub_packs for c in sp.categories]
        for category in every:
            unknown = (set(category.applies_to) | set(category.must_have_in)) - known_subs
            if unknown:
                raise ConfigError(
                    f"pack {pack.id}: category {category.id} names unknown "
                    f"sub-domains {sorted(unknown)}"
                )


def load_packs(config_dir: Path) -> PackSet:
    """Load every ``*.yaml`` file in ``config_dir/domains``."""
    directory = config_dir / "domains"
    packs: dict[str, Pack] = {}
    for path in sorted(directory.glob("*.yaml")):
        try:
            pack = Pack.model_validate(load_yaml(path))
        except ValidationError as exc:
            raise ConfigError(f"invalid pack {path.name}: {exc}") from exc
        if pack.id != path.stem:
            raise ConfigError(f"pack id {pack.id} must match file name {path.name}")
        if pack.id in packs:
            raise ConfigError(f"duplicate pack id {pack.id}")
        packs[pack.id] = pack
    return PackSet(packs)
