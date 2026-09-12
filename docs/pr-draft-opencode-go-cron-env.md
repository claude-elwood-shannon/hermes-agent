# PR Draft — fix(opencode-go): cron .env loading + session header on every request path

> **Status:** DRAFT — prepared by Andrew (Hermes Agent) for the user to open via web.
> **Do NOT open the PR from here.** The user opens it in 2 clicks with the Compare link below.
>
> **UPDATE 2026-09-12:** the branch now also carries `3919d5b002` —
> *fix(cron): `_resolve_job_runtime` returns 2-tuple on happy path again* — which fixes every
> agent-path cron crashing since Sep 8 with `ValueError: too many values to unpack (expected 2)`
> (verified live: autonomous-task-creator failure_streak 68, morning-report failure_streak 2,
> both failing every fire). See the added section at the bottom before opening the PR.

## Compare link (open this in the browser)

```
https://github.com/NousResearch/hermes-agent/compare/main...claude-elwood-shannon:hermes-agent:fix/opencode-go-cron-env-session
```

Base: `NousResearch/hermes-agent:main`
Head: `claude-elwood-shannon:hermes-agent:fix/opencode-go-cron-env-session`

---

## Title

```
fix(opencode-go): load profile .env in cron subprocesses; guard x-opencode-session on every request path
```

## Description (EN — paste into the PR body)

### Motivation

The autonomous-task-creator cron job was failing with a reproducible **HTTP 400** (`MissingSessionID`) and, separately, with an **API key of `None`** when the gateway ran in multiplex mode. Two distinct root causes, both in the OpenCode Go integration:

1. **Cron subprocesses lost the profile `.env` credentials.** The multiplex gateway skips dotenv loads; the `no_agent` path already reloaded via `load_hermes_dotenv`, but the **agent path did not**. So `resolve_runtime_provider()` saw no `OPENCODE_GO_API_KEY` and the API key resolved to `None`.
2. **Two request paths bypassed the session header.** Upstream's `opencode_affinity` module covers the main turn and `auxiliary_client` paths, but two shortcuts skipped it:
   - `_iteration_summary_chat_kwargs()` calls `chat.completions.create` directly with a rebuilt client, skipping `build_api_kwargs`.
   - `build_anthropic_client()` for the Anthropic-wire OpenCode path only sent attribution headers, not the session header.

OpenCode Go has enforced `x-opencode-session` for backend routing since 2026-09-06, so these bypasses fail with `MissingSessionID` (HTTP 400).

### The fix — three layers

| Layer | File | What |
|-------|------|------|
| 1. Cron agent-path `.env` reload | `cron/scheduler.py` | `_resolve_job_runtime()` now calls `load_hermes_dotenv(hermes_home=...)` and derives `explicit_api_key` from `OPENCODE_GO_API_KEY` / `OPENCODE_API_KEY`; `run_job()` reloads the profile `.env` into the ephemeral subprocess (which runs with `start_new_session=True` and does not inherit the gateway's env). |
| 2. Session header on summary path | `agent/chat_completion_helpers.py` | `_iteration_summary_chat_kwargs()` now injects `x-opencode-session` (uuid fallback) on the rebuilt client. |
| 3. Session header on Anthropic-wire path | `agent/anthropic_adapter.py` | `build_anthropic_client()` now sends the session header alongside the attribution headers. |

Plus a final guard in `agent/chat_completion_helpers.py` so **every** chat-completions request to an opencode endpoint carries `x-opencode-session`.

### Live evidence

- Creator fire at **01:18** succeeded after the gateways restarted at **01:02** with this code (previously reproducible HTTP 400).
- The fix is defensive: every new block is wrapped in `try/except` so a `.env` reload failure degrades to the previous behavior instead of raising.

### Tests

- `cron/scheduler.py` imports cleanly; `_get_hermes_home`, `_resolve_job_runtime`, `run_job` all present.
- `load_hermes_dotenv(hermes_home=...)` callable with the fix's exact signature; agent-path block derives `explicit_api_key` correctly.
- Defensive check: `load_hermes_dotenv` with a nonexistent home does not raise.
- Syntax check passes on all 5 modified files.

### Files changed

```
agent/anthropic_adapter.py           | 13 ++++++++
agent/chat_completion_helpers.py     | 64 ++++++++++++++++++++++++++++++++++++
agent/client_lifecycle.py            | 13 ++++++++
agent/transports/chat_completions.py | 20 +++++++++--
cron/scheduler.py                    | 31 ++++++++++++++++-
5 files changed, 138 insertions(+), 3 deletions(-)
```

### Commits

| Hash | Message |
|------|---------|
| `b1e469f51c` | fix(opencode-go): inject x-opencode-session in summary and Anthropic paths |
| `582542c46b` | fix(opencode-go): load profile .env in cron subprocesses; guard session header on every request path |

---

## Notes for the user

- **Fork sync already done:** `claude-fork` main was force-pushed from `73f5c44` to `4006947` (4 commits ahead of upstream `main`). This happened in the interrupted manual attempt; it is intentional and verified.
- **This PR contains only the 2 opencode-go commits.** The other two local commits (`5f32c6b8b5` kanban triage gate, `400694719a` nanogpt pricing) are separate concerns and are **not** in this branch.
- **Do not push to `NousResearch`.** The Compare link targets the fork head; the PR is opened from the fork.
- Author: `Claude Elwood Shannon <claude.el.shannon@proton.me>`; Co-authored-by: `Hermes Agent <agent@nousresearch.com>` (already in commit metadata).

---

## Section for 3919d5b002 (add to the PR description)

### `fix(cron): _resolve_job_runtime returns 2-tuple on happy path again`

While instrumenting the .env fix (`582542c46b`), a dead
`primary_provider_for_drift` computation was added to `_resolve_job_runtime`'s
happy path and the return changed to a 3-tuple — but the function's only call
site (`_resolve_cron_agent_setup`) still unpacks 2, and the fallback path
still returns 2. Since that commit, **every agent-path cron job crashed
before the model call** whenever the happy path was taken:

```
ValueError: too many values to unpack (expected 2)
  File "cron/scheduler.py", line 2286, in run_job
    setup = _resolve_cron_agent_setup(job, job_id, job_name, jc)
  File "cron/scheduler.py", line 2157, in _resolve_cron_agent_setup
    setup.runtime, setup.model = _resolve_job_runtime(job, job_id, jc)
```

Live evidence from one profile (2026-09-12): `autonomous-task-creator`
failure_streak **68** (every 30-min fire since Sep 11 09:52), `morning-report`
failure_streak **2**. The dead variable was also never initialized, so a
`NameError` was one refactor away.

Fix: restore the 2-tuple on the happy path (the invariant the call site and
the fallback path already agree on) instead of threading an unused third
value through. A chain that returns `(runtime, model)` from one branch and
`(runtime, model, unused)` from the other is the bug class this fix removes.
