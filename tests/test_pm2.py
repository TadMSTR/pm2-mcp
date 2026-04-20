"""
Tests for pm2-mcp server.py.

All _run_pm2 calls are mocked so no PM2 installation is required to run tests.
"""

import json
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
        with patch("server._run_pm2", return_value=_completed(stdout="line1\nline2\n", stderr="err\n")):
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
        with patch("server._run_pm2", side_effect=[
            _completed(stdout="5.0.0\n"),              # pm2 --version
            _completed(stdout=json.dumps(procs)),      # pm2 jlist
        ]):
            result = server.get_status()

        assert result["pm2_version"] == "5.0.0"
        assert result["service_count"] == 2
        assert result["status_counts"]["online"] == 1
        assert result["status_counts"]["stopped"] == 1

    def test_pm2_unavailable_returns_unknown_version(self):
        procs = []
        with patch("server._run_pm2", side_effect=[
            RuntimeError("pm2 not found"),
            _completed(stdout=json.dumps(procs)),
        ]):
            result = server.get_status()

        assert result["pm2_version"] == "unknown"
        assert result["service_count"] == 0


# ---------------------------------------------------------------------------
# _run_pm2
# ---------------------------------------------------------------------------

class TestRunPm2:
    def test_nonzero_exit_raises_runtime_error(self):
        with patch("subprocess.run", return_value=_completed(returncode=1, stderr="bad")):
            with pytest.raises(RuntimeError, match="rc=1"):
                server._run_pm2("jlist")

    def test_zero_exit_returns_completed_process(self):
        fake = _completed(stdout='[]', returncode=0)
        with patch("subprocess.run", return_value=fake):
            result = server._run_pm2("jlist")
        assert result.returncode == 0

    def test_uses_list_form_not_shell(self):
        """Verify subprocess.run is called with a list (shell=False by default)."""
        with patch("subprocess.run", return_value=_completed(stdout='[]')) as mock:
            server._run_pm2("jlist")

        call_args = mock.call_args
        cmd = call_args[0][0]
        assert isinstance(cmd, list)
        assert cmd[0] == "pm2"
        # shell kwarg must be absent or False
        assert call_args.kwargs.get("shell", False) is False
