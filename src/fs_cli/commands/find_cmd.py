"""The name/team/`following` target logic behind `fs list <name|team>...`,
also reachable as `fs find ...` -- `find` is a bare alias of `list`, not a
command with its own handler. Shared by `list_cmd.py`, `status_cmd.py`,
`at_cmd.py`, `team_cmd.py`; read-only, no `plan.py` pipeline.

Four target shapes:

  * nothing -> your own booking(s) (`own_bookings`, shared with
    `fs book`/`fs release`).
  * a bare NAME -> `user_search`, one call per name (§5.2). Every fuzzy
    hit is shown -- never picked for you, unlike `fs book`'s desk target.
  * a TEAM -> the same per-name `user_search` path, fanned out over the
    team's configured members.
  * `following` -> `booking-summary`'s Following list and `userbookings`
    -- one call for every person at once, not per-person (§5.1).

`following` is peeled out before classification (same trick
`release_cmd.py` uses for `all`) -- `args.py`'s predicates would
otherwise classify it as a NAME.

**Every row-builder returns raw `_Row` records, not display strings.**
Formatting happens once at print time; `--json` reads the same records
raw -- a desk key and ISO date, never "Today"/"5.217A".
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
    uid: object = None         # the person's uid, when known (render_rows's
                                # team-star match key); None on `_own_rows`
    desc: object = None        # user_search hit's `desc` -- disambiguates
                                # two accounts sharing a display name


def team_member_names(entries):
    """A team entry is a plain name string or a `{name=...}` table --
    `config.toml` is hand-editable, and typing a bare name is natural.
    Both spellings accepted; anything with neither is dropped rather
    than raised. No leading underscore: `team_cmd.py` reuses this too.
    """
    names = []
    for entry in entries or []:
        name = entry.get("name") if isinstance(entry, dict) else entry
        if name:
            names.append(str(name))
    return names


def team_member_keys(teams):
    """Every configured team's members, as a set of match keys -- a
    lowercased uid and lowercased name per entry, both in the same set
    since a plain-string entry has no uid. Used to star a name in
    `fs at`/`fs list`/`fs find`'s output for ANY configured team, not
    just the one searched for.

    `following` is deliberately excluded: resolving it costs a
    `booking-summary` call that free callers shouldn't pay just to
    decide whether to print a star.
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
    result -- uid checked first (unambiguous), name as fallback."""
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
    """One `user_search` hit's booking on `day`, from `future[]` or --
    today only -- the location fields on the hit itself when the person
    is currently seated (§5.2)."""
    for record in hit.get("future") or []:
        start = record.get("start")
        if start is not None and dt.datetime.fromtimestamp(start).date() == day:
            return record
    if day == today and hit.get("key"):
        return hit
    return None


def _search_window(dates, today):
    """The `start`/`finish` window `user_search` wants -- must cover
    every requested date, since `future[]` is bounded by exactly this
    window."""
    days = dates or [today]
    lo, hi = min(days), max(days)
    start = dt.datetime.combine(lo, dt.time.min).astimezone()
    finish = dt.datetime.combine(hi, dt.time.max).astimezone()
    return int(start.timestamp()), int(finish.timestamp())


def _search_rows(out, api, names, dates):
    """`fs list`/`fs find <name>...` -- one row per `user_search` hit per
    day, EVERY hit shown. `name`/`desc` kept exactly as the server
    returned them -- these rows also back `--json`; `render_rows` is
    where two rows colliding on name get told apart, using `desc`.

    Two different search names can turn up the same account twice --
    deduped here by `uid` before rows are built. A hit with no `uid` is
    kept unconditionally rather than deduped on some fallback field that
    could collide and drop a real hit. Rows are sorted by name (then
    `uid` as a tiebreaker, then date).
    """
    # A typed name that's also a team member (or listed on two teams)
    # otherwise triggers a redundant `user_search` for the same person.
    names = list(dict.fromkeys(names))
    start, finish = _search_window(dates, out.today)

    no_match = []
    matched = {}       # uid -> hit, first hit wins
    unkeyed = []        # hits with no uid -- never deduped
    for name in names:
        hits = [h for h in api.user_search(name, start, finish)
                if isinstance(h, dict)]
        if not hits:
            no_match.append(name)
            continue
        for hit in hits:
            hit_name = hit.get("name") or name
            uid = hit.get("uid")
            if uid:
                matched.setdefault(str(uid), (hit_name, hit))
            else:
                unkeyed.append((hit_name, hit))

    rows = [_Row(date=None, note="no match", name=name) for name in no_match]
    for hit_name, hit in list(matched.values()) + unkeyed:
        hit_uid = hit.get("uid")
        hit_desc = hit.get("desc")
        for day in dates:
            record = _future_hit_for_day(hit, day, out.today)
            rows.append(_Row(date=day, booking=record,
                             note=None if record else "no booking",
                             name=hit_name, uid=hit_uid, desc=hit_desc))

    rows.sort(key=lambda r: ((r.name or "").lower(), str(r.uid or ""),
                             r.date or dt.date.min))
    return rows


def _following_rows(out, api, dates):
    # Floored at 2, not 1: `days=1` is a server edge case -- `users`
    # comes back populated but `days` comes back EMPTY, so every lookup
    # would report "unknown" even for today. `days=2` onward works; the
    # requested `dates` are still what's rendered.
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
    """Tokens -> `(following, names, dates, teams, typed_names)`. `dates`
    is exactly what was typed, possibly empty -- the caller decides the
    default. `names` is expanded with every typed team's members (one
    `user_search` per person); `typed_names` is what was actually typed
    -- pass that to `target_phrase`, or a team target's intent line
    repeats the whole member list.
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

    # `desk_keys` is needed only so a desk-shaped token classifies as
    # DESK and `bind()` rejects it by name ("looks like a desk, which
    # this command doesn't take") -- without it, a token like `5.235`
    # falls through to NAME (see `classify`'s fallback) and becomes a
    # silent, always-empty `user_search` instead of a clear usage error.
    # Costs no extra catalog fetch in practice: `cmd_list` calls
    # `ctx.load_tags()` (which also fetches `catalog.desks()`)
    # immediately after this returns, on every invocation.
    vocab = Vocabulary(today=out.today, groups=cfg.groups, teams=cfg.teams,
                       desk_keys=ctx.catalog.desk_keys())
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

    return following, names, dates, teams, typed_names


def target_phrase(following, names, teams=()):
    """`(following, names, teams)` -> "everyone you follow" / "team eng" /
    "jane, bob" / "you" -- the "who" half of `fs list`'s intent line.
    `teams` shown as `team <name>`, not expanded to members. Names shown
    as typed, not resolved -- resolution happens per-row."""
    if following:
        return "everyone you follow"
    parts = [f"team {t}" for t in teams] + list(names)
    if parts:
        return ", ".join(parts)
    return "you"


def rows_for(out, api, following, names, dates):
    """`(following, names, dates)` -> rows, dispatched on what
    `resolve_targets` found. `cmd_list`'s own no-target path has separate
    handling (locker + own bookings together), so `_own_rows` here has
    no caller through a command today -- kept for correctness of all
    three `resolve_targets` outputs, not just the two reached today."""
    if following:
        return _following_rows(out, api, dates)
    if names:
        return _search_rows(out, api, names, dates)
    return _own_rows(out, api, dates)


def render_rows(out, rows, team_keys=None):
    """Rows -> cells -> `render.table`'s header-less mode; no header
    suits a listing this short. `team_keys` stars a row's name for any
    configured team.

    Two DIFFERENT accounts can share the same display name (confirmed
    live) -- unlabelled, that reads as one account duplicated. A name
    shared by more than one distinct uid gets `desc` appended in parens;
    a name with only one uid is untouched.
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
    # `r.name` is present on every row or none -- every line has the same
    # length, note always last column.
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
