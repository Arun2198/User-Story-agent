# 0005. Discovery and clarification design

- Status: accepted
- Date: 2026-10-07

## Context

Discovery must come from checklists, not model recall. Clarification is a hard gate:
the agent never drafts on its own assumptions. Free text must not be able to change
rules.

## Decision

- **Packs are data.** `config/domains/*.yaml` hold hints, sub-domains, optional
  sub-packs, actors and a category checklist. A pack may extend another. Loading
  validates cross-references. Adding an industry needs a YAML file and no code.
- **Detection is deterministic.** A weighted keyword score picks the domain,
  sub-domain and sub-packs (placeholders such as `<IFSC_1>` count as hints). The model
  receives this as a computed candidate and may override it within the allowed ids. An
  override is reported. Keyword detection is weak on short, ambiguous text, so the
  override matters.
- **Code guarantees coverage.** Every checklist category gets at least one item. Gaps
  the model omits are added as `unknown`. A `stated` item needs a verbatim excerpt that
  the grounding verifier finds in the scenario or notes, otherwise it is downgraded to
  `inferred`. Item order and ids are canonical and assigned in code.
- **Questions are ranked in code.** One question per category, scored by category weight,
  must-have bonus and number of open gaps. At most 6 per round and 3 rounds, set in
  `standards.yaml` and capped in code. The model only phrases question, reason and
  options. Options are cleaned (no "other", 2 to 4, deduplicated).
- **Answers resolve items.** An answer confirms its items with provenance. "Use your
  judgment" is recorded as an explicit judgment answer and stays visible in the readiness
  summary and in story assumptions. Defer keeps items unresolved but settled. Not
  applicable marks them rejected.
- **The gate is explicit.** `grant_go_ahead` needs every must-have category settled and a
  confirmation source (`user` or `answers_file`). `require_go_ahead` is what drafting will
  call first. After three rounds, remaining must-haves need an explicit user decision
  through `resolve_remaining`.
- **Free text is data.** It is redacted and scanned like any user text, stored on the
  round, and may be cited as `user_notes` provenance. Only a regex extractor for three
  whitelisted preferences reads it. No model is involved, so injected text cannot widen
  what it can change.
- **Answer text must be clean.** The `CleanText` type is produced only by running the
  component pre-hooks, so the type checker flags code that skips redaction and scanning.

## Consequences

The checklist can ask many questions for banking sub-domains, so three rounds of six may
not settle every optional category. Unsettled optional items become open questions on
stories. The keyword detector's accuracy is measured by `domain_detection` and its
misses are expected on short text with generic payment words.
