"""Fixtures shared across the pm2-mcp test modules."""

import pytest

from pm2_mcp import server


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


@pytest.fixture(autouse=True)
def unbound(monkeypatch):
    """main() records its bind in module state; no test may inherit another's."""
    monkeypatch.setattr(server, "_BIND", None)
