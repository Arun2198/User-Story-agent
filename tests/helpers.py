"""Shared builders for tests."""

from pathlib import Path
from typing import Any

from story_agent.config import AppConfig
from story_agent.deps import StageDeps
from story_agent.discovery.discover import run_discover
from story_agent.discovery.packs import PackSet
from story_agent.fake_llm import FakeTransport
from story_agent.llm import StructuredClient
from story_agent.schema import RunState, Scenario

DISPUTE = (
    "A customer disputes a card transaction and expects a provisional credit "
    "while the bank investigates. The relationship manager can see the case."
)


def make_state(text: str = DISPUTE, notes: str = "", run_id: str = "r1") -> RunState:
    return RunState(
        run_id=run_id,
        scenario=Scenario(text=text, notes=notes),
        redacted_text=text,
        redacted_notes=notes,
    )


def make_deps(
    config: AppConfig,
    packs: PackSet,
    prompts_dir: Path,
    queued: dict[str, list[Any]] | None = None,
) -> tuple[StageDeps, FakeTransport]:
    fake = FakeTransport(queued or {})
    client = StructuredClient(fake, config.models)
    return StageDeps(client, config, packs, prompts_dir), fake


def discover_output(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "domain": "banking",
        "subdomain": "cards",
        "subpacks": [],
        "actors": ["Customer", "relationship manager", "Customer"],
        "goals": ["Dispute a transaction"],
        "business_context": "Card dispute handling.",
        "items": [
            {
                "category_id": "dispute_handling",
                "description": "Customer expects a provisional credit during the investigation.",
                "status": "stated",
                "evidence": "expects a provisional credit while the bank investigates",
            },
            {
                "category_id": "dispute_handling",
                "description": "Time limits and evidence rules are not given.",
                "status": "unknown",
                "evidence": "",
            },
            {
                "category_id": "limits_velocity",
                "description": "A claim limit may exist.",
                "status": "inferred",
                "evidence": "",
            },
        ],
    }
    base.update(overrides)
    return base


def clarify_output(state: RunState, categories: list[str]) -> dict[str, Any]:
    return {
        "questions": [
            {
                "category_id": c,
                "question": f"Question about {c}?",
                "why_it_matters": f"It shapes the {c} stories.",
                "options": ["Option A", "Option B", "Other", "option a"],
            }
            for c in categories
        ]
    }


def discovered(
    config: AppConfig, packs: PackSet, prompts: Path, queued: dict[str, list[Any]] | None = None
) -> tuple[StageDeps, FakeTransport, RunState, Any]:
    """Run discover with the fake client and return deps, fake, state and checklist."""
    queued = dict(queued or {})
    queued.setdefault("discover", [discover_output()])
    deps, fake = make_deps(config, packs, prompts, queued)
    state = make_state()
    result = run_discover(deps, state)
    state.discovery = result.discovery
    return deps, fake, state, result.checklist
