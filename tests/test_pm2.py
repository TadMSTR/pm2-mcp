"""
Tests for pm2-mcp server.py.

All _run_pm2 calls are mocked so no PM2 installation is required to run tests.
"""

import io
import json
import logging
import subprocess
import time
from unittest.mock import MagicMock, call, patch

import pytest

import server

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_process(name="svc-a", status="online", memory=104857600, cpu=0.5, pm_uptime=None):
    """Build a minimal PM2 jlist entry."""
    if pm_uptime is None:
        pm_uptime = int(time.time() * 1000) - 60_000  # 1 minute ago
    return {
        "name": name,
        "pm_id": 0,
        "pid": 12345,
        "monit": {"cpu": cpu, "memory": memory},
        "pm2_env": {
            "status": status,
            "pm_uptime": pm_uptime,
            "restart_time": 2,
            "exec_mode": "fork_mode",
            "pm_exec_path": f"/home/ted/repos/personal/{name}/server.py",
            "pm_cwd": f"/home/ted/repos/personal/{name}",
            "args": ["--port", "8080"],
            "pm_out_log_path": f"/home/ted/.pm2/logs/{name}-out.log",
            "pm_err_log_path": f"/home/ted/.pm2/logs/{name}-err.log",
            "created_at": 1743000000000,
        },
    }


def _completed(stdout="", stderr="", returncode=0):
    """Build a fake subprocess.CompletedProcess."""
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.stdout = stdout
    r.stderr = stderr
    r.returncode = returncode
    return r


def _jlist_result(*processes):
    return _completed(stdout=json.dumps(list(processes)))


def _leaked_keys(env, candidates):
    """Names from `candidates` still present in `env`, as a sorted list.

    Assert against THIS rather than `assert name not in env`. pytest rewrites a failing
    `in` assertion by rendering the container, so `assert "CLAUDECODE" not in env` prints
    the entire environment — names AND VALUES — into the test output on failure. These
    tests run on forge, where the ambient environment of an agent shell carries real
    credentials, and that output can land in a CI log or a mutation-run artifact.
    Comparing key lists keeps the failure message to names only, which is all the
    diagnosis needs. Observed while proving the mutation gate red: a deliberately broken
    _clean_env dumped the full environment, values included, into the run log.
    """
    return sorted(k for k in candidates if k in env)


# ---------------------------------------------------------------------------
# list_services
# ---------------------------------------------------------------------------


class TestListServices:
    def test_parses_jlist_memory_bytes_to_mb(self):
        """memory field (bytes) should be divided by 1024^2 for memory_mb."""
        proc = _make_process(memory=104857600)  # exactly 100 MB
        with patch("server._run_pm2", return_value=_jlist_result(proc)):
            result = server.list_services()

        assert len(result) == 1
        assert result[0]["memory_mb"] == pytest.approx(100.0, abs=0.01)

    def test_parses_jlist_uptime_from_epoch(self):
        """uptime_ms should be derived from now - pm_uptime (epoch ms)."""
        now_ms = int(time.time() * 1000)
        pm_uptime = now_ms - 3_600_000  # 1 hour ago
        proc = _make_process(pm_uptime=pm_uptime)
        with patch("server._run_pm2", return_value=_jlist_result(proc)):
            result = server.list_services()

        assert result[0]["uptime_ms"] == pytest.approx(3_600_000, rel=0.01)

    def test_status_filter_online(self):
        """status_filter='online' should exclude stopped services."""
        procs = [
            _make_process(name="svc-a", status="online"),
            _make_process(name="svc-b", status="stopped"),
        ]
        with patch("server._run_pm2", return_value=_completed(stdout=json.dumps(procs))):
            result = server.list_services(status_filter="online")

        assert len(result) == 1
        assert result[0]["name"] == "svc-a"

    def test_status_filter_stopped(self):
        procs = [
            _make_process(name="svc-a", status="online"),
            _make_process(name="svc-b", status="stopped"),
        ]
        with patch("server._run_pm2", return_value=_completed(stdout=json.dumps(procs))):
            result = server.list_services(status_filter="stopped")

        assert len(result) == 1
        assert result[0]["name"] == "svc-b"

    def test_no_filter_returns_all(self):
        procs = [_make_process(name="a"), _make_process(name="b")]
        with patch("server._run_pm2", return_value=_completed(stdout=json.dumps(procs))):
            result = server.list_services()
        assert len(result) == 2

    def test_stopped_service_uptime_is_zero(self):
        proc = _make_process(status="stopped")
        with patch("server._run_pm2", return_value=_jlist_result(proc)):
            result = server.list_services()
        assert result[0]["uptime_ms"] == 0

    def test_invalid_status_filter_raises(self):
        with pytest.raises(ValueError, match="Invalid status_filter"):
            server.list_services(status_filter="running")


# ---------------------------------------------------------------------------
# get_service
# ---------------------------------------------------------------------------


class TestGetService:
    def test_found_returns_detail_fields(self):
        proc = _make_process()
        with patch("server._run_pm2", return_value=_jlist_result(proc)):
            result = server.get_service("svc-a")

        assert result["name"] == "svc-a"
        assert result["script"].endswith("server.py")
        assert result["log_file"].endswith("-out.log")
        assert result["error_file"].endswith("-err.log")
        assert result["created_at"] is not None
        assert "T" in result["created_at"]  # ISO format

    def test_found_includes_summary_fields(self):
        proc = _make_process(memory=52428800, cpu=1.5)  # 50 MB
        with patch("server._run_pm2", return_value=_jlist_result(proc)):
            result = server.get_service("svc-a")

        assert result["memory_mb"] == pytest.approx(50.0, abs=0.01)
        assert result["cpu_pct"] == 1.5

    def test_not_found_returns_error_dict(self):
        with patch("server._run_pm2", return_value=_jlist_result()):
            result = server.get_service("nonexistent")

        assert result == {"ok": False, "error": "not found"}


# ---------------------------------------------------------------------------
# get_logs
# ---------------------------------------------------------------------------


class TestGetLogs:
    def test_returns_stdout_and_stderr(self):
        stub = _completed(stdout="line1\nline2\n", stderr="err\n")
        with patch("server._run_pm2", return_value=stub):
            result = server.get_logs("svc-a", lines=10)

        assert "stdout" in result
        assert "stderr" in result
        assert "line1" in result["stdout"]
        assert "err" in result["stderr"]

    def test_calls_pm2_with_correct_args(self):
        with patch("server._run_pm2", return_value=_completed(stdout="")) as mock:
            server.get_logs("svc-a", lines=25)

        mock.assert_called_once_with("logs", "svc-a", "--nostream", "--lines", "25")

    def test_lines_capped_at_max(self):
        with patch("server._run_pm2", return_value=_completed(stdout="")) as mock:
            server.get_logs("svc-a", lines=1000)

        mock.assert_called_once_with("logs", "svc-a", "--nostream", "--lines", "500")

    def test_include_errors_false_clears_stderr(self):
        with patch("server._run_pm2", return_value=_completed(stdout="out", stderr="err")):
            result = server.get_logs("svc-a", include_errors=False)

        assert result["stderr"] == ""
        assert result["stdout"] == "out"

    def test_pm2_error_returns_error_dict(self):
        with patch("server._run_pm2", side_effect=RuntimeError("pm2 logs failed")):
            result = server.get_logs("missing-svc")

        assert result["ok"] is False
        assert "pm2 logs failed" in result["error"]


# ---------------------------------------------------------------------------
# restart_service
# ---------------------------------------------------------------------------


class TestRestartService:
    def test_known_service_calls_restart(self):
        proc = _make_process()
        jlist_result = _jlist_result(proc)
        restart_result = _completed()
        with patch("server._run_pm2", side_effect=[jlist_result, restart_result]) as mock:
            result = server.restart_service("svc-a")

        assert result == {"ok": True, "name": "svc-a"}
        mock.assert_any_call("restart", "svc-a")

    def test_unknown_service_returns_error_without_restart(self):
        with patch("server._run_pm2", return_value=_jlist_result()) as mock:
            result = server.restart_service("ghost")

        assert result["ok"] is False
        assert "not found" in result["error"]
        for c in mock.call_args_list:
            assert c != call("restart", "ghost")


# ---------------------------------------------------------------------------
# stop_service
# ---------------------------------------------------------------------------


class TestStopService:
    def test_unknown_service_returns_error_without_stop(self):
        with patch("server._run_pm2", return_value=_jlist_result()) as mock:
            result = server.stop_service("ghost")

        assert result["ok"] is False
        assert "not found" in result["error"]
        for c in mock.call_args_list:
            assert c != call("stop", "ghost")

    def test_known_service_calls_stop(self):
        proc = _make_process()
        with patch("server._run_pm2", side_effect=[_jlist_result(proc), _completed()]) as mock:
            result = server.stop_service("svc-a")

        assert result == {"ok": True, "name": "svc-a"}
        mock.assert_any_call("stop", "svc-a")


# ---------------------------------------------------------------------------
# start_service
# ---------------------------------------------------------------------------


class TestStartService:
    def test_known_service_calls_start(self):
        proc = _make_process(status="stopped")
        with patch("server._run_pm2", side_effect=[_jlist_result(proc), _completed()]) as mock:
            result = server.start_service("svc-a")

        assert result == {"ok": True, "name": "svc-a"}
        mock.assert_any_call("start", "svc-a")

    def test_unknown_service_returns_error_without_start(self):
        with patch("server._run_pm2", return_value=_jlist_result()) as mock:
            result = server.start_service("ghost")

        assert result["ok"] is False
        assert "not found" in result["error"]
        for c in mock.call_args_list:
            assert c != call("start", "ghost")


# ---------------------------------------------------------------------------
# reload_service
# ---------------------------------------------------------------------------


class TestReloadService:
    def test_known_service_calls_reload(self):
        proc = _make_process()
        with patch("server._run_pm2", side_effect=[_jlist_result(proc), _completed()]) as mock:
            result = server.reload_service("svc-a")

        assert result == {"ok": True, "name": "svc-a"}
        mock.assert_any_call("reload", "svc-a")

    def test_unknown_service_returns_error_without_reload(self):
        with patch("server._run_pm2", return_value=_jlist_result()) as mock:
            result = server.reload_service("ghost")

        assert result["ok"] is False
        assert "not found" in result["error"]
        for c in mock.call_args_list:
            assert c != call("reload", "ghost")


# ---------------------------------------------------------------------------
# save
# ---------------------------------------------------------------------------


class TestSave:
    def test_save_calls_pm2_save(self):
        with patch("server._run_pm2", return_value=_completed()) as mock:
            result = server.save()

        assert result == {"ok": True}
        mock.assert_called_once_with("save")

    def test_save_error_returns_error_dict(self):
        with patch("server._run_pm2", side_effect=RuntimeError("pm2 save failed")):
            result = server.save()

        assert result["ok"] is False
        assert "pm2 save failed" in result["error"]


# ---------------------------------------------------------------------------
# flush_logs
# ---------------------------------------------------------------------------


class TestFlushLogs:
    def test_known_service_calls_flush(self):
        proc = _make_process()
        with patch("server._run_pm2", side_effect=[_jlist_result(proc), _completed()]) as mock:
            result = server.flush_logs("svc-a")

        assert result == {"ok": True, "name": "svc-a"}
        mock.assert_any_call("flush", "svc-a")

    def test_unknown_service_returns_error_without_flush(self):
        with patch("server._run_pm2", return_value=_jlist_result()) as mock:
            result = server.flush_logs("ghost")

        assert result["ok"] is False
        assert "not found" in result["error"]
        for c in mock.call_args_list:
            assert c != call("flush", "ghost")


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------


class TestGetStatus:
    def test_returns_version_and_counts(self):
        procs = [
            _make_process(name="a", status="online"),
            _make_process(name="b", status="stopped"),
        ]
        with patch(
            "server._run_pm2",
            side_effect=[
                _completed(stdout="5.0.0\n"),  # pm2 --version
                _completed(stdout=json.dumps(procs)),  # pm2 jlist
            ],
        ):
            result = server.get_status()

        assert result["pm2_version"] == "5.0.0"
        assert result["service_count"] == 2
        assert result["status_counts"]["online"] == 1
        assert result["status_counts"]["stopped"] == 1

    def test_pm2_unavailable_returns_unknown_version(self):
        procs = []
        with patch(
            "server._run_pm2",
            side_effect=[
                RuntimeError("pm2 not found"),
                _completed(stdout=json.dumps(procs)),
            ],
        ):
            result = server.get_status()

        assert result["pm2_version"] == "unknown"
        assert result["service_count"] == 0


# ---------------------------------------------------------------------------
# _run_pm2
# ---------------------------------------------------------------------------


class TestRunPm2:
    def test_nonzero_exit_raises_runtime_error(self):
        with (
            patch("subprocess.run", return_value=_completed(returncode=1, stderr="bad")),
            pytest.raises(RuntimeError, match="rc=1"),
        ):
            server._run_pm2("jlist")

    def test_zero_exit_returns_completed_process(self):
        fake = _completed(stdout="[]", returncode=0)
        with patch("subprocess.run", return_value=fake):
            result = server._run_pm2("jlist")
        assert result.returncode == 0

    def test_uses_list_form_not_shell(self):
        """Verify subprocess.run is called with a list (shell=False by default)."""
        with patch("subprocess.run", return_value=_completed(stdout="[]")) as mock:
            server._run_pm2("jlist")

        call_args = mock.call_args
        cmd = call_args[0][0]
        assert isinstance(cmd, list)
        assert cmd[0] == "pm2"
        # shell kwarg must be absent or False
        assert call_args.kwargs.get("shell", False) is False

    def test_strips_pm2_ipc_vars_from_child_env(self, monkeypatch):
        """Regression for PM2-1: the pm2 CLI child must not inherit PM2's IPC vars."""
        for var in server._PM2_IPC_ENV_VARS:
            monkeypatch.setenv(var, "leaked")
        monkeypatch.setenv("KEEP_ME", "yes")

        with patch("subprocess.run", return_value=_completed(stdout="[]")) as mock:
            server._run_pm2("jlist")

        env = mock.call_args.kwargs.get("env")
        assert env is not None
        assert _leaked_keys(env, server._PM2_IPC_ENV_VARS) == []
        assert env["KEEP_ME"] == "yes"


class TestCleanEnv:
    def test_clean_env_strips_pm2_vars(self, monkeypatch):
        for var in server._PM2_IPC_ENV_VARS:
            monkeypatch.setenv(var, "leaked")
        monkeypatch.setenv("KEEP_ME", "yes")

        env = server._clean_env()

        assert _leaked_keys(env, server._PM2_IPC_ENV_VARS) == []
        assert env["KEEP_ME"] == "yes"

    def test_clean_env_strips_claude_session_vars(self, monkeypatch):
        """Regression for vikunja#767: a pm2-mcp call made from inside a Claude
        Code session must not freeze that session's env into the target app,
        including CLAUDE_CODE_OAUTH_TOKEN (a live credential)."""
        leaked = (
            "CLAUDECODE",
            "CLAUDE_AGENT_SDK_VERSION",
            "CLAUDE_CODE_ENTRYPOINT",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_PLUGIN_ROOT",
            "CLAUDE_PID",
            "CLAUDE_EFFORT",
        )
        for var in leaked:
            monkeypatch.setenv(var, "leaked")
        monkeypatch.setenv("KEEP_ME", "yes")

        env = server._clean_env()

        assert _leaked_keys(env, leaked) == []
        assert env["KEEP_ME"] == "yes"

    def test_clean_env_does_not_strip_unrelated_vars_with_similar_prefix(self, monkeypatch):
        """The match is startswith("CLAUDE"), not "CLAUD" — confirm it doesn't
        overreach into an unrelated var that merely starts similarly."""
        monkeypatch.setenv("CLAUDIA_UNRELATED", "keep")

        env = server._clean_env()

        assert env.get("CLAUDIA_UNRELATED") == "keep"

    def test_run_pm2_strips_claude_session_vars_from_child_env(self, monkeypatch):
        """Regression for vikunja#767 at the _run_pm2 call site, not just _clean_env."""
        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-leaked")
        monkeypatch.setenv("KEEP_ME", "yes")

        with patch("subprocess.run", return_value=_completed(stdout="[]")) as mock:
            server._run_pm2("jlist")

        env = mock.call_args.kwargs.get("env")
        assert env is not None
        assert _leaked_keys(env, ("CLAUDECODE", "CLAUDE_CODE_OAUTH_TOKEN")) == []
        assert env["KEEP_ME"] == "yes"


# ---------------------------------------------------------------------------
# Phase 2 additive tests
# ---------------------------------------------------------------------------


class TestGetServiceCreatedAtZero:
    def test_created_at_zero_returns_none(self):
        """created_at=0 in PM2 env should map to None, not epoch string."""
        proc = _make_process()
        proc["pm2_env"]["created_at"] = 0
        with patch("server._run_pm2", return_value=_jlist_result(proc)):
            result = server.get_service("svc-a")
        assert result["created_at"] is None


class TestGetLogsGuards:
    def test_lines_zero_clamped_to_one(self):
        with patch("server._run_pm2", return_value=_completed(stdout="")) as mock:
            server.get_logs("svc-a", lines=0)
        mock.assert_called_once_with("logs", "svc-a", "--nostream", "--lines", "1")

    def test_lines_negative_clamped_to_one(self):
        with patch("server._run_pm2", return_value=_completed(stdout="")) as mock:
            server.get_logs("svc-a", lines=-5)
        mock.assert_called_once_with("logs", "svc-a", "--nostream", "--lines", "1")


# ---------------------------------------------------------------------------
# Privileged write paths — pm2 failure after the service was found
#
# Every write tool has two distinct failure modes that both return
# {"ok": False, "error": ...}:
#
#   1. the service is not registered  -> _find_service returns None, pm2 is never invoked
#   2. the service IS registered and the pm2 verb itself fails
#
# The tests above cover (1). These cover (2), which was the whole of this
# module's uncovered surface. Each one asserts the verb was actually ATTEMPTED
# as well as that the error is reported — without that call assertion a test
# passes just as happily against mode (1), so it would not distinguish "pm2
# failed" from "we never called pm2", and the guard could invert without any
# test going red.
# ---------------------------------------------------------------------------


class TestWriteToolPm2Failure:
    def test_restart_service_reports_pm2_failure(self):
        proc = _make_process()
        with patch(
            "server._run_pm2",
            side_effect=[
                _jlist_result(proc),
                RuntimeError("pm2 restart svc-a failed (rc=1): boom"),
            ],
        ) as mock:
            result = server.restart_service("svc-a")

        assert result["ok"] is False
        assert "pm2 restart svc-a failed" in result["error"]
        mock.assert_any_call("restart", "svc-a")

    def test_stop_service_reports_pm2_failure(self):
        proc = _make_process()
        with patch(
            "server._run_pm2",
            side_effect=[_jlist_result(proc), RuntimeError("pm2 stop svc-a failed (rc=1): boom")],
        ) as mock:
            result = server.stop_service("svc-a")

        assert result["ok"] is False
        assert "pm2 stop svc-a failed" in result["error"]
        mock.assert_any_call("stop", "svc-a")

    def test_start_service_reports_pm2_failure(self):
        proc = _make_process(status="stopped")
        with patch(
            "server._run_pm2",
            side_effect=[_jlist_result(proc), RuntimeError("pm2 start svc-a failed (rc=1): boom")],
        ) as mock:
            result = server.start_service("svc-a")

        assert result["ok"] is False
        assert "pm2 start svc-a failed" in result["error"]
        mock.assert_any_call("start", "svc-a")

    def test_reload_service_reports_pm2_failure(self):
        proc = _make_process()
        with patch(
            "server._run_pm2",
            side_effect=[_jlist_result(proc), RuntimeError("pm2 reload svc-a failed (rc=1): boom")],
        ) as mock:
            result = server.reload_service("svc-a")

        assert result["ok"] is False
        assert "pm2 reload svc-a failed" in result["error"]
        mock.assert_any_call("reload", "svc-a")

    def test_flush_logs_reports_pm2_failure(self):
        proc = _make_process()
        with patch(
            "server._run_pm2",
            side_effect=[_jlist_result(proc), RuntimeError("pm2 flush svc-a failed (rc=1): boom")],
        ) as mock:
            result = server.flush_logs("svc-a")

        assert result["ok"] is False
        assert "pm2 flush svc-a failed" in result["error"]
        mock.assert_any_call("flush", "svc-a")


class TestGetStatusDegraded:
    def test_reports_version_but_empty_counts_when_jlist_fails(self):
        """pm2 answers --version but jlist fails — the two except branches are independent.

        The existing test_pm2_unavailable_returns_unknown_version drives the OTHER
        direction (--version fails, jlist succeeds). Both are needed: get_status has
        two separate try/except blocks and covering one says nothing about the other.
        """
        with patch(
            "server._run_pm2",
            side_effect=[
                _completed(stdout="5.0.0\n"),
                RuntimeError("pm2 jlist failed (rc=1): daemon unreachable"),
            ],
        ):
            result = server.get_status()

        assert result["pm2_version"] == "5.0.0"
        assert result["service_count"] == 0
        assert result["status_counts"] == {}

    def test_reports_unknown_version_and_empty_counts_when_pm2_is_absent(self):
        """Both calls fail — the degraded path must still return a well-formed dict."""
        with patch(
            "server._run_pm2",
            side_effect=[
                RuntimeError("pm2 --version failed"),
                RuntimeError("pm2 jlist failed"),
            ],
        ):
            result = server.get_status()

        assert result["pm2_version"] == "unknown"
        assert result["service_count"] == 0
        assert result["status_counts"] == {}
        assert result["host"] == "127.0.0.1"
        assert result["port"] == 8486


# ---------------------------------------------------------------------------
# _run_pm2 — the subprocess boundary itself
#
# These exist because of the mutation pilot (vikunja#769), and they guard the
# same class of defect as vikunja#767: a refactor silently dropping one of the
# kwargs that make this call safe. Asserting the FULL set together — rather
# than one kwarg per test — is what makes a deleted argument fail, since a
# missing kwarg is not a wrong value and an `assert kwargs.get(x) != y` style
# check sails straight past it.
# ---------------------------------------------------------------------------


class TestRunPm2SubprocessContract:
    def test_passes_the_full_subprocess_kwarg_set(self, monkeypatch):
        """capture_output, text, encoding and env must ALL be present and correct.

        `is True` rather than a truthiness check on purpose: `capture_output=None`
        and `capture_output=False` are both falsey and both wrong, and a plain
        `assert kwargs["capture_output"]` would not tell them apart from each other
        or from the correct value being replaced by any other truthy object.
        """
        monkeypatch.setenv("PM2_MCP_CONTRACT_PROBE", "present")
        with patch("subprocess.run", return_value=_completed(stdout="[]")) as mock:
            server._run_pm2("jlist")

        kwargs = mock.call_args.kwargs

        # Presence, separately from value — these are the "kwarg deleted entirely" mutants.
        for required in ("capture_output", "text", "encoding", "env"):
            assert required in kwargs, f"_run_pm2 stopped passing {required}= to subprocess.run"

        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        # Exact literal, which also pins the casing. `encoding="UTF-8"` selects the identical
        # codec — Python's codec lookup is case-insensitive — so this assertion is stricter
        # than behaviour requires and kills a mutant that is genuinely equivalent. That is a
        # deliberate trade: the alternative is excluding that one mutant from the CI gate by
        # its generated number (server.x__run_pm2__mutmut_17), and those numbers shift
        # whenever this function is edited, which would silently start excluding some OTHER,
        # real mutant. A test that fails loudly on a harmless casing change is the safer of
        # the two failure modes.
        assert kwargs["encoding"] == "utf-8"

        # env must be the scrubbed copy, not None (inherit-everything) and not os.environ.
        assert kwargs["env"] is not None
        assert kwargs["env"]["PM2_MCP_CONTRACT_PROBE"] == "present"

        # Command still goes as a list, so there is no shell to inject into.
        assert mock.call_args.args[0] == ["pm2", "jlist"]
        assert "shell" not in kwargs or kwargs["shell"] is False

    def test_error_message_joins_args_with_a_single_space(self):
        """A multi-argument command must render as `pm2 restart svc-a`, not `pm2 restartsvc-a`.

        Deliberately multi-arg. The pre-existing failure test called `_run_pm2("jlist")`,
        and with a single argument every possible join separator produces the same string —
        so it could not see a corrupted separator at all. This is the same shape as a
        count assertion at n=1.
        """
        with (
            patch("subprocess.run", return_value=_completed(returncode=1, stderr="boom")),
            pytest.raises(RuntimeError) as excinfo,
        ):
            server._run_pm2("restart", "svc-a")

        assert "pm2 restart svc-a failed (rc=1): boom" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Environment allowlist (vikunja#610)
# ---------------------------------------------------------------------------


class TestEnvAllowlist:
    def test_enforce_mode_keeps_only_allowlisted_names(self, monkeypatch):
        monkeypatch.setattr(server, "_ENV_MODE", "enforce")
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("HOME", "/home/ted")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp-should-not-survive")
        monkeypatch.setenv("SOME_APP_SECRET", "also-not")

        env = server._clean_env("restart", "svc-a")

        assert env["PATH"] == "/usr/bin"
        assert env["HOME"] == "/home/ted"
        # The point of inverting the list: these were never named anywhere, and that is
        # exactly why the denylist let them through.
        assert _leaked_keys(env, ("GITHUB_TOKEN", "SOME_APP_SECRET")) == []
        assert set(env) <= server._CHILD_ENV_ALLOWLIST

    def test_enforce_mode_excludes_ipc_and_claude_by_construction(self, monkeypatch):
        """Not by a rule that could be edited out — they are simply not on the list."""
        monkeypatch.setattr(server, "_ENV_MODE", "enforce")
        for var in server._PM2_IPC_ENV_VARS:
            monkeypatch.setenv(var, "leaked")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-leaked")
        monkeypatch.setenv("CLAUDECODE", "1")

        env = server._clean_env("jlist")

        assert _leaked_keys(env, server._PM2_IPC_ENV_VARS) == []
        assert _leaked_keys(env, ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDECODE")) == []
        # Neither family appears in the allowlist itself, which is the stronger statement.
        assert _leaked_keys(server._CHILD_ENV_ALLOWLIST, server._PM2_IPC_ENV_VARS) == []
        assert not any(k.startswith("CLAUDE") for k in server._CHILD_ENV_ALLOWLIST)

    def test_allowlist_carries_pm2_home(self):
        """PM2_HOME selects which daemon the CLI talks to.

        Dropping it does not error — it silently spawns a second daemon and reports an
        empty process list. On this host it equals pm2's own default so the omission would
        be invisible; on a host with a custom PM2_HOME every read would return nothing.
        """
        assert "PM2_HOME" in server._CHILD_ENV_ALLOWLIST

    def test_shadow_mode_is_the_default_and_does_not_change_behaviour(self, monkeypatch):
        monkeypatch.setattr(server, "_ENV_MODE", "shadow")
        monkeypatch.setenv("SOME_APP_SECRET", "still-here-in-shadow")

        env = server._clean_env("jlist")

        # Shadow must be a no-op on the returned value, or it isn't shadow.
        assert env["SOME_APP_SECRET"] == "still-here-in-shadow"
        assert env == server._denylist_env()

    def test_shadow_mode_logs_withheld_names_and_the_command(self, monkeypatch, caplog):
        monkeypatch.setattr(server, "_ENV_MODE", "shadow")
        monkeypatch.setenv("SOME_APP_SECRET", "canary-value-must-not-be-logged")

        with caplog.at_level(logging.INFO, logger="pm2_mcp"):
            server._clean_env("restart", "svc-a")

        text = caplog.text
        assert "SOME_APP_SECRET" in text, "withheld variable names must be reported"
        # Per vikunja#610: the command is what makes the line actionable. A report that
        # only describes the parent environment is identical on every call.
        assert "restart svc-a" in text

    def test_shadow_log_never_contains_a_value(self, monkeypatch, caplog):
        """The whole feature is about credentials. Logging their values would invert it."""
        monkeypatch.setattr(server, "_ENV_MODE", "shadow")
        monkeypatch.setenv("SOME_APP_SECRET", "canary-value-must-not-be-logged")
        monkeypatch.setenv("ANOTHER_SECRET", "second-canary-abcdef")

        with caplog.at_level(logging.INFO, logger="pm2_mcp"):
            server._clean_env("jlist")

        assert "canary-value-must-not-be-logged" not in caplog.text
        assert "second-canary-abcdef" not in caplog.text

    def test_run_pm2_passes_the_command_through_to_the_shadow_report(self, monkeypatch, caplog):
        """The call site must forward argv, or every shadow line says '<no command>'."""
        monkeypatch.setattr(server, "_ENV_MODE", "shadow")
        monkeypatch.setenv("SOME_APP_SECRET", "x")

        with (
            caplog.at_level(logging.INFO, logger="pm2_mcp"),
            patch("subprocess.run", return_value=_completed(stdout="[]")),
        ):
            server._run_pm2("reload", "svc-b")

        assert "reload svc-b" in caplog.text
        assert "<no command>" not in caplog.text

    def test_shadow_report_is_empty_when_nothing_would_be_withheld(self, monkeypatch, caplog):
        """Negative control: with nothing outside the allowlist, there is nothing to say.

        Without this, a report that fired unconditionally would satisfy every assertion
        above while telling an operator nothing.

        The two helpers are patched rather than os.environ — see the note on
        test_shadow_report_has_a_stable_parseable_shape.
        """
        monkeypatch.setattr(server, "_ENV_MODE", "shadow")
        only_allowed = {"PATH": "/usr/bin", "HOME": "/home/ted"}
        monkeypatch.setattr(server, "_denylist_env", lambda: dict(only_allowed))
        monkeypatch.setattr(server, "_allowlist_env", lambda: dict(only_allowed))

        with caplog.at_level(logging.INFO, logger="pm2_mcp"):
            server._clean_env("jlist")

        assert caplog.text == ""

    def test_shadow_report_has_a_stable_parseable_shape(self, monkeypatch, caplog):
        """The shadow report is this change's actual deliverable — an operator reads it to
        decide whether enforcement is safe. Its shape is therefore a contract, not
        decoration: a mangled separator runs every withheld name together into a single
        token, and a report nobody can parse is the same as no report.

        NEVER replace os.environ wholesale in a test in this repo. mutmut's trampoline
        reads MUTANT_UNDER_TEST from os.environ on every call to decide which variant of a
        function to run, so a test that swaps os.environ for a plain dict silently runs the
        ORIGINAL function under every mutant. Such a test still counts for coverage while
        being incapable of killing anything — and mutmut drops it from the stats mapping
        entirely. Three tests here were written that way first; all four surviving mutants
        in _clean_env were surviving for exactly that reason. Shape the environment with
        monkeypatch.setenv, or patch the helpers.
        """
        monkeypatch.setattr(server, "_ENV_MODE", "shadow")
        monkeypatch.setenv("PM2_MCP_CANARY_ONE", "value-one")
        monkeypatch.setenv("PM2_MCP_CANARY_TWO", "value-two")

        with caplog.at_level(logging.INFO, logger="pm2_mcp"):
            server._clean_env("restart", "svc-a")

        assert len(caplog.records) == 1
        message = caplog.records[0].getMessage()

        prefix = "env-allowlist shadow: pm2 restart svc-a would lose "
        assert message.startswith(prefix)

        names = message[len(prefix) :].split(",")
        expected = sorted(set(server._denylist_env()) - set(server._allowlist_env()))
        # Splitting on "," must recover exactly the withheld names. A corrupted separator
        # fails here even though the names are all still present in the raw string.
        assert names == expected
        assert "PM2_MCP_CANARY_ONE" in names
        assert "PM2_MCP_CANARY_TWO" in names

    def test_shadow_report_names_the_missing_command_explicitly(self, monkeypatch, caplog):
        """_clean_env() called with no argv still has to produce a readable line.

        Nothing in the server does this today — _run_pm2 always forwards its args — but the
        placeholder is reachable and an empty gap where the command should be would read as
        a truncated log line rather than as a caller that passed nothing.
        """
        monkeypatch.setattr(server, "_ENV_MODE", "shadow")
        monkeypatch.setenv("PM2_MCP_CANARY_ONE", "value-one")

        with caplog.at_level(logging.INFO, logger="pm2_mcp"):
            server._clean_env()

        message = caplog.records[0].getMessage()
        assert message.startswith("env-allowlist shadow: pm2 <no command> would lose ")
        assert "PM2_MCP_CANARY_ONE" in message.split(",")


@pytest.fixture
def restored_logger_state():
    """Snapshot and restore every global on the `pm2_mcp` logger.

    _configure_logging() mutates module-level state that outlives the test: it sets the
    level, sets `propagate = False`, and attaches a handler. The propagate flag is the
    dangerous one — pytest's caplog installs its handler on the ROOT logger, so
    propagate False makes `caplog.text` come back EMPTY even under
    `caplog.at_level(..., logger="pm2_mcp")`. Measured on pytest 9.1.1.

    Without this fixture a single test calling _configure_logging() would silently
    hollow out every caplog-based test that happened to run after it — they would still
    pass their `assert x not in caplog.text` lines, because nothing is ever in
    caplog.text. That is the same failure shape as vikunja#772 itself: an assertion that
    holds for the wrong reason.
    """
    original_level = server._log.level
    original_propagate = server._log.propagate
    original_handlers = list(server._log.handlers)
    try:
        yield
    finally:
        server._log.setLevel(original_level)
        server._log.propagate = original_propagate
        server._log.handlers[:] = original_handlers


class TestConfigureLogging:
    """vikunja#772 — the shadow log never reached disk in production.

    Every pre-existing test_shadow_log_* test runs under `caplog.at_level(...)`, which
    FORCES a level the production path never had. They pin the message format correctly
    and are still right; they simply cannot observe that the logger is silent by
    default. These tests exercise the unforced path, which is the one that shipped.
    """

    def test_shadow_log_is_emitted_without_a_test_forced_level(
        self, monkeypatch, restored_logger_state
    ):
        """The regression test for #772. Proven red before the fix.

        Deliberately does NOT use caplog and does NOT touch any level. It attaches its
        own handler and asserts a real emission, so the only thing that can make it pass
        is _configure_logging() having set the logger's level. On the unfixed code the
        record is dropped at the logger's level check before any handler is consulted —
        verified: getEffectiveLevel() 30, isEnabledFor(INFO) False, nothing captured.
        """
        server._configure_logging()

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        server._log.addHandler(handler)
        monkeypatch.setattr(server, "_ENV_MODE", "shadow")
        monkeypatch.setenv("PM2_MCP_UNFORCED_CANARY", "value-must-not-appear")

        server._clean_env("jlist")

        output = stream.getvalue()
        assert "env-allowlist shadow" in output
        assert "PM2_MCP_UNFORCED_CANARY" in output
        # The names-only property has to hold on this path too, not just the caplog one.
        assert "value-must-not-appear" not in output

    def test_configure_logging_enables_info_on_the_logger_itself(self, restored_logger_state):
        """Level and handler are both required; assert the level half directly.

        Setting a handler without the level drops the record at the logger. Setting the
        level without a handler falls through to logging.lastResort, which is WARNING
        and drops it too. This pins the half that #772 actually got wrong.
        """
        assert server._log.getEffectiveLevel() == logging.WARNING

        server._configure_logging()

        assert server._log.level == logging.INFO
        assert server._log.isEnabledFor(logging.INFO)

    def test_configure_logging_attaches_a_handler_and_stops_propagating(
        self, restored_logger_state
    ):
        """propagate=False is what makes the handler immune to uvicorn's dictConfig."""
        server._configure_logging()

        assert server._log.propagate is False
        assert any(getattr(h, server._LOG_HANDLER_TAG, False) for h in server._log.handlers)

    def test_configure_logging_is_idempotent(self, restored_logger_state):
        """PM2 restarts re-run the process, but a double call must not double every line.

        Guarded on an ownership tag rather than isinstance(StreamHandler), because
        pytest's LogCaptureHandler is itself a StreamHandler subclass — an isinstance
        check would mistake a test's handler for ours and skip attaching the real one,
        which is a silent re-introduction of #772 under test.
        """
        server._configure_logging()
        after_first = len(server._log.handlers)

        server._configure_logging()

        assert len(server._log.handlers) == after_first
        owned = [h for h in server._log.handlers if getattr(h, server._LOG_HANDLER_TAG, False)]
        assert len(owned) == 1

    def test_handler_survives_a_uvicorn_shaped_dictconfig(self, restored_logger_state):
        """The specific interaction that made this design non-obvious.

        FastMCP hands log_level to uvicorn, which applies logging.config.dictConfig, and
        dictConfig calls _clearExistingHandlers() — closing and de-registering every
        handler attached before it runs, which is exactly the ordering __main__
        produces. Handler.close() de-registers but does not detach the handler or drop
        its stream, so a handler attached directly to this logger survives. Asserting it
        here means a future Python that changes that behaviour fails the build loudly
        rather than silently re-opening #772.
        """
        import logging.config

        server._configure_logging()
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        server._log.addHandler(handler)

        logging.config.dictConfig(
            {
                "version": 1,
                "disable_existing_loggers": False,
                "formatters": {"default": {"format": "%(message)s"}},
                "handlers": {"default": {"class": "logging.StreamHandler", "formatter": "default"}},
                "loggers": {"uvicorn": {"handlers": ["default"], "level": "INFO"}},
            }
        )

        server._log.info("after dictConfig")

        assert server._log.level == logging.INFO
        assert "after dictConfig" in stream.getvalue()


class TestResolveBind:
    """vikunja#770 — --host/--port were passed by ecosystem.config.js but never read."""

    def test_argv_beats_env(self):
        host, port = _resolve_bind_with(
            ["--host", "127.0.0.2", "--port", "9999"],
            {"MCP_HOST": "127.0.0.3", "MCP_PORT": "8888"},
        )
        assert (host, port) == ("127.0.0.2", 9999)

    def test_env_beats_the_default(self):
        host, port = _resolve_bind_with([], {"MCP_HOST": "127.0.0.4", "MCP_PORT": "7777"})
        assert (host, port) == ("127.0.0.4", 7777)

    def test_default_applies_when_neither_is_set(self):
        host, port = _resolve_bind_with([], {})
        assert (host, port) == ("127.0.0.1", 8486)

    def test_argv_host_alone_leaves_port_on_env(self):
        """Precedence is per-field, not all-or-nothing — the mixed case is the real one.

        ecosystem.config.js can legitimately pass only --host while MCP_PORT comes from
        the environment, and a resolver that fell back to the default port the moment
        any argv appeared would silently move the listener.
        """
        host, port = _resolve_bind_with(["--host", "127.0.0.5"], {"MCP_PORT": "6666"})
        assert (host, port) == ("127.0.0.5", 6666)

    def test_explicit_port_zero_is_not_swapped_for_the_default(self):
        """`args.port or default` would silently turn --port 0 into 8486."""
        _host, port = _resolve_bind_with(["--port", "0"], {})
        assert port == 0

    @pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "localhost", "::1"])
    def test_loopback_forms_are_accepted(self, host):
        resolved, _ = _resolve_bind_with(["--host", host], {})
        assert resolved == host


class TestResolveBindRefusesNonLoopback:
    """NEGATIVE tests on a security boundary — Baseline requires these be explicit.

    pm2-mcp has no authentication on its port and its write verbs can stop or restart
    any PM2 process on the host, including every agent's own broker. A --host that
    accepted 0.0.0.0 would convert a documented-safe posture into a one-word footgun.
    """

    @pytest.mark.parametrize(
        "host",
        [
            "0.0.0.0",
            "::",
            # RFC 5737 / RFC 3849 documentation ranges and RFC 2606 names ONLY.
            # This repo is public: a real host address or a real domain in a fixture
            # publishes deployment topology to anyone who clones it, and git history
            # keeps it after the line is edited. Synthetic values only.
            "192.0.2.10",
            "198.51.100.7",
            "203.0.113.1",
            "10.0.0.1",
            "2001:db8::1",
            "example.com",
            "mcp.example.invalid",
            "",
        ],
    )
    def test_non_loopback_host_is_refused(self, host, capsys):
        with pytest.raises(SystemExit) as excinfo:
            _resolve_bind_with(["--host", host], {})

        assert excinfo.value.code != 0
        assert "refusing to bind" in capsys.readouterr().err

    def test_a_non_loopback_env_host_is_refused_too(self, capsys):
        """The guard belongs to the resolved value, not to the flag.

        MCP_HOST reaches the same bind, so a check that only validated argv would leave
        the exact hole it was added to close — and the env path is the one that has
        been live for months.
        """
        with pytest.raises(SystemExit):
            _resolve_bind_with([], {"MCP_HOST": "0.0.0.0"})

        assert "refusing to bind" in capsys.readouterr().err

    @pytest.mark.parametrize("port", ["-1", "65536", "99999", "-8486"])
    def test_a_port_outside_the_tcp_range_is_refused(self, port, capsys):
        """INFO-1, audit 2026-09-10.

        Not a boundary — the loopback guard runs on `host` independently, so a bad port
        cannot widen the bind. Previously these reached mcp.run() and died with an
        OSError out of socket.bind() well after startup began. Now they refuse in the
        same shape and at the same point as an invalid host.
        """
        with pytest.raises(SystemExit) as excinfo:
            _resolve_bind_with(["--port", port], {})

        assert excinfo.value.code != 0
        assert "outside the valid TCP range" in capsys.readouterr().err

    def test_an_out_of_range_env_port_is_refused_too(self, capsys):
        """The guard belongs to the resolved value, not to the flag — same as the host.

        A check that only validated argv would leave MCP_PORT, which reaches the identical
        bind, unguarded.
        """
        with pytest.raises(SystemExit):
            _resolve_bind_with([], {"MCP_PORT": "70000"})

        assert "outside the valid TCP range" in capsys.readouterr().err

    @pytest.mark.parametrize("port", ["0", "1", "8486", "65535"])
    def test_valid_ports_including_the_boundaries_are_accepted(self, port):
        """Pins both ends of the range, so an off-by-one in either direction fails.

        0 is deliberately valid: it asks the kernel for an ephemeral port. A `1 <= port`
        or `port < 65535` spelling would break one of these four.
        """
        _host, resolved = _resolve_bind_with(["--port", port], {})
        assert resolved == int(port)

    def test_an_explicitly_empty_host_is_refused_not_defaulted(self, capsys):
        """`--host ""` is INADDR_ANY at the socket layer, i.e. the wildcard bind.

        The obvious `args.host or env or default` chain swallows this by falsiness and
        quietly resolves it to 127.0.0.1. Safe by accident, but it means the guard never
        fires on the one spelling of a wildcard bind that does not look like one. An
        explicitly-passed flag is honoured or refused, never silently replaced.

        An empty ENV var is deliberately different — see _resolve_bind. That has been
        treated as unset since v0.1 and resolves to loopback.
        """
        with pytest.raises(SystemExit):
            _resolve_bind_with(["--host", ""], {})

        assert "refusing to bind" in capsys.readouterr().err

    def test_an_empty_env_host_still_falls_back_to_the_default(self):
        """The documented asymmetry, pinned so it is a decision and not a regression."""
        host, _ = _resolve_bind_with([], {"MCP_HOST": ""})
        assert host == "127.0.0.1"

    def test_a_hostname_is_refused_rather_than_resolved(self, capsys):
        """Resolution is not a security boundary.

        A name can point anywhere, and can be repointed after the check passes without
        this process restarting. Refusing outright is the only version of this check
        that stays true.
        """
        with pytest.raises(SystemExit):
            _resolve_bind_with(["--host", "some-internal-host"], {})

        assert "refusing to bind" in capsys.readouterr().err


def _resolve_bind_with(argv, env):
    """Call _resolve_bind with an explicit env, never the real os.environ.

    Passing env in rather than monkeypatching means these tests cannot be affected by,
    or leak into, the MCP_HOST/MCP_PORT the surrounding process happens to carry.
    """
    return server._resolve_bind(argv, env)
