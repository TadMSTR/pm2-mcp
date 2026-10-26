"""scripts/mutation-gate.sh, driven through a stub mutmut.

The gate is the fleet's reference for the mutation rule, so its own pass/fail logic gets
tests. A real mutmut run cannot produce the bad statuses on demand (`no tests`,
`not checked`, `timeout`), and those are exactly the ones the gate used to pass: it grepped
for `survived` only (audit 2026-10-25, F-02). The stub prints a results listing; the script
under test is the real one.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parent.parent / "scripts" / "mutation-gate.sh"

# The floors in the script. A listing below them fails for a different reason, so the
# status cases start from a listing that meets every floor.
FLOORS = {"clean_env": 25, "allowlist_env": 1, "denylist_env": 4, "run_pm2": 22, "is_loopback": 6}

STUB = """#!/bin/sh
case "$1" in
  run) exit 0 ;;
  results) cat "$STUB_RESULTS" ;;
esac
"""


def _listing(overrides=None, extra=()):
    lines = []
    for fn, n in FLOORS.items():
        for i in range(1, n + 1):
            key = f"pm2_mcp.server.x__{fn}__mutmut_{i}"
            lines.append(f"    {key}: {(overrides or {}).get(key, 'killed')}")
    lines.extend(extra)
    return "\n".join(lines) + "\n"


def _run_gate(tmp_path, listing):
    if shutil.which("bash") is None:  # pragma: no cover - every CI runner has bash
        pytest.fail("bash is required to test the gate script")
    stub = tmp_path / "mutmut"
    stub.write_text(STUB)
    stub.chmod(0o755)
    results = tmp_path / "listing.txt"
    results.write_text(listing)
    return subprocess.run(
        ["bash", str(GATE)],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "MUTMUT": str(stub), "STUB_RESULTS": str(results)},
        capture_output=True,
        text=True,
    )


def test_all_killed_passes(tmp_path):
    result = _run_gate(tmp_path, _listing())
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS: all 58 trust-boundary mutants killed" in result.stdout


@pytest.mark.parametrize(
    "status", ["survived", "no tests", "not checked", "timeout", "suspicious", "segfault"]
)
def test_any_non_killed_status_fails(tmp_path, status):
    key = "pm2_mcp.server.x__run_pm2__mutmut_7"
    result = _run_gate(tmp_path, _listing({key: status}))
    assert result.returncode == 1
    assert f"{key}: {status}" in result.stdout
    assert "was not killed" in result.stdout


def test_ungated_function_survivors_are_ignored(tmp_path):
    """_parse_summary's survivors are expected; the gate is scoped on purpose."""
    extra = ["    pm2_mcp.server.x__parse_summary__mutmut_1: survived"]
    result = _run_gate(tmp_path, _listing(extra=extra))
    assert result.returncode == 0, result.stdout


def test_count_below_floor_fails(tmp_path):
    listing = _listing().replace("    pm2_mcp.server.x__is_loopback__mutmut_6: killed\n", "")
    result = _run_gate(tmp_path, listing)
    assert result.returncode == 1
    assert "_is_loopback generated 5 mutants, below the measured floor of 6" in result.stdout


def test_empty_listing_fails(tmp_path):
    result = _run_gate(tmp_path, "")
    assert result.returncode == 1
    assert "evaluated ZERO mutants" in result.stdout
