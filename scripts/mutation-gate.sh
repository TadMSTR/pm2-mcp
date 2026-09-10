#!/usr/bin/env bash
#
# Mutation gate — zero surviving mutants in pm2-mcp's trust-boundary functions.
#
# Scope is deliberate and is the point of the pilot (vikunja#769). The repo-wide
# mutation score is ~67% and most of the gap is _parse_summary, whose survivors are
# dict-plumbing and arithmetic mutants worth nothing. A repo-wide percentage target
# would push every hour of effort there while _clean_env — the function that actually
# stops a live credential leaking into `dump.pm2` (vikunja#767) — is already perfect.
# So the gate is scoped to where a survivor would MEAN something, and at that scope it
# demands zero rather than a percentage.
#
# mutmut has no function-level scoping: only_mutate/do_not_mutate take FILE globs, and
# this repo is a single flat server.py. So the scoping happens here, by filtering
# `mutmut results` on the mutant key, which embeds the function name.
#
# Usage: scripts/mutation-gate.sh
#   MUTMUT=.venv/bin/mutmut scripts/mutation-gate.sh   # when not on PATH
set -euo pipefail

MUTMUT="${MUTMUT:-mutmut}"
FUNCS="clean_env|allowlist_env|denylist_env|run_pm2|is_loopback"
RESULTS="mutmut-results.txt"

# Fail loudly rather than letting `|| true` below turn a missing binary into a pass.
if ! command -v "$MUTMUT" >/dev/null 2>&1; then
  echo "FAIL: mutmut not found (tried: $MUTMUT). Install requirements-dev.txt."
  exit 1
fi

# `mutmut run`'s own exit code is NOT the gate, and must not become one. It reports
# non-zero whenever ANY mutant survives anywhere, and _parse_summary's survivors are
# expected and deliberate — gating on it would fail every build for the wrong reason.
echo "==> Running mutmut (exit code intentionally ignored; the gate is below)"
"$MUTMUT" run || true

"$MUTMUT" results > "$RESULTS"

# --- Positive control -------------------------------------------------------------
# Without this, the gate passes vacuously in every case where it is most needed. The
# survivor check below is a grep for bad news, so an EMPTY results file — mutmut
# crashing, aborting at stats collection, or the functions being renamed — produces no
# match and reads exactly like a clean run. Assert the gate actually looked at
# something before trusting it to have found nothing.
evaluated=$("$MUTMUT" results --all true | grep -cE "x__(${FUNCS})__mutmut_[0-9]+" || true)
if [ "$evaluated" -eq 0 ]; then
  echo "FAIL: mutmut evaluated ZERO mutants in the trust-boundary functions."
  echo "      The gate cannot pass on an empty result set — it would be reporting"
  echo "      'no survivors' about code it never tested."
  echo "      Either mutmut did not run, or _clean_env/_run_pm2 have been renamed and"
  echo "      the FUNCS filter in this script needs re-pointing."
  exit 1
fi

# Per-function mutant-count floors. MEASURED 2026-09-09: _clean_env 25, _allowlist_env 1,
# _denylist_env 4, _run_pm2 22.
#
# _clean_env was 4 before the allowlist inversion (vikunja#610) and is 25 after — the
# rewrite added real mutable surface rather than moving it elsewhere. _allowlist_env and
# _denylist_env are new and were added to FUNCS deliberately: the allowlist itself is the
# trust decision now, and leaving it outside the filter would have quietly narrowed this
# gate at the exact moment its subject moved.
#
# These are not decoration, and the reason is worth reading before changing them. A
# zero-survivor check can only fail if a mutant EXISTS to survive, and mutmut mutates
# operators and operands — it has no mutation for "delete a conjunct". So deleting
# `and not k.startswith(_CLAUDE_ENV_PREFIX)` from _clean_env, which is precisely
# regressing vikunja#767, removes 3 of that function's 4 mutants and the survivor check
# then passes on the regressed code.
#
# Verified 2026-09-09, not assumed: with that clause deleted the gate exited 0 and
# reported "zero surviving mutants across 23 mutants". The count floor is what turns
# that silent pass into a failure. (The ordinary test suite does catch this — two tests
# fail — so #767 is protected by tests. The point is that the mutation gate on its own
# is not the thing protecting it, and the earlier claim that it "would have failed a
# weaker version of the #767 fix" is false.)
#
# A DROP means mutable surface was removed. Re-measure and update these deliberately,
# with the date, exactly as with the coverage floor — do not just lower them to go green.
# MEASURED 2026-09-10 (vikunja#770): _is_loopback 6, zero survivors. Added to FUNCS
# because it IS the new trust boundary — it is the predicate that decides whether this
# unauthenticated server, whose write verbs can stop any PM2 process on the host, is
# reachable from off-box. A gate that covered the environment boundary but not the
# network one would be guarding the smaller of the two.
#
# _resolve_bind is deliberately NOT in FUNCS, and this is a decision rather than an
# oversight. It generates 46 mutants of which 15 survive, and ALL 15 are cosmetic:
# argparse `prog=`, `description=` and `help=` strings, plus two `default=None`
# deletions that are genuinely equivalent (argparse's own default default is None).
# Every behavioural mutant in it is killed — verified individually by diff on
# 2026-09-10, not inferred from the total:
#     `if not _is_loopback(host)` -> `if _is_loopback(host)`   killed
#     `_is_loopback(host)`        -> `_is_loopback(None)`      killed
#     every argv/env/default precedence mutant on host and port  killed
#     the refusal message itself                                 killed
# Gating it would therefore fail on help text, and the only way to go green would be
# asserting argparse's --help output — noise that would teach the next person the gate
# is something to work around. If _resolve_bind ever grows behaviour that is not
# delegated to _is_loopback, revisit this and re-measure.
declare -A MIN_MUTANTS=( [clean_env]=25 [allowlist_env]=1 [denylist_env]=4 [run_pm2]=22 [is_loopback]=6 )

for fn in clean_env allowlist_env denylist_env run_pm2 is_loopback; do
  n=$("$MUTMUT" results --all true | grep -cE "x__${fn}__mutmut_[0-9]+" || true)
  floor=${MIN_MUTANTS[$fn]}
  if [ "$n" -eq 0 ]; then
    echo "FAIL: no mutants were generated for _${fn}. Renamed, or no longer mutable?"
    exit 1
  fi
  if [ "$n" -lt "$floor" ]; then
    echo "FAIL: _${fn} generated ${n} mutants, below the measured floor of ${floor}."
    echo "      Mutable surface has been REMOVED from a trust-boundary function, so the"
    echo "      zero-survivor check below no longer covers what it used to. If the"
    echo "      reduction is deliberate, re-measure and update MIN_MUTANTS with today's"
    echo "      date and a note saying what was removed and why."
    exit 1
  fi
  echo "    _${fn}: ${n} mutants evaluated (floor ${floor})"
done

# --- The gate ---------------------------------------------------------------------
if grep -E "^[[:space:]]*server\.x__(${FUNCS})__mutmut_[0-9]+: survived" "$RESULTS"; then
  echo ""
  echo "FAIL: a mutant survived in a trust-boundary function (vikunja#767, #769)."
  echo "      These are the functions that keep session credentials and PM2 IPC vars"
  echo "      out of the environment handed to the pm2 CLI. A survivor here means a"
  echo "      change to that behaviour that no test objects to."
  exit 1
fi

echo ""
echo "PASS: zero surviving mutants across ${evaluated} mutants in the trust-boundary set"
echo "      (_clean_env, _allowlist_env, _denylist_env, _run_pm2, _is_loopback)."
echo "      (_parse_summary survivors are expected and deliberately not gated.)"
