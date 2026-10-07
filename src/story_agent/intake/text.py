"""Plain-text ingestor."""

from __future__ import annotations

import re

from story_agent.schema import Scenario


def normalize_text(text: str) -> str:
    """Use LF line endings, trim, and collapse runs of blank lines."""
    unified = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return re.sub(r"\n{3,}", "\n\n", unified)


class TextIngestor:
    """Builds a Scenario from text typed or passed on the command line."""

    def ingest(self, source: str, *, notes: str = "", workspace: str = "default") -> Scenario:
        """Return a normalised scenario. Validation of size and content happens in hooks."""
        return Scenario(
            text=normalize_text(source), notes=normalize_text(notes), workspace=workspace
        )
