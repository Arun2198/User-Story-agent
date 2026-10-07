# story-agent

Turns a plain-language scenario into traceable user stories with acceptance
criteria. It discovers what is missing, asks clarification questions, remembers
confirmed answers per workspace, and works for any industry through YAML domain
packs. Banking is the first pack.

Status: phase 1 (hooks, guardrails and their component evals). The remaining
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
