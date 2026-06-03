# pm2-mcp

FastMCP server wrapping the PM2 CLI via subprocess. Provides structured read and limited write access to PM2 processes.

## What it does

Exposes PM2 process management as MCP tools — list, inspect, control lifecycle, and fetch recent logs for any PM2-managed service on forge.

## Tools

- `list_services(status_filter)` — All PM2 processes as `ServiceSummary` dicts (name, pm_id, status, pid, uptime_ms, restarts, cpu_pct, memory_mb, exec_mode). Optional filter: `online`, `stopped`, `errored`.
- `get_service(name)` — Full detail for a named service.
- `get_service_logs(name, lines, log_type)` — Recent stdout/stderr log lines (max 500).
- `restart_service(name)` — Restart a service.
- `stop_service(name)` — Stop a service.
- `start_service(name)` — Start a stopped service.
- `reload_service(name)` — Zero-downtime reload (cluster mode only).
- `delete_service(name)` — Remove a service from the PM2 process list.

## Structure

```
server.py    Single-file FastMCP server — all tools, _run_pm2(), _find_service()
```

## Key architecture decisions

- **`_run_pm2(*args)`** — all PM2 invocations go through this helper, which raises `RuntimeError` on non-zero exit. Do not shell out to PM2 directly in tool handlers.
- **`_find_service(name)`** — validates the service exists before any write operation (restart/stop/start/reload/delete). Returns structured data from `pm2 jlist`.
- **No external dependencies beyond fastmcp** — PM2 is already installed globally on forge. No auth — this server is intended for localhost use only.

## Testing

```bash
pip install -r requirements-dev.txt
pytest
```

## Git workflow

Branch before editing — do not commit directly to `main`.
