"""A write-capable `Api` test double for `fs map`'s live book/release
tests (`test_map_cmd.py`'s `_run_live` write scenarios).

`FixtureApi` refuses every write it's asked to make -- on purpose, so an
invented response can never masquerade as a real one (see `fixtures.py`'s
module docstring). That's exactly wrong for THIS test need:
`_run_live`'s post-write refresh has to see a real state change, or
the "book -> refresh -> release -> quit" scenario the design spec calls
for can't be proven. `LiveWriteApi` is that -- every read passes straight
through to a real `FixtureApi`; `booking_create`/`booking_update`/
`booking_release` hold their effect in a small in-memory overlay that
`floorplan_booking`/`booking_list` fold in on every subsequent read.
Test-only, never imported by `src/fs_cli` -- mirrors how `scripts/probes/`
and `tests/test_write_probe.py` already keep write-capable test
infrastructure separate from the read-only fixtures (see this repo's
top-level `CLAUDE.md`, "Testing notes").
"""

import datetime as dt

from fs_cli.fixtures import FixtureApi

__all__ = ["LiveWriteApi"]

_PLANID_BY_PREFIX = {"L5.": 3, "L6.": 4}


def _planid_for_key(key):
    for prefix, planid in _PLANID_BY_PREFIX.items():
        if key.startswith(prefix):
            return planid
    raise ValueError(f"can't infer a floor for desk key {key!r}")


class LiveWriteApi:
    def __init__(self):
        self._fixture = FixtureApi()
        self._own = {}          # bkid (str) -> booking row
        self._next_bkid = 90000001

    def __getattr__(self, name):
        # Every read (deskpolys, desks, desk_group_settings, res_list,
        # booking_summary, ...) is unaffected by writes made through this
        # double, so it passes straight through -- only the methods
        # overridden below know about `self._own`.
        return getattr(self._fixture, name)

    def floorplan_booking(self, planid, day, start, finish):
        info = dict(self._fixture.floorplan_booking(planid, day, start, finish))
        desks = [dict(d) for d in info.get("desks") or []]
        bookings = dict(info.get("bookings") or {})
        for bkid, row in self._own.items():
            if row["planid"] != planid:
                continue
            if not (row["start"] < finish and row["finish"] > start):
                continue
            for d in desks:
                if d.get("key") == row["key"]:
                    d["reserved"] = True
                    d["bkid"] = bkid
                    d["uid"] = row["uid"]
            bookings[row["key"]] = [{"bkid": bkid, "uid": row["uid"],
                                     "confirmed": False, "key": row["key"]}]
        info["desks"] = desks
        info["bookings"] = bookings
        return info

    def booking_list(self):
        own_rows = [
            {"bkid": bkid, "key": row["key"], "start": row["start"],
             "finish": row["finish"], "released": False}
            for bkid, row in self._own.items()
        ]
        return list(self._fixture.booking_list()) + own_rows

    def booking_create(self, start, key, cid, day=None):
        bkid = str(self._next_bkid)
        self._next_bkid += 1
        # `day`, when given, wins over `start` -- mirrors `booking_update`
        # below, and is what keeps the overlay's window aligned with
        # `Catalog.day_bounds`'s local-midnight-to-local-midnight window
        # that `availability()` queries by.
        if day is not None:
            start = int(dt.datetime.combine(day, dt.time()).timestamp())
        finish = start + 24 * 3600
        self._own[bkid] = {"key": key, "cid": cid, "start": start,
                           "finish": finish, "uid": "test-uid",
                           "planid": _planid_for_key(key)}
        return {"result": "OK", "info": {"bkid": bkid}}

    def booking_update(self, bkid, key, cid, day=None):
        old = self._own.pop(str(bkid), None)
        if day is not None:
            start = int(dt.datetime.combine(day, dt.time()).timestamp())
        elif old is not None:
            start = old["start"]
        else:
            raise ValueError("booking_update needs day or a known bkid")
        finish = start + 24 * 3600
        new_bkid = str(self._next_bkid)
        self._next_bkid += 1
        self._own[new_bkid] = {"key": key, "cid": cid, "start": start,
                               "finish": finish, "uid": "test-uid",
                               "planid": _planid_for_key(key)}
        return {"result": "OK", "info": {"bkid": new_bkid}}

    def booking_release(self, bkid):
        self._own.pop(str(bkid), None)
        return {"result": "OK"}
