"""What the ``run`` and ``resume`` commands need beyond argument parsing."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from story_agent.cache import SqliteCache
from story_agent.config import AppConfig, ConfigError, default_config_dir, get_api_key, load_config
from story_agent.deps import StageDeps
from story_agent.discovery.packs import load_packs
from story_agent.graph import Runtime
from story_agent.hooks import build_pipeline, default_registry
from story_agent.interactive import PromptResponder
from story_agent.llm import AnthropicTransport, StructuredClient, Transport
from story_agent.responders import AnswersFileResponder, load_run_answers
from story_agent.session import Responder, RunOutcome

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NO_TERMINAL = 2
EXIT_PAUSED = 3
EXIT_STOPPED = 4

NO_TERMINAL = (
    "There is no terminal to ask you questions. Pass --answers FILE to run without one "
    "(see the README), or run this in a terminal."
)


def stdin_is_tty() -> bool:
    """Say whether a person can answer prompts."""
    return sys.stdin.isatty()


def write_private(path: Path, text: str) -> None:
    """Write a file readable by its owner only."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def make_transport(config: AppConfig) -> Transport:
    """Return the real model transport. The key comes from the environment only."""
    return AnthropicTransport(get_api_key(), config.models)


@dataclass
class Chosen:
    """The responder for this run, and the objects that can say why it stopped."""

    responder: Responder
    file: AnswersFileResponder | None = None
    prompt: PromptResponder | None = None

    @property
    def stopped(self) -> str:
        """Why the responder gave up, if it did."""
        for source in (self.file, self.prompt):
            if source is not None and source.stopped:
                return source.stopped
        return ""


def choose_responder(answers: Path | None) -> Chosen:
    """Pick who answers the pauses. Without a terminal an answers file is required.

    This runs before any model call, so a run that could not be answered never starts.
    """
    if answers is not None:
        file = AnswersFileResponder(load_run_answers(answers))
        return Chosen(file, file=file)
    if not stdin_is_tty():
        raise NoTerminalError(NO_TERMINAL)
    prompt = PromptResponder()
    return Chosen(prompt, prompt=prompt)


class NoTerminalError(ConfigError):
    """There is no terminal and no answers file."""


def build_runtime(
    config_dir: Path | None, runs_dir: Path, memory_dir: Path | None, use_memory: bool
) -> Runtime:
    """Wire the real model, response cache, hooks and memory directory."""
    config = load_config(config_dir or default_config_dir())
    packs = load_packs(config_dir or default_config_dir())
    transport = make_transport(config)
    runs_dir.mkdir(parents=True, exist_ok=True)
    cache_path = runs_dir / "response_cache.sqlite"
    cache = SqliteCache(cache_path)
    cache_path.chmod(0o600)
    client = StructuredClient(transport, config.models, cache)
    prompts_dir = (config_dir or default_config_dir()).parent / "prompts"
    deps = StageDeps(client, config, packs, prompts_dir)
    pipeline = build_pipeline(config.hooks, default_registry())
    runtime = Runtime(deps, pipeline, runs_dir, memory_dir if use_memory else None)
    runtime.on_close.append(cache.close)
    return runtime


def default_runs_dir(given: Path | None) -> Path:
    """Return the runs directory from the option, the environment or ./runs."""
    return given or Path(os.environ.get("STORY_AGENT_RUNS_DIR", "runs"))


def default_memory_dir(given: Path | None) -> Path:
    """Return the memory directory from the option, the environment or ./memory."""
    return given or Path(os.environ.get("STORY_AGENT_MEMORY_DIR", "memory"))


def summarize(outcome: RunOutcome, stopped: str, runs_dir: Path) -> tuple[list[str], int]:
    """Return the lines to print and the exit code for an outcome."""
    rid = outcome.run_id
    if outcome.status == "refused":
        return [outcome.message], EXIT_STOPPED
    if outcome.status == "blocked":
        return [f"The run was stopped: {outcome.message}", f"Run id: {rid}"], EXIT_STOPPED
    if outcome.status == "paused":
        kind = outcome.pending["kind"] if outcome.pending else "input"
        reason = stopped or f"waiting for {kind}"
        return [
            f"Paused: {reason}.",
            f"Your progress is saved. Continue with: story-agent resume {rid}",
        ], EXIT_PAUSED
    state = outcome.state
    stories = state.stories if state else []
    approved = sum(1 for s in stories if s.status.value == "approved")
    rejected = sum(1 for s in stories if s.status.value == "rejected")
    lines = [
        f"Run {rid} finished: {approved} stories approved, {rejected} rejected.",
        f"Saved to {runs_dir / rid / 'state.json'}",
    ]
    if outcome.memory_report is not None:
        saved = len(outcome.memory_report["saved"]) + len(outcome.memory_report["refreshed"])
        lines.append(
            f"Memory: {saved} entries saved, {len(outcome.memory_report['rejected'])} not saved."
        )
    return lines, EXIT_OK
