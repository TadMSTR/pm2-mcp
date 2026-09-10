# Changelog

All notable changes to pm2-mcp are documented here.

## [Unreleased]

### Added

- **Real `--host` / `--port` support** (vikunja#770). `ecosystem.config.js` passed both flags
  for months while `server.py` had no argument parsing and read only `MCP_HOST`/`MCP_PORT`,
  so both were inert; the process bound 8486 from the hardcoded default and the two agreed by
  coincidence, which is why nobody noticed. Editing `--port` there would have changed nothing.

  `_resolve_bind(argv, env)` resolves with precedence **argv > env > `127.0.0.1:8486`**,
  per field — passing only `--host` leaves the port to `MCP_PORT`.

  **Non-loopback binds are refused and exit non-zero.** Only `127.0.0.0/8`, `::1` and
  `localhost` are accepted; hostnames are refused rather than resolved, because a name can be
  repointed after the check passes without the process restarting. This server has no
  authentication and its write verbs can stop or restart any PM2 process on the host, so
  `--host 0.0.0.0` would convert a documented-safe posture into a one-word footgun. There is
  deliberately **no override flag** — shipping the escape hatch alongside the flag that makes
  it necessary defeats the check.

  Two falsiness traps closed, both surfaced by the new negative tests: `--host ""` is
  `INADDR_ANY` at the socket layer and was being swallowed into the default rather than
  refused, and `--port 0` is a legitimate request that `or` turned into 8486. An empty
  *environment* variable is still treated as unset — that predates this change and is safe.
- `_is_loopback` added to the mutation gate's trust-boundary set (6 mutants, zero survivors).
  It is the predicate deciding whether an unauthenticated server is reachable off-box, and a
  gate covering the environment boundary but not the network one guards the smaller of the
  two. `_resolve_bind` is deliberately excluded and the script records why: all 15 of its
  survivors are argparse `prog=`/`description=`/`help=` strings, while every behavioural
  mutant — including `if not _is_loopback(host)` → `if _is_loopback(host)` — is killed.
- README **Non-goals** section, the last outstanding Showcase README checklist item.

- **Environment allowlist for the `pm2` child, in shadow mode** (vikunja#610). `_clean_env`
  now computes a named allowlist — `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `TERM`,
  `LANG`, `TZ`, the POSIX `LC_*` set, and `PM2_HOME` — and logs which variables enforcement
  *would* remove, per pm2 command, by name and never by value. Behaviour is unchanged until
  `PM2_MCP_ENV_MODE=enforce`; the default stays `shadow`.

  **Honest justification, because vikunja#610's is overstated for this repo.** The ticket
  groups pm2-mcp with `system-ops` and asserts a shared environment shape — 138 variables,
  41 secret-shaped. Measured on the live process: 20 environment variables and **zero**
  secret-shaped. Enforcement would withhold 13, none of them a credential. This is not
  remediating secrets leaking today; it is structural protection against recontamination the
  next time the service is started from a shell that has sourced one. That risk is real and
  measurable here — the live process carries `SSH_CLIENT`, `SSH_CONNECTION` and
  `XDG_SESSION_*`, which is what a process started from an interactive session looks like.

  The IPC and `CLAUDE*` families are excluded **by construction** rather than by a rule:
  they are simply not on the list, so there is no denylist left to fall out of sync.

  `PM2_HOME` is on the allowlist although the build plan's proposed list omitted it. It
  selects which PM2 daemon the CLI talks to, and dropping it does not fail loudly — verified
  that a wrong `PM2_HOME` silently spawns a second daemon and reports an empty process list.
  It happens to equal pm2's default on this host, so the omission would have been invisible
  here and broken every read on a host with a custom value.
- **Mutation-testing pilot**, gated on zero surviving mutants in the trust-boundary functions
  `_clean_env` and `_run_pm2` (`scripts/mutation-gate.sh`, wired into CI). Deliberately scoped
  rather than a repo-wide score: `_clean_env` already had zero survivors while `_parse_summary`
  had 39, so a percentage target would have directed all the effort at dict plumbing and none
  at the function that keeps credentials out of the `pm2` child environment. The 11 surviving
  `_run_pm2` mutants are killed by a new test asserting the full `subprocess.run` kwarg set.
- Negative tests for every privileged write path — `restart`, `stop`, `start`, `reload`,
  `flush` — covering the `pm2 failed` branch that all 16 previously-uncovered lines belonged
  to, plus both of `get_status`'s degraded paths.
- `ruff` (lint + format) configured and enforced in CI.
- CI now builds the wheel, installs it into a clean venv and imports from *that*, outside the
  checkout.

- **Showcase documentation set**: `ARCHITECTURE.md` (five diagrams, including the
  environment-boundary one this repo is the case for), `CONTRIBUTING.md`,
  `CODE_OF_CONDUCT.md`, `docs/operations.md`, `docs/threat-model.md`, GitHub issue and PR
  templates, and `.pre-commit-config.yaml` mirroring the CI lint job.
- `examples/` — real client wiring and a worked crash-loop diagnosis. `ecosystem.config.js`
  is referenced rather than copied, so there is no second version to drift.

### Changed

- `ecosystem.config.js` restores `--host 127.0.0.1 --port 8486`, live for the first time, and
  now records that **`pm2 restart` does not re-read this file.** Measured: it was edited
  2026-09-09 21:17 and the process restarted 2026-09-10 05:53, yet the live process still
  carried args the file no longer declared. `pm2 restart` re-execs the script from disk, so
  code changes land, but config comes from PM2's own dump.
- `docs/threat-model.md` §1 no longer says the loopback posture "stops holding the moment
  `MCP_HOST` is changed" — that is now false, since a non-loopback `MCP_HOST` refuses to
  start. The bind is enforced, not defaulted.
- `docs/operations.md` gains a measured second reason for the "never start from a session
  shell" rule. Same code, two parents: started by PM2 the shadow log names 64 withheld
  variables and **zero** secret-shaped; started from an interactive agent shell, 84 and
  **12** (`TASK_QUEUE_TOKEN_*`, `LANGFUSE_SECRET_KEY`, …). Names only — values are never
  logged and canary tests pin that — but the difference is entirely who ran the start command.
- `pyproject.toml` coverage annotation refreshed to 176 statements / 85 tests (was 129/47,
  already stale when written — PR #8 landed after the comment inside the same build). The
  floor and the percentage did not move; only the measurement did.

- Coverage floor set to **100%** and enforced from `pyproject.toml` (measured 100.00%, 49
  tests). It was previously enforced nowhere at all.
- `pytest.ini` folded into `pyproject.toml`; the unused `dev` extra dropped in favour of
  `requirements-dev.txt` as the single source for dev dependencies.
- `pip-audit` now runs with `--strict`.
- CI runs on **every** pull request. It was filtered to `branches: [main]`, so a PR opened
  against any other base ran no jobs at all — and a PR with zero checks reads as "CI hasn't
  started", not as unverified. Stacked PRs are where that matters most.
- Server instructions said this server manages PM2 services on **claudebox**; it runs on forge.
- README badge row gains the License badge (last, per the standard order). No PyPI or version
  badge: this repo publishes no package, and a badge pointing at a non-existent one is worse
  than none.
- `AGENTS.md` documented three tools that do not exist (`get_service_logs`, `delete_service`,
  and reload as cluster-mode-only) and omitted four that do. It now matches `server.py`.

### Fixed

- **Shadow-mode logging never reached disk** (vikunja#772). The `pm2_mcp` logger had no level
  and no handler, so its effective level was 30 (WARNING) and every `_log.info()` was dropped
  at the logger's level check before any handler was consulted. **Every shadow-mode run since
  the allowlist merged produced no evidence at all** — which matters because shadow mode
  exists solely to gather the evidence for deciding vikunja#771.

  `_configure_logging()` now sets the level *and* attaches a handler, called from `__main__`.
  Both halves are required: level-without-handler falls through to `logging.lastResort`,
  which is itself WARNING and would drop the record anyway. The handler is attached directly
  to `pm2_mcp` with `propagate = False`, which survives uvicorn's `dictConfig` — that calls
  `_clearExistingHandlers()`, but `Handler.close()` de-registers without detaching the
  handler or dropping its stream.

  **Why the test suite could not see it.** Every `test_shadow_log_*` test runs under
  `caplog.at_level(...)`, forcing a level production never had. Those tests are correct and
  are kept — they pin the message format. What was missing was a test of the *unforced* path.
  Verified two-sided against a neutered `_configure_logging` with its symbol and signature
  intact: the 5 new tests fail, and all 80 pre-existing tests pass. The second half is the
  finding — the old suite genuinely could not observe this bug. It is the same shape as the
  CI smoke test that imported from the source tree and hid an unbuildable package: *the test
  exercised a configuration the production path never has.*

  Also verified on the real startup path rather than only in pytest, since a passing unit
  test is exactly what failed to catch this.

- **The package could not be built at all.** `build-backend` was
  `setuptools.backends.legacy:build`, which is not a real backend — `python -m build` failed
  with `BackendUnavailable`. This survived two releases because CI's smoke test imported
  `server` from the source tree, so it passed on every run while the artifact was broken.
- The built wheel is now reproducible. With auto-discovery, building after a local `mutmut run`
  produced a wheel containing `mutants/server.py` and the whole mutant corpus but *no*
  top-level `server.py`; `py-modules` now pins the contents regardless of the working tree.
- `ecosystem.config.js` passed `--host`/`--port` and claimed they "take precedence".
  `server.py` has no `argparse` and never reads `sys.argv`, so both flags were inert and the
  bind address came from the code defaults — editing the port there would have changed nothing.
  Dead flags removed and the comment corrected. (vikunja#770)
- Three tests were incapable of killing any mutant while appearing healthy. mutmut's
  trampoline reads `MUTANT_UNDER_TEST` from `os.environ` on every call, so a test that
  replaces `os.environ` wholesale runs the *original* function under every mutant. Every
  surviving mutant in `_clean_env` was surviving for that reason alone. The mutation gate
  now also covers `_allowlist_env` and `_denylist_env`, which would otherwise have sat
  outside it at exactly the moment the trust decision moved into them.
- Tests asserting on the scrubbed environment no longer render the whole environment on
  failure. `assert name not in env` makes pytest print every variable and value, and on forge
  that ambient environment carries real credentials.

### Security

- Security audit `pm2-mcp-showcase-2026-09`: 3 findings, none above Low. All three carry
  `SECURITY[accepted]` / `SECURITY[deferred]` annotations in `server.py` and rows in
  `host-forge/security/accepted-risks.md`.
  - **Accepted** — the shadow-mode log writes withheld variable *names* to the PM2 log. Names
    only, pinned by tests with value canaries. Logging a count instead would defeat the
    feature: a count describes the parent environment, not the command.
  - **Accepted** — pm2's stderr reaches the caller verbatim. Pre-existing fleet-wide pattern;
    this build's tests pin it, which is why it is now on record.
  - **Deferred** (vikunja#771) — enforcement is verified on read paths only. Do not set
    `PM2_MCP_ENV_MODE=enforce` until a write verb has been exercised against the live daemon.

## [0.3.2] — 2026-09-09

### Fixed
- `_clean_env`/`_run_pm2`: strip any environment variable starting with `CLAUDE` before invoking the `pm2` CLI, in addition to the existing IPC-var strip. `pm2 start`/`pm2 restart --update-env` ships the calling process's entire environment to the daemon as the base env for the target app; when pm2-mcp itself runs inside a Claude Code session, that froze CLAUDECODE, CLAUDE_AGENT_SDK_VERSION, CLAUDE_CODE_*, CLAUDE_PLUGIN_ROOT and others into whatever app was started or restarted — including `CLAUDE_CODE_OAUTH_TOKEN`, a live credential, found leaked into a world-readable `dump.pm2` for 9+ days. Matched by prefix rather than an enumerated list: an exact list of "known" CLAUDE_* names has already gone stale once in this codebase. (vikunja#767)

## [0.3.1] — 2026-07-16

### Fixed
- `_run_pm2`: strip `NODE_CHANNEL_FD`, `NODE_CHANNEL_SERIALIZATION_MODE`, and `NODE_UNIQUE_ID` from the subprocess environment before invoking the `pm2` CLI. PM2 injects these for its own IPC channel; leaking them into a spawned `pm2` (Node.js) child causes a SIGABRT during teardown. (PM2-1, sibling to HLOPS-1)

## [0.3.0] — 2026-05-28

### Added
- 3 new tests: `created_at=0` in PM2 env returns `None`, `get_logs(lines=0)` clamped to 1, `get_logs(lines=-5)` clamped to 1.
- `pyproject.toml` with version field.

### Changed
- fastmcp pin updated to `>=3.2.4,<4`.

### Fixed
- `subprocess.run`: added `encoding="utf-8"` to avoid locale-dependent decoding on non-UTF-8 systems.
- `get_logs`: `lines` parameter is now clamped to `max(1, ...)` so zero or negative values do not reach PM2.

## [0.2.0] — 2026-04-20

### Added
- `reload_service` tool — graceful zero-downtime reload via `pm2 reload`
- `save` tool — persists PM2 process list to disk (`pm2 save`)
- `flush_logs` tool — clears log files for a named service (`pm2 flush <name>`)
- `get_status` tool — returns server metadata and PM2 health summary (host, port, pm2 version, service counts by status)
- `.env.example` with `MCP_HOST` and `MCP_PORT` documentation
- `requirements-dev.txt` tracking `pytest>=8.0.0`
- GitHub Actions CI: Python 3.11/3.12/3.13 test matrix + `pip-audit` dependency audit
- `_VALID_STATUS_FILTERS` allowlist with `ValueError` for invalid `status_filter` in `list_services`
- `_MAX_LOG_LINES = 500` cap in `get_logs` — excess lines silently clamped
- `TestStartService` (was missing), `TestReloadService`, `TestSave`, `TestFlushLogs`, `TestGetStatus` test classes (32 tests total, up from 19)

### Changed
- Entry point switched from argparse (`--host`/`--port` flags) to env vars (`MCP_HOST`, `MCP_PORT`) — defaults unchanged (127.0.0.1:8486). PM2 `ecosystem.config.js` `env:` block is now the recommended setup path.
- Pinned `fastmcp>=3.2.4` (was `>=3.0.0`) to pick up path traversal CVE fix in the SSE endpoint

### Migration

If you were starting the server with `pm2 start server.py -- --host 127.0.0.1 --port 8486`, switch to:

```bash
MCP_HOST=127.0.0.1 MCP_PORT=8486 pm2 start server.py --interpreter python3 --name pm2-mcp
```

Or use `ecosystem.config.js` — see README for the recommended snippet.

## [0.1.0] — 2026-04-19

Initial release. Read tools: `list_services`, `get_service`, `get_logs`. Write tools: `restart_service`, `stop_service`, `start_service`. Streamable-http transport on 127.0.0.1:8486.
