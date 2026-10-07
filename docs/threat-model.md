# Threat model

This covers story-agent as it is built today: a local command line tool that sends redacted
scenario text to a hosted model and writes stories to local files. It is aligned with the
OWASP Top 10 for LLM Applications (2025 edition) and uses MITRE ATLAS technique names where
one fits. Technique ids should be checked against the current ATLAS matrix before this is
used in a formal review. Nothing here is a certification or a compliance claim.

## What is being protected

| Asset | Why it matters |
|---|---|
| Scenario text, notes, answers | May describe a real bank's processes and contain customer data |
| Redaction map (`runs/<id>/redaction_map.json`) | Turns placeholders back into the original values |
| Run checkpoints (`runs/<id>/checkpoint.sqlite`) | Hold the scenario exactly as typed |
| Memory (`memory/<workspace>.db`) | Decisions kept between runs; must stay inside one workspace |
| Published stories | Go into a backlog and are acted on |
| The API key and tool tokens | Spend money and write to other systems |
| Prompts and config | Define the rules the guardrails enforce |

## Trust boundaries and untrusted input

1. **User to agent.** The scenario, notes, answers, free text, review edits and memory
   entries are all untrusted. They are data, never instructions.
2. **Agent to model.** Only redacted text crosses this line, wrapped in delimiters. The model's
   reply is untrusted until it has been validated.
3. **Agent to disk.** Local files are trusted only as far as the operating system protects them
   (mode 0700 and 0600).
4. **Agent to other systems.** Azure DevOps, Jira and telemetry sinks. Nothing is sent without a
   person's approval, and the telemetry sinks that need details are stubs.
5. **Supply chain.** Dependencies, the lockfile and the model provider.

## Threats and controls (OWASP LLM Top 10, 2025)

| Id | Risk | How it can happen here | Controls | Evidence | Residual risk |
|---|---|---|---|---|---|
| LLM01 | Prompt injection (ATLAS AML.T0051, AML.T0054) | Instructions hidden in the scenario, notes, an answer, a review edit or a remembered entry | Heuristic detector and quarantine, delimiters, a rule in every prompt that embedded instructions are never followed, scope guard, fixed refusal, injection scan on memory writes | `guardrails/injection.py`, `guardrails/scope.py`, `memory/guard.py`, `tests/unit/guardrails/`, injection and scope component evals, planted attacks in the app eval | A new phrasing can get past a heuristic. The model-side rule and grounding limit what a missed attack can do |
| LLM02 | Sensitive information disclosure (ATLAS AML.T0057) | Card, account or id numbers, emails or secrets reach the model, a log, memory, a file or telemetry | Redaction before every model call with a local mapping, PII check on every output, memory write guard, publish scan against the redaction map, trace holds ids and counts only | `guardrails/redaction.py`, `hooks/post/checks.py`, `memory/guard.py`, `publish/safety.py`, `evals/online/trace.py`, hard-fail metrics `pii_leaks_to_model`, `pii_in_output`, `pii_persisted` | Pattern based, so a value in an unusual format can slip through. The checkpoint and the redaction map hold originals on disk by design |
| LLM03 | Supply chain | A bad dependency, a tampered lockfile, a changed model | Lockfile with `uv sync --locked`, pip-audit, licence check, SBOM, gitleaks, models named only in config | `.github/workflows/ci.yml`, `uv.lock` | A compromised upstream release that audits do not know yet |
| LLM04 | Data and model poisoning | Poisoned memory entries or a poisoned few-shot example changing later runs | Memory only from approved entries, write guard, per-workspace isolation, TTL and re-confirmation, prompt examples validated against schemas, prompts versioned and hashed | `memory/proposals.py`, `memory/store.py`, `tests/unit/test_prompt_skeleton.py`, `prompts/CHANGELOG.md` | A user approving a bad entry. We do not train or fine-tune a model |
| LLM05 | Improper output handling | Model output used as a command, a formula, markup or a query | Output validated against a schema, ids and provenance set by code, spreadsheet formula neutralising, HTML and wiki escaping, no model output is executed | `llm.py`, `publish/adocsv.py`, `publish/html.py` | Rich text rendered in a destination tool that has its own flaws |
| LLM06 | Excessive agency | The agent acts on another system or drafts without being allowed to | Go-ahead only at the graph's gate, approval bound to the exact payload before an external write, no HTTP client shipped for REST targets, memory writes default deny, non-interactive runs only with `--answers` | `graph.py`, `publish/base.py`, `runcmd.py`, `tests/unit/test_graph.py`, `tests/unit/publish/` | An answers file that approves everything is the user's explicit choice |
| LLM07 | System prompt leakage | A request to reveal prompts or stored memory | Scope guard refuses it, prompts hold no secrets or customer data, memory is never dumped into a reply | `guardrails/scope.py`, `prompts/` | The prompts are in the repository, so they are not secret. They are written so that they do not need to be |
| LLM08 | Vector and embedding weaknesses | Not applicable: recall is keyword and fixed ordering over SQLite FTS5, with no embeddings | Deterministic recall scoped by workspace, domain and tags | `memory/recall.py` | Revisit if embeddings are added (off by default in the design) |
| LLM09 | Misinformation | Invented requirements, numbers or sources in stories | Requirements come from stated or confirmed items only, provenance on every story element, grounding check, number grounding, an assumption must trace to a judgment the user gave | `guardrails/grounding.py`, `pipeline/requirements.py`, `pipeline/numbers.py`, groundedness and hallucination evals | Wording can still be wrong while being grounded. A person reviews every story |
| LLM10 | Unbounded consumption | A long scenario, a loop of revisions, repeated runs | Input size limits, token, cost and step budgets, at most 6 questions, 3 rounds and 2 revision loops, retry only on transient errors | `hooks/pre/guards.py`, `config/guardrails.yaml`, `clarify/limits.py` | Many separate runs are not limited by the tool |

## Other threats

| Threat | Control | Evidence |
|---|---|---|
| Cross-workspace leakage of memory | One database per workspace, workspace in every query, a hard-fail eval | `memory/store.py`, memory isolation eval |
| Someone else reads checkpoints, the cache or exports | Run folders 0700, files 0600, a private response cache | `session.py`, `runcmd.py`, `tests/unit/test_graph.py` |
| A resumed run skips the gate | The gate is the only place `go_ahead` is set, checked by a test that scans the source | `tests/unit/test_graph.py` |
| Spreadsheet formula injection in a CSV | Cells starting with `=`, `+`, `-` or `@` get a leading quote | `publish/adocsv.py`, `tests/unit/publish/` |
| WIQL or JQL injection through a label | The label prefix is validated and quotes are escaped | `publish/config.py`, `publish/adorest.py` |
| Duplicate work items on a re-run | Idempotency label looked up before create | `publish/base.py` |
| Telemetry leaks content | The trace schema allows ids, hashes, counts and numbers, strings are capped and scanned | `evals/online/trace.py`, `tests/unit/evals/online/test_online.py` |
| Telemetry failure stops a run | Observers fail open | `session.py` |
| A guardrail hook errors and lets data through | Guardrail hooks fail closed | `config/hooks.yaml`, `hooks/registry.py` |

## Not covered

- A hostile local user on the same machine with the same account.
- Compromise of the model provider or of the machine the tool runs on.
- Regulatory advice. The banking pack is a checklist for asking questions, not legal guidance.

## Review

Update this file when a boundary changes: a new sink, a real REST client, embeddings, or a
hosted service. The risk register lists what is open.
