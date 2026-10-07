# Model card

story-agent is a tool that turns a plain-language scenario into traceable user stories with
acceptance criteria. It uses a hosted language model for wording and classification. It does
not train or fine-tune any model. This card describes the system and how it is used and
checked, not a model of its own.

## Models

Model names live in `config/models.yaml` only. At the time of writing:

| Role | Config key | Value |
|---|---|---|
| Generator (every pipeline call) | `generator` | `claude-sonnet-5-5` |
| Judge (evals and sampled online scoring) | `judge` | `claude-opus-5-5` |

The judge is a different model from the generator on purpose. These models do not accept a
non-default temperature, so `temperature` is `null` in config (ADR 0002). Output is not
guaranteed identical between runs. A response cache, canonical ordering and ids set by code
carry most of the repeatability, and a stability eval measures the rest.

## Intended use

- A business analyst or product owner describes a need (banking first, other industries through
  a domain pack) and wants stories with Given/When/Then criteria.
- A person answers a few clarifying questions, gives an explicit go-ahead, reviews every story,
  and approves anything that is kept in memory.
- Output goes to markdown, JSON or an Azure DevOps import file.

## Out of scope

- Anything other than turning scenarios into stories, answering clarification questions,
  refining or exporting stories, and managing memory. Other requests get a fixed refusal.
- Legal, regulatory or compliance advice. The banking pack is a list of things to ask about.
- Unattended use without an `--answers` file, or writing to another system without a person's
  approval.
- Documents as input. Only typed text is accepted in this version.

## How it works

Stages: scope check, intake (redaction and injection scan), memory recall, discover, clarify,
draft, criteria, critique, human review, memory proposal, publish. Code decides which questions
to ask (from a pack checklist), what counts as a requirement (only stated or confirmed items),
ids, order, provenance and numbers. The model groups requirements, words questions and stories,
and writes criteria. A person confirms before drafting and reviews after.

## Prompts

Each prompt follows a fixed skeleton, is versioned in its front matter, hashed on every call and
listed in `prompts/CHANGELOG.md`.

| Prompt | Version | Used for |
|---|---|---|
| scope_check | 1 | Deciding whether a request is in scope |
| discover | 1 | Classifying checklist items as stated, inferred or unknown |
| clarify | 1 | Wording clarification questions |
| draft | 1 | Grouping requirements into epics and stories |
| criteria | 1 | Writing acceptance criteria |
| critique | 1 | Finding duplicates, contradictions and weak stories |
| judge | 1 | Scoring a finished run (evals and sampled online scoring) |
| scenario_synth | 1 | Generating synthetic eval scenarios |

A prompt change bumps its version, adds a line to the change log and, for a model upgrade,
adds an eval comparison to the table below.

## Evaluation

Checked in CI with `story-agent evals run`, against the thresholds in `config/evals.yaml` and
the baselines in `src/story_agent/evals/baselines/`.

**What the offline numbers mean.** The offline end-to-end runs use a scripted stand-in for the
model, driven by the gold labels, and a simulated user that answers from a hidden key. They test
the plumbing and the guardrails. They do not measure how well the real model does. The 14
synthetic cases, the gold model and the simulator were all written by me, so they share my
blind spots.

Offline results at the time of writing:

| Check | Result |
|---|---|
| Redaction precision and recall (dev and holdout) | 1.0 and 1.0 |
| Injection catch rate, false positive rate | 1.0, 0.0 |
| Scope guard accuracy | 1.0 |
| PII sent to the model, in output, or saved to memory | 0 |
| Injection text sent to the model | 0 |
| Ungrounded stories, hallucination rate | 0, 0.0 |
| Memory: question-count reduction | about 0.68 |
| Memory: contradiction handling, stale misapplication, cross-workspace leaks | 1.0, 0, 0 |

Live results (real model, judge scores, stability): **not run yet**. They need an API key. The
first run is saved as the live baseline, and the drift check uses it.

| Date | Change | Live result |
|---|---|---|
| | first live run | pending |

## Limitations

- Heuristic guardrails (redaction, injection) can miss unusual formats or phrasings.
- Banking checklists make long clarification sessions, up to 18 questions for payments with the
  India rails sub-pack.
- The domain packs and the eval cases reflect my own understanding of banking and of Indian
  payment rails. They need review by someone who works in the field.
- Stories can be well grounded and still badly worded. Review is not optional.
- Quality outside banking depends on the generic pack and has had less checking.
- English only.

## Human oversight

The user decides when to stop asking and start drafting, approves, edits or rejects every story,
approves each memory entry, and approves any external write. Edits are logged with how far they
moved from the draft.

## Monitoring

Online evaluation is off by default. When it is on, each finished run produces a trace and
feedback signals (approve, edit and reject rates, edit distance, answers typed as "other",
rejected memory entries), a sample can be scored by the judge, and a drift check compares the
averages with the live baseline. See `docs/adr/0011-online-evaluation.md`.

## Contact

Arun, owner and maintainer.
