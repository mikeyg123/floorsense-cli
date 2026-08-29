"""`fs book [<desk>...|<group>] [<date>...] [new]` -- the rank-comparison
command PLAN.md specs out in full, built on `plan.py`'s confirm/execute
pipeline rather than re-deriving the gather/propose/select/run loop.

One target, many dates. The target is either one or more bare desks (an
ad-hoc preference list, in the order typed -- a single desk is just that
list of one), a named `[groups]` entry (an ordered preference list), or --
given neither -- the configured default group (`config.toml`'s
`[preferences] default_group`, `preferred` unless changed -- see
`config.DEFAULT_GROUP_NAME`). A desk list and a group are still mutually
exclusive -- mixing them, or giving more than one group, is a `UsageError`
rather than either being silently dropped. For each date the row is decided by
comparing what's currently booked against the best free+bookable desk in the
target, per PLAN.md's rank table:

    current None,  best found   -> CREATE
    current None,  no best      -> BLOCKED
    current == best              -> NOOP  (already booked)
    current ranks better than best (still in the target) -> NOOP
    current ranks worse, or isn't in the target at all    -> REPLACE

The last row is the one PLAN.md calls out as easy to miss: a booking outside
the target is still replaced, never left alone. And REPLACE always goes
through `booking-update` on the existing `bkid` -- never release-then-book,
which risks the day's one-desk-per-group limit refusing the re-create
(`api.py`'s `refusal_kind`, DESK_LIMIT).

`new`, a literal keyword like `list_cmd.py`'s `following`, drops any date
that already has an own booking -- in ANY group, not just this run's target
-- before the dates above the table ever reach it: `fs book new` after
office days books only the gaps, instead of re-confirming every date it
already holds a desk on.

"Best" comes from `catalog.bookable()`, not `catalog.availability()`: a free
desk the server won't let *you* advance-book is not a candidate (PLAN.md's
"three things step 7 must not get wrong", #2).

A date that's already past, or beyond `Config.book_ahead_days`'s advance
window, is a `BLOCKED` row decided before `catalog.bookable()` is ever
called for it (`Config.classify_booking_date`) -- the server keeps no
booking history and won't accept the write either way, so there's nothing
to gain from asking it.
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
    """1-based position of `key` in `group_keys`, or None if it isn't there.
    Local rather than `desks._rank`: that helper walks every NAMED group
    looking for the first hit, but here there is exactly one ordered list --
    the target itself -- and its order IS the preference to compare against.
    """
    want = normalise(key)
    for i, k in enumerate(group_keys):
        if normalise(k) == want:
            return i + 1
    return None


def bookings_for_date(bookings, day):
    """Every own booking that covers `day`. Plural, not singular: the
    one-desk-per-day limit the manual records is per GROUP (§8), so this
    account can hold two desks the same day in two different groups --
    `fs release <date>` releasing only the first of them would be exactly
    the silent partial action this project's error philosophy rejects.
    Covers on `finish` first, same reasoning `list_cmd._covers_today_or_later`
    documents: a multi-day booking's `start` can be before the day it still
    covers."""
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
    comparison -- which target-per-desk-group already assumes one current
    booking per group, so the first match is the relevant one. Bookings in
    OTHER groups the same day are outside the target and don't affect the
    comparison. See `bookings_for_date` for the plural case `fs release`
    needs."""
    found = bookings_for_date(bookings, day)
    return found[0] if found else None


def cmd_book(ctx):
    out, cfg, api, catalog = ctx.out, ctx.config, ctx.api, ctx.catalog

    if ctx.args.name or ctx.args.all:
        raise UsageError("fs book does not take --name or --all",
                         hint="It takes a desk, a group, and dates.")

    # After the usage-error check above, not before: a bad `fs book --name`/
    # `--all` must still fail fast without ever touching the catalog (same
    # rule find_cmd.py/release_cmd.py already follow).
    ctx.load_tags()

    # `new` is a literal keyword, stripped before `bind()` sees the tokens --
    # same shape `list_cmd.py`/`find_cmd.py` use for `following`. It isn't
    # part of `Vocabulary`: it names no desk, group, date, or team, so
    # classifying it there would mean teaching every other command's
    # `Vocabulary` about a word only `fs book` cares about.
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
        # Desk keys are the raw catalog form here (`bind()` already resolved
        # them via `match_desk`) -- render.py's rule ("a desk is ALWAYS
        # `Output.fmt_desk`") applies to this hint same as anywhere else, so
        # a group name is shown as typed and a desk key goes through
        # `fmt_desk` rather than leaking `L5.D.217A` at the user.
        shown = [out.fmt_desk(d) for d in desk_targets] + group_targets
        raise UsageError("fs book takes a desk list or a group, not both",
                         hint=f"Got: {', '.join(shown)}.")
    if len(group_targets) > 1:
        raise UsageError("fs book takes at most one group",
                         hint=f"Got: {', '.join(group_targets)}.")

    if desk_targets:
        # `desk_targets` is already in the order typed -- `bind()` appends
        # each classified token in encounter order -- so it doubles as an
        # ad-hoc preference list with no further sorting: the rank-table
        # comparison below (`_group_rank`) treats it exactly like a named
        # group's ordered desk list, no special-casing needed.
        target_keys = desk_targets
        target_shown = ", ".join(out.fmt_desk(k) for k in target_keys)
        target_label = target_shown  # only ever read on the group branch's
        # "nothing available in {target_label!r}" reasons; kept in step so
        # it isn't a dangling reference should that reachability change.
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
        # `booking_for_date` -- the same "current" `booking_for_date`/rank
        # comparison below uses -- is ANY own booking covering the day, not
        # just one in the target group: "already have a booking" means
        # that generically, the same reading `already_booked`'s NOOP
        # branches already give the phrase everywhere else in this file.
        dates = [d for d in dates if booking_for_date(bookings, d) is None]

    if new_only and not dates:
        # Checked ahead of `used_office_days`, not folded into its branch:
        # `fs book new` after office days is the invocation this keyword
        # exists for, and it's exactly the case where every date can come
        # out already booked. Left to fall into the "on office-days"
        # branch below, this would print that line and then nothing --
        # `actions` ends up empty, `confirm()` returns before it can say
        # " nothing to change" (that message only fires when the plan has
        # rows but none are selectable, not when it's empty), and
        # `execute` on an empty plan has nothing to report either. Silent
        # success and silent no-op must not look the same on stderr.
        out.intent(f"Booking {target_shown} -- nothing new to book")
    elif used_office_days:
        # No date list here even without `new`: PLAN.md's rank table and
        # this command's own docstring both treat "on office-days" as the
        # target description, not a promise to enumerate every date --
        # spelling out "Tomorrow, Thursday 27th Aug, ..." for a run that
        # may cover weeks buries the one thing worth saying (which desks)
        # under a list nobody asked to read.
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
            # Nothing better is available -- PLAN.md's table has no row for
            # this (it only spells out REPLACE when a best is found), so
            # this is the conservative reading: keep what you have rather
            # than manufacture a replacement target that doesn't exist. Still
            # a NOOP, not BLOCKED -- there IS a desk held for this day, so
            # `cmd_book`'s "nothing selectable" check below must not raise
            # NoDeskAvailable for it.
            #
            # The wording forks on whether the held desk is even IN the
            # target: "already booked" is right when it's a target member
            # ranked below everything else that was tried and found
            # unavailable, but reads as a lie when the held desk belongs to
            # a DIFFERENT group entirely and the target (e.g. a
            # `--group quiet-corners` book while sitting on a `preferred`
            # desk) simply has nothing free -- that's "no change", not "this
            # is what quiet-corners gave you".
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

    # Exit 6/NO_DESK is reserved for "nothing available" (PLAN.md, `plan.py`
    # docstring) -- `execute` can't decide that on its own, since a BLOCKED
    # row is indistinguishable from a NOOP one once nothing is selected. So
    # it's decided here, before `confirm`, and only when NOTHING in the plan
    # is selectable: a mix of one CREATE and one BLOCKED is a partial
    # success, not a failure, so it must still return OK.
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
