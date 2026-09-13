# Contributor Notes — hermes-agent (OBJ-33)

Notes from our first contribution cycle to `NousResearch/hermes-agent`,
2026-09-10. Goal was dual: ship something upstream and learn the core
tool we run on daily. What follows is what we learned, not what we
shipped — the delta is the point.

## What we did

1. **Audited our 4 unpushed fork commits against current upstream main**
   (`git fetch origin`, then line-level comparison of every file the
   commits touch). Verdict:
   - `4c6db9d3` (cron profile-.env in subprocesses) — upstream fixed the
     same bug class on main, architecturally: `_run_one_job_body` and the
     external-worker entry both install `set_secret_scope(
     build_profile_secret_scope(...))`, and `get_secret()` fails closed
     without a scope. Our subprocess dotenv hack is obsolete. This was a
     parallel discovery of a real bug, independently confirmed.
   - `30b42feb` + `4c6db9d3` (x-opencode-session coverage) — upstream has
     `agent/opencode_affinity.py` now, plus 4 open community PRs covering
     the summary path (#105141), stateless one-shots (#105023, #105867,
     #107021). Our per-path guards duplicate those.
   - `84734505` (triage sweep vs human gates) — open PR #105324 covers it.
   We opened **no duplicate PR**. The docs gap we found (how a cron fire
   gets credentials under multiplex) became our first upstream PR.

2. **Opened PR**: docs(cron) — "Credential Resolution for a Fire"
   section in `website/docs/developer-guide/cron-internals.md`,
   grounded line-by-line in main's code.

## What we learned about the core

- **The secret scope is the nervous system of multi-profile isolation.**
  `agent/secret_scope.py`: `get_secret()` resolves global-allowlist →
  context-local scope → (fail closed under multiplex). Gateway turns,
  cron fires, and external workers all live or die by installing
  `set_secret_scope(build_profile_secret_scope(home))`. Any new code
  that reads credentials via raw `os.environ` is a leak or a 401
  waiting to happen.
- **Multiplex skip is a feature, not a bug.** `load_hermes_dotenv`
  deliberately skips process-global loads under multiplex+override:
  writing profile credentials to shared `os.environ` is exactly the
  leak the design forbids. Our Sep-8 "fix" fought this; upstream's
  design answers it properly.
- **The affinity-header pattern** (`opencode_affinity.py`) is the
  upstream idiom for per-conversation provider routing: resolve scope →
  conversation context → session id, merge with `setdefault` so
  caller-pinned values win. Our fork's scattered per-path guards
  predate this idiom.
- **Upstream moves fast**: our 4-commit delta (Sep 8) was substantially
  covered by Sep 10 main. Fork-first fixes have a shelf life of days.
  The durable fork assets are our own plugin + scripts, not core diffs.
- **Upstream review culture** (from CONTRIBUTING + AGENTS.md rubric):
  premises get verified line-level before merge; "fix the whole class"
  beats "fix my instance"; tests must be 1-2 invariant tests, red on
  base; change-detector tests are rejected on sight; docs updates ride
  the same PR as the symbol move they describe.

## Process notes (the contributor's path, concrete)

- GitHub over Tor: SSH (`ssh.github.com:443` via ProxyCommand nc) works
  for auth but `git fetch` over it hung; **HTTPS + `-c http.proxy=
  socks5h://127.0.0.1:9050` + `--depth=1` works reliably**. API via
  `curl --proxy socks5h://...` + token header file (umask 077). gh CLI
  is unauthenticated on this host; REST is fine.
- The running install IS the fork checkout (`~/.hermes/hermes-agent`).
  Never branch/commit there mid-session. Contribution worktree:
  `git worktree add <ws>/hermes-contrib FETCH_HEAD` after a shallow
  fetch of upstream main.
- Commit identity: `-c user.name/user.email` per command (never
  `git config --global`), human author + `Co-authored-by: Hermes Agent
  <agent@nousresearch.com>` trailer.
- Duplicate check before ANY PR: search issues AND PRs (separate
  queries; GitHub search API requires `is:issue` or `is:pull-request`),
  then read the competitor PRs' diffs — a "same topic" PR may cover
  different paths, and a same-function PR may encode a design decision
  (read its comments/diff notes) you must not fight.

## Next contributor moves (ideas, not commitments)

- Review kokhlo's #105141 (summary session header) against the
  anthropic summary path we fixed on the fork; our live reproduction
  (MissingSessionID on max-iterations summary) is test evidence they
  may not have.
- The aux-client `AnthropicMessagesShim.create()` accepts caller
  `extra_headers` passthrough; worth checking whether every aux
  caller that resolves opencode endpoints actually populates them
  (potential follow-up to #105023 rather than a competing PR).
- Our fork's `[OPENGUARD]` log lines are useful diagnostics; if a
  maintainer wants them upstreamed as a debug aid, propose it as an
  addition to `opencode_affinity` (single chokepoint) — not as
  per-path logging.
