# Architecture

`pm2-mcp` is a single-file FastMCP server that wraps the `pm2` CLI. It is deliberately small:
one module, one subprocess helper, no persistent state, no database, no cache, and one
dependency (`fastmcp`).

The interesting part is not the structure — it is the **environment boundary**, because this
server shells out to a tool that copies its caller's environment into long-lived daemons. That
boundary is the subject of the last diagram, and of `docs/threat-model.md`.

---

## Overview

```mermaid
flowchart LR
    agent["MCP client\n(Claude Code, scoped-mcp)"]
    subgraph proc["pm2-mcp process"]
        direction TB
        fastmcp["FastMCP\nstreamable-http\n127.0.0.1:8486/mcp"]
        tools["10 tool functions\n4 read · 6 write"]
        helpers["_find_service\n_parse_summary"]
        boundary["_clean_env → _run_pm2"]
    end
    cli["pm2 CLI\n(Node.js)"]
    daemon["PM2 daemon\n~42 processes\n~/.pm2/dump.pm2"]

    agent -->|"JSON-RPC over HTTP"| fastmcp
    fastmcp --> tools
    tools --> helpers
    helpers --> boundary
    tools --> boundary
    boundary -->|"subprocess.run\n(list form, no shell)"| cli
    cli --> daemon
```

Every path to PM2 goes through `_run_pm2`, and `_run_pm2` is the only place `subprocess.run`
is called. That is what makes the environment scrub enforceable in one place rather than at
eleven call sites.

---

## Read path

```mermaid
sequenceDiagram
    participant A as MCP client
    participant T as list_services / get_service
    participant R as _run_pm2
    participant P as pm2 CLI

    A->>T: list_services(status_filter="online")
    T->>T: reject filter not in<br/>{online, stopped, errored}
    T->>R: _run_pm2("jlist")
    R->>P: subprocess.run(["pm2","jlist"], env=_clean_env())
    P-->>R: JSON on stdout
    R-->>T: CompletedProcess (raises RuntimeError if rc != 0)
    T->>T: _parse_summary per entry
    T-->>A: typed dicts — status, uptime_ms, cpu_pct, memory_mb
```

`_parse_summary` is where the raw `pm2 jlist` entry is narrowed to a fixed field set. This is
a deliberate reduction, not laziness: the raw entry contains `pm2_env`, which is the target
app's **entire environment**. Returning it wholesale would turn every read tool into a
credential disclosure. See `docs/threat-model.md`.

---

## Write path

```mermaid
sequenceDiagram
    participant A as MCP client
    participant W as restart_service (and stop/start/reload/flush)
    participant F as _find_service
    participant R as _run_pm2
    participant P as pm2 CLI

    A->>W: restart_service("svc-a")
    W->>F: _find_service("svc-a")
    F->>R: _run_pm2("jlist")
    R-->>F: process list
    alt name not in the live PM2 process list
        F-->>W: None
        W-->>A: {"ok": false, "error": "service 'svc-a' not found"}
        Note over W,P: pm2 is never invoked with the name
    else name is a registered app
        F-->>W: process entry
        W->>R: _run_pm2("restart", "svc-a")
        R->>P: subprocess.run(["pm2","restart","svc-a"], env=_clean_env())
        alt non-zero exit
            R-->>W: raise RuntimeError
            W-->>A: {"ok": false, "error": "pm2 restart svc-a failed (rc=...): ..."}
        else
            W-->>A: {"ok": true, "name": "svc-a"}
        end
    end
```

The `_find_service(name) is None` guard is a genuine allowlist, not a sanitiser: the name must
already be a registered PM2 app before it reaches `pm2`. Combined with the list form of
`subprocess.run` — no `shell=True` anywhere — there is no injection surface in the name.

Both `{"ok": false}` outcomes look identical to a caller, which is why the tests for the
second one assert the pm2 verb was *attempted* rather than only checking the returned dict.

---

## Module lifecycle

```mermaid
stateDiagram-v2
    [*] --> Import: python server.py
    Import --> Registered: FastMCP() constructed,<br/>@mcp.tool decorators run
    Registered --> Serving: mcp.run(streamable-http)<br/>host/port from MCP_HOST/MCP_PORT<br/>(defaults 127.0.0.1:8486)
    Serving --> Serving: tool call → subprocess → response<br/>(no state retained between calls)
    Serving --> [*]: SIGTERM from PM2

    note right of Serving
        Stateless by construction.
        Every call re-reads `pm2 jlist`;
        nothing is cached, so there is no
        staleness window and no invalidation
        logic to get wrong.
    end note
```

The server is itself a PM2 service, so it appears in its own `list_services` output — and can
in principle be asked to restart itself. It has no `delete` verb, which is why recovering from
a bad `pm2` registration is an operator task rather than something an agent can do through
this server. See `docs/operations.md`.

---

## Scope and security enforcement — the environment boundary

This diagram is the reason the others are short. `pm2 start` and
`pm2 restart --update-env` hand the **calling process's entire environment** to the PM2 daemon
as the base env for the target app, and `pm2 save` writes it to `~/.pm2/dump.pm2` on disk. An
ecosystem file's `env` block can only *add* keys on top of that — it cannot remove one.

So whatever this process is carrying when it invokes `pm2` can be frozen into an unrelated
long-lived service and persisted to a file. That is not hypothetical: it is vikunja#767, where
a live `CLAUDE_CODE_OAUTH_TOKEN` sat in a world-readable `dump.pm2` for nine days.

```mermaid
flowchart TB
    subgraph parent["pm2-mcp process environment"]
        direction LR
        keep["PATH, HOME, LANG, TZ …"]
        ipc["NODE_CHANNEL_FD\nNODE_CHANNEL_SERIALIZATION_MODE\nNODE_UNIQUE_ID"]
        claude["CLAUDECODE, CLAUDE_CODE_OAUTH_TOKEN,\nCLAUDE_* — inherited if started\nfrom a session shell"]
    end

    clean["_clean_env()"]
    child["env= handed to subprocess.run"]
    daemon["PM2 daemon → target app env\n→ ~/.pm2/dump.pm2 on `pm2 save`"]

    keep --> clean
    ipc --> clean
    claude --> clean
    clean -->|"kept"| child
    clean -.->|"dropped: PM2 IPC vars\n(SIGABRT on Node teardown)"| x1["✗"]
    clean -.->|"dropped: CLAUDE* prefix\n(live credentials)"| x2["✗"]
    child --> daemon
```

Two independent reasons to scrub, with different failure modes:

| Dropped | Why | Failure if it regresses |
|---|---|---|
| `NODE_CHANNEL_FD` and friends | PM2 sets them for its own IPC channel; a Node child inherits a stray fd | `SIGABRT` during teardown — loud, immediate (vikunja#80) |
| `CLAUDE*` | Session credentials inherited when started from an interactive shell | Silent: a credential persisted to disk, discovered nine days later (vikunja#767) |

The second failure mode is why this boundary carries a mutation gate on top of ordinary tests
(`scripts/mutation-gate.sh`). It is also why the shipped `ecosystem.config.js` declares
`env: {}` and says so at length: the app requires nothing from the environment, and stating
that explicitly is what makes "no env block" unambiguous.

**Matching is by prefix, not an enumerated list.** An exact list of known `CLAUDE_*` names has
already gone stale once in this codebase, and every Claude Code release is free to add more
without touching this file.

---

## What is not here

- **No authentication.** Any caller that reaches port 8486 can stop or restart any PM2 process
  on the host. This is a deliberate localhost-only posture, documented rather than fixed —
  see `docs/threat-model.md`.
- **No audit log.** A privileged tool that can stop any of ~42 processes currently emits no
  record when it does. Acknowledged gap, not in scope for the Showcase promotion.
- **No `delete` verb**, and no way to register a new process. `start_service` resumes an
  already-registered app.
- **No caching or persistence.** Deliberate; see the lifecycle note above.
