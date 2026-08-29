"""`fs at <desk|group> [<date>…]` -- who's on those desks, per PLAN.md.

Read-only, no `plan.py` pipeline: nothing here is ever confirmed or run.

**Reads `Catalog.availability()`, not `booking-summary`.** The manual's
§5.1 line "the occupant of a desk is directly available" (`userbookings`
keyed by uid) reads like the answer for this command, but that's only true
when the occupant is you or someone you follow. `fs at` has to answer for
*anyone* sitting at the desk, and `booking-summary` genuinely cannot: it
never mentions a person who isn't a friend. `Catalog.availability()` (the
same source `fs book` reads) carries the true `uid` off
`floorplan-booking`'s per-desk rows regardless of following status, so
that's the read this command uses instead.

The cost that shifts back in return is naming: a `uid` off `availability()`
needs one `GET /app/user?uid=` call to become a name, and the manual is
explicit that fan-out is only a "fallback, not the main path" *for the
whole floor* (§5.1) -- but the target set here is the handful of desks the
user actually asked about, not the floor, so one lookup per DISTINCT
occupant among THOSE desks is the bounded, correct cost, not the N+1 the
manual warns off. Results are cached per command run so the same occupant
across several dates or desks costs one call, not several.

Rows carry the raw desk key/uid/`confirmed` for `--json`; text formatting
(`fmt_date`/`fmt_desk`/colour) happens only when printing, same rule
`find_cmd.py` and `list_cmd.py` follow.

**Cost, stated rather than assumed:** `Catalog.availability(day, planids=...)`
is narrowed here to only the floors `target_keys` actually resolve to
(via `desk_by_key`, free -- desk identity is cached for a month), the same
trick `Catalog.bookable()` already used for `fs book`. Without it, every
date queried every floor with desks in the building regardless of how many
of them the target could possibly be on -- `fs at <group> <several dates>`
was the command that made it add up (`floors × dates` calls, most of them
wasted). A target whose keys don't resolve at all (a stale config entry)
now queries no floor rather than every floor for nothing -- `availability`
already returns `{}` for an empty `planids` list, so the "desk not
found -> shown as free" fallback is unchanged either way.

A past date never reaches `floorplan-booking` at all -- the server keeps no
booking history, so a past date is answered client-side ("no info for past
date") before `catalog.availability()` is ever called.
"""

from dataclasses import dataclass

from .. import render
from ..args import TokenType, Vocabulary, bind
from ..dates import split_past
from ..desks import resolve_group_keys
from ..errors import ExitCode, UsageError
from .find_cmd import is_team_member, team_member_keys

__all__ = ["cmd_at", "occupant_name"]

_ACCEPTS = {TokenType.DATE, TokenType.DESK, TokenType.GROUP}


@dataclass
class _Row:
    date: object
    key: object
    free: bool
    uid: object = None
    occupant: object = None    # "you" / resolved name / None when free
    confirmed: object = None   # only meaningful for today


def _target_label(bound, out):
    """The typed desks/groups, formatted -- the "who/what" half of the
    intent line, same shape as `book_cmd`'s multi-target error hint: a desk
    key goes through `fmt_desk`, a group name is shown as typed."""
    shown = [out.fmt_desk(k) for k in bound[TokenType.DESK]]
    shown += list(bound[TokenType.GROUP])
    return ", ".join(shown)


def _target_keys(bound, cfg, catalog, out):
    keys = list(bound[TokenType.DESK])
    for group in bound[TokenType.GROUP]:
        if group not in cfg.groups:
            raise UsageError(f"no group {group!r} configured")
        keys.extend(resolve_group_keys(cfg.groups[group], catalog.desk_keys(),
                                       out))
    return keys


def occupant_name(api, own_uid, uid, bkid, cache):
    """`state.uid` -> a display name, `"you"` for the caller's own uid, one
    `/user` lookup per distinct uid, memoised in `cache` for the run.

    `bkid` is passed through to `api.user` -- the manual documents this
    endpoint as `GET /user?bkid=N&uid=N`, "for a booking", and the only
    confirmed-live call omitting it is a SELF lookup (`Catalog.own_uid`'s
    docstring). Looking up someone else's uid with no `bkid` was silently
    falling back to the raw uid on every call -- not a name lookup failure
    so much as a lookup this project's own manual never claimed would work
    for a stranger's booking. `state.bkid` off `Catalog.availability()` is
    exactly the booking id the endpoint wants.

    Public (not `_occupant`) so other commands needing "who has this desk"
    can reuse the exact same lookup/cache/fallback shape rather than
    reimplementing it -- `map_cmd.py`'s live-view status line is the other
    caller, from a background thread rather than inline, which is exactly
    why this function is otherwise unremarkable: it does its own error
    swallowing and its own caching, so a caller on any thread can call it
    and just read `cache` back afterward.
    """
    if own_uid and uid == own_uid:
        return "you"
    if uid in cache:
        return cache[uid]
    try:
        info = api.user(uid, bkid=bkid) or {}
        name = info.get("name") or uid
    except Exception:                            # noqa: BLE001
        # A name is a nicety; the uid itself already answered "occupied".
        name = uid
    cache[uid] = name
    return name


def cmd_at(ctx):
    out, cfg, api, catalog = ctx.out, ctx.config, ctx.api, ctx.catalog

    if ctx.args.name:
        raise UsageError("fs at does not take --name",
                         hint="It takes a desk or group, and dates.")
    if getattr(ctx.args, "all", False):
        raise UsageError("fs at does not take --all",
                         hint="It takes a desk or group, and dates.")

    # After the usage-error checks above, not before: a bad `fs at --name`/
    # `--all` must still fail fast without ever touching the catalog (same
    # rule find_cmd.py/release_cmd.py already follow).
    ctx.load_tags()

    vocab = Vocabulary(today=out.today, groups=cfg.groups, teams=cfg.teams,
                       desk_keys=catalog.desk_keys())
    bound = bind(ctx.args.args, vocab, _ACCEPTS, forced={
        TokenType.DATE: ctx.args.date,
        TokenType.DESK: ctx.args.desk,
        TokenType.GROUP: ctx.args.group,
    })

    target_keys = _target_keys(bound, cfg, catalog, out)
    if not target_keys:
        raise UsageError("fs at needs a desk or group",
                         hint="e.g. `fs at 5.235A` or `fs at <group>`.")

    # Narrow `availability()` to the floors `target_keys` are actually on,
    # same trick `Catalog.bookable()` already uses -- resolved via
    # `desk_by_key` (identity, cached for a month, so this costs no extra
    # call). Without this, every date queried every non-empty floor
    # regardless of how many of them the target could possibly be on
    # (this module's own docstring named it as the known cost). A target
    # whose keys don't resolve at all queries no floor rather than every
    # floor for nothing -- `availability` already returns `{}` for an
    # empty `planids` list, so the "desk not found -> shown as free"
    # fallback below is unchanged.
    wanted_planids = set()
    for key in target_keys:
        desk = catalog.desk_by_key(key)
        if desk is not None:
            wanted_planids.add(desk.planid)
    planids = sorted(wanted_planids)

    dates = bound[TokenType.DATE] or [out.today]
    past, dates = split_past(dates, out.today)
    for d in past:
        out.print(f"No info for past date: {out.fmt_date(d)}")

    # A group-only target reads as a *kind* of desk, not one -- "Who's at
    # preferred" leaves "preferred what?" unanswered, so it gets a plural
    # noun; a bare desk key already names the thing, so it doesn't. Mixed
    # desk+group targets are left alone rather than guessed at.
    desks_word = (" desks" if bound[TokenType.GROUP] and not bound[TokenType.DESK]
                 else "")
    # Same reasoning `book_cmd.py`/`checkin_cmd.py` apply: when the date was
    # never typed and just defaulted to today, say so in plain words rather
    # than routing it through `fmt_dates` (which would still say "Today",
    # capitalised mid-sentence, for the one-date case, and reads oddly next
    # to a plural target).
    when = (f", {out.fmt_dates(dates)}" if bound[TokenType.DATE] else " today")

    if not dates:
        out.emit({"rows": []})
        return ExitCode.OK

    out.intent(f"Who's at {_target_label(bound, out)}{desks_word}{when}")

    try:
        own_uid = catalog.own_uid()
    except Exception:                             # noqa: BLE001
        # Best-effort: worst case every occupant is named rather than one of
        # them being recognised as "you" -- never worth failing the command.
        own_uid = None

    names = {}
    rows = []
    for day in dates:
        states = catalog.availability(day, planids=planids)
        for key in target_keys:
            state = states.get(key)
            if state is None or state.free:
                rows.append(_Row(date=day, key=key, free=True))
                continue
            who = (occupant_name(api, own_uid, state.uid, state.bkid, names)
                  if state.uid else "someone")
            rows.append(_Row(date=day, key=key, free=False, uid=state.uid,
                             occupant=who,
                             confirmed=state.confirmed if day == out.today
                             else None))

    team_keys = team_member_keys(cfg.teams)
    if not rows:
        out.print("Nothing to show.")
    else:
        out.print(_render(out, rows, team_keys))

    out.emit({"rows": [
        {"date": r.date, "desk": r.key, "free": r.free, "occupant": r.occupant,
         "confirmed": r.confirmed}
        for r in rows]})
    return ExitCode.OK


def _render(out, rows, team_keys=None):
    """`team_keys` (from `team_member_keys`) stars an occupant's name when
    they're on any configured team -- never applied to "you"/"someone",
    which aren't names to begin with."""
    lines = []
    for r in rows:
        if r.free:
            lines.append([out.fmt_date(r.date), out.fmt_desk(r.key), "free", ""])
            continue
        checkin = ""
        if r.confirmed is not None:
            checkin = (out.good("checked in") if r.confirmed
                      else out.attention("not checked in"))
        occupant = r.occupant
        if (team_keys and occupant not in ("you", "someone")
                and is_team_member(team_keys, uid=r.uid, name=occupant)):
            occupant = f"* {occupant}"
        lines.append([out.fmt_date(r.date), out.fmt_desk(r.key), occupant,
                     checkin])
    return render.table(None, lines)
