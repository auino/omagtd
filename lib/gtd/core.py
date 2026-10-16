"""The workflow.

Five functions, in GTD order, each one a step of the discipline:

    capture()   1 — get it out of your head
    clarify()   2 — decide what it actually is
    organize()  3 — next action, context, project, date, flags
    (review)    4 — see the whole system; lives in :mod:`gtd.review`
    engage()    5 — do the next thing, or consciously not

OmniFocus is the reference for the vocabulary: an Inbox you empty rather than
organize on the fly, actions rather than tasks, contexts as tags, defer and
due dates rather than "when", and projects that complete when their last
action does.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
from pathlib import Path

from . import dates, quick, repository
from .db import STAMP, Store, new_id, now, today

ACTION = "action"
PROJECT = "project"
AREA = "area"

CONTAINERS = (PROJECT, AREA)
LIVE_STATUSES = ("active", "waiting", "onhold", "someday", "reference")
LIVE_SQL = "('inbox', 'active', 'waiting', 'onhold', 'someday', 'reference')"

CLARIFY_OUTCOMES = ("actionable", "do-now", "defer", "wait", "someday",
                    "reference", "delete")
ENGAGE_ACTIONS = ("start", "stop", "done", "undone", "drop", "wait", "resume",
                  "defer", "flag", "unflag", "inbox", "archive", "unarchive",
                  "trash")
REVIEW_INTERVALS = ("daily", "weekly", "monthly", "quarterly", "yearly", "never")


class GtdError(Exception):
    """Anything the user did wrong, phrased so the CLI can print it as-is.

    `reason` is an optional, stable machine-readable tag (used by the panel
    and the full-screen view to tell one failure from another without parsing
    the message).
    """

    def __init__(self, message, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


# --------------------------------------------------------------- fetching


def as_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    item["repeat"] = _decode_repeat(item.get("repeat"))
    return item


def _decode_repeat(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


ORIGIN_RE = re.compile(r"\s+(?:after|from)\s+(complete|schedule)\s*$", re.IGNORECASE)


def _encode_repeat(rule) -> str | None:
    if rule is None:
        return None
    if isinstance(rule, str):
        rule = rule.strip()
        if not rule:
            return None
        if rule.startswith("{"):
            # Already in the stored JSON form (imports hand us one verbatim).
            try:
                rule = json.loads(rule)
            except ValueError as exc:
                raise GtdError(f"cannot read repeat rule: {exc}") from exc
        else:
            # "every 2 weeks after schedule" — the cadence plus where the
            # countdown starts, which is what the exporter writes.
            origin = "complete"
            suffix = ORIGIN_RE.search(rule)
            if suffix:
                origin = suffix.group(1).lower()
                rule = rule[:suffix.start()]
            parsed = dates.parse_duration(rule)
            if not parsed:
                raise GtdError(f"cannot read repeat rule: {rule!r}")
            rule = {"interval": parsed[0], "unit": parsed[1], "from": origin}
    elif isinstance(rule, (tuple, list)):
        # Quick entry produces (interval, unit); accept it as a first-class shape.
        rule = {"interval": rule[0], "unit": rule[1], "from": "complete"}
    elif not isinstance(rule, dict):
        raise GtdError(f"cannot read repeat rule: {rule!r}")
    interval = int(rule.get("interval", 1))
    unit = str(rule.get("unit", "week")).lower()
    if unit not in ("day", "week", "month", "year"):
        raise GtdError(f"unknown repeat unit: {unit}")
    if interval <= 0:
        raise GtdError("repeat interval must be positive")
    origin = str(rule.get("from", "complete")).lower()
    if origin not in ("complete", "schedule"):
        raise GtdError("repeat 'from' must be complete or schedule")
    return json.dumps({"interval": interval, "unit": unit, "from": origin},
                      sort_keys=True)


def find(store: Store, ref: str, *, kind: str | None = None) -> dict:
    """Resolve a reference to one item.

    Exact id, unique id prefix, or the convenience references `last` and `-2`
    (second most recent). Anything ambiguous raises rather than guessing.
    """
    ref = "" if ref is None else str(ref).strip()
    if not ref:
        raise GtdError("need an item id")

    where = "status <> 'trashed'" if ref in ("last", "@last", "-", "-1") else None
    params: list = []

    if ref in ("last", "@last", "-", "-1"):
        sql = "SELECT * FROM items WHERE status NOT IN ('archived', 'trashed')"
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT 1"
        row = store.one(sql, params)
        if row is None:
            raise GtdError("nothing captured yet")
        return as_dict(row)
    if ref.startswith("-") and ref[1:].isdigit():
        offset = max(0, int(ref[1:]) - 1)
        sql = "SELECT * FROM items WHERE status NOT IN ('archived', 'trashed')"
        params = []
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT 1 OFFSET ?"
        row = store.one(sql, [*params, offset])
        if row is None:
            raise GtdError(f"no item {ref}")
        return as_dict(row)

    if kind:
        row = store.one("SELECT * FROM items WHERE id = ? AND kind = ?", (ref, kind))
    else:
        row = store.one("SELECT * FROM items WHERE id = ?", (ref,))
    if row is not None:
        return as_dict(row)

    sql = "SELECT id, title, kind FROM items WHERE id LIKE ?"
    args = [ref + "%"]
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    sql += " ORDER BY length(id), created_at"
    rows = store.query(sql, args)
    if len(rows) == 1:
        return as_dict(store.one("SELECT * FROM items WHERE id = ?", (rows[0]["id"],)))
    if len(rows) > 1:
        options = ", ".join(f"{r['id']} ({r['title'][:24]})" for r in rows[:6])
        raise GtdError(f"{ref!r} matches {len(rows)} items: {options}")

    # Names are a convenience for humans; ids stay canonical. Containers win a
    # name collision because "Home admin" is meant to be the area, not some
    # action that happens to be called the same thing.
    sql = ("SELECT id, kind FROM items WHERE lower(title) = lower(?) "
           "AND status NOT IN ('archived', 'trashed')")
    args: list = [ref]
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    sql += (" ORDER BY CASE kind WHEN 'project' THEN 0 WHEN 'area' THEN 1 "
            "ELSE 2 END, created_at")
    rows = store.query(sql, args)
    if rows:
        best = rows[0]["kind"]
        winners = [r for r in rows if r["kind"] == best]
        if len(winners) > 1:
            names = ", ".join(winners[i]["id"] for i in range(min(6, len(winners))))
            raise GtdError(f"{ref!r} matches {len(winners)} items: {names}")
        return as_dict(store.one("SELECT * FROM items WHERE id = ?",
                                 (winners[0]["id"],)))
    raise GtdError(f"no item matches {ref!r}")


def get(store: Store, item_id: str) -> dict:
    """One item with everything the panel needs to draw a row."""
    row = store.one("SELECT * FROM item_titles WHERE id = ?", (item_id,))
    if row is None:
        raise GtdError(f"no item matches {item_id!r}")
    item = as_dict(row)
    item["tags"] = tags_of(store, item_id)
    item["attachments"] = attachments_of(store, item_id)
    item["children"] = [
        as_dict(r) for r in store.query(
            "SELECT id, title, status, priority, defer_at, due_at, repeat, "
            "completed, completed_at FROM items "
            "WHERE parent_id = ? AND status NOT IN ('archived', 'trashed') "
            "ORDER BY completed, rank, created_at", (item_id,))
    ]
    item["open_children"] = sum(1 for c in item["children"] if c["status"] in LIVE_STATUSES)
    item["last_log"] = store.scalar(
        "SELECT at FROM log WHERE item_id = ? ORDER BY at DESC, id DESC LIMIT 1",
        (item_id,))
    item["parent_path"] = container_path(store, item)
    return item


def container_path(store: Store, item: dict) -> str:
    """A container's ancestry, top-down, joined with ">" — "Personal>Healthcare".

    The editor seeds its project field with this whole path instead of the
    leaf `parent_title`, so a subproject reads where it actually lives. The
    chain is walked live over parent links — the same links `ensure_container`
    re-enters when an edit hands the path back — and an item filed nowhere
    gets "".
    """
    parent = item.get("parent_id")
    if not parent:
        return ""
    titles = []
    seen = set()
    hops = 0
    while parent and parent not in seen and hops < 64:
        seen.add(parent)
        row = store.one("SELECT title, parent_id FROM items WHERE id = ?", (parent,))
        if row is None:
            break
        titles.append(row["title"])
        parent = row["parent_id"]
        hops += 1
    return ">".join(reversed(titles))


def container_names(store: Store) -> dict[str, str]:
    """Map container name -> kind, so quick entry's `/Name/` can resolve."""
    return {
        row["title"].lower(): row["kind"]
        for row in store.query(
            "SELECT title, kind FROM items WHERE kind IN ('project', 'area')")
    }


# ------------------------------------------------------ name completion

# What each autocomplete kind draws its candidates from. Projects and areas
# share one namespace in quick entry, so they share one candidate list here.
COMPLETION_KINDS = ("project", "context", "date", "defer")


def complete_name(store: Store, kind: str, prefix: str) -> str | None:
    """Finish a half-typed name against the names already in use.

    An exact match (ignoring case) always wins, so a name that is already a
    prefix of a longer one is never dragged into that longer name. Otherwise a
    prefix match is only offered when exactly one existing name starts with
    it: `Ga` with two gardens in it is ambiguous, and guessing which one the
    user meant is worse than leaving the text alone.

    Returns the completion to substitute in, or None to leave the input as-is.
    """
    if kind not in COMPLETION_KINDS:
        raise GtdError(f"completion kind must be one of: {', '.join(COMPLETION_KINDS)}")
    text = str(prefix or "").strip()
    if not text:
        return None

    if kind in ("date", "defer"):
        return dates.complete_when(text, defer=kind == "defer")
    if kind == "project":
        candidates = [row["title"] for row in store.query(
            "SELECT title FROM items WHERE kind IN ('project', 'area') "
            "AND status NOT IN ('trashed', 'done', 'archived') "
            "ORDER BY title COLLATE NOCASE")]
    else:
        candidates = [row["name"] for row in store.query(
            "SELECT name FROM tags ORDER BY name COLLATE NOCASE")]

    for name in candidates:
        if name.lower() == text.lower():
            return None  # already exact: nothing to add
    hits = [name for name in candidates if name.lower().startswith(text.lower())]
    return hits[0] if len(hits) == 1 else None


# ------------------------------------------------------------------- tags


def tag_depth(name: str) -> int:
    """How deep a context path is: "home" is 0, "home>kitchen" is 1."""
    return str(name or "").count(">")


def ensure_tag(store: Store, name: str) -> dict:
    """Create a context, and every context above it.

    `#home>kitchen` is a path: "home" is ensured first, then "home>kitchen"
    with parent_id pointing at it, so a context can hold subcontexts. The full
    path is stored as the name, which keeps it unique and makes "everything
    under home" a simple prefix match.

    Every segment is folded to lowercase, because contexts get typed in a
    hurry: `Errands` and `errands` are one context, and the name the database
    holds is always the lowercase one.
    """
    clean = str(name).strip().lstrip("#@")
    segments = [segment.strip().lower() for segment in clean.split(">")]
    if any(not segment for segment in segments):
        raise GtdError(f"context path {name!r} has an empty segment")
    clean = ">".join(segments)
    parent_id = None
    tag = None
    path = ""
    stamp = now()
    for segment in segments:
        path = segment if not path else f"{path}>{segment}"
        row = store.one("SELECT * FROM tags WHERE name = ? COLLATE NOCASE", (path,))
        if row is None:
            tag_id = new_id()
            store.run(
                "INSERT INTO tags(id, name, parent_id, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?)", (tag_id, path, parent_id, stamp, stamp))
            store.log(None, "context", path)
            row = store.one("SELECT * FROM tags WHERE id = ?", (tag_id,))
        elif row["parent_id"] != parent_id:
            store.run("UPDATE tags SET parent_id = ?, updated_at = ? WHERE id = ?",
                      (parent_id, stamp, row["id"]))
            row = store.one("SELECT * FROM tags WHERE id = ?", (row["id"],))
        parent_id = row["id"]
        tag = row
    return dict(tag)


def tags_of(store: Store, item_id: str) -> list[str]:
    return [
        row["name"] for row in store.query(
            "SELECT t.name FROM item_tags it JOIN tags t ON t.id = it.tag_id "
            "WHERE it.item_id = ? ORDER BY t.name COLLATE NOCASE", (item_id,))
    ]


def split_names(names) -> list[str]:
    """Flatten a context list that may pack several contexts into one entry.

    A task takes any number of contexts, and the places they arrive from are
    loose about the packaging: the editor field is a single string of
    space-separated names, and so is `gtd capture --tag "errands home"`. Each
    entry is split on whitespace and commas here, once, so every caller gets
    separate contexts instead of one context called "errands home".
    """
    out = []
    for name in names or []:
        for part in re.split(r"[,\s]+", str(name).strip().lstrip("#@")):
            part = part.strip()
            if part:
                out.append(part)
    return out


def set_tags(store: Store, item_id: str, names) -> None:
    _undo_snapshot_item(store, item_id)   # a context change is a change too
    for name in split_names(names):
        tag = ensure_tag(store, name)
        store.run("INSERT OR IGNORE INTO item_tags(item_id, tag_id) VALUES(?, ?)",
                  (item_id, tag["id"]))


def add_tags(store: Store, item_id: str, names) -> None:
    set_tags(store, item_id, names)


def remove_tags(store: Store, item_id: str, names) -> None:
    _undo_snapshot_item(store, item_id)
    for name in split_names(names):
        row = store.one("SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (name,))
        if row:
            store.run("DELETE FROM item_tags WHERE item_id = ? AND tag_id = ?",
                      (item_id, row["id"]))


def set_tag_review(store: Store, name: str, interval: str) -> dict:
    tag = ensure_tag(store, name)
    if interval not in REVIEW_INTERVALS:
        raise GtdError(f"review must be one of: {', '.join(REVIEW_INTERVALS)}")
    store.run("UPDATE tags SET review = ?, updated_at = ? WHERE id = ?",
              (interval, now(), tag["id"]))
    store.log(None, "context-review", f"{tag['name']}: {interval}")
    return dict(store.one("SELECT * FROM tags WHERE id = ?", (tag["id"],)))


# ---------------------------------------------------------- attachments


def attachments_of(store: Store, item_id: str) -> list[dict]:
    rows = store.query(
        "SELECT * FROM attachments WHERE item_id = ? ORDER BY created_at, rowid",
        (item_id,))
    return [dict(r) for r in rows]


def attachments_dir(store: Store) -> Path:
    """Where attached files are copied so an export can carry them along."""
    path = Path(store.path).resolve().parent / "attachments"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _remote_uri(uri: str) -> bool:
    if uri.lower().startswith("file://"):
        return False
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", uri)) or uri.startswith("mailto:")


def link_for(store: Store, item_id: str) -> str | None:
    """The link an item can open: its `url` field, else its first URL
    attachment. A local file is not a link — the editor hands files to the
    copy button, never to an opener — so file attachments are not offered.
    """
    row = store.one("SELECT url FROM items WHERE id = ?", (item_id,))
    if row is not None and row["url"]:
        return row["url"]
    for attachment in attachments_of(store, item_id):
        uri = attachment.get("uri") or ""
        if _remote_uri(uri):
            return uri
    return None


def _file_hash(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()[:12]


def _store_root(store: Store) -> Path:
    return Path(store.path).resolve().parent


def attachment_path(store: Store, uri: str) -> Path | None:
    """The file an attachment uri points at, or None when it names no file.

    What the database holds is the path relative to its own folder, so the
    store can be moved — or restored from an export somewhere else — without
    every row still naming the directory it was copied from. A URL has no
    file behind it, and neither does an empty uri.
    """
    if not uri or _remote_uri(uri):
        return None
    raw = uri[len("file://"):] if uri.startswith("file://") else uri
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        path = _store_root(store) / path
    return path


def _store_attachment(store: Store, item_id: str, uri: str,
                     name: str | None) -> tuple[str, str]:
    """Copy a local file into this item's managed attachment folder.

    Returns `(stored_uri, label)`. URLs and paths that do not exist are kept
    as-is, so a link to something on the web still works and a file that is
    attached before it is written is not silently dropped. A file that is
    copied is recorded by the path relative to the store's own folder, which
    is what makes it travel with the database rather than with this machine.
    """
    label = (name or uri.rstrip("/").rsplit("/", 1)[-1]).strip() or uri
    if _remote_uri(uri):
        return uri, label
    source = attachment_path(store, uri)
    if source is None or not source.exists():
        return uri, label
    resolved = source.resolve()
    managed_dir = attachments_dir(store).resolve()
    task_dir = re.sub(r"[^A-Za-z0-9_-]+", "-", str(item_id)).strip("-") or "task"
    target_dir = managed_dir / task_dir
    try:
        resolved.relative_to(target_dir.resolve())
        return str(resolved.relative_to(_store_root(store))), label
    except ValueError:
        pass
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-") or "file"
    # Keep copied files together by task; the digest avoids collisions inside
    # that task's attachment folder.
    destination = target_dir / f"{_file_hash(resolved)}-{safe}"
    target_dir.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.copy2(resolved, destination)
    return str(destination.relative_to(_store_root(store))), label


def add_attachment(store: Store, ref: str, uri: str, *, name: str | None = None) -> dict:
    """Point an item at a file or URL. An item holds one attachment.

    Giving it a second replaces the first, because a file picker hands over
    exactly one file and "attach this instead" is what pressing it means.
    Reattaching the file already held changes nothing.

    Local files are copied into the store's `attachments/` folder so the
    content survives the original moving and so export can bundle it.
    """
    item = find(store, ref)
    clean = str(uri or "").strip()
    if not clean:
        raise GtdError("need a file path or URL to attach")
    stored, label = _store_attachment(store, item["id"], clean, name)
    held = store.query(
        "SELECT * FROM attachments WHERE item_id = ? ORDER BY created_at, rowid",
        (item["id"],))
    if len(held) == 1 and held[0]["uri"] == stored:
        return dict(held[0])
    for row in held:
        store.run("DELETE FROM attachments WHERE id = ?", (row["id"],))
        # A file may back another item too, so the discard asks what is left
        # after this row is gone rather than assuming it was the only one.
        if row["uri"] != stored:
            _discard_managed_file(store, row["uri"])
            store.log(item["id"], "detach", row["uri"])
    attachment_id = new_id()
    stamp = now()
    store.run(
        "INSERT INTO attachments(id, item_id, name, uri, created_at, updated_at) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        (attachment_id, item["id"], label, stored, stamp, stamp))
    store.log(item["id"], "attach", stored)
    return dict(store.one("SELECT * FROM attachments WHERE id = ?", (attachment_id,)))


def remove_attachment(store: Store, ref: str, target: str) -> bool:
    """Detach by id or by uri. Returns whether anything was removed."""
    item = find(store, ref)
    row = store.one(
        "SELECT * FROM attachments WHERE item_id = ? AND (id = ? OR uri = ?)",
        (item["id"], target, target))
    if row is None:
        return False
    store.run("DELETE FROM attachments WHERE id = ?", (row["id"],))
    _discard_managed_file(store, row["uri"])
    store.log(item["id"], "detach", row["uri"])
    return True


def attachment_uris(store: Store, item_ids) -> list[str]:
    """Every attachment these items hold, descendants included.

    Read before the rows go: `items.parent_id` is ON DELETE CASCADE, so
    purging a project takes its actions with it and their files would
    otherwise be left on disk with nothing to say who put them there.
    """
    ids = [str(i) for i in item_ids]
    if not ids:
        return []
    marks = ", ".join("?" for _ in ids)
    rows = store.query(
        "WITH RECURSIVE branch(id) AS ("
        f"  SELECT id FROM items WHERE id IN ({marks})"
        "  UNION ALL"
        "  SELECT i.id FROM items i JOIN branch b ON i.parent_id = b.id)"
        "SELECT DISTINCT a.uri FROM attachments a JOIN branch b ON a.item_id = b.id",
        tuple(ids))
    return [str(r["uri"] or "") for r in rows]


def discard_attachments(store: Store, uris) -> None:
    """Take the files behind attachment uris that nothing points at any more.

    Call this once the rows those uris belonged to are gone; the count of
    what still references a uri is what decides, so a file shared with a
    task that survived is left where it is.
    """
    for uri in dict.fromkeys(str(u) for u in uris if u):
        _discard_managed_file(store, uri)


def _discard_managed_file(store: Store, uri: str) -> None:
    """Delete the copied file, but only once no row still points at it.

    Files live under `attachments/<item-id>/`; legacy shared paths still wait
    until their final database reference is removed.
    """
    if not uri or _remote_uri(uri):
        return
    if store.scalar("SELECT COUNT(*) FROM attachments WHERE uri = ?", (uri,)):
        return
    path = attachment_path(store, uri)
    if path is None:
        return
    try:
        resolved = path.resolve()
        managed_dir = attachments_dir(store).resolve()
        resolved.relative_to(managed_dir)
        resolved.unlink()
        if resolved.parent != managed_dir:
            resolved.parent.rmdir()
    except OSError:
        pass
    except ValueError:
        pass


def remove_item_attachments(store: Store, ref: str) -> int:
    """Remove attachment records and managed files belonging to one item."""
    item = find(store, ref)
    uris = [str(row["uri"] or "") for row in store.query(
        "SELECT uri FROM attachments WHERE item_id = ?", (item["id"],))]
    count = int(store.scalar(
        "SELECT COUNT(*) FROM attachments WHERE item_id = ?", (item["id"],)) or 0)
    store.run("DELETE FROM attachments WHERE item_id = ?", (item["id"],))
    discard_attachments(store, uris)
    return count


# ------------------------------------------------------------- containers


def lookup_container(store: Store, name: str, kind: str) -> dict | None:
    row = store.one(
        "SELECT * FROM items WHERE kind = ? AND title = ? COLLATE NOCASE "
        "AND status <> 'trashed'", (kind, str(name).strip()))
    return as_dict(row)


def _ensure_path(store: Store, parts: list, *, review: str = "never",
                 area=None) -> dict:
    """Create (reusing) the project chain in `parts` and return the end node.

    An existing AREA named like the first segment becomes the root of the
    chain — "Personal>Healthcare" is Healthcare in the Personal area — and
    every later segment is a project nested below the one before it.
    Otherwise every segment is a project. `area` is only attached to a project
    that is not already rooted in an area, because a chain rooted at one has
    its home already.
    """
    node = None
    segments = parts
    head = lookup_container(store, parts[0], AREA)
    if head is not None:
        node = head
        segments = parts[1:]
    for seg in segments:
        if node is None:
            node = ensure_container(store, seg, PROJECT, area=area, review=review)
        elif node["kind"] == "area":
            # Hand the project to the area by name, the way `--area` does;
            # its id passed as `parent` would read as a title and go astray.
            node = ensure_container(store, seg, PROJECT, area=node["title"],
                                    review=review)
        else:
            node = ensure_container(store, seg, PROJECT, parent=node["id"],
                                    review=review)
    return node


def ensure_container(store: Store, name: str, kind: str = PROJECT,
                     *, review: str = "never", area=None, parent=None) -> dict:
    """Find a project or area by name, creating it if it does not exist.

    An item id is accepted as well: the UI works in ids, and `edit --project
    2a4j6xze` should file the item under that project rather than invent a new
    project literally called "2a4j6xze".
    """
    clean = str(name).strip()
    if not clean:
        raise GtdError(f"empty {kind} name")
    if kind not in CONTAINERS:
        raise GtdError(f"{kind} is not a container")

    # "Move house>Kitchen" is a path: create the parent chain, then the leaf
    # under it. Existing names are reused, so this is idempotent like the rest.
    # A leading name that already belongs to an area roots the chain there.
    if kind == PROJECT and ">" in clean:
        parts = [part.strip() for part in clean.split(">")]
        parts = [part for part in parts if part]
        if not parts:
            raise GtdError(f"empty {kind} name")
        if len(parts) > 1:
            return _ensure_path(store, parts, review=review, area=area)
        clean = parts[0]

    row = store.one(
        "SELECT * FROM items WHERE kind = ? AND title = ? COLLATE NOCASE "
        "AND status <> 'trashed'", (kind, clean))
    if row is None:
        # The UI works in ids, so one is accepted in place of a name.
        row = store.one(
            "SELECT * FROM items WHERE id = ? AND kind = ? AND status <> 'trashed'",
            (clean, kind))
    if row is None:
        if review not in REVIEW_INTERVALS:
            review = "never"
        stamp = now()
        item_id = new_id()
        store.run(
            "INSERT INTO items(id, kind, title, status, review, created_at, updated_at) "
            "VALUES(?, ?, ?, 'active', ?, ?, ?)",
            (item_id, kind, clean, review, stamp, stamp))
        store.log(item_id, "create", f"{kind}: {clean}")
        row = store.one("SELECT * FROM items WHERE id = ?", (item_id,))
    node = as_dict(row)
    if kind == PROJECT and (parent or area):
        holder = (ensure_container(store, parent, PROJECT) if parent
                  else ensure_container(store, area, AREA))
        if holder["id"] == node["id"]:
            raise GtdError(f"a {kind} cannot contain itself")
        if node.get("parent_id") != holder["id"]:
            if _is_descendant(store, holder["id"], node["id"]):
                raise GtdError(f"cannot move {node['title']} under its own "
                               f"descendant {holder['title']}")
            _update(store, node["id"], parent_id=holder["id"])
            node["parent_id"] = holder["id"]
    return node


def _is_descendant(store: Store, node_id: str, ancestor_id: str) -> bool:
    """True when `node_id` is `ancestor_id` itself or nested somewhere below it."""
    seen: set[str] = set()
    current = node_id
    while current:
        if current == ancestor_id:
            return True
        if current in seen:  # a cycle already in the data: stop walking
            return True
        seen.add(current)
        current = store.scalar("SELECT parent_id FROM items WHERE id = ?",
                               (current,))
    return False


def close_container(store: Store, ref: str, *, drop_open: bool = True,
                    note: str | None = None) -> dict:
    """Finish a project or area. Closing a project drops whatever was still in
    it, exactly like OmniFocus, and says so."""
    item = find(store, ref)
    if item["kind"] not in CONTAINERS:
        raise GtdError(f"{item['id']} is an action, not a container")
    dropped = []
    if drop_open:
        rows = store.query(
            "SELECT id, title FROM items WHERE parent_id = ? AND status IN "
            "('inbox', 'active', 'waiting', 'onhold', 'someday', 'reference')",
            (item["id"],))
        dropped = [r["id"] for r in rows]
        for child in rows:
            _update(store, child["id"], status="dropped", dropped_at=now())
            store.log(child["id"], "dropped-with", item["title"])
    stamp = now()
    _update(store, item["id"], status="done", completed=1, completed_at=stamp)
    store.log(item["id"], "closed", note or ("dropped %d actions" % len(dropped) if dropped else "closed"))
    _refresh_container(store, item["parent_id"])
    result = get(store, item["id"])
    result["dropped"] = dropped
    return result


def reopen_container(store: Store, ref: str) -> dict:
    item = find(store, ref)
    if item["kind"] not in CONTAINERS:
        raise GtdError(f"{item['id']} is an action, not a container")
    _update(store, item["id"], status="active", completed=0, completed_at=None)
    store.log(item["id"], "reopened", item["title"])
    _refresh_container(store, item["id"])
    _refresh_container(store, item["parent_id"])
    return get(store, item["id"])


def resolve_parent(store: Store, project=None, area=None, *, create: bool = True) -> dict | None:
    """Work out where an action belongs.

    Explicit flags beat quick-entry syntax; a project with no area stays a
    project with no area, which is what the weekly review nags about.
    """
    def pick(name, kind):
        if create:
            return ensure_container(store, name, kind)
        return lookup_container(store, name, kind)

    if project:
        node = pick(project, PROJECT)
        if node is None:
            raise GtdError(f"no project called {project!r}")
        if area:
            holder = pick(area, AREA)
            if holder is None:
                raise GtdError(f"no area called {area!r}")
            if node.get("parent_id") != holder["id"]:
                _update(store, node["id"], parent_id=holder["id"])
        return node
    if area:
        node = pick(area, AREA)
        if node is None:
            raise GtdError(f"no area called {area!r}")
        return node
    return None


def _rank_next(store: Store, parent_id: str | None) -> int:
    if not parent_id:
        return 0
    top = store.scalar(
        "SELECT MAX(rank) FROM items WHERE parent_id = ? AND status <> 'trashed'",
        (parent_id,))
    return int(top or 0) + 10


def _update(store: Store, item_id: str, **fields) -> None:
    if not fields:
        return
    _undo_snapshot_item(store, item_id)   # before-image, once per command
    if "repeat" in fields:
        fields["repeat"] = _encode_repeat(fields["repeat"])
    fields["updated_at"] = now()
    columns = ", ".join(f"{name} = ?" for name in fields)
    store.run(f"UPDATE items SET {columns} WHERE id = ?", (*fields.values(), item_id))


def _field(value, *, defer=False):
    """Normalize a date-ish argument: datetime, date, or a parseable string.

    `defer` picks the configured defer clock for a value that carries no time
    of its own — a bare date or a word like "tomorrow" — so the `--defer` flag
    lands on the same hour as the inline `<tomorrow` form. A value that already
    has a time (a datetime from the quick parser) keeps it.
    """
    if value in (None, ""):
        return None
    if isinstance(value, _dt.datetime):
        return dates.to_stamp(value)
    if isinstance(value, _dt.date):
        clock = dates.DEFAULT_DEFER_TIME if defer else dates.DEFAULT_TIME
        return dates.to_stamp(value, clock)
    parsed = dates.parse_when(str(value), defer=defer)
    if parsed is None:
        raise GtdError(f"cannot read date: {value!r}")
    return parsed.strftime("%Y-%m-%d %H:%M")


def _validate_dates(defer_at, due_at):
    """The defer date opens the window a task is live for; it cannot open
    after the deadline that closes it. Compared day by day, so a task
    deferred and due on the same day is fine."""
    if defer_at and due_at and defer_at[:10] > due_at[:10]:
        raise GtdError(
            f"defer date {defer_at[:10]} must not be after the deadline "
            f"{due_at[:10]}")


def _minutes(value):
    """Normalize an effort estimate to whole minutes, or None."""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise GtdError(f"cannot read duration: {value!r}")
    if isinstance(value, int):
        return value if value > 0 else None
    parsed = dates.parse_minutes(value)
    if parsed is None:
        raise GtdError(f"cannot read duration: {value!r} (try 30m, 1h, 1h30m)")
    return parsed


def _priority(value):
    """Normalize a flag. There is one flag, so 0 clears it and 1 sets it.

    OmniFocus offered three priority levels and the schema still holds them,
    but ranking tasks by how many marks were typed at them never made a day
    easier: everything flagged was already "do this first". Values above 1
    are refused rather than quietly lowered so a caller that still means
    three levels gets told, not believed.
    """
    if value in (None, ""):
        return 0
    try:
        level = int(value)
    except (TypeError, ValueError):
        raise GtdError(f"priority is 0 (off) or 1 (on), not {value!r}") from None
    if level < 0 or level > 1:
        raise GtdError(f"priority is 0 (off) or 1 (on), not {value}")
    return level


def normalize_inbox(store: Store, item_id: str) -> None:
    """Keep an action's status in step with the inbox rule.

    The inbox is every action with no project/area and no context: give it one
    of those and it becomes active (available); take the last one away and it
    falls back to the inbox. Waiting, someday, reference and the closed states
    are deliberate and stay where they are.
    """
    row = store.one("SELECT kind, status, parent_id FROM items WHERE id = ?",
                    (item_id,))
    if row is None or row["kind"] != ACTION:
        return
    if row["status"] not in ("inbox", "active"):
        return
    has_home = row["parent_id"] is not None or store.scalar(
        "SELECT 1 FROM item_tags WHERE item_id = ? LIMIT 1", (item_id,))
    target = "active" if has_home else "inbox"
    if target != row["status"]:
        _update(store, item_id, status=target)


# ---------------------------------------------------------- 1. capture


def capture(store: Store, text: str | None = None, *, title=None, notes: str = "",
            project=None, area=None, tags=(), priority=None, defer=None, due=None,
            duration=None, repeat=None, review=None,
            url=None, attach=(), to_inbox: bool = False, parse_text: bool = True) -> dict:
    """Put something into the system, unthought-through.

    Everything is optional; whatever is missing lands in the inbox, which is
    the correct home for a capture.
    """
    parsed: dict = {}
    if parse_text and text:
        parsed = quick.parse(text, container_names(store))
        title = title or parsed["title"]
        notes = notes or parsed.get("notes") or ""
        parsed_tags = parsed["tags"]
        tags = list(tags) + [t for t in parsed_tags if t not in tags]
        priority = priority if priority is not None else parsed["priority"]
        defer = defer if defer is not None else parsed["defer"]
        due = due if due is not None else parsed["due"]
        duration = duration if duration is not None else parsed.get("duration")
        repeat = repeat if repeat is not None else parsed["repeat"]
        project = project or parsed["project"]
        area = area or parsed["area"]
    elif text and title is None:
        title = text

    title = str(title or "").strip()
    if not title:
        raise GtdError("nothing to capture — need a title")

    parent = None if to_inbox else resolve_parent(store, project, area)

    if parent is not None:
        status = "active"
    else:
        status = "inbox"

    stamp = now()
    item_id = new_id()
    defer_at, due_at = _field(defer, defer=True), _field(due)
    _validate_dates(defer_at, due_at)
    _undo_snapshot_item(store, item_id)   # records "did not exist" for undo
    store.run(
        "INSERT INTO items(id, kind, title, notes, url, parent_id, status, priority, "
        "defer_at, due_at, duration, repeat, review, rank, "
        "created_at, updated_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (item_id, ACTION, title, (notes or "").strip(),
         (url or "").strip() or None,
         parent["id"] if parent else None, status, _priority(priority),
         defer_at, due_at, _minutes(duration),
         _encode_repeat(repeat),
         review if review in REVIEW_INTERVALS else "never",
         _rank_next(store, parent["id"] if parent else None), stamp, stamp))
    set_tags(store, item_id, tags)
    normalize_inbox(store, item_id)
    for uri in attach or ():
        add_attachment(store, item_id, uri)
    store.log(item_id, "capture", title)
    if parent:
        store.log(parent["id"], "add-action", title)
    return get(store, item_id)


# ----------------------------------------------------------- 2. clarify


def clarify(store: Store, ref: str, outcome: str, *, project=None, area=None,
            notes=None, defer=None, due=None, priority=None,
            tags=(), review=None) -> dict:
    """Decide what an inbox item is. This is the step people skip.

    outcome:
      actionable  a real next action — give it a project and it is organized
      do-now      it takes two minutes — done immediately (GTD's 2-minute rule)
      defer       real, but not now — give it a defer date
      wait        not yours to do — park it until someone else does
      someday     a maybe — incubate it, revisit at review
      reference   worth keeping, not worth doing
      delete      no. the trash takes it
    """
    outcome = str(outcome or "").strip().lower().replace("_", "-")
    if outcome not in CLARIFY_OUTCOMES:
        raise GtdError(f"clarify outcome must be one of: {', '.join(CLARIFY_OUTCOMES)}")

    item = find(store, ref, kind=ACTION)
    if item["status"] in ("done", "dropped", "archived"):
        raise GtdError(f"{item['id']} is already {item['status']}")

    if outcome == "delete":
        return trash(store, item["id"])

    if outcome == "do-now":
        return complete(store, item["id"])

    fields: dict = {}
    if notes is not None:
        fields["notes"] = notes
    if priority is not None:
        fields["priority"] = _priority(priority)
    if review is not None:
        if review not in REVIEW_INTERVALS:
            raise GtdError(f"review must be one of: {', '.join(REVIEW_INTERVALS)}")
        fields["review"] = review

    if outcome == "wait":
        fields["status"] = "waiting"
    elif outcome == "someday":
        fields["status"] = "someday"
    elif outcome == "reference":
        fields["status"] = "reference"
    elif outcome in ("actionable", "defer"):
        # Leaving the inbox means it has to have a home: a project, an area,
        # or at least a context. Refusing here is deliberate: an "organized"
        # action with nowhere to live is exactly the kind of thing that makes
        # people stop trusting their system.
        parent = resolve_parent(store, project, area)
        if parent is None and not item["parent_id"] and not tags:
            raise GtdError(
                f"an {outcome} action needs a project, area or context — "
                f"pass --project, --area or --tag")
        if parent is not None:
            fields["parent_id"] = parent["id"]
            fields["rank"] = _rank_next(store, parent["id"])
        fields["status"] = "active"
        if outcome == "defer" and defer is None:
            raise GtdError("a deferred action needs a defer date — pass --defer")

    if defer is not None:
        fields["defer_at"] = _field(defer, defer=True)
    if due is not None:
        fields["due_at"] = _field(due)
    _validate_dates(fields.get("defer_at", item["defer_at"]),
                    fields.get("due_at", item["due_at"]))

    _update(store, item["id"], **fields)
    if tags:
        add_tags(store, item["id"], tags)
    normalize_inbox(store, item["id"])
    store.log(item["id"], "clarify", outcome)
    if fields.get("parent_id") and fields["parent_id"] != item["parent_id"]:
        store.log(fields["parent_id"], "add-action", item["title"])
        _refresh_container(store, item["parent_id"])
        _refresh_container(store, fields["parent_id"])
    return get(store, item["id"])


# ---------------------------------------------------------- 3. organize


def organize(store: Store, ref: str, *, project=None, area=None, tags=None, add=None,
             drop_tags=None, defer=None, clear_defer=False, due=None, clear_due=False,
             duration=None, clear_duration=False,
             priority=None, notes=None, url=None, clear_url=False,
             attach=(), unattach=(),
             review=None, repeat=None, clear_repeat=False,
             inbox=False, onhold=None, rank=None, title=None) -> dict:
    """Give an item its home, context, timing and flags."""
    item = find(store, ref)
    fields: dict = {}

    if title is not None:
        name = str(title).strip()
        if not name:
            raise GtdError("title cannot be empty")
        fields["title"] = name

    if inbox:
        fields["status"] = "inbox"
        fields["parent_id"] = None
    elif onhold:
        # Truthiness, not `is not None`: the CLI's --onhold defaults to False,
        # and testing for None made that default swallow every --project and
        # --area that came with it.
        fields["status"] = "onhold"
    elif project or area:
        parent = resolve_parent(store, project, area)
        if item["kind"] in CONTAINERS and _is_descendant(store, parent["id"], item["id"]):
            raise GtdError(f"cannot move {item['title']} under its own "
                           f"descendant {parent['title']}")
        fields["parent_id"] = parent["id"]
        fields["rank"] = _rank_next(store, parent["id"])
        if item["status"] in ("inbox", "onhold"):
            fields["status"] = "active"

    if tags is not None:
        store.run("DELETE FROM item_tags WHERE item_id = ?", (item["id"],))
        set_tags(store, item["id"], tags)
    if add:
        add_tags(store, item["id"], add)
    if drop_tags:
        remove_tags(store, item["id"], drop_tags)

    if defer is not None:
        fields["defer_at"] = _field(defer, defer=True)
    if clear_defer:
        fields["defer_at"] = None
    if due is not None:
        fields["due_at"] = _field(due)
        # A new deadline is a new announcement.
        fields["notified_at"] = None
    if clear_due:
        fields["due_at"] = None
        fields["notified_at"] = None
    _validate_dates(fields.get("defer_at", item["defer_at"]),
                    fields.get("due_at", item["due_at"]))
    if duration is not None:
        fields["duration"] = _minutes(duration)
    if clear_duration:
        fields["duration"] = None
    if priority is not None:
        fields["priority"] = _priority(priority)
    if notes is not None:
        fields["notes"] = notes
    if url is not None:
        fields["url"] = str(url).strip() or None
    if clear_url:
        fields["url"] = None
    if review is not None:
        if review not in REVIEW_INTERVALS:
            raise GtdError(f"review must be one of: {', '.join(REVIEW_INTERVALS)}")
        fields["review"] = review
    if repeat is not None:
        _encode_repeat(repeat)  # validate before writing
        fields["repeat"] = repeat
    if clear_repeat:
        fields["repeat"] = None
    if rank is not None:
        fields["rank"] = int(rank)

    # Contexts count as a change in their own right: adding or removing one
    # touches only item_tags, so `fields` stays empty and a guard that looked
    # at fields alone rejected `gtd edit <id> --tag errands`.
    if (not fields and not attach and not unattach
            and tags is None and not add and not drop_tags):
        raise GtdError("nothing to change")

    old_parent = item["parent_id"]
    if fields:
        _update(store, item["id"], **fields)
        store.log(item["id"], "organize", ", ".join(sorted(fields)))
    for uri in attach or ():
        add_attachment(store, item["id"], uri)
    for uri in unattach or ():
        remove_attachment(store, item["id"], uri)
    if "parent_id" in fields and fields["parent_id"] != old_parent:
        store.log(fields["parent_id"], "add-action", item["title"])
        _refresh_container(store, old_parent)
        _refresh_container(store, fields["parent_id"])
    if not (inbox or onhold):
        normalize_inbox(store, item["id"])
    return get(store, item["id"])


# ------------------------------------------------------------ 5. engage


def engage(store: Store, ref: str, action: str, *, when=None, priority=None,
           notes=None, project=None, area=None, defer=None,
           due=None, tags=()) -> dict:
    """Do something about an item, or consciously decline to."""
    action = str(action or "").strip().lower()
    if action not in ENGAGE_ACTIONS:
        raise GtdError(f"engage action must be one of: {', '.join(ENGAGE_ACTIONS)}")

    item = find(store, ref)

    if action == "done":
        fields: dict = {}
        if priority is not None:
            fields["priority"] = _priority(priority)
        if notes is not None:
            fields["notes"] = notes
        if fields:
            _update(store, item["id"], **fields)
        return complete(store, item["id"], when=when)

    if action == "undone":
        # Mark a completed (done) task as active again. Undo handles most
        # mutating commands, but this makes it explicit in engage and maps
        # directly to the "u" shortcut in the UI.
        item = find(store, ref)
        if item["status"] == "done":
            _update(store, item["id"], status="active", completed_at=None,
                    started_at=None)
            store.log(item["id"], "undone", item["title"])
            _refresh_container(store, item["parent_id"])
        return get(store, item["id"])

    if action == "drop":
        _update(store, item["id"], status="dropped", dropped_at=now())
        store.log(item["id"], "drop", item["title"])
        _refresh_container(store, item["parent_id"])
        return get(store, item["id"])

    if action == "archive":
        return archive(store, item["id"])

    if action == "unarchive":
        return unarchive(store, item["id"])

    if action == "wait":
        # Waiting is a decision: park the task until someone else moves it.
        _update(store, item["id"], status="waiting")
        store.log(item["id"], "wait", item["title"])
        return get(store, item["id"])

    if action == "resume":
        _update(store, item["id"], status="active")
        store.log(item["id"], "resume", item["title"])
        return get(store, item["id"])

    if action == "start":
        if item["started_at"] is None:
            _update(store, item["id"], started_at=now())
            store.log(item["id"], "start", item["title"])
        return get(store, item["id"])

    if action == "stop":
        _update(store, item["id"], started_at=None)
        store.log(item["id"], "stop", item["title"])
        return get(store, item["id"])

    if action == "defer":
        if when is None and defer is None:
            raise GtdError("defer needs a date")
        _update(store, item["id"],
                defer_at=_field(when if when is not None else defer, defer=True))
        store.log(item["id"], "defer", str(when if when is not None else defer))
        return get(store, item["id"])

    if action == "trash":
        return trash(store, item["id"])

    if action in ("flag", "unflag"):
        level = 0 if action == "unflag" else (
            _priority(priority) if priority is not None else 1)
        _update(store, item["id"], priority=level)
        store.log(item["id"], "flag" if level else "unflag", item["title"])
        return get(store, item["id"])

    # inbox — send it back to the clarify queue. The inbox is "no project and
    # no context", so this drops both; otherwise it would not really be there.
    _update(store, item["id"], status="inbox", parent_id=None)
    store.run("DELETE FROM item_tags WHERE item_id = ?", (item["id"],))
    store.log(item["id"], "inbox", item["title"])
    _refresh_container(store, item["parent_id"])
    return get(store, item["id"])


def complete(store: Store, ref: str, *, when=None) -> dict:
    """Finish an action, and everything that follows from finishing it.

    Three consequences, all of them easy to forget by hand: the action is
    stamped done, a repeating action spawns its next instance, and a project
    whose last action is gone is marked complete.
    """
    item = find(store, ref)
    if item["status"] == "done":
        # Completing twice must not spawn a second instance of a repeating
        # action — imports and double-clicks both land here.
        return get(store, item["id"])
    stamp = _field(when) if when is not None else now()
    _update(store, item["id"], status="done", completed_at=stamp, started_at=None)
    store.log(item["id"], "done", item["title"])

    spawned = None
    if item.get("repeat"):
        spawned = _respawn(store, item)

    _refresh_container(store, item["parent_id"])
    done = get(store, item["id"])
    if spawned:
        done["next"] = {
            "id": spawned["id"], "title": spawned["title"],
            "defer_at": spawned.get("defer_at"), "due_at": spawned.get("due_at"),
        }
    return done


def postpone(store: Store, ref: str, *, days: int | None = None) -> dict:
    """Roll an action's deadline forward by one day, keeping its clock time.

    The quick "I will not make it today" gesture: a deadline moves to the next
    day at the same hour, so a 15:00 task stays a 15:00 task. Only the due
    date moves; the defer that opens the window is left alone, but it can never
    be pushed past the due that closes it, so a defer already sitting on the
    coming deadline is nudged to the new one. An action with no deadline has
    nothing to roll, so this is a no-op rather than an error.
    """
    item = find(store, ref)
    if not item.get("due_at"):
        return get(store, item["id"])

    count = days if days is not None else 1
    if not isinstance(count, int) or count == 0:
        return get(store, item["id"])

    moment = dates.parse_stamp(item["due_at"])
    if moment is None:
        raise GtdError(f"deadline for {item['title']!r} is not a date")
    rolled = dates.add_unit(moment, count, "day")
    new_due = rolled.strftime("%Y-%m-%d %H:%M")

    fields: dict = {"due_at": new_due}
    # A defer may have been parked on the old deadline; keep it inside the
    # window by walking it up with the due, since it cannot open after it.
    if item.get("defer_at") and item["defer_at"][:10] >= item["due_at"][:10]:
        fields["defer_at"] = dates.add_unit(
            dates.parse_stamp(item["defer_at"]), count, "day").strftime("%Y-%m-%d %H:%M")
    _update(store, item["id"], **fields)
    store.log(item["id"], "postpone", new_due)
    return get(store, item["id"])


def _respawn(store: Store, item: dict) -> dict:
    """Create the next instance of a repeating action."""
    rule = item["repeat"]
    interval, unit = int(rule.get("interval", 1)), str(rule.get("unit", "week"))
    origin = str(rule.get("from", "complete"))
    moment_now = dates.parse_stamp(now())

    def advance(moment):
        # Walk the grid until the slot is genuinely upcoming, so a repeating
        # item that slipped past its date does not keep replaying in the past.
        guard = 0
        while moment <= moment_now and guard < 5000:
            moment = dates.add_unit(moment, interval, unit)
            guard += 1
        return moment

    defer = due = None
    if origin == "schedule":
        if item.get("defer_at"):
            defer = dates.to_stamp(advance(dates.parse_stamp(item["defer_at"])))
        if item.get("due_at"):
            due = dates.to_stamp(advance(dates.parse_stamp(item["due_at"])))
        if defer is None and due is None:
            defer = dates.to_stamp(advance(moment_now))
    else:
        if item.get("defer_at"):
            defer = dates.to_stamp(dates.add_unit(moment_now, interval, unit))
        if item.get("due_at"):
            due = dates.to_stamp(advance(dates.parse_stamp(item["due_at"])))
        if defer is None and due is None:
            defer = now()

    return create_child(store, item["parent_id"], item["title"],
                        notes=item.get("notes") or "", defer=defer, due=due,
                        priority=item.get("priority", 0), repeat=rule,
                        review=item.get("review"), tags=tags_of(store, item["id"]),
                        status="active")


def create_child(store: Store, parent_id: str | None, title: str, **fields) -> dict:
    """Add an action to a container, or to the inbox when parent_id is None."""
    parent = None
    if parent_id:
        parent = as_dict(store.one("SELECT * FROM items WHERE id = ?", (parent_id,)))
        if parent is None:
            raise GtdError(f"no container {parent_id}")
        if parent["kind"] == ACTION:
            raise GtdError("actions cannot contain actions")

    title = str(title or "").strip()
    if not title:
        raise GtdError("need a title")

    status = fields.get("status") or ("active" if parent else "inbox")
    stamp = now()
    item_id = new_id()
    _undo_snapshot_item(store, item_id)
    store.run(
        "INSERT INTO items(id, kind, title, notes, parent_id, status, priority, "
        "defer_at, due_at, duration, repeat, review, rank, "
        "created_at, updated_at) "
        "VALUES(?, 'action', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (item_id, title, (fields.get("notes") or "").strip(),
         parent["id"] if parent else None, status,
         _priority(fields.get("priority")),
         _field(fields.get("defer"), defer=True), _field(fields.get("due")),
         _minutes(fields.get("duration")),
         _encode_repeat(fields.get("repeat")),
         fields.get("review") if fields.get("review") in REVIEW_INTERVALS else "never",
         _rank_next(store, parent["id"] if parent else None), stamp, stamp))
    set_tags(store, item_id, fields.get("tags") or [])
    store.log(item_id, "create", title)
    if parent:
        store.log(parent["id"], "add-action", title)
        _refresh_container(store, parent["id"])
    return get(store, item_id)


def _drop_empty_project(store: Store, project: dict) -> bool:
    """Remove a project left with nothing in it by its last action finishing.

    Once a project has had work in it, that work is what makes it worth having:
    a project whose only actions are all done or in the trash is a name with
    nothing behind it, and leaving it there only feeds the stale counts the bar
    and the review read.

    This is deliberately not the rule behind the `empty_projects` view, which
    the review uses for projects that never had any actions at all and which
    still holds: a project created to incubate has no actions on purpose, and
    deleting it would make `someday` unusable.

    Only the envelope goes. `items.parent_id` is ON DELETE CASCADE, so the
    children have to be let go of it before the project row goes, or finishing
    an action would quietly destroy the very thing being archived. A finished
    action stays in the archive and an action in the trash keeps its 30 days;
    both simply lose the parent, so a restored one lands in the inbox instead
    of under a project that is no longer there. The project's own title stays
    in the log, so the removal is never silent.
    """
    had_work = store.scalar(
        "SELECT COUNT(*) FROM log WHERE item_id = ? AND kind = 'add-action'",
        (project["id"],))
    if not had_work:
        return False
    for child in store.query("SELECT id FROM items WHERE parent_id = ?",
                             (project["id"],)):
        # Detaching the children is part of the same undoable change as the
        # project's removal, so it goes through `_update` like any other edit.
        _update(store, child["id"], parent_id=None)
    store.log(project["id"], "auto-remove", project["title"])
    uris = attachment_uris(store, [project["id"]])
    _undo_snapshot_item(store, project["id"])
    store.run("DELETE FROM items WHERE id = ?", (project["id"],))
    discard_attachments(store, uris)
    return True


def _refresh_container(store: Store, container_id: str | None) -> None:
    """Recompute a container's completion flag from what is still inside it.

    A project completes when it has no live actions left. An area completes
    when it has no live projects left — the "standard of responsibility has
    nothing in progress" moment.
    """
    if not container_id:
        return
    node = as_dict(store.one("SELECT * FROM items WHERE id = ?", (container_id,)))
    if node is None or node["kind"] == ACTION:
        return

    if node["kind"] == PROJECT:
        pending = store.scalar(
            f"SELECT COUNT(*) FROM items WHERE parent_id = ? AND kind = 'action' "
            f"AND status IN {LIVE_SQL}", (container_id,))
    else:
        pending = store.scalar(
            "SELECT COUNT(*) FROM items WHERE parent_id = ? AND kind = 'project' "
            "AND completed = 0 AND status NOT IN ('trashed', 'done', 'dropped', 'archived')",
            (container_id,))

    completed = 1 if not pending else 0
    if completed == (node["completed"] or 0):
        return
    if completed and node["kind"] == PROJECT and _drop_empty_project(store, node):
        # The area that held it should notice the gap, so refresh upwards
        # before the deleted row can no longer be read.
        if node.get("parent_id"):
            _refresh_container(store, node["parent_id"])
        return
    _update(store, container_id, completed=completed)
    store.log(container_id, "completed" if completed else "reopened", node["title"])
    if node["kind"] == PROJECT and node.get("parent_id"):
        _refresh_container(store, node["parent_id"])


def set_status(store: Store, ref: str, status: str) -> dict:
    item = find(store, ref)
    status = str(status).strip().lower()
    valid = ("active", "onhold", "waiting", "someday", "reference", "inbox",
             "done", "dropped", "archived")
    if status not in valid:
        raise GtdError(f"status must be one of: {', '.join(valid)}")
    if status == "done":
        return complete(store, item["id"])
    if status == "archived":
        return archive(store, item["id"])
    _update(store, item["id"], status=status)
    store.log(item["id"], "status", status)
    _refresh_container(store, item["parent_id"])
    return get(store, item["id"])


# ---------------------------------------------------------------- trash


def trash(store: Store, ref: str) -> dict:
    item = find(store, ref)
    remove_item_attachments(store, item["id"])
    _update(store, item["id"], status="trashed", prev_status=item["status"])
    store.log(item["id"], "trash", item["title"])
    _refresh_container(store, item["parent_id"])
    return get(store, item["id"])


def restore(store: Store, ref: str) -> dict:
    rows = store.query(
        "SELECT * FROM items WHERE status = 'trashed' AND (id = ? OR id LIKE ?) "
        "ORDER BY updated_at DESC", (str(ref), str(ref) + "%"))
    if not rows:
        raise GtdError(f"no trashed item matches {ref!r}")
    if len(rows) > 1:
        options = ", ".join(f"{r['id']} ({r['title'][:24]})" for r in rows[:6])
        raise GtdError(f"{ref!r} matches {len(rows)} trashed items: {options}")
    item = as_dict(rows[0])
    back = item.get("prev_status") or ("active" if item["parent_id"] else "inbox")
    if back == "trashed":
        back = "inbox"
    _update(store, item["id"], status=back, prev_status=None)
    store.log(item["id"], "restore", back)
    return get(store, item["id"])


# How long a trashed item is kept before it is destroyed for good. Long enough
# to change your mind about a deletion, short enough that the trash is not a
# second archive nobody ever prunes.
TRASH_DAYS = 30


def sweep_trash(store: Store, *, days: int = TRASH_DAYS) -> list[str]:
     """Delete anything that has been in the trash longer than `days`.

     Called on every command, so the trash empties itself instead of asking to
     be emptied. It is one indexed read and, when nothing has expired, no
     writes at all.
     """
     cutoff = (_dt.datetime.now() - _dt.timedelta(days=days)).strftime(STAMP)
     ids = [r["id"] for r in store.query(
          "SELECT id FROM items WHERE status = 'trashed' AND updated_at < ?", (cutoff,))]
     uris = attachment_uris(store, ids)
     for item_id in ids:
         store.log(None, "purge", item_id)
         store.run("DELETE FROM items WHERE id = ?", (item_id,))
     discard_attachments(store, uris)
     return ids


def sweep_done(store: Store) -> list[str]:
     """Delete completed tasks that have aged past the retention setting.

     Reads `done_retention_days` from the settings: 0 keeps completed tasks
     forever, any positive number keeps them that many days. Called on every
     command after `sweep_trash`, so finished work empties itself on the same
     terms as the trash.
     """
     days = _done_retention_days(store)
     if days <= 0:
         return []
     cutoff = (_dt.datetime.now() - _dt.timedelta(days=days)).strftime(STAMP)
     ids = [r["id"] for r in store.query(
          "SELECT id FROM items WHERE status = 'done' AND "
          "completed_at < ?", (cutoff,))]
     uris = attachment_uris(store, ids)
     for item_id in ids:
         store.log(None, "purge", item_id)
         store.run("DELETE FROM items WHERE id = ?", (item_id,))
     discard_attachments(store, uris)
     return ids


# --------------------------------------------------------------- archive


def archive(store: Store, ref: str) -> dict:
    """Put an item away, out of every list but still on record.

    Archiving is not deleting: the item leaves the open lists, the way a
    completed one does, but stays readable and can be brought back with
    `unarchive`. Where to return it to is remembered in `prev_status`, the same
    way the trash remembers where a deleted item came from.
    """
    item = find(store, ref)
    if item["status"] == "archived":
        return get(store, item["id"])
    _update(store, item["id"], status="archived", prev_status=item["status"],
            archived_at=now())
    store.log(item["id"], "archive", item["title"])
    _refresh_container(store, item["parent_id"])
    return get(store, item["id"])


def unarchive(store: Store, ref: str) -> dict:
    """Bring an archived item back to the state it was put away from."""
    item = find(store, ref)
    if item["status"] != "archived":
        raise GtdError(f"{item['id']} is not archived")
    back = item.get("prev_status") or ("active" if item["parent_id"] else "inbox")
    if back in ("archived", "trashed"):
        back = "inbox"
    _update(store, item["id"], status=back, prev_status=None, archived_at=None)
    store.log(item["id"], "unarchive", back)
    _refresh_container(store, item["parent_id"])
    return get(store, item["id"])


def sweep_archived(store: Store) -> list[str]:
     """Delete archived tasks that have aged past the retention setting.

     Reads `archive_retention_days`: 0 keeps archived tasks forever, which is
     the default, and any positive number keeps them that many days from the
     day they were archived. Called on every command, after `sweep_done`, so
     the archive prunes itself on the same terms as the trash and the completed
     list.
     """
     days = _archive_retention_days(store)
     if days <= 0:
         return []
     cutoff = (_dt.datetime.now() - _dt.timedelta(days=days)).strftime(STAMP)
     ids = [r["id"] for r in store.query(
          "SELECT id FROM items WHERE status = 'archived' AND "
          "COALESCE(archived_at, updated_at) < ?", (cutoff,))]
     uris = attachment_uris(store, ids)
     for item_id in ids:
         store.log(None, "purge", item_id)
         store.run("DELETE FROM items WHERE id = ?", (item_id,))
     discard_attachments(store, uris)
     return ids


# How long a container must have been left alone before the auto-remove
# settings may take it. A project is almost always empty in the moment between
# creating it and capturing its first task; a day is far longer than that and
# still short enough that yesterday's leftovers are gone by tomorrow.
AUTO_REMOVE_GRACE_HOURS = 24

# The sweeper's own conditions, spelled once so the lists can hide exactly
# what it will take: never a container that is still safe to use, never one
# that is already due to go. Each fragment carries one `?` for the cutoff.
SWEEP_PROJECT_SQL = (
    "{alias}.kind = 'project' AND {alias}.status = 'active' "
    "AND {alias}.updated_at < ? "
    # Nothing filed under it at all — not "nothing to do", which is what the
    # `empty_projects` view reports for the weekly review, but nothing at all.
    # The strict reading is the safe one to delete on: `items.parent_id` is
    # ON DELETE CASCADE, so a project with any child would take the child too.
    "AND NOT EXISTS (SELECT 1 FROM items c WHERE c.parent_id = {alias}.id)")

SWEEP_CONTEXT_SQL = (
    "{alias}.updated_at < ? "
    # Nothing on it and no sub-context under it: tags.parent_id cascades too,
    # and a parent context is not empty when it only holds a used sub-context.
    "AND NOT EXISTS (SELECT 1 FROM item_tags it "
    "WHERE it.tag_id = {alias}.id) "
    "AND NOT EXISTS (SELECT 1 FROM tags c "
    "WHERE c.parent_id = {alias}.id)")


def _empty_cutoff(days: int) -> str:
    """The age a container must have reached before the sweeper may take it."""
    return (_dt.datetime.now()
            - _dt.timedelta(days=days)).strftime(STAMP)


def prune_cutoff(store: Store, key: str, alias: str = "t") -> tuple[str, str]:
    """The (filter, cutoff) that hides what the auto-remove setting would delete.

    A category with a 0-day retention keeps its containers forever, so its
    filter is empty and the cutoff unused. A positive retention hides anything
    empty for longer than that many days, on the same condition the sweeper
    uses, so a list and the sweeper can never disagree.
    """
    days = _empty_retention_days(store, key)
    if days <= 0:
        return ("", _empty_cutoff(0))
    cutoff = _empty_cutoff(days)
    if key == "auto_remove_empty_projects":
        return (f"NOT ({SWEEP_PROJECT_SQL.format(alias=alias)})", cutoff)
    return (f"NOT ({SWEEP_CONTEXT_SQL.format(alias=alias)})", cutoff)


def prune_where(store: Store, alias: str = "t") -> tuple[dict[str, str], dict[str, str]]:
    """The "hide what the auto-remove settings would delete" filters, if any.

    Returns (filters, cutoffs), each non-empty filter carrying exactly one `?`
    for its cutoff, and an empty string meaning the setting keeps containers
    forever — so no parameter goes with it and nothing is hidden, which is the
    default when a category is set to 0.

    The filters are the sweeper's conditions turned inside out, so a list and
    the sweeper can never disagree about what belongs there.
    """
    p_filter, p_cutoff = prune_cutoff(store, "auto_remove_empty_projects", alias=alias)
    c_filter, c_cutoff = prune_cutoff(store, "auto_remove_empty_contexts", alias=alias)
    return ({"projects": p_filter, "contexts": c_filter},
            {"projects": p_cutoff, "contexts": c_cutoff})


def sweep_empty(store: Store) -> dict[str, list[str]]:
    """Delete the empty projects and contexts the settings ask to remove.

    Called on every command, like `sweep_trash`: these are a setting, not a
    button, and a container that has just lost its last task should not outlive
    it by a poll interval. Each category ages on its own day count — 0 keeps it
    forever — so a freshly emptied project is only removed after the days its
    setting names. What is returned is what went, so a caller that wants to say
    so can.
    """
    removed: dict[str, list[str]] = {"projects": [], "contexts": []}

    proj_days = _empty_retention_days(store, "auto_remove_empty_projects")
    if proj_days > 0:
        cutoff = _empty_cutoff(proj_days)
        rows = store.query("SELECT id, title FROM items p WHERE "
                           + SWEEP_PROJECT_SQL.format(alias="p"), (cutoff,))
        uris = attachment_uris(store, [r["id"] for r in rows])
        for row in rows:
            store.log(None, "auto-remove", row["title"])
            store.run("DELETE FROM items WHERE id = ?", (row["id"],))
            removed["projects"].append(row["title"])
        discard_attachments(store, uris)

    ctx_days = _empty_retention_days(store, "auto_remove_empty_contexts")
    if ctx_days > 0:
        cutoff = _empty_cutoff(ctx_days)
        rows = store.query("SELECT id, name FROM tags t WHERE "
                           + SWEEP_CONTEXT_SQL.format(alias="t"), (cutoff,))
        for row in rows:
            store.log(None, "auto-remove", f"#{row['name']}")
            store.run("DELETE FROM tags WHERE id = ?", (row["id"],))
            removed["contexts"].append(row["name"])

    return removed


def purge(store: Store, ref: str | None = None, *, days: int | None = None,
          all_items: bool = False) -> dict:
    """Empty the trash.

    `days` takes only what has been sitting there longer than that; the ids
    are named explicitly, because emptying a whole database on a mistyped flag
    is not a mistake worth allowing.
    """
    if days is not None:
        cutoff = (_dt.datetime.now() - _dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
        ids = [r["id"] for r in store.query(
            "SELECT id FROM items WHERE status = 'trashed' AND updated_at < ?", (cutoff,))]
    elif all_items:
        ids = [r["id"] for r in store.query("SELECT id FROM items WHERE status = 'trashed'")]
    else:
        if not ref:
            raise GtdError("purge needs an id, --days N or --all")
        item = find(store, ref)
        if item["status"] != "trashed":
            raise GtdError(f"{item['id']} is not in the trash")
        ids = [item["id"]]
    uris = attachment_uris(store, ids)
    for item_id in ids:
        _undo_snapshot_tree(store, item_id)   # cascade deletes take the tree
        store.run("DELETE FROM items WHERE id = ?", (item_id,))
        store.log(None, "purge", item_id)
    discard_attachments(store, uris)
    return {"purged": ids}


# ------------------------------------------------------------------- undo

# Undo works as a journal. Every mutating CLI command opens a session before
# the handler runs and closes it when the handler returns; anything the
# handler changes through the shared write helpers (`_update`, capture,
# create_child, purge, and the containers pruning themselves) records a
# before-image of the row it touched into that session, once per item. A
# success commits the session to the `undo` table as a single entry; a
# failure discards it. `gtd undo <ref>` walks the most recent entry that
# touched the item and restores every before-image it holds.
#
# The journal lives on the Store instance, not in module state, so two stores
# opened in one process never share a session, and an entry is only ever
# visible after the command that made it has fully succeeded.


def undo_begin(store: Store) -> None:
    store._gtd_undo = {"items": {}, "tags": {}}


def undo_cancel(store: Store) -> None:
    store._gtd_undo = None


def undo_commit(store: Store, action: str) -> None:
    session = store._gtd_undo
    store._gtd_undo = None
    if not session or not session["items"]:
        return
    store.run("INSERT INTO undo(at, action) VALUES(?, ?)",
              (now(), (action or "").strip() or "change"))
    undo_id = store.scalar("SELECT last_insert_rowid()")
    for item_id, snapshot in session["items"].items():
        payload = dict(snapshot)
        payload["tags"] = session["tags"].get(item_id, [])
        store.run("INSERT INTO undo_item(undo_id, item_id, data) VALUES(?, ?, ?)",
                  (undo_id, item_id, json.dumps(payload)))


def _undo_tags_names(store: Store, item_id: str) -> list[str]:
    return sorted(r["name"] for r in store.query(
        "SELECT t.name FROM item_tags it JOIN tags t ON t.id = it.tag_id "
        "WHERE it.item_id = ?", (item_id,)))


def _undo_snapshot_item(store: Store, item_id: str) -> None:
    """Record the item's before-image, once per session per item.

    The first call wins, because that is the state the item was in before
    the command touched it — later changes inside the same command are part
    of what `undo` rolls back.
    """
    session = store._gtd_undo
    if not session or item_id in session["items"]:
        return
    row = store.one("SELECT * FROM items WHERE id = ?", (item_id,))
    session["items"][item_id] = {
        "absent": row is None,
        "row": dict(row) if row is not None else None,
    }
    session["tags"][item_id] = _undo_tags_names(store, item_id) if row else []


def _undo_snapshot_tree(store: Store, item_id: str) -> None:
    """Snapshot an item and everything under it.

    A purge deletes one row and sqlite cascades the children with it, so
    undo has to bring the whole subtree back — there is no coherent state
    where the project is gone and the actions it held are not.
    """
    _undo_snapshot_item(store, item_id)
    for child in store.query("SELECT id FROM items WHERE parent_id = ?",
                             (item_id,)):
        _undo_snapshot_tree(store, child["id"])


def undo(store: Store, ref: str | None = None) -> dict:
    """Undo the most recent tracked change.

    With `ref`, the most recent entry that touched that item; without one,
    the most recent entry anywhere. Every before-image in the chosen entry is
    restored, so a change that touched a project along with the task it was
    about is undone as one step. The entry is consumed: the next call sees
    the change before it.
    """
    undo_cancel(store)   # applying must not journal itself as the new change
    if ref:
        entry = _undo_find(store, ref)
        if entry is None:
            raise GtdError(f"nothing to undo for {ref!r}")
    else:
        entry = store.one("SELECT id, action FROM undo ORDER BY id DESC LIMIT 1")
        if entry is None:
            raise GtdError("nothing to undo")
    restored = _undo_apply(store, entry["id"])
    return {"reverted": entry["action"], "count": restored}


def _undo_find(store: Store, ref: str):
    """The newest entry that touched the item `ref` names, by exact id or
    unique prefix — the same way every other verb resolves its reference. A
    row that has since been purged still matches, because that is exactly
    when an undo is wanted."""
    try:
        ref = find(store, ref)["id"]
    except GtdError:
        pass
    return store.one(
        "SELECT u.id, u.action FROM undo u JOIN undo_item ui "
        "ON ui.undo_id = u.id WHERE ui.item_id LIKE ? "
        "ORDER BY u.id DESC LIMIT 1", (str(ref) + "%",))


def _undo_apply(store: Store, undo_id: int) -> int:
    rows = list(store.query("SELECT * FROM undo_item WHERE undo_id = ?",
                            (undo_id,)))
    payloads = [(row["item_id"], json.loads(row["data"])) for row in rows]
    _undo_check_absences(store, payloads)
    titles: list[str] = []
    for item_id, payload in _undo_order(store, payloads):
        if payload.get("absent"):
            uris = attachment_uris(store, [item_id])
            store.run("DELETE FROM items WHERE id = ?", (item_id,))
            discard_attachments(store, uris)
            titles.append(item_id)
            continue
        before = payload.get("row") or {}
        titles.append(_undo_restore_row(store, item_id, before))
        tags = payload.get("tags") or []
        store.run("DELETE FROM item_tags WHERE item_id = ?", (item_id,))
        if tags:
            set_tags(store, item_id, tags)
        # Recompute the completion flags the change moved — but with a helper
        # that never drops an empty project: undoing a change must not then
        # delete the container it just brought back.
        _undo_refresh_flag(store, item_id)
        _undo_refresh_flag(store, before.get("parent_id"))
    store.run("DELETE FROM undo WHERE id = ?", (undo_id,))
    store.log(None, "undo", ", ".join(t for t in titles if t))
    return len(rows)


def _undo_check_absences(store: Store, payloads) -> None:
    """Refuse an undo that would delete a row which has since gained children.

    A captured project the user has since filled in is not something undo can
    take back without taking the work with it. Children this same entry is
    about to delete do not count against it — that is not work, that is the
    rest of the same change.
    """
    absences = [item_id for item_id, payload in payloads
                if payload.get("absent")]
    for item_id in absences:
        others = [other for other in absences if other != item_id]
        sql = "SELECT COUNT(*) FROM items WHERE parent_id = ?"
        params: tuple = (item_id,)
        if others:
            sql += (" AND id NOT IN ("
                    + ", ".join("?" for _ in others) + ")")
            params = (item_id, *others)
        if store.scalar(sql, params):
            raise GtdError(
                f"cannot undo: {item_id} now holds children of its own")


def _undo_order(store: Store, payloads):
    """Sort what is about to be restored parents-first, deletions last.

    sqlite enforces `items.parent_id` as a foreign key, so a task cannot come
    back under a project that has not come back yet. Rows already in the
    database are of course ready at once.
    """
    pending = [(item_id, payload) for item_id, payload in payloads
               if not payload.get("absent")]
    deletions = [(item_id, payload) for item_id, payload in payloads
                 if payload.get("absent")]
    planned: set = set()
    ordered = []
    while pending:
        progress = False
        for entry in list(pending):
            row = entry[1].get("row") or {}
            parent = row.get("parent_id")
            if (not parent or parent in planned
                    or store.one("SELECT id FROM items WHERE id = ?", (parent,))):
                ordered.append(entry)
                planned.add(entry[0])
                pending.remove(entry)
                progress = True
        if not progress:      # unreachable in practice; sqlite will say why
            ordered.extend(pending)
            break
    ordered.extend(deletions)
    return ordered


def _undo_live_columns(store: Store) -> set[str]:
    """The columns the items table has right now.

    An undo entry was snapshot against whatever schema was live then; a column
    dropped or added since (the `waiting_for` field is the case that motivated
    this) must not be written back, or the restore hits a column the table no
    longer has and the whole undo fails.
    """
    return {row["name"] for row in store.query("PRAGMA table_info(items)")}


def _undo_restore_row(store: Store, item_id: str, before: dict) -> str:
    columns = [key for key in before if key != "id"]
    live = _undo_live_columns(store)
    columns = [c for c in columns if c in live]
    sets = ", ".join(f"{name} = ?" for name in columns)
    values = [before[name] for name in columns]
    if store.one("SELECT id FROM items WHERE id = ?", (item_id,)):
        if sets:
            store.run(f"UPDATE items SET {sets} WHERE id = ?", (*values, item_id))
    else:
        names = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        store.run(f"INSERT INTO items(id, {names}) VALUES(?, {placeholders})",
                  (item_id, *values))
    return str(before.get("title") or item_id)


def _undo_refresh_flag(store: Store, node_id: str | None) -> None:
    """Recompute a container's `completed` flag from what it holds.

    The same arithmetic `_refresh_container` does, minus the part that prunes
    a project left with nothing in it — inside an undo that pruning would
    undo the undo.
    """
    if not node_id:
        return
    node = store.one("SELECT * FROM items WHERE id = ?", (node_id,))
    if node is None or node["kind"] == ACTION:
        return
    if node["kind"] == PROJECT:
        pending = store.scalar(
            f"SELECT COUNT(*) FROM items WHERE parent_id = ? AND kind = 'action' "
            f"AND status IN {LIVE_SQL}", (node_id,))
    else:
        pending = store.scalar(
            "SELECT COUNT(*) FROM items WHERE parent_id = ? AND kind = 'project' "
            "AND completed = 0 AND status NOT IN ('trashed', 'done', 'dropped', 'archived')",
            (node_id,))
    completed = 1 if not pending else 0
    if completed != (node["completed"] or 0):
        store.run("UPDATE items SET completed = ?, updated_at = ? WHERE id = ?",
                  (completed, now(), node_id))


# --------------------------------------------------------------- review


def mark_reviewed(store: Store, ref: str | None = None, *, tag: str | None = None,
                  all_scopes: bool = False) -> dict:
    """Clear the review counters. This is what the last step of a review
    session commits: everything in this container is now seen."""
    stamp = now()
    if tag:
        row = store.one("SELECT * FROM tags WHERE name = ? COLLATE NOCASE",
                        (str(tag).strip().lstrip("#@"),))
        if row is None:
            raise GtdError(f"no context called {tag!r}")
        ids = [r["id"] for r in store.query(
            "SELECT i.id FROM item_tags it JOIN items i ON i.id = it.item_id "
            "WHERE it.tag_id = ? AND i.status NOT IN ('trashed', 'done', 'dropped', "
            "'archived')",
            (row["id"],))]
        for item_id in ids:
            store.run("UPDATE items SET reviewed_at = ?, updated_at = ? WHERE id = ?",
                      (stamp, stamp, item_id))
        store.run("UPDATE tags SET reviewed_at = ?, updated_at = ? WHERE id = ?",
                  (stamp, stamp, row["id"]))
        store.log(None, "review", f"context {row['name']}: {len(ids)}")
        return {"scope": f"#{row['name']}", "marked": len(ids)}

    if all_scopes:
        rows = store.query("SELECT id FROM items WHERE status NOT IN ('archived', 'trashed')")
        for row in rows:
            store.run("UPDATE items SET reviewed_at = ? WHERE id = ?", (stamp, row["id"]))
        store.run("UPDATE tags SET reviewed_at = ?", (stamp,))
        store.log(None, "review", "everything")
        return {"scope": "everything", "marked": len(rows)}

    if not ref:
        raise GtdError("mark needs a container id, --tag or --all")
    item = find(store, ref)
    ids = [r["id"] for r in store.query(
        "SELECT id FROM items WHERE status NOT IN ('trashed', 'done', 'dropped', "
        "'archived') AND (parent_id = ? OR id = ?)", (item["id"], item["id"]))]
    for item_id in ids:
        store.run("UPDATE items SET reviewed_at = ? WHERE id = ?", (stamp, item_id))
    if item["review"] != "never":
        store.run("UPDATE items SET reviewed_at = ? WHERE id = ?", (stamp, item["id"]))
    store.log(item["id"], "review", f"{len(ids)} items")
    return {"scope": item["title"], "marked": len(ids)}


# ---------------------------------------------------------------- views
#
# Fragments of the form "<FROM clause> <WHERE ...> <ORDER BY ...>", always
# written against the item_titles view so every row carries its container and
# area names, and always with a WHERE clause so list_view can add a filter.

VIEW_SQL = {
    "inbox": "FROM item_titles t WHERE t.status = 'inbox' AND t.kind = 'action' "
             "ORDER BY t.created_at",
    "clarify": "FROM item_titles t WHERE t.id IN (SELECT id FROM clarify_queue)",
    "focus": "FROM item_titles t WHERE t.id IN (SELECT id FROM next_actions) "
             "ORDER BY t.priority DESC, COALESCE(t.due_at, '9999'), t.rank, t.created_at",
    "ten": "FROM item_titles t WHERE t.id IN (SELECT id FROM focus_ten)",
    "today": "FROM item_titles t WHERE t.id IN (SELECT id FROM today)",
    "forecast": "FROM item_titles t WHERE t.id IN (SELECT id FROM forecast)",
    "overdue": "FROM item_titles t WHERE t.id IN (SELECT id FROM overdue)",
    "due_today": "FROM item_titles t WHERE t.id IN (SELECT id FROM due_today) "
                 "ORDER BY t.due_at, t.priority DESC",
    "today_open": "FROM item_titles t WHERE t.id IN (SELECT id FROM today_open) "
                  "ORDER BY COALESCE(t.due_at, '9999'), t.priority DESC, t.defer_at",
    "upcoming": "FROM item_titles t WHERE t.id IN (SELECT id FROM upcoming) "
                "ORDER BY t.due_at, t.priority DESC",
    "waiting": "FROM item_titles t WHERE t.id IN (SELECT id FROM waiting_for)",
    "someday": "FROM item_titles t WHERE t.id IN (SELECT id FROM someday)",
    "flagged": "FROM item_titles t WHERE t.id IN (SELECT id FROM flagged)",
    "reference": "FROM item_titles t WHERE t.status = 'reference' ORDER BY t.updated_at DESC",
    "onhold": "FROM item_titles t WHERE t.status = 'onhold' ORDER BY t.title COLLATE NOCASE",
    "trash": "FROM item_titles t WHERE t.status = 'trashed' ORDER BY t.updated_at DESC",
    "done": "FROM item_titles t WHERE t.status = 'done' ORDER BY t.completed_at DESC",
    "archived": "FROM item_titles t WHERE t.status = 'archived' "
                "ORDER BY t.archived_at DESC",
    "projects": "FROM item_titles t WHERE t.kind = 'project' "
                "AND t.status NOT IN ('archived', 'trashed') "
                "ORDER BY t.completed, COALESCE(t.parent_title, '~'), t.title COLLATE NOCASE",
    "areas": "FROM item_titles t WHERE t.kind = 'area' "
             "AND t.status NOT IN ('archived', 'trashed') "
             "ORDER BY t.completed, t.title COLLATE NOCASE",
    "stalled": "FROM item_titles t WHERE t.id IN (SELECT id FROM stalled_projects)",
    "empty": "FROM item_titles t WHERE t.id IN (SELECT id FROM empty_projects)",
    # Every open action, ignoring the step it happens to sit in. Combined with
    # `--tag`/`--project` this answers "what is filed under X?"
    # across the whole database rather than inside a single view.
    "tasks": "FROM item_titles t WHERE t.kind = 'action' "
             "AND t.status NOT IN ('done', 'dropped', 'archived', 'trashed') "
             "ORDER BY COALESCE(t.due_at, '9999'), t.priority DESC, "
             "t.title COLLATE NOCASE",
}

VIEW_ALIASES = {
    "clarify_queue": "clarify",
    "next_actions": "focus",
    "focus_ten": "ten",
    "waiting_for": "waiting",
    "stalled_projects": "stalled",
    "empty_projects": "empty",
}

VIEWS = tuple(sorted(VIEW_SQL)) + ("focus-ten", "log", "tags")


def _page_clause(limit: int | None, offset: int) -> tuple[str, tuple]:
    """The LIMIT/OFFSET tail for one page, with its bindings.

    SQLite needs a LIMIT before it will take an OFFSET, so an offset with no
    limit becomes the unbounded ``LIMIT -1``. An honest zero offset and no
    limit produce nothing at all, keeping the unpaged queries unpaged.
    """
    offset = int(offset or 0)
    if limit is None and not offset:
        return "", ()
    if limit is None:
        return " LIMIT -1 OFFSET ?", (offset,)
    if offset:
        return " LIMIT ? OFFSET ?", (int(limit), offset)
    return " LIMIT ?", (int(limit),)


def list_view(store: Store, view: str, *, limit: int | None = None, offset: int = 0,
              project=None, area=None, tag=None, reviewed: bool | None = None) -> list[dict]:
    name = str(view or "").strip().lower()

    if name == "log":
        clause, page_params = _page_clause(limit, offset)
        rows = store.query(
            "SELECT l.at, l.kind, l.detail, i.id AS item_id, i.title, i.status "
            "FROM log l LEFT JOIN items i ON i.id = l.item_id "
            "ORDER BY l.at DESC, l.id DESC" + clause, page_params)
        return [dict(r) for r in rows]

    if name == "tags":
        # Ordered by name so a context and its subcontexts sort together.
        # With auto-remove on, a context that would be pruned is not listed:
        # showing it for the few hours before it goes would only make the list
        # lie about what is really there.
        sql = "SELECT * FROM tag_load WHERE open_count > 0"
        filters, cutoffs = prune_where(store, alias="t")
        tag_params: tuple = ()
        if filters["contexts"]:
             # tag_load carries no timestamp of its own, so the sweep's own
             # conditions are checked against the tag the view is reading.
            sql += (" AND id IN (SELECT t.id FROM tags t WHERE "
                     + filters["contexts"] + ")")
            tag_params = (cutoffs["contexts"],)
        clause, page_params = _page_clause(limit, offset)
        return [dict(r) for r in store.query(
            sql + " ORDER BY name COLLATE NOCASE" + clause,
            tag_params + page_params)]


    if name == "focus-ten":
        name = "ten"
    # Accept the names the schema's views use too, so `gtd list next_actions`
    # and `gtd list focus` mean the same thing.
    name = VIEW_ALIASES.get(name, name)
    sql = VIEW_SQL.get(name)
    if sql is None:
        raise GtdError(f"unknown view {view!r}; try: {', '.join(sorted(VIEW_SQL))}, "
                       f"tags, log")

    params: list = []
    extra = []

    if tag:
        # Filtering by a context includes its subcontexts: "home" also finds
        # everything tagged "#home>kitchen", since names are full paths.
        clean = str(tag).strip().lstrip("#@")
        target = store.one("SELECT name FROM tags WHERE name = ? COLLATE NOCASE",
                           (clean,))
        if target is None:
            extra.append("t.id IN (SELECT it.item_id FROM item_tags it "
                         "JOIN tags g ON g.id = it.tag_id "
                         "WHERE g.name = ? COLLATE NOCASE)")
            params.append(clean)
        else:
            prefix = target["name"] + ">"
            extra.append("t.id IN (SELECT it.item_id FROM item_tags it "
                         "JOIN tags g ON g.id = it.tag_id "
                         "WHERE g.name = ? COLLATE NOCASE "
                         "OR lower(substr(g.name, 1, ?)) = lower(?))")
            params.extend([target["name"], len(prefix), prefix])
    elif project:
        target = lookup_container(store, project, PROJECT) or lookup_container(store, project, AREA)
        if target is None:
            raise GtdError(f"no project or area called {project!r}")
        extra.append("(t.parent_id = ? OR t.id = ?)")
        params.extend([target["id"], target["id"]])
    elif area:
        target = lookup_container(store, area, AREA)
        if target is None:
            raise GtdError(f"no area called {area!r}")
        extra.append("t.parent_id = ?")
        params.append(target["id"])


    if reviewed is True:
        extra.append("t.reviewed_at IS NOT NULL")
    elif reviewed is False:
        extra.append("t.reviewed_at IS NULL")

    if name == "projects":
        # Keep the project picker focused on work that still has an open
        # action. Descendant projects count too, so an empty parent with an
        # active subproject remains visible.
        extra.append("t.id IN (WITH RECURSIVE descendants(root_id, node_id) AS ("
                     "SELECT id, id FROM items WHERE kind = 'project' "
                     "UNION ALL SELECT d.root_id, c.id FROM descendants d "
                     "JOIN items c ON c.parent_id = d.node_id "
                     "WHERE c.kind = 'project') "
                     "SELECT d.root_id FROM descendants d JOIN items a "
                     "ON a.parent_id = d.node_id WHERE a.kind = 'action' "
                     "AND a.status NOT IN ('done', 'dropped', 'archived', 'trashed'))")
        filters, cutoffs = prune_where(store)
        if filters["projects"]:
            extra.append(filters["projects"])
            params.append(cutoffs["projects"])

    if extra:
        sql = sql.replace("WHERE ", "WHERE " + " AND ".join(extra) + " AND ", 1)

    # The limit goes to the database, not to a slice after the fact: enriching
    # every row of a ten-thousand-row view and then dropping all but the first
    # page is the whole cost with none of the use. Only the page that is
    # returned is enriched, in a batch.
    page_sql = "SELECT t.* " + sql
    clause, page_params = _page_clause(limit, offset)
    rows = [as_dict(r) for r in store.query(page_sql + clause, params + list(page_params))]
    if rows:
        ids = [row["id"] for row in rows]
        tag_map = repository.tags_for(store, ids)
        path_map = repository.paths_for(store, ids)
        for item in rows:
            item["tags"] = tag_map.get(item["id"], [])
            item["parent_path"] = path_map.get(item["id"], "")
    return rows


def search(store: Store, query: str, *, limit: int | None = 50, offset: int = 0) -> list[dict]:
    """Find everything matching a free-text query.

    Searches task titles and notes, the names of the projects and areas they
    sit in, their contexts, the person an item is waiting on,
    and any attachment paths. One query over the item_titles view, so a match
    on a project or context still returns the action that carries it.

    The match is a substring LIKE on purpose, not an FTS5 token match: typing
    "e" is meant to find "Renovate", which a token index would not. That means
    this scans item_titles, but it is bounded to the page and measured in tens
    of milliseconds at ten thousand items, so the index is not worth the
    behavior change here.
    """
    text = str(query or "").strip()
    if not text:
        return []
    like = f"%{text}%"
    clause, page_params = _page_clause(limit, offset)
    rows = store.query(
        "SELECT DISTINCT t.* FROM item_titles t "
        "LEFT JOIN item_tags it ON it.item_id = t.id "
        "LEFT JOIN tags g ON g.id = it.tag_id "
        "LEFT JOIN items p ON p.id = t.parent_id "
        "LEFT JOIN attachments a ON a.item_id = t.id "
        "WHERE t.title LIKE ? COLLATE NOCASE "
        "   OR t.notes LIKE ? COLLATE NOCASE "
        "   OR t.parent_title LIKE ? COLLATE NOCASE "
        "   OR t.area_title LIKE ? COLLATE NOCASE "
        "   OR t.url LIKE ? COLLATE NOCASE "
        "   OR g.name LIKE ? COLLATE NOCASE "
        "   OR a.uri LIKE ? COLLATE NOCASE "
        # Archived items come after everything still to be done, since that is
        # the only reason they turn up here at all; among themselves the most
        # recently archived is the one you are most likely looking for.
        "ORDER BY CASE WHEN t.status IN ('done', 'archived') THEN 1 ELSE 0 END, "
        "   CASE t.status "
        "   WHEN 'inbox' THEN 0 WHEN 'active' THEN 1 WHEN 'waiting' THEN 2 "
        "   WHEN 'done' THEN 4 WHEN 'archived' THEN 5 ELSE 3 END, "
        "   t.priority DESC, COALESCE(t.due_at, '9999') ASC, "
        "   CASE WHEN t.status = 'done' THEN t.completed_at "
        "        WHEN t.status = 'archived' THEN t.archived_at END DESC, "
        "   t.title COLLATE NOCASE"
        + clause,
        (like, like, like, like, like, like, like) + page_params)
    out = [as_dict(r) for r in rows]
    if out:
        tag_map = repository.tags_for(store, [item["id"] for item in out])
        for item in out:
            item["tags"] = tag_map.get(item["id"], [])
    return out


SETTING_DEFAULTS = {
      "due_time": "18:00",
      "defer_time": "09:00",
      "time_format": "24h",
      # How many days an empty project or context is kept before it is deleted.
      # "0" keeps it forever; any positive number removes it once it has sat with
      # nothing filed under it for that many days. An empty project is often one
      # you have just started, so the default is generous.
      "auto_remove_empty_projects": "7",
      "auto_remove_empty_contexts": "7",
      # How many days a completed task is kept before it is swept away. "0"
      # means keep it forever; any positive number keeps it that many days.
      "done_retention_days": "30",
      # How many days an archived task is kept before it is swept away, counted
      # from the day it was archived. The default 0 keeps archived tasks until
      # you decide otherwise, since the archive is a record you chose to keep.
      "archive_retention_days": "0",
      # Whether the calendar marks the day a task was completed with a green
      # symbol. Off by default: done work is not on the backlog.
      "calendar_show_completed": "off",
      # Which palette the fullscreen app paints itself in. "omarchy" follows
      # the active desktop theme; "of-light" / "of-dark" pin the app to an
      # OmniFocus-inspired palette of its own.
      "theme": "omarchy",
      # The narrowest the full-screen window gets before it drops to a message
      # asking to enlarge it. Below this the columns do not fit, so the app
      # says so rather than drawing a cramped layout.
      "fullscreen_width_limit": "768",
      }

# Every spelling people actually type for a switch, folded to the two that are
# stored.
ON_WORDS = ("on", "true", "yes", "1")
OFF_WORDS = ("off", "false", "no", "0")


def flag(value) -> bool:
    """Whether a stored on/off setting is on."""
    return str(value or "").strip().lower() in ON_WORDS


def settings(store: Store) -> dict:
    """The user-tunable knobs, as stored in the meta table."""
    return {key: store.meta(key, default) for key, default in SETTING_DEFAULTS.items()}


def _done_retention_days(store: Store) -> int:
    """The completed-task retention in days; 0 keeps them forever."""
    raw = settings(store).get("done_retention_days", "30")
    try:
        days = int(str(raw).strip())
    except (TypeError, ValueError):
        days = 30
    return days if days > 0 else 0


def _archive_retention_days(store: Store) -> int:
    """The archived-task retention in days; 0 keeps them forever."""
    raw = settings(store).get("archive_retention_days", "0")
    try:
        days = int(str(raw).strip())
    except (TypeError, ValueError):
        days = 0
    return days if days > 0 else 0


def _empty_retention_days(store: Store, key: str) -> int:
    """How many days an empty container is kept; 0 keeps it forever.

    The `auto_remove_empty_*` settings hold a day count in place of the old
    on/off switch: 0 means keep it, a positive number means remove it once it
    has sat empty that long. An unparseable value falls back to the default
    so a mistype can never delete on a zero window.
    """
    default = SETTING_DEFAULTS.get(key, "7")
    raw = settings(store).get(key, default)
    try:
        days = int(str(raw).strip())
    except (TypeError, ValueError):
        days = int(default)
    return days if days > 0 else 0


def set_setting(store: Store, key: str, value: str) -> str:
    """Validate and store one setting, returning the normalized value."""
    if key in ("due_time", "defer_time"):
        match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(value).strip())
        if not match:
            raise GtdError(f"{key} must be a 24h clock like 18:00, not {value!r}")
        value = f"{int(match.group(1)):02d}:{match.group(2)}"
    elif key == "time_format":
        value = str(value).strip().lower()
        if value not in ("12h", "24h"):
            raise GtdError(f"time_format must be 12h or 24h, not {value!r}")
    elif key == "theme":
        value = str(value).strip().lower()
        if value not in ("omarchy", "of-light", "of-dark"):
            raise GtdError(f"theme must be omarchy, of-light or of-dark, not {value!r}")
    elif key in ("auto_remove_empty_projects", "auto_remove_empty_contexts",
                  "done_retention_days", "archive_retention_days"):
          # A day count, not an on/off switch: 0 keeps the container forever, a
          # positive number is how many days it is kept before the sweeper may
          # take it. Anything that is not a whole number of days is a mistake.
        raw = str(value).strip()
        if raw.isdigit():
            value = str(int(raw))
        else:
            raise GtdError(
                f"{key} must be a whole number of days, 0 for forever, "
                f"not {value!r}")
    elif key == "calendar_show_completed":
        value = str(value).strip().lower()
        if value in ON_WORDS:
            value = "on"
        elif value in OFF_WORDS:
            value = "off"
        else:
            raise GtdError(f"{key} must be on or off, not {value!r}")
    elif key == "fullscreen_width_limit":
        raw = str(value).strip()
        if raw.isdigit() and int(raw) > 0:
            value = str(int(raw))
        else:
            raise GtdError(
                f"fullscreen_width_limit must be a positive whole number of "
                f"pixels, not {value!r}")
    else:
        raise GtdError(f"unknown setting: {key!r}")
    store.set_meta(key, value)
    return value


def stats(store: Store) -> dict:
    """Every number the bar pill and the panel header are made of.

    One aggregate over ``items`` under the repository layer, with the same
    conditions the schema's views carry, so the polling bar does not reopen
    twenty views a minute.
    """
    return repository.counts(store)


def bar_snapshot(store: Store) -> dict:
    """The one payload the bar widget polls: small, cheap, a handful of counts
    over indexed views, and the title of the one action the panel would open
    on."""
    data = stats(store)
    return {
        "inbox": data["inbox"],
        "next": data["next"],
        "today": data["today"],
        "due_today": data["due_today"],
        "due_today_late": data["due_today_late"],
        "overdue": data["overdue"],
        "upcoming": data["upcoming"],
        "waiting": data["waiting"],
        "archived": data["archived"],
        "flagged": data["flagged"],
        "flagged_today": data.get("flagged_today", 0),
        "projects_open": data["projects_open"],
        "due_time": settings(store)["due_time"],
        "defer_time": settings(store)["defer_time"],
        "time_format": settings(store)["time_format"],
        "next_title": store.scalar("SELECT title FROM focus_ten LIMIT 1"),
    }


# A project is worth drawing only while open work still hangs somewhere below
# it, however deep. Both the tree and the scoped subtree filter on this.
_PROJECT_HAS_OPEN_WORK = (
    "id IN (WITH RECURSIVE descendants(root_id, node_id) AS ("
    "SELECT id, id FROM items WHERE kind = 'project' "
    "UNION ALL SELECT d.root_id, c.id FROM descendants d "
    "JOIN items c ON c.parent_id = d.node_id WHERE c.kind = 'project') "
    "SELECT d.root_id FROM descendants d JOIN items a "
    "ON a.parent_id = d.node_id WHERE a.kind = 'action' "
    "AND a.status NOT IN ('done', 'dropped', 'archived', 'trashed'))")


def _container_counts(store: Store) -> dict[str, tuple[int, int]]:
    """Direct open actions per container, as (total, late).

    Late is the same test the view uses: a deadline that has passed while the
    task is still open.
    """
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    counts: dict[str, tuple[int, int]] = {}
    for row in store.query(
        "SELECT parent_id, COUNT(*) AS total, "
        "SUM(CASE WHEN due_at IS NOT NULL AND substr(due_at, 1, 16) < ? "
        "THEN 1 ELSE 0 END) AS late "
        "FROM open_items WHERE kind = 'action' AND parent_id IS NOT NULL "
        "GROUP BY parent_id", (now,)):
        counts[row["parent_id"]] = (row["total"], row["late"] or 0)
    return counts


def _nest_projects(projects: list[dict],
                   counts: dict[str, tuple[int, int]]) -> list[dict]:
    """Nest a flat list of projects and roll (total, late) up each branch.

    Every node gains `projects`, `action_count` (the direct open actions under
    it), and the subtree-wide `total`/`late`. Projects whose parent is not in
    the set are returned as the tops of their branches.
    """
    by_id = {project["id"]: project for project in projects}
    for project in projects:
        project["projects"] = []
        total, late = counts.get(project["id"], (0, 0))
        project["action_count"] = total
        project["_total"], project["_late"] = total, late

    tops: list[dict] = []
    for project in projects:
        parent = by_id.get(project.get("parent_id") or "")
        if parent is not None:
            parent["projects"].append(project)
        else:
            tops.append(project)

    def roll(node: dict) -> tuple[int, int]:
        total, late = node["_total"], node["_late"]
        node["projects"].sort(key=lambda c: (c.get("title") or "").lower())
        for child in node["projects"]:
            child_total, child_late = roll(child)
            total += child_total
            late += child_late
        node["total"], node["late"] = total, late
        del node["_total"], node["_late"]
        return total, late

    tops.sort(key=lambda c: (c.get("title") or "").lower())
    for top in tops:
        roll(top)
    return tops


def containers(store: Store) -> dict:
    """Areas and the projects inside them, with counts but no action rows.

    The same tree as `tree` without the weight: the left column can draw every
    area and project, and pull one container's actions only when it is opened.
    Each project carries `action_count` (its direct open actions) and the
    subtree-wide `total`/`late`; the unnamed bucket is reported as
    `loose_count`.
    """
    areas = [as_dict(r) for r in store.query(
          "SELECT * FROM items WHERE kind = 'area' AND status = 'active' "
          "ORDER BY title COLLATE NOCASE")]
    project_filters, project_cutoffs = prune_where(store, alias="p")
    project_sql = ("SELECT * FROM items p WHERE p.kind = 'project' "
                   "AND p.status != 'done' AND p." + _PROJECT_HAS_OPEN_WORK)
    if project_filters["projects"]:
        project_sql += " AND " + project_filters["projects"]
        project_params: tuple = (project_cutoffs["projects"],)
    else:
        project_params = ()
    projects = [as_dict(r) for r in store.query(
        project_sql + " ORDER BY title COLLATE NOCASE", project_params)]
    counts = _container_counts(store)

    tag_map = repository.tags_for(store, [i["id"] for i in areas + projects])
    for item in areas + projects:
        item["tags"] = tag_map.get(item["id"], [])

    tops = _nest_projects(projects, counts)

    area_by_id = {area["id"]: area for area in areas}
    for area in areas:
        area["projects"] = []

    # Projects nest inside projects as deep as you like; only the top of a
    # branch sits in an area (or in the unnamed bucket). Recursion happens in
    # the view, which walks `project["projects"]`.
    unnamed = {"id": "", "title": "", "kind": "area", "status": "active",
              "tags": [], "projects": [], "parent_id": None}
    for project in tops:
        parent_id = project.get("parent_id") or ""
        (area_by_id.get(parent_id) or unnamed)["projects"].append(project)

    groups = list(area_by_id.values()) + [unnamed]
    for group in groups:
        group["projects"].sort(key=lambda c: (c.get("title") or "").lower())
        group["total"] = sum(p["total"] for p in group["projects"])
        group["late"] = sum(p["late"] for p in group["projects"])

    # Open actions with nowhere to sit; the view fetches their rows on demand.
    loose_count = store.scalar(
        "SELECT COUNT(*) FROM open_items WHERE kind = 'action' "
        "AND status != 'inbox' AND (parent_id IS NULL OR parent_id NOT IN "
        "(SELECT id FROM items WHERE kind = 'project'))", default=0)

    return {
        "areas": [a for a in groups if a["id"] or a["projects"] or a["tags"]],
        "loose_count": loose_count,
    }


def subtree(store: Store, container_id: str) -> dict:
    """One container's branch, with action rows, for a lazily-opened column.

    The node as it sits inside `tree` — nested projects and the open actions
    filed under each — but scoped to a single area or project, so focusing a
    container costs only its own branch rather than every open action.
    """
    row = store.one("SELECT * FROM items WHERE id = ?", (container_id,))
    if row is None or row["kind"] not in ("area", "project"):
        raise GtdError(f"no container with id {container_id!r}")
    node = as_dict(row)
    counts = _container_counts(store)

    if node["kind"] == "area":
        descendants = [as_dict(r) for r in store.query(
            "WITH RECURSIVE down(id) AS ("
            "SELECT id FROM items WHERE parent_id = ? AND kind = 'project' "
            "AND status != 'done' "
            "UNION ALL SELECT c.id FROM down d JOIN items c "
            "ON c.parent_id = d.id WHERE c.kind = 'project' "
            "AND c.status != 'done') "
            "SELECT * FROM items WHERE id IN (SELECT id FROM down) "
            "AND " + _PROJECT_HAS_OPEN_WORK, (container_id,))]
        tops = _nest_projects(descendants, counts)
        tag_map = repository.tags_for(store, [p["id"] for p in descendants])
        for project in descendants:
            project["tags"] = tag_map.get(project["id"], [])
        node["projects"] = tops
        node["actions"] = []
        node["total"] = sum(p["total"] for p in tops)
        node["late"] = sum(p["late"] for p in tops)
        return node

    projects = [as_dict(r) for r in store.query(
        "WITH RECURSIVE down(id) AS ("
        "SELECT id FROM items WHERE id = ? "
        "UNION ALL SELECT c.id FROM down d JOIN items c "
        "ON c.parent_id = d.id WHERE c.kind = 'project' AND c.status != 'done') "
        "SELECT * FROM items WHERE id IN (SELECT id FROM down) "
        "AND kind = 'project' AND status != 'done' AND " + _PROJECT_HAS_OPEN_WORK,
        (container_id,))]
    tops = _nest_projects(projects, counts)

    project_by_id: dict[str, dict] = {}

    def index(project: dict) -> None:
        project.setdefault("actions", [])
        project_by_id[project["id"]] = project
        for child in project["projects"]:
            index(child)

    for top in tops:
        index(top)
    me = project_by_id.get(node["id"], node)

    if project_by_id:
        marks = ",".join("?" * len(project_by_id))
        actions = [as_dict(r) for r in store.query(
            "SELECT * FROM open_items WHERE kind = 'action' "
            f"AND parent_id IN ({marks}) ORDER BY priority DESC, "
            "COALESCE(due_at, '9999') ASC, created_at ASC",
            tuple(project_by_id))]
    else:
        actions = []
    action_tags = repository.tags_for(store, [a["id"] for a in actions])
    for action in actions:
        action["tags"] = action_tags.get(action["id"], [])
        parent = project_by_id.get(action.get("parent_id") or "")
        if parent is not None:
            parent["actions"].append(action)

    tag_map = repository.tags_for(store, [p["id"] for p in projects])
    for project in projects:
        project["tags"] = tag_map.get(project["id"], [])
    me["tags"] = tag_map.get(me["id"], [])
    return me



def tree(store: Store) -> dict:
    """Areas, the projects inside them, and each project's actions.

    The fullscreen view's left column: the shape of everything that was given
    a destination, with the open work hanging off it. One payload, because a
    panel that has to ask for each project's children on selection flickers.
    `containers` is the same tree without the actions, for a panel that is
    happy to ask per container instead.
    """
    data = containers(store)
    project_by_id: dict[str, dict] = {}

    def index(project: dict) -> None:
        project.setdefault("actions", [])
        project_by_id[project["id"]] = project
        for child in project.get("projects", []):
            index(child)

    for area in data["areas"]:
        for project in area["projects"]:
            index(project)

    actions = [as_dict(r) for r in store.query(
          "SELECT * FROM open_items WHERE kind = 'action' ORDER BY "
          "COALESCE(parent_id, id) COLLATE NOCASE, priority DESC, "
          "COALESCE(due_at, '9999') ASC, created_at ASC")]
    action_tags = repository.tags_for(store, [action["id"] for action in actions])
    for action in actions:
        action["tags"] = action_tags.get(action["id"], [])
    loose: list[dict] = []
    for action in actions:
        parent = project_by_id.get(action.get("parent_id") or "")
        if parent is not None:
            parent["actions"].append(action)
        elif action.get("status") != "inbox":
            # Inbox tasks have no project by definition, but the project view
            # is about assigned work, so they are not repeated here.
            loose.append(action)

    tag_sql = "SELECT * FROM tag_load WHERE open_count > 0"
    tag_filters, tag_cutoffs = prune_where(store, alias="t")
    if tag_filters["contexts"]:
        tag_sql += (" AND id IN (SELECT t.id FROM tags t WHERE "
                    + tag_filters["contexts"] + ")")
        tag_params: tuple = (tag_cutoffs["contexts"],)
    else:
        tag_params = ()
    tags = [as_dict(r) for r in store.query(
        tag_sql + " ORDER BY open_count DESC, name COLLATE NOCASE", tag_params)]

    return {
        "areas": data["areas"],
        "loose": loose,
        "tags": tags,
        "counts": stats(store),
    }


# -------------------------------------------------------------- calendar


def _day_key(value: str | None) -> str | None:
    return value[:10] if value else None


def calendar(store: Store, *, start=None, end=None, all_due: bool = False) -> dict:
    """A day-by-day diary between two dates, inclusive.

    Two kinds of commitment share one cell: deadlines (`due`) and defer dates
    (`scheduled`). A defer date is not a single appointment: it opens the span
    a task is live for, so a task deferred on Monday and due Friday is
    scheduled on Monday, Tuesday, Wednesday and Thursday, and due on Friday.
    Today also carries whatever is already `overdue`, and what you finished
    today (`completed`) — earlier days keep only the two kinds of commitment,
    since completed work is archived and reachable through search.
    This is the data behind both the panel's calendar page and `gtd agenda`.

    With ``all_due`` the window is stretched to cover every open deadline, so
    nothing with a due date is left off the calendar, however far out.
    """
    start = (str(start) if start else today())[:10]
    end = (str(end) if end else start)[:10]
    first = _dt.date.fromisoformat(start)
    last = _dt.date.fromisoformat(end)
    if last < first:
        first, last = last, first

    if all_due:
        states = "('inbox', 'active', 'waiting', 'onhold', 'someday', 'reference')"
        bounds = store.one(
            f"SELECT MIN(substr(due_at, 1, 10)) AS lo, "
            f"MAX(substr(due_at, 1, 10)) AS hi FROM items "
            f"WHERE kind = 'action' AND status IN {states} AND due_at IS NOT NULL")
        if bounds and bounds["lo"]:
            first = min(first, _dt.date.fromisoformat(bounds["lo"]))
        if bounds and bounds["hi"]:
            last = max(last, _dt.date.fromisoformat(bounds["hi"]))

    days: dict[str, dict] = {}
    cursor = first
    while cursor <= last:
        key = cursor.isoformat()
        days[key] = {
            "date": key,
            "weekday": cursor.strftime("%a"),
            "overdue": [],
            "due": [],
            "scheduled": [],
            "completed": [],
        }
        cursor += _dt.timedelta(days=1)

    open_states = "('inbox', 'active', 'waiting', 'onhold', 'someday', 'reference')"

    for row in store.query(
            f"SELECT * FROM item_titles t WHERE t.kind = 'action' "
            f"AND t.status IN {open_states} AND t.due_at IS NOT NULL "
            f"ORDER BY t.due_at, t.priority DESC"):
        cell = days.get(_day_key(row["due_at"]))
        if cell is not None:
            cell["due"].append(as_dict(row))

    for row in store.query(
            f"SELECT * FROM item_titles t WHERE t.kind = 'action' "
            f"AND t.status IN {open_states} AND t.defer_at IS NOT NULL "
            f"ORDER BY t.defer_at, t.priority DESC"):
        item = as_dict(row)
        defer_day = _dt.date.fromisoformat(_day_key(row["defer_at"]))
        due_day = (_dt.date.fromisoformat(_day_key(row["due_at"]))
                   if row["due_at"] else None)
        # A defer date opens the span and the deadline closes it: the task is
        # scheduled on every day from its defer date up to, but not including,
        # its deadline day — that day already lists it under `due`. No deadline,
        # or a legacy row whose deadline fell before its defer date, is
        # scheduled on the defer date alone.
        if due_day is not None and due_day > defer_day:
            span_first, span_last = defer_day, due_day - _dt.timedelta(days=1)
        else:
            span_first, span_last = defer_day, defer_day
        day = span_first if span_first > first else first
        while day <= span_last and day <= last:
            days[day.isoformat()]["scheduled"].append(item)
            day += _dt.timedelta(days=1)

    # Completed tasks are archived: the workflow views leave them out and search
    # is where you go looking for one. The calendar keeps only today's, so the
    # day you are looking at still says what got finished, without every past
    # day on the month grid going on listing work that is already behind you.
    if today() in days:
        for row in store.query(
                "SELECT * FROM item_titles t WHERE t.status = 'done' "
                "AND t.completed_at IS NOT NULL ORDER BY t.completed_at"):
            cell = days.get(_day_key(row["completed_at"]))
            if cell is not None:
                cell["completed"].append(as_dict(row))

    # Overdue only makes sense on today's cell, and only if today is in range.
    # When the window already covers every due day, a past deadline sits under
    # its own date, so the today bucket would only repeat it.
    here = today()
    if here in days and not all_due:
        for row in store.query(
                "SELECT * FROM item_titles t WHERE t.kind = 'action' "
                f"AND t.status IN {open_states} AND t.due_at IS NOT NULL "
                "AND t.due_at < ? ORDER BY t.due_at",
                (now(),)):
            days[here]["overdue"].append(as_dict(row))

    ordered = [days[key] for key in sorted(days)]
    for cell in ordered:
        cell["count"] = sum(len(cell[k]) for k in
                            ("overdue", "due", "scheduled", "completed"))
    totals = {
        "due": sum(len(c["due"]) for c in ordered),
        "scheduled": sum(len(c["scheduled"]) for c in ordered),
        "completed": sum(len(c["completed"]) for c in ordered),
        "overdue": sum(len(c["overdue"]) for c in ordered),
    }
    return {"start": ordered[0]["date"] if ordered else start,
            "end": ordered[-1]["date"] if ordered else end,
            "days": ordered, "totals": totals}


def agenda(store: Store, *, days: int = 7, start=None, all_due: bool = False) -> list[dict]:
    """Just the cells of :func:`calendar` that have something in them."""
    start = (str(start) if start else today())[:10]
    first = _dt.date.fromisoformat(start)
    last = first + _dt.timedelta(days=max(1, int(days)) - 1)
    data = calendar(store, start=first.isoformat(), end=last.isoformat(),
                    all_due=all_due)
    return [cell for cell in data["days"] if cell["count"]]


def due_notifications(store: Store) -> list[dict]:
    """Open actions whose deadline has passed and that have not been announced.

    The engine half of `gtd notify`. ``notified_at`` is stamped by
    :func:`mark_notified` so a deadline fires once; moving it later and letting
    it pass again announces it again.
    """
    rows = store.query(
        "SELECT t.* FROM item_titles t WHERE t.kind = 'action' "
        "AND t.status IN ('inbox', 'active', 'waiting', 'onhold', 'someday', 'reference') "
        "AND t.due_at IS NOT NULL AND t.due_at <= ? "
        "AND (t.notified_at IS NULL OR t.notified_at < t.due_at) "
        "ORDER BY t.due_at, t.priority DESC",
        (now(),))
    return [as_dict(row) for row in rows]


def mark_notified(store: Store, ids) -> int:
    """Remember that these deadlines have been announced."""
    ids = list(ids)
    if not ids:
        return 0
    stamp = now()
    for item_id in ids:
        store.run("UPDATE items SET notified_at = ? WHERE id = ?", (stamp, item_id))
    store.log(None, "notify", f"{len(ids)} deadline(s) reached")
    return len(ids)
