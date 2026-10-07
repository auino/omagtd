"""Getting data in and out.

In, in decreasing order of fidelity:

``ofxml``      an OmniFocus backup / export (a plist). Folders, projects,
               areas, contexts, notes, flags, defer and due dates,
               waiting-for, completion, repetition and review dates survive.
``ofocusmx``   OmniFocus's line-oriented text export. Same ideas, less
               structure — parsed leniently, so unknown keys are ignored
               rather than fatal.
``csv``        one row per item, container names in columns. Written by hand
               or exported from a spreadsheet.
``json``       this system's own export format; importing it is a lossless
               round trip, which makes backups boring.

Out: the same formats, so a database can be read by something else.
"""

from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import io
import json
import os
import plistlib
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

from . import dates
from .core import GtdError, ensure_container, ensure_tag

# OmniFocus folder location -> our status.
LOCATION_STATUS = {
    "inbox": "inbox",
    "active": "active",
    "inprogress": "active",
    "projects": "active",
    "deferred": "active",
    "future": "active",
    "available": "active",
    "waiting": "waiting",
    "waitingimminent": "waiting",
    "someday": "someday",
    "reference": "reference",
    "completed": "done",
    "dropped": "dropped",
}

STATE_STATUS = {
    "active": "active",
    "inprogress": "active",
    "available": "active",
    "deferred": "active",
    "future": "active",
    "waiting": "waiting",
    "waitingimminent": "waiting",
    "someday": "someday",
    "inbox": "inbox",
    "completed": "done",
    "dropped": "dropped",
    # Our own states, which OmniFocus does not have but our export writes.
    "done": "done",
    "trashed": "trashed",
    "onhold": "onhold",
    "reference": "reference",
}

UNIT_MAP = {
    "day": "day", "days": "day",
    "week": "week", "weeks": "week", "biweek": "week", "fortnight": "week",
    "month": "month", "months": "month",
    "quarter": "month", "quarters": "month",
    "year": "year", "years": "year", "annually": "year", "yearly": "year",
}

CSV_COLUMNS = [
    "id", "kind", "title", "status", "project", "area", "tags",
    "priority", "defer", "due", "waiting_for", "notes", "url", "repeat",
    "review", "reviewed", "completed",
]


# ------------------------------------------------------------- utilities


def _stamp(value) -> str | None:
    """Normalize anything date-shaped — plist date, ISO string, "friday"."""
    if value in (None, "", "0000-00-00 00:00:00"):
        return None
    if isinstance(value, _dt.datetime):
        # OmniFocus writes plist dates with a timezone offset; we store local
        # wall time, which is the time the user actually meant.
        return value.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M")
    if isinstance(value, _dt.date):
        return dates.to_stamp(value)
    text = str(value).strip()
    # .ofocusmx writes "2026-10-05 09:00:00 +0000", which no ISO parser in the
    # standard library takes verbatim.
    offset = re.search(r"\s([+-]\d{2}):?(\d{2})$", text)
    if offset:
        text = text[:offset.start()] + offset.group(1) + ":" + offset.group(2)
        try:
            aware = _dt.datetime.fromisoformat(text)
        except ValueError:
            return None
        return aware.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M")
    parsed = dates.parse_when(text)
    return parsed.strftime("%Y-%m-%d %H:%M") if parsed else None


ORIGIN_SUFFIX_RE = re.compile(r"\s+(?:after|from)\s+\w+\s*$", re.IGNORECASE)


def _repeat_from_plist(rule) -> str | None:
    """OmniFocus repetition: {Mode, Interval, Unit, AnchorDate}."""
    if isinstance(rule, str):
        try:
            rule = json.loads(rule)
        except (TypeError, ValueError):
            return None
    if not isinstance(rule, dict):
        return None
    unit = UNIT_MAP.get(str(rule.get("Unit") or rule.get("unit") or "").lower())
    if unit is None:
        return None
    try:
        interval = int(rule.get("Interval") or rule.get("interval") or 1)
    except (TypeError, ValueError):
        interval = 1
    if interval <= 0:
        return None
    mode = str(rule.get("Mode") or rule.get("mode") or "").lower()
    origin = "complete" if ("completion" in mode or "complete" in mode) else "schedule"
    return json.dumps({"interval": interval, "unit": unit, "from": origin})


def _classify(entry: dict) -> str:
    raw = str(entry.get("Type") or entry.get("type") or "").lower()
    if "action" in raw or "task" in raw:
        return "action"
    if "project" in raw:
        return "project"
    if "area" in raw:
        return "area"
    if "context" in raw or "tag" in raw:
        return "context"
    if "Contents" in entry or "contents" in entry:
        return "folder"
    return "action"


# Keys whose values point *at* another record instead of being one. They still
# get read off the record they belong to; walking into them would turn every
# project reference and every tag into a project or an action of its own.
REF_KEYS = ("Tags", "Contexts", "Parents", "Parent", "Project", "Area",
            "Container", "Folder")


def _walk(node, out: list) -> list:
    """Collect every dict in the plist that looks like a record."""
    if isinstance(node, list):
        for child in node:
            _walk(child, out)
        return out
    if not isinstance(node, dict):
        return out
    if isinstance(node.get("Title"), str):
        out.append(node)
    for key, value in node.items():
        if key in REF_KEYS:
            continue
        if isinstance(value, (dict, list)):
            _walk(value, out)
    return out


def _ref_name(entry: dict, kind: str) -> str | None:
    """Find the name of this record's parent of the given kind.

    OmniFocus nests parent references under different keys across export
    versions (``Parents``, ``Project``, ``Area``, ``Container``), so this walks
    the record breadth-first and stops at the first title of the right type.

    An empty ``kind`` means "any parent, whatever it is called" — used where
    the export gives only a name. .ofocusmx hands areas and projects the same
    key, and only the database can tell them apart.
    """
    queue = [entry]
    seen = 0
    while queue and seen < 64:
        node = queue.pop(0)
        seen += 1
        if not isinstance(node, dict):
            continue
        title = node.get("Title")
        node_kind = str(node.get("Type") or "").lower()
        if isinstance(title, str) and title.strip():
            # A bare kind would match every node, because "" is in everything.
            if kind:
                if kind in node_kind:
                    return title
            elif node_kind in ("", "container", "folder"):
                return title
        for key in ("Parents", "Parent", "Project", "Area", "Container", "Folder"):
            value = node.get(key)
            if isinstance(value, list):
                queue.extend(child for child in value if isinstance(child, dict))
            elif isinstance(value, dict):
                queue.append(value)
    return None


def _split_container(store, entry: dict) -> tuple[str | None, str | None]:
    """Work out this record's project and area.

    A typed reference is believed outright. An untyped one — a bare parent name
    from .ofocusmx — is looked up, so a project inside an area keeps its area
    and a project without one stays homeless rather than being filed under a
    same-named project.
    """
    from . import core

    project = _ref_name(entry, "project")
    area = _ref_name(entry, "area")
    if project or area:
        return project, area

    name = _ref_name(entry, "")
    if not name or name == str(entry.get("Title") or ""):
        return None, None
    if lookup := core.lookup_container(store, name, core.AREA):
        return None, lookup["title"]
    if lookup := core.lookup_container(store, name, core.PROJECT):
        return lookup["title"], None
    return None, None


def _record_tags(entry: dict) -> list[str]:
    raw = entry.get("Tags") or entry.get("tags") or []
    if isinstance(raw, str):
        raw = raw.split(",")
    names: list[str] = []
    if isinstance(raw, list):
        for tag in raw:
            name = tag.get("Title") or tag.get("title") if isinstance(tag, dict) else tag
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
    return names


def _state_of(entry: dict) -> str:
    for key in ("LocationInFolder", "locationInFolder", "FolderLocation"):
        if entry.get(key):
            return LOCATION_STATUS.get(str(entry[key]).strip().lower(), "active")
    for key in ("State", "state", "Status"):
        if entry.get(key):
            return STATE_STATUS.get(str(entry[key]).strip().lower(), "active")
    return "active"


def _review_interval(entry: dict) -> str:
    raw = entry.get("ReviewInterval") or entry.get("reviewInterval")
    if raw is None:
        return "never"
    text = str(raw).strip().lower()
    for value, name in (("1", "daily"), ("2", "weekly"), ("3", "monthly"),
                        ("4", "quarterly"), ("5", "yearly")):
        if text in (value, name):
            return name
    return "never"


def _blank_result(source: str) -> dict:
    return {"source": source, "actions": 0, "projects": 0, "areas": 0, "updated": 0,
            "tags": 0, "attachments": 0, "skipped": 0, "remapped": 0, "warnings": []}


# ------------------------------------------------------------ OmniFocus


def import_omnifocus(store, payload, *, dry_run=False, progress=False,
                     source: str = "omnifocus") -> dict:
    """Import an OmniFocus payload: a parsed plist, or .ofocusmx records."""
    from . import core  # imported here: core does not need the importer

    result = _blank_result(source)
    records = payload if isinstance(payload, list) else _walk(payload, [])

    # Containers first: an action can reference a project that appears later in
    # the file, and OmniFocus does not promise an order.
    containers = [r for r in records if _classify(r) != "action"]
    actions = [r for r in records if _classify(r) == "action"]

    for entry in containers:
        kind = _classify(entry)
        title = str(entry.get("Title") or "").strip()
        if not title:
            continue
        if kind == "context":
            ensure_tag(store, title)
            result["tags"] += 1
            continue
        if kind not in ("project", "area"):
            continue
        holder = _ref_name(entry, "area") if kind == "project" else None
        if kind == "project" and not holder:
            parent_name = _ref_name(entry, "")
            if parent_name and parent_name != title:
                found = core.lookup_container(store, parent_name, core.AREA)
                holder = found["title"] if found else None
        node = ensure_container(
            store, title, kind, review=_review_interval(entry),
            area=holder if kind == "project" else None)
        result["projects" if kind == "project" else "areas"] += 1
        reviewed = _stamp(entry.get("ReviewDate"))
        if reviewed:
            store.run("UPDATE items SET reviewed_at = ? WHERE id = ?",
                      (reviewed, node["id"]))
        if _stamp(entry.get("CompletedDate") or entry.get("CompletionDate")):
            core.close_container(store, node["id"], drop_open=False)

    for entry in actions:
        title = str(entry.get("Title") or "").strip()
        if not title:
            result["skipped"] += 1
            continue

        project, area = _split_container(store, entry)
        defer = _stamp(entry.get("DeferredDate") or entry.get("StartDate"))
        due = _stamp(entry.get("DueDate"))
        status = _state_of(entry)
        waiting = str(entry.get("WaitingFor") or "").strip()
        flagged = bool(entry.get("Flagged"))

        # OmniFocus only knows flagged / not flagged, and so does this: a flag
        # is on or off, however many marks the file typed at it.
        priority = 1 if flagged else 0
        explicit = entry.get("Priority") or entry.get("priority")
        if isinstance(explicit, (int, float)) and 0 <= int(explicit) <= 3:
            # Our own exports carry the level; a real OmniFocus export never does.
            priority = min(1, int(explicit))

        note = entry.get("Note") or entry.get("Notes") or ""
        item = core.capture(
            store, None, title=title, parse_text=False,
            project=project, area=None if project else area,
            tags=_record_tags(entry), priority=priority,
            defer=defer, due=due,
            waiting_for=waiting if status == "waiting" else None,
            repeat=_repeat_from_plist(entry.get("Repetition") or entry.get("RepetitionRule")),
            review=_review_interval(entry),
            notes=note if isinstance(note, str) else "",
            to_inbox=(status == "inbox"))

        if status in ("done", "dropped"):
            pass  # the CompletedDate / DroppedDate branches below own this
        elif status != item["status"]:
            try:
                core.set_status(store, item["id"], status)
            except GtdError as exc:
                result["warnings"].append(f"{title}: {exc}")
        if status == "waiting" and waiting:
            store.run("UPDATE items SET waiting_for = ? WHERE id = ?",
                      (waiting, item["id"]))

        if status == "trashed":
            store.run("UPDATE items SET prev_status = 'active' WHERE id = ?",
                      (item["id"],))
        completed = _stamp(entry.get("CompletedDate") or entry.get("CompletionDate"))
        dropped = _stamp(entry.get("DroppedDate"))
        if completed:
            # A completed recurrence stays a single record; the schedule
            # history is not worth inventing.
            # The repeat rule stays on the completed record: our export also
            # carries the live instance, and `complete` is idempotent, so
            # keeping the rule is both lossless and harmless.
            store.run("UPDATE items SET status = 'done', completed_at = ? "
                      "WHERE id = ?", (completed, item["id"]))
        elif dropped:
            store.run("UPDATE items SET status = 'dropped', dropped_at = ? "
                      "WHERE id = ?", (dropped, item["id"]))
        result["actions"] += 1

    store.log(None, "import", f"{source}: {result['actions']} actions")
    return result


# ----------------------------------------------------------- .ofocusmx
#
# `Key: value` pairs, blank line between records, `Note: |` for a note block.

MX_KEYS = {
    "title": "Title", "parent": "Parent", "parentfolder": "ParentFolder",
    "container": "Container", "type": "Type", "note": "Note", "notes": "Note",
    "tags": "Tags", "flagged": "Flagged", "deferreddate": "DeferredDate",
    "startdate": "StartDate", "duedate": "DueDate", "completeddate": "CompletedDate",
    "droppeddate": "DroppedDate", "waitingfor": "WaitingFor", "state": "State",
    "locationinfolder": "LocationInFolder", "repetition": "Repetition",
    "repetitionmode": "RepetitionMode", "repetitioninterval": "RepetitionInterval",
    "repetitionunit": "RepetitionUnit", "reviewdate": "ReviewDate",
    "reviewinterval": "ReviewInterval", "archived": "Archived",
    "project": "Project", "area": "Area", "projecttitle": "Project",
    # .ofocusmx spells contexts "Context", sometimes one line per context.
    "context": "Contexts", "contexts": "Contexts", "contextlist": "Contexts",
}


def parse_ofocusmx(text: str) -> list[dict]:
    """Parse the line-oriented OmniFocus export. Unknown keys are kept as-is,
    so a format change shows up as data we skip rather than an import failure."""
    records: list[dict] = []
    current: dict = {}
    block_key: str | None = None
    last_key: str | None = None

    def flush():
        nonlocal current, block_key, last_key
        if current:
            records.append(current)
        current, block_key, last_key = {}, None, None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            flush()
            continue

        indented = line[:1] in (" ", "\t")
        if block_key is not None:
            if indented:
                current[block_key] = (str(current[block_key]) + " " + line.strip()).strip()
                continue
            block_key = None
        if indented:
            if last_key and last_key in current:
                current[last_key] = (str(current[last_key]) + " " + line.strip()).strip()
            continue

        key, sep, value = line.partition(":")
        if not sep:
            continue
        clean = key.strip().lower().replace(" ", "")
        canonical = MX_KEYS.get(clean, key.strip())
        value = value.strip()
        if value in ("|", "|-"):
            block_key = canonical
            current[canonical] = ""
        elif canonical in ("Tags", "Contexts"):
            names = [part.strip() for part in value.split(",") if part.strip()]
            existing = current.get(canonical)
            current[canonical] = (existing + names) if isinstance(existing, list) else names
        elif canonical == "Flagged":
            current[canonical] = value.lower() in ("true", "yes", "1")
        elif canonical in ("Interval", "RepetitionInterval"):
            try:
                current[canonical] = int(value)
            except ValueError:
                current[canonical] = 1
        else:
            current[canonical] = value
        last_key = canonical
    flush()
    return records


def _mx_to_records(rows: list[dict]) -> list[dict]:
    """Fold the flat .ofocusmx key/value records into the shape the OFXML
    path expects, so both formats go through one importer."""
    out: list[dict] = []
    for row in rows:
        title = str(row.get("Title") or "").strip()
        if not title:
            continue
        kind = str(row.get("Type") or "").strip() or "Action"

        entry: dict = {"Title": title, "Type": kind}
        if row.get("Note"):
            entry["Note"] = str(row["Note"])
        names = list(row.get("Contexts") or []) + list(row.get("Tags") or [])
        if names:
            entry["Tags"] = list(dict.fromkeys(str(n).strip() for n in names if str(n).strip()))
        if row.get("Flagged") is not None:
            entry["Flagged"] = row["Flagged"]
        if row.get("State"):
            entry["State"] = str(row["State"])

        parents: list[dict] = []
        for key, type_name in (("Project", "Project"), ("Area", "Area")):
            if row.get(key):
                parents.append({"Title": str(row[key]), "Type": type_name})
        # ParentFolder/Parent is only a name, never a type: it is an area in
        # some exports and a project in others, so it is resolved by lookup.
        for key in ("ParentFolder", "Parent", "Container"):
            container = str(row.get(key) or "").strip()
            if container:
                parents.append({"Title": container, "Type": "Container"})
                break
        location = str(row.get("LocationInFolder") or "").strip()
        if location:
            entry["LocationInFolder"] = location
        if parents:
            entry["Parents"] = parents

        for source, target in (("DeferredDate", "DeferredDate"),
                               ("StartDate", "DeferredDate"),
                               ("DueDate", "DueDate"),
                               ("CompletedDate", "CompletedDate"),
                               ("DroppedDate", "DroppedDate"),
                               ("WaitingFor", "WaitingFor"),
                               ("ReviewDate", "ReviewDate"),
                               ("ReviewInterval", "ReviewInterval")):
            if row.get(source) and target not in entry:
                entry[target] = row[source]

        if row.get("RepetitionInterval") or row.get("RepetitionUnit"):
            entry["Repetition"] = {
                "Mode": row.get("RepetitionMode", "AfterLastCompletion"),
                "Interval": row.get("RepetitionInterval", 1),
                "Unit": row.get("RepetitionUnit", "Weeks"),
            }
        elif row.get("Repetition"):
            # "every 2 months" written out, which is how a hand-made .ofocusmx
            # and an OmniFocus quick-entry repetition both look.
            cadence = dates.parse_duration(str(row["Repetition"]))
            if cadence:
                entry["Repetition"] = {
                    "Mode": row.get("RepetitionMode", "AfterLastCompletion"),
                    "Interval": cadence[0],
                    "Unit": cadence[1].capitalize() + "s",
                }
        out.append(entry)
    return out


# -------------------------------------------------------------- CSV/JSON


def _row_to_fields(row: dict) -> dict:
    def clean(key):
        value = row.get(key)
        return value.strip() if isinstance(value, str) else value

    tags = clean("tags") or []
    if isinstance(tags, str):
        tags = [part.strip() for part in tags.replace("|", ",").split(",") if part.strip()]

    priority = clean("priority")
    try:
        priority = int(priority) if priority not in (None, "") else 0
    except (TypeError, ValueError):
        priority = 0

    review = (clean("review") or "never").lower()
    if review not in ("daily", "weekly", "monthly", "quarterly", "yearly", "never"):
        review = "never"

    attachments = clean("attachments") or []
    if isinstance(attachments, str):
        try:
            attachments = json.loads(attachments)
        except ValueError:
            attachments = []
    if not isinstance(attachments, list):
        attachments = []

    return {
        "title": clean("title") or "",
        "kind": (clean("kind") or "action").lower(),
        "status": (clean("status") or "").lower() or None,
        "project": clean("project") or None,
        "area": clean("area") or None,
        "url": clean("url") or None,
        "attachments": attachments,
        "tags": tags,
        "priority": max(0, min(1, priority)),
        "defer": clean("defer") or None,
        "due": clean("due") or None,
        "waiting_for": clean("waiting_for") or clean("waiting") or None,
        "notes": clean("notes") or "",
        "repeat": clean("repeat") or None,
        "review": review,
        "reviewed": clean("reviewed") or None,
        "completed": clean("completed") or clean("completed_at") or None,
        "id": clean("id") or None,
    }


def _remap_ids(store, renames: list[tuple[str, str]]) -> dict[str, str]:
    """Restore original ids after an import.

    References are rewritten before any key changes, and the whole thing runs
    in one deferred transaction: a parent key change is checked against
    children immediately, and an action is imported long before the project it
    belongs to. Deferring moves both checks to COMMIT, by which point every
    parent_id points at a row that actually exists.
    """
    targets = {new for _, new in renames}
    sources = {old for old, _ in renames}
    # A row whose target id is still someone's source id has to be renamed
    # last, otherwise it would move onto a key that is about to move.
    ordered = sorted(renames, key=lambda pair: pair[1] in sources)

    renamed: dict[str, str] = {}
    store.run("BEGIN IMMEDIATE")
    try:
        store.run("PRAGMA defer_foreign_keys = ON")
        for old_id, new_id in ordered:
            if store.one("SELECT 1 FROM items WHERE id = ?", (new_id,)):
                continue  # that id is taken by something real; keep the fresh one
            for table, column in (("items", "parent_id"), ("item_tags", "item_id"),
                                  ("attachments", "item_id"), ("log", "item_id")):
                store.run(f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
                          (new_id, old_id))
        for old_id, new_id in ordered:
            if store.one("SELECT 1 FROM items WHERE id = ?", (new_id,)):
                continue
            store.run("UPDATE items SET id = ? WHERE id = ?", (new_id, old_id))
            renamed[old_id] = new_id
        store.run("COMMIT")
    except Exception:
        store.run("ROLLBACK")
        raise
    return renamed


def _overwrite(store, item_id: str, fields: dict) -> dict:
    """Update an item that is already present, matched by its exported id.

    This is what makes re-importing the same export a no-op instead of a
    second copy of your database.
    """
    from . import core

    parent = core.resolve_parent(store, fields["project"], fields["area"])
    changes = {
        "title": fields["title"],
        "notes": fields["notes"],
        "url": fields.get("url"),
        "priority": fields["priority"],
        "waiting_for": fields["waiting_for"],
        "repeat": fields["repeat"],
        "review": fields["review"],
        "defer_at": core._field(fields["defer"]),
        "due_at": core._field(fields["due"]),
    }
    if parent is not None:
        changes["parent_id"] = parent["id"]
    core._update(store, item_id, **changes)
    core.set_tags(store, item_id, fields["tags"])
    return core.get(store, item_id)


def _restore_extras(store, item_id: str, fields: dict, result: dict,
                   *, restore_attachments: bool = True) -> None:
    """Put back the link and, when requested, attachments a record carried."""
    from . import core

    if fields.get("url"):
        core._update(store, item_id, url=fields["url"])
    if restore_attachments:
        _restore_attachments(store, item_id, fields, result)


def _restore_attachments(store, item_id: str, fields: dict, result: dict) -> None:
    from . import core

    for raw in fields.get("attachments") or []:
        if isinstance(raw, dict):
            uri = raw.get("uri") or raw.get("file")
            label = raw.get("name") or None
        else:
            uri, label = raw, None
        if not uri:
            continue
        try:
            core.add_attachment(store, item_id, uri, name=label)
            result["attachments"] += 1
        except GtdError as exc:
            result["warnings"].append(f"attachment {uri}: {exc}")


def _import_rows(store, records, result: dict, source: str,
                 dry_run: bool = False) -> dict:
    from . import core

    renames: list[tuple[str, str]] = []
    attachment_jobs: list[tuple[str, dict]] = []

    for raw in records:
        row = {str(k).lower(): v for k, v in dict(raw).items() if k}
        fields = _row_to_fields(row)
        if not fields["title"]:
            result["skipped"] += 1
            continue

        if fields["kind"] in ("project", "area"):
            nested = fields["kind"] == "project"
            node = ensure_container(
                store, fields["title"], fields["kind"], review=fields["review"],
                area=fields["area"] if nested else None,
                parent=fields["project"] if nested else None)
            result["projects" if fields["kind"] == "project" else "areas"] += 1
            _restore_extras(store, node["id"], fields, result,
                            restore_attachments=False)
            attachment_jobs.append((node["id"], fields))
            if fields["id"] and fields["id"] != node["id"]:
                renames.append((node["id"], fields["id"]))
            continue

        existing = (store.one("SELECT id FROM items WHERE id = ?", (fields["id"],))
                    if fields["id"] else None)
        if existing:
            # Same id already here: rewrite it rather than making a twin.
            item = _overwrite(store, existing["id"], fields)
            result["updated"] += 1
        else:
            item = core.capture(
                store, None, title=fields["title"], notes=fields["notes"],
                project=fields["project"], area=fields["area"], tags=fields["tags"],
                priority=fields["priority"], defer=fields["defer"], due=fields["due"],
                waiting_for=fields["waiting_for"], repeat=fields["repeat"],
                review=fields["review"],
                to_inbox=(fields["status"] == "inbox"))
            result["actions"] += 1
            if fields["id"] and fields["id"] != item["id"]:
                # Keeping original ids makes an import re-runnable and keeps any
                # notes or links you already wrote against those ids meaningful.
                renames.append((item["id"], fields["id"]))

        _restore_extras(store, item["id"], fields, result,
                        restore_attachments=False)
        attachment_jobs.append((item["id"], fields))

        if fields["status"] and fields["status"] != item["status"]:
            if fields["status"] in ("done", "dropped"):
                # History is restored as plain state. Completing an action has
                # side effects — a repeating one spawns its next instance — and
                # an export already carries that instance, so going through
                # `complete` would duplicate it on every import.
                stamp = _stamp(fields["completed"]) or now()
                store.run(
                    "UPDATE items SET status = ?, waiting_for = NULL, "
                    "completed_at = ?, dropped_at = ? WHERE id = ?",
                    (fields["status"],
                     stamp if fields["status"] == "done" else None,
                     stamp if fields["status"] == "dropped" else None,
                     item["id"]))
                core._refresh_container(store, item["parent_id"])
            else:
                try:
                    core.set_status(store, item["id"], fields["status"])
                except GtdError as exc:
                    result["warnings"].append(f"{fields['title']}: {exc}")
        if fields["reviewed"]:
            store.run("UPDATE items SET reviewed_at = ? WHERE id = ?",
                      (_stamp(fields["reviewed"]), item["id"]))

    remapped = {}
    if renames and not dry_run:
        # A dry run is already inside the rehearsal transaction, and the remap
        # needs one of its own.
        remapped = _remap_ids(store, renames)
        result["remapped"] = len(remapped)
    if not dry_run:
        for original_id, fields in attachment_jobs:
            _restore_attachments(store, remapped.get(original_id, original_id),
                                 fields, result)
    store.log(None, "import", f"{source}: {result['actions']} actions")
    return result


def import_csv(store, text: str, *, dry_run=False, progress=False,
               source: str = "csv") -> dict:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "title" not in {f.lower() for f in reader.fieldnames}:
        raise GtdError("CSV needs at least a 'title' column")
    return _import_rows(store, list(reader), _blank_result(source), source,
                       dry_run=dry_run)


def import_json(store, payload, *, dry_run=False, progress=False,
                source: str = "json") -> dict:
    """Round-trip our own export; also accepts a bare list of items."""
    if isinstance(payload, dict):
        records = payload.get("items") or payload.get("actions") or []
        contexts = payload.get("tags") or []
    elif isinstance(payload, list):
        records, contexts = payload, []
    else:
        raise GtdError("JSON must be an object with 'items', or a list of items")

    result = _blank_result(source)
    for context in contexts:
        name = context.get("name") if isinstance(context, dict) else context
        if name:
            ensure_tag(store, name)
            result["tags"] += 1
    return _import_rows(store, records, result, source, dry_run=dry_run)


def _import_zip(store, path: Path, *, dry_run=False, progress=False) -> dict:
    """Read the JSON payload out of one of our own zip exports. The archive
    also carries a raw sqlite snapshot, but importing through the JSON (and
    the shared item pipeline) keeps ids, parents and tags consistent with
    every other import instead of swapping the database file underneath."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        member = None
        if "gtd.json" in names:
            member = "gtd.json"
        else:
            member = next((n for n in names
                           if n.endswith(".json") and Path(n).name != "manifest.json"),
                          None)
        if member is None:
            raise GtdError(f"{path} holds no gtd.json to import")
        payload = json.loads(archive.read(member).decode("utf-8", "replace"))

        extracted: dict[str, str] = {}
        with tempfile.TemporaryDirectory(prefix="omagtd-import-") as staging:
            for name in names:
                parts = name.split("/")
                if (len(parts) < 2 or parts[0] != "attachments" or name.endswith("/")
                        or any(part in ("", ".", "..") for part in parts)):
                    continue
                target = Path(staging).joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as source, open(target, "wb") as sink:
                    shutil.copyfileobj(source, sink)
                extracted[name] = str(target)

            if extracted:
                records = payload.get("items") if isinstance(payload, dict) else payload
                for record in records or []:
                    if not isinstance(record, dict):
                        continue
                    for attachment in record.get("attachments") or []:
                        if not isinstance(attachment, dict):
                            continue
                        ref = attachment.get("file")
                        if ref and ref in extracted:
                            attachment["uri"] = extracted[ref]
            return import_json(store, payload, dry_run=dry_run, progress=progress,
                               source=f"{path}:{member}")


# ------------------------------------------------------------ entry point


def import_file(store, source: str, *, fmt: str = "auto", dry_run: bool = False,
                progress: bool = False) -> dict:
    """Import from a file or folder, choosing the format by content."""
    path = Path(os.path.expanduser(source))
    if not path.exists():
        raise GtdError(f"no such file or folder: {source}")

    kind = fmt if fmt != "auto" else _sniff(path)

    def note(message: str) -> None:
        if progress and not dry_run:
            print(f"  {message}", file=sys.stderr)

    if dry_run:
        # Everything below is ordinary DDL/DML, so a transaction is a faithful
        # rehearsal: the same code paths run and then the writes are undone.
        store.run("BEGIN")
    try:
        if kind in ("omnifocus", "ofxml", "ofocusmx"):
            result = _import_omnifocus_path(store, path, kind, note)
        elif kind == "csv":
            result = import_csv(
                store, path.read_text(encoding="utf-8", errors="replace"),
                dry_run=dry_run, progress=progress, source=str(path))
        elif kind == "json":
            result = import_json(
                store, json.loads(path.read_text(encoding="utf-8", errors="replace")),
                dry_run=dry_run, progress=progress, source=str(path))
        elif kind == "zip":
            result = _import_zip(store, path, dry_run=dry_run, progress=progress)
        else:
            raise GtdError(f"unsupported import format: {kind}")
    except Exception:
        if dry_run:
            store.run("ROLLBACK")
        raise
    if dry_run:
        store.run("ROLLBACK")

    result["format"] = kind
    return result


def _sniff(path: Path) -> str:
    if path.is_dir():
        return "ofocusmx" if any(path.glob("*.ofocusmx")) else "ofxml"

    suffix = path.suffix.lower()
    if suffix == ".csv":
        return "csv"
    if suffix == ".json":
        return "json"
    if suffix == ".zip":
        return "zip"
    if suffix == ".ofocusmx":
        return "ofocusmx"
    if suffix in (".plist", ".ofocusbackup") or "backup" in path.name.lower():
        return "ofxml"

    head = path.read_bytes()[:4096]
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if head.startswith(b"bplist00") or head.lstrip().startswith(b"<?xml"):
        return "ofxml"
    text = head.decode("utf-8", "replace")
    lines = text.splitlines()
    first = text.lstrip()[:1]
    if first in ("[", "{"):
        return "json"
    lowered = text.lower()
    if "title:" in lowered or "parent:" in lowered:
        return "ofocusmx"
    if lines and "," in lines[0]:
        return "csv"
    return "ofxml"


def _import_omnifocus_path(store, path: Path, kind: str, note) -> dict:
    if path.is_dir():
        texts = sorted(path.glob("*.ofocusmx"))
        if texts:
            records: list[dict] = []
            for text_path in texts:
                note(f"reading {text_path.name}")
                records.extend(_mx_to_records(parse_ofocusmx(
                    text_path.read_text(encoding="utf-8", errors="replace"))))
            return import_omnifocus(store, records, source=f"{path} ({len(texts)} files)")
        plists = sorted(path.glob("*.plist")) or sorted(path.glob("*.ofocusbackup"))
        if not plists:
            raise GtdError(f"{path} has no .ofocusmx or .plist files")
        path = plists[0]
        note(f"reading {path.name}")

    raw = path.read_bytes()
    if kind == "ofocusmx":
        return import_omnifocus(
            store, _mx_to_records(parse_ofocusmx(raw.decode("utf-8", "replace"))),
            source=str(path))

    try:
        payload = plistlib.loads(raw)
    except Exception:
        text = raw.decode("utf-8", "replace")
        if "Title:" in text:
            return import_omnifocus(store, _mx_to_records(parse_ofocusmx(text)),
                                   source=str(path))
        raise GtdError(f"{path.name} is not a readable OmniFocus export")
    return import_omnifocus(store, payload, source=str(path))


# ----------------------------------------------------------------- export


# Our status -> the State string OmniFocus understands (and we can read back).
OFXML_STATE = {
    "active": "Active",
    "inbox": "Inbox",
    "waiting": "Waiting",
    "someday": "Someday",
    "reference": "Active",
    "onhold": "Active",
    "done": "Completed",
    "dropped": "Dropped",
    "trashed": "Dropped",
}


def _repeat_parts(text: str) -> tuple[int, str]:
    """Read the cadence out of our own repeat text: "every 2 weeks after schedule"."""
    cadence = ORIGIN_SUFFIX_RE.sub("", str(text)).strip()
    return dates.parse_duration(cadence) or (1, "week")


def _attachment_file_name(item_id: str, uri: str) -> str:
    """The task-id-scoped path an attachment takes inside a zip export."""
    base = os.path.basename(str(uri).rstrip("/")) or "file"
    digest = hashlib.sha1(str(uri).encode("utf-8")).hexdigest()[:8]
    task = re.sub(r"[^A-Za-z0-9_-]+", "-", str(item_id)).strip("-") or "task"
    return f"attachments/{task}/{digest}-{base}"


def _export_attachments(item: dict, store) -> list[dict]:
    from . import core

    out = []
    for row in item.get("attachments") or []:
        uri = str(row.get("uri") or "")
        file_name = None
        if uri:
            # Relative rows only resolve against the store they belong to, so
            # the database is what decides whether the file is really there.
            local = core.attachment_path(store, uri)
            if local is not None and local.exists():
                file_name = _attachment_file_name(str(item.get("id") or "task"), uri)
        out.append({"name": row.get("name") or "", "uri": uri, "file": file_name})
    return out


def _export_row(item: dict, tags: list[str] | None = None, store=None) -> dict:
    repeat = item.get("repeat")
    if isinstance(repeat, str):
        try:
            repeat = json.loads(repeat)
        except ValueError:
            repeat = None
    if repeat:
        interval = int(repeat.get("interval", 1))
        unit = str(repeat.get("unit", "week"))
        origin = str(repeat.get("from", "complete"))
        every = "every week" if interval == 1 else f"every {interval} {unit}s"
        repeat_text = f"{every} after {origin}"
    else:
        repeat_text = None
    # A project can live inside another project now, so the immediate parent's
    # kind decides whether it is `project` (nest) or `area` (top level).
    kind = item.get("kind", "action")
    parent_title = item.get("parent_title")
    parent_kind = item.get("parent_kind")
    project = area = None
    if parent_title:
        if parent_kind == "area":
            area = parent_title
        elif parent_kind == "project":
            project = parent_title
        elif kind == "action":  # an older view that did not carry the kind
            project = parent_title
    return {
        "id": item.get("id"),
        "kind": kind,
        "title": item.get("title", ""),
        "status": item.get("status", ""),
        "project": project,
        "area": area,
        "tags": tags if tags is not None else (item.get("tags") or []),
        "priority": min(1, int(item.get("priority") or 0)),
        "defer": item.get("defer_at"),
        "due": item.get("due_at"),
        "waiting_for": item.get("waiting_for"),
        "notes": item.get("notes") or "",
        "url": item.get("url"),
        "attachments": _export_attachments(item, store),
        "repeat": repeat_text,
        "review": item.get("review") or "never",
        "reviewed": item.get("reviewed_at"),
        "completed": item.get("completed_at"),
    }


def _all_rows(store) -> list[dict]:
    from . import core

    rows = [core.as_dict(row) for row in store.query(
        "SELECT * FROM item_titles WHERE status <> 'trashed' ORDER BY kind, created_at")]
    rows += [core.as_dict(row) for row in store.query(
        "SELECT * FROM item_titles WHERE status = 'trashed' ORDER BY updated_at DESC")]

    exported = []
    for item in rows:
        item["attachments"] = core.attachments_of(store, item["id"])
        exported.append(_export_row(item, core.tags_of(store, item["id"]), store))
    exported.sort(key=lambda row: (row["kind"], (row["project"] or ""), row["title"] or ""))
    return exported


def export_text(store, fmt: str = "json") -> str:
    rows = _all_rows(store)

    if fmt == "csv":
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "tags": "|".join(row.get("tags") or [])})
        return buffer.getvalue()

    if fmt == "ofxml":
        # Shaped so our own importer can read it back, which is the point.
        contents = []
        for row in rows:
            entry: dict = {
                "Title": row["title"],
                "Type": row["kind"].capitalize(),
                "Note": row.get("notes") or "",
                "Tags": list(row.get("tags") or []),
                # OmniFocus only knows flagged / not flagged; ours is the same
                # one flag, carried as a number for the round trip.
                "Priority": min(1, int(row.get("priority") or 0)),
                "Flagged": bool(row.get("priority")),
                "State": OFXML_STATE.get(row["status"], "Active"),
                "ReviewInterval": {"daily": 1, "weekly": 2, "monthly": 3,
                                   "quarterly": 4, "yearly": 5,
                                   "never": 0}.get(row.get("review") or "never", 0),
            }
            parents = []
            if row.get("project"):
                entry["Project"] = {"Title": row["project"], "Type": "Project"}
                parents.append({"Title": row["project"], "Type": "Project"})
            if row.get("area"):
                entry["Area"] = {"Title": row["area"], "Type": "Area"}
                parents.append({"Title": row["area"], "Type": "Area"})
            if parents:
                entry["Parents"] = parents
            if row.get("defer"):
                entry["DeferredDate"] = _dt.datetime.strptime(row["defer"], "%Y-%m-%d %H:%M")
            if row.get("due"):
                entry["DueDate"] = _dt.datetime.strptime(row["due"], "%Y-%m-%d %H:%M")
            if row.get("waiting_for"):
                entry["WaitingFor"] = row["waiting_for"]
            if row.get("repeat"):
                parsed = _repeat_parts(row["repeat"])
                entry["Repetition"] = {
                    "Mode": "AfterLastCompletion"
                            if "complete" in row["repeat"] else "AfterScheduledDate",
                    "Interval": parsed[0],
                    "Unit": parsed[1].capitalize(),
                }
            if row.get("completed"):
                entry["CompletedDate"] = _dt.datetime.strptime(
                    row["completed"], "%Y-%m-%d %H:%M")
            if row.get("reviewed"):
                entry["ReviewDate"] = _dt.datetime.strptime(
                    row["reviewed"], "%Y-%m-%d %H:%M")
            contents.append(entry)

        return plistlib.dumps({
            "ExportDate": _dt.datetime.now(),
            "Archives": [{"ArchiveName": "GTD", "Contents": contents}],
        }, fmt=plistlib.FMT_XML, sort_keys=False).decode("utf-8")

    tags = [{"name": row["name"]} for row in
            store.query("SELECT name FROM tags ORDER BY name")]
    return json.dumps({
        "format": "omagtd",
        "version": 1,
        "exported": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "items": rows,
        "tags": tags,
    }, indent=2, default=str)
