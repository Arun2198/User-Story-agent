"""Transport for Anthropic's Messages API, using JSON-schema structured output.

Claude 5.x models reject non-default sampling settings and forced tool choice, so the schema goes
in ``output_config.format`` and temperature is sent only when configured. The API needs a limit
on every call, so when config sets none a generous default is used.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic

from story_agent.config import ModelsConfig
from story_agent.llm import LLMError, LLMRequest, RawResponse, TransientError, Usage

DEFAULT_MAX_TOKENS = 16000
DETAIL_CHARS = 200


class AnthropicTransport:
    """Sends structured requests to Anthropic."""

    def __init__(self, api_key: str, models: ModelsConfig) -> None:
        """Create the SDK client. SDK retries are off; ``retry_transient`` handles them."""
        self._client = anthropic.Anthropic(api_key=api_key, timeout=models.timeout_s, max_retries=0)
        self._max_tokens = models.max_tokens or DEFAULT_MAX_TOKENS
        self._temperature = models.temperature

    def send(self, request: LLMRequest, json_schema: dict[str, Any]) -> RawResponse:
        """Call the API and return the parsed JSON object."""
        user = request.user
        if request.repair:
            user += (
                "\n\nYour previous output failed validation with this error. "
                f"Return corrected JSON only.\n{request.repair}"
            )
        extra: dict[str, Any] = {}
        if self._temperature is not None:
            extra["temperature"] = self._temperature
        try:
            response = self._client.messages.create(
                model=request.model,
                max_tokens=self._max_tokens,
                system=request.system,
                messages=[{"role": "user", "content": user}],
                output_config={
                    "format": {
                        "type": "json_schema",
                        "schema": anthropic.transform_schema(json_schema),
                    }
                },
                extra_body=extra,
            )
        except (
            anthropic.APIConnectionError,
            anthropic.APITimeoutError,
            anthropic.RateLimitError,
        ) as exc:
            raise TransientError(f"{type(exc).__name__}: {str(exc)[:DETAIL_CHARS]}") from exc
        except anthropic.APIStatusError as exc:
            raise self._error(exc, request.model) from exc
        if response.stop_reason in {"refusal", "max_tokens"}:
            raise LLMError(f"model stopped with {response.stop_reason}")
        for block in response.content:
            if block.type == "text":
                try:
                    data = json.loads(block.text)
                except json.JSONDecodeError:
                    data = {}
                usage = Usage(response.usage.input_tokens, response.usage.output_tokens)
                return RawResponse(data if isinstance(data, dict) else {}, usage)
        raise LLMError("model returned no text output")

    def list_models(self) -> list[str]:
        """Return the model ids the key can use."""
        try:
            return sorted(m.id for m in self._client.models.list(limit=100))
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            raise TransientError(f"network error: {type(exc).__name__}") from exc
        except anthropic.APIStatusError as exc:
            raise self._error(exc, "(list)") from exc

    @staticmethod
    def _error(exc: anthropic.APIStatusError, model: str) -> LLMError:
        status = exc.status_code
        if status >= 500 or status in {408, 409, 429}:  # 529 is "overloaded"
            return TransientError(f"API error {status}")
        if status in {401, 403}:
            return LLMError(f"API error {status}: the key was rejected")
        if status == 404:
            return LLMError(f"API error 404: model {model} was not found")
        return LLMError(f"API error {status}")
