"""The name/team/`following` target logic behind `fs list <name|team>...`,
also reachable as `fs find ...` -- `find` is a bare alias of `list`
(`cli.py`'s `HANDLERS["find"] = cmd_list`, same shape as `ls`), not a
command with its own handler here. This module exists because
`list_cmd.py`, `status_cmd.py`, `at_cmd.py`, and `team_cmd.py` all reach
into it for the functions below; read-only throughout, no `plan.py`
pipeline, because there is nothing to confirm.

Four target shapes, chosen by what's on the command line:

  * nothing (no name/team/`following`) -> your own booking(s), same source
    (`own_bookings`) `fs list`/`fs book`/`fs release` already share.
  * a bare NAME -> `user_search`, one call per name (§5.2). Every fuzzy hit
    is shown -- this command never picks one for you, unlike `fs book`'s
    desk target, because guessing which "Jane" you meant is a worse failure
    than a short list.
  * a TEAM -> the same per-name `user_search` path, fanned out over the
    team's configured members. A team is nothing more to this command than
    a saved list of names -- it carries no special server call.
  * the reserved word `following` -> `booking-summary`'s Following list and
    `userbookings`, the one case that's genuinely one call for every person
    at once rather than one call per person (§5.1's "not an N+1 fan-out").

`following` is peeled out of the token list before classification, the same
trick `release_cmd.py` uses for `all`: `args.py`'s ordered predicates would
otherwise classify it as a NAME (nobody configures a team or person actually
called "following"), and searching for a person by that name would silently
return nothing instead of doing what was meant.

Check-in status (`checkin_note`, shared with `fs list`) is shown on every
row for today only -- `confirmed` is `false` on every future booking by
construction (§5.1), regardless of whose record it's read from.

**Every row-builder returns raw `_Row` records, not display strings.** Text
formatting (`fmt_date`/`fmt_desk`/`checkin_note`) happens once, at print
time; `--json` reads the same records straight -- a raw desk key and an ISO
date, never `"Today"` or `"5.217A"` (render.py's rule, and `fs list`'s own
`--json` test pins it: dates and keys go out raw, only the printed line is
formatted).
"""

import datetime as dt
from dataclasses import dataclass

from .. import render
from ..args import TokenType, Vocabulary, bind
from ..errors import UsageError
from .book_cmd import bookings_for_date
from .list_cmd import (checkin_note, checkin_row_style, checkin_rows,
                       checkin_style, own_bookings)

__all__ = ["team_member_names", "team_member_keys", "is_team_member",
           "resolve_targets", "rows_for", "render_rows", "json_row",
           "target_phrase"]

_ACCEPTS = {TokenType.DATE, TokenType.NAME, TokenType.TEAM}


@dataclass
class _Row:
    date: object
    booking: object = None     # the raw record (has key/confirmed/...), or None
    note: object = None        # why there's no booking, when booking is None
    name: object = None        # the person, when this isn't the own-desk view
    uid: object = None         # the person's uid, when known -- `render_rows`'s
                                # team-star match key; `None` on `_own_rows`
    desc: object = None        # `user_search` hit's `desc` (an internal
                                # id/extension) -- `None` unless the row came
                                # from `_search_rows`; used to disambiguate
                                # two different accounts sharing a display
                                # name, never shown otherwise


def team_member_names(entries):
    """A team entry is a plain name string or a `{name=...}` table --
    `config.toml` is hand-editable (PLAN.md), and typing a bare name is the
    natural thing to do even though the brief's example shows a table (which
    also carries `uid` -- unused here, but not something a plain team needs;
    only `fs team following` resolves and uses a `uid`, per `PLAN.md`'s step
    8 handoff note). Both spellings are accepted; anything with neither is
    dropped rather than raised, the same tolerance `office_day_indexes`
    gives a typo. No leading underscore: `team_cmd.py` reuses this too,
    same convention `book_cmd`/`release_cmd` already share.
    """
    names = []
    for entry in entries or []:
        name = entry.get("name") if isinstance(entry, dict) else entry
        if name:
            names.append(str(name))
    return names


def team_member_keys(teams):
    """Every configured team's members, as a set of match keys -- a
    lowercased uid and a lowercased name per entry, both put in the same set
    since a plain-string entry (see `team_member_names`) has no uid to match
    on. Used to star a name in `fs at`/`fs list`/`fs find`'s output when the
    person is on ANY configured team, not just the one (if any) that was
    searched for.

    `following` is deliberately excluded: it's server-backed
    (`team_cmd.py`'s module docstring), and resolving its membership costs a
    `booking-summary` call `fs at`/`fs list --json`-free callers shouldn't
    pay just to decide whether to print a star. `cfg.teams` alone -- the
    hand-authored lists `fs team <name> add/remove/set` builds -- is what
    stays free.
    """
    keys = set()
    for entries in (teams or {}).values():
        for entry in entries or []:
            if isinstance(entry, dict):
                uid, name = entry.get("uid"), entry.get("name")
            else:
                uid, name = None, entry
            if uid:
                keys.add(str(uid).strip().lower())
            if name:
                keys.add(str(name).strip().lower())
    return keys


def is_team_member(team_keys, uid=None, name=None):
    """Whether `uid`/`name` matches something in `team_member_keys`'s
    result -- uid checked first since it's the unambiguous identity, name as
    the fallback for a hand-typed team entry that has no uid at all."""
    if uid and str(uid).strip().lower() in team_keys:
        return True
    if name and str(name).strip().lower() in team_keys:
        return True
    return False


def _entry_date(day_entry):
    y, m, d = day_entry.get("year"), day_entry.get("month"), day_entry.get("day")
    if not (y and m and d):
        return None
    return dt.date(y, m, d)


def _future_hit_for_day(hit, day, today):
    """One `user_search` hit's booking on `day`, from `future[]` or -- for
    today only -- the location fields carried directly on the hit itself
    when the person is currently seated (§5.2)."""
    for record in hit.get("future") or []:
        start = record.get("start")
        if start is not None and dt.datetime.fromtimestamp(start).date() == day:
            return record
    if day == today and hit.get("key"):
        return hit
    return None


def _search_window(dates, today):
    """The `start`/`finish` window `user_search` wants -- it must cover
    every requested date, since `future[]` is bounded by exactly this
    window and nothing wider (§5.2's corrected reading)."""
    days = dates or [today]
    lo, hi = min(days), max(days)
    start = dt.datetime.combine(lo, dt.time.min).astimezone()
    finish = dt.datetime.combine(hi, dt.time.max).astimezone()
    return int(start.timestamp()), int(finish.timestamp())


def _search_rows(out, api, names, dates):
    """`fs list`/`fs find <name>...` -- one row per `user_search` hit per
    day, EVERY hit shown (this command never guesses which one you meant,
    per the module docstring). `name`/`desc` are kept exactly as the server
    returned them -- no disambiguating suffix baked in here, since that's
    display formatting and this function's rows also back `--json`
    (`json_row`). `render_rows` is where two rows that collide on name get
    told apart, using `desc`.
    """
    start, finish = _search_window(dates, out.today)
    rows = []
    for name in names:
        hits = [h for h in api.user_search(name, start, finish)
                if isinstance(h, dict)]
        if not hits:
            rows.append(_Row(date=None, note="no match", name=name))
            continue
        for hit in hits:
            hit_name = hit.get("name") or name
            hit_uid = hit.get("uid")
            hit_desc = hit.get("desc")
            for day in dates:
                record = _future_hit_for_day(hit, day, out.today)
                rows.append(_Row(date=day, booking=record,
                                 note=None if record else "no booking",
                                 name=hit_name, uid=hit_uid, desc=hit_desc))
    return rows


def _following_rows(out, api, dates):
    # Floored at 2, not 1: `days=1` is a server edge case, not just the
    # documented top-end cap (§5.1) --
    # `users` comes back fully populated but `days` comes back EMPTY, so
    # every date lookup below would miss and report "unknown" even for
    # today. `days=2` onward behaves correctly and still starts at today,
    # so asking for one extra day we don't need is the workaround; the
    # requested `dates` are still what gets rendered, this only changes
    # what's requested from the server.
    span = max((max(dates) - out.today).days + 1, 1) if dates else 1
    summary = api.booking_summary(days=max(span, 2)) or {}
    users = [u for u in (summary.get("users") or []) if isinstance(u, dict)]
    if not users:
        return []

    by_day = {}
    for entry in summary.get("days") or []:
        d = _entry_date(entry)
        if d is not None:
            by_day[d] = entry.get("userbookings") or {}

    rows = []
    for user in sorted(users, key=lambda u: u.get("name") or ""):
        uid, name = user.get("uid"), user.get("name") or user.get("uid")
        for day in dates:
            if day not in by_day:
                rows.append(_Row(date=day, note="unknown", name=name, uid=uid))
                continue
            bookings = by_day[day].get(uid) or []
            if not bookings:
                rows.append(_Row(date=day, note="not booked", name=name,
                                 uid=uid))
            for b in bookings:
                rows.append(_Row(date=day, booking=b, name=name, uid=uid))
    return rows


def _own_rows(out, api, dates):
    bookings = own_bookings(api, out.today)
    rows = []
    for day in dates:
        matches = bookings_for_date(bookings, day)
        if not matches:
            rows.append(_Row(date=day, note="no desk booked"))
        for b in matches:
            rows.append(_Row(date=day, booking=b))
    return rows


def resolve_targets(ctx, usage="this command"):
    """Tokens -> `(following, names, dates, teams, typed_names)`, called
    from `list_cmd.py` (`usage="fs list"` always -- `find` is a bare alias
    of `list`, so this never runs under any other name any more).

    `dates` is exactly what was typed -- possibly empty; the caller defaults
    it (`target_dates = dates or [today]` for a name/team/`following`
    target, `list_cmd.cmd_list`'s own separate no-target path defaults
    differently -- "every upcoming booking" -- so the default belongs to
    the caller, not here).

    `names` and `typed_names` diverge on purpose: `names` is expanded with
    every typed team's members, because `rows_for` needs one `user_search`
    per person regardless of how they were named. `typed_names` is what was
    actually typed -- pass THAT to `target_phrase` (with `teams`), not
    `names`, or a team target's intent line repeats the whole member list
    right next to the "team <name>" that already says it.
    """
    out, cfg = ctx.out, ctx.config

    if ctx.args.desk or ctx.args.group:
        raise UsageError(f"{usage} does not take a desk or group",
                         hint="It takes a name, a team, `following`, and "
                              "dates.")
    if getattr(ctx.args, "all", False):
        raise UsageError(f"{usage} does not take --all",
                         hint="It takes a name, a team, `following`, and "
                              "dates.")

    tokens = list(ctx.args.args)
    following = False
    remaining = []
    for t in tokens:
        if t.strip().lower() == "following":
            following = True
        else:
            remaining.append(t)

    vocab = Vocabulary(today=out.today, groups=cfg.groups, teams=cfg.teams)
    bound = bind(remaining, vocab, _ACCEPTS,
                forced={TokenType.DATE: ctx.args.date,
                        TokenType.NAME: ctx.args.name})
    dates = bound[TokenType.DATE]

    if following and (bound[TokenType.NAME] or bound[TokenType.TEAM]):
        raise UsageError("`following` cannot be combined with a name or team")

    teams = list(bound[TokenType.TEAM])
    typed_names = list(bound[TokenType.NAME])
    names = list(typed_names)
    for team in teams:
        members = team_member_names(cfg.teams.get(team))
        if not members:
            out.warn(f"team {team!r} has no members configured")
        names.extend(members)

    # `names` (expanded with team members) is what `rows_for` searches on --
    # every member needs its own `user_search`. `typed_names` is what
    # `target_phrase` shows: `teams` already names the team, so echoing its
    # members too would just be noise.
    return following, names, dates, teams, typed_names


def target_phrase(following, names, teams=()):
    """`(following, names, teams)` -> "everyone you follow" / "team eng" /
    "jane, bob" / "you" -- the "who" half of `fs list`'s intent line for a
    name/team/`following` target. `teams` is shown as `team <name>` rather
    than expanded to its members -- "Showing bookings for team eng" says
    what was typed; the member list is what the rows below it show, not
    what this line needs to repeat. Names are shown as typed, not resolved
    -- resolution happens per-row (`user_search`), not here, and an
    ambiguous hit is exactly what the rows themselves show."""
    if following:
        return "everyone you follow"
    parts = [f"team {t}" for t in teams] + list(names)
    if parts:
        return ", ".join(parts)
    return "you"


def rows_for(out, api, following, names, dates):
    """`(following, names, dates)` -> rows, dispatched on what
    `resolve_targets` found -- all three target shapes, including "none of
    the above" (`_own_rows`). Called from `list_cmd.py`'s name/team/
    `following`-target branch only; `cmd_list`'s own no-target path has its
    own, separate handling there (locker + own bookings together), so the
    `_own_rows` branch here currently has no caller through a command --
    kept anyway, since `rows_for`'s whole point is being correct for any of
    `resolve_targets`'s three outputs, not just the two `list_cmd.py`
    happens to reach today."""
    if following:
        return _following_rows(out, api, dates)
    if names:
        return _search_rows(out, api, names, dates)
    return _own_rows(out, api, dates)


def render_rows(out, rows, team_keys=None):
    """Rows -> cells -> `render.table`'s header-less mode; no header suits a
    listing this short.

    `team_keys` (from `team_member_keys`) stars a row's name when the person
    is on any configured team -- `None`/empty is simply "nobody starred",
    the shape a caller with no teams configured, or `_own_rows`'s rows
    (which never carry a name at all), already gets for free.

    Two DIFFERENT accounts can share the exact same display name (confirmed
    live: two "Alex Chen"s, distinct `uid`s, one `fs list alex`) -- with
    no way to tell their rows apart, that reads as one account duplicated
    rather than two real people. So a name shared by more than one distinct
    uid among `rows` gets that row's `desc` (an internal id/extension,
    `floorsense-api-manual.md`'s `/user` shape -- the same field
    `team_cmd.resolve_person`'s `pick_one` label already disambiguates an
    equally ambiguous `user_search` hit with) appended in parens; a name
    with only one uid behind it -- the common case -- is untouched.
    """
    uids_by_name = {}
    for r in rows:
        if r.name is not None and r.uid is not None:
            uids_by_name.setdefault(r.name, set()).add(r.uid)
    collides = {name for name, uids in uids_by_name.items() if len(uids) > 1}

    triples = []
    for r in rows:
        cells = []
        if r.name is not None:
            label = (f"{r.name} ({r.desc})" if r.name in collides and r.desc
                     else r.name)
            star = "* " if team_keys and is_team_member(
                team_keys, uid=r.uid, name=r.name) else ""
            cells.append(f"{star}{label}")
        cells.append(out.fmt_date(r.date) if r.date else "?")
        if r.booking:
            note = checkin_note(r.booking, out.today)
            cells.append(out.fmt_desk(r.booking.get("key", "?")))
            cells.append(note)
            style = checkin_style(r.booking, out.today)
        else:
            note, style = None, None
            cells.append(r.note or "?")
            cells.append("")
        triples.append((cells, note, style))
    lines, notes, styles = checkin_rows(triples)
    # `r.name` is either present on every row or none (one of `_search_rows`
    # `/_following_rows`/`_own_rows` runs per call, never a mix), so every
    # line built above has the same length and the note is always its last
    # column.
    note_col = (len(lines[0]) - 1) if lines else 0
    return render.table(None, lines,
                        cell_style=checkin_row_style(out, notes, styles,
                                                     note_col=note_col))


def json_row(out, r, team_keys=None):
    doc = {"date": r.date, "desk": r.booking.get("key") if r.booking else None}
    if r.name is not None:
        doc["name"] = r.name
        doc["uid"] = r.uid
        doc["desc"] = r.desc
        doc["on_team"] = bool(team_keys) and is_team_member(
            team_keys, uid=r.uid, name=r.name)
    if not r.booking:
        doc["status"] = r.note
    else:
        today_row = r.date == out.today
        doc["confirmed"] = bool(r.booking.get("confirmed")) if today_row else None
    return doc
