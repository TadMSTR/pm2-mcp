"""
Tests for pm2-mcp server.py.

All _run_pm2 calls are mocked so no PM2 installation is required to run tests.
"""

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
