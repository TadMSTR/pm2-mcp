# Security audit history

Dated record of security audits against this repo, and how each finding was disposed of.
This is a history, not a threat model — for the current security posture (what's protected,
what isn't, and why) see [threat-model.md](threat-model.md).

**pm2-mcp has no authentication on `127.0.0.1:8486`, and this is an accepted, documented
posture, not an oversight.** See [threat-model.md §1](threat-model.md#1-no-authentication-on-the-mcp-endpoint)
rather than re-deriving the reasoning here.

The environment allowlist added in v0.4.0 (`_clean_env`, vikunja#610) currently runs in
**shadow mode**: it computes and logs what it would withhold, and changes nothing. It is a
verified no-op on behaviour until `PM2_MCP_ENV_MODE=enforce` is set — do not read it as
enforcing.

## History

| Date | Build | Findings | Max severity | Result |
|---|---|---|---|---|
| 2026-09-10 | `pm2-mcp-followups-2026-09` | 2 | Info | 1 fixed, 1 accepted-risk re-confirmed |
| 2026-09-09 | `pm2-mcp-showcase-2026-09` | 3 | Low | 2 accepted, 1 deferred |

## 2026-09-10 — `pm2-mcp-followups-2026-09`

Two Info-level findings, zero Critical/High/Medium/Low. Full report:
`host-forge/build-reports/pm2-mcp-followups-2026-09/audit.md`.

- **INFO-1 — `--port` accepted values outside 0–65535, unvalidated.** Fixed, not accepted
  (`95ed8e0`). Worth stating plainly: this was **not** a security boundary. The loopback
  guard on `--host`/`MCP_HOST` runs independently of `--port`, so an out-of-range port could
  never have widened the bind — the worst case was an opaque `OSError` traceback instead of
  a clean refusal. `parser.error()` now rejects it on the resolved value (so `MCP_PORT` is
  covered the same as `--port`), giving bad bind input one failure mode instead of two. `0`
  remains valid as a legitimate ephemeral-port request.
- **INFO-2 — accepted-risk row F-01 needed re-confirmation, not an inherited pass.** See
  "What changed between audits" below — this is the one worth reading in full.

## 2026-09-09 — `pm2-mcp-showcase-2026-09`

Three findings on the Baseline → Showcase promotion, zero Critical/High/Medium. Full report:
`host-forge/build-reports/pm2-mcp-showcase-2026-09/audit.md`.

- **F-01 (Low) — accepted.** Shadow-mode logging writes withheld environment variable
  *names* to `/home/ted/logs/pm2-mcp.log` on each `pm2` invocation. Names only, never
  values, canary-pinned. **Re-confirmed 2026-09-10 on a corrected premise — see below.**
- **F-02 (Info) — accepted.** pm2's stderr is embedded verbatim in write-tool error
  responses (the fleet-wide OE-02 pattern, `security-patterns.md` Recurrence 21). Localhost
  server, forge-agent callers only, and for an operator tool the pm2 error is the diagnosis.
- **F-03 (Info) — deferred.** The environment allowlist was verified against the live PM2
  daemon on read paths only (`pm2 jlist`, `--version`, `logs`); no write verb has been
  exercised under the trimmed environment. Deferred to **vikunja#771**, gating the
  `PM2_MCP_ENV_MODE=enforce` flip. Not a defect in what shipped — shadow mode is a verified
  no-op, so nothing deployed is affected.

## What changed between audits — F-01's premise was wrong

F-01 was accepted 2026-09-09 on the premise that shadow-mode logging writes withheld
variable names to the log "on every `pm2` invocation." **It never had.** The `pm2_mcp`
logger had no level and no handler, so every line was silently dropped — verified as zero
matching lines in a live, actively-written log (vikunja#772). The risk was accepted for a
disclosure that was not occurring; `pm2-mcp-followups-2026-09` (`_configure_logging()`,
v0.4.0) is what makes it real for the first time.

Re-confirmed at Low, 2026-09-10, not inherited — the original reasoning was never
contingent on frequency. What's new is a measurement that didn't exist at first acceptance,
and it relocates the risk from the code to *how the service is started*:

| Started by | Names withheld | Of which secret-shaped |
|---|---|---|
| PM2 at boot | 64 | 0 |
| An interactive shell | 84 | 12 |

Names only in both cases, never values. This gives the pre-existing vikunja#767 rule (never
start this service from a session shell) a second, independent reason — documented in
[docs/operations.md](operations.md).

## Prior security history

| Ticket | Finding | Fixed in |
|---|---|---|
| vikunja#80 | PM2 IPC environment variables (`NODE_CHANNEL_FD` and siblings) leaked into the spawned `pm2` child, causing a `SIGABRT` on teardown | v0.3.1 |
| vikunja#767 | `CLAUDE*`-prefixed variables, including a live `CLAUDE_CODE_OAUTH_TOKEN`, leaked into a world-readable `dump.pm2` for 9+ days | v0.3.2 |

## Current version

v0.4.0 (2026-09-10).
