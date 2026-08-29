"""`fs book` -- the rank-comparison table from PLAN.md, end to end through
`cmd_book`. `catalog` and `api` are both fakes here rather than
`FixtureApi`: the rank logic is what's under test, not the catalog's own
caching (that's `test_catalog.py`'s job) -- so availability is stated
directly per day rather than reconstructed from a floorplan capture.
"""

import datetime as dt
import io
from types import SimpleNamespace

import pytest

from fs_cli.commands.book_cmd import cmd_book
from fs_cli.errors import ExitCode, NoDeskAvailable, UsageError
from fs_cli.render import Output

TODAY = dt.date(2026, 8, 21)
MON = dt.date(2026, 8, 24)          # the next office day after TODAY (Fri)


class FakeState:
    def __init__(self, key):
        self.key = key
        self.desk = SimpleNamespace(cid=f"cid-{key}")


class FakeCatalog:
    """`bookable_by_day[day]` is the set of keys the server would accept an
    advance booking for on that day; `desk_keys` is every real key, for
    resolving config group entries."""

    def __init__(self, desk_keys, bookable_by_day=None, book_day_start=480,
                 tags=None):
        self._desk_keys = desk_keys
        self._by_day = bookable_by_day or {}
        self._book_day_start = book_day_start
        self._tags = tags or {}
        self.bookable_calls = []

    def desk_keys(self):
        return list(self._desk_keys)

    def bookable(self, day, keys=None):
        self.bookable_calls.append(day)
        available = self._by_day.get(day, set())
        keys = keys if keys is not None else self._desk_keys
        return [FakeState(k) for k in keys if k in available]

    def book_day_start_mins(self):
        return self._book_day_start

    def tag_map(self):
        return dict(self._tags)


class FakeApi:
    def __init__(self, bookings=None):
        self._bookings = bookings or []
        self.created = []
        self.updated = []

    def booking_list(self):
        return list(self._bookings)

    def booking_summary(self, *a, **kw):
        return {"days": []}

    def booking_create(self, start, key, cid, day=None):
        self.created.append((start, key, cid))
        return {"bkid": "new"}

    def booking_update(self, bkid, key, cid, day=None):
        self.updated.append((bkid, key, cid))
        return {}

    def booking_release(self, bkid):
        raise AssertionError("book should never release")


class Config:
    def __init__(self, groups=None, teams=None, office_days=(),
                 book_ahead_days=10, default_group="preferred",
                 day_opening_time=None):
        self.groups = groups or {}
        self.teams = teams or {}
        self.office_days = list(office_days)
        self.book_ahead_days = book_ahead_days
        self.default_group = default_group
        self.day_opening_time = day_opening_time

    def next_office_days(self, today):
        from fs_cli.dates import WEEKDAYS
        wanted = {WEEKDAYS[d] for d in self.office_days}
        out = []
        for offset in range(1, self.book_ahead_days + 1):
            d = today + dt.timedelta(offset)
            if d.weekday() in wanted:
                out.append(d)
        return out

    def booking_window(self, today, now):
        from fs_cli.config import _booking_window
        return _booking_window(today, now, self.book_ahead_days,
                               self.day_opening_time)

    def classify_booking_date(self, day, today, now):
        from fs_cli.config import _classify_booking_date
        return _classify_booking_date(day, today, now, self.book_ahead_days,
                                      self.day_opening_time)


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


class Ctx:
    def __init__(self, out, config, api, catalog, args):
        self.out, self.config, self.api, self.catalog, self.args = \
            out, config, api, catalog, args

    def load_tags(self):
        self.out.tags = self.catalog.tag_map()


def run(catalog, api, config, args, today=TODAY, now=None):
    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=today, now=now, groups=config.groups, stdout=stdout,
                stderr=stderr)
    code = cmd_book(Ctx(out, config, api, catalog, args))
    out.finish()
    return code, stdout.getvalue(), stderr.getvalue()


# -- the rank table ----------------------------------------------------------

def test_no_current_booking_and_a_best_desk_creates(tmp_path):
    catalog = FakeCatalog(["L5.D.A", "L5.D.B"], {MON: {"L5.D.A", "L5.D.B"}})
    api = FakeApi(bookings=[])
    config = Config(groups={"preferred": ["L5.D.A", "L5.D.B"]})
    code, stdout, _ = run(catalog, api, config, Args(args=["mon"]))
    assert code == ExitCode.OK
    assert api.created == [(api.created[0][0], "L5.D.A", "cid-L5.D.A")]
    assert "✓" in stdout


def test_a_tagged_desk_shows_its_tags_in_the_plan(tmp_path, monkeypatch):
    """Same interactive-table shape as `test_blocked_desk_target_is_shown_
    with_fmt_desk` -- a single auto-confirmed row collapses to just the
    date, so tags (like the desk name itself) only show once the table has
    more than one row to justify printing."""
    class ScriptedStdin:
        def isatty(self):
            return True

        def readline(self):
            return "\n"

    monkeypatch.setattr("sys.stdin", ScriptedStdin())
    tue = MON + dt.timedelta(days=1)
    catalog = FakeCatalog(["L5.D.A"], {MON: {"L5.D.A"}, tue: {"L5.D.A"}},
                          tags={"L5.D.A": ("quiet",)})
    api = FakeApi(bookings=[])
    config = Config(groups={"preferred": ["L5.D.A"]})
    _, stdout, _ = run(catalog, api, config,
                       Args(args=["mon", "tue"], yes=False))
    assert "[quiet]" in stdout


def test_no_current_booking_and_nothing_bookable_is_blocked(tmp_path):
    """Nothing selectable in the whole plan -> exit 6/NO_DESK, the code
    PLAN.md and `plan.py` reserve for "nothing available"."""
    catalog = FakeCatalog(["L5.D.A"], {})
    api = FakeApi(bookings=[])
    config = Config(groups={"preferred": ["L5.D.A"]})
    with pytest.raises(NoDeskAvailable):
        run(catalog, api, config, Args(args=["mon"]))
    assert not api.created


def test_a_partial_block_alongside_a_create_is_still_ok(tmp_path):
    """One selectable row anywhere in the plan means it's a partial
    success, not a failure -- NO_DESK is reserved for a plan with nothing
    selectable in it at all."""
    catalog = FakeCatalog(["L5.D.A"], {MON: {"L5.D.A"}})
    api = FakeApi(bookings=[])
    config = Config(groups={"preferred": ["L5.D.A"]}, office_days=[])
    code, stdout, _ = run(catalog, api, config, Args(args=["mon", "tue"]))
    assert code == ExitCode.OK
    assert len(api.created) == 1


def test_already_on_the_best_desk_is_a_noop(tmp_path):
    catalog = FakeCatalog(["L5.D.A"], {MON: {"L5.D.A"}})
    api = FakeApi(bookings=[{"bkid": "1", "key": "L5.D.A",
                             "start": _at(MON)}])
    config = Config(groups={"preferred": ["L5.D.A"]})
    code, stdout, _ = run(catalog, api, config, Args(args=["mon"]))
    assert code == ExitCode.OK
    assert not api.created and not api.updated


def test_a_better_ranked_current_desk_is_kept(tmp_path):
    """Booked at 1st preferred; 2nd preferred is the best still bookable ->
    never a downgrade."""
    catalog = FakeCatalog(["L5.D.A", "L5.D.B"], {MON: {"L5.D.B"}})
    api = FakeApi(bookings=[{"bkid": "1", "key": "L5.D.A", "start": _at(MON)}])
    config = Config(groups={"preferred": ["L5.D.A", "L5.D.B"]})
    code, stdout, _ = run(catalog, api, config, Args(args=["mon"]))
    assert code == ExitCode.OK
    assert not api.updated


def test_the_interactive_table_shows_the_noop_and_blocked_reasons(tmp_path,
                                                                   monkeypatch):
    """The one test that actually reads the confirm table -- `--yes` skips
    rendering it entirely, so every other test above only checks side
    effects."""
    class ScriptedStdin:
        def isatty(self):
            return True

        def readline(self):
            return "\n"

    monkeypatch.setattr("sys.stdin", ScriptedStdin())
    catalog = FakeCatalog(["L5.D.A", "L5.D.B"], {MON: {"L5.D.A"}})
    api = FakeApi(bookings=[{"bkid": "1", "key": "L5.D.A", "start": _at(MON)},
                            ])
    config = Config(groups={"preferred": ["L5.D.A", "L5.D.B"]})
    code, stdout, _ = run(catalog, api, config,
                          Args(args=["mon"], yes=False))
    assert code == ExitCode.OK
    assert "already booked" in stdout


def test_a_worse_ranked_current_desk_is_replaced(tmp_path):
    catalog = FakeCatalog(["L5.D.A", "L5.D.B"],
                          {MON: {"L5.D.A", "L5.D.B"}})
    api = FakeApi(bookings=[{"bkid": "1", "key": "L5.D.B", "start": _at(MON)}])
    config = Config(groups={"preferred": ["L5.D.A", "L5.D.B"]})
    code, stdout, _ = run(catalog, api, config, Args(args=["mon"]))
    assert code == ExitCode.OK
    assert api.updated == [("1", "L5.D.A", "cid-L5.D.A")]


def test_a_booking_outside_the_group_is_replaced(tmp_path):
    """PLAN.md's easy-to-miss row: not in the group at all still loses to
    anything in it."""
    catalog = FakeCatalog(["L5.D.A", "L5.D.OUT"], {MON: {"L5.D.A"}})
    api = FakeApi(bookings=[{"bkid": "1", "key": "L5.D.OUT",
                             "start": _at(MON)}])
    config = Config(groups={"preferred": ["L5.D.A"]})
    code, stdout, _ = run(catalog, api, config, Args(args=["mon"]))
    assert code == ExitCode.OK
    assert api.updated == [("1", "L5.D.A", "cid-L5.D.A")]


def test_release_then_book_is_never_used(tmp_path):
    """The manual's warning, checked directly: a replacement is always
    `booking-update`, never a release call."""
    catalog = FakeCatalog(["L5.D.A", "L5.D.B"], {MON: {"L5.D.A"}})
    api = FakeApi(bookings=[{"bkid": "1", "key": "L5.D.B", "start": _at(MON)}])
    config = Config(groups={"preferred": ["L5.D.A", "L5.D.B"]})
    run(catalog, api, config, Args(args=["mon"]))
    assert api.updated


def test_current_desk_kept_when_nothing_else_is_bookable(tmp_path):
    catalog = FakeCatalog(["L5.D.A", "L5.D.B"], {})
    api = FakeApi(bookings=[{"bkid": "1", "key": "L5.D.B", "start": _at(MON)}])
    config = Config(groups={"preferred": ["L5.D.A", "L5.D.B"]})
    code, stdout, _ = run(catalog, api, config, Args(args=["mon"]))
    assert code == ExitCode.OK
    assert not api.updated


# -- target resolution --------------------------------------------------------

def test_a_bare_desk_token_is_a_group_of_one(tmp_path):
    catalog = FakeCatalog(["L5.D.A"], {MON: {"L5.D.A"}})
    api = FakeApi(bookings=[])
    config = Config()
    code, stdout, _ = run(catalog, api, config, Args(args=["L5.D.A", "mon"]))
    assert code == ExitCode.OK
    assert api.created


def test_no_desk_or_group_defaults_to_preferred(tmp_path):
    catalog = FakeCatalog(["L5.D.A"], {MON: {"L5.D.A"}})
    api = FakeApi(bookings=[])
    config = Config(groups={"preferred": ["L5.D.A"]})
    code, _, _ = run(catalog, api, config, Args(args=["mon"]))
    assert code == ExitCode.OK
    assert api.created


def test_no_default_group_configured_is_a_usage_error(tmp_path):
    catalog = FakeCatalog(["L5.D.A"], {})
    api = FakeApi(bookings=[])
    config = Config()
    with pytest.raises(UsageError):
        run(catalog, api, config, Args(args=["mon"]))


def test_default_group_name_is_configurable(tmp_path):
    """The default group is whatever `config.toml`'s `default_group` says,
    not a hardcoded name -- a renamed default must be reached the same way
    'preferred' is above."""
    catalog = FakeCatalog(["L5.D.A"], {MON: {"L5.D.A"}})
    api = FakeApi(bookings=[])
    config = Config(default_group="quiet-corner",
                    groups={"quiet-corner": ["L5.D.A"]})
    code, _, _ = run(catalog, api, config, Args(args=["mon"]))
    assert code == ExitCode.OK
    assert api.created


def test_two_targets_is_a_usage_error(tmp_path):
    catalog = FakeCatalog(["L5.D.A"], {})
    api = FakeApi(bookings=[])
    config = Config(groups={"quiet": ["L5.D.A"]})
    with pytest.raises(UsageError):
        run(catalog, api, config, Args(args=["L5.D.A", "quiet"]))


def test_two_groups_is_a_usage_error(tmp_path):
    catalog = FakeCatalog([], {})
    api = FakeApi(bookings=[])
    config = Config(groups={"quiet": [], "preferred": []})
    with pytest.raises(UsageError):
        run(catalog, api, config, Args(args=["quiet", "preferred"]))


def test_multiple_desks_form_an_ad_hoc_preference_list(tmp_path):
    """`fs book <desk> <desk> ...` with no group -- a bare desk is already
    documented as a preference list of one; more than one desk must behave
    exactly like a named group whose members are that list, in the order
    typed."""
    catalog = FakeCatalog(["L5.D.A", "L5.D.B"], {MON: {"L5.D.A", "L5.D.B"}})
    api = FakeApi(bookings=[])
    config = Config()
    code, _, _ = run(catalog, api, config,
                     Args(args=["L5.D.B", "L5.D.A", "mon"]))
    assert code == ExitCode.OK
    # L5.D.B was typed first, so it's rank 1 even though the catalog lists
    # A before B -- the preference order is command-line order, not catalog
    # order.
    assert len(api.created) == 1
    _, key, cid = api.created[0]
    assert (key, cid) == ("L5.D.B", "cid-L5.D.B")


def test_a_worse_ranked_current_desk_is_replaced_within_a_desk_list(tmp_path):
    catalog = FakeCatalog(["L5.D.A", "L5.D.B"], {MON: {"L5.D.A", "L5.D.B"}})
    api = FakeApi(bookings=[{"key": "L5.D.B", "bkid": "old",
                             "start": int(dt.datetime.combine(
                                 MON, dt.time.min).timestamp()),
                             "finish": int(dt.datetime.combine(
                                 MON, dt.time.max).timestamp())}])
    config = Config()
    code, _, _ = run(catalog, api, config,
                     Args(args=["L5.D.A", "L5.D.B", "mon"]))
    assert code == ExitCode.OK
    assert api.updated == [("old", "L5.D.A", "cid-L5.D.A")]


def test_all_flag_is_a_usage_error():
    """`--all` is a global flag now that `fs release` uses it -- a typo'd
    `fs book --all` must not silently do nothing."""
    catalog = FakeCatalog(["L5.D.A"], {})
    api = FakeApi(bookings=[])
    config = Config(groups={"preferred": ["L5.D.A"]})
    with pytest.raises(UsageError):
        run(catalog, api, config, Args(args=["mon"], all=True))


class ExplodingCatalog:
    """Pins that a usage error still costs no catalog access -- `tag_map()`
    must never run before `cmd_book`'s own `--name`/`--all` UsageError
    check (PLAN.md's code-review finding: it used to)."""
    def tag_map(self):
        raise AssertionError("catalog touched before validation")


def test_an_all_flag_usage_error_never_touches_the_catalog():
    api = FakeApi(bookings=[])
    config = Config(groups={"preferred": ["L5.D.A"]})
    with pytest.raises(UsageError):
        run(ExplodingCatalog(), api, config, Args(args=["mon"], all=True))


def test_a_name_flag_usage_error_never_touches_the_catalog():
    api = FakeApi(bookings=[])
    config = Config(groups={"preferred": ["L5.D.A"]})
    with pytest.raises(UsageError):
        run(ExplodingCatalog(), api, config,
           Args(args=["mon"], name=["jane"]))


def test_blocked_desk_target_is_shown_with_fmt_desk(tmp_path, monkeypatch):
    """PLAN.md's output rule: a desk is always `Output.fmt_desk`, never the
    raw catalog key. `tue` is bookable and `mon` isn't, so the plan has one
    selectable row alongside the BLOCKED one and reaches the table instead
    of raising NoDeskAvailable."""
    class ScriptedStdin:
        def isatty(self):
            return True

        def readline(self):
            return "\n"

    monkeypatch.setattr("sys.stdin", ScriptedStdin())
    tue = MON + dt.timedelta(days=1)
    catalog = FakeCatalog(["L5.D.A"], {tue: {"L5.D.A"}})
    api = FakeApi(bookings=[])
    config = Config()
    code, stdout, _ = run(catalog, api, config,
                          Args(args=["L5.D.A", "mon", "tue"], yes=False))
    assert "L5.D.A not available" not in stdout
    assert "5.A not available" in stdout


def test_no_dates_uses_office_days(tmp_path):
    catalog = FakeCatalog(["L5.D.A"], {MON: {"L5.D.A"}})
    api = FakeApi(bookings=[])
    config = Config(groups={"preferred": ["L5.D.A"]}, office_days=["monday"])
    code, _, _ = run(catalog, api, config, Args())
    assert code == ExitCode.OK
    assert api.created


def test_no_dates_and_no_office_days_is_a_usage_error(tmp_path):
    catalog = FakeCatalog(["L5.D.A"], {})
    api = FakeApi(bookings=[])
    config = Config(groups={"preferred": ["L5.D.A"]})
    with pytest.raises(UsageError):
        run(catalog, api, config, Args())



def _at(day, hour=8):
    return int(dt.datetime.combine(day, dt.time(hour)).astimezone().timestamp())


# -- intent line ---------------------------------------------------------

def test_office_days_intent_line_names_the_target_not_every_date(tmp_path):
    """Used to spell out every resolved date ("Booking preferred, Tomorrow,
    Thursday 27th Aug, ...") -- for a multi-week run that buries the one
    thing worth reading (which desks) under a date dump nobody asked for.
    Only fires when no date was TYPED; an explicit date list still gets
    `fmt_dates` (2026-08-25 fix)."""
    catalog = FakeCatalog(["L5.D.A"], {MON: {"L5.D.A"}})
    api = FakeApi(bookings=[])
    config = Config(groups={"preferred": ["L5.D.A"]}, office_days=["monday"])
    _, _, stderr = run(catalog, api, config, Args())
    assert stderr.startswith("Booking preferred desks on office-days")


def test_explicit_dates_still_get_the_date_list(tmp_path):
    catalog = FakeCatalog(["L5.D.A"], {MON: {"L5.D.A"}})
    api = FakeApi(bookings=[])
    config = Config(groups={"preferred": ["L5.D.A"]})
    _, _, stderr = run(catalog, api, config, Args(args=["mon"]))
    assert stderr.startswith("Booking preferred, Monday 24th Aug")


def test_a_bare_desk_target_has_no_desks_word_in_the_intent_line(tmp_path):
    catalog = FakeCatalog(["L5.D.A"], {MON: {"L5.D.A"}})
    api = FakeApi(bookings=[])
    config = Config(office_days=["monday"])
    _, _, stderr = run(catalog, api, config, Args(args=["L5.D.A"]))
    assert stderr.startswith("Booking 5.A on office-days")


# -- `new` keyword ---------------------------------------------------------

def test_new_skips_a_date_that_already_has_a_booking(tmp_path):
    """The existing booking is on a DIFFERENT desk (`L5.D.B`, not even in
    the target group) so that, with the `new` filter deleted, Monday would
    still produce a selectable REPLACE row (current outside the target,
    `L5.D.A` available) rather than an unselectable NOOP -- an unfiltered
    NOOP never reaches stdout or `api.updated` either way (`confirm`
    selects it, `execute` still only prints/writes selected rows, but a
    NOOP's `run` is `None`), so it wouldn't tell a working filter from a
    deleted one. A REPLACE would write; `assert not api.updated` is what
    actually catches the filter being missing."""
    tue = MON + dt.timedelta(days=1)
    catalog = FakeCatalog(["L5.D.A"], {MON: {"L5.D.A"}, tue: {"L5.D.A"}})
    api = FakeApi(bookings=[{"bkid": "1", "key": "L5.D.B",
                             "start": _at(MON), "finish": _at(MON)}])
    config = Config(groups={"preferred": ["L5.D.A"]},
                    office_days=["monday", "tuesday"], book_ahead_days=5)
    code, stdout, _ = run(catalog, api, config, Args(args=["new"]))
    assert code == ExitCode.OK
    assert len(api.created) == 1
    assert api.created[0][1] == "L5.D.A"
    assert not api.updated
    assert "Monday" not in stdout


def test_new_ignores_a_booking_in_a_different_group():
    """"Already have a booking" means ANY own booking that day, not just one
    in the target group -- the same reading `already_booked`'s NOOP rows
    already give elsewhere in this file. A desk held in a different group
    still counts as "not new" for that date -- without the filter this
    would be a selectable REPLACE (current outside the target, `L5.D.A`
    available), so `api.updated` is what actually pins the filter running,
    not just the intent line."""
    catalog = FakeCatalog(["L5.D.A"], {MON: {"L5.D.A"}})
    api = FakeApi(bookings=[{"bkid": "1", "key": "L5.D.B",
                             "start": _at(MON), "finish": _at(MON)}])
    config = Config(groups={"preferred": ["L5.D.A"]})
    code, stdout, stderr = run(catalog, api, config,
                               Args(args=["mon", "new"]))
    assert code == ExitCode.OK
    assert not api.created
    assert not api.updated
    assert "nothing new to book" in stderr


def test_new_with_an_explicit_date_already_booked_says_so():
    catalog = FakeCatalog(["L5.D.A"], {MON: {"L5.D.A"}})
    api = FakeApi(bookings=[{"bkid": "1", "key": "L5.D.A",
                             "start": _at(MON), "finish": _at(MON)}])
    config = Config(groups={"preferred": ["L5.D.A"]})
    _, _, stderr = run(catalog, api, config, Args(args=["mon", "new"]))
    assert stderr.startswith("Booking preferred -- nothing new to book")


def test_bare_new_with_every_office_day_already_booked_says_so(tmp_path):
    """`fs book new` with no dates typed sources `dates` from office days,
    not from `bound[TokenType.DATE]` -- the `used_office_days` intent
    branch would otherwise fire first, print "on office-days", and then go
    silent once `new` filters every one of them out (the actions list ends
    up empty, and neither `confirm()` nor `execute()` says anything about
    an empty plan). This is the exact invocation `new` is for
    (`fs book new` after office days), so it must not go quiet."""
    catalog = FakeCatalog(["L5.D.A"], {MON: {"L5.D.A"}})
    api = FakeApi(bookings=[{"bkid": "1", "key": "L5.D.A",
                             "start": _at(MON), "finish": _at(MON)}])
    # `book_ahead_days=5` keeps Aug 24 the only Monday in the window --
    # the default 10 reaches Aug 31 too, a second office day this test
    # isn't about and that would go BLOCKED (nothing bookable configured
    # for it) rather than exercising the empty-`dates` path under test.
    config = Config(groups={"preferred": ["L5.D.A"]}, office_days=["monday"],
                    book_ahead_days=5)
    code, stdout, stderr = run(catalog, api, config, Args(args=["new"]))
    assert code == ExitCode.OK
    assert not api.created
    assert stderr.startswith("Booking preferred -- nothing new to book")


def test_new_still_books_a_date_with_no_existing_booking(tmp_path):
    catalog = FakeCatalog(["L5.D.A"], {MON: {"L5.D.A"}})
    api = FakeApi(bookings=[])
    config = Config(groups={"preferred": ["L5.D.A"]})
    code, _, _ = run(catalog, api, config, Args(args=["mon", "new"]))
    assert code == ExitCode.OK
    assert api.created


# -- past / opening-time / advance-window blocking ---------------------

def test_a_past_date_is_blocked_without_touching_the_catalog(tmp_path):
    catalog = FakeCatalog(["L5.D.A"], {})
    api = FakeApi()
    config = Config(groups={"preferred": ["L5.D.A"]})
    yesterday = (TODAY - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    with pytest.raises(NoDeskAvailable):
        run(catalog, api, config, Args(args=[yesterday]), today=TODAY)
    assert catalog.bookable_calls == []
    assert not api.created


def test_today_after_the_opening_time_is_blocked(tmp_path):
    catalog = FakeCatalog(["L5.D.A"], {TODAY: {"L5.D.A"}})
    api = FakeApi()
    config = Config(groups={"preferred": ["L5.D.A"]})
    now = dt.datetime.combine(TODAY, dt.time(9, 0))
    with pytest.raises(NoDeskAvailable):
        run(catalog, api, config, Args(args=["today"]), today=TODAY, now=now)
    assert catalog.bookable_calls == []
    assert not api.created


def test_blocked_reasons_show_in_the_plan_table_alongside_a_create(
        tmp_path, monkeypatch):
    class ScriptedStdin:
        def isatty(self):
            return True

        def readline(self):
            return "\n"

    monkeypatch.setattr("sys.stdin", ScriptedStdin())
    catalog = FakeCatalog(["L5.D.A"], {MON: {"L5.D.A"}})
    api = FakeApi()
    config = Config(groups={"preferred": ["L5.D.A"]}, book_ahead_days=10)
    yesterday = (TODAY - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    now = dt.datetime.combine(TODAY, dt.time(9, 0))
    code, stdout, _ = run(
        catalog, api, config,
        Args(args=[yesterday, "today", "mon"], yes=False),
        today=TODAY, now=now)
    assert code == ExitCode.OK
    assert "can't book past date" in stdout
    assert "cannot book for today after the day opening time" in stdout
    assert MON in catalog.bookable_calls
    assert api.created


def test_today_before_the_opening_time_still_books(tmp_path):
    catalog = FakeCatalog(["L5.D.A"], {TODAY: {"L5.D.A"}})
    api = FakeApi()
    config = Config(groups={"preferred": ["L5.D.A"]})
    now = dt.datetime.combine(TODAY, dt.time(8, 0))
    code, _, _ = run(catalog, api, config, Args(args=["today"]),
                    today=TODAY, now=now)
    assert code == ExitCode.OK
    assert api.created


def test_a_date_beyond_the_advance_window_is_blocked(tmp_path):
    too_far = TODAY + dt.timedelta(days=30)
    catalog = FakeCatalog(["L5.D.A"], {too_far: {"L5.D.A"}})
    api = FakeApi()
    config = Config(groups={"preferred": ["L5.D.A"]}, book_ahead_days=10)
    with pytest.raises(NoDeskAvailable):
        run(catalog, api, config, Args(args=[too_far.strftime("%Y-%m-%d")]),
           today=TODAY)
    assert catalog.bookable_calls == []
    assert not api.created
