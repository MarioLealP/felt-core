#!/usr/bin/env bash
# Every check CI runs, runnable locally: lint, format, types, file size, layout, TLS checks, the lockfile, pip-audit, tests.
# Meru/nuwa-python's check.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

step() { printf '\n== %s\n' "$*"; }

step ruff
uv run ruff check .
uv run ruff format --check .
step ty
uv run ty check
step "no source file over 500 lines"
if find src plugins -name '*.py' -exec awk 'FNR == 501 { print FILENAME }' {} + | grep .; then
  echo "source files over 500 lines (STANDARDS.md: no god files)"
  exit 1
fi
step "layout: only __init__, __main__ and config at the top of src/<app>"
if find src -mindepth 2 -maxdepth 2 -name '*.py' ! -name __init__.py ! -name __main__.py ! -name config.py | grep .; then
  echo "modules at the top of src/felt_core - move them into a folder (STANDARDS.md, Layout)"
  exit 1
fi
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
