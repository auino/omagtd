-- GTD for Omarchy — OmniFocus-shaped task database.
--
-- The whole system is one sqlite3 file (default
-- ~/.local/share/omagtd/gtd.sqlite3). This file is the only place the
-- schema lives; lib/gtd/db.py applies it and stamps `meta.schema`.
--
-- Timestamps are local-time strings in the fixed-width form
-- 'YYYY-MM-DD HH:MM', which sorts and compares correctly as plain text and
-- still parses with strftime()/datetime() in SQL.

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- Containers and actions share one table, the way OmniFocus does:
--   kind = 'area'    an Area of Focus / standard of responsibility
--   kind = 'project' an outcome with a completion criterion
--   kind = 'action'  the next physical, visible action
--
-- Containment is a single parent pointer: a project belongs to an area (or
-- to nothing, an orphan project), an action belongs to a project or an area.
-- The inbox is not a row — it is `status = 'inbox'`, which is what makes
-- "what have I not clarified yet" a one-line query.
CREATE TABLE IF NOT EXISTS items (
  id           TEXT PRIMARY KEY,
  kind         TEXT    NOT NULL CHECK (kind IN ('action', 'project', 'area')),
  title        TEXT    NOT NULL,
  notes        TEXT    NOT NULL DEFAULT '',

  -- One link the item points at: a URL or a file path. Attachments (below)
  -- cover the "many files" case; this is the single "open this" reference.
  url          TEXT,

  -- Container this item lives in (project -> area, action -> project/area).
  parent_id    TEXT    REFERENCES items(id) ON DELETE CASCADE,

  -- inbox     captured, not yet clarified
  -- active    organized and available
  -- onhold    a container paused on purpose
  -- waiting   delegated to someone, tracked in waiting_for
  -- someday   incubated deliberately
  -- reference worth keeping, not worth doing (OmniFocus's Reference folder)
  -- done      completed (action) or completed (project/area)
  -- dropped   consciously abandoned
  -- trashed   deleted; prev_status remembers where to restore it
  status       TEXT    NOT NULL CHECK (status IN
                 ('inbox', 'active', 'onhold', 'waiting', 'someday',
                  'reference', 'done', 'dropped', 'trashed')),
  prev_status  TEXT,

  -- 0 none, 1 flagged. OmniFocus had three levels; only the boolean ever
  -- changed a day, so nothing writes above 1 any more. The CHECK still
  -- spans 0..3 because the fold on open is what lowers old rows.
  priority     INTEGER NOT NULL DEFAULT 0 CHECK (priority BETWEEN 0 AND 3),

  -- Start/defer and deadline. OmniFocus calls these start and due.
  defer_at     TEXT,
  due_at       TEXT,

  -- Estimated effort, in whole minutes (OmniFocus's "estimated duration").
  -- NULL means unestimated, which is not the same as zero.
  duration     INTEGER,

  started_at   TEXT,
  completed_at TEXT,
  dropped_at   TEXT,

  -- When the deadline notification was last shown, so it fires once per
  -- deadline instead of every poll.
  notified_at  TEXT,

  -- Delegation: who it is waiting on.
  waiting_for  TEXT,

  -- Recurrence rule, JSON: {"interval": 2, "unit": "week", "from": "complete"}.
  -- `from` is "complete" (repeat N units after you finish) or "schedule"
  -- (repeat on the grid regardless of when you finish).
  repeat       TEXT,

  -- OmniFocus review cadence. Items are "unreviewed" until `reviewed_at`
  -- moves past the end of the last completed review of that container.
  review       TEXT    NOT NULL DEFAULT 'never'
                 CHECK (review IN ('daily', 'weekly', 'monthly', 'quarterly',
                                   'yearly', 'never')),
  reviewed_at  TEXT,

  -- Manual ordering inside a container.
  rank         INTEGER NOT NULL DEFAULT 0,

  created_at   TEXT    NOT NULL,
  updated_at   TEXT    NOT NULL,

  -- A container whose last action is done has completed; GTD calls that a
  -- completed standard, OmniFocus a completed folder. Cached so `projects`
  -- never needs a correlated subquery per row.
  completed    INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_items_parent   ON items(parent_id);
CREATE INDEX IF NOT EXISTS idx_items_status   ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_kind     ON items(kind);
CREATE INDEX IF NOT EXISTS idx_items_kind_st  ON items(kind, status);
CREATE INDEX IF NOT EXISTS idx_items_defer    ON items(defer_at);
CREATE INDEX IF NOT EXISTS idx_items_due      ON items(due_at);
CREATE INDEX IF NOT EXISTS idx_items_priority ON items(priority, status);
CREATE INDEX IF NOT EXISTS idx_items_review   ON items(review, reviewed_at);

-- Contexts. OmniFocus calls these tags and GTD calls them contexts; they are
-- the same thing — where an action can be done (@errands, @computer, @home).
-- Contexts (tags). A context can nest: the name stores the full path
-- ("home>kitchen"), parent_id points at the immediate parent, so filtering by
-- "home" can include everything beneath it.
CREATE TABLE IF NOT EXISTS tags (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
  parent_id   TEXT REFERENCES tags(id) ON DELETE CASCADE,
  review      TEXT NOT NULL DEFAULT 'never'
                CHECK (review IN ('daily', 'weekly', 'monthly', 'quarterly',
                                  'yearly', 'never')),
  reviewed_at TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tags_parent ON tags(parent_id);

CREATE TABLE IF NOT EXISTS item_tags (
  item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  tag_id  TEXT NOT NULL REFERENCES tags(id)  ON DELETE CASCADE,
  PRIMARY KEY (item_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_item_tags_tag  ON item_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_item_tags_item ON item_tags(item_id);

-- Attachments. An item can point at any number of files or links; the row
-- stores the path (or URL) verbatim, plus an optional friendly name, so the
-- database never has to copy the file itself.
CREATE TABLE IF NOT EXISTS attachments (
  id         TEXT PRIMARY KEY,
  item_id    TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  name       TEXT NOT NULL DEFAULT '',
  uri        TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attachments_item ON attachments(item_id);

-- Append-only history. Powers `gtd log`, the panel's review dashboard, and
-- the "untouched for N days" signal the weekly review depends on.
CREATE TABLE IF NOT EXISTS log (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  at      TEXT NOT NULL,
  item_id TEXT REFERENCES items(id) ON DELETE SET NULL,
  kind    TEXT NOT NULL,
  detail  TEXT
);

CREATE INDEX IF NOT EXISTS idx_log_at   ON log(at);
CREATE INDEX IF NOT EXISTS idx_log_item ON log(item_id);

-- ---------------------------------------------------------------- views
--
-- Views exist so the database is useful on its own: `sqlite3 gtd.sqlite3`
-- and "select * from next_actions" answer the questions the GTD review asks.

-- Everything that is not gone. The starting set for every workflow step.
CREATE VIEW IF NOT EXISTS open_items AS
SELECT * FROM items
WHERE status NOT IN ('done', 'dropped', 'trashed');

-- GTD step 1 — the inbox is "mind is clear" or it is not.
CREATE VIEW IF NOT EXISTS inbox AS
SELECT * FROM items
WHERE status = 'inbox'
ORDER BY created_at;

-- GTD step 2 — the clarify queue, oldest first: unclarified captures that
-- have not been touched yet. Anything that is `reference` has already been
-- decided to be non-actionable, so it is out of this view.
CREATE VIEW IF NOT EXISTS clarify_queue AS
SELECT * FROM items
WHERE status = 'inbox'
  AND kind = 'action'
ORDER BY created_at;

-- GTD step 5 — next actions: organized, not waiting, not incubated, not
-- inside a completed or dropped project, and not deferred into the future.
-- `now` is truncated to the minute so it compares like-for-like with the
-- stored 'YYYY-MM-DD HH:MM' strings.
CREATE VIEW IF NOT EXISTS next_actions AS
SELECT i.* FROM items i
LEFT JOIN items p ON p.id = i.parent_id AND p.kind = 'project'
WHERE i.kind = 'action'
  AND i.status = 'active'
  AND (i.parent_id IS NULL OR p.id IS NULL OR p.status = 'active')
  AND (i.defer_at IS NULL OR i.defer_at <= substr(datetime('now', 'localtime'), 1, 16))
ORDER BY i.priority DESC, COALESCE(i.due_at, '9999') ASC, i.rank ASC, i.created_at ASC;

-- The ten-minute slice: the first ten next actions, nothing else.
CREATE VIEW IF NOT EXISTS focus_ten AS
SELECT * FROM next_actions LIMIT 10;

-- GTD step 3 — the daily planning list: what is due, and what is flagged.
CREATE VIEW IF NOT EXISTS today AS
SELECT * FROM open_items
WHERE kind = 'action'
  AND status = 'active'
  AND (
        due_at IS NOT NULL
     OR priority > 0
     OR (defer_at IS NOT NULL AND defer_at <= substr(date('now', 'localtime'), 1, 10) || ' 23:59')
  )
ORDER BY priority DESC, COALESCE(due_at, '9999') ASC, created_at ASC;

-- Past the deadline and not finished. A hard GTD signal. "Past" is a due date
-- before today: work left over from an earlier day. Something due later today
-- whose time has slipped is still today's, and shows red in the Today list.
CREATE VIEW IF NOT EXISTS overdue AS
SELECT * FROM open_items
WHERE kind = 'action'
  AND due_at IS NOT NULL
  AND substr(due_at, 1, 10) < date('now', 'localtime')
ORDER BY due_at;

-- Due today, by calendar day whatever the time: the panel's Today list.
CREATE VIEW IF NOT EXISTS due_today AS
SELECT * FROM open_items
WHERE kind = 'action'
  AND due_at IS NOT NULL
  AND substr(due_at, 1, 10) = date('now', 'localtime')
ORDER BY due_at, priority DESC;

-- Due after today: the forward half of the deadline line.
CREATE VIEW IF NOT EXISTS upcoming AS
SELECT * FROM open_items
WHERE kind = 'action'
  AND due_at IS NOT NULL
  AND substr(due_at, 1, 10) > date('now', 'localtime')
ORDER BY due_at, priority DESC;

-- GTD step 4 — OmniFocus's forecast: everything deferred to a future date,
-- so the calendar of commitments is one query.
CREATE VIEW IF NOT EXISTS forecast AS
SELECT * FROM open_items
WHERE kind = 'action'
  AND defer_at IS NOT NULL
  AND defer_at >= substr(date('now', 'localtime'), 1, 10) || ' 00:00'
ORDER BY defer_at, priority DESC;

CREATE VIEW IF NOT EXISTS waiting_for AS
SELECT * FROM open_items
WHERE status = 'waiting'
ORDER BY COALESCE(NULLIF(waiting_for, ''), '~') ASC, defer_at;

CREATE VIEW IF NOT EXISTS someday AS
SELECT * FROM open_items
WHERE status = 'someday'
ORDER BY priority DESC, created_at;

CREATE VIEW IF NOT EXISTS flagged AS
SELECT * FROM open_items
WHERE kind = 'action' AND priority > 0
ORDER BY priority DESC, COALESCE(due_at, '9999') ASC, created_at;

CREATE VIEW IF NOT EXISTS projects AS
SELECT * FROM open_items WHERE kind = 'project';

CREATE VIEW IF NOT EXISTS areas AS
SELECT * FROM open_items WHERE kind = 'area';

CREATE VIEW IF NOT EXISTS completed_items AS
SELECT * FROM items WHERE status = 'done' ORDER BY completed_at;

-- Containers with actions in them but nothing you could do: the classic
-- "stalled project" the weekly review hunts for.
CREATE VIEW IF NOT EXISTS stalled_projects AS
SELECT p.* FROM items p
WHERE p.kind = 'project'
  AND p.status = 'active'
  AND NOT EXISTS (
        SELECT 1 FROM items a
        WHERE a.parent_id = p.id AND a.kind = 'action' AND a.status = 'active'
      );

-- Envelopes with nothing in them at all.
CREATE VIEW IF NOT EXISTS empty_projects AS
SELECT p.* FROM items p
WHERE p.kind = 'project'
  AND p.status = 'active'
  AND NOT EXISTS (
        SELECT 1 FROM items a
        WHERE a.parent_id = p.id AND a.kind = 'action' AND a.status <> 'trashed'
      );

-- Everything in a container, plus the container's name, for list output.
CREATE VIEW IF NOT EXISTS item_titles AS
SELECT i.*,
       parent.title AS parent_title,
       parent.kind AS parent_kind,
       grandparent.title AS area_title
FROM items i
LEFT JOIN items parent   ON parent.id = i.parent_id
LEFT JOIN items grandparent ON grandparent.id = parent.parent_id;

-- Contexts with their open-action counts, for the tag view.
CREATE VIEW IF NOT EXISTS tag_load AS
SELECT t.id, t.name, t.parent_id, t.review, t.reviewed_at,
       (SELECT COUNT(*) FROM item_tags it
         JOIN items i ON i.id = it.item_id
        WHERE it.tag_id = t.id AND i.kind = 'action'
          AND i.status NOT IN ('done', 'dropped', 'trashed')
       ) AS open_count
FROM tags t;
