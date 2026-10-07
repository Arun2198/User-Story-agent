from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2 as httpx
import pytest

from story_agent.config import AppConfig
from story_agent.llm import AnthropicTransport, LLMError, LLMRequest, TransientError

REQ = LLMRequest("p", "sys", "user", "claude-sonnet-5-5")
SCHEMA: dict[str, Any] = {"type": "object", "properties": {"a": {"type": "string"}}}


def _response(text: str, stop: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        usage=SimpleNamespace(input_tokens=5, output_tokens=7),
    )


def _transport(app_config: AppConfig, create: Any) -> AnthropicTransport:
    transport = AnthropicTransport("key", app_config.models)
    transport._client = SimpleNamespace(messages=SimpleNamespace(create=create))  # type: ignore[assignment]  # stub client
    return transport


def test_parses_json_text(app_config: AppConfig) -> None:
    seen: dict[str, Any] = {}

    def create(**kwargs: Any) -> SimpleNamespace:
        seen.update(kwargs)
        return _response('{"a": "x"}')

    raw = _transport(app_config, create).send(REQ, SCHEMA)
    assert raw.data == {"a": "x"}
    assert raw.usage.output_tokens == 7
    assert seen["output_config"]["format"]["type"] == "json_schema"
    assert seen["extra_body"] == {}


def test_temperature_only_when_configured(app_config: AppConfig) -> None:
    seen: dict[str, Any] = {}

    def create(**kwargs: Any) -> SimpleNamespace:
        seen.update(kwargs)
        return _response("{}")

    transport = _transport(app_config, create)
    transport._temperature = 0.0
    transport.send(REQ, SCHEMA)
    assert seen["extra_body"] == {"temperature": 0.0}


def test_repair_text_is_appended(app_config: AppConfig) -> None:
    seen: dict[str, Any] = {}

    def create(**kwargs: Any) -> SimpleNamespace:
        seen.update(kwargs)
        return _response("{}")

    repair = LLMRequest("p", "sys", "user", "m", repair="field missing")
    _transport(app_config, create).send(repair, SCHEMA)
    assert "field missing" in seen["messages"][0]["content"]


def test_non_json_text_becomes_empty_dict(app_config: AppConfig) -> None:
    raw = _transport(app_config, lambda **_k: _response("not json")).send(REQ, SCHEMA)
    assert raw.data == {}


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_bad_stop_reason(app_config: AppConfig, stop: str) -> None:
    with pytest.raises(LLMError):
        _transport(app_config, lambda **_k: _response("{}", stop)).send(REQ, SCHEMA)


def test_no_text_block(app_config: AppConfig) -> None:
    empty = SimpleNamespace(content=[], stop_reason="end_turn", usage=None)
    with pytest.raises(LLMError):
        _transport(app_config, lambda **_k: empty).send(REQ, SCHEMA)


def _status_error(code: int) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://example.invalid")
    response = httpx.Response(code, request=request)
    return anthropic.APIStatusError("boom", response=response, body=None)


def test_error_mapping(app_config: AppConfig) -> None:
    def server_error(**_k: Any) -> None:
        raise _status_error(503)

    def client_error(**_k: Any) -> None:
        raise _status_error(400)

    def connection(**_k: Any) -> None:
        raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://x.invalid"))

    with pytest.raises(TransientError):
        _transport(app_config, server_error).send(REQ, SCHEMA)
    with pytest.raises(LLMError) as info:
        _transport(app_config, client_error).send(REQ, SCHEMA)
    assert not isinstance(info.value, TransientError)
    with pytest.raises(TransientError):
        _transport(app_config, connection).send(REQ, SCHEMA)
