# 0006. Memory design

- Status: accepted
- Date: 2026-10-07

## Context

Memory should make repeat scenarios faster without weakening the clarification gate,
leaking one client's context to another, or storing personal data.

## Decision

- **Isolation by file.** Each workspace has its own SQLite file,
  `<memory_dir>/<workspace>.db`. Workspace names must match a strict pattern and the
  resolved path must stay inside the memory directory. The store refuses entries whose
  `workspace` differs from its own. A cross-workspace leak is a hard eval failure.
- **Only confirmed entries.** The `confirmed` column has a `CHECK (confirmed = 1)`.
  Entries come from answers the user gave, and only after the user approves each
  proposal. A proposal with no decision is rejected.
- **Stable ids.** An entry id is a hash of its type, domain, sub-domain and key tag, not
  its text. The same kind of fact keeps one id, so a changed answer updates the entry
  and is shown as a conflict. A "question pattern" is the checklist category
  (`category:<id>`), because question wording is generated and varies.
- **Write guard.** Every write, including CLI edits, is checked for PII and secrets,
  redaction placeholders, instruction-like or hidden text, length, and copied scenario
  text. A candidate that fails is refused and reported, never stored in redacted form.
- **Deterministic recall.** Filter by domain (the run's domain and `generic`) and
  sub-domain, select by tag overlap or by an FTS5 keyword hit with at least two shared
  terms, then sort by tag overlap, type, matched terms, last confirmed date and id.
  Keywords are limited to `[a-z0-9]` terms and quoted, so scenario text cannot inject FTS
  syntax. Bm25 scores are not used because they are unstable on small corpora.
- **Still always asked.** A remembered answer is shown as a default on the question
  ("Previously you said X ... still valid? yes / edit / no"). Yes confirms it with
  `memory_confirmed` provenance. No leaves the question open and counts as a rejected
  memory entry. A stale entry (past its TTL) is labelled and re-confirmed by the same
  explicit yes, which also proposes a refresh of its date.
- **Conflicts.** An answer that differs from the remembered default is a conflict. The
  user chooses to replace the saved answer or treat this run as an exception.
- **Effect on question count.** Memory does not remove questions, because that would
  bypass the gate. It turns them into one-keystroke confirmations. The component eval
  reports the share of first-round questions that carry a fresh default. An end-to-end
  measure (memory off, then on) comes with the app evals.

## Consequences

If a stored answer is paraphrased rather than repeated, it counts as a conflict and the
user decides. Entries are plain text, so long answers must be shortened (300 characters).
Embedding-based recall is not built and stays off.
