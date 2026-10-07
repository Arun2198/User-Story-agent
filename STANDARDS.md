# Standards and where they are enforced

Status values: **enforced** (a check fails the build), **planned** (lands in the
phase shown), **manual** (a review step).

| Standard | Enforced by | Status |
|---|---|---|
| ruff lint with strict ruleset | `[tool.ruff]` in `pyproject.toml`; CI `quality`; pre-commit | enforced |
| ruff format | CI `quality` (`ruff format --check`); pre-commit | enforced |
| `mypy --strict` | `[tool.mypy]`; CI `quality`; pre-commit | enforced |
| PEP 257 docstrings on public code | ruff `D` rules | enforced |
| `noqa` / `type: ignore` need a reason | review; tests use inline comments | manual |
| src layout, SemVer, lockfile | `pyproject.toml`, `uv.lock`, `uv sync --locked` in CI, ADR 0003 | enforced |
| Keep a Changelog | `CHANGELOG.md` | manual |
| Protocol interfaces, DI, no global state | `LLMClient`, `Transport`, `Hook`, `Detector` so far; others as they land | partial |
| No prompt text in code; prompt skeleton | `prompts/`, `prompts.py`, `tests/unit/test_prompt_skeleton.py` | enforced |
| Secrets only from env | `config.get_api_key`; `.env.example`; gitleaks | enforced |
| Structured JSON logs with run_id and stage | `logging.py` | enforced |
| Timeouts, backoff with jitter, transient-only retry | `llm.py` (`retry_transient`, SDK timeout) | enforced |
| Token, cost and step budgets | `config/guardrails.yaml`; `BudgetHook` and `UsageAccountingHook` | enforced |
| Tests: unit, integration, e2e, fake LLM | `tests/`, `fake_llm.py` | partial |
| Hypothesis property tests | cache keys, redaction, injection quarantine, deterministic ids, preferences whitelist | enforced |
| Coverage 85% overall | `[tool.coverage.report] fail_under` | enforced |
| Coverage 95% on guardrails and hooks | CI `quality`, `coverage report --fail-under=95` on those paths | enforced |
| Offline evals as a regression gate | CI `evals` job, `config/evals.yaml`, baselines in `src/story_agent/evals/baselines/` | enforced |
| bandit, pip-audit, gitleaks | CI `security`; pre-commit (gitleaks) | enforced |
| License check, CycloneDX SBOM | CI `supply-chain` | enforced |
| Conventional Commits | pre-commit `commit-msg` hook | enforced locally |
| Threat model (OWASP LLM Top 10, MITRE ATLAS) | `docs/threat-model.md` | planned (phase 8) |
| Risk register (aligned with NIST AI RMF) | `docs/risk-register.md` | planned (phase 8) |
| Model card, prompt change log | `docs/model-card.md`, `prompts/CHANGELOG.md` | planned (phase 8) |
| Memory TTL, deletion, no PII, workspace isolation | `memory/` (`guard.py`, `store.py`), `config/memory.yaml`, memory CLI, `evals/components/memory.py` (hard-fail metrics `pii_persisted`, `cross_workspace_leaks`, `stale_misapplication_rate`) | enforced |
| Guardrails fail closed, observers fail open | `config/hooks.yaml`, `hooks/registry.py`, `tests/unit/hooks/test_hooks.py` | enforced |
| Component evals gate regressions | `config/evals.yaml`, `evals/components/`, `tests/unit/evals/` (redaction, injection, scope guard) | enforced |
| Packs are data; pack validation | `discovery/packs.py`, `tests/unit/discovery/test_packs.py` | enforced |
| Clarification gate (6 per round, 3 rounds, explicit go-ahead) | `clarify/`, `tests/unit/clarify/` | enforced |
| Non-interactive runs only with `--answers`; the go-ahead is set only at the graph's gate | `runcmd.choose_responder`, `graph.RunGraph.gate`, `tests/unit/test_run_cli.py`, `tests/unit/test_graph.py` | enforced |
| Runs can pause and resume from a private checkpoint | `session.py`, `runs/<id>/checkpoint.sqlite` (0600), `tests/unit/test_graph.py` | enforced |
| Every story grounded and traceable | `guardrails/grounding.py`, `pipeline/requirements.py`, `pipeline/numbers.py`, `GroundingHook`, `tests/unit/pipeline/` | enforced |
| Deterministic ids, ordering and ID stability | `pipeline/postprocess.py`, `IdStabilityHook`, Hypothesis tests | enforced |
| Prompts: few-shot outputs match their schemas | `tests/unit/test_prompt_skeleton.py` | enforced |
| ADR per significant decision | `docs/adr/` | manual |

External standards are referred to as "aligned with". This project makes no
certification or compliance claim.
