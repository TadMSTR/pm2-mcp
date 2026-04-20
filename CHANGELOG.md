# Changelog

All notable changes to pm2-mcp are documented here.

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
