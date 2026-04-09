"""
pm2-mcp — FastMCP server wrapping the PM2 CLI.
Transport: streamable-http on 127.0.0.1:8486/mcp
"""

import json
import subprocess
import time
from typing import Optional

from fastmcp import FastMCP

mcp = FastMCP(
    name="pm2",
    instructions=(
        "PM2 process manager MCP. Provides structured read and limited write access "
        "to PM2 services on claudebox via typed tool calls."
    ),
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run_pm2(*args: str) -> subprocess.CompletedProcess:
    """Run a pm2 command. Raises RuntimeError if exit code is non-zero."""
    result = subprocess.run(
        ["pm2", *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"pm2 {' '.join(args)} failed (rc={result.returncode}): {result.stderr.strip()}"
        )
    return result


def _get_all_services() -> list[dict]:
    """Return parsed PM2 jlist."""
    result = _run_pm2("jlist")
    return json.loads(result.stdout)


def _find_service(name: str) -> Optional[dict]:
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
def list_services(status_filter: Optional[str] = None) -> list[dict]:
    """List all PM2 services with key fields.

    Args:
        status_filter: Optional filter — "online", "stopped", or "errored".
    """
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
        lines: Number of lines to return (default 50).
        include_errors: Whether to include stderr log (default True).
    """
    try:
        result = _run_pm2("logs", name, "--nostream", "--lines", str(lines))
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="pm2-mcp server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8486, help="Bind port (default: 8486)")
    args = parser.parse_args()

    mcp.run(transport="streamable-http", host=args.host, port=args.port)
