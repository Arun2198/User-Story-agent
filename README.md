# story-agent

Turns a plain-language scenario into traceable user stories with acceptance
criteria. It discovers what is missing, asks clarification questions, remembers
confirmed answers per workspace, and works for any industry through YAML domain
packs. Banking is the first pack.

Status: phase 2 (domain packs, discover and clarify). The remaining
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
