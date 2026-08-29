"""`fs release [<date>... | all]`."""

import datetime as dt
import io

import pytest

from fs_cli.commands.release_cmd import cmd_release
from fs_cli.errors import ExitCode, UsageError
from fs_cli.render import Output

TODAY = dt.date(2026, 8, 21)
MON = dt.date(2026, 8, 24)
TUE = dt.date(2026, 8, 25)


class FakeApi:
    def __init__(self, bookings=None):
        self._bookings = bookings or []
        self.released = []
        self.booking_list_calls = 0

    def booking_list(self):
        self.booking_list_calls += 1
        return list(self._bookings)

    def booking_summary(self, *a, **kw):
        return {"days": []}

    def booking_release(self, bkid):
        self.released.append(bkid)
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


def run(api, args, config=None, today=TODAY, catalog=None):
    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=today, stdout=stdout, stderr=stderr)
    code = cmd_release(Ctx(out, config or Config(), api,
                           catalog or FakeCatalog(), args))
    out.finish()
    return code, stdout.getvalue(), stderr.getvalue()


def at(day, hour=8):
    return int(dt.datetime.combine(day, dt.time(hour)).astimezone().timestamp())


def test_a_named_date_releases_its_booking():
    api = FakeApi([{"bkid": "1", "key": "L5.D.A", "start": at(MON)}])
    code, stdout, _ = run(api, Args(args=["mon"]))
    assert code == ExitCode.OK
    assert api.released == ["1"]


def test_a_date_with_no_booking_is_a_noop_not_an_error():
    api = FakeApi([])
    code, stdout, _ = run(api, Args(args=["mon"]))
    assert code == ExitCode.OK
    assert not api.released


def test_all_releases_every_own_booking():
    api = FakeApi([{"bkid": "1", "key": "L5.D.A", "start": at(MON)},
                   {"bkid": "2", "key": "L5.D.B", "start": at(TUE)}])
    code, stdout, _ = run(api, Args(args=["all"]))
    assert code == ExitCode.OK
    assert sorted(api.released) == ["1", "2"]


def test_the_all_flag_is_equivalent_to_the_bare_word():
    api = FakeApi([{"bkid": "1", "key": "L5.D.A", "start": at(MON)}])
    code, stdout, _ = run(api, Args(all=True))
    assert code == ExitCode.OK
    assert api.released == ["1"]


def test_all_combined_with_a_date_is_a_usage_error():
    api = FakeApi([{"bkid": "1", "key": "L5.D.A", "start": at(MON)}])
    with pytest.raises(UsageError):
        run(api, Args(args=["all", "mon"]))


def test_no_dates_and_no_all_defaults_to_today():
    api = FakeApi([{"bkid": "1", "key": "L5.D.A", "start": at(TODAY)}])
    code, stdout, _ = run(api, Args())
    assert code == ExitCode.OK
    assert api.released == ["1"]


def test_no_dates_and_no_booking_today_is_a_noop_not_an_error():
    api = FakeApi([])
    code, stdout, _ = run(api, Args())
    assert code == ExitCode.OK
    assert not api.released


def test_a_tagged_desk_shows_its_tags(monkeypatch):
    """`--yes`'s post-run summary is terse (date + checkmark only, as the
    other tests here confirm) -- the pre-confirm table, where a released
    desk's tags actually print, needs the interactive path."""
    class ScriptedStdin:
        def isatty(self):
            return True

        def readline(self):
            return "\n"

    monkeypatch.setattr("sys.stdin", ScriptedStdin())
    api = FakeApi([{"bkid": "1", "key": "L5.D.A", "start": at(MON)},
                   {"bkid": "2", "key": "L5.D.B", "start": at(TUE)}])
    catalog = FakeCatalog(tags={"L5.D.A": ("quiet",)})
    _, stdout, _ = run(api, Args(args=["mon", "tue"], yes=False),
                       catalog=catalog)
    assert "[quiet]" in stdout


def test_two_bookings_the_same_day_are_both_released():
    """The one-per-day limit is per GROUP (§8) -- this account can hold two
    desks on one day in two different groups, and releasing that day must
    not silently drop one of them."""
    api = FakeApi([{"bkid": "1", "key": "L5.D.A", "start": at(MON)},
                   {"bkid": "2", "key": "L5.D.B", "start": at(MON)}])
    code, stdout, _ = run(api, Args(args=["mon"]))
    assert code == ExitCode.OK
    assert sorted(api.released) == ["1", "2"]


def test_multiple_dates_release_each_booking():
    api = FakeApi([{"bkid": "1", "key": "L5.D.A", "start": at(MON)},
                   {"bkid": "2", "key": "L5.D.B", "start": at(TUE)}])
    code, stdout, _ = run(api, Args(args=["mon", "tue"]))
    assert code == ExitCode.OK
    assert sorted(api.released) == ["1", "2"]


def test_nothing_to_release_says_so():
    api = FakeApi([])
    code, stdout, _ = run(api, Args(args=["all"]))
    assert code == ExitCode.OK
    assert "Nothing to release." in stdout



def test_a_desk_or_group_argument_is_a_usage_error():
    api = FakeApi([])
    with pytest.raises(UsageError):
        run(api, Args(desk=["L5.D.A"]))


class ExplodingCatalog:
    """Pins that a usage error still costs no catalog access -- `tag_map()`
    must never run before `cmd_release`'s own UsageError checks."""
    def tag_map(self):
        raise AssertionError("catalog touched before validation")


def test_a_desk_argument_usage_error_never_touches_the_catalog():
    api = FakeApi([])
    with pytest.raises(UsageError):
        run(api, Args(desk=["L5.D.A"]), catalog=ExplodingCatalog())


def test_all_combined_with_a_date_never_touches_the_catalog():
    api = FakeApi([{"bkid": "1", "key": "L5.D.A", "start": at(MON)}])
    with pytest.raises(UsageError):
        run(api, Args(args=["all", "mon"]), catalog=ExplodingCatalog())


def test_a_past_date_is_reported_without_querying_the_server():
    api = FakeApi([])
    yesterday = (TODAY - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    code, stdout, _ = run(api, Args(args=[yesterday]))
    assert code == ExitCode.OK
    assert "No info for past date" in stdout
    assert api.booking_list_calls == 0
    assert not api.released


def test_a_past_date_mixed_with_a_real_one_still_releases_the_real_one():
    api = FakeApi([{"bkid": "1", "key": "L5.D.A", "start": at(MON)}])
    yesterday = (TODAY - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    code, stdout, _ = run(api, Args(args=[yesterday, "mon"]))
    assert code == ExitCode.OK
    assert "No info for past date" in stdout
    assert api.released == ["1"]
