#!/usr/bin/env python3
"""Fill a throwaway database with a realistic, large task set.

Not part of the product: a development tool for measuring the engine at a size
a real GTD practitioner reaches after years, so a change can be judged against
ten thousand tasks instead of ten. It writes rows directly rather than through
``gtd.core`` because the point is to reach 10k quickly and in one transaction,
and it stamps a spread of dates so the views have something to sort.

    python3 tools/seed.py --tasks 10000 --db /tmp/bench.sqlite3
    GTD_DB=/tmp/bench.sqlite3 python3 tools/bench.py

The database it writes is disposable; delete it when done.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from gtd.db import STAMP, Store, new_id, now  # noqa: E402

CONTEXTS = [
    "home", "office", "computer", "phone", "errands", "online", "calls",
    "home>kitchen", "home>garden", "home>garage", "office>meetings",
    "office>writing", "office>reports", "review>weekly", "review>monthly",
    "shop>groceries", "shop>hardware", "family", "health", "finance",
    "reading", "someday", "waiting", "travel", "car", "pets", "admin",
    "email", "research", "planning",
]
VERBS = ["Draft", "Review", "Send", "Call", "Buy", "Plan", "Fix", "Read",
         "Write", "Book", "Prepare", "File", "Email", "Describe", "Outline",
         "Test", "Deploy", "Schedule", "Confirm", "Archive"]
NOUNS = ["the proposal", "the report", "the invoice", "a quote", "the deck",
         "the draft", "the budget", "the roadmap", "the notes", "the spec",
         "the tickets", "the backlog", "the slides", "the summary",
         "the contract", "the venue", "the tickets", "the parts", "the lab"]
PROJECTS = ["Website refresh", "Q3 planning", "Home renovation", "Tax season",
            "Kitchen remodel", "Team offsite", "Book draft", "Conference talk",
            "Move house", "Garden redesign", "Car service", "Reading list",
            "Health checkups", "Holiday planning", "Budget review",
            "CRM migration", "Onboarding docs", "Supplier audit"]
AREAS = ["Personal", "Work", "Home", "Finance", "Health", "Family",
         "Learning", "Community", "Travel", "Side projects"]


def _stamp(delta_days: float) -> str:
    return (_dt.datetime.now() + _dt.timedelta(days=delta_days)).strftime(STAMP)


def seed(path: str, tasks: int, seed_value: int = 7) -> None:
    rng = random.Random(seed_value)
    store = Store(path=path)
    db = store.db
    db.execute("BEGIN")
    try:
        stamp = now()
        tag_ids: dict[str, str] = {}
        for name in CONTEXTS:
            tag_id = new_id()
            parent_id = None
            if ">" in name:
                parent_id = tag_ids.get(name.rsplit(">", 1)[0])
            tag_ids[name] = tag_id
            db.execute(
                "INSERT INTO tags(id, name, parent_id, review, created_at, updated_at) "
                "VALUES(?, ?, ?, 'never', ?, ?)", (tag_id, name, parent_id, stamp, stamp))

        area_ids = []
        for title in AREAS:
            item_id = new_id()
            area_ids.append(item_id)
            db.execute(
                "INSERT INTO items(id, kind, title, status, priority, rank, "
                "created_at, updated_at) VALUES(?, 'area', ?, 'active', 0, 0, ?, ?)",
                (item_id, title, stamp, stamp))

        project_ids = []
        for title in PROJECTS:
            parent = rng.choice(area_ids) if rng.random() < 0.85 else None
            item_id = new_id()
            project_ids.append(item_id)
            db.execute(
                "INSERT INTO items(id, kind, title, status, priority, parent_id, "
                "rank, created_at, updated_at) VALUES(?, 'project', ?, 'active', 0, ?, ?, ?, ?)",
                (item_id, title, parent, 0, _stamp(-rng.uniform(30, 500)), stamp))
            # A few subprojects, one level deep.
            for extra in range(rng.randint(0, 2)):
                sub = new_id()
                project_ids.append(sub)
                db.execute(
                    "INSERT INTO items(id, kind, title, status, priority, parent_id, "
                    "rank, created_at, updated_at) VALUES(?, 'project', ?, 'active', 0, ?, ?, ?, ?)",
                    (sub, f"{title} — part {extra + 1}", item_id, 0,
                     _stamp(-rng.uniform(10, 200)), stamp))

        total = tasks
        for n in range(total):
            roll = rng.random()
            if roll < 0.70:
                status = "active"
            elif roll < 0.80:
                status = "inbox"
            elif roll < 0.85:
                status = "waiting"
            elif roll < 0.89:
                status = "someday"
            elif roll < 0.92:
                status = "reference"
            elif roll < 0.97:
                status = "done"
            elif roll < 0.99:
                status = "dropped"
            else:
                status = "trashed"

            parent = None
            if status not in ("inbox",) and rng.random() < 0.75 and project_ids:
                parent = rng.choice(project_ids)

            priority = 1 if rng.random() < 0.12 else 0
            due_at = defer_at = None
            if rng.random() < 0.35:
                due_at = _stamp(rng.uniform(-40, 60))
            if rng.random() < 0.25:
                defer_at = _stamp(rng.uniform(-30, 45))
            if defer_at and due_at and defer_at[:10] > due_at[:10]:
                defer_at, due_at = due_at, defer_at
            duration = rng.choice([None, 15, 30, 45, 60, 90, 120])
            waiting_for = "the bank" if status == "waiting" and rng.random() < 0.5 else None
            created = _stamp(-rng.uniform(0, 730))
            updated = _stamp(-rng.uniform(0, min(60, 730)))
            completed_at = _stamp(-rng.uniform(0, 45)) if status == "done" else None
            dropped_at = _stamp(-rng.uniform(0, 45)) if status == "dropped" else None
            completed = 0
            title = f"{rng.choice(VERBS)} {rng.choice(NOUNS)} {n}"

            item_id = new_id()
            db.execute(
                "INSERT INTO items(id, kind, title, notes, parent_id, status, "
                "priority, defer_at, due_at, duration, completed_at, dropped_at, "
                "waiting_for, rank, created_at, updated_at, completed) "
                "VALUES(?, 'action', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item_id, title, "" if rng.random() > 0.3 else "some notes here",
                 parent, status, priority, defer_at, due_at, duration,
                 completed_at, dropped_at, waiting_for, rng.randint(0, 100),
                 created, updated, completed))

            if rng.random() < 0.45:
                for ctx in rng.sample(CONTEXTS, rng.randint(1, 2)):
                    db.execute(
                        "INSERT OR IGNORE INTO item_tags(item_id, tag_id) VALUES(?, ?)",
                        (item_id, tag_ids[ctx]))

        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    finally:
        store.close()
    print(f"seeded {tasks} tasks into {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed a large GTD database.")
    parser.add_argument("--tasks", type=int, default=10000)
    parser.add_argument("--db", default=os.environ.get("GTD_DB", "/tmp/omagtd-bench.sqlite3"))
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    seed(args.db, args.tasks, args.seed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
