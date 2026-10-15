"""Storage layer: the connection, the schema, ids, timestamps, the log.

Everything the plugin writes lands in one sqlite3 file. WAL is enabled so the
Omarchy shell's bar poll never blocks a capture typed in a terminal.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
import re
import secrets
import sqlite3
import shutil
from pathlib import Path

SCHEMA_VERSION = 15
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"

# The items table as it is after the archive migration. sqlite cannot widen a
# CHECK constraint in place, so `_migrate_v14` rebuilds the table from this
# definition; it is kept here, next to the migration, because it pins the shape
# that migration produced rather than the shape schema.sql has today.
_ITEMS_REBUILD_DDL = """
CREATE TABLE items_new (
  id           TEXT PRIMARY KEY,
  kind         TEXT    NOT NULL CHECK (kind IN ('action', 'project', 'area')),
  title        TEXT    NOT NULL,
  notes        TEXT    NOT NULL DEFAULT '',
  url          TEXT,
  parent_id    TEXT    REFERENCES items(id) ON DELETE CASCADE,
  status       TEXT    NOT NULL CHECK (status IN
                 ('inbox', 'active', 'onhold', 'waiting', 'someday',
                  'reference', 'done', 'dropped', 'archived', 'trashed')),
  prev_status  TEXT,
  priority     INTEGER NOT NULL DEFAULT 0 CHECK (priority BETWEEN 0 AND 3),
  defer_at     TEXT,
  due_at       TEXT,
  duration     INTEGER,
  started_at   TEXT,
  completed_at TEXT,
  dropped_at   TEXT,
  archived_at  TEXT,
  notified_at  TEXT,
  waiting_for  TEXT,
  repeat       TEXT,
  review       TEXT    NOT NULL DEFAULT 'never'
                 CHECK (review IN ('daily', 'weekly', 'monthly', 'quarterly',
                                   'yearly', 'never')),
  reviewed_at  TEXT,
  rank         INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT    NOT NULL,
  updated_at   TEXT    NOT NULL,
  completed    INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1))
)
"""

# Crockford-ish alphabet: no i, l, o, u, so an id can be read aloud on the
# phone without "was that an l or a one".
_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"

# 'YYYY-MM-DD HH:MM' — sorts as text, so every ORDER BY on a date column is
# also a chronological sort and comparison needs no function calls.
STAMP = "%Y-%m-%d %H:%M"


def now() -> str:
    """Local timestamp, minute precision."""
    return _dt.datetime.now().strftime(STAMP)


def today() -> str:
    """Local date, 'YYYY-MM-DD'."""
    return _dt.date.today().isoformat()


def new_id(length: int = 8) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def data_home() -> Path:
    """Where the database and its profiles live."""
    override = os.environ.get("GTD_HOME")
    base = Path(override) if override else Path.home() / ".local" / "share" / "omagtd"
    base.mkdir(parents=True, exist_ok=True)
    # The database used to be `gtd.sqlite3`; it is now `omagtd.sqlite3`. Carry
    # the old one forward so an existing store is not orphaned, but only when the
    # new name has not been created on its own.
    old = base / "gtd.sqlite3"
    if old.exists() and not (base / "omagtd.sqlite3").exists():
        old.rename(base / "omagtd.sqlite3")
        for suffix in ("-wal", "-shm", "-journal"):
            side = Path(str(old) + suffix)
            if side.exists():
                side.rename(base / ("omagtd.sqlite3" + suffix))
    return base


def db_path(profile: str | None = None, explicit: str | None = None) -> Path:
    """Resolve the database path, most specific source first.

    --db beats $GTD_DB beats --profile beats the default database.
    """
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("GTD_DB")
    if env:
        return Path(env).expanduser()
    if profile:
        return data_home() / "profiles" / f"{profile}.sqlite3"
    return data_home() / "omagtd.sqlite3"


class Store:
    """A thin, explicit wrapper around one sqlite3 connection.

    Not a context manager by design: long-lived callers (the CLI) want the
    handle, and the schema is applied on first use so every command works
    against an empty directory without a separate `init`.
    """

    def __init__(self, path: str | Path | None = None, *, profile: str | None = None,
                 readonly: bool = False):
        self.path = db_path(profile, str(path) if path else None)
        self.readonly = readonly
        self._conn: sqlite3.Connection | None = None
        self._gtd_undo: dict | None = None
        self.ensure()

    # ---------------------------------------------------------------- conn

    @property
    def db(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            if not self.readonly:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = NORMAL")
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            if not self.readonly:
                # Give sqlite a chance to refresh its query-planner stats after
                # whatever this session changed, on the way out.
                try:
                    self._conn.execute("PRAGMA optimize")
                except sqlite3.Error:
                    pass
            self._conn.close()
            self._conn = None

    # -------------------------------------------------------------- schema

    def ensure(self) -> None:
        """Create the schema if needed and stamp the version.

        Applying the schema on every open is what lets a fresh install work
        with no setup step; every statement in schema.sql is IF NOT EXISTS, so
        this is a no-op once the file exists. The folds in `_migrate` are
        deliberate too — they run every open so a database that recorded a
        current version before a fold landed is still repaired.
        """
        if not self.readonly:
            self._migrate()
            self.db.executescript(SCHEMA_PATH.read_text())
            self.db.execute(
                "INSERT INTO meta(key, value) VALUES('schema', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def _migrate(self) -> None:
        """Bring a database created by an older schema forward.

        schema.sql is all IF NOT EXISTS, which creates what is missing but
        cannot add a column to a table that already exists or redefine a view.
        Both need doing explicitly, and only when they are missing, so this
        stays a no-op on every normal open.
        """
        self._finish_interrupted_items_rebuild()
        try:
            columns = {row["name"] for row in self.query("PRAGMA table_info(items)")}
        except sqlite3.OperationalError:
            return  # no items table yet — executescript will build it
        if not columns:
            return  # fresh database: schema.sql creates the current shape
        added = False
        if "duration" not in columns:
            self.db.execute("ALTER TABLE items ADD COLUMN duration INTEGER")
            added = True
        if "notified_at" not in columns:
            self.db.execute("ALTER TABLE items ADD COLUMN notified_at TEXT")
            added = True
        if "url" not in columns:
            self.db.execute("ALTER TABLE items ADD COLUMN url TEXT")
            added = True
        # item_titles is `SELECT i.*`, which sqlite freezes at creation, so it
        # has to be dropped for executescript to rebuild it around any new
        # columns, and it gained `parent_kind` for nested projects.
        try:
            view_columns = {row["name"] for row in
                            self.query("PRAGMA table_info(item_titles)")}
        except sqlite3.OperationalError:
            view_columns = set()
        if added or (view_columns and "parent_kind" not in view_columns):
            self.db.execute("DROP VIEW IF EXISTS item_titles")
        try:
            version = int(self.meta("schema") or 0)
        except (sqlite3.OperationalError, ValueError):
            version = 0
        if version < 6:
            self._migrate_v6()
        if version < 7:
            self._migrate_v7()
        if version < 8:
            self._migrate_v8()
        if version < 10:
            self._migrate_v10()
        if version < 11:
            self._migrate_v11()
        if version < 12:
            self._migrate_v12()
        # v14 and v15 rebuild or alter the table and are self-guarding (each
        # early-returns once its change is in place), so they run every open
        # rather than only when the stored version lags behind them. A database
        # can sit at the current version with the change still undone — the
        # meta is written at the tail of every open, so a build that recorded
        # the version before the step landed would otherwise skip it forever.
        self._migrate_v14()
        self._migrate_v15()
        self._fold_context_case()
        self._fold_priority()
        self._fold_attachment_paths()
        self._drop_stale_deadline_views()
        self._drop_stale_archived_views()

    def _fold_priority(self) -> None:
        """Lower every priority above 1 to the one flag there is now.

        Flags used to come in three levels and the column still accepts them,
        so a database written before the collapse holds rows at 2 or 3. The
        flag is on or off today, and a row still sitting at 3 would sort above
        a row at 1 for a difference nothing ranks by any more.

        updated_at is deliberately untouched: it is the stamp the trash
        grace period and the recency reads are cut from, and lowering an old
        flag is not a reason to restart either of them. Like the context fold
        this runs on every open, so a database that recorded a current schema
        version before this landed is repaired too.
        """
        self.db.execute("UPDATE items SET priority = 1 WHERE priority > 1")

    def _fold_context_case(self) -> None:
        """Put every context in lowercase.

        Contexts get typed in a hurry, so "Errands" and "errands" both arrived
        and the context list filled with near duplicates that only a case-
        insensitive lookup could tell apart. Contexts are now written in
        lowercase, so the names already on disk are folded too. tags.name is
        UNIQUE COLLATE NOCASE, which means the database was already refusing to
        hold two spellings of one context, so folding case cannot collide and
        no merging is needed.

        The row filter is GLOB rather than `name <> lower(name)` on purpose:
        the column is COLLATE NOCASE, so an ordinary comparison there decides
        that "Errands" equals "errands" and matches nothing. GLOB is case
        sensitive, and so is sqlite's lower(), which is ASCII-only — the two
        cover exactly the same set of names.

        Unlike the numbered migrations this one runs on every open instead of
        only when the recorded version is behind. It matches no rows once the
        names are folded, and running it unconditionally is what repairs a
        database that already recorded a current schema version back when the
        filter was still case-blind.
        """
        self.db.execute(
            "UPDATE tags SET name = lower(name), updated_at = ? "
            "WHERE name GLOB '*[A-Z]*'", (now(),))

    def _fold_attachment_paths(self) -> None:
        """Record managed attachment files by the path relative to this store.

        A file copied into the attachments folder used to be written down as
        the absolute path it was copied to, which is the one thing that cannot
        survive the database moving — or being restored from an export on
        another machine. A row that still names a file inside this store's own
        folder is rewritten to the path relative to it; anything pointing
        elsewhere is left alone, because that file is not ours to relocate.

        idempotent by construction: a row already relative matches no
        absolute path, so the second open changes nothing.
        """
        root = Path(self.path).resolve().parent
        managed = root / "attachments"
        for row in self.query("SELECT id, uri FROM attachments"):
            uri = str(row["uri"] or "")
            raw = uri[len("file://"):] if uri.startswith("file://") else uri
            if not raw or not os.path.isabs(raw):
                continue
            try:
                path = Path(os.path.expanduser(raw)).resolve()
                if path.parent != managed:
                    continue
                relative = str(path.relative_to(root))
            except (OSError, ValueError):
                continue
            if relative != uri:
                self.db.execute("UPDATE attachments SET uri = ? WHERE id = ?",
                                (relative, row["id"]))

    def _migrate_v7(self) -> None:
        """Teach contexts to nest.

        tags gains a self-referencing parent_id so a context path like
        "home>kitchen" is stored as an explicit chain. Existing names that
        already carry a ">" are relinked to a parent row, and the tag_load view
        is dropped so executescript can rebuild it around the new column.
        """
        try:
            columns = {row["name"] for row in self.query("PRAGMA table_info(tags)")}
        except sqlite3.OperationalError:
            return
        if not columns:
            return
        if "parent_id" not in columns:
            self.db.execute(
                "ALTER TABLE tags ADD COLUMN parent_id TEXT "
                "REFERENCES tags(id) ON DELETE CASCADE")
        try:
            view_columns = {row["name"] for row in
                            self.query("PRAGMA table_info(tag_load)")}
        except sqlite3.OperationalError:
            view_columns = set()
        if view_columns and "parent_id" not in view_columns:
            self.db.execute("DROP VIEW IF EXISTS tag_load")
        for row in self.query("SELECT id, name, parent_id FROM tags"):
            name = row["name"]
            if ">" not in name:
                continue
            parent_name = name.rsplit(">", 1)[0]
            parent = self.one("SELECT id FROM tags WHERE name = ? COLLATE NOCASE",
                              (parent_name,))
            if parent is None:
                parent_id = new_id()
                stamp = now()
                self.db.execute(
                    "INSERT INTO tags(id, name, created_at, updated_at) "
                    "VALUES(?, ?, ?, ?)", (parent_id, parent_name, stamp, stamp))
            else:
                parent_id = parent["id"]
            if row["parent_id"] != parent_id:
                self.db.execute("UPDATE tags SET parent_id = ? WHERE id = ?",
                                (parent_id, row["id"]))

    def _migrate_v8(self) -> None:
        """Take locations away.

        Places were a first-class thing with their own table, view and
        `@place` syntax, and it turned out to be a context wearing a different
        name — everything it did, a tag already did, and better (nesting, open
        counts, review cadence). So the feature is gone rather than hidden.

        sqlite can drop the column outright, but only once nothing else in the
        schema still names it, and three things do: the index on it, the
        item_titles view (which is `SELECT i.*`, so it froze the column into
        its own list) and the location_load view. They go first, in that
        order, and item_titles is dropped again below so executescript
        rebuilds it without the joined location name.
        """
        self.db.execute("DROP INDEX IF EXISTS idx_items_location")
        self.db.execute("DROP VIEW IF EXISTS location_load")
        columns = {row["name"] for row in self.query("PRAGMA table_info(items)")}
        if "location_id" in columns:
            self.db.execute("DROP VIEW IF EXISTS item_titles")
            self.db.execute("ALTER TABLE items DROP COLUMN location_id")
        self.db.execute("DROP VIEW IF EXISTS item_titles")
        self.db.execute("DROP TABLE IF EXISTS locations")

    def _migrate_v10(self) -> None:
        """Count task associations, not tagged projects or areas, in tag_load."""
        self.db.execute("DROP VIEW IF EXISTS tag_load")

    def _migrate_v11(self) -> None:
        """Move managed attachments under a directory named for their item."""
        root = self.path.resolve().parent
        managed = (root / "attachments").resolve()
        old_paths: set[str] = set()
        rows = self.query("SELECT id, item_id, uri FROM attachments")
        for row in rows:
            uri = str(row["uri"] or "")
            if (not uri or (not uri.lower().startswith("file://")
                            and re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", uri))):
                continue
            raw = uri[len("file://"):] if uri.startswith("file://") else uri
            source = Path(os.path.expanduser(raw))
            if not source.is_absolute():
                source = root / source
            try:
                source = source.resolve()
                relative = source.relative_to(managed)
            except (OSError, ValueError):
                continue
            task_dir_name = re.sub(r"[^A-Za-z0-9_-]+", "-", str(row["item_id"])).strip("-") or "task"
            task_dir = managed / task_dir_name
            if relative.parts and relative.parts[0] == task_dir_name:
                continue
            if not source.is_file():
                continue
            task_dir.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha1()
            with source.open("rb") as handle:
                for chunk in iter(lambda: handle.read(65536), b""):
                    digest.update(chunk)
            destination = task_dir / f"{digest.hexdigest()[:12]}-{source.name}"
            if not destination.exists():
                shutil.copy2(source, destination)
            stored = str(destination.relative_to(root))
            self.db.execute("UPDATE attachments SET uri = ? WHERE id = ?",
                            (stored, row["id"]))
            old_paths.add(str(source))
        for old in old_paths:
            if not self.scalar("SELECT COUNT(*) FROM attachments WHERE uri = ?",
                               (str(Path(old).relative_to(root)),)):
                try:
                    Path(old).unlink()
                except OSError:
                    pass

    def _migrate_v12(self) -> None:
        """Replace the single-column context index with a covering one.

        Counting open actions per context and filtering by context both join
        item_tags back to items, and idx_item_tags_tag(tag_id) left sqlite to
        fetch each row for its item_id. The covering (tag_id, item_id) index
        answers those from the index alone; the old one is now a redundant
        prefix of it and is dropped.
        """
        self.db.execute("DROP INDEX IF EXISTS idx_item_tags_tag")

    def _finish_interrupted_items_rebuild(self) -> None:
        """Finish—or discard—a half-done items rebuild left by _migrate_v14.

        The rebuild copies every row into `items_new`, drops `items` and renames
        the copy into place. sqlite cannot roll that back as one unit, so a crash
        or an error between the drop and the rename can strand the data in
        `items_new` while an empty `items` is rebuilt from schema.sql on the
        next open. The early-exit in _migrate_v14 then trusts that empty table —
        which already carries the new shape—and never looks at the copy, so the
        store reads as empty forever. This runs on every open and puts the rows
        back where they belong.
        """
        tables = {row["name"] for row in self.query(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "items_new" not in tables:
            return
        has_items = "items" in tables
        items_rows = self.scalar("SELECT COUNT(*) FROM items") if has_items else 0
        new_rows = self.scalar("SELECT COUNT(*) FROM items_new")
        # A leftover copy beside a populated items table is just debris the
        # failed rebuild forgot; the rows already in place are the truth. Only
        # an empty (or missing) items table with rows waiting in the copy is an
        # interrupted rebuild worth finishing.
        if items_rows or not new_rows:
            self.db.execute("DROP TABLE IF EXISTS items_new")
            return
        for row in self.query(
                "SELECT name FROM sqlite_master WHERE type = 'view'"):
            self.db.execute(f'DROP VIEW IF EXISTS "{row["name"]}"')
        self.db.execute("PRAGMA foreign_keys = OFF")
        try:
            if has_items:
                self.db.execute("DROP TABLE items")
            self.db.execute("ALTER TABLE items_new RENAME TO items")
        finally:
            self.db.execute("PRAGMA foreign_keys = ON")

    def _migrate_v14(self) -> None:
        """Widen the items status CHECK to accept 'archived', add archived_at.

        sqlite cannot redefine a CHECK constraint in place, so the table is
        rebuilt: foreign keys off, a new table carrying the widened constraint,
        every row copied across, then the old table dropped and the new one
        renamed into its place. executescript rebuilds the indexes and views
        around the fresh table on the same open.

        `archived_at` is new, so it is left at its default NULL for every
        existing row; the archive sweep falls back to `updated_at` for those.
        """
        body = self.scalar(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'items'")
        columns = {row["name"] for row in self.query("PRAGMA table_info(items)")}
        if not body or ("'archived'" in body and "archived_at" in columns):
            return
        # Every view that reads items would be a dangling reference the moment
        # the table is dropped, and sqlite refuses to rename into a name a view
        # still points at, so they all go first. executescript rebuilds them
        # from schema.sql on the same open.
        for row in self.query(
                "SELECT name FROM sqlite_master WHERE type = 'view'"):
            self.db.execute(f'DROP VIEW IF EXISTS "{row["name"]}"')
        self.db.execute("PRAGMA foreign_keys = OFF")
        try:
            self.db.execute("DROP TABLE IF EXISTS items_new")
            self.db.execute(_ITEMS_REBUILD_DDL)
            has_waiting = "waiting_for" in columns
            self.db.execute(
                "INSERT INTO items_new("
                "id, kind, title, notes, url, parent_id, status, prev_status, "
                "priority, defer_at, due_at, duration, started_at, completed_at, "
                "dropped_at, notified_at, " + ("waiting_for, " if has_waiting else "") +
                "repeat, review, reviewed_at, "
                "rank, created_at, updated_at, completed) "
                "SELECT id, kind, title, notes, url, parent_id, status, prev_status, "
                "priority, defer_at, due_at, duration, started_at, completed_at, "
                "dropped_at, notified_at, " + ("waiting_for, " if has_waiting else "") +
                "repeat, review, reviewed_at, "
                "rank, created_at, updated_at, completed FROM items")
            self.db.execute("DROP TABLE items")
            self.db.execute("ALTER TABLE items_new RENAME TO items")
        finally:
            self.db.execute("PRAGMA foreign_keys = ON")

    def _migrate_v15(self) -> None:
        """Drop the waiting_for column: delegation no longer records a name.

        Parking a task as 'waiting' is still a status, but the store no longer
        remembers who it is waiting on, so the column goes. sqlite refuses to
        drop a column any view still points at, and `open_items` (`SELECT *`,
        frozen at creation) and the `waiting_for` view both name it, so every
        view is dropped first and executescript rebuilds them around the fresh
        table on the same open.
        """
        columns = {row["name"] for row in self.query("PRAGMA table_info(items)")}
        if "waiting_for" not in columns:
            return
        for row in self.query(
                "SELECT name FROM sqlite_master WHERE type = 'view'"):
            self.db.execute(f'DROP VIEW IF EXISTS "{row["name"]}"')
        self.db.execute("ALTER TABLE items DROP COLUMN waiting_for")

    def _migrate_v6(self) -> None:
        """Realign every action with the new inbox rule.

        The inbox now means "no project and no context". Actions that had
        acquired a home but were still sitting in the inbox move out; actions
        that had neither move back in.
        """
        tagged = {row["item_id"] for row in self.query("SELECT item_id FROM item_tags")}
        rows = self.query(
            "SELECT id, status, parent_id FROM items WHERE kind = 'action' "
            "AND status IN ('inbox', 'active')")
        for row in rows:
            has_home = row["parent_id"] is not None or row["id"] in tagged
            target = "active" if has_home else "inbox"
            if row["status"] != target:
                self.db.execute(
                    "UPDATE items SET status = ?, updated_at = ? WHERE id = ?",
                    (target, now(), row["id"]))

    def _drop_stale_deadline_views(self) -> None:
        """Rebuild any deadline view whose body predates the calendar-day rule.

        schema.sql can only CREATE VIEW IF NOT EXISTS, so a view that already
        exists is never replaced. Dropping it here lets executescript build the
        current definition in the same open.
        """
        for view in ("overdue", "due_today", "upcoming"):
            body = self.scalar(
                "SELECT sql FROM sqlite_master WHERE type = 'view' AND name = ?",
                (view,))
            if body and "date('now', 'localtime')" not in body:
                self.db.execute(f"DROP VIEW IF EXISTS {view}")
        # The Today views used to take in deferred work with no deadline, which
        # kept an old someday/maybe task on Today forever. They now require a
        # real deadline, so a body that still admits `due_at IS NULL` inside its
        # defer window — or that never got the `>= date('now'...)` deadline
        # rule at all — is stale and is rebuilt from schema.sql.
        for view in ("today", "today_open"):
            body = self.scalar(
                "SELECT sql FROM sqlite_master WHERE type = 'view' AND name = ?",
                (view,))
            if body and ("due_at IS NULL" in body
                         or "substr(due_at, 1, 10) >= date('now', 'localtime')" not in body):
                self.db.execute(f"DROP VIEW IF EXISTS {view}")

    def _drop_stale_archived_views(self) -> None:
        """Rebuild the views that must leave archived work out.

        `archived` is a new status, so a database that recorded the current
        schema version before `open_items` and `tag_load` learned to exclude it
        keeps the older bodies — schema.sql can only CREATE VIEW IF NOT EXISTS,
        which never replaces an existing view. Dropping them here lets
        executescript build the current definitions in the same open. A database
        rebuilt by `_migrate_v14` already has them right, so this is a no-op
        there; it is the repair for one that reached version 14 beforehand.
        """
        for view in ("open_items", "tag_load"):
            body = self.scalar(
                "SELECT sql FROM sqlite_master WHERE type = 'view' AND name = ?",
                (view,))
            if body and "'archived'" not in body:
                self.db.execute(f"DROP VIEW IF EXISTS {view}")

    # ------------------------------------------------------------- queries

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.db.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.db.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: tuple | dict = (), default=None):
        row = self.one(sql, params)
        if row is None:
            return default
        return row[0]

    def run(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.db.execute(sql, params)

    def transaction(self):
        return self.db

    # ----------------------------------------------------------------- log

    def log(self, item_id: str | None, kind: str, detail: str | None = None) -> None:
        """Append to the history. Every state change goes through here so
        `gtd log` and the "untouched for N days" review signal stay truthful."""
        self.db.execute(
            "INSERT INTO log(at, item_id, kind, detail) VALUES(?, ?, ?, ?)",
            (now(), item_id, kind, detail),
        )

    # -------------------------------------------------------------- meta

    def meta(self, key: str, default: str | None = None) -> str | None:
        value = self.scalar("SELECT value FROM meta WHERE key = ?", (key,))
        return default if value is None else str(value)

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def open_store(profile: str | None = None, path: str | None = None) -> Store:
    return Store(path=path, profile=profile)
