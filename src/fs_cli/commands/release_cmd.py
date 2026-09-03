"""`fs release [<date>... | all]` -- built on the same `plan.py` pipeline
as `fs book`, but with only one kind of row: RELEASE.

No date and no `all` defaults to today, not a usage error.

`all` is a reserved word, not a date -- "every own booking from today
forward", matching `fs list`. `--all` is the flag spelling. Either form
skips date parsing entirely: `all` would otherwise classify as a NAME,
silently releasing nothing.

A requested date with no booking on it becomes a NOOP row saying so, not
an error. A PAST date never reaches `own_bookings` -- the server keeps
no booking history, so it's reported and dropped before the fetch.
"""

import datetime as dt

from ..args import TokenType, Vocabulary, bind
from ..dates import split_past
from ..errors import UsageError
from ..plan import Action, ActionPlan, Kind, confirm, execute
from .book_cmd import bookings_for_date
from .list_cmd import own_bookings

__all__ = ["cmd_release"]

_ACCEPTS = {TokenType.DATE}


def cmd_release(ctx):
    out, api = ctx.out, ctx.api

    if ctx.args.desk or ctx.args.group or ctx.args.name:
        raise UsageError("fs release does not take a desk, group or name",
                         hint="It takes dates, or `all`.")

    tokens = list(ctx.args.args)
    all_requested = bool(getattr(ctx.args, "all", False))
    remaining = []
    for t in tokens:
        if t.strip().lower() == "all":
            all_requested = True
        else:
            remaining.append(t)

    if all_requested and remaining:
        raise UsageError("`all` cannot be combined with dates",
                         hint=f"Got: {', '.join(remaining)}.")

    # After every UsageError check above: a bad `fs release` invocation
    # must still fail fast without touching the catalog.
    ctx.load_tags()

    if all_requested:
        bookings = own_bookings(api, out.today)
        out.intent("Releasing every booking from today forward")
        actions = [_release_row(out, api, b) for b in bookings]
    else:
        vocab = Vocabulary(today=out.today, groups=ctx.config.groups,
                           teams=ctx.config.teams,
                           desk_keys=ctx.catalog.desk_keys())
        bound = bind(remaining, vocab, _ACCEPTS,
                    forced={TokenType.DATE: ctx.args.date})
        dates = bound[TokenType.DATE] or [out.today]
        past, rest = split_past(dates, out.today)
        for d in past:
            out.print(f"No info for past date: {out.fmt_date(d)}")
        out.intent(f"Releasing bookings for {out.fmt_dates(dates)}")
        actions = []

        # Nothing left to release against -- skip the fetch entirely.
        bookings = own_bookings(api, out.today) if rest else []
        for day in rest:
            # Plural: the one-per-day limit is per-group (§8), so this
            # account can hold two desks the same day in two groups.
            matches = bookings_for_date(bookings, day)
            if not matches:
                actions.append(Action(out.fmt_date(day), None, None,
                                      Kind.NOOP, reason="nothing booked"))
            else:
                for booking in matches:
                    actions.append(_release_row(out, api, booking, day=day))

    if not actions:
        out.print("Nothing to release.")

    plan = ActionPlan(actions)
    plan = confirm(plan, out, yes=ctx.args.yes, stdin=None)
    return execute(plan, out)


def _release_row(out, api, booking, day=None):
    if day is None:
        start = booking.get("start")
        day = dt.datetime.fromtimestamp(start).date() if start else None
    subject = out.fmt_date(day) if day else "?"
    desk = out.fmt_desk(booking.get("key", "?"))
    return Action(subject, desk, None, Kind.RELEASE, detail=desk,
                 run=_releaser(api, booking.get("bkid")))


def _releaser(api, bkid):
    def run():
        return api.booking_release(bkid)
    return run
