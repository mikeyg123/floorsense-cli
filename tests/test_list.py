"""`fs list`, the locker escalation, and the today-only check-in rule."""

import datetime as dt
import io
import json

import pytest

from fs_cli.commands.list_cmd import locker_warning, cmd_list
from fs_cli.catalog import Catalog, CacheStore
from fs_cli.errors import ExitCode
from fs_cli.fixtures import FixtureApi
from fs_cli.render import Output


class Config:
    def __init__(self, teams=None, groups=None):
        self.teams = teams or {}
        self.groups = groups or {}


class Args:
    def __init__(self, args=(), date=(), desk=(), group=(), name=()):
        self.args = list(args)
        self.date = list(date)
        self.desk = list(desk)
        self.group = list(group)
        self.name = list(name)


class Ctx:
    def __init__(self, out, api, catalog, config=None, args=None):
        self.out, self.api, self.catalog = out, api, catalog
        self.config = config or Config()
        self.args = args or Args()

    def load_tags(self):
        self.out.tags = self.catalog.tag_map()


def run(today, bookings=None, lockers=None, tmp_path=None, json_mode=False,
        groups=None, summary=None, args=None, config=None):
    """Drive the command with a stubbed backend so a fixed `today` can be
    injected -- check-in rendering depends entirely on what day it is.

    Both booking sources are stubbed together: `fs list` merges
    `booking-list` with `booking-summary`, so leaving one live would let the
    captured fixture leak into a test that thinks it controls the input.
    """
    api = FixtureApi()
    if bookings is not None:
        api.booking_list = lambda: bookings
        api.booking_summary = lambda *a, **kw: summary or {"days": []}
    if lockers is not None:
        api.res_list = lambda: lockers

    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=today, groups=groups or {}, json_mode=json_mode,
                 stdout=stdout, stderr=stderr)
    cache = CacheStore(tmp_path / "cache.json") if tmp_path else None
    code = cmd_list(Ctx(out, api, Catalog(api, cache), config=config,
                        args=args))
    out.finish()
    return code, stdout.getvalue(), stderr.getvalue()


def at(day, hour=8):
    return int(dt.datetime.combine(day, dt.time(hour)).astimezone().timestamp())


TODAY = dt.date(2026, 8, 21)


class ExplodingCatalog:
    """Pins that a usage error still costs no catalog access -- `tag_map()`
    must never run before `resolve_targets`'s own UsageError checks
    (PLAN.md's code-review finding: it used to, in `cmd_list` too)."""
    def tag_map(self):
        raise AssertionError("catalog touched before validation")


def test_a_desk_flag_usage_error_never_touches_the_catalog():
    from fs_cli.errors import UsageError
    from fs_cli.fixtures import FixtureApi
    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=TODAY, stdout=stdout, stderr=stderr)
    ctx = Ctx(out, FixtureApi(), ExplodingCatalog(),
             args=Args(desk=["L5.D.A"]))
    with pytest.raises(UsageError):
        cmd_list(ctx)


# -- the listing -----------------------------------------------------------

def test_bookings_are_listed_chronologically(tmp_path):
    out_of_order = [
        {"bkid": "2", "key": "L5.D.236A", "start": at(TODAY + dt.timedelta(3))},
        {"bkid": "1", "key": "L5.D.216A", "start": at(TODAY + dt.timedelta(1))},
    ]
    code, stdout, _ = run(TODAY, out_of_order, [], tmp_path)
    assert code == ExitCode.OK
    assert stdout.index("5.216A") < stdout.index("5.236A")


def test_released_bookings_are_not_shown(tmp_path):
    """§6: `released == 0` is the test for "still stands" -- `active` means
    "in progress right now" and is False on every future booking."""
    rows = [{"bkid": "1", "key": "L5.D.216A", "start": at(TODAY), "released": 0,
             "active": False},
            {"bkid": "2", "key": "L5.D.236A", "start": at(TODAY),
             "released": 1787000000}]
    _, stdout, _ = run(TODAY, rows, [], tmp_path)
    assert "5.216A" in stdout and "5.236A" not in stdout


def test_desks_are_rendered_with_their_group_rank(tmp_path):
    _, stdout, _ = run(TODAY, [{"bkid": "1", "key": "L5.D.216A",
                                "start": at(TODAY)}], [], tmp_path,
                       groups={"favourite": ["L5.D.217A", "L5.D.216A"]})
    assert "5.216A (2nd favourite)" in stdout


def test_a_tagged_desk_shows_its_tags(tmp_path):
    """`L5.D.220` is a real tagged desk in the captured fixture
    (`catalog.tag_map()`, sourced from `floorplan-booking`'s `desktags`)."""
    _, stdout, _ = run(TODAY, [{"bkid": "1", "key": "L5.D.220",
                                "start": at(TODAY)}], [], tmp_path)
    assert "[Window]" in stdout


def test_no_bookings_says_so_rather_than_printing_nothing(tmp_path):
    _, stdout, _ = run(TODAY, [], [], tmp_path)
    assert "No desks booked." in stdout


# -- intent line, on stderr (never data, never --json) ---------------------

def test_own_view_states_intent_first_on_stderr(tmp_path):
    _, stdout, stderr = run(TODAY, [], [], tmp_path)
    assert stderr.startswith("Showing your upcoming bookings")
    assert "Showing" not in stdout


def test_own_view_with_dates_states_the_resolved_dates(tmp_path):
    _, _, stderr = run(TODAY, [], [], tmp_path, args=Args(["tomorrow"]))
    assert stderr.startswith("Showing your bookings for Tomorrow")


def test_json_mode_suppresses_the_intent_line(tmp_path):
    _, stdout, stderr = run(TODAY, [], [], tmp_path, json_mode=True)
    assert "Showing" not in stdout and "Showing" not in stderr


# -- check-in, today only --------------------------------------------------

def test_today_unconfirmed_shows_the_auto_release_deadline(tmp_path):
    """§8: an advance booking not checked into by `confexpiry` is released.
    A silent deadline, so it gets said out loud."""
    row = {"bkid": "1", "key": "L5.D.216A", "start": at(TODAY),
           "confirmed": False,
           "confexpiry": at(TODAY, 8) + 90 * 60}
    _, stdout, _ = run(TODAY, [row], [], tmp_path)
    assert "not checked in -- auto-releases 09:30" in stdout


def test_today_confirmed_says_checked_in(tmp_path):
    row = {"bkid": "1", "key": "L5.D.216A", "start": at(TODAY),
           "confirmed": True}
    _, stdout, _ = run(TODAY, [row], [], tmp_path)
    assert "checked in" in stdout
    assert "not checked in" not in stdout


def test_future_rows_never_mention_check_in(tmp_path):
    """`confirmed` is False on EVERY future booking by construction (§5.1),
    so rendering it there would put a warning on every row that means
    nothing."""
    row = {"bkid": "1", "key": "L5.D.216A",
           "start": at(TODAY + dt.timedelta(3)), "confirmed": False,
           "confexpiry": at(TODAY + dt.timedelta(3), 8) + 90 * 60}
    _, stdout, _ = run(TODAY, [row], [], tmp_path)
    assert "checked in" not in stdout
    assert "auto-releases" not in stdout


def test_checkin_row_style_colours_only_when_a_column_follows(tmp_path):
    """The regression the plain-text split guards against: colouring via
    `row_style` must work even when the checkin note ISN'T the last column
    -- which baking ANSI into the cell before `table()` computes widths
    could never do correctly."""
    from fs_cli.commands.list_cmd import checkin_row_style
    from fs_cli import render

    out = Output(today=TODAY, color=True)
    notes = ["checked in"]
    styles = ["good"]  # a `render.THEME` name, not a raw colour -- "good" -> green
    # An extra trailing column AFTER the note -- the exact case the finding
    # says would silently misalign if colour were baked into the cell.
    rows = [["Monday", "5.217A", "checked in", "note"]]
    text = render.table(None, rows,
                        cell_style=checkin_row_style(out, notes, styles, note_col=2))
    assert "\x1b[32mchecked in\x1b[0m" in text
    # And the trailing column, added after the note, is still intact --
    # colouring the note cell didn't eat or shift it.
    assert text.rstrip().endswith("note")


def test_checkin_row_style_matches_by_position_not_content(tmp_path):
    """PLAN.md's "Next up" item 4: the old implementation matched by
    CONTENT (`raw == notes[row_index]`), so a coincidental duplicate of the
    note text in another column would get coloured too -- or the real note
    cell would be missed if some other value in the row was checked first.
    Position is correct-by-construction; content was correct only because
    no other cell happened to collide."""
    from fs_cli.commands.list_cmd import checkin_row_style
    from fs_cli import render

    out = Output(today=TODAY, color=True)
    notes = ["checked in"]
    styles = ["good"]  # a `render.THEME` name -- "good" -> green
    # The note text appears twice: once in the note's real column (2), and
    # once, coincidentally, earlier in the row.
    rows = [["checked in", "5.217A", "checked in"]]
    text = render.table(None, rows,
                        cell_style=checkin_row_style(out, notes, styles, note_col=2))
    # Only the real note column (2) is coloured -- the coincidental match
    # in column 0 is left plain.
    assert text.count("\x1b[32m") == 1
    assert not text.startswith("\x1b[32m")


def test_checkin_note_is_plain_text_never_ansi(tmp_path):
    """Group G: `checkin_note`'s ANSI-colored text used to be baked into the
    cell before it reached `render.table`, bypassing the `row_style` hook
    `table()`'s own docstring says exists for exactly this ("wrapping text
    in ANSI can't perturb the column widths"). It only looked right because
    the note happens to be the last, unpadded column -- adding a column
    after it would silently misalign. Colour must be applied at render time,
    not baked into the value `checkin_note` returns."""
    from fs_cli.commands.list_cmd import checkin_note
    confirmed = {"start": at(TODAY), "confirmed": True}
    assert checkin_note(confirmed, TODAY) == "checked in"
    assert "\x1b" not in checkin_note(confirmed, TODAY)

    unconfirmed = {"start": at(TODAY), "confirmed": False}
    assert "\x1b" not in checkin_note(unconfirmed, TODAY)


# -- the locker ------------------------------------------------------------

def days_out(n):
    return {"key": "5-227",
            "finish": int(dt.datetime.combine(
                TODAY + dt.timedelta(n), dt.time(12)).astimezone().timestamp())}


@pytest.mark.parametrize("days,level", [
    (180, None), (31, None), (30, "notice"), (20, "notice"),
    (14, "warning"), (10, "warning"), (7, "banner"), (1, "banner"),
    (-3, "banner"),
])
def test_the_locker_warning_escalates(days, level):
    """A single flag at 30 days is easy to miss, and the failure mode is
    silent reassignment with no notification (§8)."""
    warning = locker_warning(days_out(days), TODAY)
    assert (warning[0] if warning else None) == level


def test_an_expired_locker_says_it_may_already_be_gone():
    _, text = locker_warning(days_out(-5), TODAY)
    assert "EXPIRED" in text and "reassigned" in text


def test_the_locker_line_shows_expiry_and_days(tmp_path):
    _, stdout, _ = run(TODAY, [], [days_out(177)], tmp_path)
    assert "5-227" in stdout and "(177 days)" in stdout


def test_warnings_go_to_stderr_so_a_pipe_stays_clean(tmp_path):
    """`fs list | ...` must not have the locker banner in its data."""
    _, stdout, stderr = run(TODAY, [], [days_out(3)], tmp_path)
    assert "expires in 3 days" in stderr
    assert "expires in 3 days" not in stdout


# -- --json ----------------------------------------------------------------

def test_json_carries_warnings_in_the_payload(tmp_path):
    """A consumer reading only stdout must still see them (render.py)."""
    _, stdout, stderr = run(TODAY, [], [days_out(3)], tmp_path, json_mode=True)
    doc = json.loads(stdout)
    assert any("3 days" in w for w in doc["warnings"])
    assert stderr == ""


def test_json_dates_are_iso_strings_not_unix_ints(tmp_path):
    row = {"bkid": "1", "key": "L5.D.216A", "start": at(TODAY)}
    _, stdout, _ = run(TODAY, [row], [days_out(100)], tmp_path, json_mode=True)
    doc = json.loads(stdout)
    assert doc["bookings"][0]["date"] == "2026-08-21"
    assert doc["lockers"][0]["expires"] == "2026-11-29"


def test_json_confirm_by_is_only_set_for_today(tmp_path):
    rows = [{"bkid": "1", "key": "L5.D.216A", "start": at(TODAY),
             "confexpiry": at(TODAY, 8) + 90 * 60},
            {"bkid": "2", "key": "L5.D.236A",
             "start": at(TODAY + dt.timedelta(2)),
             "confexpiry": at(TODAY + dt.timedelta(2), 8) + 90 * 60}]
    _, stdout, _ = run(TODAY, rows, [], tmp_path, json_mode=True)
    doc = json.loads(stdout)
    assert doc["bookings"][0]["confirm_by"] == "09:30"
    assert doc["bookings"][1]["confirm_by"] is None


# -- the two booking sources -----------------------------------------------

def test_today_is_picked_up_from_booking_summary(tmp_path):
    """`booking-list` is documented as returning *future* bookings and the
    capture cannot say whether that includes today (the account had no desk
    booked on capture day). `booking-summary.days[0]` IS today by
    construction, so today's row cannot go missing."""
    summary = {"days": [{"day": 21, "bookings": [
        {"bkid": "today-1", "key": "L5.D.216A", "start": at(TODAY),
         "confirmed": False, "confexpiry": at(TODAY, 8) + 90 * 60}]}]}
    _, stdout, _ = run(TODAY, [], [], tmp_path, summary=summary)
    assert "5.216A" in stdout
    assert "auto-releases 09:30" in stdout


def test_a_booking_in_both_sources_is_listed_once(tmp_path):
    """§5.1: a multi-day booking appears under every day it covers with the
    same bkid, so the merge dedupes on bkid rather than on date."""
    row = {"bkid": "dup", "key": "L5.D.216A", "start": at(TODAY)}
    summary = {"days": [{"day": 21, "bookings": [row]},
                        {"day": 22, "bookings": [row]}]}
    _, stdout, _ = run(TODAY, [row], [], tmp_path, summary=summary)
    assert stdout.count("5.216A") == 1


def test_past_bookings_from_the_summary_are_not_listed(tmp_path):
    summary = {"days": [{"day": 20, "bookings": [
        {"bkid": "old", "key": "L5.D.999", "start": at(TODAY - dt.timedelta(1))}]}]}
    _, stdout, _ = run(TODAY, [], [], tmp_path, summary=summary)
    assert "5.999" not in stdout


def test_a_failing_summary_does_not_break_the_command(tmp_path):
    """It is a supplement to a source that has already answered; letting it
    throw would make `fs list` fail for an extra call it did not need."""
    def boom(*a, **kw):
        raise RuntimeError("server having a bad day")

    api = FixtureApi()
    api.booking_list = lambda: [{"bkid": "1", "key": "L5.D.216A",
                                 "start": at(TODAY)}]
    api.booking_summary = boom
    api.res_list = lambda: []

    import io
    stdout = io.StringIO()
    out = Output(today=TODAY, stdout=stdout, stderr=io.StringIO())
    assert cmd_list(Ctx(out, api, Catalog(api, CacheStore(tmp_path / "c.json")))) \
        == ExitCode.OK  # Ctx() defaults config/args -- see class above
    assert "5.216A" in stdout.getvalue()


def test_a_multi_day_booking_covering_today_is_still_listed(tmp_path):
    """§5.1: a multi-day booking appears under every day it covers, and its
    `start` can be BEFORE that day. Filtering on `start` would drop today's
    desk in exactly the case where the user is sitting at it -- so the test
    is on `finish`."""
    spanning = {"bkid": "long", "key": "L5.D.216A",
                "start": at(TODAY - dt.timedelta(2)),
                "finish": at(TODAY + dt.timedelta(2), 16)}
    summary = {"days": [{"day": 21, "bookings": [spanning]}]}
    _, stdout, _ = run(TODAY, [], [], tmp_path, summary=summary)
    assert "5.216A" in stdout


def test_a_booking_that_finished_yesterday_is_not_listed(tmp_path):
    done = {"bkid": "old", "key": "L5.D.999",
            "start": at(TODAY - dt.timedelta(3)),
            "finish": at(TODAY - dt.timedelta(1), 16)}
    summary = {"days": [{"day": 18, "bookings": [done]}]}
    _, stdout, _ = run(TODAY, [], [], tmp_path, summary=summary)
    assert "5.999" not in stdout


# -- extended grammar: `fs list [<name|team>...] [<date>...]` --------------

def test_a_date_with_no_target_filters_own_bookings(tmp_path):
    rows = [{"bkid": "1", "key": "L5.D.216A", "start": at(TODAY)},
            {"bkid": "2", "key": "L5.D.217A", "start": at(TODAY + dt.timedelta(1))}]
    code, stdout, _ = run(TODAY, rows, [], tmp_path,
                          args=Args(["tomorrow"]))
    assert code == ExitCode.OK
    assert "5.217A" in stdout and "5.216A" not in stdout


def test_a_name_delegates_to_find_style_rendering(tmp_path):
    api = FixtureApi()
    api.booking_list = lambda: []
    api.booking_summary = lambda *a, **kw: {"days": []}
    api.res_list = lambda: []
    api.user_search = lambda name, start, finish: [
        {"uid": "1", "name": "Jane Doe",
         "future": [{"start": at(TODAY), "key": "L5.D.5", "confirmed": False}]}]

    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=TODAY, stdout=stdout, stderr=stderr)
    cache = CacheStore(tmp_path / "cache.json")
    code = cmd_list(Ctx(out, api, Catalog(api, cache),
                        args=Args(["jane"])))
    out.finish()
    assert code == ExitCode.OK
    assert "Jane Doe" in stdout.getvalue() and "5.5" in stdout.getvalue()
    # No locker line for a target that isn't you.
    assert "Locker" not in stdout.getvalue()
    assert stderr.getvalue().startswith("Showing bookings for jane, Today")


def test_a_team_delegates_and_fans_out(tmp_path):
    api = FixtureApi()
    api.booking_list = lambda: []
    api.booking_summary = lambda *a, **kw: {"days": []}
    api.res_list = lambda: []
    api.user_search = lambda name, start, finish: [
        {"uid": "1", "name": name,
         "future": [{"start": at(TODAY), "key": "L5.D.5", "confirmed": False}]}]

    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=TODAY, stdout=stdout, stderr=stderr)
    cache = CacheStore(tmp_path / "cache.json")
    config = Config(teams={"inception": [{"uid": "1", "name": "Jane Doe"},
                                         {"uid": "2", "name": "Bob Smith"}]})
    code = cmd_list(Ctx(out, api, Catalog(api, cache), config=config,
                        args=Args(["inception"])))
    out.finish()
    text = stdout.getvalue()
    assert code == ExitCode.OK
    assert "Jane Doe" in text and "Bob Smith" in text
    # The intent line names the team, not its expanded members -- the rows
    # above already show who's in it; repeating that list here would just
    # be noise (2026-08-25 fix). Pinned as a `startswith`, not a substring
    # check: a substring check would still pass if "Jane Doe, Bob Smith"
    # were tacked on after it.
    assert stderr.getvalue().startswith(
        "Showing bookings for team inception, Today")


# -- past dates --------------------------------------------------------

def test_a_past_date_is_reported_without_querying_the_server(tmp_path):
    api = FixtureApi()
    calls = []
    api.booking_list = lambda: calls.append("list") or []
    api.booking_summary = lambda *a, **kw: calls.append("summary") or {"days": []}
    api.res_list = lambda: calls.append("lockers") or []

    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=TODAY, stdout=stdout, stderr=stderr)
    cache = CacheStore(tmp_path / "cache.json")
    yesterday = TODAY - dt.timedelta(days=1)
    code = cmd_list(Ctx(out, api, Catalog(api, cache),
                        args=Args(args=[yesterday.strftime("%Y-%m-%d")])))
    out.finish()
    assert code == ExitCode.OK
    assert "No info for past date" in stdout.getvalue()
    assert calls == []


def test_a_past_date_mixed_with_today_still_shows_today(tmp_path):
    bookings = [{"bkid": "1", "key": "L5.D.216A", "start": at(TODAY)}]
    yesterday = TODAY - dt.timedelta(days=1)
    code, stdout, _ = run(TODAY, bookings, [], tmp_path,
                          args=Args(args=[yesterday.strftime("%Y-%m-%d"),
                                          "today"]))
    assert code == ExitCode.OK
    assert "No info for past date" in stdout
    assert "216A" in stdout


def test_a_past_date_targeting_a_name_skips_the_search(tmp_path):
    api = FixtureApi()
    calls = []
    api.user_search = lambda *a, **kw: calls.append("search") or []

    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=TODAY, stdout=stdout, stderr=stderr)
    cache = CacheStore(tmp_path / "cache.json")
    yesterday = TODAY - dt.timedelta(days=1)
    code = cmd_list(Ctx(out, api, Catalog(api, cache),
                        args=Args(args=["jane",
                                        yesterday.strftime("%Y-%m-%d")])))
    out.finish()
    assert code == ExitCode.OK
    assert "No info for past date" in stdout.getvalue()
    assert calls == []
