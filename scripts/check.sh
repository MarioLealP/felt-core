#!/usr/bin/env bash
# Every check CI runs, runnable locally: lint, format, types, file size, layout, TLS checks, the lockfile, pip-audit, tests.
# Meru/nuwa-python's check.sh, with one difference: felt-core predates the standard, so the file-size and layout checks
# read a baseline (scripts/baseline/) of what failed on 2026-11-10. A file that fails and isn't listed fails the check,
# and so does a listed file that no longer fails - fix a file, delete its line.
set -euo pipefail
cd "$(dirname "$0")/.."
# one sort order everywhere: comm needs both sides sorted the same way (a laptop locale sorts _ differently)
export LC_ALL=C

step() { printf '\n== %s\n' "$*"; }

# against_baseline <baseline file> <what> - stdin: the files failing now, one per line
against_baseline() {
  local baseline=$1 what=$2 now
  now=$(sort)
  local new stale
  new=$(comm -23 <(printf '%s\n' "$now" | sed '/^$/d') "$baseline")
  stale=$(comm -13 <(printf '%s\n' "$now" | sed '/^$/d') "$baseline")
  if [[ -n $new ]]; then
    printf '%s\n' "$new"
    echo "new $what (STANDARDS.md); not in $baseline"
    return 1
  fi
  if [[ -n $stale ]]; then
    printf '%s\n' "$stale"
    echo "fixed - delete these lines from $baseline"
    return 1
  fi
}

step ruff
uv run ruff check .
uv run ruff format --check .
step ty
uv run ty check
step "no source file over 500 lines"
find src plugins -name '*.py' -exec awk 'FNR == 501 { print FILENAME }' {} + |
  against_baseline scripts/baseline/over-500.txt "source files over 500 lines"
step "layout: only __init__, __main__ and config at the top of src/<app>"
find src -mindepth 2 -maxdepth 2 -name '*.py' ! -name __init__.py ! -name __main__.py ! -name config.py |
  against_baseline scripts/baseline/loose-modules.txt "modules at the top of src/felt_core - move them into a folder"
step "TLS checks stay on"
# ruff's rule for this only knows requests and httpx by name, not httpx2.
if grep -rn 'verify=False' src plugins; then
  exit 1
fi
step "uv.lock up to date"
uv lock --check
step pip-audit
uvx pip-audit --disable-pip --require-hashes --progress-spinner off -r <(uv export --frozen --no-emit-project --quiet)
# Exception to the standard's 2 minutes (Mario 2026-11-10): felt-core's CI still runs on the old shared ci-worker
# (4 CPUs for every job at once), where the suite took 96 s to over 120 s while it runs in 10-15 s on a desktop.
# Back to 120 when felt-core's CI moves to Meru's runner.
step "pytest, 5 minutes at most (Meru: 2 - see above)"
timeout 300 uv run pytest -q
printf '\nall checks passed\n'
