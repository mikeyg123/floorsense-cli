"""`fs list` -- the locker, then every booking from today forward.

The smallest genuinely useful command, and the one that establishes the two
rendering rules the rest of the tool follows: a date is always
`Output.fmt_date`, a desk is always `Output.fmt_desk`.

Two things here are about deadlines that pass silently, which is this
project's recurring theme:

  * **the locker** is reassigned to someone else roughly six months in, with
    no notification (`floorsense-api-manual.md` §8). So the warning
    escalates -- notice at 30 days, warning at 14, a banner at 7 -- rather
    than firing once and being missed.
  * **an un-checked-in booking auto-releases at `confexpiry`**, 09:30 on the
    day (§8). Same class of problem, so it gets the same treatment: today's
    unconfirmed rows say when they evaporate.

Check-in is shown for TODAY'S ROWS ONLY. `confirmed` is `false` on every
future booking by construction, so rendering it there would decorate every
row with a warning that means nothing.

A past date is answered client-side ("no info for past date") without
ever calling `own_bookings`/`rows_for` -- the server keeps no booking
history, so there is nothing a round-trip for one could return.

**Why two sources rather than one.** §5 describes bare `booking-list` as
returning own *future* bookings, and the Phase 0 capture cannot say whether
"future" includes today -- the account happened to have no desk booked on
capture day, so both sources returned nothing for it. That is an untested
gap sitting underneath the entire check-in feature: if `booking-list`
excludes today, the auto-release deadline is dead code in real use.

`booking-summary` settles it structurally rather than empirically:
`days[0]` **is** today by construction (§5.1), and it carries your own
bookings for each day. So today comes from there, everything else from
`booking-list`, and the two are merged by `bkid`. The overlap costs one
extra call and removes the assumption entirely -- and §5.1's warning that a
multi-day booking appears under every day it covers with the same `bkid` is
exactly why the merge dedupes on `bkid` rather than on date.
"""

import datetime as dt

from .. import render
from ..dates import split_past
from ..errors import ExitCode

__all__ = ["cmd_list", "locker_warning", "checkin_note", "checkin_style",
           "checkin_row_style", "checkin_rows", "own_bookings",
           "NOTICE_DAYS", "WARNING_DAYS", "BANNER_DAYS"]

NOTICE_DAYS = 30
WARNING_DAYS = 14
BANNER_DAYS = 7


def _local_date(unix):
    return dt.datetime.fromtimestamp(unix).date() if unix else None


def _local_time(unix):
    return dt.datetime.fromtimestamp(unix).strftime("%H:%M") if unix else None


def locker_warning(locker, today):
    """The escalating locker notice, or None while the expiry is far off.

    Escalation rather than a single flag at 30 days: the failure mode is
    silent reassignment, and one notice a month out is exactly the kind of
    thing that scrolls past unread.
    """
    finish = locker.get("finish")
    expiry = _local_date(finish)
    if not expiry:
        return None
    days = (expiry - today).days
    key = locker.get("key", "?")

    if days < 0:
        return ("banner", f"Locker {key} EXPIRED {-days} days ago "
                          f"-- it may already have been reassigned.")
    if days <= BANNER_DAYS:
        return ("banner", f"Locker {key} expires in {days} days. Renew it in "
                          f"person or it is reassigned silently.")
    if days <= WARNING_DAYS:
        return ("warning", f"Locker {key} expires in {days} days -- renew it "
                           f"in person.")
    if days <= NOTICE_DAYS:
        return ("notice", f"Locker {key} expires in {days} days.")
    return None


def checkin_note(booking, today):
    """Only meaningful for today (§5.1), so only rendered for today.

    Shared with `find_cmd.py`: `confirmed`/`confexpiry` mean the same thing
    on every booking record this API returns, whoever it belongs to.

    **Plain text, never ANSI** -- colour must go through `render.table`'s
    `row_style` hook instead of being baked into the cell here: applying
    ANSI to an already-padded, already-joined line can't perturb the
    column widths computed from raw length, applying it to a cell BEFORE
    padding can. `checkin_style` (below) is the colour half; callers
    combine them via `checkin_row_style` at render time.
    """
    if _local_date(booking.get("start")) != today:
        return ""
    if booking.get("confirmed"):
        return "checked in"
    deadline = _local_time(booking.get("confexpiry"))
    if deadline:
        return f"not checked in -- auto-releases {deadline}"
    return "not checked in"


def checkin_style(booking, today):
    """`checkin_note`'s colour, kept separate so it can be applied via
    `render.table`'s `row_style` hook instead of baked into the cell.

    Returns a `render.THEME` name ("good"/"attention"), not a raw colour --
    what each means is decided in one place (`render.THEME`), not here."""
    if _local_date(booking.get("start")) != today:
        return None
    return "good" if booking.get("confirmed") else "attention"


def checkin_row_style(out, notes, styles, note_col):
    """A `render.table` `cell_style` that colours each row's checkin-note
    cell, from parallel `notes`/`styles` lists (one entry per data row,
    `None` where there's nothing to colour).

    A `cell_style`, not a `row_style`, despite the name -- kept because it
    reads naturally at the call sites below. Matches by POSITION
    (`col_index == note_col`), which the caller passes because every row
    it builds puts the checkin note in that same column -- correct by
    construction, unlike a CONTENT match (`raw == notes[row_index]`),
    which would only colour the right cell as long as no other cell in
    the row happens to equal the checkin-note text.
    """
    colorer = {"good": out.good, "attention": out.attention}

    def style(row_index, col_index, padded, raw):
        if col_index != note_col:
            return padded
        note = notes[row_index] if row_index < len(notes) else None
        color = styles[row_index] if row_index < len(styles) else None
        if not note or not color or raw != note:
            return padded
        return colorer[color](padded)
    return style


def checkin_rows(triples):
    """Unzip `(cells, note, style)` triples into the three parallel lists
    `render.table`/`checkin_row_style` need: `(rows, notes, styles)`.

    Building one `(cells, note, style)` triple per row and unzipping it
    here, once, keeps `notes`/`styles` from drifting apart the way two
    separate hand-built lists (one `.append()` per list per row, in both
    `find_cmd.render_rows` and `list_cmd.cmd_list`) could; a spacer or
    non-checkin row (a locker row, a "no match" row) is simply
    `(cells, None, None)`.
    """
    rows, notes, styles = [], [], []
    for cells, note, style in triples:
        rows.append(cells)
        notes.append(note)
        styles.append(style)
    return rows, notes, styles


def _covers_today_or_later(booking, today):
    """Is this booking still ahead of, or covering, today?

    Tested on `finish` rather than `start`, because §5.1 warns that a
    multi-day booking appears under every day it covers and its `start` may
    be *before* that day. Filtering on `start` would therefore drop a booking
    that began yesterday and still covers today -- making today's desk vanish
    from `fs list` in exactly the case where the user is sitting at it.

    `start` is the fallback for rows with no `finish`, and a row with neither
    is kept: something is wrong with it, and dropping it silently is worse
    than showing it as an unknown date.
    """
    end = _local_date(booking.get("finish")) or _local_date(booking.get("start"))
    return end >= today if end else True


def own_bookings(api, today):
    """Every own booking from today forward, from both sources, deduped.

    `booking-list` is the wider window (no client-side end bound);
    `booking-summary.days[0]` is the one guaranteed to include today. Neither
    alone is known to cover both.
    """
    found = {}
    for row in api.booking_list():
        if row.get("bkid") and not row.get("released"):
            found[str(row["bkid"])] = row

    try:
        summary = api.booking_summary()
    except Exception:                        # noqa: BLE001
        # A best-effort supplement must never be what breaks `fs list`: the
        # primary source has already answered.
        return sorted(found.values(), key=lambda b: b.get("start") or 0)

    for day in (summary or {}).get("days") or []:
        for row in day.get("bookings") or []:
            # Dedupe by bkid, never by date: a multi-day booking appears
            # under every day it covers with the same bkid (§5.1).
            if row.get("bkid") and not row.get("released"):
                found.setdefault(str(row["bkid"]), row)

    return sorted((b for b in found.values() if _covers_today_or_later(b, today)),
                  key=lambda b: b.get("start") or 0)


def cmd_list(ctx):
    out, today = ctx.out, ctx.out.today
    api, catalog = ctx.api, ctx.catalog

    # Local import: `find_cmd.py` imports `checkin_note`/`own_bookings` from
    # this module, so importing it back at module level would be circular.
    from .find_cmd import (json_row, render_rows, resolve_targets, rows_for,
                           target_phrase, team_member_keys)

    following, names, dates, teams, typed_names = resolve_targets(
        ctx, usage="fs list")
    # After resolve_targets()'s own UsageError checks, not before: a bad
    # `fs list --desk ...`/`--all` must still fail fast without ever
    # touching the catalog (same rule find_cmd.py/release_cmd.py follow).
    ctx.load_tags()

    if following or names:
        # A team/name target reuses `find_cmd.py`'s row-building and
        # rendering wholesale rather than re-deriving it -- no locker line,
        # since a locker is only ever yours, never a target's. (`fs find`
        # is a bare alias of this command now, not a second implementation
        # of this branch -- see `find_cmd.py`'s module docstring.)
        target_dates = dates or [today]
        past, rest = split_past(target_dates, today)
        for d in past:
            out.print(f"No info for past date: {out.fmt_date(d)}")
        team_keys = team_member_keys(ctx.config.teams)
        if not rest:
            out.emit({"rows": []})
            return ExitCode.OK
        out.intent(f"Showing bookings for "
                  f"{target_phrase(following, typed_names, teams)}, "
                  f"{out.fmt_dates(rest)}")
        rows = rows_for(out, api, following, names, rest)
        if not rows:
            out.print("No matches.")
        else:
            out.print(render_rows(out, rows, team_keys=team_keys))
        out.emit({"rows": [json_row(out, r, team_keys=team_keys)
                           for r in rows]})
        return ExitCode.OK

    past, rest = split_past(dates, today) if dates else ([], [])
    for d in past:
        out.print(f"No info for past date: {out.fmt_date(d)}")
    if dates and not rest:
        out.emit({"lockers": [], "bookings": []})
        return ExitCode.OK

    if rest:
        out.intent(f"Showing your bookings for {out.fmt_dates(rest)}")
    else:
        out.intent("Showing your upcoming bookings")

    lockers = catalog.lockers()
    bookings = own_bookings(api, today)
    if rest:
        wanted = set(rest)
        bookings = [b for b in bookings
                   if _local_date(b.get("start")) in wanted]

    # Locker row(s) go through the same render.table() call as the booking
    # rows below, so the two share one set of column widths and everything
    # lines up -- without forcing the locker row into the booking row's
    # [date, item, note] order, which reads backwards for a locker (its
    # identity, not its date, is the thing worth scanning first).
    locker_triples = []
    for locker in lockers:
        expiry = _local_date(locker.get("finish"))
        days = (expiry - today).days if expiry else None
        locker_triples.append(([f"Locker {locker.get('key', '?')}",
                                f"Expires: {out.fmt_date(expiry)}"
                                if expiry else "Expires: unknown",
                                f"({days} days)" if days is not None else ""],
                               None, None))

    booking_triples = []
    for b in bookings:
        day = _local_date(b.get("start"))
        note = checkin_note(b, today)
        booking_triples.append(([out.fmt_date(day) if day else "?",
                                 out.fmt_desk(b.get("key", "?")), note],
                                note, checkin_style(b, today)))

    if locker_triples and booking_triples:
        triples = locker_triples + [(["", "", ""], None, None)] + booking_triples
    else:
        triples = locker_triples + booking_triples

    if triples:
        rows, notes, styles = checkin_rows(triples)
        # All three row shapes (locker, booking, spacer) are exactly
        # [item, date, note] -- the note is always the last column.
        out.print(render.table(None, rows,
                                cell_style=checkin_row_style(
                                    out, notes, styles, note_col=2)))
    if not bookings:
        out.print("No desks booked.")

    # Warnings AFTER the data, and on stderr, so `fs list | ...` stays clean.
    for locker in lockers:
        warning = locker_warning(locker, today)
        if warning:
            level, text = warning
            out.warn(out.danger(text) if level == "banner" else
                     out.attention(text) if level == "warning" else text)

    out.emit({
        "lockers": [{"key": locker.get("key"),
                     "expires": _local_date(locker.get("finish"))}
                    for locker in lockers],
        "bookings": [{"date": _local_date(b.get("start")),
                      "desk": b.get("key"),
                      "bkid": b.get("bkid"),
                      "confirmed": bool(b.get("confirmed")),
                      "confirm_by": (_local_time(b.get("confexpiry"))
                                     if _local_date(b.get("start")) == today
                                     else None)}
                     for b in bookings]})
    return ExitCode.OK
