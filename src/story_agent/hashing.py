"""Stable hashing helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable


def hash_text(text: str) -> str:
    """Return the SHA-256 hex digest of ``text``."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cache_key(prompt_hash: str, input_hash: str, memory_ids: Iterable[str], model: str) -> str:
    """Build the response-cache key. Memory ids are sorted so order does not matter."""
    payload = [prompt_hash, input_hash, sorted(set(memory_ids)), model]
    return hash_text(json.dumps(payload, separators=(",", ":")))
