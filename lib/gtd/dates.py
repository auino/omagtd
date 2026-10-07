"""Dates in both directions: what you type, and what the panel shows.

Parsing is deliberately forgiving because the whole point of a capture box is
that it is fast. Anything ambiguous is rejected rather than guessed, so a
mistyped date ends up in the title instead of silently landing somewhere
wrong.
"""

from __future__ import annotations

import datetime as _dt
import re
import warnings

from .db import STAMP

WEEKDAYS = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3,
    "thursday": 3, "fri": 4, "friday": 4, "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

UNITS = {
    "d": "day", "day": "day", "days": "day",
    "w": "week", "week": "week", "weeks": "week",
    "m": "month", "mo": "month", "month": "month", "months": "month",
    "y": "year", "yr": "year", "year": "year", "years": "year",
}

# The clock a bare date lands on when no time is given, and how times are shown.
# Both live in the database and are set with `gtd settings`; these are the
# defaults for a fresh install and for code that never configures them.
DEFAULT_TIME = "18:00"
TIME_FORMAT = "24h"


def configure(*, due_time: str | None = None, time_format: str | None = None) -> None:
    """Point the parser and formatter at the store's settings."""
    global DEFAULT_TIME, TIME_FORMAT
    if due_time:
        DEFAULT_TIME = due_time
    if time_format:
        TIME_FORMAT = time_format


def default_hm() -> tuple[int, int]:
    """The configured default time as whole hours and minutes."""
    try:
        hour, minute = str(DEFAULT_TIME).split(":")[:2]
        return max(0, min(23, int(hour))), max(0, min(59, int(minute)))
    except (ValueError, IndexError):
        return 18, 0


def format_time(moment: _dt.datetime) -> str:
    """A clock time in the configured 24h or 12h shape."""
    if TIME_FORMAT == "12h":
        return moment.strftime("%I:%M %p").lstrip("0")
    return moment.strftime("%H:%M")


def to_stamp(value: _dt.date | _dt.datetime | None, default_time: str | None = None) -> str | None:
    """Normalize a date or datetime to the stored 'YYYY-MM-DD HH:MM' form."""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.strftime(STAMP)
    hour, minute = _hm(default_time)
    return _dt.datetime(value.year, value.month, value.day, hour, minute).strftime(STAMP)


def _hm(default_time: str | None) -> tuple[int, int]:
    if not default_time:
        return default_hm()
    try:
        hour, minute = str(default_time).split(":")[:2]
        return max(0, min(23, int(hour))), max(0, min(59, int(minute)))
    except (ValueError, IndexError):
        return default_hm()


def parse_stamp(value: str | None) -> _dt.datetime | None:
    """Read a stored stamp back. Tolerates the full-second form sqlite emits."""
    if not value:
        return None
    for pattern in (STAMP, "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(value, pattern)
        except ValueError:
            continue
    return None


def add_unit(moment: _dt.datetime, interval: int, unit: str) -> _dt.datetime:
    """Advance by whole units, clamping the day so 31 Jan + 1 month = 28/29 Feb
    rather than blowing up into March."""
    if unit == "day":
        return moment + _dt.timedelta(days=interval)
    if unit == "week":
        return moment + _dt.timedelta(weeks=interval)
    if unit == "month":
        month = moment.month - 1 + interval
        year = moment.year + month // 12
        month = month % 12 + 1
        day = min(moment.day, _days_in_month(year, month))
        return moment.replace(year=year, month=month, day=day)
    if unit == "year":
        year = moment.year + interval
        return moment.replace(year=year, day=min(moment.day, _days_in_month(year, moment.month)))
    raise ValueError(f"unknown repeat unit: {unit}")


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (_dt.date(year, month + 1, 1) - _dt.timedelta(days=1)).day


def _end_of_week(moment: _dt.datetime) -> _dt.datetime:
    return moment + _dt.timedelta(days=6 - moment.weekday())


# The words a half-typed date can be finished into. Every one of them is
# something `parse_when` already understands.
DATE_WORDS = (
    "today", "tonight", "tomorrow", "yesterday",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "eow", "eom", "eoy",
)

# Words that already say when they land: "tonight" is 20:00, and eow/eom/eoy
# land on the configured default time without needing it spelled out.
_FIXED_TIME = ("tonight", "eow", "eom", "eoy")


def complete_when(text: str | None) -> str | None:
    """Finish a half-typed date against the words above.

    Tab in a date field wants an answer it can commit to, so a prefix that
    could be two words ("to" -> today, tonight, tomorrow) is left alone; only a
    prefix of exactly one word completes. A bare date word is completed with
    the configured default time, so the field says when it will actually
    happen rather than leaving that to be guessed.

    Returns the whole text with that word replaced, so the caller can put the
    result straight into the field: "next fri" comes back as
    "next friday 18:00". "friday 18:00" is left exactly as typed.
    """
    raw = str(text or "").strip().lower()
    if not raw:
        return None
    head = raw.split()[-1]
    # A leading +/-, a digit or a colon means the user has moved on to an
    # offset or a clock time, neither of which these words can finish.
    if not head or head[0] in "+-0123456789:":
        return None

    hits = []
    for word in DATE_WORDS:
        if word.startswith(head) and word not in hits:
            hits.append(word)
    if len(hits) != 1:
        return None
    word = hits[0]
    if word in _FIXED_TIME:
        done = word
    else:
        hour, minute = default_hm()
        done = f"{word} {hour:02d}:{minute:02d}"
    if head == word:
        return done
    return raw[:len(raw) - len(head)] + done


def parse_when(text: str | None, reference: _dt.datetime | None = None) -> _dt.datetime | None:
    """Parse a date expression into a datetime, or None if it is not one.

    Understands: today, tonight, tomorrow, weekday names, "next week",
    eow/eom/eoy, +3d / in 3 days, ISO dates, and "5 oct" / "oct 5".
    A bare date lands on the configured default time (18:00 out of the box),
    except "tonight" (20:00); an explicit clock time is honoured when given.
    """
    if text is None:
        return None
    raw = str(text).strip().lower()
    if not raw:
        return None

    base = reference or _dt.datetime.now()
    hour, minute = default_hm()
    today = base.replace(hour=hour, minute=minute, second=0, microsecond=0)

    words = raw.split()
    head = words[0]

    if head in ("today", "tod", "today's", "today's"):
        return today
    if head in ("tonight", "tn"):
        return today.replace(hour=20)
    if head in ("tomorrow", "tom", "tmr", "tomorrow's"):
        return today + _dt.timedelta(days=1)
    if head in ("yesterday", "yest"):
        return today - _dt.timedelta(days=1)
    if head in ("eow", "endofweek"):
        return _end_of_week(today)
    if head in ("eom", "endofmonth"):
        return today.replace(day=_days_in_month(today.year, today.month))
    if head in ("eoy", "endofyear"):
        return today.replace(month=12, day=31)

    # "next" on its own is too ambiguous to claim, so it needs a unit or weekday.
    if head == "nextweek" or (head == "next" and len(words) > 1 and words[1] in ("week", "wk")):
        return today + _dt.timedelta(days=7 - today.weekday())
    if head == "next" and len(words) > 1:
        if words[1] in WEEKDAYS:
            return _next_weekday(today, WEEKDAYS[words[1]], force_next_week=True)
        unit = UNITS.get(words[1].rstrip("s"))
        if unit:
            return add_unit(today, 1, unit)
    if head in WEEKDAYS and len(words) == 1:
        return _next_weekday(today, WEEKDAYS[head])

    relative = re.fullmatch(r"([+-]?)(\d+)\s*([a-z]+)", raw)
    if relative:
        unit = UNITS.get(relative.group(3))
        if unit:
            magnitude = int(relative.group(2))
            if relative.group(1) == "-":
                magnitude = -magnitude
            return add_unit(today, magnitude, unit)

    in_delta = re.fullmatch(r"in\s+(\d+)\s*([a-z]+)", raw)
    if in_delta:
        unit = UNITS.get(in_delta.group(2))
        if unit:
            return add_unit(today, int(in_delta.group(1)), unit)

    return _parse_absolute(raw, hour, minute)


def _next_weekday(base: _dt.datetime, target: int, force_next_week: bool = False) -> _dt.datetime:
    delta = (target - base.weekday()) % 7
    if delta == 0 and force_next_week:
        delta = 7
    return base + _dt.timedelta(days=delta)


def _parse_absolute(raw: str, hour: int | None = None, minute: int | None = None) -> _dt.datetime | None:
    text = raw.replace("  ", " ").strip()
    if hour is None:
        hour, minute = default_hm()
    dated = [
        "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %I:%M %p", "%Y-%m-%d %I:%M%p", "%Y-%m-%d %I:%M:%S %p",
        "%Y-%m-%d", "%d.%m.%Y", "%d.%m.%Y %H:%M", "%Y/%m/%d",
        "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y",
    ]
    for pattern in dated:
        try:
            parsed = _dt.datetime.strptime(text, pattern)
        except ValueError:
            continue
        if "%H" not in pattern and "%I" not in pattern:
            parsed = parsed.replace(hour=hour, minute=minute)
        return parsed

    # Year-less forms ("5 oct", "oct 5") mean the next time that day comes
    # around, not 1900 — which is what strptime defaults to, and which Python
    # 3.14 warns about.
    partial = [
        "%d %b %H:%M", "%d %B %H:%M", "%b %d %H:%M", "%B %d %H:%M",
        "%d %b %I:%M %p", "%d %B %I:%M %p", "%b %d %I:%M %p", "%B %d %I:%M %p",
        "%d %b %I:%M%p", "%d %B %I:%M%p", "%b %d %I:%M%p", "%B %d %I:%M%p",
        "%d %b", "%d %B", "%b %d", "%B %d",
    ]
    base = _dt.datetime.now()
    midnight = base.replace(hour=0, minute=0, second=0, microsecond=0)
    for pattern in partial:
        # strptime warns (and, from 3.15, may refuse) when a pattern names a
        # day without a year. We put the year back ourselves two lines down,
        # so the ambiguity it is worried about cannot reach the caller.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            try:
                parsed = _dt.datetime.strptime(text, pattern)
            except ValueError:
                continue
        try:
            parsed = parsed.replace(year=base.year)
        except ValueError:  # 29 Feb in a non-leap year
            continue
        if parsed < midnight:
            try:
                parsed = parsed.replace(year=base.year + 1)
            except ValueError:
                continue
        if "%H" not in pattern and "%I" not in pattern:
            parsed = parsed.replace(hour=hour, minute=minute)
        return parsed
    return None


def parse_duration(text: str | None) -> tuple[int, str] | None:
    """Parse "every week", "2 weeks", "every other month", "daily".

    Returns (interval, unit) or None.
    """
    if not text:
        return None
    raw = str(text).strip().lower()
    raw = re.sub(r"^(every|each|repeats?|recurs?)\s+", "", raw).strip()
    if raw in ("day", "daily"):
        return (1, "day")
    if raw in ("week", "weekly"):
        return (1, "week")
    if raw in ("month", "monthly"):
        return (1, "month")
    if raw in ("year", "yearly", "annually"):
        return (1, "year")

    match = re.fullmatch(r"(?:other\s+)?(\d+)?\s*([a-z]+)", raw)
    if not match:
        return None
    unit = UNITS.get(match.group(2))
    if not unit:
        return None
    count = int(match.group(1)) if match.group(1) else 1
    if count <= 0:
        return None
    # "every other week" without a number means every second unit.
    if raw.startswith("other") and match.group(1) is None:
        count = 2
    return (count, unit)


_MINUTES_RE = re.compile(
    r"^\s*(?:(?P<h>\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours))?"
    r"\s*(?:(?P<m>\d+)\s*(?:m|min|mins|minute|minutes))?\s*$",
    re.IGNORECASE,
)


def parse_minutes(text: str | int | None) -> int | None:
    """Parse an effort estimate: "30m", "1h", "1.5h", "1h30m", "90 minutes".

    Returns whole minutes, or None when it is not an estimate. Distinct from
    :func:`parse_duration`, which reads repeat rules (where "m" means month).
    """
    if text is None or isinstance(text, bool):
        return None
    if isinstance(text, int):
        return text if text > 0 else None
    raw = str(text).strip().lower()
    if not raw:
        return None
    match = _MINUTES_RE.match(raw)
    if not match or (match.group("h") is None and match.group("m") is None):
        return None
    total = 0.0
    if match.group("h") is not None:
        total += float(match.group("h")) * 60
    if match.group("m") is not None:
        total += int(match.group("m"))
    minutes = int(round(total))
    return minutes if minutes > 0 else None


def human_minutes(minutes: int | None) -> str:
    """Render an effort estimate compactly: 90 -> "1h30m"."""
    if not minutes or minutes <= 0:
        return ""
    hours, mins = divmod(int(minutes), 60)
    if hours and mins:
        return f"{hours}h{mins:02d}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def human(stamp: str | None, reference: _dt.datetime | None = None, *, long: bool = False) -> str:
    """Format a stored stamp for a list row.

    Near dates become words ("today", "in 3 days", "5d late") because that is
    what a planning decision needs; far dates become dates.
    """
    moment = parse_stamp(stamp)
    if moment is None:
        return ""
    base = reference or _dt.datetime.now()
    today = base.replace(hour=0, minute=0)
    days = (moment.date() - today.date()).days

    if days == 0:
        d_hour, d_minute = default_hm()
        bare = moment.hour == d_hour and moment.minute == d_minute
        return "today" if bare else "today " + format_time(moment)
    if days == 1:
        return "tomorrow"
    if days == -1:
        return "yesterday"
    if -6 <= days < 0:
        return f"{abs(days)}d late"
    if 0 < days <= 6:
        return f"in {days}d"
    if days > 6:
        if long:
            return moment.strftime("%a %-d %b")
        return moment.strftime("%-d %b")
    return moment.strftime("%-d %b")


def day_stamp(stamp: str | None) -> str:
    moment = parse_stamp(stamp)
    return moment.date().isoformat() if moment else ""
