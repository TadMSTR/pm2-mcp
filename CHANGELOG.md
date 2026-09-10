# Changelog

All notable changes to pm2-mcp are documented here.

## [Unreleased]

### Added
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

### Changed
- Coverage floor set to **100%** and enforced from `pyproject.toml` (measured 100.00%, 49
  tests). It was previously enforced nowhere at all.
- `pytest.ini` folded into `pyproject.toml`; the unused `dev` extra dropped in favour of
  `requirements-dev.txt` as the single source for dev dependencies.
- `pip-audit` now runs with `--strict`.
- Server instructions said this server manages PM2 services on **claudebox**; it runs on forge.

### Fixed
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
- Tests asserting on the scrubbed environment no longer render the whole environment on
  failure. `assert name not in env` makes pytest print every variable and value, and on forge
  that ambient environment carries real credentials.

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
