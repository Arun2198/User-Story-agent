# Risk register

Risks to the quality, safety and use of story-agent, organised with the four functions of the
NIST AI Risk Management Framework (Govern, Map, Measure, Manage) in mind. This is aligned with
that framework as a way of thinking. It is not an assessment against it, and nothing here is a
claim of compliance.

Owner for every item is Arun. Likelihood and impact are my own estimate on a three step scale
(low, medium, high) for a single user running the tool locally. "Residual" is what I expect to
remain after the controls.

## How the framework maps to the project

| Function | What it means here | Where |
|---|---|---|
| Govern | Decisions are written down, standards are checked in CI, people approve what matters | `docs/adr/`, `STANDARDS.md`, `.github/workflows/ci.yml` |
| Map | Intended use, users and limits are stated, and inputs are classed as untrusted | `docs/model-card.md`, `docs/threat-model.md` |
| Measure | Component, app, memory, stability and online evals, with thresholds and baselines | `config/evals.yaml`, `src/story_agent/evals/` |
| Manage | Guardrails, gates, approvals, budgets and a plan for each open risk below | this file |

## Register

| Id | Risk | Function | Likelihood | Impact | Controls | Residual | Status |
|---|---|---|---|---|---|---|---|
| R01 | Personal or secret data reaches the model, a log, memory or an export | Manage | medium | high | Redaction before every call, PII check on outputs, memory write guard, publish scan, hard-fail evals | low, pattern based so unusual formats can slip | controlled |
| R02 | A prompt injection changes behaviour or extracts content | Manage | medium | high | Detector and quarantine, delimiters, scope guard, injection scan on memory writes, planted attacks in the evals | medium, heuristics miss new phrasings | controlled, watch |
| R03 | A story contains a requirement, number or source nobody gave | Measure | medium | high | Requirements from stated or confirmed items only, provenance and number grounding, human review | low, wording can still be off | controlled |
| R04 | A stale or wrong remembered answer is applied | Manage | medium | medium | Always asked, never assumed. TTL and stale label, conflict prompt, default deny on writes | low | controlled |
| R05 | Memory from one workspace shows up in another | Manage | low | high | One database per workspace, hard-fail isolation eval | low | controlled |
| R06 | Results vary between runs because the 5.x models take no temperature | Measure | high | medium | Cache, canonical ordering, deterministic ids, stability eval with live thresholds (ADR 0002) | medium, inherent | accepted |
| R07 | The evals pass but the real model does worse (synthetic cases and a scripted offline model written by one person) | Measure | high | medium | Live mode, judge scoring, holdout split, mutation tests, drift check once live baselines exist | medium | open: no live baseline yet |
| R08 | Clarification is long or tiring (banking checklists reach 18 questions) | Map | medium | medium | At most 6 per round and 3 rounds, memory defaults, "use your judgment" and defer | medium | open |
| R09 | A model or prompt change quietly lowers quality | Manage | medium | medium | Prompt hashes and versions, change log, baselines, rule to record an eval comparison in the model card | low | controlled |
| R10 | Cost or token use runs away | Manage | low | medium | Per-run token, cost and step budgets, response cache, size limits | low | controlled |
| R11 | An external write creates wrong or duplicate items | Manage | low | high | Approval bound to the exact payload, dry run, idempotency labels, no REST client shipped yet | low | controlled |
| R12 | Telemetry carries content out of the machine | Manage | low | high | Trace schema allows ids, hashes, counts only, scan before send, sinks off by default | low | controlled |
| R13 | Reviewers approve without reading (`review: approve_all`, fatigue) | Govern | medium | medium | Approve-all is opt-in per run, edits are logged with edit distance, rejection and edit rates are tracked online | medium | accepted |
| R14 | The checkpoint and redaction map hold original values on disk | Manage | medium | medium | Mode 0700 and 0600, local only, documented | medium | accepted for local use, revisit for hosting |
| R15 | The domain packs are wrong or out of date for a regulation or a payment rail | Map | medium | medium | Packs are checklists that ask questions, not rules. Changes go through review and the pack validator | medium | open: needs a domain reviewer |
| R16 | A vulnerable or malicious dependency | Govern | low | high | Lockfile, pip-audit, licence check, SBOM, gitleaks | low | controlled |
| R17 | The REST publishers are plans only and untested against a real tenant | Map | high | low | Stated in the README and the ADR, CSV import works today, a fake system tests the logic | low | open |
| R18 | Online evaluation sinks are stubs, so live monitoring is not running | Measure | high | medium | Local JSONL sink works, stubs refuse at start-up and say what is needed | medium | open: pending details |
| R19 | Banking and India-specific content reflects my own assumptions | Map | medium | medium | Say so in the model card, keep packs as data, invite correction | medium | open |
| R20 | Prompt examples teach the model a bad habit | Manage | low | medium | Few-shot outputs are validated against their schemas and run through the evals | low | controlled |

## Open items and what closes them

| Item | Closes when |
|---|---|
| R07 | A live run of the evals with a key, saved as the live baseline, and `story-agent online drift` run against real usage |
| R08 | Live use shows how many questions people actually answer. Tune the checklists if the rate of "other" or "use your judgment" is high |
| R15, R19 | Someone who works in banking reviews the banking pack and the India rails sub-pack |
| R17 | A real client is written and tried against a test tenant |
| R18 | I choose a destination and share its details (see ADR 0011) |

## Review

Review at every release, whenever a model or prompt changes, and whenever a trust boundary in
the threat model changes.
