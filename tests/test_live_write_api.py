"""`LiveWriteApi` -- the write-capable test double `fs map`'s live
book/release tests need, since `FixtureApi` refuses every write by
design (see `fixtures.py`'s module docstring). This file tests the
double itself in isolation, before Task 10 builds `_run_live` write
tests on top of it.
"""

import datetime as dt

from fs_cli.catalog import CacheStore, Catalog

from live_write_api import LiveWriteApi

DAY = dt.date(2026, 9, 1)
PLANID_LEVEL5 = 3
FREE_DESK = "L5.D.226"   # confirmed free+book_advance on DAY against the
                         # real fixture before writing this test, per
                         # this repo's verification-discipline rule.


def test_a_created_booking_shows_up_on_the_next_floorplan_booking_read(tmp_path):
    api = LiveWriteApi()
    catalog = Catalog(api, CacheStore(tmp_path / "cache.json"))
    before = catalog.availability(DAY, planids=[PLANID_LEVEL5])
    assert before[FREE_DESK].free is True

    api.booking_create(1787270400, FREE_DESK, cid=1, day=DAY)

    after = catalog.availability(DAY, planids=[PLANID_LEVEL5])
    assert after[FREE_DESK].free is False


def test_booking_list_includes_a_created_booking(tmp_path):
    api = LiveWriteApi()
    api.booking_create(1787270400, FREE_DESK, cid=1, day=DAY)
    keys = {row.get("key") for row in api.booking_list()}
    assert FREE_DESK in keys


def test_a_released_booking_reverts_the_desk_to_free(tmp_path):
    api = LiveWriteApi()
    catalog = Catalog(api, CacheStore(tmp_path / "cache.json"))
    result = api.booking_create(1787270400, FREE_DESK, cid=1, day=DAY)
    bkid = result["info"]["bkid"]

    api.booking_release(bkid)

    after = catalog.availability(DAY, planids=[PLANID_LEVEL5])
    assert after[FREE_DESK].free is True
    assert bkid not in {str(row.get("bkid")) for row in api.booking_list()}


def test_reads_pass_through_to_a_real_fixtureapi(tmp_path):
    api = LiveWriteApi()
    catalog = Catalog(api, CacheStore(tmp_path / "cache.json"))
    # Unaffected by any overlay -- exactly what FixtureApi alone returns.
    assert len(catalog.deskpolys(PLANID_LEVEL5)) > 0
