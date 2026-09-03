"""`fs book [<desk>...|<group>] [<date>...] [new]` -- the rank-comparison
command, built on `plan.py`'s confirm/execute pipeline.

One target, many dates. The target is one or more bare desks (an ad-hoc
preference list, in typed order), a named `[groups]` entry, or -- given
neither -- the configured default group. A desk list and a group are
mutually exclusive; mixing them, or giving more than one group, is a
`UsageError`. For each date, the row is decided by comparing what's
currently booked against the best free+bookable desk in the target:

    current None,  best found   -> CREATE
    current None,  no best      -> BLOCKED
    current == best              -> NOOP  (already booked)
    current ranks better than best (still in the target) -> NOOP
    current ranks worse, or isn't in the target at all    -> REPLACE

The last row is easy to miss: a booking outside the target is still
replaced, never left alone. REPLACE always goes through `booking-update`
on the existing `bkid` -- never release-then-book, which risks the
day's one-desk-per-group limit refusing the re-create.

`new`, a literal keyword like `list_cmd.py`'s `following`, drops any date
that already has an own booking -- in ANY group -- before the table above
ever reaches it: `fs book new` after office days books only the gaps.

"Best" comes from `catalog.bookable()`, not `catalog.availability()`: a
free desk the server won't let *you* advance-book is not a candidate.

A date that's past, or beyond `book_ahead_days`, is `BLOCKED` before
`catalog.bookable()` is ever called for it -- nothing to gain from asking.
"""

import datetime as dt

from ..api import book_start_for
from ..args import TokenType, Vocabulary, bind
from ..config import BOOKING_BLOCK_REASONS
from ..desks import normalise, resolve_group_keys
from ..errors import NoDeskAvailable, UsageError
from ..plan import Action, ActionPlan, Kind, confirm, execute
from .list_cmd import own_bookings

__all__ = ["cmd_book", "booking_for_date", "bookings_for_date"]

_ACCEPTS = {TokenType.DATE, TokenType.GROUP, TokenType.DESK}


def _group_rank(key, group_keys):
    """1-based position of `key` in `group_keys`, or None. Local rather
    than `desks._rank`: here there is exactly one ordered list -- the
    target itself -- and its order IS the preference to compare against.
    """
    want = normalise(key)
    for i, k in enumerate(group_keys):
        if normalise(k) == want:
            return i + 1
    return None


def bookings_for_date(bookings, day):
    """Every own booking that covers `day`. Plural: the one-desk-per-day
    limit is per GROUP (§8), so this account can hold two desks the same
    day in two different groups. Covers on `finish` first -- a multi-day
    booking's `start` can be before the day it still covers."""
    found = []
    for b in bookings:
        start = b.get("start")
        finish = b.get("finish") or start
        if start is None:
            continue
        start_d = dt.datetime.fromtimestamp(start).date()
        finish_d = dt.datetime.fromtimestamp(finish).date()
        if start_d <= day <= finish_d:
            found.append(b)
    return found


def booking_for_date(bookings, day):
    """The single own booking covering `day`, for `fs book`'s rank
    comparison -- assumes one current booking per group, so the first
    match is the relevant one. See `bookings_for_date` for the plural
    case `fs release` needs."""
    found = bookings_for_date(bookings, day)
    return found[0] if found else None


def cmd_book(ctx):
    out, cfg, api, catalog = ctx.out, ctx.config, ctx.api, ctx.catalog

    if ctx.args.name or ctx.args.all:
        raise UsageError("fs book does not take --name or --all",
                         hint="It takes a desk, a group, and dates.")

    # After the usage-error check above, not before: a bad `fs book --name`/
    # `--all` must still fail fast without touching the catalog.
    ctx.load_tags()

    # `new` is a literal keyword, stripped before `bind()` sees the tokens
    # (same shape `list_cmd.py` uses for `following`) -- not part of
    # `Vocabulary`, or every other command's Vocabulary would need to know it.
    tokens = list(ctx.args.args)
    new_only = False
    remaining = []
    for t in tokens:
        if t.strip().lower() == "new":
            new_only = True
        else:
            remaining.append(t)

    vocab = Vocabulary(today=out.today, groups=cfg.groups, teams=cfg.teams,
                       desk_keys=catalog.desk_keys())
    bound = bind(remaining, vocab, _ACCEPTS, forced={
        TokenType.DATE: ctx.args.date,
        TokenType.DESK: ctx.args.desk,
        TokenType.GROUP: ctx.args.group,
    })

    desk_targets, group_targets = bound[TokenType.DESK], bound[TokenType.GROUP]
    if desk_targets and group_targets:
        # Desk keys are the raw catalog form here (`bind()` already
        # resolved them) -- goes through `fmt_desk` rather than leaking
        # `L5.D.217A` at the user.
        shown = [out.fmt_desk(d) for d in desk_targets] + group_targets
        raise UsageError("fs book takes a desk list or a group, not both",
                         hint=f"Got: {', '.join(shown)}.")
    if len(group_targets) > 1:
        raise UsageError("fs book takes at most one group",
                         hint=f"Got: {', '.join(group_targets)}.")

    if desk_targets:
        # Already in typed order (`bind()` appends in encounter order),
        # so it doubles as an ad-hoc preference list -- `_group_rank`
        # treats it exactly like a named group's ordered list.
        target_keys = desk_targets
        target_shown = ", ".join(out.fmt_desk(k) for k in target_keys)
        target_label = target_shown  # only read by the group branch's
        # "nothing available in {target_label!r}" reasons
        is_desk_target = True
    else:
        name = (group_targets[0] if group_targets else cfg.default_group)
        if name not in cfg.groups:
            raise UsageError(
                f"no group {name!r} configured" if group_targets
                else f"no default group {name!r} configured",
                hint=f"Set one with `fs desks {name} set <desk>...`.")
        target_label = name
        target_shown = name
        target_keys = resolve_group_keys(cfg.groups[name], catalog.desk_keys(),
                                          out)
        is_desk_target = False

    used_office_days = not bound[TokenType.DATE]
    dates = bound[TokenType.DATE]
    if not dates:
        dates = cfg.next_office_days(out.today)
        if not dates:
            raise UsageError(
                "no dates given and no office days configured",
                hint="Pass dates, or set office days with `fs office-days`.")

    bookings = own_bookings(api, out.today)

    if new_only:
        # ANY own booking covering the day, not just one in the target
        # group -- "already have a booking" means that generically.
        dates = [d for d in dates if booking_for_date(bookings, d) is None]

    if new_only and not dates:
        # Checked ahead of `used_office_days`: left to fall into the
        # "on office-days" branch below, this would print that line and
        # then nothing -- `actions` ends up empty, and neither `confirm`
        # nor `execute` reports anything for an empty plan. Silent
        # success and silent no-op must not look the same on stderr.
        out.intent(f"Booking {target_shown} -- nothing new to book")
    elif used_office_days:
        # No date list here even without `new`: "on office-days" is the
        # target description, not a promise to enumerate every date --
        # spelling out weeks of dates buries the one thing worth saying.
        suffix = " desks" if not is_desk_target else ""
        out.intent(f"Booking {target_shown}{suffix} on office-days")
    else:
        out.intent(f"Booking {target_shown}, {out.fmt_dates(dates)}")

    book_day_start = catalog.book_day_start_mins()

    def already_booked(day, current_key):
        return Action(out.fmt_date(day), out.fmt_desk(current_key), None,
                     Kind.NOOP, reason="already booked")

    actions = []
    for day in dates:
        block = cfg.classify_booking_date(day, out.today, out.now)
        if block != "ok":
            actions.append(Action(out.fmt_date(day), None, None, Kind.BLOCKED,
                                  reason=BOOKING_BLOCK_REASONS[block]))
            continue

        current = booking_for_date(bookings, day)
        current_key = current.get("key") if current else None

        states = catalog.bookable(day, keys=target_keys)
        best = states[0] if states else None

        if current_key and best and normalise(current_key) == normalise(best.key):
            actions.append(already_booked(day, current_key))
            continue

        if current is not None and current_key and best:
            cur_rank = _group_rank(current_key, target_keys)
            best_rank = _group_rank(best.key, target_keys)
            if (cur_rank is not None and best_rank is not None
                    and cur_rank < best_rank):
                actions.append(already_booked(day, current_key))
                continue
            actions.append(Action(
                out.fmt_date(day), out.fmt_desk(current_key),
                out.fmt_desk(best.key), Kind.REPLACE,
                detail=out.fmt_desk(best.key),
                run=_updater(api, current["bkid"], best, day)))
            continue

        if current_key and not best:
            # Nothing better available -- conservative reading: keep what
            # you have rather than manufacture a replacement that doesn't
            # exist. Still a NOOP, not BLOCKED -- a desk IS held.
            #
            # Wording forks on whether the held desk is even IN the
            # target: "already booked" fits a target member ranked below
            # everything else tried; "no change" fits a held desk in a
            # DIFFERENT group entirely (e.g. `--group quiet-corners`
            # while sitting on a `preferred` desk) -- that's not "this is
            # what quiet-corners gave you".
            if is_desk_target or _group_rank(current_key, target_keys) is not None:
                reason = "already booked"
            else:
                reason = f"no change -- nothing available in {target_label!r}"
            actions.append(Action(out.fmt_date(day), out.fmt_desk(current_key),
                                  None, Kind.NOOP, reason=reason))
            continue

        if best:
            actions.append(Action(
                out.fmt_date(day), None, out.fmt_desk(best.key), Kind.CREATE,
                detail=out.fmt_desk(best.key),
                run=_creator(api, day, book_day_start, best)))
        else:
            reason = (f"{target_shown} not available"
                      if is_desk_target
                      else f"nothing available in {target_label!r}")
            actions.append(Action(out.fmt_date(day), None, None, Kind.BLOCKED,
                                  reason=reason))

    # Exit 6/NO_DESK is "nothing available" -- decided here, before
    # `confirm`, only when NOTHING in the plan is selectable: a mix of
    # one CREATE and one BLOCKED is a partial success, still OK.
    if actions and not any(a.selectable for a in actions):
        blocked = [a for a in actions if a.kind is Kind.BLOCKED]
        if blocked:
            raise NoDeskAvailable(
                f"nothing available for {len(blocked)} of {len(actions)} "
                f"requested date{'s' if len(actions) != 1 else ''}")

    plan = ActionPlan(actions)
    plan = confirm(plan, out, yes=ctx.args.yes, stdin=None)
    code = execute(plan, out)
    return code


def _creator(api, day, book_day_start, state):
    def run():
        return api.booking_create(book_start_for(day, book_day_start),
                                   state.key, state.desk.cid, day=day)
    return run


def _updater(api, bkid, state, day):
    def run():
        return api.booking_update(bkid, state.key, state.desk.cid, day=day)
    return run
