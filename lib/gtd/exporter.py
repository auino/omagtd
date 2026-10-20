"""Export the commitments of one day to a text file.

This is the only place that knows how a day reads as a document: the markdown,
JSON, YAML and PDF each carry the same task list and its export stamp, differ
only in the dressing. The CLI handler in :mod:`gtd.cli` collects the day's
items and hands them here; the four renderers below are pure functions of that
data, so a test can compare a markdown export against a JSON export of the very
same day without a database in the way.
"""

from __future__ import annotations

import base64 as _b64
import datetime as _dt
import io as _io
import json
import os as _os
import zlib as _zlib

from . import core, dates, theme
from .core import GtdError

EXPORT_FORMATS = ("md", "json", "yaml", "pdf", "html")

# What the export files recognise as one of the four formats, by suffix.
_FORMAT_BY_SUFFIX = {
    ".md": "md", ".markdown": "md",
    ".json": "json",
    ".yaml": "yaml", ".yml": "yaml",
    ".pdf": "pdf",
    ".html": "html", ".htm": "html",
}

# The one suffix a name with no suffix is given, per format.
_FORMAT_BY_SUFFIX_INV = {"md": ".md", "json": ".json", "yaml": ".yaml", "pdf": ".pdf", "html": ".html"}

# The day's groups: its deadline, what its defer window has swept up, and what
# got finished.
_GROUPS = ("due", "overdue", "completed")

# A context-scoped export ignores the calendar, so it has one group only: the
# work that carries the context.
_CONTEXT_GROUPS = ("context",)

# The day's groups print in this order: its deadline, what its defer window has
# swept up, and what got finished. A context export has just its own group.
_PRINT_ORDER = ("due", "overdue", "completed", "context")

_GROUP_HEADING = {
    "due": "Due",
    "overdue": "Overdue",
    "completed": "Completed",
    "context": "Context",
}

# The short word a group counts read as in the one-line summary, set apart from
# its heading so "Overdue" stays a heading while "overdue" is the count.
_GROUP_WORD = {
    "due": "due",
    "overdue": "overdue",
    "completed": "completed",
    "context": "context",
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


def _open_action_filter() -> str:
    return ("t.kind = 'action' "
            "AND t.status NOT IN ('done', 'dropped', 'archived', 'trashed')")


def _in_context(item_tags: set[str], wanted: set[str]) -> bool:
    """Whether a task belongs to one of the named contexts.

    A context path is stored in full, so "home>kitchen" names both the
    subcontext and the "home" tree it sits in. Asking for "home" therefore
    reaches that context and everything nested under it -- the same prefix
    rule the rest of the program uses -- and nothing outside the tree.
    """
    for tag in item_tags:
        for name in wanted:
            if tag == name or tag.startswith(name + ">"):
                return True
    return False


def day_export(store, *text: str, groups: tuple[str, ...] | None = None,
               include_meta: bool = True, tags: list[str] | None = None,
               no_overdue: bool = False, command: str | None = None) -> dict:
    """The commitments of one date as one structured document.

    An export is about a single date, so it holds only the work that date is
    about, and one rule decides it for every date: what falls due that day, or
    whatever its defer window has already opened -- defer on or before the
    date, whatever the deadline says. The export of the 5th lists what is due
    on the 5th and everything available by the 5th, and nothing else. Work
    finished that day is written too, when the date is today. Within each
    group the tasks are ordered by deadline and the ones with no deadline
    come last. `no_overdue` leaves the overdue list out of the document whole:
    the export then holds only the day's own deadline -- and what finished that
    day, when the day is today -- with nothing carried over from another day.
    The day's own deadline is read against the clock too, so a task whose
    deadline has already gone by, an earlier hour of that day included, is not
    written either.

    When one or more contexts are named with `tags` the export is about the
    context instead of the date: every open task carrying one of those contexts
    -- or a context nested beneath one -- is written, whatever its dates, and
    the calendar is not consulted at all -- no date decides whether a task is
    listed, nor how the list is grouped -- so `no_overdue`, being a date's
    question, changes nothing here. The same dictionary drives every
    renderer, and a date that is the day it was taken for, not the day it
    happens to be read on: re-running the export the next morning still reports
    the day the file names. `command` is the command line the file was produced
    by, kept so the record says how it was asked for.
    """
    day = _resolve_day(text[0] if text else None)
    is_today = day == core.today()
    tagset = {t.strip().lstrip("#").lower() for t in (tags or []) if t.strip()}

    if tagset:
        # A context-scoped export ignores the calendar and lists every open
        # task for the named contexts and their subcontexts, whatever its dates.
        groups = _CONTEXT_GROUPS
    else:
        groups = groups or _GROUPS
        if no_overdue:
            # The overdue list is what the flag exists to keep out, so the
            # group is dropped before anything is collected for it: the
            # document carries no "overdue" key at all, and the renderers --
            # which walk the document's own groups -- print no such section.
            groups = tuple(name for name in groups if name != "overdue")

    raw: dict[str, list[dict]] = {name: [] for name in groups}
    if "context" in groups:
        rows = [dict(r) for r in store.query(
            "SELECT * FROM item_titles t WHERE " + _open_action_filter()
            + " ORDER BY COALESCE(t.due_at, '9999'), t.priority DESC, "
            "t.title COLLATE NOCASE")]
        for entry in rows:
            item_tags = {str(x).strip().lstrip("#").lower()
                         for x in (core.tags_of(store, entry.get("id")) or [])}
            if _in_context(item_tags, tagset):
                raw["context"].append(entry)
    else:
        # The day's two clauses: its deadline, or a defer window already open
        # by it. A task satisfying both is the day's due work, so "overdue"
        # takes only the rest -- including a task with no deadline at all,
        # which the due clause cannot reach.
        if "due" in groups:
            # With `no_overdue` the day's own deadline is read against the
            # clock: one already gone by here and now is expired, an earlier
            # hour of the same day included.
            clause = (" AND t.due_at IS NOT NULL AND substr(t.due_at, 1, 10) = ? "
                      + ("AND t.due_at >= ? " if no_overdue else ""))
            params: list = [day] + ([now_stamp()] if no_overdue else [])
            raw["due"] = [dict(r) for r in store.query(
                "SELECT * FROM item_titles t WHERE " + _open_action_filter()
                + clause
                + "ORDER BY t.due_at, t.priority DESC", tuple(params))]
        if "overdue" in groups:
            # A task whose defer window is open but whose deadline is another
            # day. The group is only asked for when the export is meant to
            # carry it: `no_overdue` drops it above, before this runs.
            clause = (" AND t.defer_at IS NOT NULL AND substr(t.defer_at, 1, 10) <= ? "
                      "AND (t.due_at IS NULL OR substr(t.due_at, 1, 10) != ?) ")
            params = [day, day]
            raw["overdue"] = [dict(r) for r in store.query(
                "SELECT * FROM item_titles t WHERE " + _open_action_filter()
                + clause
                + "ORDER BY COALESCE(t.due_at, '9999'), t.priority DESC, "
                "t.defer_at", tuple(params))]
        if "completed" in groups and is_today:
            raw["completed"] = [dict(r) for r in store.query(
                "SELECT * FROM item_titles t WHERE t.status = 'done' "
                "AND t.completed_at IS NOT NULL "
                "AND substr(t.completed_at, 1, 10) = ? "
                "ORDER BY COALESCE(t.due_at, '9999'), t.priority DESC, "
                "t.completed_at", (day,))]

    items = []
    for group in groups:
        for entry in raw[group]:
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

    weekday = _dt.date.fromisoformat(day).strftime("%A")
    # The groups in the order they print: the day's deadline first, then the
    # work its defer window swept up, then what got finished.
    ordered = [name for name in _PRINT_ORDER if name in groups]
    ordered += [name for name in groups if name not in ordered]
    document = {
        # The day the document is about, and the moment it was written. The
        # latter is what makes an export a record of when it was taken; an
        # "as of" line a reader can trust without opening the database again.
        "date": day,
        "weekday": weekday,
        "exported_at": now_stamp(),
        "command": command or "",
        "groups": {name: [item for item in items if item["group"] == name]
                   for name in ordered},
        "items": items,
        "counts": {name: len([item for item in items if item["group"] == name])
                   for name in ordered},
    }
    return document


def now_stamp() -> str:
    """The export's stamp, 'YYYY-MM-DD HH:MM', the same shape the rest of the
    program keeps its timestamps in."""
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def render(document: dict, fmt: str, *, title: str | None = None,
           accent: str | None = None) -> bytes:
    """Render a day document to its chosen format, as bytes.

    The text formats are UTF-8; the PDF is its own binary. An unknown format
    refuses rather than guessing, the way an unrecognised file would. The
    accent the reports dress themselves in follows the app's palette setting;
    a caller that knows the database passes it in, and one that does not gets
    the desktop theme's own.
    """
    if fmt == "md":
        return render_markdown(document, title=title or "").encode("utf-8")
    if fmt == "json":
        return render_json(document).encode("utf-8")
    if fmt == "yaml":
        return render_yaml(document).encode("utf-8")
    if fmt == "pdf":
        return render_pdf(document, title=title or "", accent=accent)
    if fmt == "html":
        return render_html(document, title=title or "", accent=accent).encode("utf-8")
    raise GtdError(f"unknown export format: {fmt!r}")


# -------------------------------------------------------------- markdown


def _item_markdown(item: dict) -> list[str]:
    lines = []
    flag = "!" if item.get("priority") else " "
    bullet = "[ ] "
    stamp = item.get("due_at") or item.get("defer_at")
    when = dates.human(stamp) if stamp and item["group"] != "completed" else ""
    line = f"{bullet}{flag} **{item['title']}**".rstrip()
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
    if item.get("defer_at") and item["group"] != "completed":
        lines.append(f"    _defer: {dates.human(item['defer_at'])}_")
    if item.get("due_at") and item["group"] != "completed":
        lines.append(f"    _due: {dates.human(item['due_at'])}_")
    if item.get("completed_at") and item["group"] == "completed":
        lines.append("    _completed " + dates.human(item["completed_at"]) + "_")
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
    if document.get("command"):
        lines.insert(3, f"_Command: `{document['command']}`._")
    total = sum(document["counts"].values())
    detail = " · ".join(f"{document['counts'][name]} "
                        + _GROUP_WORD[name]
                        for name in document["groups"] if document["counts"][name])
    lines.append(f"{total} item" + ("" if total == 1 else "s")
                 + (f" — {detail}" if detail else ""))
    lines.append("")
    any_items = False
    for name in _PRINT_ORDER:
        rows = document["groups"].get(name) or []
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


# -------------------------------------------------------------- HTML


def _html_escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


# The app's own icon — the same mark the bar pill and the fullscreen overlay
# show. It ships as app/icon.png beside this package; a report embeds a small
# copy of it so the page or the sheet carries the mark on its own.
_APP_ICON = _os.path.normpath(
    _os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir,
                  "app", "icon.png"))


def _icon_rgba(size: int = 192):
    """The app icon as an RGBA image downscaled to `size`, or None."""
    try:
        from PIL import Image
    except Exception:  # pragma: no cover - no Pillow on this box
        return None
    try:
        image = Image.open(_APP_ICON).convert("RGBA")
        image.thumbnail((size, size))
        return image
    except Exception:  # pragma: no cover - unreadable icon
        return None


def _icon_png(size: int = 160) -> bytes | None:
    """A downscaled PNG of the app icon, or None when it cannot be produced."""
    image = _icon_rgba(size)
    if image is None:
        return None
    buffer = _io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _icon_bytes() -> bytes | None:
    """The icon as PNG bytes: downscaled when possible, whole when not."""
    png = _icon_png()
    if png is not None:
        return png
    try:  # pragma: no cover - only when Pillow is missing
        with open(_APP_ICON, "rb") as handle:
            return handle.read()
    except Exception:
        return None


# The mark drawn as vector art, in the report's accent. Only used when the
# icon file cannot be read at all, so a report is never shipped without a mark.
def _vector_mark(accent: str) -> str:
    return (
        '<div class="logo">'
        '<svg width="56" height="56" viewBox="0 0 56 56" role="img" aria-label="OmaGTD">'
        '<rect x="8" y="8" width="40" height="40" rx="6" fill="none" '
        f'stroke="{accent}" stroke-width="3"/>'
        '<path d="M17 30 L25 38 L40 20" fill="none" '
        f'stroke="{accent}" '
        'stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>'
        '</svg>'
        '<div class="wordmark">OmaGTD</div>'
        '</div>'
    )


def _html_logo(accent: str) -> str:
    """The mark for an HTML report: the app icon, or the vector mark."""
    png = _icon_bytes()
    if png is None:
        return _vector_mark(accent)
    src = "data:image/png;base64," + _b64.b64encode(png).decode("ascii")
    return ('<div class="logo">'
            f'<img src="{src}" alt="OmaGTD" width="64" height="64" />'
            '<div class="wordmark">OmaGTD</div>'
            '</div>')


def render_html(document: dict, *, title: str = "", accent: str | None = None) -> str:
    # The accent follows the app's palette setting; the page itself keeps its
    # white paper and dark ink so it stays readable and printable.
    accent = accent or theme.accent()
    heading = _html_escape(title or f"OmaGTD — {document['weekday']}, {document['date']}")
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        f"<title>{heading}</title>",
        "<style>",
        "  body { font-family: sans-serif; max-width: 60em; margin: 2em auto; padding: 0 1em; color: #222; }",
        "  .logo { text-align: center; margin-bottom: 1em; }",
        f"  .wordmark {{ color: {accent}; font-weight: bold; letter-spacing: 0.08em; }}",
        "  h1 { text-align: center; border-bottom: 1px solid #ccc; padding-bottom: 0.4em; }",
        f"  h2 {{ margin-top: 1.5em; color: {accent}; }}",
        "  ul { list-style: none; padding-left: 0; }",
        "  li { margin: 0.8em 0; padding-left: 2em; }",
        f"  .bullet {{ font-weight: bold; color: {accent}; }}",
        "  .flag { color: red; }",
        "  .meta { color: #666; font-size: 0.9em; }",
        "  .meta span { margin-right: 1em; }",
        "  .when { color: #444; }",
        "  .stamp { color: #666; font-size: 0.9em; margin-top: 2em; border-top: 1px solid #ccc; padding-top: 0.5em; }",
        "</style>",
        "</head>",
        "<body>",
        _html_logo(accent),
        f"<h1>{heading}</h1>",
        f"<p class=\"meta\" style=\"text-align:center\">{_html_escape(str(document['date']))} · exported {_html_escape(str(document['exported_at']))}</p>",
    ]
    if document.get("command"):
        parts.append(f"<p class=\"stamp\" style=\"text-align:center\">"
                     f"<code>{_html_escape(document['command'])}</code></p>")
    any_items = False
    for name in _PRINT_ORDER:
        rows = document["groups"].get(name) or []
        if not rows:
            continue
        any_items = True
        parts.append(f"<h2>{_html_escape(_GROUP_HEADING[name])}</h2>")
        parts.append("<ul>")
        for item in rows:
            flag = "!" if item.get("priority") else ""
            parts.append("  <li>")
            parts.append(f"    <span class=\"bullet\">■</span> "
                         + ("<span class=\"flag\">! </span>" if flag else "")
                         + f"<strong>{_html_escape(item['title'])}</strong>")
            if item.get("due_at"):
                parts.append(f"    <div class=\"meta\">"
                             f"due: {_html_escape(dates.human(item['due_at']))}</div>")
            if item.get("defer_at"):
                parts.append(f"    <div class=\"meta\">"
                             f"defer: {_html_escape(dates.human(item['defer_at']))}</div>")
            if item.get("completed_at") and item["group"] == "completed":
                parts.append(f"    <div class=\"meta\">"
                             f"completed {_html_escape(dates.human(item['completed_at']))}</div>")
            meta = []
            if item.get("parent_title"):
                meta.append(f"in {_html_escape(item['parent_title'])}")
            if item.get("area_title") and item["area_title"] != item.get("parent_title"):
                meta.append(f"area {_html_escape(item['area_title'])}")
            if item.get("tags"):
                meta.append(" ".join("#" + _html_escape(t) for t in item["tags"]))
            if item.get("duration"):
                meta.append(_html_escape(str(dates.human_minutes(int(item['duration']))) + " estimated"))
            if item.get("url"):
                meta.append(f'<a href="{_html_escape(item["url"])}">{_html_escape(item["url"])}</a>')
            if meta:
                parts.append(f"    <div class=\"meta\">" + " · ".join(meta) + "</div>")
            if item.get("notes"):
                parts.append(f"    <div class=\"meta\">"
                             + _html_escape(str(item["notes"]).strip()) + "</div>")
            parts.append("  </li>")
        parts.append("</ul>")
    if not any_items:
        parts.append("<p>Nothing on the calendar for this day.</p>")
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts) + "\n"


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


# The app's mark, drawn as vector art in the report's accent: a checked box. It
# sits centered at the top of every PDF page, above the date line.
def _pdf_logo(accent: str) -> str:
    colour = theme.pdf_colour(accent)
    return (
        "q\n"
        f"{colour} RG\n2 w\n1 J\n1 j\n"
        "294 730 24 24 re S\n"
        "300 741 m\n304.5 736 l\n313 748 l\nS\n"
        f"{colour} rg\n"
        "292 760 28 3 re f\n"
        "Q\n"
    )


def _pdf_icon(size: int = 192, accent: str | None = None) -> tuple[str, bytes | None, int, int]:
    """The app's mark for a PDF page.

    Returns the operators that paint it, the Flate-compressed RGB pixels and
    their size — or the vector mark and no data when the icon cannot be read.
    The mark sits centered above the date line on every page, and the image
    object it names (Im0) is added by :func:`render_pdf`.
    """
    from PIL import Image

    image = _icon_rgba(size)
    if image is None:
        return _pdf_logo(accent or theme.accent()), None, 0, 0
    background = Image.new("RGBA", image.size, (255, 255, 255, 255))
    flat = Image.alpha_composite(background, image).convert("RGB")
    width, height = flat.size
    drawn = 48
    x = (612 - drawn) // 2
    ops = "q %d 0 0 %d %d 730 cm /Im0 Do Q\n" % (drawn, drawn, x)
    return ops, _zlib.compress(flat.tobytes()), width, height


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


def _pdf_page_stream(lines: list[str], icon_ops: str) -> bytes:
    """A page's content stream with limited bold support.

    Uses Helvetica (F1) and Bold (F2) when lines are marked with **. The
    app's mark — the icon image when there is one, a checked box when there
    is not — opens every page.
    """
    stream = icon_ops
    stream += "BT\n/F1 11 Tf\n14 TL\n72 710 Td\n"
    for row in lines:
        i = 0
        text = str(row)
        while True:
            b1 = text.find("**", i)
            if b1 == -1:
                if i < len(text):
                    stream += "(" + _pdf_escape(text[i:]) + ") Tj\n"
                break
            if b1 > i:
                stream += "(" + _pdf_escape(text[i:b1]) + ") Tj\n"
            b2 = text.find("**", b1 + 2)
            if b2 == -1:
                # unclosed, treat rest as bold? or normal
                bold = text[b1 + 2:]
                stream += "/F2 11 Tf\n(" + _pdf_escape(bold) + ") Tj\n/F1 11 Tf\n"
                break
            bold = text[b1 + 2:b2]
            rest = text[b2 + 2:]
            stream += "/F2 11 Tf\n(" + _pdf_escape(bold) + ") Tj\n/F1 11 Tf\n"
            text = rest
            i = 0
        stream += "T*\n"
    stream += "ET"
    return stream.encode("cp1252", errors="replace")


def _pdf_build_lines(document: dict, title: str, groups: tuple[str, ...] | None = None) -> list[str]:
    lines: list[str] = []
    heading = title or f"OmaGTD - {document['weekday']}, {document['date']}"
    lines.append(heading)
    lines.append("Exported " + document["exported_at"])
    if document.get("command"):
        lines.append("Command: " + document["command"])
    lines.append("")
    names = groups or tuple(document["groups"].keys())
    for name in names:
        rows = document["groups"].get(name) or []
        if not rows:
            continue
        lines.append(_GROUP_HEADING[name])
        for item in rows:
            flag = "!" if item.get("priority") else " "
            stamp = item.get("due_at") or item.get("defer_at")
            when = (dates.human(stamp)
                    if stamp and item["group"] != "completed" else "")
            lines.append(f"  [ ] {flag} **{item['title']}**"
                         + (f"  ({when})" if when else ""))
            if item.get("parent_title"):
                lines.append(f"     in {item['parent_title']}")
            if item.get("area_title") and item["area_title"] != item.get("parent_title"):
                lines.append(f"     area {item['area_title']}")
            if item.get("tags"):
                lines.append("     " + ", ".join("#" + tag for tag in item["tags"]))
            if item.get("duration"):
                lines.append(f"     {dates.human_minutes(int(item['duration']))} estimated")
            if item.get("url"):
                lines.append(f"     {item['url']}")
            if item.get("defer_at") and item["group"] != "completed":
                lines.append(f"     defer: {dates.human(item['defer_at'])}")
            if item.get("due_at") and item["group"] != "completed":
                lines.append(f"     due: {dates.human(item['due_at'])}")
            if item.get("completed_at") and item["group"] == "completed":
                lines.append("     completed " + dates.human(item["completed_at"]))
            if item.get("notes"):
                lines.append("     " + " ".join(str(item["notes"]).split()))
        lines.append("")
    return lines


def _pdf_pages(document: dict, title: str, icon_ops: str) -> list[bytes]:
    """Break the day into page content streams.

    The logo, the heading, the export stamp and a one-line count open every
    sheet, so a printed page still says what it is and when it was taken. The
    groups follow in the order the calendar prints them.
    """
    total = sum(document["counts"].values())
    if total == 0:
        lines = [title or f"OmaGTD - {document['weekday']}, {document['date']}",
                 "Exported " + document["exported_at"]]
        if document.get("command"):
            lines.append("Command: " + document["command"])
        lines += ["", "Nothing on the calendar for this day."]
    else:
        lines = _pdf_build_lines(document, title)
    if not lines:
        lines = ["Nothing on the calendar for this day."]

    per_page = 52
    return [ _pdf_page_stream(page, icon_ops) for page in
             (lines[i:i + per_page] for i in range(0, len(lines), per_page)) ]


def render_pdf(document: dict, *, title: str = "", accent: str | None = None) -> bytes:
    """A minimal, dependency-free PDF 1.4 of the day.

    No library to lean on: the object graph — a catalog, a page tree, one font
    and one content stream per page — is small enough to write by hand, and the
    result opens in any reader. The font is Helvetica, the standard base 14.
    The page stays black on white; only the drawn mark wears the accent, which
    follows the app's palette setting.
    """
    accent = accent or theme.accent()
    icon_ops, icon_data, icon_w, icon_h = _pdf_icon(accent=accent)
    streams = _pdf_pages(document, title, icon_ops)

    # Number the objects up front so every cross-reference is fixed before a
    # single byte is written: 1 the catalog, 2 the page tree, 3 the regular
    # font, 4 the bold font, 5 the app icon when there is one, and from
    # page_obj on a page and its content stream per sheet, alternating. Page i
    # is object page_obj + 2*i; its stream the very next object.
    page_obj = 5 if icon_data is None else 6

    kids = b" ".join(("%d 0 R" % (page_obj + 2 * i)).encode()
                     for i in range(len(streams)))
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % len(streams),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
    ]
    if icon_data is not None:
        # The icon the content streams paint as /Im0: a Flate-compressed RGB
        # bitmap. Its "cm" scales the unit square onto the 48pt box at the top
        # of the page, so the pixel size here never has to match that box.
        objects.append(
            b"<< /Type /XObject /Subtype /Image /Width %d /Height %d "
            b"/ColorSpace /DeviceRGB /BitsPerComponent 8 "
            b"/Filter /FlateDecode /Length %d >>\nstream\n"
            % (icon_w, icon_h, len(icon_data)) + icon_data + b"endstream")
    xobject = b" /XObject <</Im0 5 0 R>>" if icon_data is not None else b""
    for i, stream in enumerate(streams):
        p = page_obj + 2 * i
        s = p + 1
        # /Resources is a dict whose /Font maps the names F1 and F2 (the very
        # names the content stream's "F1 11 Tf" / "F2 11 Tf" pick) onto the
        # regular and bold fonts, and whose /XObject maps Im0 — the icon —
        # onto its image object.
        resources = b"<< /Font <</F1 3 0 R /F2 4 0 R>>" + xobject + b" >>"
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources " + resources + b" /Contents %d 0 R >>" % s)
        objects.append(b"<< /Length %d >>\nstream\n%s"
                       % (len(stream), stream) + b"endstream")
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
