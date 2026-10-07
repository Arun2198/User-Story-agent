# 0003. Packaging and tooling

- Status: accepted
- Date: 2026-10-07

## Context

The standards ask for a src layout, pinned dependencies with a lockfile, and strict
lint and type checks enforced in CI.

## Decision

- `pyproject.toml` with hatchling and a `src/` layout. Dependencies are declared with
  lower bounds and pinned exactly by `uv.lock`. CI installs with `uv sync --locked`.
- uv is a development and CI tool only, not a runtime dependency.
- ruff for lint and format, `mypy --strict` with the Pydantic plugin, pytest with
  coverage, Hypothesis for property tests.
- Config templates use the standard library, with `html.escape` for HTML output.
- HTTP uses `httpx2`, the client the Anthropic SDK 1.x already depends on.

## Consequences

One lockfile to review. Coverage is gated at 85% overall now. The 95% gate for
`guardrails/` and `hooks/` is enforced by a per-path check once those packages exist.
