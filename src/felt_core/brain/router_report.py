"""What the semantic router would have done (cm-6b0 shadow mode) vs what
Felt did - run inside the felt-core container:

    docker exec felt-core /app/.venv/bin/python -m felt_core.brain.router_report [--all]

Prints agreement among its sure picks, how much it would hand to felt-llm,
and every sure pick that differs (the ones to fix with examples, or where
the router was right and the old route wrong).
"""

import argparse
import sys
from collections import Counter

from felt_core.brain.router import agrees, shadow_rows
from felt_core.config import load_config


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="list every row, not just differences")
    args = ap.parse_args()
    rows = shadow_rows(load_config().db_path)
    if not rows:
        print("no shadow records yet")
        return 0
    sure = [r for r in rows if r["sure"]]
    agree = [r for r in sure if agrees(r["pick"], r["actual"])]
    print(f"{len(rows)} messages since {rows[0]['at']}")
    print(f"  sure: {len(sure)} - agree {len(agree)}, differ {len(sure) - len(agree)}")
    print(f"  unsure (would go to felt-llm): {len(rows) - len(sure)}")
    print(f"  of the fast-path ones: {sum(r['fast'] for r in rows)}")
    pairs = Counter((r["pick"], r["actual"]) for r in sure if not agrees(r["pick"], r["actual"]))
    if pairs:
        print("\nmost common differences (router -> actual):")
        for (pick, actual), n in pairs.most_common(10):
            print(f"  {n:3} x {pick} -> {actual}")
    print()
    for r in rows:
        differ = not agrees(r["pick"], r["actual"])
        if args.all or (r["sure"] and differ):
            mark = "!!" if differ else "  "
            route = f"router={r['pick']} actual={r['actual']}"
            print(f"{mark} {r['score']:.2f} {r['via']:9} {route}: {r['message'][:70]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
