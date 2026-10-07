# 0008. End-to-end evals and the simulated user

- Status: accepted
- Date: 2026-10-07

## Context

Component evals check one piece at a time. We also need to know that the whole flow
behaves: questions get asked, nothing private reaches the model, stories stay grounded.
Live runs cost money and vary, so CI needs an offline check.

## Decision

- **Cases carry a hidden answer key.** Each case has gold items, planted ambiguities,
  planted PII and injections, and a memory plan. The simulated user answers only from the
  key and says "yes" to unchanged remembered defaults.
- **Offline uses a scripted model.** `GoldModel` answers from the gold labels. It can be
  degraded (omit items, invent a number, leak PII) so tests show the evals can fail.
  Offline results prove the plumbing and guardrails, not quality.
- **Metrics are deterministic.** The judge model is optional and live only.
- **Thresholds and baselines.** `config/evals.yaml` holds bounds. Baselines are stored per
  mode and a metric worse by more than the tolerance is a regression. Leaks are hard
  failures with exit code 2.
- **Dev and holdout splits** for component datasets, to limit tuning to the test.

## Consequences

- Datasets and the gold model were written by one person, so they share blind spots.
- Banking checklists make long clarify sessions (up to 18 questions).
- Baselines must be regenerated deliberately after an intended change.
