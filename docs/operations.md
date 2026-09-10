# Operator guide

Running, checking and recovering `pm2-mcp`.

---

## Deploying

The server runs as a PM2 app defined by the repository's own `ecosystem.config.js`:

```bash
pm2 start ecosystem.config.js --only pm2-mcp
pm2 save
```

Read the comment block in that file before editing it. Two things there are load-bearing:

- **`env: {}` is deliberate.** It asserts that the app requires nothing from the environment,
  which is true — the only two variables the code reads are `MCP_HOST` and `MCP_PORT`, and
  both have defaults. Do not add a shared-secrets loader: PM2 would write every key into
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

A bind on `0.0.0.0` means `MCP_HOST` has been set somewhere. That is a security problem, not
a configuration preference — see [threat-model.md](threat-model.md) §1.

### Verifying a deploy actually took

"Shipped" and "running" have diverged in this repo's history, so compare rather than assume:

```bash
stat -c '%y' server.py
pm2 jlist | python3 -c "
import json,sys,datetime
for a in json.load(sys.stdin):
    if a['name']=='pm2-mcp':
        print(datetime.datetime.fromtimestamp(a['pm2_env']['pm_uptime']/1000))"
```

**The restart timestamp must be later than the file mtime.** If it isn't, the process is
still running the old code no matter what the repository says.

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
Measured on this host, 13 variables would be withheld and none is required: `pm2 jlist`,
`pm2 --version` and `pm2 logs` were each verified working with only the seven allowlisted
variables present. `PM2_JSON_PROCESSING` and `PM2_USAGE` appear in the withheld list and are
the two worth watching, since pm2 sets them itself.

To enforce, change the `_ENV_MODE` default in `server.py` and redeploy. It is deliberately
**not** wired through `ecosystem.config.js`'s `env` block — that block is empty on purpose,
and flipping a security posture should be a reviewable code diff rather than an environment
variable on a service whose whole declaration says it needs none.

## Recovery

| Symptom | What to do |
|---|---|
| Tools time out, process `online` | `pm2 restart pm2-mcp`, then re-check the bind with `ss` |
| Crash loop (`restart_time` climbing) | `pm2 logs pm2-mcp --nostream --lines 100`. Usually the venv interpreter path in `ecosystem.config.js` no longer exists |
| Port 8486 held by something else | `ss -tlnp \| grep 8486`. The app will crash-loop until the port is free |
| Bad PM2 registration (wrong args, wrong env) | Needs `pm2 delete pm2-mcp` then `pm2 start ecosystem.config.js --only pm2-mcp`. **This server cannot do it** — there is no `delete` verb, deliberately |

A `pm2 delete` + `pm2 start` cycle re-reads `ecosystem.config.js`; a plain `pm2 restart` does
**not**. Changes to that file therefore have no effect until someone does the full cycle —
and when they do, the "start it from PM2, not a session shell" rule above applies with full
force, because that is the moment a fresh environment is captured.

## Upgrading

```bash
git -C /path/to/pm2-mcp pull
pm2 restart pm2-mcp
```

Then run the deploy-verification check above. `restart` is enough for code changes;
`ecosystem.config.js` changes need the delete/start cycle.

## Logs

`ecosystem.config.js` merges stdout and stderr into a single timestamped file. Either:

```bash
pm2 logs pm2-mcp --nostream --lines 100
```

or, through the server itself, `get_logs(name="pm2-mcp")`.
