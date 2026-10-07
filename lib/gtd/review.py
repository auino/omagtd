"""Step 4: reflect.

Two cadences, because they answer different questions.

`daily` is "what is true right now": what came in, what is overdue, what is
waiting. Two minutes, once or twice a day.

`weekly` is the GTD Weekly Review: the empty-inbox checklist, worked through
in order, each step already carrying the items it is about so the work is
deciding rather than hunting. On top of that it carries OmniFocus's review
counters — the "Review: 12" on a folder or a context, which is how OmniFocus
tells you a container has quietly drifted out of sight.

Both return the same shape so the panel can render either one with the same
component, and `mark` is the closing move: it says you looked, and resets the
counters.
"""

from __future__ import annotations

import datetime as _dt

from . import core, dates
from .db import Store, now

# Rough day counts are fine here: these drive a nag, not a calendar.
INTERVAL_DAYS = {
    "daily": 1, "weekly": 7, "monthly": 30,
    "quarterly": 91, "yearly": 365, "never": None,
}

STALE_DAYS = 7


def _ids(rows) -> list[str]:
    return [row["id"] for row in rows]


def _step(key: str, question: str, rows, target: int = 0, hint: str = "") -> dict:
    ids = _ids(rows)
    return {
        "key": key,
        "question": question,
        "count": len(ids),
        "target": target,
        "ok": len(ids) <= target,
        "hint": hint,
        "ids": ids[:50],
    }


def daily(store: Store) -> dict:
    """The short version, run as often as you like."""
    steps = [
        _step("inbox", "How many things are still unclarified?",
              store.query("SELECT * FROM inbox")),
        _step("today", "What is on for today?",
              store.query("SELECT * FROM today")),
        _step("overdue", "What slipped past its deadline?",
              store.query("SELECT * FROM overdue")),
        _step("waiting", "What are you waiting on?",
              store.query("SELECT * FROM waiting_for")),
    ]
    return {
        "kind": "daily",
        "date": now()[:10],
        "steps": steps,
        "next_action": store.scalar("SELECT title FROM focus_ten LIMIT 1"),
        "due": due_reviews(store),
        "stats": core.stats(store),
    }


def weekly(store: Store) -> dict:
    """The GTD Weekly Review, in the order Allen wrote it."""
    stale_cutoff = (_dt.datetime.now() - _dt.timedelta(days=STALE_DAYS)).strftime("%Y-%m-%d %H:%M")
    forecast_soon = store.query(
        "SELECT * FROM forecast WHERE substr(defer_at, 1, 10) <= ? "
        "ORDER BY defer_at LIMIT 200",
        ((_dt.date.today() + _dt.timedelta(days=7)).isoformat(),))

    steps = [
        _step("inbox", "Is your inbox empty?",
              store.query("SELECT * FROM inbox"),
              hint="clarify each item before organizing it"),
        _step("forecast", "What is on your calendar this week?",
              forecast_soon,
              hint="deferred items due in the next seven days"),
        _step("waiting", "Who are you waiting on?",
              store.query("SELECT * FROM waiting_for"),
              hint="chase, or let it drop"),
        _step("someday", "What is in someday/maybe?",
              store.query("SELECT * FROM someday"),
              hint="promote, date it, or delete it"),
        _step("stalled", "Which projects have stalled?",
              store.query("SELECT * FROM stalled_projects"),
              hint="projects with actions but nothing you can do"),
        _step("empty", "Which projects have no actions at all?",
              store.query("SELECT * FROM empty_projects"),
              hint="define the next action, or close them"),
        _step("flagged", "What did you flag as important?",
              store.query("SELECT * FROM flagged")),
        _step("overdue", "What is overdue?",
              store.query("SELECT * FROM overdue")),
        _step("stale", f"What has not moved in {STALE_DAYS} days?",
              store.query(
                  "SELECT * FROM item_titles t WHERE t.kind = 'action' "
                  "AND t.status IN ('active', 'waiting') AND t.updated_at < ?",
                  (stale_cutoff,)),
              hint="still real, or delete it"),
        _step("references", "What did you keep as reference?",
              store.query("SELECT * FROM item_titles WHERE status = 'reference'"), target=0,
              hint="reference material is fine, review it occasionally"),
    ]

    return {
        "kind": "weekly",
        "date": now()[:10],
        "steps": steps,
        "due": due_reviews(store),
        "stats": core.stats(store),
        "completed": [k for k in ("inbox", "stalled", "overdue", "stale")
                      if steps[[s["key"] for s in steps].index(k)]["ok"]],
    }


def due_reviews(store: Store) -> dict:
    """OmniFocus's per-folder and per-context review counters.

    A scope is due when its cadence has elapsed since it was last reviewed, and
    the count is how much has changed in it since — which is exactly the
    number OmniFocus puts next to "Review:".
    """
    scopes: list[dict] = []
    moment = _dt.datetime.now()

    containers = store.query(
        "SELECT id, title, kind, review, reviewed_at, completed FROM items "
        "WHERE kind IN ('project', 'area') AND review <> 'never' "
        "AND status NOT IN ('trashed', 'done', 'dropped') "
        "ORDER BY kind, title COLLATE NOCASE")
    for row in containers:
        scopes.append(_scope(store, row["id"], row["title"], row["review"],
                             row["reviewed_at"], "container", moment))

    for row in store.query(
            "SELECT id, name, review, reviewed_at FROM tags WHERE review <> 'never' "
            "ORDER BY name COLLATE NOCASE"):
        scopes.append(_scope(store, row["id"], row["name"], row["review"],
                             row["reviewed_at"], "context", moment))

    return {
        "scopes": scopes,
        "due": sum(1 for s in scopes if s["due"]),
        "unreviewed": sum(s["unreviewed"] for s in scopes if s["due"]),
    }


def _scope(store: Store, scope_id: str, title: str, cadence: str, reviewed_at,
           kind: str, moment: _dt.datetime) -> dict:
    if kind == "context":
        rows = store.query(
            "SELECT i.* FROM item_tags it JOIN items i ON i.id = it.item_id "
            "WHERE it.tag_id = ? AND i.status NOT IN ('trashed', 'done', 'dropped')",
            (scope_id,))
    else:
        rows = store.query(
            "SELECT * FROM item_titles WHERE (parent_id = ? OR id = ?) "
            "AND status NOT IN ('trashed', 'done', 'dropped')",
            (scope_id, scope_id))

    stamp = dates.parse_stamp(reviewed_at)
    unreviewed = [r["id"] for r in rows
                  if stamp is None
                  or dates.parse_stamp(r["updated_at"]) > stamp]

    due_in = None
    if stamp is not None:
        due_in = int((moment - stamp).days - (INTERVAL_DAYS.get(cadence) or 0))
    return {
        "id": scope_id,
        "title": title,
        "kind": kind,
        "cadence": cadence,
        "reviewed_at": reviewed_at,
        "unreviewed": len(unreviewed),
        "open": len(rows),
        "due": due_in is not None and due_in >= 0,
        "due_in_days": due_in,
        "ids": unreviewed[:50],
    }


def mark(store: Store, kind: str = "weekly", *, scope: str | None = None) -> dict:
    """Close a review session.

    Records when it happened, and — unless you name one scope — resets every
    review counter, which is what makes "Review: 0" mean something.
    """
    moment = now()
    key = f"review:last_{kind}"
    store.set_meta(key, moment)
    store.set_meta(f"review:count_{kind}",
                   str(int(store.meta(f"review:count_{kind}", "0") or 0) + 1))

    if scope:
        result = core.mark_reviewed(store, scope)
    else:
        result = core.mark_reviewed(store, all_scopes=True)
    store.log(None, f"{kind}-review", f"{result['scope']}: {result['marked']} items")
    return {"kind": kind, "at": moment, **result}


def history(store: Store) -> dict:
    return {
        "last_daily": store.meta("review:last_daily"),
        "last_weekly": store.meta("review:last_weekly"),
        "dailies": int(store.meta("review:count_daily", "0") or 0),
        "weeklies": int(store.meta("review:count_weekly", "0") or 0),
        "since_daily": days_since(store.meta("review:last_daily")),
        "since_weekly": days_since(store.meta("review:last_weekly")),
    }


def days_since(stamp: str | None) -> int | None:
    moment = dates.parse_stamp(stamp)
    if moment is None:
        return None
    return (dates.parse_stamp(now()) - moment).days
