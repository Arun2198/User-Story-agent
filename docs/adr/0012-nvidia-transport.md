# 0012. NVIDIA-hosted models as the only provider

- Status: accepted
- Date: 2026-10-07
- Supersedes: 0002

## Context

I want to run the tool on NVIDIA's hosted models, using an NVIDIA API key. The model call is
already behind a small `Transport` interface, so the pipeline, guardrails, memory and evals
do not know which provider is behind it.

## Decision

- **One transport, `NvidiaTransport`** (`nvidia.py`), posting to `<base_url>/chat/completions`
  with `httpx2`. The earlier SDK-based transport and its dependency are removed.
- **Settings in config.** `base_url`, `api_key_env`, model ids, temperature, timeout and the
  structured output mode live in `config/models.yaml`. The key is read from the environment
  variable named there (`NVIDIA_API_KEY`), and is only sent in the `Authorization` header.
- **Structured output.** Default `guided_json` sends the schema in NVIDIA's `nvext.guided_json`.
  `json_schema` uses `response_format`. `none` puts the schema in the prompt. Whatever the
  mode, the reply is cleaned (code fences and `<think>` blocks removed), validated against the
  schema, repaired once, then fails closed, exactly as before.
- **Temperature 0** is sent, as the original brief asked. Output is still not guaranteed
  identical between calls.
- **Errors.** Timeouts, network errors, 408, 409, 425, 429 and 5xx are transient and retried with
  backoff. 401 and 403 say the key was rejected. 404 names the model. Error details are cut to
  200 characters and never include the request.
- **Prices are nominal.** The hosted free tier does not bill per token. The numbers in config
  keep the cost budget and cost metrics meaningful and should be replaced with a real rate on a
  paid host.

## Consequences

- The prompts were written before this change and have not been tuned for these models. Live
  evals decide whether they need work.
- The generator and judge ids in config were chosen without seeing the live catalog and must
  be checked against the account.
- Not tested against the real service from the build environment, which could not reach it. The
  transport is tested against a mock server only.
- A different provider needs a new transport and a config change, nothing else.
