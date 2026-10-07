# 0013. Two providers, chosen at run time

- Status: accepted
- Date: 2026-10-07
- Amends: 0012

## Context

NVIDIA's hosted models work, but the large reasoning models are slow and their latency varies a
lot. I also want a faster paid option. The model call is behind the `Transport` interface, so the
pipeline does not need to change.

## Decision

- **Two transports.** `AnthropicTransport` (Messages API, JSON-schema output) and
  `NvidiaTransport` (ADR 0012). `providers.transport_for` returns the right one.
- **Choosing.** `provider: auto|anthropic|nvidia` in `config/models.yaml`. The environment variable
  `STORY_AGENT_PROVIDER` and the global `--provider` option override it. `auto` takes the first
  provider whose key variable is set (Anthropic first); with no key it resolves to Anthropic so
  offline work needs nothing.
- **Config layout.** Shared settings (timeout, retries, backoff, token limit) sit at the top;
  each provider block holds its key variable, generator, judge, prices and its own overrides.
- **Models.** Anthropic: `claude-sonnet-5-5` generates, `claude-opus-5-5` judges. NVIDIA: the
  strongest model verified to work, `nvidia/nemotron-3-ultra-550b-a55b`, for both roles until a
  different-family judge is picked from `story-agent models --best`.
- **Keys.** One variable per provider, read from the environment only.

## Consequences

- Switching provider is a flag, not a code change. Behaviour differs between providers, so the
  live baseline must be recorded per provider.
- Prices in config are nominal budget numbers. Check real prices before relying on the cost cap.
