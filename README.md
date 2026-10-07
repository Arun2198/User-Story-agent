# story-agent

Turns a plain-language scenario into traceable user stories with acceptance
criteria. It discovers what is missing, asks clarification questions, remembers
confirmed answers per workspace, and works for any industry through YAML domain
packs. Banking is the first pack.

Status: phase 4 (draft, criteria, critique, review). The remaining
stages arrive in later phases; see `STANDARDS.md` for what is enforced.

New here? Read [docs/getting-started.md](docs/getting-started.md) first.

## Setup

```bash
uv sync
cp .env.example .env   # then set ANTHROPIC_API_KEY in your shell
uv run pytest
```

## Running

```bash
story-agent run "A customer disputes a card payment and expects a temporary credit." \
    --workspace acme
story-agent resume run-20261007-101500-3fa2
```

`run` asks clarifying questions in the terminal (at most 6 per round, 3 rounds), shows a
readiness summary and waits for your go-ahead before drafting. You then approve, edit or
reject each story, and approve each memory entry you want kept. Ctrl-C at any prompt
leaves the run paused and saved; `resume` carries on from the last finished stage.

Runs are saved in `runs/<run_id>/` (`--runs-dir` or `STORY_AGENT_RUNS_DIR`). The checkpoint
holds the scenario as you typed it, so the folder is private to you (mode 0700, files 0600).
The finished run is written to `runs/<run_id>/state.json`.

### Without a terminal

A run with no terminal must be given an answers file. Without one, `run` and `resume` exit
with code 2 before any model call. The file answers only what it says; anything missing
stops the run (exit code 3) so you can `resume` it later.

```yaml
# answers.yaml
answers:                       # question id or category id: the reply you would type
  dispute_handling: "Within 10 business days"
  limits_velocity: "yes"       # "yes" confirms a remembered answer
free_text: "At most 4 criteria per story."
go_ahead: true                 # required, or the run stops at the readiness summary
unanswered: judgment           # stop (default) | judgment | defer
on_conflict: replace           # stop (default) | replace | exception
review: approve_all            # none (default) | approve_all | a list of actions
memory: none                   # none (default) | approve_all
```

`unanswered: judgment` records "use your judgment" as an explicit assumption on the stories
that rely on it. `review: none` leaves stories waiting for you.

| Exit code | Meaning |
|---|---|
| 0 | finished |
| 1 | error (bad config, bad file, unknown run, missing API key) |
| 2 | no terminal and no `--answers` |
| 3 | paused: waiting for input; run `resume` |
| 4 | refused by scope, or stopped by a guardrail |

## Publishing

```bash
story-agent publish run-20261007-101500-3fa2 --target md          # runs/<id>/stories.md
story-agent publish run-20261007-101500-3fa2 --target json --out stories.json
story-agent publish run-20261007-101500-3fa2 --target ado_csv --out import.csv
story-agent run "..." --format csv --out import.csv               # publish when the run ends
story-agent publish run-20261007-101500-3fa2 --target ado_rest --dry-run
```

| Target | State |
|---|---|
| `md`, `json` | complete |
| `ado_csv` | complete: Azure DevOps import file, Epic > Feature > Story through the Title 1, 2, 3 columns |
| `ado_rest`, `jira_rest` | request plan and field mapping with `--dry-run`; **writing is not enabled in this build** (no HTTP client ships) |

Only approved and edited stories are published. A run with stories still waiting for review,
or with an approved story that fails the grounding check, is refused. Output that repeats a
value that was redacted from your input, or that holds sensitive values, is refused.

Settings are in `config/destinations.yaml`: label names, MoSCoW to priority maps, the story
points field, the ADO process (`agile`, `scrum` or `cmmi`) and the Jira field names. Description
layouts are in `config/templates/` (HTML for Azure DevOps, wiki markup for Jira, markdown).
Tokens are never stored there; they come from the environment variables it names.

- Every story and epic carries a label `sa-<12 hex>` made from the workspace, the scenario and
  the story's identity. An external publish looks the item up by that label and updates it,
  so running it twice does not duplicate. A CSV import cannot update, so re-importing a CSV
  creates new items.
- ADO needs a feature between epic and story, so stories with no feature go under
  `ado.default_feature` (`General`). Jira has no feature level, so the feature becomes a label.
- Cells that would run as a spreadsheet formula (starting with `=`, `+`, `-`, `@`) are prefixed
  with `'` in the CSV.
- **Approval before any external write.** A write needs a person to see the exact payload and
  confirm at a terminal. The approval is for that payload only (it carries its hash), and there
  is no flag that skips it. Without a terminal, use `--dry-run`.

## Online evaluation

Off by default (`online` in `config/evals.yaml`). It records how finished runs go without
changing them.

```bash
story-agent online trace run-20261007-101500-3fa2 --otlp   # OpenTelemetry-compatible trace
story-agent online feedback run-20261007-101500-3fa2       # approve/edit/reject, edit distance, ...
story-agent online drift                                   # compare recorded runs with a baseline
```

To record runs locally, set `online.enabled: true` and `online.sink: jsonl`; records go to
`runs/online/records.jsonl` (private). Traces hold ids, hashes, counts and numbers only. The
`otlp_http`, `langfuse` and `warehouse` sinks are stubs that are **pending my input**: choosing
one stops at start-up and says what is needed. Drift compares with the live baseline, so run
`story-agent evals run --live --update-baseline` once first.

## Skill

`skill/scenario-to-stories/SKILL.md` is the same workflow for use without code. It points at
the same `prompts/`, `config/domains/` and `config/standards.yaml`, and tests check that its
limits and refusal message still match the config.

## Configuration

All settings are in `config/`. Model names are in `config/models.yaml` only.
Secrets come from environment variables, never from files.

## Determinism

Determinism is best-effort. The Claude 5.x models do not accept a temperature
setting (see `docs/adr/0002-structured-output-and-sampling.md`), so repeatability
comes from the response cache, deterministic IDs and ordering, and checklist-driven
discovery. A stability test (N repeated runs) will report variance in discovered
items, questions asked and stories produced.

## Development

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest --cov
pre-commit install
```

## Adding a domain pack

A pack is one YAML file in `config/domains/`. No code changes.

1. Copy `config/domains/banking.yaml` as a starting point. The file name must equal `id`.
2. Set `extends: generic` to inherit the generic categories. A category with the same `id`
   replaces the inherited one.
3. Add `hints` (words that suggest the domain, with optional `weight`), `subdomains` (each
   with its own hints), optional `sub_packs` (extra categories that switch on when their
   hints match) and `actors`.
4. Add `categories`. Each has `probes` (what to ask about), `weight` (1 to 5), optional
   `typical_options`, `applies_to` (sub-domains, empty means all) and `must_have` or
   `must_have_in` (sub-domains where it must be settled before drafting).
5. Run `uv run pytest tests/unit/discovery`. The loader rejects unknown references,
   duplicate ids and extends cycles.

## Adding an ingestor

Implement `story_agent.intake.ingestor.Ingestor` (`ingest(source, notes=..., workspace=...)`
returning a `Scenario`). Only plain text (`TextIngestor`) is built. Ingested text goes
through the same redaction and injection hooks as typed text.

## Adding a publisher

A file publisher is a class with `name`, `extension` and `render(view, ctx) -> str`
(see `publish/markdown.py`). It reads the `PublishView` from `publish/view.py`, which already
holds the approved stories, labels, provenance lines and idempotency keys. Register it in
`FILE_TARGETS` in `publish/service.py`, add a snapshot test with `check_snapshot` and run
`UPDATE_SNAPSHOTS=1 pytest tests/unit/publish` once to create the snapshot, then read it.

An external system implements `plan(view, ctx) -> Plan` plus a few small methods that read its
responses (see `publish/adorest.py`). It never sends anything: `apply_plan` does, and only with
an `Approval` for that exact plan.

## Memory

Memory makes repeat scenarios sharper: when a similar scenario comes up, remembered answers
appear as defaults ("Previously you said X ... still valid? yes / edit / no"). You are still
asked, and nothing is saved without your approval.

- Storage is `memory/<workspace>.db` (SQLite with FTS5), one file per workspace. Pass
  `--workspace` (default `default`). Set the directory with `--memory-dir` or
  `STORY_AGENT_MEMORY_DIR`.
- Entries are `confirmed_answer`, `decision`, `glossary`, `nfr_default` or `preference`.
  PII, secrets, instruction-like text and copied scenario text are refused on write.
- Entries past `ttl_days` are marked stale and need re-confirming. Settings are in
  `config/memory.yaml`.

```bash
story-agent memory list --workspace acme
story-agent memory show M-1a2b3c4d5e --workspace acme
story-agent memory edit M-1a2b3c4d5e --content "Retain 7 years" --workspace acme
story-agent memory delete M-1a2b3c4d5e --workspace acme
story-agent memory export --workspace acme --out acme-memory.json
story-agent memory clear --workspace acme
```

The CLI reads `config/` from the current directory, or from `STORY_AGENT_CONFIG_DIR`.

## Evals

```bash
story-agent evals run                      # everything, offline
story-agent evals run --component redaction
story-agent evals run --app --cases bk-card-dispute
story-agent evals run --stability 5 --live # needs ANTHROPIC_API_KEY
story-agent evals add-case my-case.json    # validates, then stores the case
story-agent evals run --update-baseline    # after an intended change
```

Reports go to `eval-reports/`. Exit code 0 is ok, 1 a threshold miss or regression against
the baseline, 2 a hard failure (a PII or injection leak). Thresholds are in
`config/evals.yaml`.

Offline runs use a scripted model driven by the case's gold labels. They test the plumbing
and the guardrails, not model quality; only `--live` measures the real model. The cases are
synthetic and written by hand. A simulated user answers from each case's hidden answer key.

## How stories are built

1. Stated and confirmed discovery items become numbered requirements. Nothing the user has
   not confirmed becomes a requirement.
2. The model groups requirements into epics and writes stories that cite them. Code
   assigns ids (`<PREFIX>-0001`), order, provenance, assumptions, NFRs and open questions.
3. The model writes Given/When/Then criteria. Code rejects invented numbers, flags vague
   wording and caps the count (`max_criteria_per_story` in `config/standards.yaml`, or your
   preference).
4. Critique checks coverage, duplicates, size, readiness and grounding in code, and asks
   the model for duplicates, contradictions and negotiability. Errors cause a revision, at
   most twice.
5. You approve, edit or reject each story. Edits are logged. Estimates and priorities are
   suggestions you can change.
