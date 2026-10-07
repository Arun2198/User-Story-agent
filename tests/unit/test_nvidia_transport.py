import json
from collections.abc import Callable
from typing import Any

import httpx2 as httpx
import pytest
from pydantic import BaseModel

from story_agent.config import AppConfig, ConfigError, ModelsConfig, get_api_key
from story_agent.llm import LLMError, LLMRequest, StructuredClient, TransientError
from story_agent.nvidia import NvidiaTransport, clean_json_text

SCHEMA: dict[str, Any] = {"type": "object", "properties": {"a": {"type": "string"}}}


def reply(text: str = '{"a": "x"}', finish: str = "stop", **extra: Any) -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": text}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7},
        **extra,
    }


def transport(
    models: ModelsConfig, handler: Callable[[httpx.Request], httpx.Response]
) -> NvidiaTransport:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return NvidiaTransport("test-key", models, client)


def request(models: ModelsConfig, repair: str | None = None) -> LLMRequest:
    return LLMRequest("p", "sys", "user", models.generator, repair=repair)


def models_with(app_config: AppConfig, **update: Any) -> ModelsConfig:
    return app_config.models.model_copy(update=update)


def test_a_request_is_built_for_the_chat_endpoint(app_config: AppConfig) -> None:
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=reply())

    raw = transport(app_config.models, handler).send(request(app_config.models), SCHEMA)
    assert raw.data == {"a": "x"}
    assert (raw.usage.input_tokens, raw.usage.output_tokens) == (5, 7)
    assert seen["url"] == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert seen["auth"] == "Bearer test-key"
    body = seen["body"]
    assert body["model"] == app_config.models.generator
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["messages"][1] == {"role": "user", "content": "user"}
    assert body["nvext"] == {"guided_json": SCHEMA}
    assert body["temperature"] == 0
    assert body["stream"] is False
    assert "response_format" not in body


def test_the_key_is_only_ever_sent_in_the_header(app_config: AppConfig) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=reply())

    transport(app_config.models, handler).send(request(app_config.models), SCHEMA)
    assert b"test-key" not in seen[0].content
    assert "test-key" not in str(seen[0].url)


def test_json_schema_mode_uses_response_format(app_config: AppConfig) -> None:
    models = models_with(app_config, structured_output="json_schema")
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(json.loads(req.content))
        return httpx.Response(200, json=reply())

    transport(models, handler).send(request(models), SCHEMA)
    assert seen["response_format"]["type"] == "json_schema"
    assert seen["response_format"]["json_schema"]["schema"] == SCHEMA
    assert "nvext" not in seen


def test_none_mode_puts_the_schema_in_the_prompt(app_config: AppConfig) -> None:
    models = models_with(app_config, structured_output="none")
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(json.loads(req.content))
        return httpx.Response(200, json=reply())

    transport(models, handler).send(request(models), SCHEMA)
    assert "nvext" not in seen
    assert "response_format" not in seen
    assert json.dumps(SCHEMA) in seen["messages"][0]["content"]


def test_temperature_is_sent_only_when_configured(app_config: AppConfig) -> None:
    models = models_with(app_config, temperature=None)
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(json.loads(req.content))
        return httpx.Response(200, json=reply())

    transport(models, handler).send(request(models), SCHEMA)
    assert "temperature" not in seen


def test_repair_text_is_appended_to_the_user_message(app_config: AppConfig) -> None:
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(json.loads(req.content))
        return httpx.Response(200, json=reply())

    transport(app_config.models, handler).send(request(app_config.models, "bad field"), SCHEMA)
    assert "bad field" in seen["messages"][1]["content"]
    assert "corrected JSON" in seen["messages"][1]["content"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": "x"}', {"a": "x"}),
        ('```json\n{"a": "x"}\n```', {"a": "x"}),
        ('<think>hmm {"a": 1}</think>\n{"a": "x"}', {"a": "x"}),
        ("not json", {}),
        ("[1, 2]", {}),
    ],
)
def test_replies_are_cleaned_and_parsed(
    app_config: AppConfig, text: str, expected: dict[str, Any]
) -> None:
    raw = transport(app_config.models, lambda _r: httpx.Response(200, json=reply(text))).send(
        request(app_config.models), SCHEMA
    )
    assert raw.data == expected


def test_clean_json_text_leaves_plain_json_alone() -> None:
    assert clean_json_text('  {"a": 1}  ') == '{"a": 1}'


@pytest.mark.parametrize("finish", ["length", "content_filter"])
def test_a_cut_off_or_filtered_reply_is_an_error(app_config: AppConfig, finish: str) -> None:
    t = transport(app_config.models, lambda _r: httpx.Response(200, json=reply(finish=finish)))
    with pytest.raises(LLMError, match=finish):
        t.send(request(app_config.models), SCHEMA)


@pytest.mark.parametrize(
    "payload", [{"choices": []}, {}, reply(""), {"choices": [{"message": {"content": None}}]}]
)
def test_an_empty_reply_is_an_error(app_config: AppConfig, payload: dict[str, Any]) -> None:
    t = transport(app_config.models, lambda _r: httpx.Response(200, json=payload))
    with pytest.raises(LLMError):
        t.send(request(app_config.models), SCHEMA)


def test_a_non_json_body_is_an_error(app_config: AppConfig) -> None:
    t = transport(app_config.models, lambda _r: httpx.Response(200, content=b"<html>"))
    with pytest.raises(LLMError, match="no choices"):
        t.send(request(app_config.models), SCHEMA)


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503])
def test_retryable_statuses_are_transient(app_config: AppConfig, status: int) -> None:
    t = transport(app_config.models, lambda _r: httpx.Response(status, json={"detail": "busy"}))
    with pytest.raises(TransientError, match=str(status)):
        t.send(request(app_config.models), SCHEMA)


def test_authentication_failures_are_clear_and_do_not_retry(app_config: AppConfig) -> None:
    body = {"status": 401, "detail": "Authentication failed"}
    t = transport(app_config.models, lambda _r: httpx.Response(401, json=body))
    with pytest.raises(LLMError, match="key was rejected") as info:
        t.send(request(app_config.models), SCHEMA)
    assert not isinstance(info.value, TransientError)
    assert "test-key" not in str(info.value)


def test_an_unknown_model_names_the_model(app_config: AppConfig) -> None:
    body = {"error": {"message": "not found"}}
    t = transport(app_config.models, lambda _r: httpx.Response(404, json=body))
    with pytest.raises(LLMError, match=f"model {app_config.models.generator} was not found"):
        t.send(request(app_config.models), SCHEMA)


def test_other_client_errors_are_fatal_and_details_are_short(app_config: AppConfig) -> None:
    body = {"detail": "x" * 1000}
    t = transport(app_config.models, lambda _r: httpx.Response(422, json=body))
    with pytest.raises(LLMError, match="422") as info:
        t.send(request(app_config.models), SCHEMA)
    assert len(str(info.value)) < 300
    assert not isinstance(info.value, TransientError)


def test_network_problems_are_transient(app_config: AppConfig) -> None:
    def timeout(_r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    def refused(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with pytest.raises(TransientError, match="timed out"):
        transport(app_config.models, timeout).send(request(app_config.models), SCHEMA)
    with pytest.raises(TransientError, match="ConnectError"):
        transport(app_config.models, refused).send(request(app_config.models), SCHEMA)


class Out(BaseModel):
    a: str


def test_the_client_retries_a_rate_limit_then_succeeds(app_config: AppConfig) -> None:
    calls = {"n": 0}

    def handler(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429) if calls["n"] == 1 else httpx.Response(200, json=reply())

    client = StructuredClient(
        transport(app_config.models, handler), app_config.models, sleep=lambda _s: None
    )
    result = client.complete(request(app_config.models), Out)
    assert result.value.a == "x"
    assert calls["n"] == 2


def test_the_client_repairs_once_with_the_validation_error(app_config: AppConfig) -> None:
    bodies: list[dict[str, Any]] = []

    def handler(r: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(r.content))
        return httpx.Response(
            200, json=reply('{"wrong": 1}' if len(bodies) == 1 else '{"a": "ok"}')
        )

    client = StructuredClient(
        transport(app_config.models, handler), app_config.models, sleep=lambda _s: None
    )
    assert client.complete(request(app_config.models), Out).value.a == "ok"
    assert "corrected JSON" in bodies[1]["messages"][1]["content"]


def test_default_models_and_key_name_come_from_config(app_config: AppConfig) -> None:
    models = app_config.models
    assert models.api_key_env == "NVIDIA_API_KEY"
    assert models.base_url.startswith("https://integrate.api.nvidia.com")
    assert models.generator != models.judge
    assert set(models.prices) == {models.generator, models.judge}


def test_the_key_is_read_from_the_named_variable() -> None:
    assert get_api_key("NVIDIA_API_KEY", {"NVIDIA_API_KEY": " k \n"}) == "k"
    with pytest.raises(ConfigError, match="NVIDIA_API_KEY is not set"):
        get_api_key("NVIDIA_API_KEY", {})
    with pytest.raises(ConfigError, match="is not set"):
        get_api_key("NVIDIA_API_KEY", {"NVIDIA_API_KEY": "  "})
