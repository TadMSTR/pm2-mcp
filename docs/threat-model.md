# Threat model

What `pm2-mcp` protects, what it deliberately does not, and where the sharp edges are.

This document states the posture rather than proposing changes to it. Where something is a
known gap it says so plainly; an undocumented gap is worse than an accepted one.

---

## What the server is

A localhost-bound HTTP server that can **stop, start, restart or reload any process the PM2
daemon manages**, and read their logs. On a typical forge host that is ~42 processes,
including every agent's own MCP broker.

It runs as an ordinary user, not root, and inherits exactly that user's authority over PM2.

## Trust boundaries

| Boundary | Who is trusted | Enforcement |
|---|---|---|
| The MCP port (`127.0.0.1:8486`) | **Everyone who can reach it** | None — no authentication |
| Tool arguments → `pm2` argv | Nobody | `_find_service` allowlist + list-form `subprocess.run` |
| This process's env → `pm2` child | Nothing | `_clean_env()` |
| `pm2 jlist` output → tool response | n/a | `_parse_summary` field allowlist |

---

## 1. No authentication on the MCP endpoint

**This is the largest single risk and it is accepted, not mitigated.**

Any client that can open a socket to port 8486 can call every tool, including the write
tools. There is no token, no client identity, and no per-tool authorisation. A caller that
reaches the port can stop every service on the host, including the broker that another agent
is using, and including `pm2-mcp` itself.

What limits it:

- **The loopback bind is enforced in code, not merely defaulted** (vikunja#770, v0.4.0). The
  resolved host — from `--host`, `MCP_HOST`, or the default — must be `127.0.0.0/8`, `::1`
  or `localhost`. Anything else, including `0.0.0.0` and any hostname, exits non-zero
  instead of starting. There is deliberately no override flag.
- Anything already running locally as this user could invoke `pm2` directly anyway, so
  against a *local* attacker the server grants no authority they did not already have.

That second point is the actual justification, and it has a limit worth naming: it holds for
code running **as this user**. It does not hold for a lower-privileged local process, or for
a container that can reach the host loopback.

Before v0.4.0 the first point read "binds `127.0.0.1` by default … without someone
deliberately proxying it or changing `MCP_HOST`", and that was the weaker claim: a one-word
edit to a config file was enough to expose it. Changing `MCP_HOST` is no longer a way out of
this boundary. Putting a reverse proxy in front still is — see below.

**Do not put this behind a public reverse proxy.** If it ever needs to be reachable
off-host, it needs real authentication first — not a proxy ACL.

## 2. The environment handed to the `pm2` CLI

The most consequential thing this server does is invoke a Node CLI, because of how PM2
propagates environments:

- `pm2 start` and `pm2 restart --update-env` pass the **calling process's entire
  environment** to the daemon as the base environment for the target app.
- An ecosystem file's `env` block only *adds* keys on top of that. It cannot remove one.
- `pm2 save` writes the result to `~/.pm2/dump.pm2` on disk.

So a variable present in this process at the moment of the call can be frozen into an
unrelated long-lived service and persisted to a file that is often group-readable.

This happened: `CLAUDE_CODE_OAUTH_TOKEN`, a live credential, was inherited from an
interactive session shell and sat in a mode-664 `dump.pm2` for nine days (vikunja#767).

`_clean_env()` is the mitigation. It drops:

- the three PM2 IPC variables (`NODE_CHANNEL_FD`, `NODE_CHANNEL_SERIALIZATION_MODE`,
  `NODE_UNIQUE_ID`) — otherwise a Node child inherits a stray fd and `SIGABRT`s on teardown
- anything matching the `CLAUDE` **prefix**

Prefix rather than a list is deliberate: an enumerated list of "known" `CLAUDE_*` names has
already gone stale once here, and new releases add names without touching this repo.

**Residual risk, and the allowlist that addresses it.** A denylist can only refuse what it
has been told to name. Any *other* secret in the parent environment — `GITHUB_TOKEN`,
`ANTHROPIC_API_KEY`, anything an operator's shell happened to source — passes straight
through.

An allowlist (vikunja#610) is implemented and currently runs in **shadow mode**: it computes
what it would keep, logs the names it would withhold for each pm2 command, and changes
nothing. Set `PM2_MCP_ENV_MODE=enforce` to apply it.

Measured on the live process before enforcing: 20 environment variables, of which 13 would be
withheld and **none** is secret-shaped. So enforcement is not removing credentials that are
leaking today — it removes the possibility of tomorrow's. The evidence that this matters is
in the withheld list itself, which contains `SSH_CLIENT`, `SSH_CONNECTION` and
`XDG_SESSION_*`: this process was started from an interactive session, which is precisely the
path that produced #767.

Until enforcement is on, the protection is operational rather than structural: the shipped
`ecosystem.config.js` declares `env: {}`, and the service is meant to start from PM2 at boot.
A `pm2 start` run by hand from a session that has sourced a secrets file re-contaminates it,
and nothing in the code prevents that.

## 3. Service-name handling

Not an injection surface, and the README's claim to that effect is accurate:

- Every write tool calls `_find_service(name)` first, which requires the name to match an app
  **already registered with PM2**. An unknown name returns `{"ok": false}` and `pm2` is never
  invoked with it. This is an allowlist, not sanitisation.
- `subprocess.run` is always called with a list and never `shell=True`, so shell
  metacharacters in a name have no meaning even if one somehow reached the call.

The residual authority is real but bounded: a caller can act on any *registered* service.
Given there is no authentication (§1), assume a caller who reaches the port can operate on
every service on the host.

## 4. Information disclosure through read tools

`pm2 jlist` returns each app's full `pm2_env`, which is that app's **entire environment** —
routinely including credentials belonging to other services.

`_parse_summary` narrows every response to a fixed field set (name, pm_id, status, pid,
uptime_ms, restarts, cpu_pct, memory_mb, exec_mode), and `get_service` adds only paths and
`created_at`. **No tool returns `pm2_env`.** That reduction is a security control, not
formatting — widening it, or adding a "raw" passthrough tool, would turn every read tool into
a cross-service credential disclosure.

`get_logs` returns application log content, which can contain whatever those applications
log. pm2-mcp does not filter it.

## 5. Known gaps

| Gap | Status |
|---|---|
| No authentication on the MCP port | Accepted, documented above |
| `_clean_env` is a denylist, not an allowlist | Tracked — vikunja#610 |
| No audit log of privileged actions | **Open.** A tool that can stop any of ~42 processes writes no record when it does. Reconstruction depends on PM2's own restart counters |
| Self-management | The server can restart itself; it has no `delete` verb, so a broken registration needs an operator |

## Out of scope

Vulnerabilities in PM2 itself, in the host, or in the MCP transport. See
[SECURITY.md](../SECURITY.md) for reporting and for the full scope statement.
