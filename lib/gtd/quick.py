"""OmniFocus-style quick entry.

One line, no dialogs, everything else optional:

    !                       flag it (a flag is on or off, so ! and !!! are one)
    #errands                context
    #home>kitchen           subcontext (its parent is created too)
    /Garden fence/          project, if a project of that name exists
    /a/Home admin/          area, explicitly
    <tomorrow               defer date
    >friday                 deadline
    =Ana                    waiting for someone
    ~every week             repeat rule
    *30m  *1h  *1h30m       estimated effort

A newline in the captured text starts the note body, like OmniFocus's quick
entry does.

Deliberately conservative about one thing: `/Name/` is only stripped when a
container actually answers to that name. Otherwise "and/or" and "his/her"
survive as the title, which is why a capture can never lose text to a guess.
"""

from __future__ import annotations

import re

from . import dates

FLAG_RE = re.compile(r"^(!{1,3})\s+")
TAG_RE = re.compile(r"^#(\S+)$")
CONTAINER_RE = re.compile(r"^/(?P<body>[^/]+)/$")
DUE_RE = re.compile(r"^>(?P<body>.*)$")
DEFER_RE = re.compile(r"^<(?P<body>.*)$")
WAIT_RE = re.compile(r"^=(?P<body>.*)$")
REPEAT_RE = re.compile(r"^~(?P<body>.*)$")
EFFORT_RE = re.compile(r"^\*(?P<body>.*)$")
QUOTED_RE = re.compile(r'^"(?P<body>[^"]*)"$')

# Longest run of following tokens a `>`/`<`/`~` prefix is allowed to claim
# while looking for something that parses. Three covers "next friday" and
# "every other month"; anything longer than that is prose, not a token.
_LOOKAHEAD = 3

FLAG_TOKEN_RE = re.compile(r"^!{1,3}$")

# Any token that begins a new construct. A `>`/`<`/`~` claim must never
# swallow one, otherwise ">2026-11-01 ~every other month" would have its
# deadline resolved from the head word of a three-word date and eat the
# repeat rule with it.
BREAK_RE = re.compile(r"^[<>=~/#!*]")


def _unquote(value: str) -> str | None:
    match = QUOTED_RE.fullmatch(value.strip())
    return match.group("body") if match else None


def _tokenize(text: str) -> list[str]:
    """Split on whitespace but keep "quoted phrases" as single tokens, so
    `@"Home Depot"` stays one token rather than becoming `@"Home` + `Depot"`."""
    return re.findall(r'[^\s"]*"[^"]*"|\S+', text)


def _claim(tokens: list[str], start: int, validator) -> tuple[str | None, int]:
    """Consume the longest run starting at a sigil token that `validator`
    accepts.

    `tokens[start]` begins with the sigil; the rest of that token plus up to
    three more tokens are candidates, longest first. A candidate that would
    cross into another construct is rejected outright. The validator returns
    the normalized value, which is what makes ">next friday about the fence"
    stop at the date instead of eating the sentence.
    """
    prefix = tokens[start][1:].strip()
    available = min(_LOOKAHEAD, len(tokens) - start - 1)
    for size in range(available, -1, -1):
        chunk = tokens[start + 1:start + 1 + size]
        if any(BREAK_RE.match(token) for token in chunk):
            continue
        parts = [prefix] if prefix else []
        parts.extend(chunk)
        candidate = " ".join(part for part in parts if part)
        if not candidate:
            continue
        result = validator(candidate)
        if result:
            return result, start + 1 + size
    return None, start + 1


def parse(text: str, known: dict[str, str] | None = None) -> dict:
    """Parse a quick-entry line.

    `known` maps a container name (case-insensitive) to its kind, so `/Name/`
    can be resolved without a database round trip. Names that are not in
    `known` stay in the title.

    Returns a dict with title, notes, priority, defer, due, waiting_for,
    repeat, tags, project, area and leftover_unknown (candidate containers
    that no longer exist, kept so the caller can warn).
    """
    known = known or {}
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")

    title_text, _, note_text = raw.partition("\n")
    out = {
        "title": "",
        "notes": note_text.strip(),
        "priority": None,
        "defer": None,
        "due": None,
        "duration": None,
        "waiting_for": None,
        "repeat": None,
        "tags": [],
        "project": None,
        "area": None,
        "unknown_containers": [],
    }

    tokens = _tokenize(title_text.strip())

    flag = FLAG_RE.match(title_text.strip())
    if flag:
        # A run of marks is one flag: OmniFocus typed !!! for emphasis, and
        # counting them would only invent levels nobody asked to rank by.
        out["priority"] = 1
        tokens = _tokenize(title_text.strip()[flag.end():])

    title_parts: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        span = 1

        # Container names may contain spaces, so `/Garden fence/` arrives as
        # two tokens and has to be glued back together first.
        if token.startswith("/") and not CONTAINER_RE.fullmatch(token):
            for size in range(2, min(6, len(tokens) - index) + 1):
                joined = " ".join(tokens[index:index + size])
                if CONTAINER_RE.fullmatch(joined):
                    token, span = joined, size
                    break

        container = CONTAINER_RE.fullmatch(token)
        if container:
            body = container.group("body").strip()
            kind = known.get(body.lower())
            if kind == "project":
                out["project"] = body
            elif kind == "area":
                out["area"] = body
            else:
                # Not a container we know — keep the slashes in the title.
                title_parts.append(token)
            index += span
            continue

        tag = TAG_RE.fullmatch(token)
        if tag:
            out["tags"].append(tag.group(1))
            index += 1
            continue

        if token.startswith(">"):
            value, index = _claim(tokens, index, dates.parse_when)
            if value:
                out["due"] = value
            else:
                title_parts.append(token)
            continue

        if token.startswith("<"):
            value, index = _claim(tokens, index, dates.parse_when)
            if value:
                out["defer"] = value
            else:
                title_parts.append(token)
            continue

        if token.startswith("="):
            # Deliberately not _claim: a name is only "parseable" if it is
            # non-empty, so the greedy validator would swallow #tags.
            body = _unquote(token[1:])
            if body is None and len(token) > 1:
                body = token[1:]
                index += 1
            elif body is None and index + 1 < len(tokens):
                body = tokens[index + 1]
                index += 2
            else:
                index += 1
            out["waiting_for"] = (body or "").strip() or None
            continue

        if token.startswith("~"):
            value, index = _claim(tokens, index, dates.parse_duration)
            if value:
                out["repeat"] = value
            else:
                title_parts.append(token)
            continue

        if token.startswith("*"):
            value, index = _claim(tokens, index, dates.parse_minutes)
            if value:
                out["duration"] = value
            else:
                title_parts.append(token)
            continue

        if FLAG_TOKEN_RE.fullmatch(token):  # a flag may trail the title too
            out["priority"] = 1
            index += 1
            continue

        title_parts.append(token)
        index += 1

    out["title"] = " ".join(part for part in title_parts if part).strip()
    return out


def format_item(item: dict) -> str:
    """Render a parsed item back to quick-entry syntax (used by tests and by
    `gtd show --raw`)."""
    parts = []
    if item.get("priority"):
        parts.append("!")
    parts.append(item.get("title") or "")
    if item.get("defer_at"):
        parts.append("<" + dates.human(item["defer_at"]))
    if item.get("due_at"):
        parts.append(">" + dates.human(item["due_at"]))
    if item.get("duration"):
        parts.append("*" + dates.human_minutes(item["duration"]))
    if item.get("waiting_for"):
        parts.append("=" + item["waiting_for"])
    return " ".join(parts).strip()
