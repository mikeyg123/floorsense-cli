"""`fs checkin` -- confirm today's booking(s) so the ~90-minute auto-release
deadline (`confexpiry`, `floorsense-api-manual.md` §8) doesn't fire.

No arguments at all, unlike `release`'s date/`all` grammar: check-in is
meaningful only for today (§5.1 -- `confirmed` is `false` on every future
booking observed, and the deadline this exists to beat is always *today's*
09:30-ish, not some other day's). There is nothing to point it at, so a desk,
group, name, date, or `all` is a usage error rather than a token this command
would have to decide how to ignore.

`Api.booking_confirm`'s docstring covers the endpoint discovery and the
refusal shapes in detail. The one thing worth repeating here: this command
does NOT replicate the web UI's client-side eligibility check
(`book_confirm_app`/`book_early_activate`, scraped from `/app/site`'s inline
HTML, not any JSON endpoint) before calling `booking-confirm`. It only
short-circuits the two cases it can already tell apart from the booking
record alone -- nothing booked today, and already confirmed -- and lets the
server refuse anything else (e.g. too early) on its own, the same
server-is-authoritative posture `api.py` already takes for the advance-window
policy.
"""

from ..plan import Action, ActionPlan, Kind, confirm, execute
from ..errors import UsageError
from .book_cmd import bookings_for_date
from .list_cmd import own_bookings

__all__ = ["cmd_checkin"]


def cmd_checkin(ctx):
    out, api = ctx.out, ctx.api

    if (ctx.args.args or ctx.args.date or ctx.args.desk or ctx.args.group
            or ctx.args.name or getattr(ctx.args, "all", False)):
        raise UsageError("fs checkin takes no arguments",
                         hint="It always acts on today's booking(s).")

    # After the UsageError check above, not before -- same ordering
    # `release_cmd.py` uses and for the same reason: a bad invocation must
    # fail fast without ever touching the catalog.
    ctx.load_tags()

    out.intent("Checking in today's booking")

    bookings = own_bookings(api, out.today)
    matches = bookings_for_date(bookings, out.today)

    if not matches:
        actions = [Action(out.fmt_date(out.today), None, None, Kind.NOOP,
                          reason="nothing booked today")]
    else:
        actions = [_checkin_row(out, api, b) for b in matches]

    plan = ActionPlan(actions)
    plan = confirm(plan, out, yes=ctx.args.yes, stdin=None)
    return execute(plan, out)


def _checkin_row(out, api, booking):
    subject = out.fmt_date(out.today)
    desk = out.fmt_desk(booking.get("key", "?"))
    if booking.get("confirmed"):
        return Action(subject, desk, None, Kind.NOOP,
                     reason="already checked in")
    bkid = booking.get("bkid")
    return Action(subject, desk, None, Kind.CHECKIN, reason="check in",
                 detail=desk, run=_confirmer(api, bkid))


def _confirmer(api, bkid):
    def run():
        return api.booking_confirm(bkid)
    return run
