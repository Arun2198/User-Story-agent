"""Dependencies passed into pipeline stages. Built once by the caller."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from story_agent.config import AppConfig
from story_agent.discovery.packs import PackSet
from story_agent.llm import LLMClient


@dataclass(frozen=True)
class StageDeps:
    """Everything a stage needs besides the run state."""

    client: LLMClient
    config: AppConfig
    packs: PackSet
    prompts_dir: Path
