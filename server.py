"""
pm2-mcp — FastMCP server wrapping the PM2 CLI.
Transport: streamable-http on 127.0.0.1:8486/mcp
"""

import json
import logging
import os
import subprocess
import time

from fastmcp import FastMCP

mcp = FastMCP(
    name="pm2",
    instructions=(
        "PM2 process manager MCP. Provides structured read and limited write access "
        "to PM2 services on forge via typed tool calls."
    ),
)

_VALID_STATUS_FILTERS = {"online", "stopped", "errored"}
_MAX_LOG_LINES = 500

# PM2 sets these variables in the process environment for its own IPC channel.
# If they leak into spawned children, any Node.js child (node, pnpm, tsc, or any
# node CLI) inherits a stray file descriptor and SIGABRTs during process
# teardown — 100% reproducible via _run_pm2, 0% via a direct shell. Strip
# them before exec so the pm2 CLI runs in a clean environment. (HLOPS-1, PM2-1)
_PM2_IPC_ENV_VARS = (
    "NODE_CHANNEL_FD",
    "NODE_CHANNEL_SERIALIZATION_MODE",
    "NODE_UNIQUE_ID",
)

# `pm2 start`/`pm2 restart --update-env` ships the CLI process's entire env to
# the daemon as the base env for the target app — an ecosystem file's `env`
# block only adds keys on top of that, it cannot remove one. When this MCP
# itself runs inside a Claude Code session (it does — pm2-mcp is launched the
# same way as every other forge PM2 app), every `_run_pm2` call freezes that
# session's env into whatever app it starts or restarts: CLAUDECODE,
# CLAUDE_AGENT_SDK_VERSION, CLAUDE_CODE_*, CLAUDE_PLUGIN_ROOT, CLAUDE_PID,
# CLAUDE_EFFORT — including CLAUDE_CODE_OAUTH_TOKEN, a live credential, found
# leaked into a 0664 dump.pm2 for 9+ days (vikunja#767).
#
# Matched by PREFIX, not an enumerated tuple like _PM2_IPC_ENV_VARS above.
# An exact list of "known" CLAUDE_* names has already gone stale once in this
# codebase (CLAUDE_AGENT_SDK_VERSION was missing from an earlier hand-written
# safe-to-strip list) and every new Claude Code release is free to add more
# without touching this file. None of these are ever legitimate app config.
_CLAUDE_ENV_PREFIX = "CLAUDE"

# The allowlist that replaces the denylist above (vikunja#610).
#
# HONEST JUSTIFICATION, because the ticket's is overstated for this repo. #610 groups
# pm2-mcp with system-ops and asserts they share an environment shape — measured there at
# 138 variables, 41 secret-shaped. Measured on the live pm2-mcp process: 69 keys, 20
# actual environment variables, and ZERO secret-shaped. Because it is a long-lived PM2
# service declared with `env: {}`, it starts clean and stays clean.
#
# So this is NOT remediating 41 secrets leaking today. It is structural protection against
# recontamination the next time someone runs `pm2 start` from an interactive shell that has
# sourced a secrets file — which is exactly how vikunja#767 happened. A denylist can only
# refuse what it has been told to name; an allowlist refuses everything nobody argued for.
# Overstating the justification is how the original denylist became a fleet standard, so
# it is stated at its real strength here.
#
# Enumerated, never prefix-matched — the opposite choice from _CLAUDE_ENV_PREFIX above,
# and deliberately so. A prefix on a DENYlist fails safe when it over-matches; a prefix on
# an ALLOWlist fails open, because one careless pattern admits every variable sharing it.
_CHILD_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "LANG",
        "TZ",
        # The POSIX LC_* set, written out rather than matched by prefix.
        "LC_ALL",
        "LC_COLLATE",
        "LC_CTYPE",
        "LC_MESSAGES",
        "LC_MONETARY",
        "LC_NUMERIC",
        "LC_TIME",
        # PM2_HOME is NOT in the build plan's proposed list, and leaving it out would be a
        # latent bug rather than a tightening. It selects which PM2 daemon the CLI talks
        # to. Measured on this host it is /home/ted/.pm2, identical to the default pm2
        # derives from $HOME — so dropping it happens to work HERE, by coincidence, the
        # same way the inert --host/--port flags did (vikunja#770).
        #
        # Verified what the coincidence is hiding: running `pm2 jlist` with a PM2_HOME
        # pointing somewhere else does not error. It silently spawns a SECOND daemon and
        # reports an empty process list. On any host with a non-default PM2_HOME, omitting
        # it would make every read return nothing and every write act on the wrong daemon,
        # with no failure to notice.
        "PM2_HOME",
    }
)

# Shadow mode is the default and enforcement is opt-in, per the build plan: log what
# enforcement WOULD remove before removing it.
#
# Two variables measured on the live process would be withheld by the list above and are
# not obviously inert: PM2_JSON_PROCESSING and PM2_USAGE, both set by the pm2 invocation
# that started this service. Whether the CLI cares is not something to settle by reading
# it, which is the whole reason this ships observing rather than enforcing.
#
# Deliberately NOT wired through ecosystem.config.js's `env` block, which is empty on
# purpose (see that file). Flipping to enforcement is a one-word change to this default,
# reviewed as a diff and redeployed — not an environment variable set on a service whose
# entire posture is that it needs none.
# SECURITY[deferred]: enforcement has been verified against the live daemon on READ paths
# only — pm2 jlist, --version and logs. No write verb (restart/stop/start/reload/flush/save)
# has been exercised under the trimmed environment, and it cannot be: every _run_pm2 test
# mocks subprocess.run, so the suite cannot see a real pm2 CLI incompatibility on a write
# path. PM2_JSON_PROCESSING and PM2_USAGE are withheld and are set by pm2 itself.
# Do NOT change this default to "enforce" until vikunja#771 (id 854) is closed — it carries
# the exact command to run. Shadow-log observation does not substitute: the log says which
# variables would go, not whether pm2 needed them.
# Audit: 2026-09-09/pm2-mcp-showcase-2026-09 (F-03, Info). Ted's ruling 2026-09-09.
_ENV_MODE = os.environ.get("PM2_MCP_ENV_MODE", "shadow").strip().lower()

_log = logging.getLogger("pm2_mcp")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _denylist_env() -> dict:
    """The env pm2-mcp has shipped since v0.3.2 — everything except PM2 IPC vars and
    anything on the CLAUDE prefix."""
    return {
        k: v
        for k, v in os.environ.items()
        if k not in _PM2_IPC_ENV_VARS and not k.startswith(_CLAUDE_ENV_PREFIX)
    }


def _allowlist_env() -> dict:
    """Only the variables named in _CHILD_ENV_ALLOWLIST.

    The PM2 IPC variables are excluded BY CONSTRUCTION here rather than by a rule: they
    are simply not on the list, so there is nothing to keep in sync and no way to
    reintroduce them by editing a denylist. Same for the CLAUDE family.
    """
    return {k: v for k, v in os.environ.items() if k in _CHILD_ENV_ALLOWLIST}


def _clean_env(*command: str) -> dict:
    """Environment handed to the pm2 child.

    In `shadow` mode (the default) this returns the denylist result — i.e. current,
    shipped behaviour — and logs which variables enforcement WOULD additionally remove.
    In `enforce` mode it returns the allowlist result.

    The log line carries variable NAMES and the pm2 command, never values, and never a
    bare count. vikunja#610 makes the point that a count of withheld variables describes
    the parent environment rather than the command, so it is identical on every call and
    predicts nothing about which callers would break. Names and the invoking command are
    the two things that can actually be acted on.
    """
    allowed = _allowlist_env()
    if _ENV_MODE == "enforce":
        return allowed

    current = _denylist_env()
    withheld = sorted(set(current) - set(allowed))
    if withheld:
        # SECURITY[accepted]: this line writes environment variable NAMES (never values) to
        # /home/ted/logs/pm2-mcp.log, mode 644, on every pm2 invocation. Ted's ruling
        # 2026-09-09. Rationale: the log sits in the service account's own home directory,
        # so a reader is already that account or root — and this server has no
        # authentication on 127.0.0.1:8486 at all, meaning such a reader can already stop
        # every PM2 process on the host. Names in a log they own is not the interesting
        # exposure next to that.
        # SECURITY[control]: test_shadow_log_never_contains_a_value pins the names-only
        # property with value canaries, and test_shadow_report_has_a_stable_parseable_shape
        # pins the format.
        # NOT a count or a hash, which is what a reviewer will reach for next: a count of
        # withheld variables describes the PARENT ENVIRONMENT, not the command, so it is
        # identical on every call and predicts nothing about which callers break. That is
        # vikunja#610's own argument and the reason names are logged at all. Replacing this
        # with a count would leave the feature collecting no usable evidence.
        # Audit: 2026-09-09/pm2-mcp-showcase-2026-09 (F-01, Low).
        _log.info(
            "env-allowlist shadow: pm2 %s would lose %s",
            " ".join(command) or "<no command>",
            ",".join(withheld),
        )
    return current


def _run_pm2(*args: str) -> subprocess.CompletedProcess:
    """Run a pm2 command. Raises RuntimeError if exit code is non-zero."""
    result = subprocess.run(
        ["pm2", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",  # explicit — system locale may not be UTF-8
        env=_clean_env(*args),
    )
    if result.returncode != 0:
        # SECURITY[accepted]: pm2's stderr is embedded verbatim and reaches the MCP caller
        # through every write tool's `except RuntimeError` branch. Ted's ruling 2026-09-09.
        # This is the fleet-wide OE-02 class (security-patterns.md, Recurrence 21) and the
        # same disposition as the other forge MCP servers: loopback-only, trusted callers,
        # and for an operator tool the pm2 error IS the diagnosis. Constructed here rather
        # than at the seven call sites so there is one place to change if that ever flips.
        # Audit: 2026-09-09/pm2-mcp-showcase-2026-09 (F-02, Info).
        raise RuntimeError(
            f"pm2 {' '.join(args)} failed (rc={result.returncode}): {result.stderr.strip()}"
        )
    return result


def _get_all_services() -> list[dict]:
    """Return parsed PM2 jlist."""
    result = _run_pm2("jlist")
    return json.loads(result.stdout)


def _find_service(name: str) -> dict | None:
    """Find a service by name in the current PM2 process list. Returns None if not found."""
    for s in _get_all_services():
        if s.get("name") == name:
            return s
    return None


def _parse_summary(s: dict) -> dict:
    """Extract ServiceSummary fields from a raw PM2 jlist entry."""
    monit = s.get("monit", {})
    pm2_env = s.get("pm2_env", {})
    now_ms = int(time.time() * 1000)
    pm_uptime = pm2_env.get("pm_uptime", now_ms)
    status = pm2_env.get("status")
    return {
        "name": s.get("name"),
        "pm_id": s.get("pm_id"),
        "status": status,
        "pid": s.get("pid"),
        "uptime_ms": now_ms - pm_uptime if status == "online" else 0,
        "restarts": pm2_env.get("restart_time", 0),
        "cpu_pct": monit.get("cpu", 0),
        "memory_mb": round(monit.get("memory", 0) / (1024 * 1024), 2),
        "exec_mode": pm2_env.get("exec_mode"),
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool
def list_services(status_filter: str | None = None) -> list[dict]:
    """List all PM2 services with key fields.

    Args:
        status_filter: Optional filter — "online", "stopped", or "errored".
    """
    if status_filter and status_filter not in _VALID_STATUS_FILTERS:
        raise ValueError(
            f"Invalid status_filter '{status_filter}'. Valid values: online, stopped, errored"
        )
    services = _get_all_services()
    summaries = [_parse_summary(s) for s in services]
    if status_filter:
        summaries = [s for s in summaries if s["status"] == status_filter]
    return summaries


@mcp.tool
def get_service(name: str) -> dict:
    """Get full detail for one PM2 service by name.

    Args:
        name: PM2 service name.
    """
    s = _find_service(name)
    if s is None:
        return {"ok": False, "error": "not found"}

    summary = _parse_summary(s)
    pm2_env = s.get("pm2_env", {})

    created_at_ms = pm2_env.get("created_at", 0)
    created_at = (
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created_at_ms / 1000))
        if created_at_ms
        else None
    )

    args = pm2_env.get("args", [])
    args_str = " ".join(str(a) for a in args) if args else ""

    return {
        **summary,
        "script": pm2_env.get("pm_exec_path"),
        "cwd": pm2_env.get("pm_cwd"),
        "args": args_str,
        "log_file": pm2_env.get("pm_out_log_path"),
        "error_file": pm2_env.get("pm_err_log_path"),
        "created_at": created_at,
    }


@mcp.tool
def get_logs(name: str, lines: int = 50, include_errors: bool = True) -> dict:
    """Tail recent log output for a PM2 service.

    Args:
        name: PM2 service name.
        lines: Number of lines to return (default 50, max 500).
        include_errors: Whether to include stderr log (default True).
    """
    try:
        # max(1, ...) — PM2 rejects --lines 0 with an error
        result = _run_pm2(
            "logs", name, "--nostream", "--lines", str(max(1, min(lines, _MAX_LOG_LINES)))
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr if include_errors else "",
        }
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool
def restart_service(name: str) -> dict:
    """Restart a PM2 service.

    Args:
        name: PM2 service name.
    """
    if _find_service(name) is None:
        return {"ok": False, "error": f"service '{name}' not found"}
    try:
        _run_pm2("restart", name)
        return {"ok": True, "name": name}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool
def stop_service(name: str) -> dict:
    """Stop a PM2 service.

    Args:
        name: PM2 service name.
    """
    if _find_service(name) is None:
        return {"ok": False, "error": f"service '{name}' not found"}
    try:
        _run_pm2("stop", name)
        return {"ok": True, "name": name}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool
def start_service(name: str) -> dict:
    """Resume a stopped PM2 service already registered in PM2.

    Note: resumes a registered service — does not register a new process.

    Args:
        name: PM2 service name.
    """
    if _find_service(name) is None:
        return {"ok": False, "error": f"service '{name}' not found"}
    try:
        _run_pm2("start", name)
        return {"ok": True, "name": name}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool
def reload_service(name: str) -> dict:
    """Gracefully reload a PM2 service (zero-downtime).

    Unlike restart_service, reload waits for existing connections to finish
    before cycling the process. Preferred for production services.

    Args:
        name: PM2 service name.
    """
    if _find_service(name) is None:
        return {"ok": False, "error": f"service '{name}' not found"}
    try:
        _run_pm2("reload", name)
        return {"ok": True, "name": name}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool
def save() -> dict:
    """Persist the current PM2 process list to disk.

    Call after any write operation (start, stop, restart, reload) to ensure
    the process list survives a system reboot.
    """
    try:
        _run_pm2("save")
        return {"ok": True}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool
def flush_logs(name: str) -> dict:
    """Clear log files for a PM2 service.

    Args:
        name: PM2 service name.
    """
    if _find_service(name) is None:
        return {"ok": False, "error": f"service '{name}' not found"}
    try:
        _run_pm2("flush", name)
        return {"ok": True, "name": name}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool
def get_status() -> dict:
    """Return server metadata and PM2 health summary.

    Reports configured host/port, PM2 version, total service count, and
    a breakdown of services by status. Use this to verify setup after
    installation.
    """
    try:
        version_result = _run_pm2("--version")
        pm2_version = version_result.stdout.strip()
    except RuntimeError:
        pm2_version = "unknown"

    try:
        services = _get_all_services()
        status_counts: dict[str, int] = {}
        for s in services:
            status = s.get("pm2_env", {}).get("status", "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
    except RuntimeError:
        services = []
        status_counts = {}

    return {
        "host": os.environ.get("MCP_HOST") or "127.0.0.1",
        "port": int(os.environ.get("MCP_PORT") or "8486"),
        "pm2_version": pm2_version,
        "service_count": len(services),
        "status_counts": status_counts,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    host = os.environ.get("MCP_HOST") or "127.0.0.1"
    port = int(os.environ.get("MCP_PORT") or "8486")
    mcp.run(transport="streamable-http", host=host, port=port)
