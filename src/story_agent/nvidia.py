"""Transport for NVIDIA's hosted models (OpenAI-style chat completions).

The request goes to ``<base_url>/chat/completions``. Structured output uses NVIDIA's
``nvext.guided_json`` by default, or ``response_format`` / plain instructions when
``structured_output`` in config says so. The key is read from the environment and only ever
sent in the ``Authorization`` header.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx2 as httpx

from story_agent.config import ModelsConfig
from story_agent.llm import LLMError, LLMRequest, RawResponse, TransientError, Usage

TRANSIENT_STATUS = {408, 409, 425, 429}
MODES = ("guided_json", "json_schema", "none")
_UNSUPPORTED = re.compile(
    r"guided_json|nvext|response_format|json_schema|unknown field|unsupported|not supported", re.I
)
DETAIL_CHARS = 200
_THINK = re.compile(r"<think>.*?</think>", re.S)
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.S)


def clean_json_text(text: str) -> str:
    """Drop reasoning blocks and code fences some models wrap around JSON."""
    text = _THINK.sub("", text).strip()
    fenced = _FENCE.match(text)
    return fenced.group(1) if fenced else text


class UnsupportedModeError(LLMError):
    """The model rejected the way the schema was sent, not the request itself."""


class NvidiaTransport:
    """Sends structured requests to an NVIDIA-hosted model."""

    def __init__(
        self, api_key: str, models: ModelsConfig, client: httpx.Client | None = None
    ) -> None:
        """Keep the settings. ``client`` can be replaced in tests."""
        self._key = api_key
        self._models = models
        self._client = client or httpx.Client(timeout=models.timeout_s)
        self._url = models.base_url.rstrip("/") + "/chat/completions"
        self._working: dict[str, str] = {}

    @property
    def modes(self) -> dict[str, str]:
        """The structured-output mode that worked for each model so far."""
        return dict(self._working)

    def send(self, request: LLMRequest, json_schema: dict[str, Any]) -> RawResponse:
        """Call the model and return the parsed JSON object.

        With ``structured_output: auto`` a model that rejects one way of sending the schema is
        retried with the next, and the first that works is remembered for that model.
        """
        configured = self._models.structured_output
        if configured != "auto":
            return self._send_once(request, json_schema, configured)
        remembered = self._working.get(request.model)
        order = [remembered] if remembered else list(MODES)
        last: UnsupportedModeError | None = None
        for mode in order:
            try:
                raw = self._send_once(request, json_schema, mode)
            except UnsupportedModeError as exc:
                last = exc
                continue
            self._working[request.model] = mode
            return raw
        raise LLMError(f"the model accepted none of the structured output modes: {last}")

    def _send_once(
        self, request: LLMRequest, json_schema: dict[str, Any], mode: str
    ) -> RawResponse:
        try:
            response = self._client.post(
                self._url,
                json=self._body(request, json_schema, mode),
                headers={"Authorization": f"Bearer {self._key}", "Accept": "application/json"},
            )
        except httpx.TimeoutException as exc:
            raise TransientError("the request timed out") from exc
        except httpx.TransportError as exc:
            raise TransientError(f"network error: {type(exc).__name__}") from exc
        payload = self._payload(response)
        if response.status_code >= 400:
            raise self._error(response.status_code, payload, request.model)
        return self._parse(payload)

    def _body(self, request: LLMRequest, json_schema: dict[str, Any], mode: str) -> dict[str, Any]:
        system = request.system
        if mode == "none":
            schema_text = json.dumps(json_schema)
            system += f"\n\nReturn one JSON object that matches this JSON schema:\n{schema_text}"
        user = request.user
        if request.repair:
            user += (
                "\n\nYour previous output failed validation with this error. "
                f"Return corrected JSON only.\n{request.repair}"
            )
        body: dict[str, Any] = {
            "model": request.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self._models.max_tokens,
            "stream": False,
        }
        if self._models.temperature is not None:
            body["temperature"] = self._models.temperature
        if mode == "guided_json":
            body["nvext"] = {"guided_json": json_schema}
        elif mode == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": request.prompt_id, "schema": json_schema},
            }
        return body

    @staticmethod
    def _payload(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _error(status: int, payload: dict[str, Any], model: str) -> LLMError:
        detail = payload.get("detail") or payload.get("message")
        error = payload.get("error")
        if isinstance(error, dict):
            detail = error.get("message") or detail
        text = f" ({str(detail)[:DETAIL_CHARS]})" if detail else ""
        if status in {400, 422} and _UNSUPPORTED.search(text):
            return UnsupportedModeError(f"API error {status}{text}")
        if status in TRANSIENT_STATUS or status >= 500:
            return TransientError(f"API error {status}{text}")
        if status in {401, 403}:
            return LLMError(f"API error {status}: the key was rejected{text}")
        if status == 404:
            return LLMError(f"API error 404: model {model} was not found{text}")
        return LLMError(f"API error {status}{text}")

    @staticmethod
    def _parse(payload: dict[str, Any]) -> RawResponse:
        choices = payload.get("choices") or []
        if not choices:
            raise LLMError("the model returned no choices")
        choice = choices[0]
        reason = choice.get("finish_reason")
        if reason in {"length", "content_filter"}:
            raise LLMError(f"model stopped with {reason}")
        content = (choice.get("message") or {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise LLMError("model returned no text output")
        try:
            data = json.loads(clean_json_text(content))
        except json.JSONDecodeError:
            data = {}
        used = payload.get("usage") or {}
        usage = Usage(int(used.get("prompt_tokens", 0)), int(used.get("completion_tokens", 0)))
        return RawResponse(data if isinstance(data, dict) else {}, usage)
