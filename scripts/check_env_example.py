#!/usr/bin/env python3
"""WP-39: fail if config.py declares a settings field .env.example doesn't
document (ENGINEERING-RULES.md rule 6).

felt-core's FeltConfig is a pydantic-settings BaseSettings with env_prefix
"FELT_" — every class-level annotated attribute maps 1:1 to FELT_<UPPER>.
Cheap regex over the class body, not a real parse: it matches exactly
4-space-indented "name: type" lines, which excludes method defs (8+ space
bodies, no top-level colon-annotation) and model_config (no annotation).
See docs/plans/impl/WP-39-tooling.md for what this does and doesn't catch.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "src" / "felt_core" / "config.py"
ENV_EXAMPLE = ROOT / ".env.example"


def main() -> int:
    src = CONFIG.read_text(encoding="utf-8")
    env = ENV_EXAMPLE.read_text(encoding="utf-8")

    fields = re.findall(r"^\s{4}(\w+):\s", src, re.M)
    keys = {f"FELT_{f.upper()}" for f in fields}

    missing = sorted(k for k in keys if f"{k}=" not in env)
    if missing:
        print(f"Missing from {ENV_EXAMPLE.name}: {', '.join(missing)}")
        return 1
    print(f"OK - {len(keys)} FeltConfig keys all documented in {ENV_EXAMPLE.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
