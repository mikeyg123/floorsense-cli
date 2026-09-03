"""Date grammar and printing. Pure: `today` is always injected, never
read -- keeps the whole grammar testable against fixed dates, including
boundary cases (`mon` on a Monday, `tue-next` when it coincides with
`tue`).

The grammar, case-insensitive throughout:

    today / tomorrow        as written
    mon, monday, ...        the first such weekday STRICTLY AFTER today
    mon-next, tues-next     roll forward to the next Saturday STRICTLY AFTER
                            today (a week away if today already is one),
                            then the first such weekday strictly after THAT
    28, 28th                the next date with that day-of-month, after today
    dd/mm                   this year, or next year if already past
    dd/mm/yy, dd/mm/yyyy    explicit
    yyyy-mm-dd, yyyymmdd    explicit, ISO 8601 (both spellings) -- always a
                            4-digit year, never inferred like dd/mm's is
"""

import datetime as dt
import re

__all__ = ["parse_date", "is_date_token", "fmt_date", "fmt_weekday",
           "WEEKDAYS", "weekday_index", "split_past"]

# Every accepted spelling -> Python's Monday=0 weekday index. `tues`, `thur`
# and `thurs` are here because people type them, not because they are correct.
WEEKDAYS = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

_DOM_RE = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?$")
_SLASH_RE = re.compile(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?$")
# ISO 8601, both spellings: `2026-08-25` and its separator-free form
# `20260825`. Explicit `yyyy` makes an 8-digit token unambiguous against
# `_DOM_RE` (1-2 digits) and `_SLASH_RE` (needs a `/`).
_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_ISO_COMPACT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_DAYNAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
             "Friday", "Saturday", "Sunday"]


def weekday_index(token):
    """Weekday index for a bare day name, or None. Used by `fs office-days`."""
    return WEEKDAYS.get(token.strip().lower())


def _next_weekday(today, target, on_or_after=None):
    """The first `target` weekday strictly after `today`, or on/after a given
    date if one is supplied."""
    start = on_or_after if on_or_after is not None else today + dt.timedelta(1)
    delta = (target - start.weekday()) % 7
    return start + dt.timedelta(days=delta)


def _next_saturday(today):
    """The first Saturday strictly after today. Anchors every `-next`
    weekday; Saturday rather than Monday, since a Monday anchor would
    collapse `tue-next` on a Sunday onto plain `tue`'s result. Doesn't
    handle `sun-next` -- `parse_date` special-cases that."""
    return _next_weekday(today, 5)


def _next_day_of_month(today, dom):
    """The next date with this day-of-month, strictly after today. Skips
    months that are too short -- from 1 Sep, `31` is 31 October, not a
    ValueError and not 1 October."""
    year, month = today.year, today.month
    for _ in range(14):          # 14 months is enough for any 28-31 day walk
        try:
            candidate = dt.date(year, month, dom)
        except ValueError:
            candidate = None
        if candidate is not None and candidate > today:
            return candidate
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return None


def parse_date(token, today):
    """Parse one token into a date, or return None if it isn't a date.

    Returning None rather than raising is deliberate: this doubles as the
    classifier's DATE predicate (`args.py`), where "not a date" is an
    ordinary answer, not an error.
    """
    if not token:
        return None
    t = token.strip().lower()
    if not t:
        return None

    if t == "today":
        return today
    if t == "tomorrow":
        return today + dt.timedelta(days=1)

    # `-next`: the first such weekday strictly after the next Saturday.
    if t.endswith("-next"):
        day = WEEKDAYS.get(t[:-len("-next")])
        if day is None:
            return None
        if day == 6:
            # Sunday is adjacent to the Saturday anchor -- the Saturday-
            # anchor algorithm can never separate `sun-next` from plain
            # `sun` (always exactly one day past the anchor), so `-next`
            # unconditionally needs one week beyond plain `sun` instead.
            return _next_weekday(today, 6) + dt.timedelta(days=7)
        return _next_weekday(_next_saturday(today), day)

    if t in WEEKDAYS:
        return _next_weekday(today, WEEKDAYS[t])

    m = _ISO_RE.match(t) or _ISO_COMPACT_RE.match(t)
    if m:
        year, month, day = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        try:
            return dt.date(year, month, day)
        except ValueError:
            return None

    m = _DOM_RE.match(t)
    if m:
        dom = int(m.group(1))
        if not 1 <= dom <= 31:
            return None
        return _next_day_of_month(today, dom)

    m = _SLASH_RE.match(t)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
        if year is None:
            # This year, or next year if already past. Today is not past.
            try:
                candidate = dt.date(today.year, month, day)
            except ValueError:
                return None
            if candidate < today:
                try:
                    return dt.date(today.year + 1, month, day)
                except ValueError:        # 29 Feb into a non-leap year
                    return None
            return candidate
        year = int(year)
        if year < 100:
            year += 2000
        try:
            return dt.date(year, month, day)
        except ValueError:
            return None

    return None


def split_past(dates, today):
    """`dates` split into `(past, rest)`, `today` counted as `rest`.

    The server has no record of past bookings, so callers use this to
    drop past dates before ever making a request for them.
    """
    past = [d for d in dates if d < today]
    rest = [d for d in dates if d >= today]
    return past, rest


def is_date_token(token, today):
    """DATE predicate for the classifier.

    Note the documented consequence: `mon` is a date, so `fs find mon` can
    never mean a colleague called Monica. `--name mon` is the escape hatch.
    """
    return parse_date(token, today) is not None


def _ordinal(n):
    # 11th/12th/13th are why this isn't a lookup on the last digit alone.
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def fmt_date(d, today):
    """`Today 27th Aug`, `Tomorrow 28th Aug`, else `Monday 24th Aug` --
    year appended only when it isn't the current one. `Today`/`Tomorrow`
    still carry the day/month tail so they stay legible next to weekday
    rows in a mixed table like `fs list`."""
    tail = f"{_ordinal(d.day)} {_MONTHS[d.month - 1]}"
    if d.year != today.year:
        tail = f"{tail} {d.year}"
    if d == today:
        return f"Today {tail}"
    if d == today + dt.timedelta(days=1):
        return f"Tomorrow {tail}"
    return f"{_DAYNAMES[d.weekday()]} {tail}"


def fmt_weekday(index):
    """`0` -> `Monday`. For `fs office-days`, which prints weekday names with
    no date attached."""
    return _DAYNAMES[index]
