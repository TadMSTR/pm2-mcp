# pm2-mcp

FastMCP server wrapping the PM2 CLI via subprocess. Provides structured read and limited write access to PM2 processes.

## What it does

Exposes PM2 process management as MCP tools — list, inspect, control lifecycle, and fetch recent logs for any PM2-managed service on forge.

## Tools

- `list_services(status_filter)` — All PM2 processes as summary dicts (name, pm_id, status, pid, uptime_ms, restarts, cpu_pct, memory_mb, exec_mode). Optional filter: `online`, `stopped`, `errored`.
- `get_service(name)` — Full detail for a named service.
- `get_logs(name, lines, include_errors)` — Recent log lines. `lines` defaults to 50, clamped to 1..500.
- `restart_service(name)` — Restart a service.
- `stop_service(name)` — Stop a service. Does not deregister it.
- `start_service(name)` — Resume a service **already registered** with PM2. Does not register a new process.
- `reload_service(name)` — Graceful zero-downtime reload.
- `save()` — Persist the current PM2 process list to disk.
- `flush_logs(name)` — Clear a service's log files.
- `get_status()` — Server metadata and PM2 health summary.

There is **no `delete_service`**, deliberately — recovering from a bad PM2 registration is an
operator task. This list said otherwise until 2026-09-09, along with naming `get_service_logs`
(the tool is `get_logs`) and describing reload as cluster-mode-only.

## Structure

```
src/pm2_mcp/server.py   The FastMCP server: all tools, _run_pm2(), _find_service(),
                        _clean_env(), _resolve_bind(), _configure_logging(),
                        HostOriginGuard, main() (the `pm2-mcp` entry point)
tests/test_pm2.py       Unit tests; _run_pm2 / subprocess.run mocked
tests/test_host_origin.py   Host/Origin check against the real Starlette app
tests/test_integration_pm2.py   Write verbs against a real pm2 and a private daemon
uv.lock                 The dependency set CI tests, audits and the deploy installs
```

## Key architecture decisions

- **`_run_pm2(*args)`** — all PM2 invocations go through this helper, which raises `RuntimeError` on non-zero exit. Do not shell out to PM2 directly in tool handlers.
- **`_find_service(name)`** — validates the service exists before any write operation (restart/stop/start/reload/flush) and before `get_logs` (since v0.5.0: `pm2 logs` takes ids, namespaces, `all` and options in the same position). Returns structured data from `pm2 jlist`. This is an allowlist: the name must already be a registered PM2 app.
- **`_clean_env()`** — the trust boundary. Strips PM2's IPC variables and anything matching the `CLAUDE` prefix from the environment handed to the `pm2` child. `pm2 start`/`restart --update-env` copy the caller's whole environment into the target app and `pm2 save` writes it to disk, so a regression here persists credentials silently (vikunja#767). Changes to this function or `_run_pm2` must pass `scripts/mutation-gate.sh`.
- **`_is_loopback(host)` / `_resolve_bind(argv, env)`** — the network trust boundary (vikunja#770). `_resolve_bind` resolves the bind with precedence argv > env > `127.0.0.1:8486`, per field, and refuses any non-loopback result by exiting non-zero. `_is_loopback` is in the mutation gate's function set; `_resolve_bind` deliberately is not, and the script says why. **Do not add an override flag** — the refusal is the mitigation that `docs/threat-model.md` §1 leans on, and an escape hatch removes it.
- **`_configure_logging()`** — must stay a function called from `main()`, never module-level or import-time code. `pyproject.toml` sets `fail_under = 100` and excludes the `__main__` block from coverage, so startup logic written there is untestable, invisible to coverage, and unreachable from a regression test. That is exactly how vikunja#772 shipped: the shadow log had no level and no handler for its entire life, and every test that "covered" it forced the level via `caplog.at_level(...)`, which production never has. If you add startup behaviour, extract it the same way and test it without forcing a level.
- **`HostOriginGuard`** — the DNS-rebinding check (v0.5.0). Loopback `Host` names and the bound address on any port (mcp's prefix wildcard), plus exact `PM2_MCP_ALLOWED_HOSTS` entries (`*` refuses to start); any non-empty `Origin` refused. It is handed to `mcp.run` by `main()`, and a test pins that it is. Never log the header values.
- **`PM2_MCP_ENV_MODE`** — `shadow` (default) or `enforce`; anything else makes `main()` refuse to start. **Do not flip it** as part of other work: that is Ted's decision on vikunja#771.
- **Dependencies** — fastmcp, and minimum versions on mcp and several transitives, each carrying the advisory it closes. `uv.lock` is the source of truth; CI installs from it and audits it. PM2 is already installed globally on forge. No auth — this server is intended for localhost use only, and the loopback bind is enforced. See `docs/threat-model.md`.

## Testing

```bash
uv sync --frozen --extra dev
uv run pytest                                # coverage floor comes from pyproject.toml
uv run pytest -m integration --no-cov        # real pm2; needs pm2 on PATH
```

**Deploying is not `git pull`.** The PM2 app runs the installed package from `.venv`, so a
change reaches it only after the venv is reinstalled from `uv.lock`
(`uv sync --frozen --no-dev --no-editable`). That is sysadmin's job, not this repo's.

## Git workflow

Branch before editing — do not commit directly to `main`.
