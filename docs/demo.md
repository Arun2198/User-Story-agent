# Demo: run the agent end to end

These are the exact steps to reproduce the demo, on your own machine or in a GitHub Codespace.
Allow 10 to 20 minutes for the first live run. NVIDIA's hosted models are slow and sometimes busy;
Anthropic is faster.

## 1. Get the code

```bash
git clone https://github.com/Arun2198/User-Story-agent.git
cd User-Story-agent
git checkout claude/story-agent-v2-plan-f7jhxq     # the branch with the latest work
```

In a Codespace, open the repository on GitHub, then Code, Codespaces, Create codespace on this
branch. It installs everything for you. On your own machine you need Python 3.11+ and
[uv](https://docs.astral.sh/uv/).

## 2. Install

```bash
uv sync
```

## 3. Prove the install works (no key needed)

```bash
uv run pytest -q                    # the test suite
uv run story-agent evals run        # offline evals with a scripted model; a few minutes
```

## 4. Add your key

You need one key: Anthropic (console.anthropic.com) or NVIDIA (build.nvidia.com, starts with
`nvapi-`).

```bash
source scripts/setup_keys.sh        # asks for each key, hidden; Enter skips one
```

The keys are saved in `~/.nvidia_env` (readable only by you) and are never printed or committed.
Never paste a key into a chat, a screenshot or a file in the repository.

## 5. Check the models answer

```bash
uv run story-agent models            # model ids your key can use
uv run story-agent models --best     # strongest ones first
uv run story-agent check             # one tiny call per model, with timings
```

## 6. Run the scenario

Pick the provider (or leave it out to use whichever key you set; Anthropic wins if both are set):

```bash
uv run story-agent --provider nvidia run \
  "A customer applies to the bank for a loan. The bank needs a way to decide whether the loan is approved." \
  --workspace demo
```

```bash
uv run story-agent --provider anthropic run "<same scenario>" --workspace demo
```

What you will see: progress lines for each stage (`scope_check`, `discover`, `clarify`, then
drafting, criteria and critique), then the agent asks you clarifying questions. Answer with a
number, your own words, `use your judgment`, `defer` or `n/a`. At the gate choose `go` to start
drafting. At review, approve, edit or reject each story. At the end you can approve what it
remembers for next time; nothing is saved unless you approve.

If the model is overloaded (`API error 503`), the tool retries, and if it gives up it prints
`story-agent resume <run id>`. Run that command to carry on from the last finished step.

## 7. Get the stories out

Everything for a run is saved in `runs/<run id>/` (in a Codespace:
`/workspaces/User-Story-agent/runs/`). The run id is printed at the start and end of a run.

```bash
ls runs/                  # find your run id (the newest folder)
```

Create the export file with `publish`. A run must be finished.

```bash
uv run story-agent publish <run id> --target json --out stories.json    # JSON
uv run story-agent publish <run id> --target md --out stories.md        # readable document
uv run story-agent publish <run id> --target ado_csv --out stories.csv  # Azure DevOps import
uv run story-agent publish <run id> --target jira_rest --dry-run        # shows the payload only
```

Leave out `--out` to save to `runs/<run id>/stories.<ext>`, or use `--out -` to print it in the
terminal. To create the file as part of the run itself, add `--format json --out stories.json`
to the `run` command.

| File | What it is |
|---|---|
| `runs/<run id>/stories.json` | The final JSON, after `publish --target json` |
| `runs/<run id>/state.json` | Everything the run produced |
| `runs/<run id>/checkpoint.sqlite` | The saved progress, used by `resume` |

In a Codespace, `runs/` is deleted with the codespace. Download the export first: in VS Code,
right-click the file in the Explorer and choose Download.

## 8. Show memory (run the scenario a second time)

```bash
uv run story-agent memory list --workspace demo
uv run story-agent run "<a similar scenario>" --workspace demo      # asks fewer questions
```

## Without a keyboard (scripts, CI)

```bash
uv run story-agent run "<scenario>" --answers answers.yaml
```

Without a terminal the tool refuses to run unless `--answers` covers every question. See
`docs/getting-started.md` for the file format and exit codes.

## Troubleshooting

| You see | Do this |
|---|---|
| `... is not set` | Run `source scripts/setup_keys.sh`, or use `--provider` for the key you have |
| `API error 503` | The provider is busy. Wait, then `story-agent resume <run id>` |
| `API error 410` or `404` | The model id is retired or wrong. `story-agent models --best`, then edit `config/models.yaml` |
| `the key was rejected` | The key is wrong or revoked. Make a new one and run the setup script again |
| Very slow | Reasoning models think for minutes. Use `--provider anthropic`, or be patient |

More detail: `docs/getting-started.md`.
