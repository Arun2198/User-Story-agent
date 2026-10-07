# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- `scripts/setup_keys.sh` saves the NVIDIA key privately and loads them, for machines where Codespaces secrets do not arrive.
- The judge model is `nvidia/nemotron-3-ultra-550b-a55b`. One key (`NVIDIA_API_KEY`) is used for every model. Token limits are off by default and thinking is left on. `story-agent models` lists live model ids; `story-agent check` times each model.
- The model provider is now NVIDIA's hosted API through `NvidiaTransport`. The Anthropic transport, SDK dependency and key variable are removed. The key is `NVIDIA_API_KEY`; models, base URL and structured-output mode are in `config/models.yaml` (ADR 0012).

### Added
- Online evaluation (off by default): `OnlineEvaluator` interface, OpenTelemetry-compatible trace schema with OTLP/JSON export, feedback signals, sampled judge scoring, drift check against a baseline, a local JSONL sink and pending stubs for OTLP, Langfuse and a warehouse.
- `story-agent online trace|feedback|drift`.
- Run state keeps `memory_outcome`; stage usage records `latency_s`.
- `docs/threat-model.md`, `docs/risk-register.md`, `docs/model-card.md`, ADR 0011.
- Publishers: markdown, JSON and Azure DevOps CSV import files; request plans with field mapping and `--dry-run` for Azure DevOps and Jira REST; `story-agent publish`; `--format` and `--out` on `run`.
- Approval gate for external writes, bound to the exact payload, and idempotency labels for re-runs.
- Publish checks: no unreviewed stories, grounding re-checked, no PII or redacted values in output, spreadsheet formula neutralising.
- `config/destinations.yaml` and `config/templates/`.
- `skill/scenario-to-stories/SKILL.md` and tests that keep it in step with the config.
- Run graph (LangGraph, SQLite checkpoints) that pauses for answers, memory conflicts, the readiness gate, story review and memory approval.
- `story-agent run` and `story-agent resume`, with terminal prompts or an `--answers` file; exit codes for paused, refused and stopped runs.
- Answers file settings for unanswered questions, conflicts, review and memory.
- `Flow.restore`, so a stage can run from saved state in a new process.
- End-to-end evals: 14 synthetic banking and generic cases with hidden answer keys, a simulated user, an offline scripted model, metrics for discovery, stories, guardrails, preferences and cost.
- Memory app eval (off then on per case, conflicts, stale entries, isolation) and stability eval (repeat runs).
- Eval runner with baselines, regression tolerance and exit codes; `story-agent evals run` and `evals add-case`.
- Scenario synthesiser and LLM judge prompts (live mode only).
- `Flow`, the stage sequence with hooks around each step, shared by the evals and the later graph.
- CI `evals` job.
- Drafting: requirement derivation, `draft`, `criteria` and `critique` stages and prompts, a revision loop (at most two), deterministic ids, ordering and number grounding.
- `human_review`: approve, edit or reject with sanitised edits and an edit log; persona glossary proposals for memory.
- Deduplication, id stability and coverage post-hooks.
- `critic_checks` and `criteria_checks` component evals.
- Memory: workspace-scoped SQLite store with FTS5, write guard, deterministic recall, conflict detection, write proposals with per-entry approval, and `story-agent memory` commands.
- Memory evals: retrieval, staleness, contradiction handling, safety, isolation and effect, with hard-fail metrics.
- `memory_proposal` post-hook.
- Domain packs: generic and banking (with an optional India rails sub-pack), loader and validator, deterministic domain detection.
- `discover` stage and prompt: checklist coverage, grounded `stated` items, canonical ids.
- `clarify` stage and prompt: ranked questions, rounds, answers, free text and whitelisted preferences, readiness summary and go-ahead gate.
- `domain_detection` component eval.
- Plain-text ingestor and the `Ingestor` interface.
- Hook framework: protocol, registry, ordered pipeline from `hooks.yaml`, fail-closed and fail-open handling.
- Guardrails: PII and secret redaction, prompt-injection detection with quarantine, grounding verifier, scope guard.
- Pre and post hooks for input size, schema version, scope, redaction, injection, budget, prompt recording, schema validation, grounding, PII leak, usage accounting, metrics and trace.
- `scope_check` stage and prompt.
- Component evals for redaction, injection and the scope guard, with labelled dev and holdout splits.
- Project scaffold with src layout, uv lockfile and tool configuration.
- Pydantic schemas with `schema_version`.
- Config loading from YAML, with secrets read from the environment only.
- LLM wrapper: structured output, one repair retry, fail closed, backoff with jitter on
  transient errors, response cache and a fake transport for tests.
- CI workflow, pre-commit config and a Conventional Commits check.
