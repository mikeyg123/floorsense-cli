"""`fs at <desk|group> [<date>...]` -- occupancy for ANY occupant, not just
self/following (that was a design correction -- see `at_cmd.py`'s docstring).
"""

import datetime as dt
import io

import pytest

from fs_cli.catalog import Desk, DeskState
from fs_cli.commands.at_cmd import cmd_at
from fs_cli.errors import ExitCode, UsageError
from fs_cli.render import Output

TODAY = dt.date(2026, 8, 21)
MON = dt.date(2026, 8, 24)


def state(key, free=True, uid=None, bkid=None, confirmed=False):
    return DeskState(Desk(key, cid=1, planid=3, groupid=6), free=free,
                     book_advance=True, bkid=bkid, uid=uid, confirmed=confirmed)


class FakeCatalog:
    def __init__(self, desk_keys, by_day=None, own_uid_value=None, tags=None):
        self._desk_keys = desk_keys
        self._by_day = by_day or {}
        self._own_uid = own_uid_value
        self._tags = tags or {}
        self.availability_calls = []

    def desk_keys(self):
        return list(self._desk_keys)

    def tag_map(self):
        return dict(self._tags)

    def desk_by_key(self, key):
        # Every fake desk lives on the same floor -- these tests don't
        # exercise the floor-narrowing itself (that's `catalog.py`'s own
        # `Desk`/`availability` job), only that `at_cmd.py` still resolves
        # and queries correctly once `availability()` takes `planids`.
        return Desk(key, cid=1, planid=1, groupid=1) if key in self._desk_keys else None

    def availability(self, day, planids=None):
        self.availability_calls.append(day)
        return dict(self._by_day.get(day, {}))

    def own_uid(self):
        return self._own_uid


class FakeApi:
    def __init__(self, users=None):
        self._users = users or {}
        self.user_calls = []

    def user(self, uid, bkid=None):
        self.user_calls.append(uid)
        return self._users.get(uid, {})


class Config:
    def __init__(self, groups=None, teams=None):
        self.groups = groups or {}
        self.teams = teams or {}


class Args:
    def __init__(self, args=(), date=(), desk=(), group=(), name=(),
                 all=False):
        self.args = list(args)
        self.date = list(date)
        self.desk = list(desk)
        self.group = list(group)
        self.name = list(name)
        self.all = all


class Ctx:
    def __init__(self, out, config, api, catalog, args):
        self.out, self.config, self.api, self.catalog, self.args = \
            out, config, api, catalog, args

    def load_tags(self):
        self.out.tags = self.catalog.tag_map()


def run(catalog, api, config, args, today=TODAY, json_mode=False):
    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=today, groups=config.groups, stdout=stdout,
                stderr=stderr, json_mode=json_mode)
    code = cmd_at(Ctx(out, config, api, catalog, args))
    out.finish()
    return code, stdout.getvalue(), stderr.getvalue()


def test_a_free_desk_says_free():
    catalog = FakeCatalog(["L5.D.A"], {MON: {"L5.D.A": state("L5.D.A", free=True)}})
    code, stdout, _ = run(catalog, FakeApi(), Config(), Args(["L5.D.A", "mon"]))
    assert code == ExitCode.OK
    assert "free" in stdout


def test_a_tagged_desk_shows_its_tags():
    catalog = FakeCatalog(["L5.D.A"], {MON: {"L5.D.A": state("L5.D.A", free=True)}},
                          tags={"L5.D.A": ("quiet", "window")})
    _, stdout, _ = run(catalog, FakeApi(), Config(), Args(["L5.D.A", "mon"]))
    assert "[quiet, window]" in stdout


def test_states_the_target_and_resolved_date_first_on_stderr():
    catalog = FakeCatalog(["L5.D.A"], {MON: {"L5.D.A": state("L5.D.A", free=True)}})
    _, stdout, stderr = run(catalog, FakeApi(), Config(), Args(["L5.D.A", "mon"]))
    assert stderr.startswith("Who's at 5.A, Monday 24th Aug")
    assert "Who's at" not in stdout


def test_a_group_target_says_desks_and_defaulted_date_says_today():
    catalog = FakeCatalog(["L5.D.A"], {TODAY: {"L5.D.A": state("L5.D.A", free=True)}})
    config = Config(groups={"preferred": ["L5.D.A"]})
    _, _, stderr = run(catalog, FakeApi(), config, Args(["preferred"]))
    # No date typed -> "today" in plain words, not `fmt_dates`'s "Today"
    # (2026-08-25 fix: matches `checkin_cmd.py`/`book_cmd.py`'s phrasing).
    # A group target reads as a kind of desk, not one -- "desks" says so.
    assert stderr.startswith("Who's at preferred desks today")


def test_a_bare_desk_target_has_no_desks_word():
    catalog = FakeCatalog(["L5.D.A"], {TODAY: {"L5.D.A": state("L5.D.A", free=True)}})
    _, _, stderr = run(catalog, FakeApi(), Config(), Args(["L5.D.A"]))
    assert stderr.startswith("Who's at 5.A today")


def test_json_mode_suppresses_the_intent_line():
    catalog = FakeCatalog(["L5.D.A"], {MON: {"L5.D.A": state("L5.D.A", free=True)}})
    _, _, stderr = run(catalog, FakeApi(), Config(), Args(["L5.D.A", "mon"]),
                       json_mode=True)
    assert "Who's at" not in stderr


def test_own_uid_is_shown_as_you():
    catalog = FakeCatalog(["L5.D.A"],
                          {MON: {"L5.D.A": state("L5.D.A", free=False, uid="42")}},
                          own_uid_value="42")
    _, stdout, _ = run(catalog, FakeApi(), Config(), Args(["L5.D.A", "mon"]))
    assert "you" in stdout


def test_a_stranger_occupying_the_desk_is_resolved_by_name():
    """The whole point of the redesign: `fs at` must show anyone who has the
    desk booked, not only self or a followed colleague."""
    catalog = FakeCatalog(["L5.D.A"],
                          {MON: {"L5.D.A": state("L5.D.A", free=False, uid="99")}},
                          own_uid_value="42")
    api = FakeApi(users={"99": {"name": "A Stranger"}})
    _, stdout, _ = run(catalog, api, Config(), Args(["L5.D.A", "mon"]))
    assert "A Stranger" in stdout
    assert api.user_calls == ["99"]


def test_occupant_lookup_is_not_repeated_for_the_same_uid():
    catalog = FakeCatalog(
        ["L5.D.A", "L5.D.B"],
        {MON: {"L5.D.A": state("L5.D.A", free=False, uid="99"),
               "L5.D.B": state("L5.D.B", free=False, uid="99")}})
    api = FakeApi(users={"99": {"name": "A Stranger"}})
    _, stdout, _ = run(catalog, api, Config(), Args(["L5.D.A", "L5.D.B", "mon"]))
    assert stdout.count("A Stranger") == 2
    assert api.user_calls == ["99"]


def test_a_desk_target_defaults_to_today():
    catalog = FakeCatalog(["L5.D.A"], {TODAY: {"L5.D.A": state("L5.D.A")}})
    code, stdout, _ = run(catalog, FakeApi(), Config(), Args(["L5.D.A"]))
    assert code == ExitCode.OK
    assert "Today" in stdout


def test_a_group_target_expands_to_its_desks():
    catalog = FakeCatalog(
        ["L5.D.A", "L5.D.B"],
        {MON: {"L5.D.A": state("L5.D.A", free=False, uid="1"),
               "L5.D.B": state("L5.D.B", free=True)}})
    api = FakeApi(users={"1": {"name": "Someone"}})
    config = Config(groups={"favourite": ["L5.D.A", "L5.D.B"]})
    _, stdout, _ = run(catalog, api, config, Args(["favourite", "mon"]))
    assert "Someone" in stdout and "free" in stdout


def test_no_desk_or_group_is_a_usage_error():
    catalog = FakeCatalog(["L5.D.A"])
    with pytest.raises(UsageError):
        run(catalog, FakeApi(), Config(), Args([]))


def test_a_name_flag_is_a_usage_error():
    catalog = FakeCatalog(["L5.D.A"])
    with pytest.raises(UsageError):
        run(catalog, FakeApi(), Config(), Args(["L5.D.A"], name=["jane"]))


def test_an_all_flag_is_a_usage_error():
    # Group G: `--all` used to be silently dropped, running the normal
    # desk/group path instead of rejecting it -- the way `fs status --all`
    # already does.
    catalog = FakeCatalog(["L5.D.A"])
    with pytest.raises(UsageError):
        run(catalog, FakeApi(), Config(), Args(["L5.D.A"], all=True))


class ExplodingCatalog:
    """Pins that a usage error still costs no catalog access -- `tag_map()`
    must never run before `cmd_at`'s own `--name`/`--all` UsageError checks
    (PLAN.md's code-review finding: it used to)."""
    def tag_map(self):
        raise AssertionError("catalog touched before validation")


def test_a_name_flag_usage_error_never_touches_the_catalog():
    with pytest.raises(UsageError):
        run(ExplodingCatalog(), FakeApi(), Config(),
           Args(["L5.D.A"], name=["jane"]))


def test_an_all_flag_usage_error_never_touches_the_catalog():
    with pytest.raises(UsageError):
        run(ExplodingCatalog(), FakeApi(), Config(), Args(["L5.D.A"], all=True))


def test_json_desk_and_date_are_raw_not_display_strings():
    """render.py's rule: raw values in --json, display formatting is
    print-only. Regression guard for the same class of bug `find_cmd.py`'s
    JSON tests pin."""
    import json
    catalog = FakeCatalog(["L5.D.A"], {TODAY: {"L5.D.A": state("L5.D.A", free=True)}})
    _, stdout, _ = run(catalog, FakeApi(), Config(), Args(["L5.D.A"]),
                       json_mode=True)
    doc = json.loads(stdout)
    assert doc["rows"][0]["date"] == "2026-08-21"
    assert doc["rows"][0]["desk"] == "L5.D.A"


def test_availability_is_called_once_per_date_not_per_desk():
    """`Catalog.availability(day, planids=...)` is called once per date
    regardless of `len(target_keys)` -- the cost here is `len(dates)`, not
    `len(dates) * len(target_keys)`. Pinned so a future reader doesn't have
    to re-derive it from `catalog.py`."""
    counts = {"calls": 0}

    class CountingCatalog(FakeCatalog):
        def availability(self, day, planids=None):
            counts["calls"] += 1
            return super().availability(day, planids=planids)

    catalog = CountingCatalog(
        ["L5.D.A", "L5.D.B"],
        {MON: {"L5.D.A": state("L5.D.A"), "L5.D.B": state("L5.D.B")},
         TODAY: {"L5.D.A": state("L5.D.A"), "L5.D.B": state("L5.D.B")}})
    run(catalog, FakeApi(), Config(), Args(["L5.D.A", "L5.D.B", "today", "mon"]))
    assert counts["calls"] == 2


def test_availability_is_narrowed_to_the_target_desks_floors():
    """The optimisation this test pins: `availability()` is only asked
    about the floors `target_keys` actually resolve to, not every floor in
    the building -- same trick `Catalog.bookable()` already uses. Real
    `Catalog.desk_by_key()`/`availability()` do the actual narrowing; this
    only pins that `at_cmd.py` computes and passes `planids` through."""
    planids_seen = []

    class PlanidCatalog(FakeCatalog):
        def desk_by_key(self, key):
            return Desk(key, cid=1, planid=5, groupid=1) \
                if key in self._desk_keys else None

        def availability(self, day, planids=None):
            planids_seen.append(planids)
            return super().availability(day, planids=planids)

    catalog = PlanidCatalog(["L5.D.A"], {MON: {"L5.D.A": state("L5.D.A")}})
    run(catalog, FakeApi(), Config(), Args(["L5.D.A", "mon"]))
    assert planids_seen == [[5]]


def test_checkin_only_shown_for_today():
    catalog = FakeCatalog(
        ["L5.D.A"],
        {TODAY: {"L5.D.A": state("L5.D.A", free=False, uid="1", confirmed=True)},
         MON: {"L5.D.A": state("L5.D.A", free=False, uid="1", confirmed=False)}})
    api = FakeApi(users={"1": {"name": "Someone"}})
    _, stdout, _ = run(catalog, api, Config(), Args(["L5.D.A", "today", "mon"]))
    lines = [line for line in stdout.splitlines() if "Someone" in line]
    assert "checked in" in lines[0]
    assert "checked in" not in lines[1]


def test_a_past_date_is_reported_without_querying_availability():
    catalog = FakeCatalog(["L5.D.A"], {})
    yesterday = TODAY - dt.timedelta(days=1)
    code, stdout, _ = run(catalog, FakeApi(), Config(),
                          Args(["L5.D.A", yesterday.strftime("%Y-%m-%d")]))
    assert code == ExitCode.OK
    assert "No info for past date" in stdout
    assert catalog.availability_calls == []


def test_a_past_date_mixed_with_a_real_one_still_answers_the_real_one():
    catalog = FakeCatalog(["L5.D.A"], {MON: {"L5.D.A": state("L5.D.A")}})
    yesterday = TODAY - dt.timedelta(days=1)
    code, stdout, _ = run(
        catalog, FakeApi(), Config(),
        Args(["L5.D.A", yesterday.strftime("%Y-%m-%d"), "mon"]))
    assert code == ExitCode.OK
    assert "No info for past date" in stdout
    assert "free" in stdout
    assert catalog.availability_calls == [MON]
