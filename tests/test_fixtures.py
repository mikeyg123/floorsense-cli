"""`FixtureApi` -- the backend the test suite runs against.

If this drifts from the captures, the suite stops being evidence. So these
tests are mostly about the matching rules rather than about the data.
"""

import json

import pytest

from fs_cli.fixtures import FixtureApi
from fs_cli.errors import CommError


def test_discriminating_params_select_the_right_capture():
    """Four `floorplan-booking` captures differ only by `planid`; picking the
    wrong one would silently hand back another floor."""
    api = FixtureApi()
    import datetime as dt
    l5 = api.floorplan_booking(3, dt.date(2026, 8, 24), 0, 0)
    l6 = api.floorplan_booking(4, dt.date(2026, 8, 24), 0, 0)
    assert l5["name"] == "Level 5" and len(l5["desks"]) == 262
    assert l6["name"] == "Level 6" and len(l6["desks"]) == 103


def test_volatile_params_do_not_have_to_match():
    """The captures were taken on one day. If `date`/`start`/`finish` had to
    match, every fixture would be useless the following morning and the
    suite would break on a calendar boundary rather than on a code
    change."""
    import datetime as dt
    api = FixtureApi()
    later = api.floorplan_booking(3, dt.date(2030, 1, 1), 999, 1000)
    assert len(later["desks"]) == 262


def test_a_missing_endpoint_says_how_to_capture_it():
    api = FixtureApi()
    with pytest.raises(CommError) as caught:
        api._get("no-such-endpoint")
    assert "07_capture_shapes" in (caught.value.hint or "")


def test_the_index_comes_from_the_captures_not_from_filenames(tmp_path):
    """Renaming a fixture must not change what it answers -- the `request`
    block in the file is the authority, so a re-run of 07 cannot desync a
    map that no longer exists."""
    (tmp_path / "wildly-misleading-name.json").write_text(json.dumps({
        "request": {"method": "GET", "path": "/app/res-list"},
        "body": {"result": True, "info": [{"key": "9-999"}]}}))
    api = FixtureApi(tmp_path)
    assert api.res_list() == [{"key": "9-999"}]


def test_files_without_a_request_block_are_ignored(tmp_path):
    (tmp_path / "notes.json").write_text(json.dumps({"hello": "world"}))
    (tmp_path / "real.json").write_text(json.dumps({
        "request": {"method": "GET", "path": "/app/res-list"},
        "body": {"result": True, "info": []}}))
    assert FixtureApi(tmp_path).res_list() == []


@pytest.mark.parametrize("call,args", [
    ("booking_create", (1787601600, "L5.D.187", 2)),
    ("booking_update", ("123", "L5.D.188", 2)),
    ("booking_release", ("123",)),
    ("friend_create", ("123",)),
    ("friend_delete", ("123",)),
])
def test_writes_are_refused_rather_than_faked(call, args):
    """A faked write invents a response, and an invented response is the one
    thing this class must never produce -- see its class docstring for why
    writes go through `live_write_api.py`'s double instead."""
    api = FixtureApi()
    with pytest.raises(CommError) as caught:
        getattr(api, call)(*args)
    assert "asked to" in str(caught.value)


def test_the_fixtures_on_disk_answer_every_read_the_cli_makes():
    """A guard against a half-captured fixture set: every read method the
    tool has must be answerable offline, or the test suite fails on a
    missing fixture rather than a real code issue."""
    import datetime as dt
    api = FixtureApi()
    assert api.booking_list()
    assert api.booking_summary()["days"]
    assert api.res_list()
    assert api.floorplan_list()
    assert api.desk_group_settings(11, 11)["book_day_start"]
    assert api.floorplan_booking(3, dt.date(2026, 8, 24), 0, 0)["desks"]
    assert api.user_search("example", 0, 1)


# -- ties ------------------------------------------------------------------

def test_the_search_window_width_selects_the_right_capture():
    """The bug this guards: `start`/`finish` are volatile as values, so the
    one-day and 14-day `user-search` captures both reduced to no
    discriminating params, and glob order silently picked the one-day one --
    whose `future` is empty. A test reading that would report that nobody
    has upcoming bookings, which is precisely the misreading §5.2 was
    written to correct."""
    api = FixtureApi()
    base = 1787227200
    wide = api.user_search("example", base, base + 14 * 86400)
    narrow = api.user_search("example", base, base + 86399)
    assert sum(len(h.get("future") or []) for h in wide) == 3
    assert sum(len(h.get("future") or []) for h in narrow) == 0


def test_two_captures_that_disagree_are_an_error_not_a_coin_toss(tmp_path):
    for name, key in (("a.json", "5-111"), ("b.json", "5-222")):
        (tmp_path / name).write_text(json.dumps({
            "request": {"method": "GET", "path": "/app/res-list"},
            "body": {"result": True, "info": [{"key": key}]}}))
    with pytest.raises(CommError) as caught:
        FixtureApi(tmp_path).res_list()
    assert "ambiguous" in str(caught.value)


def test_identical_captures_tie_harmlessly(tmp_path):
    """The 15d and 30d `booking-summary` captures are byte-identical, because
    the server capped both to 15 days (§5.1). Erroring on that would be
    pedantry."""
    body = {"result": True, "info": [{"key": "5-227"}]}
    for name in ("a.json", "b.json"):
        (tmp_path / name).write_text(json.dumps({
            "request": {"method": "GET", "path": "/app/res-list"},
            "body": body}))
    assert FixtureApi(tmp_path).res_list() == [{"key": "5-227"}]


def test_a_missing_fixture_directory_says_what_is_wrong():
    with pytest.raises(CommError) as caught:
        FixtureApi("/nonexistent/fixtures")
    assert "checkout" in (caught.value.hint or "")


def test_floorplan_booking_is_not_window_matched():
    """Window width discriminates `user-search`, where it changes the answer
    (§5.2), and deliberately not `floorplan-booking`, which is always asked
    about a single day. Otherwise a multi-day request would fail with "no
    fixture" rather than matching the captures that answer it correctly."""
    import datetime as dt
    api = FixtureApi()
    wide = api.floorplan_booking(3, dt.date(2026, 8, 24), 0, 30 * 86400)
    assert wide["name"] == "Level 5"
