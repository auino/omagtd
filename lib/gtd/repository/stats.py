"""The bar's and the panel's counters, in one query instead of twenty.

Every number :func:`gtd.core.stats` returns used to come from its own
``COUNT(*)`` over a view, and `bar_snapshot` polls that set every minute. The
views are not materialized, so each count walked the table again. One
aggregate over ``items`` (with the same parent join `next_actions` uses)
answers all of them at once; only `stalled`, which needs a correlated
``NOT EXISTS``, stays a query of its own.

The conditions are copied from `schema.sql` on purpose: the views stay the
documented, hand-runnable definition, and this is the fast path that must
agree with them.
"""

from __future__ import annotations

import datetime as _dt

from ..db import STAMP, Store, now

_ITEMS_SQL = """
SELECT
  SUM(i.status = 'inbox') AS inbox,
  SUM(i.status = 'inbox' AND i.kind = 'action') AS clarify,
  SUM(i.kind = 'action' AND i.status = 'active'
      AND (i.parent_id IS NULL OR p.id IS NULL OR p.status = 'active')
      AND (i.defer_at IS NULL
           OR i.defer_at <= substr(datetime('now', 'localtime'), 1, 16))) AS next,
  SUM(i.kind = 'action' AND i.status = 'active'
      AND (i.due_at IS NOT NULL OR i.priority > 0
           OR (i.defer_at IS NOT NULL
               AND i.defer_at <= date('now', 'localtime') || ' 23:59'))) AS today,
  SUM(i.kind = 'action' AND i.status NOT IN ('done', 'dropped', 'trashed')
      AND i.due_at IS NOT NULL
      AND substr(i.due_at, 1, 10) < date('now', 'localtime')) AS overdue,
  SUM(i.kind = 'action' AND i.status NOT IN ('done', 'dropped', 'trashed')
      AND i.due_at IS NOT NULL
      AND substr(i.due_at, 1, 10) = date('now', 'localtime')) AS due_today,
  SUM(i.kind = 'action' AND i.status NOT IN ('done', 'dropped', 'trashed')
      AND i.due_at IS NOT NULL
      AND substr(i.due_at, 1, 10) = date('now', 'localtime')
      AND i.priority > 0) AS flagged_today,
  SUM(i.kind = 'action' AND i.status NOT IN ('done', 'dropped', 'trashed')
      AND i.due_at IS NOT NULL
      AND substr(i.due_at, 1, 10) = date('now', 'localtime')
      AND substr(i.due_at, 1, 16) < ?) AS due_today_late,
  SUM(i.kind = 'action' AND i.status NOT IN ('done', 'dropped', 'trashed')
      AND i.due_at IS NOT NULL
      AND substr(i.due_at, 1, 10) > date('now', 'localtime')) AS upcoming,
  SUM(i.status = 'waiting') AS waiting,
  SUM(i.status = 'someday') AS someday,
  SUM(i.kind = 'action' AND i.priority > 0
      AND i.status NOT IN ('done', 'dropped', 'trashed')) AS flagged,
  SUM(i.status = 'reference') AS reference,
  SUM(i.kind = 'project' AND i.status = 'active') AS projects,
  SUM(i.kind = 'project' AND i.status = 'active' AND i.completed = 0) AS projects_open,
  SUM(i.kind = 'area' AND i.status = 'active') AS areas,
  SUM(i.status NOT IN ('done', 'dropped', 'trashed')) AS open_items,
  SUM(i.status = 'done'
      AND substr(i.completed_at, 1, 10) = date('now', 'localtime')) AS done_today,
  SUM(i.kind = 'action' AND i.created_at >= ?) AS created_30,
  SUM(i.kind = 'action' AND i.status IN ('done', 'dropped')
      AND COALESCE(i.completed_at, i.dropped_at) >= ?) AS closed_30
FROM items i
LEFT JOIN items p ON p.id = i.parent_id AND p.kind = 'project'
"""

_STALLED_SQL = """
SELECT COUNT(*) FROM items p
WHERE p.kind = 'project' AND p.status = 'active'
  AND NOT EXISTS (SELECT 1 FROM items a
                  WHERE a.parent_id = p.id AND a.kind = 'action'
                    AND a.status = 'active')
"""


def counts(store: Store) -> dict:
    """Every number the bar pill and the panel header are made of."""
    month_ago = (_dt.datetime.now() - _dt.timedelta(days=30)).strftime(STAMP)
    row = store.one(_ITEMS_SQL, (now(), month_ago, month_ago))
    data = {key: int(row[key] or 0) for key in row.keys()}
    # `focus_ten` is the first ten of next_actions, so its count is capped.
    data["ten"] = min(10, data["next"])
    data["stalled"] = int(store.scalar(_STALLED_SQL) or 0)
    return data
