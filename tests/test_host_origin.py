"""DNS-rebinding protection: the HTTP app refuses a non-loopback Host or any Origin.

These drive the real Starlette app FastMCP builds, with the middleware main() hands to
mcp.run, rather than calling the guard in isolation, so a guard that is defined but never
wired in fails here. The request is MCP `initialize`, which mutates nothing.

The refusal cases were run red first, against the same app built without the middleware,
so none of them can pass vacuously.
"""

import asyncio
import io
import logging
import sys

import pytest
from starlette.testclient import TestClient

from pm2_mcp import server

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("PM2_MCP_ALLOWED_HOSTS", raising=False)

    def make():
        app = server.mcp.http_app(path="/mcp", middleware=server._http_middleware())
        return TestClient(app)

    return make


def _post(client, **headers):
    with client() as c:
        return c.post("/mcp", json=INIT, headers={**HEADERS, **headers})


@pytest.mark.parametrize(
    "host",
    [
        "localhost:8486",
        "127.0.0.1:8486",
        "[::1]:8486",
        # A client behind a port forward sends the forward's local port.
        "127.0.0.1:18486",
    ],
)
def test_loopback_host_is_served(client, host):
    r = _post(client, host=host)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "evil.example:8486",
        "192.0.2.10:8486",
        # A loopback name as a PREFIX of a foreign one. mcp's wildcard match is
        # `startswith(base + ":")`, so this must not pass on the "localhost" prefix.
        "localhost.evil.example:8486",
        # No port. Every real client sends one; the wildcard entries require it.
        "localhost",
    ],
)
def test_foreign_host_is_refused(client, host):
    r = _post(client, host=host)
    assert r.status_code == 421


def test_foreign_origin_is_refused(client):
    r = _post(client, host="localhost:8486", origin="http://evil.example")
    assert r.status_code == 403


@pytest.mark.parametrize(
    "origin", ["http://localhost:8486", "http://127.0.0.1:3005", "http://localhost:7777", "null"]
)
def test_any_origin_is_refused_even_a_loopback_one(client, origin):
    """Only browsers send Origin, and no browser is a client.

    Allowing loopback origins would trust every web app served on this host, which on
    forge includes Dockhand (7777) and PatchMon (3005).
    """
    r = _post(client, host="localhost:8486", origin=origin)
    assert r.status_code == 403


def test_extra_host_from_env(client, monkeypatch):
    monkeypatch.setenv("PM2_MCP_ALLOWED_HOSTS", " pm2.internal:8486 ,")
    assert _post(client, host="pm2.internal:8486").status_code == 200
    # Exact values: another port on that name is still refused.
    assert _post(client, host="pm2.internal:9999").status_code == 421


def test_settings_default_is_loopback_only():
    settings = server._transport_security_settings(env={})
    assert settings.enable_dns_rebinding_protection is True
    assert settings.allowed_hosts == ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    assert settings.allowed_origins == []
    # Set explicitly, not inherited from mcp's field defaults: mcp disables this check by
    # default elsewhere (TransportSecurityMiddleware(None)), and a default can move.
    assert {"enable_dns_rebinding_protection", "allowed_origins"} <= settings.model_fields_set


def test_settings_explicit_env_overrides_os_environ(monkeypatch):
    monkeypatch.setenv("PM2_MCP_ALLOWED_HOSTS", "from-os-environ:1")
    assert server._transport_security_settings(env={}).allowed_hosts == [
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
    ]


def test_settings_reads_os_environ_by_default(monkeypatch):
    monkeypatch.setenv("PM2_MCP_ALLOWED_HOSTS", "a:1,b:2")
    assert server._transport_security_settings().allowed_hosts[-2:] == ["a:1", "b:2"]


def test_refusal_is_logged_without_the_header(client, caplog, monkeypatch):
    """Neither our record nor any library record may carry the caller's header value.

    mcp's own check logs `Invalid Host header: <value>`. A test that only watched
    pm2_mcp's logger could not see that, so every captured record is checked.

    The mcp logger's level is reset first because the guard's silencing is a process-
    global side effect: any earlier test that built the guard has already set it, so
    without the reset this passes even with the silencing deleted. Measured under mutmut,
    whose forked workers inherit the level from the un-mutated run.
    """
    monkeypatch.setattr(logging.getLogger("mcp.server.transport_security"), "level", 0)
    with caplog.at_level(logging.DEBUG):
        _post(client, host="evil.example", origin="http://evil-origin.example")
        _post(client, host="localhost:8486", origin="http://evil-origin.example")
    ours = [r.getMessage() for r in caplog.records if r.name == "pm2_mcp"]
    assert ours == [
        "http request refused: status=421 scope=http",
        "http request refused: status=403 scope=http",
    ]
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "evil.example" not in logged
    assert "evil-origin.example" not in logged


def _ws_scope(host):
    return {
        "type": "websocket",
        "path": "/mcp",
        "headers": [(b"host", host.encode())],
        "query_string": b"",
    }


async def _drive(guard, scope):
    sent = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    await guard(scope, receive, send)
    return sent


def _guard_over_recorder():
    reached = []

    async def app(scope, receive, send):
        reached.append(scope["type"])

    return server.HostOriginGuard(app, server._transport_security_settings(env={})), reached


def test_websocket_with_a_foreign_host_is_closed_unserved():
    guard, reached = _guard_over_recorder()
    sent = asyncio.run(_drive(guard, _ws_scope("evil.example")))
    assert sent == [{"type": "websocket.close", "code": 1008}]
    assert reached == []


def test_websocket_with_a_loopback_host_reaches_the_app():
    guard, reached = _guard_over_recorder()
    asyncio.run(_drive(guard, _ws_scope("localhost:8486")))
    assert reached == ["websocket"]


def test_lifespan_scope_passes_through():
    """Only http and websocket carry headers to check; lifespan must reach the app."""
    guard, reached = _guard_over_recorder()
    asyncio.run(_drive(guard, {"type": "lifespan"}))
    assert reached == ["lifespan"]


def test_main_wires_the_guard(monkeypatch, restored_logger_state):
    """The guard only protects anything if main() hands it to mcp.run."""
    seen = {}
    monkeypatch.setattr(server.mcp, "run", lambda **kw: seen.update(kw))
    server.main(["--port", "8486"])
    [mw] = seen["middleware"]
    assert mw.cls is server.HostOriginGuard
    assert seen["transport"] == "streamable-http"
    assert (seen["host"], seen["port"]) == ("127.0.0.1", 8486)


def test_main_reads_sys_argv_when_not_given(monkeypatch, restored_logger_state):
    seen = {}
    monkeypatch.setattr(server.mcp, "run", lambda **kw: seen.update(kw))
    monkeypatch.setattr(sys, "argv", ["pm2-mcp", "--port", "9999"])
    server.main()
    assert seen["port"] == 9999


@pytest.mark.parametrize("mode", ["enforced", "on", "", "Enforce!"])
def test_main_refuses_an_unknown_env_mode(monkeypatch, restored_logger_state, mode):
    """A typo of "enforce" must not quietly run shadow (v0.5.0 review)."""
    ran = []
    monkeypatch.setattr(server.mcp, "run", lambda **kw: ran.append(kw))
    monkeypatch.setattr(server, "_ENV_MODE", mode)
    with pytest.raises(SystemExit, match="refusing to start: PM2_MCP_ENV_MODE"):
        server.main([])
    assert ran == []


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_main_starts_in_either_known_mode(monkeypatch, restored_logger_state, mode):
    ran = []
    monkeypatch.setattr(server.mcp, "run", lambda **kw: ran.append(kw))
    monkeypatch.setattr(server, "_ENV_MODE", mode)
    server.main([])
    assert len(ran) == 1


def test_get_status_reports_the_bind_main_used(monkeypatch, restored_logger_state):
    """--port wins over MCP_PORT, so get_status must report the flag, not the variable."""
    monkeypatch.setattr(server, "_BIND", None)
    monkeypatch.setattr(server.mcp, "run", lambda **kw: None)
    monkeypatch.setenv("MCP_PORT", "8486")
    server.main(["--host", "127.0.0.2", "--port", "9123"])
    monkeypatch.setattr(server, "_run_pm2", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    status = server.get_status()
    assert (status["host"], status["port"]) == ("127.0.0.2", 9123)


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_get_status_reports_the_env_mode(monkeypatch, mode):
    monkeypatch.setattr(server, "_ENV_MODE", mode)
    monkeypatch.setattr(server, "_run_pm2", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    assert server.get_status()["env_mode"] == mode


def test_get_status_without_main_reports_the_environment(monkeypatch):
    monkeypatch.setenv("MCP_HOST", "127.0.0.9")
    monkeypatch.setenv("MCP_PORT", "8999")
    monkeypatch.setattr(server, "_run_pm2", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    status = server.get_status()
    assert (status["host"], status["port"]) == ("127.0.0.9", 8999)


def test_main_logs_the_bind_and_mode_it_starts_with(monkeypatch, restored_logger_state):
    """The startup line is how an operator confirms the mode after a deploy.

    Captured with a handler on pm2_mcp itself: main() sets propagate False, so caplog,
    which listens on the root logger, would see nothing.
    """
    monkeypatch.setattr(server.mcp, "run", lambda **kw: None)
    monkeypatch.setattr(server, "_ENV_MODE", "enforce")
    stream = io.StringIO()
    server._log.addHandler(logging.StreamHandler(stream))
    server.main(["--port", "8486"])
    assert "starting on 127.0.0.1:8486, env-allowlist mode enforce\n" in stream.getvalue()


def test_unknown_env_mode_message_names_the_value_and_the_choices(
    monkeypatch, restored_logger_state
):
    monkeypatch.setattr(server.mcp, "run", lambda **kw: None)
    monkeypatch.setattr(server, "_ENV_MODE", "x" * 40)
    with pytest.raises(SystemExit) as raised:
        server.main([])
    message = str(raised.value)
    # Bounded: the value is caller-controlled, so at most 32 characters are echoed.
    assert f"PM2_MCP_ENV_MODE={'x' * 32!r} is not one of shadow, enforce." in message
