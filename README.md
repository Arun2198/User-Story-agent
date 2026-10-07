# story-agent

Turns a plain-language scenario into traceable user stories with acceptance
criteria. It discovers what is missing, asks clarification questions, remembers
confirmed answers per workspace, and works for any industry through YAML domain
packs. Banking is the first pack.

Status: phase 3 (memory). The remaining
stages arrive in later phases; see `STANDARDS.md` for what is enforced.

## Setup

```bash
uv sync
cp .env.example .env   # then set ANTHROPIC_API_KEY in your shell
uv run pytest
```

## Configuration

All settings are in `config/`. Model names are in `config/models.yaml` only.
Secrets come from environment variables, never from files.

## Determinism

Determinism is best-effort. The Claude 5.x models do not accept a temperature
setting (see `docs/adr/0002-structured-output-and-sampling.md`), so repeatability
comes from the response cache, deterministic IDs and ordering, and checklist-driven
discovery. A stability test (N repeated runs) will report variance in discovered
items, questions asked and stories produced.

## Development

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest --cov
pre-commit install
```

## Adding a domain pack

A pack is one YAML file in `config/domains/`. No code changes.

1. Copy `config/domains/banking.yaml` as a starting point. The file name must equal `id`.
2. Set `extends: generic` to inherit the generic categories. A category with the same `id`
   replaces the inherited one.
3. Add `hints` (words that suggest the domain, with optional `weight`), `subdomains` (each
   with its own hints), optional `sub_packs` (extra categories that switch on when their
   hints match) and `actors`.
4. Add `categories`. Each has `probes` (what to ask about), `weight` (1 to 5), optional
   `typical_options`, `applies_to` (sub-domains, empty means all) and `must_have` or
   `must_have_in` (sub-domains where it must be settled before drafting).
5. Run `uv run pytest tests/unit/discovery`. The loader rejects unknown references,
   duplicate ids and extends cycles.

## Adding an ingestor

Implement `story_agent.intake.ingestor.Ingestor` (`ingest(source, notes=..., workspace=...)`
returning a `Scenario`). Only plain text (`TextIngestor`) is built. Ingested text goes
through the same redaction and injection hooks as typed text.

## Memory

Memory makes repeat scenarios sharper: when a similar scenario comes up, remembered answers
appear as defaults ("Previously you said X ... still valid? yes / edit / no"). You are still
asked, and nothing is saved without your approval.

- Storage is `memory/<workspace>.db` (SQLite with FTS5), one file per workspace. Pass
  `--workspace` (default `default`). Set the directory with `--memory-dir` or
  `STORY_AGENT_MEMORY_DIR`.
- Entries are `confirmed_answer`, `decision`, `glossary`, `nfr_default` or `preference`.
  PII, secrets, instruction-like text and copied scenario text are refused on write.
- Entries past `ttl_days` are marked stale and need re-confirming. Settings are in
  `config/memory.yaml`.

```bash
story-agent memory list --workspace acme
story-agent memory show M-1a2b3c4d5e --workspace acme
story-agent memory edit M-1a2b3c4d5e --content "Retain 7 years" --workspace acme
story-agent memory delete M-1a2b3c4d5e --workspace acme
story-agent memory export --workspace acme --out acme-memory.json
story-agent memory clear --workspace acme
```

The CLI reads `config/` from the current directory, or from `STORY_AGENT_CONFIG_DIR`.
