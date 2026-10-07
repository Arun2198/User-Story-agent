# 0002. Structured output without forced tool choice; temperature from config

- Status: superseded by [0012](0012-nvidia-transport.md)
- Date: 2026-10-07

## Context

The brief asks for temperature 0 on every call and JSON-only structured output.
The Claude 5.x models (including the default `claude-sonnet-5-5`) reject
non-default sampling parameters and reject forced `tool_choice`. The installed
SDK does not expose `temperature` on `messages.create` for these models.

## Decision

- Structured output uses `output_config.format` with a JSON schema, and the reply
  is validated against the Pydantic model. One repair retry, then fail closed.
- `temperature` is read from `config/models.yaml` and sent only when set. It is
  `null` for the 5.x models and can be set to `0` for any model that accepts it.
- Determinism is therefore best-effort. The response cache, canonical ordering and
  deterministic IDs carry most of the weight. A stability test measures the rest.

## Consequences

Calls against 5.x models are not guaranteed identical across runs. README and the
model card state this. If a model that accepts `temperature` is configured, setting
it to 0 needs no code change.
