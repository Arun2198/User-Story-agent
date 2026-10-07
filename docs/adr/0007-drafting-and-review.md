# 0007. Drafting, critique and review design

- Status: accepted
- Date: 2026-10-07

## Context

Stories must be traceable to what the user said or confirmed. The model is good at
wording and grouping, and unreliable about facts, numbers and ids.

## Decision

- **Requirements come from code.** Only stated and confirmed discovery items become
  requirements, in checklist order with ids `REQ-001`, ... An item settled by "use your
  judgment" becomes a requirement marked `assumed`. Inferred and unknown items never do.
- **The model groups and words.** It returns epics, optional features and stories that
  cite requirement ids. It does not set ids, provenance, assumptions, NFRs, open
  questions or confidence.
- **Provenance is derived.** Persona, want and benefit each get provenance copied from
  the requirements the model names for that part, or from all cited requirements when it
  names none. A story citing no valid requirement is dropped and reported.
- **Invented numbers are an error.** Any number in a title, want, benefit or criterion
  that the user never gave (scenario, notes, answers, free text, edits) is a blocking
  finding. 0 and 1 are allowed. Under an assumption the prompt asks for no number and the
  value goes to an open question.
- **Ids are deterministic.** `<EPIC_PREFIX>-<4 digits>`. Epics are ordered by their
  lowest requirement id, stories by feature, priority, lowest requirement id and title.
  A story with the same epic, requirements and title keeps its id across revisions; new
  stories take the next free number. The prefix comes from `standards.yaml`
  (`epic_prefixes`) or from the epic name. Epic names are model wording, so they can vary
  between runs.
- **Critique is code first.** Coverage, duplicates, size, readiness, grounding, id
  format and stability, dependency cycles and INVEST heuristics are checked in code. The
  model adds duplicates, contradictions, negotiability and split suggestions, and its ids
  are validated. Errors trigger a revision, at most twice. Anything left is shown to the
  reviewer, not dropped.
- **Estimates and priority are suggestions.** They are the agent's, snapped to the
  configured scale, and are editable at review.
- **Review is explicit.** Edits pass redaction and the injection scan first. An edited
  persona, want or benefit gets `user_notes` provenance that points at a recorded edit
  note, and the edit is applied only if the story still passes grounding. Every field
  change is logged with before, after and an edit distance. Approving a story with open
  blocking findings is allowed and logged with their codes.
- **Quality hooks.** Deduplication, id stability and coverage hooks report while drafting
  and block at review and publish.

## Consequences

The number check can flag a correct paraphrase ("one day" versus "24 hours"), which costs
a revision. Without a live run the quality of drafted wording is unmeasured; the offline
evals cover only the deterministic rules.
