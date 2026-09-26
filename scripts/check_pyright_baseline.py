#!/usr/bin/env python3
"""WP-39: fail CI if pyright's error count exceeds this WP's recorded
baseline, so CI gates on *new* type errors without requiring the existing
backlog to be fixed first. See docs/plans/impl/WP-39-tooling.md for the
baseline count and why it wasn't fixed this WP. `.pyright-baseline` holds
a single integer.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE_FILE = ROOT / ".pyright-baseline"


def main() -> int:
    baseline = int(BASELINE_FILE.read_text().strip())
    proc = subprocess.run(
        ["uv", "run", "pyright", "--outputjson"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        return 2

    count = data["summary"]["errorCount"]
    print(f"pyright: {count} errors (baseline {baseline})")
    if count > baseline:
        print(
            "New pyright errors introduced - fix them, or if the increase is "
            "deliberate and reviewed, update .pyright-baseline with why."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
