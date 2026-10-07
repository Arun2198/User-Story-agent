# 0001. Record decisions as ADRs

- Status: accepted
- Date: 2026-10-07

## Context

The project has many choices that are easy to forget (guardrail behaviour, model
settings, packaging). Reviewers need to see why, not only what.

## Decision

Every significant decision gets a short ADR in `docs/adr/`, numbered in order and
based on `0000-template.md`. A decision that changes an earlier one supersedes it
rather than editing it.

## Consequences

Small cost per decision. `STANDARDS.md` links to the ADRs behind each standard.
