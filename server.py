"""
pm2-mcp — FastMCP server wrapping the PM2 CLI.
Transport: streamable-http on 127.0.0.1:8486/mcp
"""

import argparse
import ipaddress
import json
import logging
import os
import subprocess
import sys
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

# Marks the handler this module owns, so _configure_logging() is idempotent without
# reaching for isinstance(): pytest's LogCaptureHandler is itself a StreamHandler
# subclass, so an isinstance check would mistake a test's handler for ours and skip
# attaching the real one.
_LOG_HANDLER_TAG = "_pm2_mcp_owned_handler"

# Bind defaults. Kept as named constants rather than inline literals because
# _resolve_bind's precedence test asserts the default arm specifically, and a test that
# imports the same literal it is checking asserts nothing.
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8486


def _configure_logging() -> None:
    """Make the `pm2_mcp` logger actually emit on the production startup path.

    Both halves below are required and neither is sufficient alone. Setting the level
    with no handler anywhere falls through to `logging.lastResort`, which is itself
    WARNING and drops the INFO record regardless; attaching a handler without setting
    the level loses the record at the logger's own level check, before any handler is
    consulted. vikunja#772 was the second failure: effective level 30, handlers [], and
    so every shadow-mode run since the allowlist merged produced no evidence at all.

    Attaching the handler DIRECTLY to `pm2_mcp` with `propagate = False` is deliberate.
    FastMCP hands `log_level` to uvicorn, which applies `logging.config.dictConfig`, and
    dictConfig calls `_clearExistingHandlers()` — which closes and de-registers every
    handler attached before it runs, exactly the ordering this function produces by
    being called before `mcp.run(...)`. Measured on the Python 3.13 this repo runs: a
    handler attached directly to this logger survives that intact — still attached,
    still level 20, still emitting — because `Handler.close()` de-registers but neither
    detaches the handler nor drops its stream. uvicorn's default LOGGING_CONFIG also
    declares only the `uvicorn*` loggers, carries `disable_existing_loggers: False`, and
    has no `root` key at all, so nothing it does reaches this logger.

    NOT called at import time, deliberately. `server.py` is imported by the test suite
    and by anything installing the wheel, and configuring global logging as an import
    side effect is the kind of thing that surprises a consumer. It belongs on the
    startup path only, which is why it is a function and not module-level code.

    TESTING NOTE, learned the hard way: `propagate = False` cuts pytest's `caplog` off
    completely, because caplog installs its handler on the ROOT logger. Measured on
    pytest 9.1.1 — with propagate False, `caplog.text` is empty even under
    `caplog.at_level(..., logger="pm2_mcp")`. Any test that calls this function MUST
    snapshot and restore the logger's level, propagate flag and handler list, or it
    silently breaks every caplog-based test that happens to run after it. See the
    `restored_logger_state` fixture in the test suite.
    """
    _log.setLevel(logging.INFO)
    _log.propagate = False
    if not any(getattr(h, _LOG_HANDLER_TAG, False) for h in _log.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s: %(message)s"))
        setattr(handler, _LOG_HANDLER_TAG, True)
        _log.addHandler(handler)


def _is_loopback(host: str) -> bool:
    """True only for an address that cannot be reached from off-box.

    A bare hostname other than `localhost` is refused rather than resolved. Resolution
    is not a security boundary — the name can point anywhere, and can change to point
    somewhere else after this check passes without the process restarting.
    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _resolve_bind(argv: list | None = None, env: dict | None = None) -> tuple:
    """Resolve the (host, port) to bind, precedence argv > env > default.

    vikunja#770: `ecosystem.config.js` passed `--host`/`--port` for months while
    `__main__` read only MCP_HOST/MCP_PORT, so the flags were silently inert. This makes
    them live.

    The loopback refusal is the security-relevant half. This server has NO
    authentication on its port and its write verbs can stop or restart any PM2 process
    on the host, including every agent's own broker — see `docs/threat-model.md` §1.
    `--host 0.0.0.0` would turn a documented-safe posture into a one-word footgun, so a
    non-loopback bind exits non-zero instead of starting.

    There is deliberately NO override flag. Nothing on forge needs one, and shipping the
    escape hatch alongside the flag that makes it necessary defeats the point of the
    check. If a real need appears, that is its own reviewable change with its own audit.
    """
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(
        prog="pm2-mcp",
        description="FastMCP server wrapping the PM2 CLI. Loopback binds only.",
    )
    parser.add_argument(
        "--host",
        default=None,
        help=f"Bind address; must be loopback. Overrides MCP_HOST. Default {_DEFAULT_HOST}.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"Bind port. Overrides MCP_PORT. Default {_DEFAULT_PORT}.",
    )
    args = parser.parse_args(argv)

    # `is not None` throughout rather than `or`, so an explicitly-passed flag is
    # honoured or refused but never silently replaced by falsiness. Two cases make this
    # load-bearing rather than pedantic:
    #   --host ""  -> an empty host is INADDR_ANY at the socket layer, i.e. the exact
    #                 wildcard bind the loopback guard exists to refuse. Falling through
    #                 to the default would swallow it instead of rejecting it.
    #   --port 0   -> a legitimate "pick a free port" request, which `or` would turn
    #                 into 8486.
    # An EMPTY env var is different and is treated as unset: that is the behaviour
    # MCP_HOST/MCP_PORT have had since v0.1, it is safe (it resolves to loopback), and
    # changing it would break deployments that export the vars empty.
    host = args.host if args.host is not None else env.get("MCP_HOST") or _DEFAULT_HOST
    port = args.port if args.port is not None else int(env.get("MCP_PORT") or _DEFAULT_PORT)

    # SECURITY[control]: a port outside the TCP range cannot widen the bind — the loopback
    # guard below runs on `host` independently of whatever `port` is — so this is a
    # legibility fix, not a boundary. Without it the failure is an OSError traceback out of
    # socket.bind() well after startup begins; with it, an invalid port refuses in the same
    # shape and at the same point as an invalid host. Audit: 2026-09-10 /
    # pm2-mcp-followups-2026-09 (INFO-1). 0 is deliberately IN range: it is a valid request
    # for an ephemeral port, and is pinned by its own test.
    if not 0 <= port <= 65535:
        parser.error(
            f"refusing to bind port {port}: outside the valid TCP range 0-65535. "
            f"Port 0 is permitted and asks the kernel for an ephemeral port."
        )

    if not _is_loopback(host):
        parser.error(
            f"refusing to bind {host!r}: pm2-mcp has no authentication and its write "
            f"verbs can stop any PM2 process on the host, so it binds loopback only "
            f"(127.0.0.0/8, ::1 or localhost). See docs/threat-model.md. vikunja#770."
        )
    return host, port


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
        # /home/ted/logs/pm2-mcp.log, mode 644, on each pm2 invocation. Ted's ruling
        # 2026-09-09, RE-CONFIRMED 2026-09-10 on a corrected premise: when first accepted
        # this line had never once reached disk, because _configure_logging() did not exist
        # and the logger had no level or handler (vikunja#772). The risk was accepted for a
        # disclosure that was not happening; v0.4.0 is what makes it real. Low survives, as
        # the reasoning below was never contingent on frequency.
        # Measured 2026-09-10 from the LIVE log after redeploy, and it is the START METHOD
        # that governs exposure, not this code: started by PM2 at boot this line names 62
        # variables, zero secret-shaped; started from an interactive shell, 84 of which 12
        # are secret-shaped.
        # 62, not 64. Both numbers are right and measure different things — 64 is
        # present-minus-allowlisted (71 - 7), while this line reports only what enforcement
        # would withhold ADDITIONALLY, i.e. after the shipped denylist has already removed
        # NODE_CHANNEL_FD and NODE_CHANNEL_SERIALIZATION_MODE (vikunja#80, fixed v0.3.1).
        # 62 is the operationally meaningful figure because the enforce decision is about the
        # delta from current behaviour. Do not "correct" it back to 64.
        # And note the count is not a constant: it describes the PARENT environment, which is
        # the same argument this comment makes below for logging names rather than a count.
        # See
        # docs/operations.md — this is the second independent reason for the vikunja#767
        # rule against starting the service from a session shell.
        # Rationale: the log sits in the service account's own home directory,
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
    # Deliberately thin: everything here is coverage-excluded and unreachable from a
    # test, so both the logging setup and the bind resolution live in module-level
    # functions above that the suite can actually exercise. vikunja#772 is what happens
    # when startup-only logic has no test that can reach it.
    _configure_logging()
    _host, _port = _resolve_bind(sys.argv[1:])
    mcp.run(transport="streamable-http", host=_host, port=_port)
