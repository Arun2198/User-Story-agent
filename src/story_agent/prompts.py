"""Load versioned prompt files from the prompts directory."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from story_agent.hashing import hash_text


@dataclass(frozen=True)
class Prompt:
    """A prompt file with its version and content hash."""

    name: str
    version: str
    text: str

    @property
    def hash(self) -> str:
        """Hash of the prompt body."""
        return hash_text(self.text)


def load_prompt(prompts_dir: Path, name: str) -> Prompt:
    """Read ``<name>.md``. An optional front-matter block may set ``version``."""
    raw = (prompts_dir / f"{name}.md").read_text(encoding="utf-8")
    version = "0"
    body = raw
    if raw.startswith("---\n"):
        _, front, body = raw.split("---\n", 2)
        meta = yaml.safe_load(front) or {}
        version = str(meta.get("version", "0"))
    return Prompt(name=name, version=version, text=body.strip())
