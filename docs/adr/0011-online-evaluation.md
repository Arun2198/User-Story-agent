# 0011. Online evaluation, trace schema and pending sinks

- Status: accepted
- Date: 2026-10-07

## Context

The offline evals use a scripted model and synthetic cases. To know how real runs go, I want
signals from real use: what people approve, edit and reject, how often they type "other", which
memory entries they refuse, and whether quality drifts from a baseline. I have not chosen where
telemetry should go, so the destination is open.

## Decision

- **Off by default.** `online.enabled` is false in `config/evals.yaml`. Enabled with no sink is a
  config error. A run behaves the same either way.
- **A small interface.** `OnlineEvaluator.on_run_finished(state, runs_dir)` is called once when a
  run finishes (not when it pauses). The session calls it and treats any failure as non-fatal and
  logs only the exception class, like the other observers.
- **An OpenTelemetry-compatible trace.** `evals/online/trace.py` follows the OTLP data model:
  trace and span ids, nanosecond times, kind, status, attributes, events and a resource. Model
  calls use `gen_ai.*` attribute names and the rest use `story_agent.*`. Ids are derived from the
  run id, so the same run always gives the same trace. `to_otlp` produces OTLP/JSON. No
  OpenTelemetry package is needed.
- **No content in telemetry.** The builder reads ids, hashes, counts and numbers only. Names are
  checked, strings are capped at 200 characters, and every string is scanned for sensitive values
  and for anything in the run's redaction map before a record is made. The workspace appears as a
  short hash.
- **Feedback signals.** Counts and rates of approved, edited and rejected stories, mean edit
  distance, answers typed as "other", forced judgments, rejected defaults, and memory entries
  saved or rejected. To count the last one, run state now keeps `memory_outcome`. Metric names
  shared with the offline app eval use the same names.
- **Sampled judging.** Runs are chosen by a hash of the run id, so a sample is repeatable. The
  judge only runs when it is enabled, a sample rate is set and a live client exists.
- **Drift check.** `story-agent online drift` compares the mean of recorded metrics with a
  baseline, per metric, using an absolute or a relative tolerance from config. It skips the
  check when there are too few runs. It compares with the live baseline by default, because the
  offline baseline comes from a scripted model.
- **One working sink and three stubs.** `jsonl` writes a private local file and skips a run it
  already has. `otlp_http`, `langfuse` and `warehouse` are stubs. Choosing one fails at start-up
  and prints what is needed. They are pending my input.

## Pending input

| Sink | Needed |
|---|---|
| `otlp_http` | Collector endpoint, the environment variable holding the auth header, extra resource attributes, batching |
| `langfuse` | Host, the environment variables for the public and secret keys, which metrics become scores |
| `warehouse` | System, table and schema, the environment variable for credentials, retention |

## Consequences

- Nothing leaves the machine until a real sink is written and chosen.
- A drift check is only meaningful after a live baseline exists.
- The trace is rebuilt from `trace.jsonl` and run state, so old runs can be exported later.
