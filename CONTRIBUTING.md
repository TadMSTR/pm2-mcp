# Contributing

Thanks for looking. This is a small server, one module; most changes are small too.

## Setup

```bash
uv sync --frozen --extra dev     # the locked set CI uses
.venv/bin/pre-commit install     # optional, mirrors the CI lint job
```

Dependencies live in `pyproject.toml` and are locked in `uv.lock`. After changing one, run
`uv lock` and commit both files; CI's `uv lock --check` fails a stale lock.

No PM2 installation is needed for the default test run, which mocks `_run_pm2`. The
integration tests (`-m integration`) need pm2 on `PATH`: they start a private daemon in a
temporary `PM2_HOME` and refuse to run any pm2 command outside it.

## The loop

```bash
.venv/bin/pytest                    # coverage and its floor come from pyproject.toml
.venv/bin/ruff check .
.venv/bin/ruff format .
```

`pytest` enforces the coverage floor on its own — there is no separate CI-only flag. If your
change adds a line, it needs a test, or the run goes red locally before it ever reaches CI.

## If you touch `_clean_env` or `_run_pm2`

These two functions are the trust boundary. Run the mutation gate as well:

```bash
rm -rf mutants && PATH="$PWD/.venv/bin:$PATH" ./scripts/mutation-gate.sh
```

Delete `mutants/` first: mutmut caches which tests cover which function there, and a stale
cache reports mutants a new test kills as survivors.

It requires **zero surviving mutants** in those two functions, and separately requires that
the number of mutants generated for each has not dropped below a recorded floor. Read the
comments in that script before changing either number — the count floor exists because
deleting a condition removes the mutants that would have caught its deletion, so a
survivor-only check passes on the regressed code.

The gate is deliberately **not** a repo-wide mutation score. `_parse_summary` has ~39
surviving mutants and they are expected: they are dict-field and arithmetic mutations, and
chasing them produces tests that assert dict plumbing. Please don't "finish the job" there.

### Writing tests around the environment

Assert on key **names**, never on the environment dict:

```python
# no — pytest renders the whole container on failure, values included
assert "CLAUDE_CODE_OAUTH_TOKEN" not in env

# yes
assert _leaked_keys(env, ("CLAUDE_CODE_OAUTH_TOKEN",)) == []
```

These tests run on machines whose ambient environment holds real credentials. A failing
assertion should not print them.

**Never replace `os.environ` wholesale.** Not `monkeypatch.setattr(os, "environ", {...})`,
not a `dict` swap of any kind. mutmut wraps every mutated function in a trampoline that reads
`MUTANT_UNDER_TEST` from `os.environ` *on each call* to choose which variant to run. A test
that swaps `os.environ` for a plain dict therefore runs the **original** function under every
mutant, so it can never kill one — and mutmut drops it from the stats mapping entirely.

The trap is that such a test looks completely healthy: it passes, it raises coverage, and it
reads as thorough. Three tests here were written that way, and every surviving mutant in
`_clean_env` was surviving for that reason alone. Shape the environment with
`monkeypatch.setenv` / `delenv`, or patch `_allowlist_env` / `_denylist_env` directly.

## Style

- `ruff` decides formatting; don't hand-format around it.
- Comments should say **why**, and ideally cite the incident. Several comments in this repo
  name a ticket number — that is the house style, and it is why the `CLAUDE*` prefix match
  has survived people's urge to "tidy" it into an explicit list.
- Tests are named for the scenario: `test_restart_service_reports_pm2_failure`, not
  `test_restart_service_2`.

## Pull requests

Branch, don't commit to `main`. Update `CHANGELOG.md` under `[Unreleased]`. CI installs
from `uv.lock` and runs lint, tests on Python 3.11/3.12/3.13, the mutation gate, the real-pm2
integration tests, a test against the newest versions `pyproject.toml` allows, `pip-audit
--strict` over the locked runtime and dev sets, and a build job that installs the wheel into
a clean venv and imports from *that* rather than from the checkout. `secret-scan.yml` and
`codeql.yml` run alongside.

That last job exists for a reason worth knowing: the package was unbuildable for two
releases, and CI stayed green the whole time because its smoke test imported `server` from
the source tree. If you change anything in `[build-system]` or `[tool.setuptools]`, build
locally and look inside the wheel:

```bash
uv build
python -c "import zipfile,glob; print(zipfile.ZipFile(glob.glob('dist/*.whl')[0]).namelist())"
```

## Scope

pm2-mcp wraps the `pm2` CLI for local agent use. It does not authenticate callers, register
new processes, or delete them. Those are deliberate boundaries — see `ARCHITECTURE.md` and
`docs/threat-model.md`. Proposals to cross one are welcome, but they change what the server
is, so open an issue before writing the code.

## Security

Don't open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).
