# 0009. Run graph, pauses and the answers file

- Status: accepted
- Date: 2026-10-07

## Context

The run must stop and wait for a person at several points, survive a closed terminal, and
never start drafting without an explicit go-ahead. The same stages are already driven by
`Flow` (used by the evals), so the graph should not duplicate them.

## Decision

- **One graph, thin nodes.** Each node rebuilds a `Flow` from the saved `RunState`, runs one
  step and saves the state. The checkpoint holds the serialised state, so a resume in a new
  process needs nothing else: the redaction map is read from the run folder, the checklist
  is rebuilt from the packs and recalled memory is reloaded by id.
- **Pauses use `interrupt`, and never sit beside a model call.** A node that calls the model
  does not pause. A node that pauses only validates and applies the reply. A resume therefore
  repeats no model call.
- **Replies are validated.** Every pause has a reply model. A reply that does not validate
  asks again. A responder that keeps sending bad replies is stopped after 200 pauses.
- **The gate is the only place the go-ahead is set**, from a validated `GateReply`. A test
  scans the source for any other place. `go` is refused while must-have items are open;
  `judgment` and `defer` are explicit user choices recorded as answers.
- **Responders answer pauses.** The terminal responder asks a person. The answers-file
  responder answers only what the file says and stops on any gap. With no terminal and no
  file, `run` exits before building the model client.
- **Defaults deny.** An answers file does not approve review or memory unless it says so.
- **Checkpoints are local and private.** They contain the scenario as typed, so the run
  folder is 0700 and its files 0600. A hosted service would need encryption at rest.
- **Publishing is a later phase.** The last node writes `state.json`. Publishers attach there.

## Consequences

- A failed stage (for example a model outage) resumes from the last finished stage.
- The graph and the eval driver share `Flow`; a test checks they produce the same stories.
- Review and memory prompts are plain terminal prompts; a richer UI can send the same replies.
