"""`find_cmd.py`'s name/team/`following`-target logic -- name search, team
fan-out, `following`, and the today-only check-in note shared with `fs
list`'s own view.

`fs find` is a bare alias of `fs list` now (2026-08-23, `cli.py`'s
`HANDLERS["find"] = cmd_list`, same as `ls`) -- there is no separate
`cmd_find` to test any more, so every test here drives `cmd_list` with a
name/team/`following` token, the one branch of that command where this
module's `resolve_targets`/`rows_for`/`render_rows`/`json_row` actually
run. `fs list`'s own no-target view (locker + own bookings, its own
separate code path) is `test_list.py`'s job, not this file's.
"""

import datetime as dt
import io

import pytest

from fs_cli.commands.list_cmd import cmd_list
from fs_cli.errors import ExitCode, UsageError
from fs_cli.render import Output

TODAY = dt.date(2026, 8, 21)
MON = dt.date(2026, 8, 24)


def at(day, hour=8):
    return int(dt.datetime.combine(day, dt.time(hour)).astimezone().timestamp())


class FakeApi:
    def __init__(self, bookings=None, summary=None, search=None):
        self._bookings = bookings or []
        self._summary = summary or {"users": [], "days": []}
        self._search = search or {}
        self.search_calls = []
        self.summary_days_calls = []

    def booking_list(self):
        return list(self._bookings)

    def booking_summary(self, *a, **kw):
        self.summary_days_calls.append(kw.get("days"))
        return self._summary

    def user_search(self, name, start, finish):
        self.search_calls.append(name)
        return self._search.get(name, [])


class Config:
    def __init__(self, teams=None, groups=None):
        self.teams = teams or {}
        self.groups = groups or {}


class Args:
    def __init__(self, args=(), date=(), desk=(), group=(), name=(),
                 all=False):
        self.args = list(args)
        self.date = list(date)
        self.desk = list(desk)
        self.group = list(group)
        self.name = list(name)
        self.all = all


class FakeCatalog:
    def __init__(self, tags=None):
        self._tags = tags or {}

    def tag_map(self):
        return dict(self._tags)

    def lockers(self):
        # An empty team (`names == []`) falls through `cmd_list`'s own-view
        # branch, same as no target at all -- that branch always asks for
        # the locker row.
        return []


class Ctx:
    def __init__(self, out, config, api, args, catalog=None):
        self.out, self.config, self.api, self.args = out, config, api, args
        self.catalog = catalog or FakeCatalog()

    def load_tags(self):
        self.out.tags = self.catalog.tag_map()


def run(api, config, args, today=TODAY, json_mode=False, catalog=None):
    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=today, stdout=stdout, stderr=stderr, json_mode=json_mode)
    code = cmd_list(Ctx(out, config, api, args, catalog=catalog))
    out.finish()
    return code, stdout.getvalue(), stderr.getvalue()


# -- name search --------------------------------------------------------------

def test_a_name_searches_and_shows_the_booking():
    api = FakeApi(search={"jane": [
        {"uid": "1", "name": "Jane Doe",
         "future": [{"start": at(MON), "key": "L5.D.5", "confirmed": False}]}]})
    _, stdout, _ = run(api, Config(), Args(["jane", "mon"]))
    assert "Jane Doe" in stdout and "5.5" in stdout
    assert api.search_calls == ["jane"]


def test_every_fuzzy_hit_is_shown_not_picked():
    api = FakeApi(search={"jane": [
        {"uid": "1", "name": "Jane Doe", "future": []},
        {"uid": "2", "name": "Jane Smith", "future": []}]})
    _, stdout, _ = run(api, Config(), Args(["jane"]))
    assert "Jane Doe" in stdout and "Jane Smith" in stdout


def test_two_accounts_sharing_a_name_are_told_apart_by_desc():
    """Confirmed live: two different accounts, same display name, one
    query -- with nothing to tell them apart the two rows read as one
    account duplicated. `desc` disambiguates, same field
    `team_cmd.resolve_person`'s `pick_one` label already uses."""
    api = FakeApi(search={"alex": [
        {"uid": "1", "name": "Alex Chen", "desc": "8742", "future": []},
        {"uid": "2", "name": "Alex Chen", "desc": "9016", "future": []}]})
    _, stdout, _ = run(api, Config(), Args(["alex"]))
    assert "Alex Chen (8742)" in stdout
    assert "Alex Chen (9016)" in stdout


def test_two_different_people_with_different_names_are_not_annotated():
    api = FakeApi(search={"jane": [
        {"uid": "1", "name": "Jane Doe", "desc": "1111", "future": []},
        {"uid": "2", "name": "Jane Smith", "desc": "2222", "future": []}]})
    _, stdout, _ = run(api, Config(), Args(["jane"]))
    assert "Jane Doe" in stdout and "1111" not in stdout
    assert "Jane Smith" in stdout and "2222" not in stdout


def test_no_match_says_so():
    _, stdout, _ = run(FakeApi(search={}), Config(), Args(["nobody"]))
    assert "no match" in stdout


def test_no_booking_on_the_requested_date_says_so():
    api = FakeApi(search={"jane": [{"uid": "1", "name": "Jane Doe", "future": []}]})
    _, stdout, _ = run(api, Config(), Args(["jane", "mon"]))
    assert "no booking" in stdout


def test_a_currently_seated_hit_is_shown_for_today():
    """§5.2: a hit carries location fields directly, no `future[]` entry,
    only when the person is currently seated. `checkin_note` must still
    render off that hit, not just off `future[]` records."""
    api = FakeApi(search={"jane": [
        {"uid": "1", "name": "Jane Doe", "key": "L5.D.9",
         "start": at(TODAY), "confirmed": True, "future": []}]})
    _, stdout, _ = run(api, Config(), Args(["jane"]))
    assert "5.9" in stdout and "checked in" in stdout


def test_states_intent_on_stderr_not_stdout():
    api = FakeApi(search={"jane": [{"uid": "1", "name": "Jane Doe", "future": []}]})
    _, stdout, stderr = run(api, Config(), Args(["jane"]))
    assert stderr.startswith("Showing bookings for jane, Today")
    assert "Showing" not in stdout


# -- team fan-out ---------------------------------------------------------

def test_a_team_expands_to_every_member():
    api = FakeApi(search={
        "Jane Doe": [{"uid": "1", "name": "Jane Doe", "future": []}],
        "Bob Smith": [{"uid": "2", "name": "Bob Smith", "future": []}]})
    config = Config(teams={"crew": [{"name": "Jane Doe"}, "Bob Smith"]})
    _, stdout, _ = run(api, config, Args(["crew"]))
    assert "Jane Doe" in stdout and "Bob Smith" in stdout
    assert set(api.search_calls) == {"Jane Doe", "Bob Smith"}


def test_an_empty_team_warns_rather_than_erroring():
    config = Config(teams={"crew": []})
    code, _, stderr = run(FakeApi(), config, Args(["crew"]))
    assert code == ExitCode.OK
    assert "no members" in stderr


# -- following ----------------------------------------------------------------

def test_following_lists_every_followed_colleague():
    summary = {
        "users": [{"uid": "1", "name": "Alex"}],
        "days": [{"year": 2026, "month": 8, "day": 21,
                  "userbookings": {"1": [{"key": "L5.D.9", "start": at(TODAY),
                                          "confirmed": True}]}}]}
    api = FakeApi(summary=summary)
    _, stdout, _ = run(api, Config(), Args(["following"]))
    assert "Alex" in stdout and "5.9" in stdout and "checked in" in stdout


def test_following_today_only_requests_at_least_two_days():
    """§5.1 confirmed live (2026-08-25): `days=1` is a server edge case
    distinct from the documented top-end cap -- `users` comes back
    populated but `days` comes back EMPTY, so a same-day-only `fs list
    following` would report "unknown" for everyone even though the
    server actually knows. Flooring the request at 2 works around it;
    this pins that the workaround stays in place."""
    summary = {"users": [{"uid": "1", "name": "Alex"}],
              "days": [{"year": TODAY.year, "month": TODAY.month,
                       "day": TODAY.day, "userbookings": {"1": []}}]}
    api = FakeApi(summary=summary)
    _, stdout, _ = run(api, Config(), Args(["following"]))
    assert api.summary_days_calls == [2]
    assert "not booked" in stdout
    assert "unknown" not in stdout


def test_following_with_no_booking_that_day_says_so():
    summary = {"users": [{"uid": "1", "name": "Alex"}],
              "days": [{"year": 2026, "month": 8, "day": 21,
                       "userbookings": {"1": []}}]}
    _, stdout, _ = run(FakeApi(summary=summary), Config(), Args(["following"]))
    assert "not booked" in stdout


def test_following_beyond_the_15_day_cap_says_so_rather_than_not_booked():
    """§5.1: `booking-summary`'s `days` is capped at 15, silently. A date
    past the returned window must not be reported as "not booked" -- that
    claims a fact the server never actually answered."""
    far = TODAY + dt.timedelta(days=20)
    summary = {"users": [{"uid": "1", "name": "Alex"}],
              "days": [{"year": 2026, "month": 8, "day": 21,
                       "userbookings": {"1": []}}]}
    _, stdout, _ = run(FakeApi(summary=summary), Config(),
                       Args(["following"], date=[far.strftime("%d/%m/%Y")]))
    assert "unknown" in stdout
    assert "not booked" not in stdout
    assert "outside the booking-summary window" not in stdout


def test_following_combined_with_a_name_is_a_usage_error():
    with pytest.raises(UsageError):
        run(FakeApi(), Config(), Args(["following", "jane"]))


def test_nobody_followed_shows_no_matches():
    _, stdout, _ = run(FakeApi(), Config(), Args(["following"]))
    assert "No matches." in stdout


# -- rejected token types ------------------------------------------------------

def test_a_group_flag_is_a_usage_error():
    with pytest.raises(UsageError):
        run(FakeApi(), Config(), Args([], group=["favourite"]))


def test_an_all_flag_is_a_usage_error():
    # Group G: `--all` used to be silently dropped by `resolve_targets`,
    # running the normal no-args path instead of rejecting it -- the way
    # `fs status --all` already does. `test_list.py` covers `--desk`;
    # `--group`/`--all` are this file's since they're otherwise untested.
    with pytest.raises(UsageError):
        run(FakeApi(), Config(), Args([], all=True))


# -- --json: raw values, never the display strings --------------------------

def test_json_date_is_iso_not_the_display_string():
    """render.py's rule: a date goes out ISO in --json, `fmt_date`'s
    "Today"/"Monday 24th Aug" is print-only. Regression guard: an earlier
    version of this rendering leaked the display string into the payload."""
    import json
    api = FakeApi(search={"jane": [
        {"uid": "1", "name": "Jane Doe",
         "future": [{"start": at(TODAY), "key": "L5.D.216A",
                     "confirmed": False}]}]})
    _, stdout, _ = run(api, Config(), Args(["jane"]), json_mode=True)
    doc = json.loads(stdout)
    assert doc["rows"][0]["date"] == "2026-08-21"


def test_json_desk_is_the_raw_key_not_the_short_form():
    import json
    api = FakeApi(search={"jane": [
        {"uid": "1", "name": "Jane Doe",
         "future": [{"start": at(TODAY), "key": "L5.D.216A",
                     "confirmed": False}]}]})
    _, stdout, _ = run(api, Config(), Args(["jane"]), json_mode=True)
    doc = json.loads(stdout)
    assert doc["rows"][0]["desk"] == "L5.D.216A"
