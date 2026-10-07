"""LLM boundary: transport, validation with one repair retry, caching and cost.

Model names and sampling settings come from config. This module knows nothing about
any provider: a ``Transport`` (see ``nvidia.py``) sends the request and returns JSON.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Generic, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from story_agent.cache import ResponseCache
from story_agent.config import ModelsConfig
from story_agent.hashing import cache_key, hash_text

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """The model call failed and the run must stop."""


class LLMValidationError(LLMError):
    """The model output did not match the schema after one repair attempt."""


class TransientError(LLMError):
    """A failure worth retrying (network, rate limit, overload)."""


@dataclass(frozen=True)
class Usage:
    """Token counts for one call."""

    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class LLMRequest:
    """One structured model call."""

    prompt_id: str
    system: str
    user: str
    model: str
    memory_ids: tuple[str, ...] = ()
    repair: str | None = field(default=None, compare=False)

    @property
    def prompt_hash(self) -> str:
        """Hash of the rendered system prompt."""
        return hash_text(self.system)

    @property
    def input_hash(self) -> str:
        """Hash of the user input."""
        return hash_text(self.user)

    @property
    def key(self) -> str:
        """Response-cache key."""
        return cache_key(self.prompt_hash, self.input_hash, self.memory_ids, self.model)


@dataclass(frozen=True)
class RawResponse:
    """Parsed JSON object plus usage, before schema validation."""

    data: dict[str, Any]
    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True)
class LLMResult(Generic[T]):
    """A validated model output with call metadata."""

    value: T
    usage: Usage
    cached: bool
    latency_s: float
    cost_usd: float


class Transport(Protocol):
    """Sends one request and returns the raw JSON object."""

    def send(self, request: LLMRequest, json_schema: dict[str, Any]) -> RawResponse:
        """Call the model. Raise TransientError for retryable failures."""
        ...


class LLMClient(Protocol):
    """What pipeline stages depend on."""

    def complete(self, request: LLMRequest, schema: type[T]) -> LLMResult[T]:
        """Return a validated output or raise LLMError."""
        ...


def call_cost(models: ModelsConfig, model: str, usage: Usage) -> float:
    """Return USD cost for ``usage``, or 0 when the model has no price entry."""
    price = models.prices.get(model)
    if price is None:
        return 0.0
    return (usage.input_tokens * price.input + usage.output_tokens * price.output) / 1_000_000


def retry_transient(
    fn: Callable[[], RawResponse],
    max_retries: int,
    base_s: float,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> RawResponse:
    """Call ``fn``, retrying TransientError with exponential backoff and jitter."""
    # Backoff jitter only, so a plain PRNG is fine.
    rand = rng or random.Random()  # noqa: S311  # nosec B311
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except TransientError:
            if attempt == max_retries:
                raise
            sleep(base_s * (2**attempt) * (0.5 + rand.random() / 2))
    raise AssertionError("unreachable")  # pragma: no cover


class StructuredClient:
    """LLMClient that validates output, repairs once, and caches successes."""

    def __init__(
        self,
        transport: Transport,
        models: ModelsConfig,
        cache: ResponseCache | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Wire the transport, config and optional cache."""
        self._transport = transport
        self._models = models
        self._cache = cache
        self._sleep = sleep

    def complete(self, request: LLMRequest, schema: type[T]) -> LLMResult[T]:
        """Return a validated output, from cache when possible."""
        if self._cache is not None:
            hit = self._cache.get(request.key)
            if hit is not None:
                try:
                    value = schema.model_validate_json(hit)
                except ValidationError:
                    pass  # stale schema: fall through and call the model
                else:
                    return LLMResult(value, Usage(), True, 0.0, 0.0)
        start = time.monotonic()
        raw = self._send(request, schema)
        try:
            value = schema.model_validate(raw.data)
            usage = raw.usage
        except ValidationError as first:
            repaired = LLMRequest(
                request.prompt_id,
                request.system,
                request.user,
                request.model,
                request.memory_ids,
                repair=str(first),
            )
            raw2 = self._send(repaired, schema)
            usage = Usage(
                raw.usage.input_tokens + raw2.usage.input_tokens,
                raw.usage.output_tokens + raw2.usage.output_tokens,
            )
            try:
                value = schema.model_validate(raw2.data)
            except ValidationError as second:
                raise LLMValidationError(
                    f"{request.prompt_id}: output failed schema validation twice: {second}"
                ) from second
        if self._cache is not None:
            self._cache.put(request.key, json.dumps(value.model_dump(mode="json")))
        return LLMResult(
            value,
            usage,
            False,
            time.monotonic() - start,
            call_cost(self._models, request.model, usage),
        )

    def _send(self, request: LLMRequest, schema: type[T]) -> RawResponse:
        return retry_transient(
            lambda: self._transport.send(request, schema.model_json_schema()),
            self._models.max_retries,
            self._models.backoff_base_s,
            self._sleep,
        )
