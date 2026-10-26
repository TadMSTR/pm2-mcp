# Operator guide

Running, checking and recovering `pm2-mcp`.

---

## Deploying

The server runs as a PM2 app defined by the repository's own `ecosystem.config.js`, which
runs the venv's `pm2-mcp` entry point. Install the package into that venv from the lock,
non-editable, first: the running code is then the installed package, not the checkout.

```bash
uv sync --frozen --no-dev --no-editable
uv pip freeze -q -p .venv | grep -v '^pm2-mcp ' \
  | uvx -q --from pip-audit==2.10.1 pip-audit --strict -r /dev/stdin --no-deps --disable-pip
```

Then start it with the clean-start command and check from
[Starting it cleanly](#starting-it-cleanly), and only then `pm2 save`. Never a bare
`pm2 start` from the shell you happen to be in.

The audit line checks what is actually installed, which is the set that runs. CI audits
the lock; this checks that the venv matches it. The project's own line is filtered out
because it is a `file://` requirement, which `--strict` refuses (it is not on PyPI). The venv
has no `pip`, so `uv pip freeze` is the way to read it.

Read the comment block in that file before editing it. Two things there are load-bearing:

- **`env: {}` is deliberate.** It asserts that the app requires nothing from the environment,
  which is true — the variables the code reads (`MCP_HOST`, `MCP_PORT`,
  `PM2_MCP_ALLOWED_HOSTS`, `PM2_MCP_ENV_MODE`) all have safe defaults when unset. Do not add a shared-secrets loader: PM2 would write every key into
  `~/.pm2/dump.pm2` at the next `pm2 save`.
- **`env: {}` does not scrub inheritance.** PM2 passes the parent environment through
  regardless. Starting the app from an interactive shell that has sourced a secrets file
  contaminates it, and no code in this repo prevents that.

### Always start it from PM2 at boot, not from a session shell

This is the single most important operational rule for this service, and it is the direct
cause of vikunja#767. `pm2 start` ships the calling shell's whole environment to the daemon,
so a hand-run start from an agent session freezes that session's credentials into the app —
and `pm2 save` then writes them to disk. `_clean_env()` protects the *children* pm2-mcp
spawns; it cannot protect pm2-mcp's own process environment, which is set by whoever started
it.

Since v0.4.0 there is a **second, independent reason**, and it is new. vikunja#772 fixed the
logger, so shadow mode now actually writes to disk — which means the *names* of every
withheld variable in the starting shell's environment land in `/home/ted/logs/pm2-mcp.log`.
Measured 2026-09-10, same code, two different parents:

| Started by | Names in the shadow line | Of which secret-shaped |
|---|---|---|
| PM2 at boot | 62 | **0** |
| An interactive agent shell | 84 | **12** (`TASK_QUEUE_TOKEN_*`, `LANGFUSE_SECRET_KEY`, …) |

Both rows are counts from the real log line, taken after the v0.4.0 redeploy.

> **62, not 64.** You will see 64 if you compute `present − allowlisted` yourself against
> `/proc/<pid>/environ` (71 − 7 on this host). The log reports the smaller number because it
> lists only what enforcement would withhold *additionally* — the shipped denylist has already
> removed `NODE_CHANNEL_FD` and `NODE_CHANNEL_SERIALIZATION_MODE` (vikunja#80, fixed in
> v0.3.1). Both figures are correct; 62 is the one to reason from, because the enforce
> decision is about the delta from current behaviour.
>
> Neither is a constant. The count describes the *parent* environment, so it moves when the
> parent does — which is exactly why the log records variable **names** rather than a count.

Values are never logged and there are canary tests pinning that. But variable *names* are
still a disclosure, and the difference between those two rows is entirely who ran the start
command. The log is mode 644 in the service account's home.

### Starting it cleanly

`pm2 start` copies the **calling process's** environment into the app, and `pm2 save` writes
it to `~/.pm2/dump.pm2`. "Start it from a clean shell" is only a rule until it is a command, so
here is the command. Run it from the repository checkout:

```bash
env -i HOME="$HOME" PATH=/usr/bin:/bin PM2_HOME="${PM2_HOME:-$HOME/.pm2}" \
  pm2 start ecosystem.config.js --only pm2-mcp
```

`env -i` empties the environment and the three assignments put back only what pm2 and the
server need. The shell expands `$HOME` and `$PM2_HOME` before `env -i` runs. `PATH` must
reach both `pm2` and `node`; widen it (say, `/usr/local/bin:/usr/bin:/bin`) if they live
elsewhere on your host.

**Check before you save.** This prints the names (never the values) of everything the app
captured beyond the clean set, and exits 1 if there is anything:

```bash
pm2 jlist | python3 -c '
import json, sys
allowed = {"HOME", "PATH", "PM2_HOME", "PM2_USAGE", "PM2_JSON_PROCESSING",
           "PM2_INTERACTOR_PROCESSING", "pm2-mcp", "unique_id"}
[app] = [a for a in json.load(sys.stdin) if a["name"] == "pm2-mcp"]
extra = sorted(set(app["pm2_env"].get("env", {})) - allowed)
print("captured env beyond the clean set:", ", ".join(extra) or "none")
sys.exit(1 if extra else 0)'
```

The extra names are pm2's own (`PM2_USAGE`, `PM2_INTERACTOR_PROCESSING`), plus the app name
and `unique_id`, which pm2 adds to every app. Exit 0: `pm2 save`. Exit 1: **do not save.**
Run `pm2 delete pm2-mcp`, find out where the extra variables came from, and start again. Measured
2026-10-25 against a throwaway daemon, pm2 6.0.14: the command above gives `none`. A plain
`pm2 start` from an agent session gives 61 captured variables, `CLAUDE_*` among them.

## Choosing the bind address

Resolved with precedence **command-line flags > `MCP_HOST`/`MCP_PORT` > `127.0.0.1:8486`**,
per field. Passing only `--host` leaves the port to the environment or the default. An empty
environment variable is treated as unset; an explicitly empty `--host ""` is refused, because
an empty host is a wildcard bind at the socket layer.

`ecosystem.config.js` passes `--host 127.0.0.1 --port 8486` explicitly. Those flags are live
as of v0.4.0 (vikunja#770) — before that `server.py` had no argument parsing, so the same two
flags sat in `args` doing nothing while the process bound 8486 from the hardcoded default.
They agreed by coincidence, which is why it went unnoticed for months.

### Non-loopback binds are refused

Only `127.0.0.0/8`, `::1` and `localhost` are accepted. Anything else — `0.0.0.0`, a LAN
address, any hostname — exits non-zero with a message rather than starting:

```
pm2-mcp: error: refusing to bind '0.0.0.0': pm2-mcp has no authentication and its write
verbs can stop any PM2 process on the host, so it binds loopback only ...
```

There is deliberately no override flag. If you genuinely need a non-loopback bind, that is a
reviewable change to this repo with its own audit — not an operational toggle. Put a
reverse proxy in front of the loopback port instead, and give it authentication.

If you see this error after a config change, the fix is the config, not the guard.

### Changing `ecosystem.config.js` needs more than `pm2 restart`

`pm2 restart` re-execs the installed entry point, `.venv/bin/pm2-mcp`. Since v0.5.0 that is
the package installed in the venv, so a change made only in the checkout does **not** land
on a restart. Reinstall first (`uv sync --frozen --no-dev --no-editable`). `restart` also
re-reads its configuration from PM2's own dump, not from `ecosystem.config.js`, so **config**
changes do not land either. Measured 2026-09-10: the file was edited 2026-09-09 21:17 and the process restarted
2026-09-10 05:53, yet the live process still carried `args` the file no longer declared.

Applying a change to that file needs `pm2 delete pm2-mcp`, then the clean-start command and
check from [Starting it cleanly](#starting-it-cleanly), then `pm2 save`. Never a bare
`pm2 start`: it re-captures the calling shell's entire environment, and since v0.4.0 the
shadow log records the names of everything withheld.

## Health check

```bash
pm2 jlist | python3 -c "
import json,sys,datetime
for a in json.load(sys.stdin):
    if a['name']=='pm2-mcp':
        e=a['pm2_env']
        print('status  :', e['status'])
        print('restarts:', e['restart_time'])
        print('started :', datetime.datetime.fromtimestamp(e['pm_uptime']/1000))"
```

Confirm it is actually listening:

```bash
ss -tlnp | grep 8486        # expect 127.0.0.1:8486, and nothing on 0.0.0.0
```

A bind on `0.0.0.0` cannot happen through this code: a non-loopback host refuses to start. If
`ss` shows one, something other than this server holds the port. See
[threat-model.md](threat-model.md) §1.

Then check the Host/Origin guard over the wire (MCP `initialize` changes nothing):

```bash
B='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"check","version":"0"}}}'
for h in localhost:8486 evil.example:8486; do
  curl -s -o /dev/null -w "$h %{http_code}\n" -H 'content-type: application/json' \
    -H 'accept: application/json, text/event-stream' -H "Host: $h" -d "$B" http://127.0.0.1:8486/mcp
done
# expect: localhost:8486 200, evil.example:8486 421
```

### Verifying a deploy actually took

"Shipped" and "running" have diverged in this repo's history, so compare rather than assume.
Since v0.5.0 the running code is the package installed in `.venv`, so compare against that,
not the checkout:

```bash
.venv/bin/python -c "import importlib.metadata as m; print(m.version('pm2-mcp'))"
stat -c '%y' .venv/lib/python3*/site-packages/pm2_mcp/server.py
pm2 jlist | python3 -c "
import json,sys,datetime
for a in json.load(sys.stdin):
    if a['name']=='pm2-mcp':
        print(datetime.datetime.fromtimestamp(a['pm2_env']['pm_uptime']/1000))"
```

**The restart timestamp must be later than the installed file's mtime.** If it isn't, the
process is still running the old code no matter what the repository says. And the installed
version must be the one you meant to deploy: a `git pull` without a reinstall changes the
checkout and nothing else.

**One false positive to know about, because it has already happened once.** `mtime` cannot
tell a code change from a comment change. A reinstall after a docs-only commit that touched
`server.py` — say, correcting a `SECURITY[accepted]` comment — rewrites the mtime and makes
this check report a stale deploy even though the running process is functionally identical.

Before scheduling a restart on the strength of this check alone, confirm there is an actual
behavioural difference:

```bash
# What changed in server.py since the process started, ignoring comments and blank lines?
git diff <sha-deployed>..HEAD -- src/pm2_mcp/server.py \
  | grep '^[+-]' | grep -vE '^(\+\+\+|---)' | grep -vE '^[+-]\s*(#|$)'
```

Empty output means the restart would be a no-op: leave it, and the next real deploy clears
the discrepancy. Non-empty means the check is telling the truth and a restart is needed.

This is worth the extra step in both directions. A needless restart of *this* service is not
free — see the rule above about what re-capturing an environment can cost.

### Confirming the environment is clean

```bash
pm2 jlist | python3 -c "
import json,sys
for a in json.load(sys.stdin):
    ks=[k for k in a['pm2_env'] if k.startswith('CLAUDE')]
    if ks: print(a['name'], ks)"
```

Expect **no line for `pm2-mcp`**. Other apps may well appear — that is a separate, wider
issue (vikunja#768) and not a failure of this service.

Note this prints key *names* only. Don't reach for a variant that prints values.

## The environment allowlist (shadow mode)

`_clean_env` computes a named allowlist for the environment handed to the `pm2` CLI. It
currently runs in **shadow mode**: behaviour is unchanged and it only reports what
enforcement would remove.

```bash
pm2 logs pm2-mcp --nostream --lines 200 | grep 'env-allowlist shadow'
```

Each line names the pm2 command and the variables that would be withheld:

```
env-allowlist shadow: pm2 restart svc-a would lose DBUS_SESSION_BUS_ADDRESS,MOTD_SHOWN,...
```

Names only — never values, and never a bare count. A count would describe the parent
environment rather than the command, so it would be identical on every call and would predict
nothing about which callers break.

**Before enforcing, read these lines and check nothing the `pm2` CLI needs is on them.**
Measured on this host, `pm2 jlist`, `pm2 --version` and `pm2 logs` were each verified working
with only the allowlisted variables present. Since v0.5.0 the write verbs are covered too:
`tests/test_integration_pm2.py` runs restart, stop, start, reload, flush and save against a
real pm2 6.0.14 under both modes, with `PM2_JSON_PROCESSING`, `PM2_USAGE`,
`NODE_APP_INSTANCE` and the IPC pair in the parent, and each verb works and takes effect
(vikunja#771).

The mode comes from `PM2_MCP_ENV_MODE`, read once at startup: unset means `shadow`,
`enforce` enforces, and **any other value refuses to start**. It can be flipped by setting
that variable or by a reviewed change to the `_ENV_MODE` default in `src/pm2_mcp/server.py`;
either way it needs a redeploy. `ecosystem.config.js`'s `env` block is empty on purpose, so
the variable would be its first entry. `get_status` reports the running mode. The flip
itself is an operator decision, not something to do as part of another change.

## Recovery

| Symptom | What to do |
|---|---|
| Tools time out, process `online` | `pm2 restart pm2-mcp`, then re-check the bind with `ss` |
| Crash loop (`restart_time` climbing) | `pm2 logs pm2-mcp --nostream --lines 100`. Usually `.venv/bin/pm2-mcp` does not exist (the venv was not reinstalled, or holds an editable install from before v0.5.0), or `PM2_MCP_ENV_MODE` holds an unrecognised value |
| Port 8486 held by something else | `ss -tlnp \| grep 8486`. The app will crash-loop until the port is free |
| Bad PM2 registration (wrong args, wrong env) | Needs `pm2 delete pm2-mcp`, then the clean start and check from [Starting it cleanly](#starting-it-cleanly). **This server cannot do it** — there is no `delete` verb, deliberately |

A `pm2 delete` + `pm2 start` cycle re-reads `ecosystem.config.js`; a plain `pm2 restart` does
**not**. Changes to that file therefore have no effect until someone does the full cycle —
and when they do, the "start it from PM2, not a session shell" rule above applies with full
force, because that is the moment a fresh environment is captured.

## Upgrading

### From v0.4.x to v0.5.0: a launch-path change, so no `pm2 restart`

v0.4.x ran `.venv/bin/python3 server.py` from the checkout; v0.5.0 runs `.venv/bin/pm2-mcp`.
PM2 holds the old definition in its dump, and once the checkout is at v0.5.0 there is no
top-level `server.py`, so **a `pm2 restart` at any point after the pull crash-loops**. In
this order:

```bash
git -C /path/to/pm2-mcp pull
cd /path/to/pm2-mcp && uv sync --frozen --no-dev --no-editable
# the deployed-venv audit from "Deploying" above: expect no known vulnerabilities
pm2 delete pm2-mcp
# the clean-start command and check from "Starting it cleanly"; exit 0, then:
pm2 save
```

Then run the deploy-verification checks above, including the Host check over the wire.

### Later upgrades within v0.5.x

```bash
git -C /path/to/pm2-mcp pull
cd /path/to/pm2-mcp && uv sync --frozen --no-dev --no-editable
pm2 restart pm2-mcp
```

The reinstall is what changes the running code; `restart` alone would re-exec the old
installed package. An `ecosystem.config.js` change needs the delete and clean-start cycle
instead of `restart`.

## Logs

`ecosystem.config.js` merges stdout and stderr into a single timestamped file. Either:

```bash
pm2 logs pm2-mcp --nostream --lines 100
```

or, through the server itself, `get_logs(name="pm2-mcp")`.
