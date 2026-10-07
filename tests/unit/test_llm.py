from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

from story_agent.cache import MemoryCache, SqliteCache
from story_agent.config import AppConfig, Price
from story_agent.fake_llm import FakeTransport, RecordingTransport, load_recordings
from story_agent.hashing import cache_key
from story_agent.llm import (
    LLMError,
    LLMRequest,
    LLMValidationError,
    RawResponse,
    StructuredClient,
    TransientError,
    Usage,
    call_cost,
    retry_transient,
)


class Out(BaseModel):
    answer: str


def _req(user: str = "hi", memory: tuple[str, ...] = ()) -> LLMRequest:
    return LLMRequest("p1", "system", user, "test-model", memory)


def _client(
    app_config: AppConfig, fake: FakeTransport, cache: MemoryCache | None = None
) -> tuple[StructuredClient, list[float]]:
    sleeps: list[float] = []
    models = app_config.models.model_copy(
        update={"prices": {"test-model": Price(input=2.0, output=10.0)}}
    )
    return StructuredClient(fake, models, cache, sleep=sleeps.append), sleeps


def test_valid_output(app_config: AppConfig) -> None:
    client, _ = _client(app_config, FakeTransport({"p1": [{"answer": "a"}]}))
    result = client.complete(_req(), Out)
    assert result.value.answer == "a"
    assert not result.cached
    assert result.cost_usd > 0


def test_repair_retry_then_success(app_config: AppConfig) -> None:
    fake = FakeTransport({"p1": [{"wrong": 1}, {"answer": "ok"}]})
    client, _ = _client(app_config, fake)
    assert client.complete(_req(), Out).value.answer == "ok"
    assert fake.calls[1].repair is not None
    assert fake.calls[0].repair is None


def test_fail_closed_after_second_failure(app_config: AppConfig) -> None:
    fake = FakeTransport({"p1": [{"wrong": 1}, {"wrong": 2}]})
    client, _ = _client(app_config, fake)
    with pytest.raises(LLMValidationError):
        client.complete(_req(), Out)
    assert len(fake.calls) == 2


def test_cache_hit_skips_transport(app_config: AppConfig) -> None:
    fake = FakeTransport({"p1": [{"answer": "a"}]})
    client, _ = _client(app_config, fake, MemoryCache())
    client.complete(_req(), Out)
    second = client.complete(_req(), Out)
    assert second.cached
    assert len(fake.calls) == 1


def test_cache_key_includes_memory_ids(app_config: AppConfig) -> None:
    fake = FakeTransport({"p1": [{"answer": "a"}, {"answer": "b"}]})
    client, _ = _client(app_config, fake, MemoryCache())
    client.complete(_req(memory=("M1",)), Out)
    client.complete(_req(memory=("M2",)), Out)
    assert len(fake.calls) == 2


def test_failures_not_cached(app_config: AppConfig) -> None:
    cache = MemoryCache()
    fake = FakeTransport({"p1": [{"x": 1}, {"x": 1}, {"answer": "ok"}]})
    client, _ = _client(app_config, fake, cache)
    with pytest.raises(LLMValidationError):
        client.complete(_req(), Out)
    assert cache.get(_req().key) is None
    assert client.complete(_req(), Out).value.answer == "ok"


def test_stale_cache_entry_is_ignored(app_config: AppConfig) -> None:
    cache = MemoryCache()
    cache.put(_req().key, '{"not": "valid"}')
    client, _ = _client(app_config, FakeTransport({"p1": [{"answer": "fresh"}]}), cache)
    assert client.complete(_req(), Out).value.answer == "fresh"


def test_transient_retry_with_backoff(app_config: AppConfig) -> None:
    fake = FakeTransport({"p1": [TransientError("x"), TransientError("y"), {"answer": "a"}]})
    client, sleeps = _client(app_config, fake)
    assert client.complete(_req(), Out).value.answer == "a"
    assert len(sleeps) == 2
    assert sleeps[1] > sleeps[0] * 0.9  # exponential, within jitter


def test_transient_exhausted(app_config: AppConfig) -> None:
    n = app_config.models.max_retries + 1
    client, _ = _client(app_config, FakeTransport({"p1": [TransientError("x")] * n}))
    with pytest.raises(TransientError):
        client.complete(_req(), Out)


def test_non_transient_error_not_retried(app_config: AppConfig) -> None:
    fake = FakeTransport({"p1": [LLMError("bad request"), {"answer": "a"}]})
    client, sleeps = _client(app_config, fake)
    with pytest.raises(LLMError):
        client.complete(_req(), Out)
    assert sleeps == []


def test_fake_without_response_raises(app_config: AppConfig) -> None:
    client, _ = _client(app_config, FakeTransport())
    with pytest.raises(LLMError):
        client.complete(_req(), Out)


def test_retry_helper_returns_first_success() -> None:
    calls = []

    def fn() -> RawResponse:
        calls.append(1)
        return RawResponse({"a": 1})

    assert retry_transient(fn, 3, 1.0, lambda _s: None).data == {"a": 1}
    assert len(calls) == 1


def test_cost(app_config: AppConfig) -> None:
    models = app_config.models.model_copy(
        update={"prices": {"test-model": Price(input=2.0, output=10.0)}}
    )
    assert call_cost(models, "test-model", Usage(1_000_000, 1_000_000)) == pytest.approx(12.0)
    assert call_cost(app_config.models, app_config.models.generator, Usage(1_000_000, 0)) > 0
    assert call_cost(app_config.models, "unknown", Usage(10, 10)) == 0.0


@given(st.lists(st.text(min_size=1), min_size=1, max_size=5))
def test_cache_key_ignores_memory_order(ids: list[str]) -> None:
    assert cache_key("p", "i", ids, "m") == cache_key("p", "i", list(reversed(ids)), "m")


def test_sqlite_cache(tmp_path: Path) -> None:
    cache = SqliteCache(tmp_path / "sub" / "c.db")
    assert cache.get("k") is None
    cache.put("k", "v")
    cache.put("k", "v2")
    assert cache.get("k") == "v2"
    cache.close()


def test_recording_roundtrip(tmp_path: Path, app_config: AppConfig) -> None:
    path = tmp_path / "rec.jsonl"
    inner = FakeTransport({"p1": [{"answer": "rec"}]})
    client = StructuredClient(RecordingTransport(inner, path), app_config.models)
    client.complete(_req(), Out)
    replay = FakeTransport(recorded=load_recordings(path))
    out = StructuredClient(replay, app_config.models).complete(_req(), Out)
    assert out.value.answer == "rec"
