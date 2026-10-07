"""Fake transport for tests and offline evals. It never touches the network."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from story_agent.llm import LLMError, LLMRequest, RawResponse, Transport, Usage


class FakeTransport:
    """Replays scripted or recorded responses.

    ``queued`` maps a prompt id to responses returned in order. ``recorded``
    maps ``(prompt_id, input_hash)`` to a response and is checked first. A
    queued item that is an exception is raised instead of returned.
    """

    def __init__(
        self,
        queued: dict[str, list[dict[str, Any] | Exception]] | None = None,
        recorded: dict[tuple[str, str], dict[str, Any]] | None = None,
        usage: Usage | None = None,
    ) -> None:
        """Set up the scripted and recorded responses."""
        self._queued: dict[str, deque[dict[str, Any] | Exception]] = defaultdict(deque)
        for prompt_id, items in (queued or {}).items():
            self._queued[prompt_id].extend(items)
        self._recorded = recorded or {}
        self._usage = usage or Usage(100, 50)
        self.calls: list[LLMRequest] = []

    def send(self, request: LLMRequest, _json_schema: dict[str, Any]) -> RawResponse:
        """Return the next response for this request."""
        self.calls.append(request)
        hit = self._recorded.get((request.prompt_id, request.input_hash))
        if hit is not None:
            return RawResponse(hit, self._usage)
        queue = self._queued[request.prompt_id]
        if not queue:
            raise LLMError(f"no fake response for prompt {request.prompt_id}")
        item = queue.popleft()
        if isinstance(item, Exception):
            raise item
        return RawResponse(item, self._usage)


class RecordingTransport:
    """Wraps a real transport and saves responses so tests can replay them."""

    def __init__(self, inner: Transport, path: Path) -> None:
        """Record to ``path`` as JSON lines."""
        self._inner = inner
        self._path = path

    def send(self, request: LLMRequest, json_schema: dict[str, Any]) -> RawResponse:
        """Forward the call and append the response to the recording."""
        response = self._inner.send(request, json_schema)
        line = {
            "prompt_id": request.prompt_id,
            "input_hash": request.input_hash,
            "data": response.data,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, sort_keys=True) + "\n")
        return response


def load_recordings(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """Read a JSON-lines recording into the mapping FakeTransport expects."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            out[(item["prompt_id"], item["input_hash"])] = item["data"]
    return out
