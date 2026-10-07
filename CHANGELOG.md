# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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
