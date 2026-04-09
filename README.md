# pm2-mcp

An MCP server that gives agents structured read and limited write access to PM2 services. Built with FastMCP, transport is streamable-http bound to localhost.

I built this because every other approach to PM2 inspection from an agent involves either raw shell access (homelab-ops `run_command`) or scraping human-readable `pm2 status` output. This server speaks directly to `pm2 jlist`, returns typed fields, and validates service names before issuing any write operations.

## Tools

### Read

| Tool | Description |
|------|-------------|
| `list_services` | List all PM2 services. Optional `status_filter`: `"online"`, `"stopped"`, or `"errored"`. |
| `get_service` | Full detail for one service by name — script path, cwd, args, log file paths, created_at, plus all summary fields. |
| `get_logs` | Tail recent log output. `lines` defaults to 50; `include_errors` defaults to `true`. |

### Write

| Tool | Description |
|------|-------------|
| `restart_service` | Restart a service. Validates the name first — returns `{ok: false}` if not found. |
| `stop_service` | Stop a service. Does not remove it from the PM2 process list. |
| `start_service` | Resume a stopped service already registered in PM2. Does not register new processes. |

### Response shape — `list_services`

```json
[
  {
    "name": "my-service",
    "pm_id": 12,
    "status": "online",
    "pid": 18432,
    "uptime_ms": 3720000,
    "restarts": 0,
    "cpu_pct": 0.2,
    "memory_mb": 48.5,
    "exec_mode": "fork_mode"
  }
]
```

`get_service` extends this with `script`, `cwd`, `args`, `log_file`, `error_file`, and `created_at`.

---

## Setup

### Requirements

- Python 3.11+
- PM2 installed and in PATH for the user running the server
- `fastmcp>=3.0.0` (see `requirements.txt`)

```bash
pip install -r requirements.txt
```

### Run as a PM2 process (recommended)

```bash
pm2 start server.py --interpreter python3 --name pm2-mcp -- --host 127.0.0.1 --port 8486
pm2 save
```

The server manages itself like any other PM2 service — it will appear in its own `list_services` output.

### Run directly

```bash
python server.py --host 127.0.0.1 --port 8486
```

---

## Wiring to Claude Code

Add to `~/.claude/settings.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "pm2": {
      "type": "streamable-http",
      "url": "http://127.0.0.1:8486/mcp"
    }
  }
}
```

---

## Security

The server binds to `127.0.0.1` by default. Any client that can reach port 8486 can restart or stop services — there is no authentication. This is intentional for local agent use: keep it localhost-only and don't proxy it externally.

The write tools (`restart_service`, `stop_service`, `start_service`) validate service names against the live PM2 process list before acting. An unrecognized name returns `{ok: false, error: "service '...' not found"}` without touching PM2.

---

## Testing

```bash
pip install -r requirements.txt pytest
pytest tests/ -v
```

All tests mock `_run_pm2` — no PM2 installation required.

---

## License

MIT
