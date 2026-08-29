"""`fs checkin` -- no args, today's booking(s) only (manual §5.1: check-in is
meaningful only for today). See `commands/checkin_cmd.py`'s docstring for why
the eligibility gate the web UI applies client-side (`book_confirm_app`/
`book_early_activate`) is deliberately NOT replicated here -- the server is
authoritative and refuses cleanly on its own."""

import datetime as dt
import io

import pytest

from fs_cli.commands.checkin_cmd import cmd_checkin
from fs_cli.errors import ExitCode, UsageError
from fs_cli.render import Output

TODAY = dt.date(2026, 8, 23)
MON = dt.date(2026, 8, 24)


class FakeApi:
    def __init__(self, bookings=None, refuse_message=None):
        self._bookings = bookings or []
        self.confirmed = []
        self._refuse_message = refuse_message

    def booking_list(self):
        return list(self._bookings)

    def booking_summary(self, *a, **kw):
        return {"days": []}

    def booking_confirm(self, bkid):
        if self._refuse_message:
            raise RuntimeError(self._refuse_message)
        self.confirmed.append(bkid)
        return {}


class Config:
    groups = {}
    teams = {}


class Args:
    def __init__(self, args=(), date=(), desk=(), group=(), name=(),
                 yes=True, all=False):
        self.args = list(args)
        self.date = list(date)
        self.desk = list(desk)
        self.group = list(group)
        self.name = list(name)
        self.yes = yes
        self.all = all


class FakeCatalog:
    def __init__(self, tags=None):
        self._tags = tags or {}

    def desk_keys(self):
        return []

    def tag_map(self):
        return dict(self._tags)


class Ctx:
    def __init__(self, out, config, api, catalog, args):
        self.out, self.config, self.api, self.catalog, self.args = \
            out, config, api, catalog, args

    def load_tags(self):
        self.out.tags = self.catalog.tag_map()


def run(api, args=None, config=None, today=TODAY, catalog=None):
    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=today, stdout=stdout, stderr=stderr)
    code = cmd_checkin(Ctx(out, config or Config(), api,
                           catalog or FakeCatalog(), args or Args()))
    out.finish()
    return code, stdout.getvalue(), stderr.getvalue()


def at(day, hour=8):
    return int(dt.datetime.combine(day, dt.time(hour)).astimezone().timestamp())


def test_todays_unconfirmed_booking_is_checked_in():
    api = FakeApi([{"bkid": "1", "key": "L5.D.A", "start": at(TODAY),
                    "confirmed": False}])
    code, stdout, _ = run(api)
    assert code == ExitCode.OK
    assert api.confirmed == ["1"]


def test_the_intent_line_says_today_once_not_twice():
    # Used to be "Checking in today's booking, Today" -- `fmt_date(today)`
    # appended a second "Today" that the sentence already said in words
    # (2026-08-25 fix).
    api = FakeApi([{"bkid": "1", "key": "L5.D.A", "start": at(TODAY),
                    "confirmed": False}])
    _, _, stderr = run(api)
    assert stderr.startswith("Checking in today's booking")
    assert stderr.count("today") + stderr.count("Today") == 1


def test_nothing_booked_today_is_a_noop_not_an_error():
    api = FakeApi([])
    code, stdout, _ = run(api)
    assert code == ExitCode.OK
    assert not api.confirmed
    assert "nothing booked" in stdout.lower()


def test_already_confirmed_is_a_noop_not_a_second_call():
    api = FakeApi([{"bkid": "1", "key": "L5.D.A", "start": at(TODAY),
                    "confirmed": True}])
    code, stdout, _ = run(api)
    assert code == ExitCode.OK
    assert not api.confirmed
    assert "already checked in" in stdout.lower()


def test_a_future_booking_is_ignored_not_confirmed_early():
    api = FakeApi([{"bkid": "1", "key": "L5.D.A", "start": at(MON),
                    "confirmed": False}])
    code, stdout, _ = run(api)
    assert code == ExitCode.OK
    assert not api.confirmed


def test_two_bookings_today_in_different_groups_are_both_confirmed():
    """The one-per-day limit is per GROUP (§8) -- this account can hold two
    desks the same day in two different groups, same as `fs release`."""
    api = FakeApi([{"bkid": "1", "key": "L5.D.A", "start": at(TODAY),
                    "confirmed": False},
                   {"bkid": "2", "key": "L5.D.B", "start": at(TODAY),
                    "confirmed": False}])
    code, stdout, _ = run(api)
    assert code == ExitCode.OK
    assert sorted(api.confirmed) == ["1", "2"]


def test_a_server_refusal_is_reported_not_swallowed():
    """The eligibility gate the web UI checks client-side is not replicated
    here -- the server refuses cleanly on its own (confirmed live, no
    `code`), so this just has to surface whatever it says."""
    api = FakeApi([{"bkid": "1", "key": "L5.D.A", "start": at(TODAY),
                    "confirmed": False}],
                  refuse_message="Booking outside early activate window")
    code, stdout, _ = run(api)
    assert code != ExitCode.OK
    assert "early activate window" in stdout


def test_a_tagged_desk_shows_its_tags(monkeypatch):
    """`--yes`'s post-run summary is terse (date + checkmark only); the
    pre-confirm table, where a checked-in desk's tags actually print, needs
    the interactive path -- same reasoning as `test_release.py`'s
    equivalent, and the same bug class `out.tags` wiring guards against."""
    class ScriptedStdin:
        def isatty(self):
            return True

        def readline(self):
            return "\n"

    monkeypatch.setattr("sys.stdin", ScriptedStdin())
    api = FakeApi([{"bkid": "1", "key": "L5.D.A", "start": at(TODAY),
                    "confirmed": False}])
    catalog = FakeCatalog(tags={"L5.D.A": ("quiet",)})
    _, stdout, _ = run(api, Args(yes=False), catalog=catalog)
    assert "[quiet]" in stdout


@pytest.mark.parametrize("kwargs", [
    dict(args=["mon"]),
    dict(date=["mon"]),
    dict(desk=["L5.D.A"]),
    dict(group=["favourite"]),
    dict(name=["jane"]),
    dict(all=True),
])
def test_any_argument_is_a_usage_error(kwargs):
    api = FakeApi([])
    with pytest.raises(UsageError):
        run(api, Args(**kwargs))
