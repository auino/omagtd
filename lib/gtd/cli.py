"""The `gtd` command line.

Everything the panel can do is a `gtd` subcommand, so the terminal, the
hotkeys and the Quickshell UI are three front ends over one engine rather
than three implementations of the same rules.

Every command takes `--json` and answers with a single object, which is what
BinWidget.qml and Panel.qml parse.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import zipfile

from . import __version__, core, dates, importer, quick, review
from .core import GtdError
from .db import Store, db_path, now

# ------------------------------------------------------------------ output


class Out:
    """Terminal output: colours when a human is watching, plain otherwise."""

    def __init__(self, args):
        self.json_mode = bool(getattr(args, "json", False))
        self.quiet = bool(getattr(args, "quiet", False))
        self.color = (
            sys.stdout.isatty()
            and not os.environ.get("NO_COLOR")
            and os.environ.get("TERM", "") != "dumb"
        )

    def paint(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color and text else text

    def dim(self, text):     return self.paint(text, "2")
    def bold(self, text):    return self.paint(text, "1")
    def red(self, text):     return self.paint(text, "31")
    def green(self, text):   return self.paint(text, "32")
    def yellow(self, text):  return self.paint(text, "33")
    def blue(self, text):    return self.paint(text, "34")
    def cyan(self, text):    return self.paint(text, "36")

    def line(self, text: str = "") -> None:
        if not self.json_mode and not self.quiet:
            print(text)

    def data(self, payload: dict) -> None:
        """Emit the machine-readable form and stop."""
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))

    def fail(self, message: str, reason: str | None = None) -> None:
        if self.json_mode:
            payload = {"ok": False, "error": message}
            if reason:
                payload["reason"] = reason
            print(json.dumps(payload, indent=2))
        else:
            print(self.red("gtd: ") + message, file=sys.stderr)
        raise SystemExit(2)


STATUS_GLYPH = {
    "inbox": "?", "active": " ", "waiting": "W", "someday": "S",
    "reference": "R", "onhold": "‖", "done": "✓", "dropped": "×",
    "trashed": "⌫",
}


def item_line(item: dict, out: Out, *, width: int = 62, show_container: bool = True) -> str:
    flags = "!" if item.get("priority") else ""
    glyph = STATUS_GLYPH.get(item.get("status") or "", " ")
    title = item.get("title") or ""
    marker = glyph + " " + (out.red(flags) if flags else flags)

    meta: list[str] = []
    for tag in item.get("tags") or []:
        meta.append(out.dim("#" + tag))
    if show_container and item.get("kind") == "action" and item.get("parent_title"):
        meta.append(out.dim(item["parent_title"]))
    if item.get("waiting_for"):
        meta.append(out.yellow("waiting " + item["waiting_for"]))

    due = item.get("due_at")
    defer = item.get("defer_at")
    stamp = dates.parse_stamp(due or defer)
    if stamp is not None:
        text = dates.human(due or defer)
        past = stamp < dates.parse_stamp(now())
        meta.append(out.red(text) if (past and due) else out.dim(text))
    if item.get("duration"):
        meta.append(out.dim(dates.human_minutes(item["duration"])))
    if item.get("url"):
        meta.append(out.dim("↗"))

    # Pad the title out to a fixed column so the metadata lines up down the
    # list; the title itself is the only part that gets highlighted.
    title_width = max(12, width - sum(len(part) for part in meta))
    padding = " " * max(1, title_width - len(title) + 1)
    line = f"{marker} {out.bold(title)}{padding}" + "  ".join(meta)
    if item.get("repeat"):
        line += " " + out.dim("↻")
    return line


def print_items(items, out: Out, *, header: str = "", empty: str = "Nothing here.",
                width: int = 62) -> None:
    if header:
        out.line(out.bold(header))
    if not items:
        out.line(out.dim("  " + empty))
        return
    for item in items:
        out.line("  " + item_line(item, out, width=width))


# -------------------------------------------------------------- commands


def cmd_init(args, out: Out, store: Store) -> dict:
    return {"ok": True, "db": str(store.path), "schema": store.meta("schema")}


def cmd_capture(args, out: Out, store: Store) -> dict:
    item = core.capture(
        store, " ".join(args.text),
        project=args.project, area=args.area, tags=args.tag or (),
        priority=args.priority, defer=args.defer, due=args.due,
        duration=getattr(args, "duration", None),
        waiting_for=args.who, repeat=args.repeat, review=args.review,
        url=getattr(args, "link", None), attach=getattr(args, "attach", None) or (),
        notes=args.notes or "", to_inbox=args.inbox)
    if out.json_mode:
        return {"ok": True, "item": item}
    where = item.get("parent_title") or {"waiting": "Waiting for",
                                         "someday": "Someday",
                                         "reference": "Reference"}.get(item["status"], "Inbox")
    out.line(f"{out.green('captured')}  {item['title']}  "
             f"{out.dim('→')}  {where} {out.dim('(' + item['id'] + ')')}")
    return {"ok": True, "item": item}


def cmd_list(args, out: Out, store: Store) -> dict:
    view = args.view
    offset = getattr(args, "offset", 0) or 0
    items = core.list_view(store, view, limit=args.limit, offset=offset,
                           project=args.project, area=args.area, tag=args.tag)
    if out.json_mode:
        return {"ok": True, "view": view, "count": len(items),
                "offset": offset, "items": items}
    titles = {
        "inbox": "Inbox", "clarify": "Clarify queue", "focus": "Next actions",
        "ten": "Focus Ten", "focus-ten": "Focus Ten", "today": "Today",
        "forecast": "Forecast", "overdue": "Overdue", "due_today": "Due today",
        "upcoming": "Upcoming", "waiting": "Waiting for",
        "someday": "Someday / Maybe", "flagged": "Flagged",
        "reference": "Reference", "onhold": "On hold", "trash": "Trash",
        "done": "Completed", "projects": "Projects", "areas": "Areas",
        "stalled": "Stalled projects", "empty": "Projects with no actions",
        "log": "Log", "tags": "Contexts",
    }
    if view == "log":
        out.line(out.bold(f"Log  ({len(items)})"))
        for entry in items:
            when = dates.human(entry["at"])
            subject = entry.get("title") or ""
            tail = out.dim(f"({subject})") if subject else ""
            out.line(f"  {out.dim(when.ljust(11))} {entry['kind'].ljust(16)} "
                     f"{(entry['detail'] or '').strip()[:58]} {tail}".rstrip())
        return {"ok": True, "view": view, "count": len(items),
                "offset": offset, "items": items}

    if view == "tags":
        # Contexts are tags, not items: they have a name and an open count.
        if not items:
            out.line(out.dim("  no contexts yet — tag something with #name"))
        else:
            out.line(out.bold(f"Contexts  ({len(items)})"))
            for tag in items:
                review = out.dim(f"  {tag['review']}") if tag.get("review") != "never" else ""
                out.line(f"  #{tag['name']}  {tag['open_count']} open"
                         f"{review}  {out.dim(tag['id'])}")
        return {"ok": True, "view": view, "count": len(items),
                "offset": offset, "items": items}

    print_items(items, out, header=f"{titles.get(view, view)}  ({len(items)})",
                empty="empty", width=args.width)
    return {"ok": True, "view": view, "count": len(items),
            "offset": offset, "items": items}


def cmd_search(args, out: Out, store: Store) -> dict:
    query = " ".join(args.query)
    offset = getattr(args, "offset", 0) or 0
    items = core.search(store, query, limit=args.limit, offset=offset)
    if out.json_mode:
        return {"ok": True, "query": query, "count": len(items),
                "offset": offset, "items": items}
    print_items(items, out, header=f'Matches for "{query}"  ({len(items)})',
                empty="nothing matches", width=args.width)
    return {"ok": True, "query": query, "count": len(items),
            "offset": offset, "items": items}


def cmd_complete(args, out: Out, store: Store) -> dict:
    done = core.complete_name(store, args.kind, args.prefix)
    if out.json_mode:
        return {"ok": True, "kind": args.kind, "prefix": args.prefix,
                "completion": done}
    if done:
        out.line(done)
    return {"ok": True, "kind": args.kind, "prefix": args.prefix, "completion": done}


def cmd_attach(args, out: Out, store: Store) -> dict:
    items = []
    for uri in args.uri:
        items.append(core.add_attachment(store, args.id, uri, name=args.name))
    detail = core.get(store, core.find(store, args.id)["id"])
    if out.json_mode:
        return {"ok": True, "item": detail}
    for attachment in items:
        out.line(f"{out.green('attached')}  {attachment['name']}  "
                 f"{out.dim(attachment['uri'])}")
    return {"ok": True, "item": detail}


def cmd_unattach(args, out: Out, store: Store) -> dict:
    removed = 0
    for target in args.target:
        if core.remove_attachment(store, args.id, target):
            removed += 1
            out.line(f"{out.green('detached')}  {out.dim(target)}")
        elif not out.json_mode:
            out.line(out.yellow(f"not attached: {target}"))
    detail = core.get(store, core.find(store, args.id)["id"])
    if out.json_mode:
        return {"ok": True, "removed": removed, "item": detail}
    return {"ok": True, "removed": removed, "item": detail}


def cmd_focus(args, out: Out, store: Store) -> dict:
    view = "ten" if args.ten else "focus"
    items = core.list_view(store, view, limit=args.limit, project=args.project,
                           tag=args.tag)
    if out.json_mode:
        return {"ok": True, "view": view, "count": len(items), "items": items}
    header = ("Focus Ten — next ten actions" if args.ten
              else f"Next actions ({len(items)})")
    print_items(items, out, header=header, empty="focus is clear", width=args.width)
    return {"ok": True, "view": view, "count": len(items), "items": items}


def cmd_show(args, out: Out, store: Store) -> dict:
    item = core.find(store, args.id)
    detail = core.get(store, item["id"])
    if out.json_mode:
        return {"ok": True, "item": detail}
    out.line(out.bold(detail["title"]) + "  " + out.dim(detail["id"]))
    out.line(out.dim(f"  {detail['kind']} · {detail['status']}"
                     + (f" · {detail['parent_title']}" if detail.get("parent_title") else "")))
    if detail.get("tags"):
        out.line("  " + " ".join("#" + t for t in detail["tags"]))
    if detail.get("url"):
        out.line("  " + out.dim("link: ") + detail["url"])
    for attachment in detail.get("attachments") or []:
        out.line("  " + out.dim("attach: ") + (attachment.get("name") or "")
                 + "  " + out.dim(attachment.get("uri") or ""))
    for label, key in (("defer", "defer_at"), ("due", "due_at"),
                       ("waiting", "waiting_for"), ("repeat", "repeat")):
        value = detail.get(key)
        if key == "repeat" and value:
            value = f"every {value['interval']} {value['unit']}s after {value['from']}"
        elif key in ("defer_at", "due_at") and value:
            value = f"{dates.human(value)} ({value})"
        if value:
            out.line(f"  {out.dim(label + ':')} {value}")
    if detail.get("notes"):
        for line in detail["notes"].splitlines():
            out.line("  | " + line)
    if detail["children"]:
        out.line(out.dim(f"  {len(detail['children'])} items, "
                         f"{detail['open_children']} still open"))
        for child in detail["children"][:20]:
            out.line("    " + out.green("✓" if child["status"] == "done" else " ")
                     + " " + child["title"])
    if detail.get("last_log"):
        out.line(out.dim(f"  last touched {dates.human(detail['last_log'])}"))
    return {"ok": True, "item": detail}


def cmd_organize(args, out: Out, store: Store) -> dict:
    item = core.organize(
        store, args.id, title=args.title,
        project=args.project, area=args.area, add=args.tag or (),
        drop_tags=args.untag or (), defer=args.defer, clear_defer=args.undefer,
        due=args.due, clear_due=args.undue, priority=args.priority,
        duration=getattr(args, "duration", None),
        clear_duration=getattr(args, "clear_duration", False),
        notes=args.notes, review=args.review, repeat=args.repeat,
        clear_repeat=args.no_repeat, waiting_for=args.who, inbox=args.inbox,
        url=getattr(args, "link", None), clear_url=getattr(args, "unlink", False),
        attach=getattr(args, "attach", None) or (),
        unattach=getattr(args, "unattach", None) or (),
        onhold=args.onhold)
    if out.json_mode:
        return {"ok": True, "item": item}
    out.line(f"{out.green('organized')}  {item['title']}")
    out.line("  " + out.dim(quick.format_item(item)))
    return {"ok": True, "item": item}


def cmd_clarify(args, out: Out, store: Store) -> dict:
    item = core.clarify(store, args.id, args.outcome, project=args.project,
                        area=args.area, defer=args.defer, due=args.due,
                        waiting_for=args.who, priority=args.priority,
                        notes=args.notes, tags=args.tag or (),
                        review=args.review)
    if out.json_mode:
        return {"ok": True, "item": item}
    where = item.get("parent_title") or item["status"]
    out.line(f"{out.green('clarified')}  {args.outcome}  {item['title']} → {where}")
    return {"ok": True, "item": item}


def cmd_triage(args, out: Out, store: Store) -> dict:
    """The inbox, oldest first, with the decision each item is waiting on."""
    items = core.list_view(store, "clarify", limit=args.limit)
    if out.json_mode:
        return {"ok": True, "count": len(items), "items": items}
    if not items:
        out.line(out.green("inbox is empty — your head is clear"))
        return {"ok": True, "count": 0, "items": []}

    out.line(out.bold(f"Clarify {len(items)} inbox item(s), oldest first"))
    for item in items:
        age = (dates.parse_stamp(now()) - dates.parse_stamp(item["created_at"])).days
        hints = []
        if item.get("priority"):
            hints.append("flagged")
        if item.get("due_at"):
            hints.append("has a deadline")
        if item.get("defer_at"):
            hints.append("already dated")
        if item.get("waiting_for"):
            hints.append(f"waiting on {item['waiting_for']}")
        out.line("  " + item_line(item, out, width=54))
        out.line(out.dim(f"      captured {age}d ago"
                         + (f" · {', '.join(hints)}" if hints else "")
                         + f" · gtd clarify {item['id']} <actionable|do-now|defer|wait|someday|reference|delete>"))
    return {"ok": True, "count": len(items), "items": items}


def cmd_engage(args, out: Out, store: Store) -> dict:
    action = args.action
    if action == "wait" and args.who:
        item = core.organize(store, args.id, waiting_for=args.who)
    elif action == "wait":
        # No name given: the bare on/off decision, what the app's switch flips.
        item = core.engage(store, args.id, "wait")
    elif action == "done":
        item = core.complete(store, args.id)
    elif action == "flag":
        item = core.engage(store, args.id, "flag",
                       priority=(1 if args.priority is None else args.priority))
    else:
        item = core.engage(store, args.id, action, when=args.when,
                           priority=args.priority, waiting_for=args.who)
    if out.json_mode:
        return {"ok": True, "item": item}
    verbs = {"done": "completed", "drop": "dropped", "start": "started",
             "stop": "stopped",
             "wait": (("waiting on " + args.who) if args.who else "waiting"),
             "resume": "resumed", "defer": "deferred to " + (args.when or ""),
             "flag": "flagged", "unflag": "unflagged",
             "inbox": "back in the inbox"}
    out.line(f"{out.green(verbs.get(action, action))}  {item['title']} "
             f"{out.dim(item['id'])}")
    if item.get("next"):
        nxt = item["next"]
        out.line("  " + out.dim(f"repeats: {nxt['title']} "
                                f"({dates.human(nxt.get('defer_at') or nxt.get('due_at'))} "
                                f"· {nxt['id']})"))
    if args.also_close:
        parent = item.get("parent_id")
        if parent and item["open_children"] == 0:
            core.close_container(store, parent, drop_open=False)
            out.line("  " + out.dim(f"closed {item['parent_title']} — nothing left in it"))
    return {"ok": True, "item": item}


def cmd_postpone(args, out: Out, store: Store) -> dict:
    """Roll an action's deadline forward one day, keeping its clock time."""
    item = core.postpone(store, args.id, days=args.days)
    if out.json_mode:
        return {"ok": True, "item": item}
    if item.get("due_at"):
        out.line(f"{out.green('postponed')}  {item['title']} "
                 f"{out.dim(item['id'])}  "
                 f"{out.dim('to ' + dates.human(item['due_at']))}")
    else:
        out.line(out.dim(f"nothing to postpone: {item['title']} has no deadline"))
    return {"ok": True, "item": item}


def cmd_open(args, out: Out, store: Store) -> dict:
    """Open the link an item carries in the desktop's default viewer.

    The link is the item's `url` field when it has one, else its first URL
    attachment. A local file is not a link, so a file attachment is left for
    the fullscreen editor's copy button rather than opened.
    """
    item = core.find(store, args.id)
    link = core.link_for(store, item["id"])
    if not link:
        raise GtdError(
            f"nothing to open: {item['title'] or item['id']} carries no link",
            reason="no-link")
    if args.print:
        if out.json_mode:
            return {"ok": True, "link": link}
        out.line(link)
        return {"ok": True, "link": link}
    target = link
    if target.startswith("file://"):
        try:
            target = urllib.parse.unquote(target[len("file://"):])
        except Exception:
            target = target[len("file://"):]
    subprocess.Popen(["xdg-open", target])
    if out.json_mode:
        return {"ok": True, "link": link}
    out.line(f"{out.green('opened')}  {link}")
    return {"ok": True, "link": link}


def cmd_project(args, out: Out, store: Store) -> dict:
    kind = "project" if args.group == "project" else "area"
    if args.action == "add":
        node = core.ensure_container(store, args.name, kind, area=args.area,
                                     parent=getattr(args, "project", None),
                                     review=args.review or "never")
        if out.json_mode:
            return {"ok": True, "item": node}
        out.line(f"{out.green('created')}  {kind}  {node['title']}  "
                 f"{out.dim(node['id'])}")
        return {"ok": True, "item": node}

    if args.action == "list":
        offset = getattr(args, "offset", 0) or 0
        items = core.list_view(store, kind + "s", limit=args.limit, offset=offset)
        if out.json_mode:
            return {"ok": True, "count": len(items), "offset": offset, "items": items}
        if not items:
            out.line(out.dim(f"no {kind}s yet"))
            return {"ok": True, "count": 0, "offset": offset, "items": []}
        out.line(out.bold(f"{kind.title()}s ({len(items)})"))
        for item in items:
            tick = out.green("✓") if item["completed"] else " "
            review_mark = out.dim("  " + item["review"]) if item.get("review") != "never" else ""
            where = out.dim("in " + item["parent_title"]) if item.get("parent_title") else ""
            out.line(f"  {tick} {item['title']}  {out.dim(item['id'])}  {where}{review_mark}")
        return {"ok": True, "count": len(items), "offset": offset, "items": items}

    if args.action == "close":
        node = core.close_container(store, args.name, drop_open=not args.keep)
        if out.json_mode:
            return {"ok": True, "item": node}
        dropped = len(node.get("dropped") or [])
        out.line(f"{out.green('closed')}  {node['title']}"
                 + (f"  {out.dim(f'{dropped} action(s) dropped')}" if dropped else ""))
        return {"ok": True, "item": node}

    if args.action == "reopen":
        node = core.reopen_container(store, args.name)
        if out.json_mode:
            return {"ok": True, "item": node}
        out.line(f"{out.green('reopened')}  {node['title']}")
        return {"ok": True, "item": node}

    if args.action == "review":
        node = core.organize(store, args.name, review=args.interval)
        if out.json_mode:
            return {"ok": True, "item": node}
        out.line(f"{out.green('review cadence set')}  {node['title']} → {args.interval}")
        return {"ok": True, "item": node}

    if args.action == "add-action":
        parent = core.find(store, args.name)
        item = core.capture(store, args.action_title, project=parent["title"],
                            tags=args.tag or (), defer=args.defer, due=args.due,
                            priority=args.priority)
        if out.json_mode:
            return {"ok": True, "item": item}
        out.line(f"{out.green('added')}  {item['title']} → "
                 f"{core.get(store, item['id']).get('parent_title')}")
        return {"ok": True, "item": item}

    raise GtdError(f"unknown project action {args.action}")


def cmd_tag(args, out: Out, store: Store) -> dict:
    if args.action == "add":
        tag = core.ensure_tag(store, args.name)
        if args.on:
            core.add_tags(store, core.find(store, args.on)["id"], [args.name])
        if out.json_mode:
            return {"ok": True, "tag": tag}
        out.line(f"{out.green('context')}  {tag['name']}  {out.dim(tag['id'])}")
        return {"ok": True, "tag": tag}

    if args.action == "list":
        offset = getattr(args, "offset", 0) or 0
        tags = core.list_view(store, "tags", limit=args.limit, offset=offset)
        if out.json_mode:
            return {"ok": True, "count": len(tags), "offset": offset, "tags": tags}
        if not tags:
            out.line(out.dim("no contexts yet — tag something with #name"))
            return {"ok": True, "count": 0, "offset": offset, "tags": tags}
        out.line(out.bold(f"Contexts ({len(tags)})"))
        for tag in tags:
            out.line(f"  #{tag['name']}  {tag['open_count']} open  "
                     f"{out.dim(tag['id'])}")
        return {"ok": True, "count": len(tags), "offset": offset, "tags": tags}

    if args.action == "rm":
        rows = store.query("SELECT id FROM tags WHERE name = ? COLLATE NOCASE",
                           (args.name.lstrip("#@"),))
        if not rows:
            raise GtdError(f"no context called {args.name!r}")
        store.run("DELETE FROM tags WHERE id = ?", (rows[0]["id"],))
        if out.json_mode:
            return {"ok": True}
        out.line(f"{out.green('removed context')}  {args.name}")
        return {"ok": True}

    if args.action == "review":
        tag = core.set_tag_review(store, args.name, args.interval)
        if out.json_mode:
            return {"ok": True, "tag": tag}
        out.line(f"{out.green('review cadence set')}  #{tag['name']} → {args.interval}")
        return {"ok": True, "tag": tag}

    raise GtdError(f"unknown tag action {args.action}")


def cmd_calendar(args, out: Out, store: Store) -> dict:
    import datetime as _dt

    if args.month:
        try:
            first = _dt.date.fromisoformat(args.month + "-01")
        except ValueError as exc:
            raise GtdError(f"month must look like YYYY-MM: {exc}") from exc
        nxt = (first.replace(day=28) + _dt.timedelta(days=8)).replace(day=1)
        start, end = first, nxt - _dt.timedelta(days=1)
    else:
        start = _dt.date.fromisoformat(args.start) if args.start else _dt.date.today()
        end = start + _dt.timedelta(days=max(1, args.days) - 1)
    data = core.calendar(store, start=start.isoformat(), end=end.isoformat(),
                         all_due=getattr(args, "all", False))
    if out.json_mode:
        return {"ok": True, **data}
    totals = data["totals"]
    out.line(out.bold(f"Calendar  {data['start']} → {data['end']}") + "  "
             + out.dim(f"{totals['due']} due · {totals['scheduled']} defer · "
                       f"{totals['completed']} done"
                       + (f" · {totals['overdue']} overdue" if totals["overdue"] else "")))
    for cell in data["days"]:
        if not cell["count"]:
            continue
        out.line("  " + out.bold(cell["date"]) + "  " + out.dim(cell["weekday"])
                 + (out.red(f"  {len(cell['overdue'])} overdue") if cell["overdue"] else ""))
        for item in cell["overdue"]:
            out.line("    " + out.red("! ") + item["title"])
        for item in cell["due"]:
            stamp = item["due_at"][11:16] if item.get("due_at") else ""
            out.line("    " + out.dim("due " + stamp.ljust(5)) + " " + item["title"])
        for item in cell["scheduled"]:
            stamp = item["defer_at"][11:16] if item.get("defer_at") else ""
            out.line("    " + out.dim("defer " + stamp.ljust(3)) + " " + item["title"])
        for item in cell["completed"]:
            out.line("    " + out.green("done") + "  " + out.dim(item["title"]))
    return {"ok": True, **data}


def cmd_agenda(args, out: Out, store: Store) -> dict:
    cells = core.agenda(store, days=args.days, start=args.start,
                        all_due=getattr(args, "all", False))
    if out.json_mode:
        return {"ok": True, "days": cells, "count": len(cells)}
    if not cells:
        out.line(out.green("agenda is clear"))
        return {"ok": True, "days": cells, "count": 0}
    for cell in cells:
        out.line(out.bold(cell["date"]) + "  " + out.dim(cell["weekday"]))
        for item in cell["overdue"]:
            out.line("  " + out.red("!") + " " + item["title"])
        for item in cell["due"]:
            out.line("  " + out.dim("due") + "   " + item["title"])
        for item in cell["scheduled"]:
            out.line("  " + out.dim("defer") + " " + item["title"])
        for item in cell["completed"]:
            out.line("  " + out.green("done") + "  " + out.dim(item["title"]))
    return {"ok": True, "days": cells, "count": len(cells)}


def _notify_send(title: str, body: str) -> bool:
    """Send one desktop notification, preferring Omarchy's notifier."""
    omarchy = shutil.which("omarchy-notification-send")
    if omarchy:
        cmd = [omarchy, "--app-name", "OmaGTD", "-u", "normal", title, body]
    else:
        notify = shutil.which("notify-send")
        if not notify:
            return False
        cmd = [notify, "-a", "OmaGTD", "-u", "normal", title, body]
    try:
        subprocess.run(cmd, check=False, timeout=5)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


# What a deadline notification says. The app name already files it under
# OmaGTD; this is the line the desktop shows, so it carries the plugin's own
# name too rather than the bare protocol the plugin happens to implement.
NOTIFY_TITLE = "OmaGTD · deadline reached"


def cmd_notify(args, out: Out, store: Store) -> dict:
    items = core.due_notifications(store)
    if out.json_mode:
        return {"ok": True, "count": len(items), "items": items}
    if not items:
        out.line(out.dim("nothing has expired"))
        return {"ok": True, "count": 0, "items": []}
    sent_ids = []
    for item in items:
        due = item.get("due_at") or ""
        body = (item.get("title") or "") + ("\ndue " + due if due else "")
        announced = args.no_send or _notify_send(NOTIFY_TITLE, body)
        if announced:
            sent_ids.append(item["id"])
        out.line(out.red("expired") + "  " + (item.get("title") or "")
                 + "  " + out.dim(due))
    if not args.dry_run:
        core.mark_notified(store, sent_ids)
    return {"ok": True, "count": len(items), "sent": len(sent_ids), "items": items}


def cmd_review(args, out: Out, store: Store) -> dict:
    if args.action in ("daily", "weekly"):
        report = review.daily(store) if args.action == "daily" else review.weekly(store)
        if args.mark:
            marked = review.mark(store, args.action, scope=args.scope)
            report["marked"] = marked
        if out.json_mode:
            return {"ok": True, **report}
        out.line(out.bold(("Daily" if args.action == "daily" else "Weekly")
                          + " review  ·  " + report["date"]))
        for step in report["steps"]:
            mark_text = out.green("ok") if step["ok"] else out.red(str(step["count"]))
            out.line(f"  {mark_text}  {step['question']}"
                     + (out.dim("  " + step["hint"]) if step["hint"] else ""))
            if not step["ok"] and step["count"]:
                out.line(out.dim(f"        gtd list {step['key']}"))
        due = report["due"]
        if due["due"]:
            out.line(out.bold(f"  review due ({due['unreviewed']} unreviewed)"))
            for scope in due["scopes"]:
                if scope["due"]:
                    out.line(f"    {scope['title']}  {scope['unreviewed']} unreviewed  "
                             f"{out.dim(scope['cadence'])}")
        stats = report.get("stats") or {}
        if "created_30" in stats:
            out.line(out.dim(f"  last 30 days: {stats['created_30']} created, "
                             f"{stats['closed_30']} closed"))
        history = review.history(store)
        out.line(out.dim(f"  last weekly review: {history['last_weekly'] or 'never'}"))
        if args.mark:
            out.line(out.green(f"  marked {report['marked']['marked']} items reviewed"))
        return {"ok": True, **report}

    if args.action == "mark":
        result = core.mark_reviewed(store, args.id, tag=args.tag, all_scopes=args.all)
        if out.json_mode:
            return {"ok": True, **result}
        out.line(f"{out.green('reviewed')}  {result['scope']} "
                 f"({result['marked']} items)")
        return {"ok": True, **result}

    if args.action == "due":
        due = review.due_reviews(store)
        if out.json_mode:
            return {"ok": True, **due}
        if not due["scopes"]:
            out.line(out.dim("no folder or context has a review cadence set "
                             "(gtd project review NAME weekly, gtd tag review NAME monthly)"))
        for scope in due["scopes"]:
            flag = out.yellow("review ") if scope["due"] else out.dim("ok")
            out.line(f"  {flag} {scope['title']}  {scope['unreviewed']} unreviewed  "
                     f"{out.dim(scope['cadence'])}")
        return {"ok": True, **due}

    if args.action == "history":
        history = review.history(store)
        if out.json_mode:
            return {"ok": True, **history}
        out.line(f"daily:  {history['last_daily'] or 'never'}"
                 + (f"  ({history['since_daily']}d ago)" if history["since_daily"] is not None else ""))
        out.line(f"weekly: {history['last_weekly'] or 'never'}"
                 + (f"  ({history['since_weekly']}d ago)" if history["since_weekly"] is not None else ""))
        return {"ok": True, **history}

    raise GtdError(f"unknown review action {args.action}")


def cmd_tree(args, out: Out, store: Store) -> dict:
    data = core.tree(store)
    if out.json_mode:
        return {"ok": True, **data}

    def line_action(action: dict, indent: str) -> None:
        marks = []
        if action.get("priority"):
            marks.append("!")
        if action.get("due_at"):
            marks.append(str(action["due_at"])[:10])
        marks.append(str(action.get("status") or ""))
        out.line(f"{indent}{action['title']}  {out.dim('  '.join(m for m in marks if m))}")

    for area in data["areas"]:
        if area["title"]:
            out.line(out.bold(str(area["title"])))
        for project in area["projects"]:
            done = int(project.get("completed") or 0)
            total = len(project.get("actions") or [])
            out.line(f"  {out.cyan(str(project['title']))}  "
                     f"{out.dim(f'{done}/{total} done')}")
            for action in project.get("actions") or []:
                line_action(action, "      ")
    if data["loose"]:
        out.line(out.bold("No project yet"))
        for action in data["loose"]:
            line_action(action, "  ")
    return {"ok": True}


def cmd_subtree(args, out: Out, store: Store) -> dict:
    node = core.subtree(store, args.id)
    if out.json_mode:
        return {"ok": True, "container": node}
    out.line(out.bold(str(node["title"] or node["id"])))
    for action in node.get("actions") or []:
        out.line(f"    {action['title']}")
    for project in node.get("projects") or []:
        out.line(f"  {out.cyan(str(project['title']))}  "
                 f"{out.dim(str(project.get('total') or 0) + ' open')}")
    return {"ok": True, "container": node}


def cmd_stats(args, out: Out, store: Store) -> dict:
    data = core.stats(store)
    if out.json_mode:
        return {"ok": True, **data}
    rows = [
        ("inbox", data["inbox"], "unclarified captures"),
        ("next", data["next"], "actions you could do right now"),
        ("today", data["today"], "due today or flagged"),
        ("overdue", data["overdue"], "past the deadline"),
        ("waiting", data["waiting"], "waiting on someone else"),
        ("someday", data["someday"], "incubated"),
        ("flagged", data["flagged"], "flagged"),
        ("reference", data["reference"], "kept, not done"),
        ("projects", data["projects_open"], "active projects"),
        ("stalled", data["stalled"], "projects with nothing to do"),
        ("done today", data["done_today"], "finished today"),
        ("created 30d", data["created_30"], "tasks that arrived in the last month"),
        ("closed 30d", data["closed_30"], "tasks finished or dropped in the last month"),
    ]
    width = max(len(label) for label, _, _ in rows)
    for label, value, hint in rows:
        colour = out.green if value == 0 else (
            out.red if label in ("overdue", "inbox") and value else out.bold)
        out.line(f"  {label.ljust(width)}  {colour(str(value).rjust(4))}  {out.dim(hint)}")
    return {"ok": True, **data}


def cmd_bar(args, out: Out, store: Store) -> dict:
    """The bar widget's poll. Cheap, and the only thing it ever needs."""
    data = core.bar_snapshot(store)
    if out.json_mode:
        return {"ok": True, **data}
    out.line(f"inbox {data['inbox']}  next {data['next']}")
    return {"ok": True, **data}


def cmd_settings(args, out: Out, store: Store) -> dict:
    """Show the knobs, or turn one: due_time, time_format, theme,
    auto_remove_*, calendar_show_completed."""
    if args.action == "set":
        value = core.set_setting(store, args.key, args.value)
        data = core.settings(store)
        if out.json_mode:
            return {"ok": True, **data}
        out.line(f"{args.key} = {value}")
        return {"ok": True, **data}

    data = core.settings(store)
    if out.json_mode:
        return {"ok": True, **data}
    out.line(out.bold("Settings"))
    out.line(f"  due_time      {data['due_time']}   a bare date lands on this clock")
    out.line(f"  time_format   {data['time_format']}   how clock times are shown")
    out.line(f"  theme         {data['theme']}   omarchy, of-light or of-dark")
    out.line(f"  auto_remove_empty_projects  {data['auto_remove_empty_projects']}   "
             "delete a project with nothing filed under it")
    out.line(f"  auto_remove_empty_contexts   {data['auto_remove_empty_contexts']}    "
              "delete a context with nothing on it")
    out.line(f"  done_retention_days      {data['done_retention_days']}    "
              "days a completed task is kept (0 = forever)")
    out.line(f"  calendar_show_completed   {data['calendar_show_completed']}     "
              "calendar green mark for completed tasks")
    return {"ok": True, **data}


def cmd_log(args, out: Out, store: Store) -> dict:
    rows = core.list_view(store, "log")[:args.limit]
    if out.json_mode:
        return {"ok": True, "count": len(rows), "entries": rows}
    out.line(out.bold(f"Log (last {len(rows)})"))
    for row in rows:
        when = dates.human(row["at"], long=True)
        out.line(f"  {out.dim(when.ljust(10))} {row['kind'].ljust(16)} "
                 f"{(row['title'] or row['detail'] or '')[:60]}")
    return {"ok": True, "count": len(rows), "entries": rows}


def cmd_trash(args, out: Out, store: Store) -> dict:
    if args.id and not args.list:
        item = core.trash(store, args.id)
        if out.json_mode:
            return {"ok": True, "item": item}
        out.line(f"{out.green('trashed')}  {item['title']} {out.dim('(' + item['id'] + ')')}")
        return {"ok": True, "item": item}

    items = core.list_view(store, "trash", limit=getattr(args, "limit", None))
    if out.json_mode:
        return {"ok": True, "count": len(items), "items": items}
    print_items(items, out, header=f"Trash ({len(items)})", empty="trash is empty")
    return {"ok": True, "count": len(items), "items": items}


def cmd_restore(args, out: Out, store: Store) -> dict:
    item = core.restore(store, args.id)
    if out.json_mode:
        return {"ok": True, "item": item}
    out.line(f"{out.green('restored')}  {item['title']} → {item['status']}")
    return {"ok": True, "item": item}


def cmd_purge(args, out: Out, store: Store) -> dict:
    result = core.purge(store, args.id, days=args.days, all_items=args.all)
    if out.json_mode:
        return {"ok": True, **result}
    out.line(f"{out.green('purged')}  {len(result['purged'])} item(s)")
    return {"ok": True, **result}


def cmd_undo(args, out: Out, store: Store) -> dict:
    """Take back the last change.

    Scoped to the item it is asked about, because that is what the `z` key
    in both front ends acts on: the task on screen. Without an id it takes
    back whatever happened last, which is what the terminal wants.
    """
    result = core.undo(store, args.id)
    if out.json_mode:
        return {"ok": True, **result}
    out.line(f"{out.green('undone')}  {result['reverted']}  "
             + out.dim("(%d item(s))" % result["count"]))
    return {"ok": True, **result}


def cmd_import(args, out: Out, store: Store) -> dict:
    result = importer.import_file(store, args.source, fmt=args.format,
                                  dry_run=args.dry_run, progress=not out.json_mode and not out.quiet)
    if out.json_mode:
        return {"ok": True, **result}
    head = "would import" if args.dry_run else "imported"
    out.line(f"{out.green(head)} {result['projects']} projects, "
             f"{result['actions']} actions, {result['tags']} contexts, "
             f"{result['skipped']} skipped")
    if result["warnings"]:
        for warning in result["warnings"][:20]:
            out.line(out.yellow("  ! ") + warning)
    return {"ok": True, **result}


def cmd_export(args, out: Out, store: Store) -> dict:
    if args.format == "zip":
        return _export_zip(args, out, store)

    text = importer.export_text(store, fmt=args.format)
    if args.output:
        with open(os.path.expanduser(args.output), "w", encoding="utf-8") as handle:
            handle.write(text)
        if out.json_mode:
            return {"ok": True, "output": os.path.expanduser(args.output),
                    "bytes": len(text)}
        out.line(f"{out.green('exported')}  {args.output}  {len(text)} bytes")
        return {"ok": True, "output": args.output, "bytes": len(text)}
    if args.format == "csv":
        sys.stdout.write(text)
    else:
        print(text)
    return {"ok": True, "bytes": len(text)}


def _export_zip(args, out: Out, store: Store) -> dict:
    """One file that holds everything: the database as it is, plus the same
    data as plain JSON so the archive can be read without this program."""
    if not args.output:
        if out.json_mode:
            return {"ok": False, "error": "zip export needs -o FILE"}
        out.line("a zip export needs -o FILE")
        return {"ok": False}

    target = os.path.expanduser(args.output)
    if not target.endswith(".zip"):
        target += ".zip"
    payload = importer.export_text(store, fmt="json")
    entries = [
        ("gtd.json", payload.encode("utf-8")),
        ("manifest.json", json.dumps({
            "format": 1,
            "program": "com.auino.omagtd",
            "exported_at": core.now(),
            "database": store.path.name,
            "stats": core.stats(store),
        }, indent=2, sort_keys=True).encode("utf-8")),
    ]
    if store.path.exists():
        # VACUUM into a temp copy first: the archive must be a consistent
        # snapshot, and a plain file copy of a live sqlite database is not.
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = os.path.join(tmp, store.path.name)
            dest = sqlite3.connect(snapshot)
            with dest:
                store.db.backup(dest)
            dest.close()
            with open(snapshot, "rb") as handle:
                entries.append((store.path.name, handle.read()))

    # Bundle the attached files, so an export can be restored somewhere the
    # originals never existed.
    seen_attachments = set()
    for row in store.query("SELECT item_id, uri FROM attachments"):
        uri = str(row["uri"] or "")
        # The row holds the path relative to the store; the file it names is
        # only reachable once that is put back against this database's home.
        path = core.attachment_path(store, uri)
        if path is None:
            continue
        entry = importer._attachment_file_name(str(row["item_id"]), uri)
        if entry in seen_attachments or not path.exists():
            continue
        seen_attachments.add(entry)
        with open(path, "rb") as handle:
            entries.append((entry, handle.read()))

    os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, blob in entries:
            archive.writestr(name, blob)

    size = os.path.getsize(target)
    if out.json_mode:
        return {"ok": True, "output": target, "bytes": size,
                "entries": [name for name, _ in entries]}
    out.line(f"{out.green('exported')}  {target}  {size} bytes  "
             f"{len(entries)} files")
    return {"ok": True, "output": target, "bytes": size}


def _db_info(store: Store, items: int | None = None) -> dict:
    return {
        "path": str(store.path),
        "exists": store.path.exists(),
        "size": store.path.stat().st_size if store.path.exists() else 0,
        "schema": store.meta("schema"),
        "sqlite": store.scalar("SELECT sqlite_version()"),
        "items": core.stats(store)["open_items"] if items is None else items,
    }


def cmd_db(args, out: Out, store: Store) -> dict:
    if args.action == "path":
        if out.json_mode:
            return {"ok": True, "path": str(store.path)}
        out.line(str(store.path))
        return {"ok": True, "path": str(store.path)}

    if args.action == "info":
        info = _db_info(store)
        if out.json_mode:
            return {"ok": True, **info}
        out.line(out.bold(str(info["path"])))
        out.line(f"  sqlite {info['sqlite']}  schema {info['schema']}  "
                 f"{info['size'] / 1024:.0f} KiB")
        out.line(f"  {info['items']} open items")
        return {"ok": True, **info}

    if args.action == "vacuum":
        store.run("VACUUM")
        if out.json_mode:
            return {"ok": True}
        out.line(out.green("vacuumed"))
        return {"ok": True}

    if args.action == "backup":
        target = os.path.expanduser(args.output) if args.output else str(
            store.path.with_name(f"omagtd-backup-{time.strftime('%Y%m%d-%H%M%S')}.sqlite3"))
        target = str(os.path.expanduser(target))
        # WAL means the .sqlite3 file alone is not always the whole story;
        # sqlite's own backup API copies a consistent snapshot regardless.
        destination = sqlite3.connect(target)
        try:
            store.db.backup(destination)
        finally:
            destination.close()
        if out.json_mode:
            return {"ok": True, "backup": target}
        out.line(f"{out.green('backed up')}  {target}")
        return {"ok": True, "backup": target}

    if args.action == "restore":
        source = os.path.expanduser(args.source)
        if not os.path.exists(source):
            raise GtdError(f"no such backup: {source}")
        for suffix in ("-wal", "-shm"):
            stray = store.path.with_name(store.path.name + suffix)
            if stray.exists():
                stray.unlink()
        shutil.copyfile(source, store.path)
        if out.json_mode:
            return {"ok": True, "path": str(store.path)}
        out.line(f"{out.green('restored')}  {source} → {store.path}")
        return {"ok": True, "path": str(store.path)}

    if args.action == "init":
        return cmd_init(args, out, store)

    raise GtdError(f"unknown db action {args.action}")


def cmd_state(args, out: Out, store: Store) -> dict:
    """One snapshot for the whole panel: everything that does not depend on
    which view is selected, plus (optionally) one page of that view.

    The fullscreen view redraws from this in a single process instead of
    asking for settings, stats, contexts, the tree and the database one after
    another. The selected list rides along when ``--view`` names it, so a
    refresh is still one call; a later page is just another ``state`` with a
    larger ``--offset``.
    """
    settings = core.settings(store)
    counts = core.stats(store)
    contexts = core.list_view(store, "tags")
    tree = core.containers(store)
    db = _db_info(store, items=counts["open_items"])

    payload: dict = {
        "ok": True,
        "settings": settings,
        "stats": counts,
        "contexts": contexts,
        "areas": tree["areas"],
        "loose_count": tree["loose_count"],
        "db": db,
    }

    if getattr(args, "view", None):
        offset = getattr(args, "offset", 0) or 0
        items = core.list_view(store, args.view, limit=args.limit, offset=offset,
                               project=getattr(args, "project", None),
                               area=getattr(args, "area", None),
                               tag=getattr(args, "tag", None))
        payload["view"] = {
            "key": args.view,
            "count": len(items),
            "offset": offset,
            "limit": args.limit,
            "items": items,
        }

    if out.json_mode:
        return payload

    out.line(out.bold("State"))
    out.line(f"  {counts['open_items']} open items  "
             f"{len(contexts)} contexts  schema {db['schema']}")
    for area in tree["areas"]:
        if area["title"]:
            out.line(f"  {area['title']}  {len(area['projects'])} projects")
    view = payload.get("view")
    if view:
        out.line(out.bold(f"{view['key']}  ({view['count']})"))
        print_items(view["items"], out, width=args.width)
    return payload


def cmd_quick(args, out: Out, store: Store) -> dict:
    """Parse a quick-entry line without saving it. Handy for tuning syntax."""
    parsed = quick.parse(args.text, core.container_names(store))
    if out.json_mode:
        return {"ok": True, **parsed}
    out.line(json.dumps(parsed, indent=2, default=str))
    return {"ok": True, **parsed}


# ---------------------------------------------------------------- parser


def _today(args, out: Out, store: Store) -> dict:
    items = core.list_view(store, "today", limit=args.limit, project=args.project,
                           tag=args.tag)
    if out.json_mode:
        return {"ok": True, "view": "today", "count": len(items), "items": items}
    print_items(items, out, header=f"Today ({len(items)})", empty="nothing due today",
                width=args.width)
    return {"ok": True, "view": "today", "count": len(items), "items": items}


def build_parser() -> argparse.ArgumentParser:
    # The shared options are declared with SUPPRESS defaults on purpose. A
    # subparser inherits them from `common`, and an inherited default would
    # silently overwrite a flag given before the subcommand — `gtd --json list
    # inbox` would print a table because the `list` subparser re-applied
    # `json=False`. With SUPPRESS an option only exists if it was typed, and
    # `main` fills the gaps in.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS,
                        help="path to the sqlite3 database")
    common.add_argument("--profile", default=argparse.SUPPRESS,
                        help="named database under $GTD_HOME/profiles")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable output")
    common.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS,
                        help="print nothing but errors")
    common.add_argument("--width", type=int, default=argparse.SUPPRESS,
                        help=argparse.SUPPRESS)

    parser = argparse.ArgumentParser(
        prog="gtd",
        description="Getting Things Done for Omarchy, modelled on OmniFocus.",
        epilog="The five steps: capture, clarify, organize, review, engage.",
        parents=[common],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"gtd {__version__}")
    sub = parser.add_subparsers(dest="command")

    def add(name, handler, help_text, description=None, aliases=()):
        node = sub.add_parser(name, parents=[common], help=help_text,
                              description=description, aliases=list(aliases),
                              formatter_class=argparse.RawDescriptionHelpFormatter)
        node.set_defaults(handler=handler)
        return node

    def engage_flags(node, positional_when=False):
        """The flags every "do something about an item" command shares."""
        node.add_argument("id", help="id, unique prefix, or 'last'")
        if positional_when:
            node.add_argument("when")
        else:
            node.add_argument("--when", help="date for defer, or completion date")
        node.add_argument("--priority", "-p", type=int, choices=(0, 1),
                          help="1 flags it, 0 clears it")
        node.add_argument("--notes", "-n")
        node.add_argument("--who", help="who you are waiting on")
        node.add_argument("--also-close", action="store_true",
                          help="close the project if this was its last action")
        return node

    def organizing_flags(node, with_notes=True):
        node.add_argument("--project", "-P", help="move into this project")
        node.add_argument("--area", "-A", help="move into this area")
        node.add_argument("--tag", "-t", action="append", help="add a context")
        node.add_argument("--defer", "-d",
                          help="defer date: today, tomorrow, fri, +2w, 2026-10-05")
        node.add_argument("--due", help="deadline")
        node.add_argument("--duration", help="estimated effort: 30m, 1h, 1h30m")
        node.add_argument("--priority", "-p", type=int, choices=(0, 1),
                          help="1 flags it, 0 clears it")
        if with_notes:
            node.add_argument("--notes", "-n", help="replace the note")
        node.add_argument("--link", help="a URL or file path the item points at")
        node.add_argument("--unlink", action="store_true", help="clear the link")
        node.add_argument("--attach", action="append", metavar="URI",
                          help="attach a file or URL (repeatable)")
        node.add_argument("--unattach", action="append", metavar="URI",
                          help="detach a file or URL (repeatable)")
        node.add_argument("--review", choices=core.REVIEW_INTERVALS,
                          help="review cadence for this item")
        return node

    add("init", cmd_init, "create the database if it does not exist")

    node = add("capture", cmd_capture, "step 1: capture something",
               description=(
                   "Capture a next action. Quick-entry syntax works here:\n"
                   "  !                flag it\n"
                   "  #errands          context\n"
                   "  #home>kitchen     subcontext (#parent>child nests)\n"
                   "  /Garden fence/    project (only if one exists by that name)\n"
                   "  <tomorrow         start / defer date\n"
                   "  >friday           deadline\n"
                   "  =Ana              waiting for\n"
                   "  ~every week       repeat rule\n"
                   "Everything not named lands in the inbox."))
    node.add_argument("text", nargs="+", help="what you want to do")
    node.add_argument("--inbox", action="store_true", help="force into the inbox")
    node.add_argument("--who", help="waiting for this person")
    node.add_argument("--repeat", help="repeat rule: 'every week', 'every other month'")
    organizing_flags(node)

    node = add("list", cmd_list, "list a view",
               description="views: " + ", ".join(sorted(core.VIEW_SQL))
                           + ", tags, log")
    node.add_argument("view")
    node.add_argument("--limit", type=int, help="maximum rows")
    node.add_argument("--offset", type=int, default=0, help="rows to skip")
    node.add_argument("--project", "-P")
    node.add_argument("--area", "-A")
    node.add_argument("--tag", "-t")

    node = add("search", cmd_search, "search titles, notes, projects, contexts…",
               description="One free-text query across task titles and notes, "
                           "project and area names, contexts, people "
                           "and attachment paths.")
    node.add_argument("query", nargs="+", help="what to look for")
    node.add_argument("--limit", type=int, default=50)
    node.add_argument("--offset", type=int, default=0, help="rows to skip")

    node = add("state", cmd_state, "one snapshot for the panel",
               description="Settings, stats, contexts, the area/project tree "
                           "and database facts in a single call; add --view to "
                           "ride one page of a list along.")
    node.add_argument("--view")
    node.add_argument("--limit", type=int, default=200)
    node.add_argument("--offset", type=int, default=0, help="rows to skip")
    node.add_argument("--project", "-P")
    node.add_argument("--area", "-A")
    node.add_argument("--tag", "-t")

    node = add("complete", cmd_complete, "finish a half-typed name",
               description="Complete a project, context or waiting-for name "
                           "against the ones already in use. Only fires when "
                           "exactly one name starts with the text you typed.")
    node.add_argument("kind", choices=core.COMPLETION_KINDS,
                      help="which kind of name to complete")
    node.add_argument("prefix", help="what has been typed so far")

    node = add("focus", cmd_focus, "step 5: what can I do right now?")
    node.add_argument("--ten", action="store_true", help="just the next ten")
    node.add_argument("--limit", type=int, default=50)
    node.add_argument("--project", "-P")
    node.add_argument("--tag", "-t")

    node = add("today", _today, "what is on for today")
    node.add_argument("--limit", type=int, default=50)
    node.add_argument("--project", "-P")
    node.add_argument("--tag", "-t")

    node = add("show", cmd_show, "everything about one item")
    node.add_argument("id", help="id, unique prefix, or 'last'")

    node = add("attach", cmd_attach, "attach files or URLs to an item")
    node.add_argument("id")
    node.add_argument("uri", nargs="+", help="file paths or URLs")
    node.add_argument("--name", help="a friendly name for the attachment")

    node = add("unattach", cmd_unattach, "detach files or URLs from an item")
    node.add_argument("id")
    node.add_argument("target", nargs="+", help="attachment id, uri, or filename")

    node = add("edit", cmd_organize, "step 3: organize an item",
               description="Give an item its home, context, timing and flags.")
    node.add_argument("id")
    node.add_argument("--title", help="rename the item")
    node.add_argument("--untag", action="append", help="remove a context")
    node.add_argument("--undefer", action="store_true", help="clear the defer date")
    node.add_argument("--undue", action="store_true", help="clear the deadline")
    node.add_argument("--clear-duration", action="store_true",
                      help="clear the effort estimate")
    node.add_argument("--inbox", action="store_true", help="send back to the inbox")
    node.add_argument("--onhold", action="store_true", help="pause this container")
    node.add_argument("--repeat", help="set a repeat rule")
    node.add_argument("--no-repeat", action="store_true", help="stop repeating")
    node.add_argument("--who", help="mark as waiting for this person")
    organizing_flags(node)

    node = add("clarify", cmd_clarify, "step 2: decide what an inbox item is",
               description="outcomes: " + " | ".join(core.CLARIFY_OUTCOMES))
    node.add_argument("id")
    node.add_argument("outcome")
    node.add_argument("--project", "-P")
    node.add_argument("--area", "-A")
    node.add_argument("--tag", "-t", action="append")
    node.add_argument("--defer", "-d")
    node.add_argument("--due")
    node.add_argument("--priority", "-p", type=int, choices=(0, 1))
    node.add_argument("--notes", "-n")
    node.add_argument("--who", help="who you are waiting on (outcome=wait)")
    node.add_argument("--review", choices=core.REVIEW_INTERVALS)

    node = add("triage", cmd_triage, "step 2: work through the inbox",
               description="Print the clarify queue, oldest first, with the "
                           "decision each item is waiting on.")
    node.add_argument("--limit", type=int, default=50)

    node = add("engage", cmd_engage, "step 5: do something about an item",
               description="actions: " + " | ".join(core.ENGAGE_ACTIONS))
    engage_flags(node)
    node.add_argument("action", choices=core.ENGAGE_ACTIONS,
                       help="what to do: " + " | ".join(core.ENGAGE_ACTIONS))

    # Convenience verbs: `gtd done 3f2a` reads better than `gtd engage 3f2a done`.
    for verb, action in (("done", "done"), ("drop", "drop"), ("start", "start"),
                         ("stop", "stop"), ("resume", "resume"),
                         ("flag", "flag"), ("unflag", "unflag")):
        node = add(verb, cmd_engage, f"{action} an item")
        engage_flags(node)
        node.set_defaults(action=action)

    node = add("defer", cmd_engage, "push an action into the future")
    engage_flags(node, positional_when=True)
    node.set_defaults(action="defer")

    node = add("postpone", cmd_postpone,
                "roll an action's deadline one day later, keeping its time",
                aliases=("roll",))
    node.add_argument("id")
    node.add_argument("--days", type=int, default=1,
                      help="how many days to roll forward (default 1)")

    node = add("open", cmd_open,
               "open the item's link in the desktop's default viewer")
    node.add_argument("id")
    node.add_argument("--print", action="store_true",
                      help="resolve and print the link without opening it")

    node = add("wait", cmd_engage, "an action someone else owes you")
    node.add_argument("id")
    node.add_argument("who", nargs="?",
                      help="who owes it; omit to wait without naming anyone")
    node.add_argument("--when")
    node.add_argument("--priority", "-p", type=int, choices=(0, 1))
    node.add_argument("--notes", "-n")
    node.add_argument("--also-close", action="store_true")
    node.set_defaults(action="wait")

    # ---- containers: projects and areas
    for group in ("project", "area"):
        plural = group + "s"
        node = sub.add_parser(group, parents=[common], aliases=[plural],
                              help=f"manage {group}s")
        node.set_defaults(handler=cmd_project, group=group)
        actions = node.add_subparsers(dest="action", required=True)

        def child(name, help_text):
            leaf = actions.add_parser(name, parents=[common], help=help_text)
            leaf.set_defaults(handler=cmd_project, group=group)
            return leaf

        add_node = child("add", f"create a {group}")
        add_node.add_argument("name")
        add_node.add_argument("--area", help="area for a project")
        add_node.add_argument("--project", dest="project",
                              help="nest this project inside another project")
        add_node.add_argument("--review", choices=core.REVIEW_INTERVALS, default="never")

        list_node = child("list", f"list {group}s")
        list_node.add_argument("--limit", type=int)
        list_node.add_argument("--offset", type=int, default=0, help="rows to skip")

        close_node = child("close", f"close a {group}")
        close_node.add_argument("name")
        close_node.add_argument("--keep", action="store_true",
                                help="do not drop what is still inside")

        reopen_node = child("reopen", f"reopen a closed {group}")
        reopen_node.add_argument("name")

        cadence_node = child("review", "set a review cadence (OmniFocus Review:)")
        cadence_node.add_argument("name")
        cadence_node.add_argument("interval", choices=core.REVIEW_INTERVALS)

        if group == "project":
            grandchild = child("add-action", "add an action to a project")
            grandchild.add_argument("name")
            grandchild.add_argument("action_title")
            grandchild.add_argument("--tag", "-t", action="append")
            grandchild.add_argument("--defer", "-d")
            grandchild.add_argument("--due")
            grandchild.add_argument("--priority", "-p", type=int, choices=(0, 1))

    # ---- contexts (tags)
    node = sub.add_parser("tag", parents=[common], aliases=["context", "tags"],
                          help="manage contexts (tags)")
    node.set_defaults(handler=cmd_tag)
    actions = node.add_subparsers(dest="action", required=True)

    def tag_child(name, help_text):
        leaf = actions.add_parser(name, parents=[common], help=help_text)
        leaf.set_defaults(handler=cmd_tag)
        return leaf

    tag_add = tag_child("add", "create a context")
    tag_add.add_argument("name")
    tag_add.add_argument("--on", help="also tag this item id")
    tag_list = tag_child("list", "list contexts with open counts")
    tag_list.add_argument("--limit", type=int)
    tag_list.add_argument("--offset", type=int, default=0, help="rows to skip")
    tag_rm = tag_child("rm", "delete a context")
    tag_rm.add_argument("name")
    tag_review = tag_child("review", "set a review cadence")
    tag_review.add_argument("name")
    tag_review.add_argument("interval", choices=core.REVIEW_INTERVALS)


    # ---- step 4: reflect
    node = sub.add_parser("review", parents=[common], help="step 4: reflect")
    node.set_defaults(handler=cmd_review)
    actions = node.add_subparsers(dest="action", required=True)

    for name, help_text in (("daily", "the daily review"),
                            ("weekly", "the GTD weekly review checklist"),
                            ("due", "OmniFocus-style review counters"),
                            ("history", "when reviews last happened")):
        leaf = actions.add_parser(name, parents=[common], help=help_text)
        leaf.set_defaults(handler=cmd_review)
        if name in ("daily", "weekly"):
            leaf.add_argument("--mark", action="store_true",
                              help="mark everything reviewed afterwards")
            leaf.add_argument("--scope", help="only mark this project or area")

    mark_node = actions.add_parser("mark", parents=[common],
                                   help="mark a container or context reviewed")
    mark_node.set_defaults(handler=cmd_review)
    mark_node.add_argument("id", nargs="?")
    mark_node.add_argument("--tag")
    mark_node.add_argument("--all", action="store_true")

    add("stats", cmd_stats, "the numbers")
    add("bar", cmd_bar, "the bar widget's poll (json)")
    node = add("tree", cmd_tree, "areas, projects and their actions (the fullscreen view)")

    node = add("subtree", cmd_subtree, "one area or project's branch, with its actions")
    node.add_argument("id", help="the area or project id")

    node = add("settings", cmd_settings, "show or change preferences")
    node.add_argument("action", nargs="?", choices=("show", "set"), default="show")
    node.add_argument("key", nargs="?",
                        help="due_time, time_format, theme or auto_remove_*")
    node.add_argument("value", nargs="?")

    node = add("calendar", cmd_calendar, "month or range view of commitments",
               aliases=("cal",))
    node.add_argument("--month", help="a calendar month, YYYY-MM")
    node.add_argument("--days", type=int, default=31, help="day count from --start")
    node.add_argument("--start", help="first day, YYYY-MM-DD (default today)")
    node.add_argument("--all", action="store_true",
                      help="stretch the window to cover every open deadline")

    node = add("agenda", cmd_agenda, "the next few busy days")
    node.add_argument("--days", type=int, default=7)
    node.add_argument("--start", help="first day, YYYY-MM-DD (default today)")
    node.add_argument("--all", action="store_true",
                      help="stretch the window to cover every open deadline")

    node = add("notify", cmd_notify, "announce deadlines that have been reached",
               description=(
                   "Every open action whose deadline has passed and that has not "
                   "been announced yet. Each is stamped so it fires once; moving "
                   "a deadline forward and letting it pass again announces it "
                   "again."))
    node.add_argument("--dry-run", action="store_true",
                      help="report without marking or sending anything")
    node.add_argument("--no-send", action="store_true",
                      help="print the items instead of a desktop notification")

    node = add("log", cmd_log, "the history")
    node.add_argument("--limit", type=int, default=30)

    node = add("trash", cmd_trash, "move an item to the trash (or list it)")
    node.add_argument("id", nargs="?", help="id, unique prefix, or 'last'")
    node.add_argument("--list", action="store_true", help="show the trash instead")

    node = add("restore", cmd_restore, "bring something back from the trash")
    node.add_argument("id")

    node = add("purge", cmd_purge, "empty the trash")
    node.add_argument("id", nargs="?")
    node.add_argument("--days", type=int, help="only what has been there longer")
    node.add_argument("--all", action="store_true", help="the whole trash")

    node = add("undo", cmd_undo, "take back the last change",
               description=("With an id, the most recent change that touched "
                            "that item; without one, whatever happened last. "
                            "One command is one undo, so repeating it walks "
                            "further back."))
    node.add_argument("id", nargs="?", help="id, unique prefix, or 'last'")

    node = add("import", cmd_import, "import from OmniFocus, CSV, JSON or a zip export")
    node.add_argument("source")
    node.add_argument("--format", choices=("auto", "omnifocus", "ofxml", "ofocusmx",
                                           "csv", "json", "zip"), default="auto")
    node.add_argument("--dry-run", action="store_true", help="report without writing")

    node = add("export", cmd_export, "export everything")
    node.add_argument("--format", choices=("json", "csv", "ofxml", "zip"), default="json")
    node.add_argument("-o", "--output", help="write to this file")

    node = add("db", cmd_db, "database maintenance")
    node.add_argument("action", choices=("path", "info", "vacuum", "backup",
                                         "restore", "init"))
    node.add_argument("--output", "-o", help="backup destination")
    node.add_argument("source", nargs="?")

    node = add("quick", cmd_quick, "parse a quick-entry line without saving")
    node.add_argument("text")

    return parser


GLOBAL_DEFAULTS = {"db": None, "profile": None, "json": False,
                   "quiet": False, "width": 62}


def _undo_label(args) -> str:
    """What an undo entry is called in the history: the verb, its action and
    the reference it was given, enough to name the change and no more."""
    command = str(getattr(args, "command", "") or "")
    action = str(getattr(args, "action", "") or "")
    ref = str(getattr(args, "id", "") or "")
    bits = [command]
    if action and action != command:
        bits.append(action)
    if ref and ref != action:
        bits.append(ref)
    return " ".join(b for b in bits if b)


def main(argv=None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(argv)
    for key, value in GLOBAL_DEFAULTS.items():
        if not hasattr(args, key):
            setattr(args, key, value)
    out = Out(args)

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    try:
        store = Store(path=args.db, profile=args.profile)
    except Exception as exc:  # unreadable path, permissions, a directory
        out.fail(f"cannot open the database: {exc}")
        return 2

    knobs = core.settings(store)
    dates.configure(due_time=knobs["due_time"], time_format=knobs["time_format"])

    try:
          # The trash empties itself; nothing here should have to remember to.
         core.sweep_trash(store)
    except sqlite3.Error:
         pass   # a sweep failure must never block the command that was asked for

    try:
          # Finished tasks empty on the same terms, on the retention setting.
         core.sweep_done(store)
    except sqlite3.Error:
         pass   # a sweep failure must never block the command that was asked for

    try:
        handler = getattr(args, "handler", None)
        if handler is None:
            parser.print_help()
            return 0
        # Undo is journaled per command: one entry for everything the handler
        # changed, nothing at all if it failed or changed nothing.
        core.undo_begin(store)
        result = handler(args, out, store)
        core.undo_commit(store, _undo_label(args))
    except GtdError as exc:
        core.undo_cancel(store)
        store.close()
        out.fail(str(exc), reason=exc.reason)
        return 2
    except KeyboardInterrupt:
        core.undo_cancel(store)
        store.close()
        return 130
    except BrokenPipeError:
        core.undo_cancel(store)
        return 0
    except Exception as exc:  # unexpected: report the type, not just the message
        core.undo_cancel(store)
        store.close()
        out.fail(f"{type(exc).__name__}: {exc}")
        return 1

    try:
        # Empty projects and contexts remove themselves, after the command that
        # may have just emptied one — and after the command that may have just
        # turned the setting on, so the switch takes effect on the spot.
        core.sweep_empty(store)
    except sqlite3.Error:
        pass  # a prune failure must never turn a working command into an error

    store.close()
    if out.json_mode:
        out.data(result if isinstance(result, dict) else {"ok": True})
    return 0
