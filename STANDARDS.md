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
| Protocol interfaces, DI, no global state | `llm.py` (`LLMClient`, `Transport`) now; others as they land | partial |
| No prompt text in code | prompts live in `prompts/`; loaded by `prompts.py` | enforced from phase 1 |
| Secrets only from env | `config.get_api_key`; `.env.example`; gitleaks | enforced |
| Structured JSON logs with run_id and stage | `logging.py` | enforced |
| Timeouts, backoff with jitter, transient-only retry | `llm.py` (`retry_transient`, SDK timeout) | enforced |
| Token, cost and step budgets | `config/guardrails.yaml`; budget hook | planned (phase 1) |
| Tests: unit, integration, e2e, fake LLM | `tests/`, `fake_llm.py` | partial |
| Hypothesis property tests | `tests/unit/test_llm.py`; more with IDs, ordering, redaction | partial |
| Coverage 85% overall | `[tool.coverage.report] fail_under` | enforced |
| Coverage 95% on guardrails and hooks | per-path check in CI | planned (phase 1) |
| Offline evals as a regression gate | CI `evals` job, `config/evals.yaml` | planned (phase 5) |
| bandit, pip-audit, gitleaks | CI `security`; pre-commit (gitleaks) | enforced |
| License check, CycloneDX SBOM | CI `supply-chain` | enforced |
| Conventional Commits | pre-commit `commit-msg` hook | enforced locally |
| Threat model (OWASP LLM Top 10, MITRE ATLAS) | `docs/threat-model.md` | planned (phase 8) |
| Risk register (aligned with NIST AI RMF) | `docs/risk-register.md` | planned (phase 8) |
| Model card, prompt change log | `docs/model-card.md`, `prompts/CHANGELOG.md` | planned (phase 8) |
| Memory TTL, deletion, no PII, workspace isolation | `memory/` and its tests and evals | planned (phase 3) |
| ADR per significant decision | `docs/adr/` | manual |

External standards are referred to as "aligned with". This project makes no
certification or compliance claim.
