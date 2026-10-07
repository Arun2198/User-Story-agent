# Getting started

A step-by-step guide for running story-agent on your own machine, and for running it somewhere
other than your own machine. No earlier experience with the tool is assumed.

## What it is

You describe a need in plain language. The tool asks a few questions, shows what it understood,
waits for you to say go, writes user stories with acceptance criteria, lets you approve, edit or
reject each one, and saves them as a markdown, JSON or Azure DevOps CSV file.

It is a **command line tool** that you run yourself. There is no web page, no hosted service and
no login. One person uses it at a time. It needs the internet only to reach the model.

## Part 1. Run it on your computer

### 1. What you need

| Need | Check it with | Where to get it |
|---|---|---|
| Python 3.11 or newer | `python3 --version` | python.org |
| git | `git --version` | git-scm.com |
| uv (installs everything else) | `uv --version` | `pip install uv` or docs.astral.sh/uv |
| An NVIDIA API key (starts with `nvapi-`) | | build.nvidia.com, sign in, then Get API Key |

The key is only needed to run real scenarios. The test suite and the offline evals work without
one.

### 2. Get the code and install

```bash
git clone https://github.com/Arun2198/User-Story-agent
cd User-Story-agent
uv sync
```

Always run the tool **from this folder**. It reads `config/`, `prompts/` and the domain packs
from here.

### 3. Check that it works (no key needed)

```bash
uv run pytest -q          # a few minutes; all tests should pass
uv run story-agent --help # lists run, resume, publish, memory, evals, online
```

### 4. Set your key

Create a key at build.nvidia.com (sign in, then Get API Key). One key works for every model.
The tool reads it from the environment variable `NVIDIA_API_KEY`. It does **not** read the `.env`
file by itself, so set it in your terminal:

```bash
export NVIDIA_API_KEY="nvapi-..."            # macOS and Linux
# PowerShell: $env:NVIDIA_API_KEY="nvapi-..."
```

**The easiest way, on any machine, including a codespace where the GitHub secrets do not arrive:**

```bash
source scripts/setup_keys.sh
```

It asks for the key without showing what you type,
saves them in a private file in your home folder (`~/.nvidia_env`, readable only by you, outside
the repository), loads them into this terminal, and makes every new terminal load them too. It
never prints a key. Run it again to replace a key. Delete `~/.nvidia_env` to remove them.

The `export` line above does the same for one terminal window only. Where the key goes, by setup:

| Where you run it | Where to put the key |
|---|---|
| Your computer | `export NVIDIA_API_KEY=...` in the terminal (or your shell profile) |
| GitHub Codespaces | Settings, Codespaces, Secrets, `NVIDIA_API_KEY` (Part 2, Option A) |
| A server | The environment of the account that runs it |
| GitHub Actions | Repository Settings, Secrets, `NVIDIA_API_KEY` |

One NVIDIA key works for every model, including the judge, so you only set `NVIDIA_API_KEY`.

Never put the key in a file you commit, a chat or a screenshot. If a key is exposed, revoke it
in the NVIDIA console and make a new one. To use another variable name, change `api_key_env` in
`config/models.yaml`.

**Check the model ids.** `config/models.yaml` names a generator and a judge. Hosted model ids get
retired, so confirm they are live:

```bash
uv run story-agent models                     # every id your key can use, and whether yours are in it
uv run story-agent models --filter nemotron   # only ids containing "nemotron"
```

If an id is missing, copy a listed one into `generator` or `judge` in `config/models.yaml`. A
retired id shows up as `API error 410 ... has been retired`, an unknown one as
`API error 404: model ... was not found`, and a bad key as `the key was rejected`.

### 5. Your first run

```bash
uv run story-agent run "A customer applies to the bank for a loan. The bank needs a way to decide whether the loan is approved." --workspace demo
```

What you will see, in order:

1. **Questions**, at most 6 at a time and at most 3 rounds. Each one says why it matters and
   lists numbered options. Type a number, type your own answer, or type `use your judgment`,
   `defer` or `n/a`. If memory has an earlier answer it shows it and asks `yes` or `no`.
2. **"Anything else you want to add or change?"** at the end of each round. Press Enter to skip.
3. **A readiness summary.** It lists what you stated, what you confirmed, what it will assume
   because you said so, and what is still open. Choose `go` to start drafting, `more` for another
   round, or `judgment` or `defer` to settle what is left.
4. **Review.** Each story is shown with its criteria and where it came from. Choose `approve`,
   `edit` or `reject`.
5. **Memory.** It offers answers worth remembering for next time. Nothing is saved unless you
   approve each one. The default is reject.

Press Ctrl-C at any prompt to stop. The run is saved. Continue it with the id it printed:

```bash
uv run story-agent resume run-20261007-101500-3fa2
```

Write the result while you run, or later:

```bash
uv run story-agent run "..." --format md --out stories.md
uv run story-agent publish run-20261007-101500-3fa2 --target md
uv run story-agent publish run-20261007-101500-3fa2 --target ado_csv --out import.csv
```

Targets: `md`, `json`, `ado_csv` (import into Azure DevOps). `ado_rest` and `jira_rest` only
print the requests they would send (`--dry-run`); they cannot write yet.

### 6. Where things are saved

| Path | What it holds |
|---|---|
| `runs/<run id>/state.json` | The finished run |
| `runs/<run id>/stories.md` | Output from `publish` |
| `runs/<run id>/checkpoint.sqlite` | The saved position, so `resume` works |
| `runs/<run id>/redaction_map.json` | Placeholders and the original values they replaced |
| `runs/<run id>/trace.jsonl` | One line per model call: ids, hashes, tokens, cost |
| `memory/<workspace>.db` | Saved memory, one file per workspace |

These folders hold your scenario as you typed it, including anything private. They are readable
by you only. Do not commit or share them. `runs/` and `memory/` are already ignored by git.

### 7. Running without a terminal (an answers file)

Scripts and servers have no keyboard. Without a terminal the tool refuses to run unless you give
it an answers file, so it can never skip your go-ahead on its own.

```yaml
# answers.yaml
answers:                    # a question id or a category id, and the reply you would type
  limits_velocity: "yes"
free_text: "At most 4 criteria per story."
go_ahead: true              # required, otherwise the run stops at the readiness summary
unanswered: judgment        # stop (default) | judgment | defer
on_conflict: replace        # stop (default) | replace | exception
review: approve_all         # none (default) | approve_all
memory: none                # none (default) | approve_all
```

```bash
uv run story-agent run "..." --answers answers.yaml --format md --out stories.md
```

Anything the file does not cover stops the run with exit code 3, and `resume` carries on later.
Exit codes: 0 finished, 1 error, 2 no terminal and no answers file, 3 paused, 4 refused or blocked.

`approve_all` means no one has read the stories. Use it for trials, not for real backlogs.

### 8. Memory

```bash
uv run story-agent memory list --workspace demo
uv run story-agent memory show <entry id> --workspace demo
uv run story-agent memory delete <entry id> --workspace demo
uv run story-agent memory export --workspace demo --out demo-memory.json
uv run story-agent memory clear --workspace demo
```

A workspace is a name you choose, for example one per client or project. Memory never crosses
workspaces. Use `--no-memory` to turn it off for a run.

### 9. Changing how it behaves

| To change | Edit |
|---|---|
| Model names | `config/models.yaml` |
| Story template, estimate scale, limits | `config/standards.yaml` |
| Questions and checklists per industry | `config/domains/*.yaml` |
| Budgets, size limits, refusal message | `config/guardrails.yaml` |
| Labels and fields for Azure DevOps or Jira | `config/destinations.yaml` |
| Memory expiry and recall | `config/memory.yaml` |

To add an industry, add a YAML file in `config/domains/`. No code is needed (see the README).

### 10. Checking quality

```bash
uv run story-agent evals run                     # offline, no key, a few minutes
uv run story-agent evals run --live --update-baseline   # real model, needs a key, costs money
```

The offline run uses a scripted stand-in for the model. It checks that the guardrails and the
plumbing work, not how good the real model's stories are. Only `--live` tests the real model.

### 11. If something goes wrong

| You see | Likely cause and fix |
|---|---|
| `NVIDIA_API_KEY is not set` | Run the `export` line again in this terminal |
| `There is no terminal to ask you questions` | You are not in an interactive terminal. Use `--answers FILE` |
| `no such file ... config` or missing prompts | Run from the repository folder, or set `STORY_AGENT_CONFIG_DIR` |
| `Paused: ...` and a `resume` hint | The run is saved. Fix what it names and run `resume` |
| `I can only turn scenarios into user stories...` | The request was judged out of scope. Describe a need or process, not a question |
| `API error 410 ... has been retired` | The configured model id is gone. Run `uv run story-agent models`, then put a listed id in `generator` and `judge` in `config/models.yaml` |
| It prints progress lines like `discover: working ...` and then seems stuck | A reasoning model can take minutes per call. A line such as `the model call failed (the request timed out); retry 1` means a call timed out and is being retried. Run `uv run story-agent check` to time each model, and `uv run story-agent check --probe-thinking` to find a setting that makes it answer quickly |
| `model stopped with length while thinking` | The model is a reasoning model and used its token budget thinking. Turn its thinking off with `extra_body` in `config/models.yaml`, set `max_tokens`, or choose a different model |
| `error: run ... has not finished` | `publish` needs a finished run. `resume` it first |
| A story count of zero or a block message | A guardrail stopped the run. The message names the reason |
| Cost surprises | Budgets are in `config/guardrails.yaml`. Check `runs/<id>/trace.jsonl` |

## Part 2. Run it "online"

Because this is a command line tool, "online" means running it on a machine that is not your
laptop, or letting someone else use it. There is **no built-in web app or multi-user service**.
These are the options that work today, from simplest to most involved.

### Option A. GitHub Codespaces (set up for you)

The repository includes `.devcontainer/devcontainer.json`, so a codespace installs everything
by itself. Good for trying it without installing anything.

1. **Add your key once, as a Codespaces secret.** On GitHub: your profile picture, Settings,
   Codespaces, Secrets, New secret. Name it `NVIDIA_API_KEY`, paste the key, and under
   "Repository access" select `User-Story-agent`. The key is then in the environment of every
   codespace for that repository. Never paste it into a terminal history or a file.
2. **Create the codespace.** On the repository page choose Code, Codespaces, Create codespace on
   the branch you want (for example `claude/story-agent-v2-plan-f7jhxq`, or `main` once merged).
3. **Wait for it to finish.** The first start takes a few minutes while it runs
   `pip install uv && uv sync --locked`. The terminal shows when it is done.
4. **Check it.** In the terminal:
   ```bash
   echo "${NVIDIA_API_KEY:+key is set}"      # prints "key is set" (and never the key)
   uv run story-agent --help
   uv run pytest -q                              # optional, a few minutes
   ```
5. **Run it.** Use it exactly as in Part 1, from step 5. The terminal is a real terminal, so the
   questions and review prompts work.

Things to know:

- **Cost and data.** The codespace is on GitHub's servers. Its disk holds `runs/` and `memory/`,
  including scenarios as typed. Use a private repository, and do not use real customer data until
  you are happy with that.
- **Stopping.** An idle codespace stops after a while and keeps its files. Delete it when you
  are done (github.com/codespaces), which also deletes `runs/` and `memory/`.
- **Keep your work.** Copy anything you want to keep out of `runs/` before deleting. Do not
  commit `runs/`, `memory/` or `.env` (they are ignored by git).
- **Other keys.** Add other secrets the same way, with the name the config expects.
- **Free allowance.** GitHub gives a monthly allowance of codespace hours. Check your plan.
- **Not tested by me.** I checked the install steps on a clean copy of the repository, but I
  could not start a real codespace from here. If the first start fails, open the creation log
  (Command Palette, "Codespaces: View Creation Log") and send me the last lines.

### Option B. A server or virtual machine you control

1. Install Python 3.11+, git and uv on the machine, clone the repository and run `uv sync`.
2. Set `NVIDIA_API_KEY` in the environment of the account that runs it (a service
   manager's environment file, or your shell profile). Do not put it in the repository.
3. Connect with SSH and run `uv run story-agent run ...`. Use `tmux` or `screen` so a dropped
   connection does not lose your session. A paused run can always be continued with `resume`.
4. Give the account its own folder for `runs/` and `memory/` and keep those folders private.

### Option C. Scheduled or automated runs (for example GitHub Actions)

Use an answers file, because there is no keyboard.

```yaml
- run: uv sync --locked
- run: uv run story-agent run "$SCENARIO" --answers answers.yaml --format md --out stories.md
  env:
    NVIDIA_API_KEY: ${{ secrets.NVIDIA_API_KEY }}
```

Be careful. The scenario goes into the workflow logs and the run folder, and anything you upload
as an artifact. Do not use real customer data this way. Review the output before it is used,
because `review: approve_all` means no one has read it.

### Option D. Use the skill, with no code

`skill/scenario-to-stories/SKILL.md` is the same workflow for an assistant that supports
skills. Give the assistant that folder and describe your scenario to it. It follows the same
rules and reads the same prompts and checklists. It has no saved runs, no checkpoints and no
automatic checks, so use the tool when you need repeatable results.

### Before sharing it with other people

The tool is built for one user on one machine. Before several people use one copy, you would
need to add:

- A login and separate run and memory folders per person (today they share the folder).
- Encryption of the run folders, which hold scenarios exactly as typed.
- A real destination for monitoring, if you want it (see "Monitoring" below).
- A web or chat front end, if non-technical people will use it.

The risk register (`docs/risk-register.md`) and the threat model (`docs/threat-model.md`) list
what changes when it moves from one person's machine to a shared service.

### Monitoring a running setup ("online evaluation")

This is separate from running it online. It records how real runs go, and is **off by default**.

```bash
# in config/evals.yaml set:  online: { enabled: true, sink: jsonl }
uv run story-agent online feedback <run id>
uv run story-agent online trace <run id> --otlp
uv run story-agent online drift      # needs a live baseline: evals run --live --update-baseline
```

Sending the records to a monitoring service needs details I do not have yet. Those sinks are
placeholders (see `docs/adr/0011-online-evaluation.md`).

## Where to read next

- `README.md` for the command reference, adding a domain pack, an ingestor and a publisher.
- `docs/model-card.md` for what it is for and how it is checked.
- `docs/adr/` for why it is built this way.
