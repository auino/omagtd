#!/usr/bin/env python3
"""Time the engine's hot read paths against a seeded database.

Companion to ``tools/seed.py``: seed once, then run this before and after a
change to see what moved. The paths are the ones the bar poll, the panel and
the fullscreen view actually call.

    python3 tools/seed.py --tasks 10000 --db /tmp/bench.sqlite3
    python3 tools/bench.py --db /tmp/bench.sqlite3
    python3 tools/bench.py --db /tmp/bench.sqlite3 --assert   # non-zero on a miss

Budgets are deliberately loose ceilings, not goals: they catch a regression
that reintroduces an N+1 or a full-table enrich, not jitter on a busy machine.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from gtd import core  # noqa: E402
from gtd.db import Store  # noqa: E402

# name -> milliseconds a run may not exceed (10k tasks).
BUDGET_MS = {
    "bar_snapshot": 60,
    "list tasks (200)": 80,
    "list inbox": 60,
    "list focus": 60,
    "list projects (200)": 80,
    "list tags": 40,
    "tree": 400,
    "search (200)": 200,
    "calendar (month)": 120,
    "show one": 20,
}


def _timed(fn, repeat: int) -> tuple[float, int]:
    samples = []
    size = 0
    for _ in range(repeat):
        start = time.perf_counter()
        result = fn()
        samples.append((time.perf_counter() - start) * 1000.0)
        size = len(result) if isinstance(result, list) else size
    return statistics.median(samples), size


def run(path: str, repeat: int) -> dict[str, tuple[float, int]]:
    store = Store(path=path)
    today = _dt.date.today().replace(day=1).isoformat()[:7]
    some_id = store.scalar("SELECT id FROM items WHERE kind = 'action' LIMIT 1")

    cases = {
        "bar_snapshot": lambda: [core.bar_snapshot(store)],
        "list tasks (200)": lambda: core.list_view(store, "tasks", limit=200),
        "list inbox": lambda: core.list_view(store, "inbox", limit=200),
        "list focus": lambda: core.list_view(store, "focus", limit=200),
        "list projects (200)": lambda: core.list_view(store, "projects", limit=200),
        "list tags": lambda: core.list_view(store, "tags"),
        "tree": lambda: [core.tree(store)],
        "search (200)": lambda: core.search(store, "report", limit=200),
        "calendar (month)": lambda: [core.calendar(store, start=f"{today}-01")],
        "show one": lambda: [core.get(store, some_id)] if some_id else [],
    }

    out = {}
    for name, fn in cases.items():
        median, size = _timed(fn, repeat)
        out[name] = (median, size)
    store.close()
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark the GTD read paths.")
    parser.add_argument("--db", default=os.environ.get("GTD_DB", "/tmp/omagtd-bench.sqlite3"))
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--assert", dest="check", action="store_true",
                        help="exit non-zero if a path exceeds its budget")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"no database at {args.db}; run tools/seed.py first", file=sys.stderr)
        return 2

    results = run(args.db, args.repeat)
    missed = []
    print(f"{'path':<22} {'median ms':>10} {'rows':>6}   budget")
    for name, (median, size) in results.items():
        budget = BUDGET_MS.get(name)
        flag = ""
        if budget is not None:
            if median > budget:
                flag = "  OVER"
                missed.append(name)
            print(f"{name:<22} {median:>10.2f} {size:>6}   <{budget}ms{flag}")
        else:
            print(f"{name:<22} {median:>10.2f} {size:>6}   -")
    if args.check and missed:
        print(f"\nbudget missed: {', '.join(missed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
