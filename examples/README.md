# Examples

Real configuration, not illustrations. Everything here is either the file forge actually
deploys or a transcript of a real diagnosis.

| File | What it is |
|---|---|
| [`../ecosystem.config.js`](../ecosystem.config.js) | **The deployed PM2 declaration.** Not copied here on purpose — see below |
| [`mcp-client.json`](mcp-client.json) | Client wiring for Claude Code and for a project `.mcp.json` |
| [`diagnose-crash-looping-service.md`](diagnose-crash-looping-service.md) | A worked diagnosis using `get_status`, `list_services`, `get_service` and `get_logs` |

## Why `ecosystem.config.js` isn't duplicated here

The repository root already contains the exact file used to run this service, including a
long comment explaining why its `env` block is empty and why that comment must not be
"tidied". Copying it into `examples/` would create a second version that drifts from the
first, and the copy is the one people would read.

To adapt it, three things are host-specific: `script` (the venv interpreter path), `cwd`, and
the log paths. Everything else — `interpreter: "none"`, `env: {}`, the restart policy —
should be kept as-is. The reasoning for each is in the file.

## Before you deploy

Read [`../docs/operations.md`](../docs/operations.md), in particular the rule that this
service must be started **by PM2 at boot, not from an interactive shell**. `pm2 start` ships
the calling shell's entire environment to the daemon, and `pm2 save` writes it to disk. That
is how a live credential ended up in a world-readable `dump.pm2` for nine days.
