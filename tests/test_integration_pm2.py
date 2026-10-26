"""vikunja#771: the write verbs against a REAL pm2 CLI and daemon, under the enforced env.

Every other `_run_pm2` test mocks subprocess.run, so the suite cannot see a pm2 CLI that
needs something the allowlist withholds. These tests run the server's own tool functions
against an isolated pm2 daemon and check that each verb both reported success and had its
effect.

Not part of the default run: `pytest -m integration --no-cov`. They need pm2 on PATH (CI
pins 6.0.14) and start and kill a private daemon.

ISOLATION IS THE FIRST REQUIREMENT. pm2 picks its daemon from PM2_HOME, falling back to
$HOME/.pm2. A pm2 call that reached the real daemon could stop or flush any service on the
host. So:
  - PM2_HOME and HOME both point at a fresh temp dir, so even a call that lost PM2_HOME
    would land in <tmp>/.pm2, not the operator's;
  - every subprocess.run the server makes passes through a guard that refuses to exec
    unless the child env's PM2_HOME is that temp dir. It fails closed: a call that would
    escape raises before pm2 runs;
  - the fixture's own pm2 calls go through the same check;
  - teardown runs `pm2 kill` under the temp PM2_HOME.
PM2_HOME is therefore never used as a negative control: dropping it targets the default
daemon.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from pm2_mcp import server

pytestmark = pytest.mark.integration

APP = "pm2-mcp-live-probe"

# What a pm2-managed parent carries, measured on the live pm2-mcp process 2026-10-25. In
# production pm2-mcp IS a pm2 child, so its own env holds these when it calls the CLI. The
# IPC pair is what vikunja#80 stripped; the other three are what #771 asks about.
PM2_CHILD_VARS = {
    "NODE_APP_INSTANCE": "0",
    "PM2_JSON_PROCESSING": "true",
    "PM2_USAGE": "CLI",
    "NODE_CHANNEL_FD": "3",
    "NODE_CHANNEL_SERIALIZATION_MODE": "json",
}

# Prints a line every 50 ms, so the log has content to flush.
APP_SOURCE = """
import itertools, sys, time
for i in itertools.count():
    print(f"probe line {i}", flush=True)
    time.sleep(0.05)
"""


class EscapedIsolation(AssertionError):
    pass


def _check_isolated(env, home):
    """Refuse any pm2 child env not pinned to the temp daemon."""
    if env is None or env.get("PM2_HOME") != home:
        got = None if env is None else env.get("PM2_HOME", "<unset>")
        raise EscapedIsolation(f"refusing to run pm2 with PM2_HOME={got!r}; expected {home!r}")


def _wait(predicate, what, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


@pytest.fixture
def pm2_home(monkeypatch):
    """A private pm2 daemon, with one app running under it. Yields its PM2_HOME."""
    pm2 = shutil.which("pm2")
    # Fail, never skip: a skipped integration run reads as a pass in CI.
    assert pm2, "pm2 is not on PATH; the integration tests need a real pm2 CLI"

    # mkdtemp under /tmp, not pytest's tmp_path: pm2 binds unix sockets in PM2_HOME and a
    # socket path is limited to 107 bytes.
    home = tempfile.mkdtemp(prefix="pm2h-")
    base_env = {"PATH": os.environ["PATH"], "HOME": home, "PM2_HOME": home}

    def pm2_cli(*args, check=True):
        _check_isolated(base_env, home)
        return subprocess.run(
            [pm2, *args], env=base_env, capture_output=True, text=True, check=check
        )

    # The server's children inherit from os.environ (via _clean_env), so pin it too.
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("PM2_HOME", home)

    real_run = subprocess.run

    def guarded_run(cmd, *args, **kwargs):
        _check_isolated(kwargs.get("env"), home)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(server.subprocess, "run", guarded_run)

    script = Path(home) / "probe.py"
    script.write_text(APP_SOURCE)
    try:
        pm2_cli("start", str(script), "--name", APP, "--interpreter", sys.executable)
        _wait(lambda: _app(pm2_cli)["pm2_env"]["status"] == "online", "the app to come up")
        yield home, pm2_cli
    finally:
        pm2_cli("kill", check=False)
        shutil.rmtree(home, ignore_errors=True)


def _app(pm2_cli):
    [app] = [p for p in json.loads(pm2_cli("jlist").stdout) if p["name"] == APP]
    return app


def _restarts(pm2_cli):
    return _app(pm2_cli)["pm2_env"]["restart_time"]


def _status(pm2_cli):
    return _app(pm2_cli)["pm2_env"]["status"]


def _out_log(pm2_cli):
    return Path(_app(pm2_cli)["pm2_env"]["pm_out_log_path"])


@pytest.fixture
def as_pm2_child(monkeypatch):
    for name, value in PM2_CHILD_VARS.items():
        monkeypatch.setenv(name, value)


@pytest.mark.usefixtures("pm2_home")
def test_isolation_guard_refuses_the_default_daemon(monkeypatch):
    """The guard is the safety of every other test here, so it gets its own red case."""
    # The fixture is needed for its daemon and its guard, not its values.
    monkeypatch.setattr(server, "_ENV_MODE", "enforce")
    monkeypatch.delenv("PM2_HOME")
    with pytest.raises(EscapedIsolation):
        server.list_services()


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_write_verbs_work_and_take_effect(pm2_home, as_pm2_child, monkeypatch, mode):
    """Each write verb, through the server's own function, in both env modes.

    Both modes, so a difference between them is visible: shadow is what ships today, and
    enforce is the flip #771 gates. Same assertions for both.
    """
    home, pm2_cli = pm2_home
    monkeypatch.setattr(server, "_ENV_MODE", mode)
    if mode == "enforce":
        # What the child actually receives. None of the pm2-child variables survive.
        child = server._clean_env("probe")
        assert set(child) <= server._CHILD_ENV_ALLOWLIST
        assert child["PM2_HOME"] == home
        assert not set(PM2_CHILD_VARS) & set(child)

    assert [s["name"] for s in server.list_services()] == [APP]
    assert server.get_service(APP)["status"] == "online"

    before = _restarts(pm2_cli)
    assert server.restart_service(APP) == {"ok": True, "name": APP}
    assert _restarts(pm2_cli) == before + 1
    _wait(lambda: _status(pm2_cli) == "online", "online after restart")

    assert server.stop_service(APP) == {"ok": True, "name": APP}
    assert _status(pm2_cli) == "stopped"

    assert server.start_service(APP) == {"ok": True, "name": APP}
    _wait(lambda: _status(pm2_cli) == "online", "online after start")

    # Fork mode: pm2 reload falls back to a restart, so the count moves and the pid
    # changes. The same in both modes is the answer to the plan's reload question.
    before, pid = _restarts(pm2_cli), _app(pm2_cli)["pid"]
    assert _app(pm2_cli)["pm2_env"]["exec_mode"] == "fork_mode"
    assert server.reload_service(APP) == {"ok": True, "name": APP}
    _wait(lambda: _status(pm2_cli) == "online", "online after reload")
    assert _restarts(pm2_cli) == before + 1
    assert _app(pm2_cli)["pid"] != pid

    log = _out_log(pm2_cli)
    _wait(lambda: log.exists() and log.stat().st_size > 0, "log output to flush")
    assert server.flush_logs(APP) == {"ok": True, "name": APP}
    # The app keeps writing, so "emptied" means it shrank to well under what it held.
    # Stop first, so the check is not racing the next line.
    server.stop_service(APP)
    assert log.stat().st_size < 200

    dump = Path(home) / "dump.pm2"
    dump.unlink(missing_ok=True)
    assert server.save() == {"ok": True}
    assert APP in dump.read_text()

    logs = server.get_logs(APP, lines=5)
    assert "error" not in logs


def test_negative_control_child_path_without_pm2(pm2_home, as_pm2_child, monkeypatch):
    """The suite can see a broken child env: a PATH that cannot reach pm2 must fail.

    NOT "drop PATH from the allowlist", which is what the build plan specified. With PATH
    absent, subprocess falls back to os.defpath (/bin:/usr/bin) and `env node` does the
    same. On forge pm2 and node both live in /usr/bin, so dropping PATH changes nothing
    and the control would pass for the wrong reason (measured 2026-10-25). A PATH that
    is present and points at an empty directory fails on every host.

    PM2_HOME stays pinned throughout, as the module docstring requires.
    """
    home, pm2_cli = pm2_home
    empty = Path(home) / "empty-bin"
    empty.mkdir()
    monkeypatch.setattr(server, "_ENV_MODE", "enforce")
    monkeypatch.setenv("PATH", str(empty))
    before = _restarts(pm2_cli)

    for verb in (
        server.restart_service,
        server.stop_service,
        server.start_service,
        server.reload_service,
        server.flush_logs,
    ):
        with pytest.raises(FileNotFoundError):
            verb(APP)
    with pytest.raises(FileNotFoundError):
        server.save()

    # And nothing happened to the app.
    assert _restarts(pm2_cli) == before
    assert _status(pm2_cli) == "online"
