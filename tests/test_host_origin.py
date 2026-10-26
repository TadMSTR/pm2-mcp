"""DNS-rebinding protection: the HTTP app refuses a non-loopback Host or any Origin.

These drive the real Starlette app FastMCP builds, with the middleware main() hands to
mcp.run, rather than calling the guard in isolation, so a guard that is defined but never
wired in fails here. The request is MCP `initialize`, which mutates nothing.

Proven red first: against d7cba00 (v0.4.0, no guard) the foreign-Host and foreign-Origin
cases returned 200 and an MCP session.
"""

import asyncio
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
