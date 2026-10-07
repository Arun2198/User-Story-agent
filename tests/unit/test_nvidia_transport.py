import json
from collections.abc import Callable
from typing import Any

import httpx2 as httpx
import pytest
from pydantic import BaseModel

from story_agent.config import AppConfig, ConfigError, ModelsConfig, get_api_key
from story_agent.llm import LLMError, LLMRequest, StructuredClient, TransientError
from story_agent.nvidia import (
    NvidiaTransport,
    clean_json_text,
    extract_json_object,
    transport_from_env,
)

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
    assert set(models.prices) == {models.generator, models.judge}


def test_the_key_is_read_from_the_named_variable() -> None:
    assert get_api_key("NVIDIA_API_KEY", {"NVIDIA_API_KEY": " k \n"}) == "k"
    with pytest.raises(ConfigError, match="NVIDIA_API_KEY is not set"):
        get_api_key("NVIDIA_API_KEY", {})
    with pytest.raises(ConfigError, match="is not set"):
        get_api_key("NVIDIA_API_KEY", {"NVIDIA_API_KEY": "  "})


# ---- structured output modes: auto fallback --------------------------------------------

REAL_REJECTION = {
    "error": {
        "message": "unknown field `guided_json`, expected one of `greed_sampling`, `use_raw_prompt`",  # noqa: E501
        "type": "Bad Request",
        "code": 400,
    }
}


def mode_of(body: dict[str, Any]) -> str:
    if "nvext" in body:
        return "guided_json"
    if "response_format" in body:
        return "json_schema"
    return "none"


def run_auto(
    app_config: AppConfig, accepts: set[str], rejection: dict[str, Any] = REAL_REJECTION
) -> tuple[NvidiaTransport, list[str]]:
    tried: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        mode = mode_of(json.loads(req.content))
        tried.append(mode)
        if mode in accepts:
            return httpx.Response(200, json=reply())
        return httpx.Response(400, json=rejection)

    models = models_with(app_config, structured_output="auto")
    return transport(models, handler), tried


def test_auto_is_the_default_setting(app_config: AppConfig) -> None:
    assert app_config.models.structured_output == "auto"


def test_auto_uses_guided_json_when_the_model_accepts_it(app_config: AppConfig) -> None:
    t, tried = run_auto(app_config, {"guided_json", "json_schema", "none"})
    assert t.send(request(app_config.models), SCHEMA).data == {"a": "x"}
    assert tried == ["guided_json"]


def test_auto_falls_back_when_guided_json_is_rejected(app_config: AppConfig) -> None:
    t, tried = run_auto(app_config, {"json_schema", "none"})
    assert t.send(request(app_config.models), SCHEMA).data == {"a": "x"}
    assert tried == ["guided_json", "json_schema"]
    assert t.modes == {app_config.models.generator: "json_schema"}


def test_auto_falls_back_to_the_prompt_as_a_last_resort(app_config: AppConfig) -> None:
    t, tried = run_auto(app_config, {"none"})
    t.send(request(app_config.models), SCHEMA)
    assert tried == ["guided_json", "json_schema", "none"]


def test_auto_remembers_the_mode_that_worked(app_config: AppConfig) -> None:
    t, tried = run_auto(app_config, {"json_schema"})
    t.send(request(app_config.models), SCHEMA)
    t.send(request(app_config.models), SCHEMA)
    assert tried == ["guided_json", "json_schema", "json_schema"]


def test_auto_remembers_per_model(app_config: AppConfig) -> None:
    t, tried = run_auto(app_config, {"json_schema"})
    t.send(request(app_config.models), SCHEMA)
    other = LLMRequest("p", "sys", "user", "some/other-model")
    t.send(other, SCHEMA)
    assert tried == ["guided_json", "json_schema", "guided_json", "json_schema"]
    assert set(t.modes) == {app_config.models.generator, "some/other-model"}


def test_auto_gives_up_when_no_mode_is_accepted(app_config: AppConfig) -> None:
    t, tried = run_auto(app_config, set())
    with pytest.raises(LLMError, match="accepted none"):
        t.send(request(app_config.models), SCHEMA)
    assert tried == ["guided_json", "json_schema", "none"]


def test_auto_does_not_try_other_modes_for_unrelated_errors(app_config: AppConfig) -> None:
    tried: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        tried.append(mode_of(json.loads(req.content)))
        return httpx.Response(401, json={"detail": "Authentication failed"})

    t = transport(models_with(app_config, structured_output="auto"), handler)
    with pytest.raises(LLMError, match="key was rejected"):
        t.send(request(app_config.models), SCHEMA)
    assert tried == ["guided_json"]


def test_a_generic_bad_request_is_not_treated_as_a_mode_problem(app_config: AppConfig) -> None:
    t, tried = run_auto(app_config, set(), {"detail": "max_tokens is too large"})
    with pytest.raises(LLMError, match="max_tokens is too large") as info:
        t.send(request(app_config.models), SCHEMA)
    assert tried == ["guided_json"]
    assert "accepted none" not in str(info.value)


def test_a_fixed_mode_never_falls_back(app_config: AppConfig) -> None:
    tried: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        tried.append(mode_of(json.loads(req.content)))
        return httpx.Response(400, json=REAL_REJECTION)

    models = models_with(app_config, structured_output="guided_json")
    with pytest.raises(LLMError, match="guided_json"):
        transport(models, handler).send(request(models), SCHEMA)
    assert tried == ["guided_json"]


# ---- reasoning models --------------------------------------------------------------------


def test_a_model_that_runs_out_of_tokens_while_thinking_gets_a_clear_error(
    app_config: AppConfig,
) -> None:
    payload = {
        "choices": [
            {
                "message": {"content": "Here's a thinking process", "reasoning_content": "Here's"},
                "finish_reason": "length",
            }
        ],
        "usage": {"prompt_tokens": 25, "completion_tokens": 50},
    }
    t = transport(app_config.models, lambda _r: httpx.Response(200, json=payload))
    with pytest.raises(LLMError, match="while thinking") as info:
        t.send(request(app_config.models), SCHEMA)
    assert "extra_body" in str(info.value)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('Let me think. The answer is {"a": "x"}.', {"a": "x"}),
        ('First idea {"a": "draft"} but final: {"a": "final"}', {"a": "final"}),
        ("Prose with {braces} and no json", {}),
        ('{"a": "x"} trailing words', {"a": "x"}),
    ],
)
def test_json_is_found_after_prose(text: str, expected: dict[str, Any]) -> None:
    assert extract_json_object(text) == expected


def test_extra_body_is_merged_for_the_matching_model_only(app_config: AppConfig) -> None:
    extra = {
        app_config.models.generator: {
            "chat_template_kwargs": {"enable_thinking": False},
            "nvext": {"max_thinking_tokens": 0},
        }
    }
    models = models_with(app_config, extra_body=extra, structured_output="guided_json")
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=reply())

    t = transport(models, handler)
    t.send(request(models), SCHEMA)
    t.send(LLMRequest("p", "sys", "user", "some/other-model"), SCHEMA)
    assert seen[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert seen[0]["nvext"] == {"guided_json": SCHEMA, "max_thinking_tokens": 0}
    assert "chat_template_kwargs" not in seen[1]
    assert seen[1]["nvext"] == {"guided_json": SCHEMA}


def test_no_token_limit_is_sent_by_default_and_a_configured_one_is(app_config: AppConfig) -> None:
    assert app_config.models.max_tokens is None
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=reply())

    transport(app_config.models, handler).send(request(app_config.models), SCHEMA)
    capped = models_with(app_config, max_tokens=300)
    transport(capped, handler).send(request(capped), SCHEMA)
    assert "max_tokens" not in seen[0]
    assert seen[1]["max_tokens"] == 300


# ---- retired models and the models command --------------------------------------------


def test_a_retired_model_says_so_and_points_to_the_models_command(app_config: AppConfig) -> None:
    body = {"detail": "The model 'x' has reached its end of life and is no longer available."}
    t = transport(app_config.models, lambda _r: httpx.Response(410, json=body))
    with pytest.raises(LLMError, match="has been retired") as info:
        t.send(request(app_config.models), SCHEMA)
    assert "story-agent models" in str(info.value)
    assert not isinstance(info.value, TransientError)


def test_list_models_returns_sorted_ids(app_config: AppConfig) -> None:
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(
            200, json={"data": [{"id": "b/two"}, {"id": "a/one"}, {"object": "x"}]}
        )

    assert transport(app_config.models, handler).list_models() == ["a/one", "b/two"]
    assert seen["url"].endswith("/v1/models")
    assert seen["auth"] == "Bearer test-key"


def test_list_models_reports_a_rejected_key(app_config: AppConfig) -> None:
    t = transport(app_config.models, lambda _r: httpx.Response(401, json={"detail": "no"}))
    with pytest.raises(LLMError, match="key was rejected"):
        t.list_models()


# ---- one key for every model ----------------------------------------------------------------


def test_every_model_is_called_with_the_same_key(app_config: AppConfig) -> None:
    models = app_config.models
    auths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        auths.append(req.headers["authorization"])
        return httpx.Response(200, json=reply())

    t = transport(models, handler)
    t.send(LLMRequest("p", "s", "u", models.generator), SCHEMA)
    t.send(LLMRequest("p", "s", "u", models.judge), SCHEMA)
    assert auths == ["Bearer test-key", "Bearer test-key"]


def test_the_key_comes_from_the_named_variable(app_config: AppConfig) -> None:
    t = transport_from_env(app_config.models, {"NVIDIA_API_KEY": " main-key \n"})
    assert t._key == "main-key"  # the one key every request uses
    with pytest.raises(ConfigError, match="NVIDIA_API_KEY is not set"):
        transport_from_env(app_config.models, {})


def test_the_shipped_config_has_one_key_and_distinct_models(app_config: AppConfig) -> None:
    models = app_config.models
    assert models.api_key_env == "NVIDIA_API_KEY"
    assert not hasattr(models, "model_api_key_env")
    assert models.judge != models.generator
    assert set(models.prices) == {models.generator, models.judge}
