# 0004. Guardrail and hook design

- Status: accepted
- Date: 2026-10-07

## Context

Four guardrails are mandatory (redaction, injection defence, grounding, scope). They
must run around every component, fail closed, and be testable without a model.

## Decision

- Hooks implement one protocol and are ordered in `config/hooks.yaml`. Each entry says
  whether it runs once per run or around every component, and whether it fails closed
  or open. A guardrail that raises blocks the run. An observer that raises is logged.
- Redaction runs before injection scanning, so quarantined snippets never hold raw PII.
  One `Redactor` serves a run so placeholders stay stable. The mapping is saved to
  `runs/<run_id>/redaction_map.json` with mode 0600 and is never sent to a model.
- Injection handling quarantines the containing sentence, replaces it with a marker and
  records the text in `runs/<run_id>/quarantine.jsonl` and the findings in run state.
  Nothing is dropped silently. The detector is heuristic and will miss some attacks.
- The scope guard refuses a request only when it is mainly an attack, meaning less than
  40 characters of real text remain after quarantine. An otherwise real scenario with one
  planted sentence is processed, and that sentence is quarantined and reported. The model
  classifier then handles everything else out of scope.
- Every refusal uses one fixed message from `guardrails.yaml`, so refusals leak nothing
  about why.
- Grounding is deterministic. A memory reference counts only if the user confirmed that
  entry in this run. A domain suggestion counts only if the item is confirmed. An
  assumption needs a linked "use your judgment" answer.

## Consequences

Component evals measure the heuristics against labelled data with dev and holdout splits.
The first holdout results were seen while fixing bugs, so later cases must extend the
holdout rather than reuse it as blind data. The model-based scope classifier has no
offline eval yet and needs a live run.
