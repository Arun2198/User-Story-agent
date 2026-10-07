# 0010. Publishers, approval and idempotency

- Status: accepted
- Date: 2026-10-07

## Context

Finished stories must reach other tools without leaking private data, publishing anything
unreviewed or ungrounded, or writing to an external system without a person agreeing to it.

## Decision

- **One view for every publisher.** `build_view` turns a finished run into approved stories
  grouped by epic and feature, with provenance in words, labels and idempotency keys. No
  publisher reads run state directly, so they cannot disagree.
- **Checks come first, for every target.** A run with stories waiting for review, no approved
  stories, or an approved story that fails grounding is refused. Rendered output is scanned
  for sensitive values and for any value in the run's redaction map.
- **File targets are complete; REST targets are a plan.** `ado_rest` and `jira_rest` build
  the exact requests and the field mapping, and `--dry-run` prints them. No HTTP client ships:
  `make_rest_client` says so. The create-or-update logic is tested against a fake system.
- **Approval is a value, not a flag.** `apply_plan` needs an `Approval` that carries the hash of
  the plan it covers. A changed plan needs a new approval. The CLI asks at a terminal and has
  no option to skip it; without a terminal only `--dry-run` works.
- **Idempotency by label.** Each epic, feature and story gets `sa-<12 hex>` from the workspace,
  the redacted scenario and the story identity. A second publish finds the item by label and
  updates it. Updates do not re-link the parent. A CSV import cannot update.
- **Destination quirks stay in config and in the publisher.** ADO needs Epic > Feature > Story,
  so unassigned stories go under a default feature. Jira has no features, so they become
  labels. Description layouts live in `config/templates/`.
- **Spreadsheet safety.** CSV cells starting with `=`, `+`, `-` or `@` are prefixed with `'`.

## Consequences

- A REST write needs a client written against a real tenant; the plan and tests are ready for it.
- Titles that really start with `-` or `=` show a leading `'` in the CSV.
- Snapshots must be reviewed by a person when they change (`UPDATE_SNAPSHOTS=1`).
