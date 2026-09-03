"""`fs at <desk|group> [<date>…]` -- who's on those desks.

Read-only, no `plan.py` pipeline: nothing here is ever confirmed or run.

**Reads `Catalog.availability()`, not `booking-summary`.** §5.1's
`userbookings` (keyed by uid) only covers you or someone you follow --
`fs at` has to answer for *anyone* sitting at the desk. `availability()`
carries the true `uid` off `floorplan-booking`'s per-desk rows
regardless of following status.

The cost that shifts back is naming: a `uid` needs one `GET /app/user`
call to become a name. The target set here is the handful of desks
asked about, not the floor, so one lookup per DISTINCT occupant among
THOSE desks is bounded, not the N+1 the manual warns off for a whole
floor. Cached per command run.

Rows carry the raw desk key/uid/`confirmed` for `--json`; text
formatting happens only when printing, same rule `find_cmd.py`/
`list_cmd.py` follow.

`Catalog.availability(day, planids=...)` is narrowed to only the floors
`target_keys` resolve to (via `desk_by_key`, free -- cached for a
month) -- without it, every date queried every floor regardless of how
many the target could possibly be on.

A past date never reaches `floorplan-booking` -- the server keeps no
booking history, so it's answered client-side before `catalog.availability()`
is ever called.
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
    """The typed desks/groups, formatted -- a desk key goes through
    `fmt_desk`, a group name is shown as typed."""
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


def occupant_name(api, own_uid, uid, bkid, cache, catalog=None):
    """`state.uid` -> a display name, `"you"` for the caller's own uid,
    one `/user` lookup per distinct uid, memoised in `cache` for the run.
    `bkid`, when known (`state.bkid` off `Catalog.availability()`), is
    the booking id the endpoint's "for a booking" framing wants --
    looking up a stranger's uid with none silently falls back to the raw
    uid on every call.

    `catalog`, when given, backs `cache` with `Catalog.cached_user_name`/
    `remember_user_name` -- a persistent `uid -> name` cache in
    `cache.json`, so a name resolved on an earlier run (or by `fs map`'s
    background fetch) is read instead of re-asking the server. Never
    persists the API-failure fallback (`name = uid`) -- that isn't a
    real name.

    Public so other commands needing "who has this desk" can reuse the
    same lookup/cache/fallback shape -- `map_cmd.py`'s status line is
    the other caller, from a background thread.
    """
    if own_uid and uid == own_uid:
        return "you"
    if uid in cache:
        return cache[uid]
    if catalog is not None:
        cached = catalog.cached_user_name(uid)
        if cached:
            cache[uid] = cached
            return cached
    try:
        info = api.user(uid, bkid=bkid) or {}
        name = info.get("name")
    except Exception:                            # noqa: BLE001
        name = None
    if name:
        if catalog is not None:
            catalog.remember_user_name(uid, name)
    else:
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
    # `--all` must still fail fast without touching the catalog.
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

    # Narrow `availability()` to the floors `target_keys` are actually
    # on, resolved via `desk_by_key` (cached, no extra call) -- same
    # trick `Catalog.bookable()` uses. A target whose keys don't resolve
    # queries no floor rather than every floor for nothing.
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

    # A group-only target reads as a *kind* of desk -- "Who's at
    # preferred" needs a plural noun; a bare desk key already names the
    # thing. Mixed desk+group targets are left alone.
    desks_word = (" desks" if bound[TokenType.GROUP] and not bound[TokenType.DESK]
                 else "")
    # When the date defaulted to today, say so in plain words rather
    # than `fmt_dates` ("Today", capitalised mid-sentence, reads oddly
    # next to a plural target).
    when = (f", {out.fmt_dates(dates)}" if bound[TokenType.DATE] else " today")

    if not dates:
        out.emit({"rows": []})
        return ExitCode.OK

    out.intent(f"Who's at {_target_label(bound, out)}{desks_word}{when}")

    try:
        own_uid = catalog.own_uid()
    except Exception:                             # noqa: BLE001
        # Best-effort: worst case every occupant is named rather than
        # one being recognised as "you".
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
            who = (occupant_name(api, own_uid, state.uid, state.bkid, names,
                                 catalog)
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
    """`team_keys` stars an occupant's name for any configured team --
    never applied to "you"/"someone", which aren't names."""
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
