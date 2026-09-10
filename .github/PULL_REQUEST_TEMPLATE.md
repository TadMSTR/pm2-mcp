## What this changes

## Why

<!-- Link the issue if there is one. -->

## Checklist

- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `pytest` passes, including the coverage floor in `pyproject.toml`
- [ ] `scripts/mutation-gate.sh` passes if this touches `_clean_env` or `_run_pm2`
- [ ] `CHANGELOG.md` updated under `[Unreleased]`

## If this touches the environment handed to the `pm2` CLI

`_clean_env` and `_run_pm2` are the trust boundary: `pm2 start` and
`pm2 restart --update-env` ship the calling process's entire environment to the daemon as
the base env for the target app, and PM2 writes it to `~/.pm2/dump.pm2` on the next
`pm2 save`. A regression here does not fail loudly — it silently persists whatever the
calling shell happened to be carrying.

- [ ] The mutation gate still reports zero survivors **and** its per-function mutant counts
      have not dropped. A falling count means mutable surface was removed and the gate now
      covers less than it did — see the note in `scripts/mutation-gate.sh`.
- [ ] New tests assert on key *names*, not on the environment dict, so a failure doesn't
      print every variable and value into the log.
