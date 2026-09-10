# Worked example: diagnosing a crash-looping service

A transcript of the sequence that actually finds the problem, and — just as usefully — the
fields to look at rather than the ones that catch the eye first.

The scenario: something is wrong with `my-api`. Nobody knows what yet.

---

## 1. Is PM2 itself healthy?

Start here, not with the service. `get_status` distinguishes "your service is broken" from
"pm2-mcp cannot talk to PM2 at all", and those have completely different fixes.

```
get_status()
```

```json
{
  "host": "127.0.0.1",
  "port": 8486,
  "pm2_version": "5.4.2",
  "service_count": 42,
  "status_counts": { "online": 40, "errored": 1, "stopped": 1 }
}
```

`pm2_version` is the tell. If it comes back `"unknown"`, the `pm2 --version` call failed and
every other tool is going to fail too — that is a PM2 or PATH problem on the host, and
nothing about `my-api`. `service_count: 0` alongside a real version means `pm2 jlist` failed
separately; both degraded paths return well-formed responses rather than erroring, so read
the values rather than assuming success from the absence of an error.

Here PM2 is fine and one service is `errored`.

## 2. Narrow to the broken ones

```
list_services(status_filter="errored")
```

```json
[
  {
    "name": "my-api",
    "pm_id": 17,
    "status": "errored",
    "pid": 0,
    "uptime_ms": 0,
    "restarts": 148,
    "cpu_pct": 0,
    "memory_mb": 0,
    "exec_mode": "fork_mode"
  }
]
```

**`restarts: 148` is the finding, not `status: "errored"`.** A high and climbing restart count
is what distinguishes a crash *loop* from a service that stopped once. Re-run this call after
thirty seconds: if the number has moved, PM2 is still cycling it and you are racing the
restart policy.

`pid: 0` and `uptime_ms: 0` are consistent with "not currently running" and add nothing —
`uptime_ms` is reported as 0 for anything that isn't `online`, so it is never evidence about
a stopped service.

## 3. Find out what it is trying to run

```
get_service(name="my-api")
```

```json
{
  "name": "my-api",
  "pm_id": 17,
  "status": "errored",
  "restarts": 148,
  "script": "/opt/my-api/.venv/bin/python",
  "cwd": "/opt/my-api",
  "args": "server.py --port 9000",
  "log_file": "/home/ted/.pm2/logs/my-api-out.log",
  "error_file": "/home/ted/.pm2/logs/my-api-err.log",
  "created_at": "2026-08-14T09:12:03Z"
}
```

Check `script` and `cwd` exist on disk before reading any logs. A venv deleted or rebuilt
under a service is the single most common cause of this shape, and it produces a crash loop
with almost nothing in the application log — because the application never starts.

`created_at` is when this PM2 *registration* was made, not when the process last started. A
`created_at` from weeks ago with 148 restarts today means the registration is old and
something under it changed.

## 4. Read the logs

```
get_logs(name="my-api", lines=100)
```

Look at the **oldest** lines in the window, not the newest. In a crash loop the tail is the
most recent restart, which is often just the same message repeated; the first failure and any
one-off startup detail scroll away. Widen with `lines` (max 500) until you can see a full
start-to-exit cycle.

```
get_logs(name="my-api", lines=500, include_errors=true)
```

`include_errors` defaults to `true` and merges the stderr log, which is where a Python
traceback or a missing-interpreter error will be. Setting it to `false` while diagnosing a
crash is almost always a mistake.

## 5. Fix, then restart deliberately

Once the cause is fixed:

```
restart_service(name="my-api")
```

```json
{ "ok": true, "name": "my-api" }
```

Then confirm it actually stayed up, rather than trusting `ok: true` — which only means `pm2
restart` exited zero, not that the process survived:

```
list_services(status_filter="online")
```

Check `my-api` is present **and** that `restarts` has stopped climbing. If the count is still
rising, the restart succeeded and the crash loop continued; that is exactly the case `ok:
true` cannot distinguish.

Finally, if the fix involved changing the PM2 registration itself rather than the application:

```
save()
```

`save` persists the current process list so it survives a reboot. Note it also writes every
app's environment to `~/.pm2/dump.pm2` — see [`../docs/threat-model.md`](../docs/threat-model.md).

---

## What this cannot do

If the registration itself is wrong — wrong interpreter, wrong `cwd`, a stale environment —
no amount of restarting fixes it. That needs `pm2 delete` followed by
`pm2 start ecosystem.config.js`, and **this server has no `delete` verb**. It is an operator
task by design; see [`../docs/operations.md`](../docs/operations.md).
