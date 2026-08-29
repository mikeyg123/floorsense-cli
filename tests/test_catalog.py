"""The catalog, and the cache boundary that keeps `book` honest.

The single most important assertion in this file is
`test_a_free_desk_is_not_necessarily_a_bookable_one`: conflating those two
is what produced two false conclusions in the first write probe and would
produce a user-visible crash in `fs book`.
"""

import datetime as dt
import json

import pytest

from fs_cli.catalog import Catalog, CacheStore, DESK_TTL_S, Desk
from fs_cli.fixtures import FixtureApi

DAY = dt.date(2026, 8, 24)


@pytest.fixture
def catalog(tmp_path):
    return Catalog(FixtureApi(), CacheStore(tmp_path / "cache.json"))


class CountingApi(FixtureApi):
    """Counts calls, so "is this actually cached" is a fact rather than a
    hope."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.counts = {}

    def _get(self, path, params=None):
        self.counts[path] = self.counts.get(path, 0) + 1
        return super()._get(path, params)


# -- identity --------------------------------------------------------------

def test_the_catalog_is_built_from_floorplan_booking(catalog):
    """§5.3: `floorplan-list` returns no desk keys at all, so the catalog
    can only come from `floorplan-booking`."""
    desks = catalog.desks()
    assert len(desks) == 365                      # 262 on L5 + 103 on L6
    assert all(d.key and d.cid is not None for d in desks)


def test_empty_floors_are_skipped(catalog):
    """Two of the four floorplans carry zero desks (§5.3), and a floor with
    no desks must not become a floor we call for every availability check."""
    catalog.desks()
    assert catalog.planids() == [3, 4]


def test_desks_carry_their_group_and_floor(catalog):
    desk = catalog.desk_by_key("L5.D.187")
    assert desk.floor == "Level 5"
    assert desk.groupid is not None


class CountingList(list):
    """Counts full iterations, so "does this rescan the whole list" is a
    fact rather than a hope -- same convention `CountingApi` uses for HTTP
    calls above."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        return super().__iter__()


def test_desk_by_key_does_not_rescan_the_full_list_each_call(catalog):
    """`desk_by_key` used to be a linear scan (`for d in self.desks(): if
    d.key == key`), called once per desk row per floor per requested date
    from `availability()` -- O(n^2) work, multiplied by `book_ahead_days`
    for `fs book`/`fs at`/`fs find`'s date loops. It should build a
    `{key: desk}` index once and look up in it from then on."""
    catalog.desks()                            # populate the real list
    counting = CountingList(catalog._desks)
    catalog._desks = counting
    catalog._desk_index = None                 # force a rebuild against the spy

    catalog.desk_by_key("L5.D.187")
    catalog.desk_by_key("L5.D.188")
    catalog.desk_by_key("L5.D.189")

    assert counting.iterations <= 1


def test_desk_by_key_keeps_the_first_desk_when_a_key_repeats(catalog):
    """PLAN.md's "Next up" item 3: the `{d.key: d for d in self.desks()}`
    index silently changed first-match-wins (the old linear scan's
    behaviour) to last-match-wins if a key is ever seen twice -- e.g. a
    desk double-listed across two floorplans during a floor migration."""
    catalog.desks()
    first = Desk("DUPE", cid=1, planid=1, groupid=None, floor="Level 1")
    second = Desk("DUPE", cid=2, planid=2, groupid=None, floor="Level 2")
    catalog._desks = [first, second]
    catalog._desk_index = None                 # force a rebuild

    assert catalog.desk_by_key("DUPE") is first


def test_tag_map_is_keyed_by_desk_and_skips_untagged_desks(catalog):
    """`fmt_desk`'s `tags` param (item 12's desk-tags bullet) -- built from
    the already-cached `desks()`, no extra API call."""
    tags = catalog.tag_map()
    assert tags["L5.D.220"]                        # a desk with real tags
    assert "L5.D.187" not in tags                   # untagged: no empty entry
    assert tags["L5.D.220"] == catalog.desk_by_key("L5.D.220").tags


def test_cached_tag_map_is_empty_on_a_cold_cache_and_touches_no_api(tmp_path):
    """`cached_tag_map()` -- `desks_cmd.py`'s bare `fs desks`/`fs desks
    <name>` listings use this instead of `tag_map()` so they keep `fs
    status`'s "never triggers a login" contract. A cold cache (nothing on
    disk, nothing fetched yet this run) must answer `{}`, not fall through
    to `desks()`'s live `floorplan-booking` calls."""
    api = CountingApi()
    catalog = Catalog(api, CacheStore(tmp_path / "cache.json"))
    assert catalog.cached_tag_map() == {}
    assert not api.counts


def test_cached_tag_map_reads_a_warm_disk_cache_without_a_live_call(tmp_path):
    """Once `desks()` has populated the on-disk cache (in an earlier run,
    or earlier this run), `cached_tag_map()` reads it back without any new
    API call -- same disk-cache path `_cached_desks()` already gives
    `desks()` itself."""
    cache_path = tmp_path / "cache.json"
    warm = Catalog(FixtureApi(), CacheStore(cache_path))
    warm.desks()                                # populates the disk cache

    api = CountingApi()
    catalog = Catalog(api, CacheStore(cache_path))
    tags = catalog.cached_tag_map()
    assert tags["L5.D.220"]
    assert not api.counts


def test_cached_tag_map_reads_the_in_memory_desks_if_already_fetched(tmp_path):
    """A `desks()` call earlier in the same `Catalog` instance's life (no
    cache needed) is also a valid source -- `cached_tag_map()` must not
    ignore `self._desks` just because there's no `CacheStore`."""
    catalog = Catalog(FixtureApi())             # no cache=... at all
    catalog.desks()
    tags = catalog.cached_tag_map()
    assert tags["L5.D.220"]


# -- availability, and the trap --------------------------------------------

def test_a_free_desk_is_not_necessarily_a_bookable_one(catalog):
    """THE finding that reshaped this build. On the captured Level 5, 15
    desks were free but only 7 would accept an advance booking -- so a
    picker filtering on "free" alone is wrong more often than right.

    `L5.D.202` is the specific desk that fooled the first write probe: free,
    reservable, and refused with `code: 64`.
    """
    states = catalog.availability(DAY)
    free = [s for s in states.values() if s.free]
    bookable = [s for s in states.values() if s.bookable]
    assert len(bookable) < len(free)

    trap = states["L5.D.202"]
    assert trap.free is True
    assert trap.book_advance is False
    assert trap.bookable is False


def test_bookable_filters_a_preference_list_without_reordering_it(catalog):
    """`fs book` walks a group in preference order, so order in must be
    order out -- minus whatever the server would refuse."""
    got = catalog.bookable(DAY, ["L5.D.202", "L5.D.187", "L5.D.216A"])
    assert [s.key for s in got] == ["L5.D.187"]


def test_bookable_only_queries_floors_the_target_keys_are_on(tmp_path):
    """The performance fix: `fs book`'s per-date cost used to be one
    `floorplan-booking` call per FLOOR, even when the target is a single
    desk or a small group confined to one of them. `bookable(keys=...)`
    must restrict `availability()` to the floor(s) those keys resolve to."""
    api = CountingApi()
    catalog = Catalog(api, CacheStore(tmp_path / "cache.json"))
    catalog.desks()                        # populate identity/planid index
    before = api.counts.get("floorplan-booking", 0)
    catalog.bookable(DAY, ["L5.D.202", "L5.D.187", "L5.D.216A"])  # all planid 3
    # One call for the single floor these keys are on, not one per floor
    # (2 non-empty floors in this fixture).
    assert api.counts["floorplan-booking"] == before + 1


def test_bookable_with_no_keys_still_queries_every_floor(catalog):
    """The unrestricted form (`fs at`/`fs find`'s use of `availability()`,
    and `bookable()` with no `keys`) must be unaffected -- it answers for
    the whole building, so every floor is still fetched."""
    states = catalog.availability(DAY)
    assert {s.desk.planid for s in states.values()} == {3, 4}


def test_bookable_with_unresolvable_keys_queries_no_floor(tmp_path):
    """A `keys` list that resolves to no real desk at all (e.g. a stale
    `config.toml` entry) must not fall back to querying every floor for
    nothing -- the old unrestricted behaviour for a case that can only ever
    return an empty result anyway."""
    api = CountingApi()
    catalog = Catalog(api, CacheStore(tmp_path / "cache.json"))
    catalog.desks()
    before = api.counts.get("floorplan-booking", 0)
    got = catalog.bookable(DAY, ["NO.SUCH.DESK"])
    assert got == []
    assert api.counts.get("floorplan-booking", 0) == before


def test_a_booked_desk_is_not_free(catalog):
    states = catalog.availability(DAY)
    taken = states["L5.D.216A"]
    assert taken.free is False
    assert taken.bkid


def test_a_booked_desk_carries_the_occupants_checkin_status(catalog):
    """`confirmed` lives in the envelope's `bookings` dict, a SIBLING of
    `info` that `unwrap` alone would discard -- `api.floorplan_booking`
    folds it back in under `info["bookings"]` for exactly this. `fs at`
    depends on it to show whether the occupant has checked in."""
    taken = catalog.availability(DAY)["L5.D.216A"]
    assert taken.uid == "81278872"
    assert taken.confirmed is False


def test_a_free_desk_is_not_confirmed(catalog):
    free = [s for s in catalog.availability(DAY).values() if s.free]
    assert free and all(s.confirmed is False for s in free)


class _StubBkidApi:
    """A minimal Api double: one floor, one desk, and a `bookings` list
    supplied by the test -- so `confirmed` can be checked for landing on
    the record that actually matches the desk row's own `bkid` rather than
    whichever the server happened to list first."""

    def __init__(self, records):
        self._records = records

    def floorplan_list(self):
        return [{"planid": 3}]

    def floorplan_booking(self, planid, day, start, finish):
        return {
            "name": "Level 3",
            "desks": [{"key": "L3.D.1", "cid": 1, "planid": 3,
                      "groupid": 1, "bkid": "current", "uid": "u1",
                      "reserved": True, "book_advance": False}],
            "bookings": {"L3.D.1": self._records},
        }


def test_availability_matches_confirmed_by_bkid_not_list_order():
    """The bug this pins: reading `records[0]` blindly picks whichever
    record the server lists first. Here the CURRENT booking (`bkid`
    matching the desk row) is confirmed but listed second -- a stale
    unrelated record sits first."""
    api = _StubBkidApi(records=[
        {"bkid": "stale", "confirmed": False},
        {"bkid": "current", "confirmed": True},
    ])
    cat = Catalog(api)
    state = cat.availability(DAY)["L3.D.1"]
    assert state.confirmed is True


def test_availability_falls_back_to_first_record_if_none_match_bkid():
    """No record's own `bkid` matches the desk row's -- fall back to the
    first record rather than showing an unconfirmed desk with no record
    read at all, same as the pre-fix default."""
    api = _StubBkidApi(records=[{"bkid": "other", "confirmed": True}])
    cat = Catalog(api)
    state = cat.availability(DAY)["L3.D.1"]
    assert state.confirmed is True


def test_availability_is_never_served_from_cache(tmp_path):
    """`book_advance` is evaluated per user and `reserved` is per day (§5.3),
    so a cached answer would have `fs book` offering desks the server then
    refuses. Two calls must hit the API twice."""
    api = CountingApi()
    catalog = Catalog(api, CacheStore(tmp_path / "cache.json"))
    catalog.desks()
    before = api.counts["floorplan-booking"]
    catalog.availability(DAY)
    catalog.availability(DAY)
    assert api.counts["floorplan-booking"] == before + 4     # 2 floors x 2


# -- caching ---------------------------------------------------------------

def test_desk_identity_is_cached_across_instances(tmp_path):
    path = tmp_path / "cache.json"
    first = CountingApi()
    Catalog(first, CacheStore(path)).desks()
    assert first.counts["floorplan-booking"] > 0

    second = CountingApi()
    desks = Catalog(second, CacheStore(path)).desks()
    assert len(desks) == 365
    assert "floorplan-booking" not in second.counts     # served from cache


def test_a_stale_cache_is_refetched(tmp_path):
    path = tmp_path / "cache.json"
    Catalog(FixtureApi(), CacheStore(path)).desks()
    api = CountingApi()
    later = Catalog(api, CacheStore(path),
                    now=lambda: __import__("time").time() + DESK_TTL_S + 1)
    later.desks()
    assert api.counts["floorplan-booking"] > 0


def test_a_corrupt_cache_is_treated_as_empty(tmp_path):
    """A cache that can fail the command it was meant to speed up is worse
    than no cache."""
    path = tmp_path / "cache.json"
    path.write_text("{not json")
    catalog = Catalog(FixtureApi(), CacheStore(path))
    assert len(catalog.desks()) == 365


def test_the_cache_file_is_not_world_readable(tmp_path):
    path = tmp_path / "cache.json"
    Catalog(FixtureApi(), CacheStore(path)).desks()
    assert path.stat().st_mode & 0o077 == 0


def test_cached_desks_carry_no_permission_fields(tmp_path):
    """Identity is cached for a month; a permission stored beside it would
    be read a month later as though it still meant something."""
    path = tmp_path / "cache.json"
    Catalog(FixtureApi(), CacheStore(path)).desks()
    blob = json.dumps(json.loads(path.read_text())["desks"]["value"])
    assert "book_advance" not in blob
    assert "reserved" not in blob


# -- own identity ------------------------------------------------------------

def test_own_uid_is_discovered_from_booking_list(catalog):
    """No config field for this -- it's read off a booking, not typed in."""
    assert catalog.own_uid() == "47044577"


def test_own_ugroupid_is_the_real_value_not_the_old_hardcoded_11(catalog):
    """PLAN.md item 1: `groupid=11, ugroupid=11` was simply wrong -- the
    account's real `ugroupid` (from `user-lookup.json`) is 10. This is the
    regression test for that bug."""
    assert catalog.own_ugroupid() == 10


def test_own_uid_and_ugroupid_are_cached_across_instances(tmp_path):
    """Same cache tier as desk identity -- discovered once, not on every
    command."""
    path = tmp_path / "cache.json"
    first = CountingApi()
    Catalog(first, CacheStore(path)).own_ugroupid()
    assert first.counts.get("booking-list")
    assert first.counts.get("user")

    second = CountingApi()
    ugroupid = Catalog(second, CacheStore(path)).own_ugroupid()
    assert ugroupid == 10
    assert "booking-list" not in second.counts
    assert "user" not in second.counts


# -- policy ----------------------------------------------------------------

def test_policy_supplies_the_booking_start_rather_than_a_hardcoded_480(catalog):
    assert catalog.book_day_start_mins() == 480


def test_the_booking_window_is_ten_days(catalog):
    """`book_advance_mins: 14400`, agreeing with the ~60-day refusal (§8)."""
    assert catalog.window_days() == 10


def test_policy_is_read_for_the_real_ugroupid_not_11(catalog):
    """`ugroupid=10`'s `booking_confirm_mins` (90) differs from the old
    hardcoded `ugroupid=11`'s (0) -- proof the two ids genuinely diverge,
    not just a hypothetical risk. `policy()` must land on 10's fixture."""
    assert catalog.policy().get("booking_confirm_mins") == 90


# -- lockers ---------------------------------------------------------------

def test_lockers_expose_the_expiry(catalog):
    lockers = catalog.lockers()
    assert len(lockers) == 1
    assert lockers[0]["key"] == "5-227"
    assert lockers[0]["finish"] > 0


def test_lockers_are_cached_so_ordinary_commands_pay_nothing(tmp_path):
    path = tmp_path / "cache.json"
    Catalog(CountingApi(), CacheStore(path)).lockers()
    api = CountingApi()
    Catalog(api, CacheStore(path)).lockers()
    assert "res-list" not in api.counts


# -- desk geometry -----------------------------------------------------------

def test_deskpolys_returns_every_poly_keyed_by_desk_id(catalog):
    polys = catalog.deskpolys(3)
    assert len(polys) == 262
    assert polys["L5.D.04"]["id"] == "L5.D.04"
    assert len(polys["L5.D.04"]["points"]) == 4


def test_deskpolys_includes_ghost_polys_with_no_catalog_desk(catalog):
    # Level 6 has 3 poly entries (L6.D.100/101/102) with geometry but no
    # `info.desks[]` entry -- deskpolys() does NOT filter these out, per
    # docs/floorplan-map-manual.md's "which polys are real desks" finding.
    # map_cmd.py is the one that filters, using catalog.desks().
    polys = catalog.deskpolys(4)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == 4}
    ghost_ids = set(polys) - catalog_keys
    assert ghost_ids  # at least one ghost poly present, unfiltered


def test_floor_image_size_matches_the_captured_fixture(catalog):
    assert catalog.floor_image_size(3) == (3041, 2013)


def test_deskpolys_is_cached_like_desk_identity(tmp_path):
    api = CountingApi()
    cat = Catalog(api, CacheStore(tmp_path / "cache.json"))
    cat.deskpolys(3)
    cat.deskpolys(3)
    assert api.counts.get("floorplan-booking", 0) == 1


def test_deskpolys_and_floor_image_size_share_one_fetch(tmp_path):
    api = CountingApi()
    cat = Catalog(api, CacheStore(tmp_path / "cache.json"))
    cat.deskpolys(3)
    cat.floor_image_size(3)
    assert api.counts.get("floorplan-booking", 0) == 1
