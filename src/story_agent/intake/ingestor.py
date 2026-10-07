"""Ingestor interface. Only plain text is built in this pass."""

from __future__ import annotations

from typing import Protocol

from story_agent.schema import Scenario


class Ingestor(Protocol):
    """Turns a source (text, file, URL) into a Scenario."""

    def ingest(self, source: str, *, notes: str = "", workspace: str = "default") -> Scenario:
        """Return the scenario for ``source``."""
        ...
