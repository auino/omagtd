"""Export the commitments of one day to a text file.

This is the only place that knows how a day reads as a document: the markdown,
JSON, YAML and PDF each carry the same task list and its export stamp, differ
only in the dressing. The CLI handler in :mod:`gtd.cli` collects the day's
items and hands them here; the four renderers below are pure functions of that
data, so a test can compare a markdown export against a JSON export of the very
same day without a database in the way.
"""

from __future__ import annotations

import datetime as _dt
import json

from . import core, dates
from .core import GtdError

EXPORT_FORMATS = ("md", "json", "yaml", "pdf")

# What the export files recognise as one of the four formats, by suffix.
_FORMAT_BY_SUFFIX = {
    ".md": "md", ".markdown": "md",
    ".json": "json",
    ".yaml": "yaml", ".yml": "yaml",
    ".pdf": "pdf",
}

# The one suffix a name with no suffix is given, per format.
_FORMAT_BY_SUFFIX_INV = {"md": ".md", "json": ".json", "yaml": ".yaml", "pdf": ".pdf"}

# The order a day's groups come down the page: what is past its deadline first,
# then what is due today, then work whose defer window has opened, and finally
# what got finished. The same order the calendar and agenda print.
_GROUPS = ("overdue", "due", "scheduled", "completed")

_GROUP_HEADING = {
    "overdue": "Overdue",
    "due": "Due today",
    "scheduled": "Scheduled here",
    "completed": "Completed",
}


def format_for(filename: str | None, requested: str | None = None) -> str:
    """Pick a format from the explicit request or the file's suffix.

    A request named first (so `gtd export-day --format pdf -o x.txt` honours the
    flag and appends the right suffix); a recognisable suffix next; and a plain
    "md" last, when neither says otherwise. A `requested` of None means the user
    named no format, so the suffix gets its turn before the default is reached.
    """
    if requested in EXPORT_FORMATS:
        return requested
    if filename:
        lower = filename.lower()
        dot = lower.rfind(".")
        suffix = lower[dot:] if dot >= 0 else ""
        if suffix in _FORMAT_BY_SUFFIX:
            return _FORMAT_BY_SUFFIX[suffix]
    return "md"


def _resolve_day(text: str | None) -> str:
    """The ISO date a `--date` value means, or today when none is given.

    "today", "tomorrow", "+2w", "5 oct" and "2026-10-05" all resolve through the
    same parser the rest of the program uses, so the day a user names is the day
    the program would open for them.
    """
    if text:
        when = dates.parse_when(text)
        if when is None:
            raise GtdError(f"not a date I can read: {text!r}")
        return when.date().isoformat()
    return core.today()


def day_export(store, *text: str, groups: tuple[str, ...] = _GROUPS,
               include_meta: bool = True) -> dict:
    """The day's commitments as one structured document.

    Each group holds the items :func:`core.calendar` would file under it. The
    same dictionary drives every renderer, and a date that is the day it was
    taken for, not the day it happens to be read on: re-running the export the
    next morning still reports the day the file names.
    """
    day = _resolve_day(text[0] if text else None)
    data = core.calendar(store, start=day, end=day)
    cell = data["days"][0]

    items = []
    for group in groups:
        for entry in cell.get(group) or []:
            item = {
                "id": entry.get("id"),
                "title": entry.get("title") or "",
                "status": entry.get("status"),
                "priority": entry.get("priority") or 0,
                "group": group,
            }
            for field in ("defer_at", "due_at", "duration", "url",
                          "completed_at", "notes", "parent_title", "area_title"):
                value = entry.get(field)
                if value:
                    item[field] = value
            if include_meta:
                item["tags"] = core.tags_of(store, entry.get("id")) or []
            items.append(item)

    document = {
        # The day the document is about, and the moment it was written. The
        # latter is what makes an export a record of when it was taken; an
        # "as of" line a reader can trust without opening the database again.
        "date": day,
        "weekday": cell.get("weekday", _dt.date.fromisoformat(day).strftime("%A")),
        "exported_at": now_stamp(),
        "groups": {name: [item for item in items if item["group"] == name]
                   for name in groups},
        "items": items,
        "counts": {name: len(cell.get(name) or []) for name in groups},
    }
    return document


def now_stamp() -> str:
    """The export's stamp, 'YYYY-MM-DD HH:MM', the same shape the rest of the
    program keeps its timestamps in."""
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def render(document: dict, fmt: str, *, title: str | None = None) -> bytes:
    """Render a day document to its chosen format, as bytes.

    The text formats are UTF-8; the PDF is its own binary. An unknown format
    refuses rather than guessing, the way an unrecognised file would.
    """
    if fmt == "md":
        return render_markdown(document, title=title or "").encode("utf-8")
    if fmt == "json":
        return render_json(document).encode("utf-8")
    if fmt == "yaml":
        return render_yaml(document).encode("utf-8")
    if fmt == "pdf":
        return render_pdf(document, title=title or "")
    raise GtdError(f"unknown export format: {fmt!r}")


# -------------------------------------------------------------- markdown


def _item_markdown(item: dict) -> list[str]:
    lines = []
    flag = "!" if item.get("priority") else " "
    bullet = "• "
    stamp = item.get("due_at") or item.get("defer_at")
    when = dates.human(stamp) if stamp and item["group"] != "completed" else ""
    line = f"{bullet}{flag} {item['title']}".rstrip()
    if when:
        line += f"  ({when})"
    lines.append(line)
    if item.get("parent_title"):
        lines.append(f"    _in {item['parent_title']}_")
    if item.get("area_title") and item["area_title"] != item.get("parent_title"):
        lines.append(f"    _area {item['area_title']}_")
    if item.get("tags"):
        lines.append("    " + ", ".join("#" + tag for tag in item["tags"]))
    if item.get("duration"):
        lines.append(f"    _{dates.human_minutes(int(item['duration']))}_"
                     + " estimated")
    if item.get("url"):
        lines.append(f"    <{item['url']}>")
    if item.get("completed_at") and item["group"] == "completed":
        lines.append("    _completed " + _stamp_day(item["completed_at"]) + "_")
    if item.get("notes"):
        lines.append("    " + " ".join(str(item["notes"]).split())
                     if len(str(item["notes"]).split()) < 6
                     else "    " + _wrap(str(item["notes"])))
    return lines


def _stamp_day(stamp: str) -> str:
    text = str(stamp)[:10]
    try:
        return _dt.date.fromisoformat(text).strftime("%d %b %Y")
    except ValueError:
        return text


def _wrap(text: str, width: int = 72) -> str:
    words = text.split()
    out, line = [], ""
    for word in words:
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = word
        else:
            line = (line + " " + word).strip()
    if line:
        out.append(line)
    return "  ".join(out)


def render_markdown(document: dict, *, title: str = "") -> str:
    heading = title or f"OmaGTD — {document['weekday']}, {document['date']}"
    lines = [f"# {heading}",
             "",
             f"_Exported {document['exported_at']}._",
             ""]
    total = sum(document["counts"].values())
    detail = " · ".join(f"{document['counts'][name]} "
                        + "of " + _GROUP_HEADING[name].lower().rstrip("d")
                        for name in document["groups"] if document["counts"][name])
    lines.append(f"{total} item" + ("" if total == 1 else "s")
                 + (f" — {detail}" if detail else ""))
    lines.append("")
    any_items = False
    for name in document["groups"]:
        rows = document["groups"][name]
        if not rows:
            continue
        any_items = True
        lines.append(f"## {_GROUP_HEADING[name]}")
        lines.append("")
        for item in rows:
            lines.extend(_item_markdown(item))
            lines.append("")
    if not any_items:
        lines.append("Nothing on the calendar for this day.")
    return "\n".join(lines).rstrip() + "\n"


# -------------------------------------------------------------- JSON


def render_json(document: dict) -> str:
    return json.dumps(document, indent=2, sort_keys=True, default=str,
                      ensure_ascii=False) + "\n"


# -------------------------------------------------------------- YAML


def _yaml_scalar(value) -> str:
    """One scalar line, always safe to read back.

    Strings are single-quoted and their single quotes doubled, the one escaping
    a YAML reader unambiguously reverses. Numbers, booleans and null stay bare.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    return "'" + text.replace("'", "''") + "'"


def _yaml_block(value, indent: int = 0) -> list[str]:
    pad = "  " * indent
    lines = []
    if isinstance(value, dict):
        if not value:
            return [pad + "{}" if indent else "{}"]
        for key, inner in value.items():
            if isinstance(inner, (dict, list)) and inner:
                lines.append(f"{pad}{key}:")
                lines.extend(_yaml_block(inner, indent + 1))
            elif isinstance(inner, (dict, list)):
                lines.append(f"{pad}{key}: " + ("{}" if isinstance(inner, dict) else "[]"))
            else:
                lines.append(f"{pad}{key}: " + _yaml_scalar(inner))
        return lines
    if isinstance(value, list):
        if not value:
            return []
        for inner in value:
            if isinstance(inner, (dict, list)) and inner:
                lines.append(f"{pad}-")
                lines.extend(_yaml_block(inner, indent + 1))
            else:
                lines.append(f"{pad}- " + _yaml_scalar(inner))
        return lines
    return [pad + _yaml_scalar(value)]


def render_yaml(document: dict) -> str:
    lines = _yaml_block(document)
    return "\n".join(lines) + "\n"


# -------------------------------------------------------------- PDF


def _pdf_escape(text: str) -> str:
    out = []
    for ch in str(text):
        if ch == "\\" or ch == "(" or ch == ")":
            out.append("\\" + ch)
        elif ord(ch) < 0x20:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def _pdf_page_stream(lines: list[str]) -> bytes:
    """A page's content stream: each line at 11pt, 14pt leading.

    `BT...ET` opens a text block; each `(text) Tj` paints one line and `T*`
    drops to the next baseline. Text is escaped and written in CP-1252, the
    encoding a Type1 font expects, so a stray character cannot corrupt the file.
    """
    stream = "BT\n/F1 11 Tf\n14 TL\n72 720 Td\n"
    for row in lines:
        stream += "(" + _pdf_escape(row) + ") Tj\nT*\n"
    stream += "ET"
    return stream.encode("cp1252", errors="replace")


def _pdf_pages(document: dict, title: str) -> list[bytes]:
    """Break the day into page content streams.

    The heading, the export stamp and a one-line count open every sheet, so a
    printed page still says what it is and when it was taken. The four groups
    follow in the order the calendar prints them.
    """
    lines: list[str] = []
    heading = title or f"OmaGTD - {document['weekday']}, {document['date']}"
    lines.append(heading)
    lines.append("Exported " + document["exported_at"])
    lines.append("")
    total = sum(document["counts"].values())
    lines.append(f"{total} item" + ("s" if total != 1 else "") + " on the day")
    lines.append("")
    for name in document["groups"]:
        rows = document["groups"][name]
        if not rows:
            continue
        lines.append(_GROUP_HEADING[name])
        for item in rows:
            flag = "*" if item.get("priority") else " "
            stamp = item.get("due_at") or item.get("defer_at")
            when = (dates.human(stamp)
                    if stamp and item["group"] != "completed" else "")
            lines.append(f"  {flag} {item['title']}"
                         + (f"  ({when})" if when else ""))
            if item.get("parent_title"):
                lines.append(f"     in {item['parent_title']}")
            if item.get("notes"):
                lines.append("     " + " ".join(str(item["notes"]).split()))
        lines.append("")
    if total == 0:
        lines.append("Nothing on the calendar for this day.")

    per_page = 54
    lines = lines or ["Nothing on the calendar for this day."]
    page_lines = [lines[i:i + per_page] for i in range(0, len(lines), per_page)]
    return [ _pdf_page_stream(page) for page in page_lines ]


def render_pdf(document: dict, *, title: str = "") -> bytes:
    """A minimal, dependency-free PDF 1.4 of the day.

    No library to lean on: the object graph — a catalog, a page tree, one font
    and one content stream per page — is small enough to write by hand, and the
    result opens in any reader. The font is Helvetica, the standard base 14.
    """
    streams = _pdf_pages(document, title)

    # Number the objects up front so every cross-reference is fixed before a
    # single byte is written. 1 catalog, 2 the page tree, 3 the font, then a
    # page and a content stream per sheet.
    page_obj = 4
    stream_obj = 4 + 2 * len(streams)

    kids = b" ".join(("%d 0 R" % (page_obj + 2 * i)).encode()
                     for i in range(len(streams)))
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % len(streams),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, stream in enumerate(streams):
        p = page_obj + 2 * i
        s = stream_obj + 2 * i
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources <</Font 3 0 R>> /Contents %d 0 R >>" % s)
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream"
                       % (len(stream), stream))
    return _pdf_assemble(objects)


def _pdf_assemble(objects: list[bytes]) -> bytes:
    """Lay the numbered objects into a file with an xref table at the end."""
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % index
        out += body
        out += b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(objects) + 1)
    out += b"startxref\n%d\n%%%%EOF\n" % xref_pos
    return bytes(out)
