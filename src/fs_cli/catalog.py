"""Desk identity, desk availability, booking policy, and the locker -- with
the cache boundary drawn where the data actually differs.

Desk identity (`key`/`cid`/`planid`/`groupid`) changes when the office is
rearranged, so it's cached 30 days. `book_advance`/`reserved`/occupancy
describe this session's view of this day -- per-user permission data that
can't be cached even briefly without `fs book` offering desks the server
then refuses. So `desks()` is cached and `availability(day)` never is.
"""

import datetime as dt
import json
import os
import time

__all__ = ["Catalog", "Desk", "DeskState", "CacheStore", "DESK_TTL_S"]

#: Furniture moves rarely; a stale entry costs one failed match, not a
#: wrong booking.
DESK_TTL_S = 30 * 24 * 3600

#: The renewal deadline is ~6 months out, so a day's staleness is fine.
LOCKER_TTL_S = 24 * 3600

#: Cheap to fetch, but being wrong about `book_day_start` means every
#: booking is refused -- a day is the compromise.
POLICY_TTL_S = 24 * 3600

FILE_MODE = 0o600


class Desk:
    """Identity only -- no `book_advance`/`reserved`: this is cached for a
    month, and a permission stored on it would be read a month stale."""

    __slots__ = ("key", "cid", "planid", "groupid", "floor", "tags")

    def __init__(self, key, cid, planid, groupid, floor=None, tags=()):
        self.key = key
        self.cid = cid
        self.planid = planid
        self.groupid = groupid
        self.floor = floor
        self.tags = tuple(tags)

    def as_dict(self):
        return {"key": self.key, "cid": self.cid, "planid": self.planid,
                "groupid": self.groupid, "floor": self.floor,
                "tags": list(self.tags)}

    @classmethod
    def from_dict(cls, d):
        return cls(d["key"], d["cid"], d["planid"], d.get("groupid"),
                   d.get("floor"), d.get("tags") or ())

    def __repr__(self):
        return f"<Desk {self.key} cid={self.cid} group={self.groupid}>"


class DeskState:
    """A desk as it is for one user on one day. Never cached, never stored.
    `bookable` is the only field callers should use to decide whether to
    offer a desk -- the AND of "nobody has it" and "the server will let
    you have it", two conditions that get conflated."""

    __slots__ = ("desk", "free", "book_advance", "bkid", "uid", "confirmed")

    def __init__(self, desk, free, book_advance, bkid=None, uid=None,
                 confirmed=False):
        self.desk = desk
        self.free = free
        self.book_advance = book_advance
        self.bkid = bkid or None
        self.uid = uid or None
        self.confirmed = bool(confirmed)

    @property
    def key(self):
        return self.desk.key

    @property
    def bookable(self):
        return bool(self.free and self.book_advance)

    def __repr__(self):
        return (f"<DeskState {self.desk.key} free={self.free} "
                f"advance={self.book_advance}>")


class CacheStore:
    """`cache.json` -- machine-owned, 0600, holds nothing secret but sits
    beside things that do. Every entry is `{"at": <unix>, "value": ...}`.
    A corrupt or unreadable cache is treated as empty: a cache that can
    fail the command it was meant to speed up is worse than no cache."""

    def __init__(self, path):
        self.path = path
        self._data = None

    def _load(self):
        if self._data is None:
            try:
                loaded = json.loads(self.path.read_text())
                self._data = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                self._data = {}
        return self._data

    def get(self, name, ttl, now=None):
        entry = self._load().get(name)
        if not isinstance(entry, dict) or "value" not in entry:
            return None
        if (now or time.time()) - entry.get("at", 0) >= ttl:
            return None
        return entry["value"]

    def put(self, name, value, now=None):
        data = self._load()
        data[name] = {"at": int(now or time.time()), "value": value}
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     FILE_MODE)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)

    def clear(self):
        self._data = {}
        try:
            self.path.unlink()
        except OSError:
            pass


def day_bounds(day):
    """Local midnight to local midnight, as unix timestamps -- `api.py`
    handles the `DD/MM/YYYY` form `floorplan-booking` also wants."""
    start = dt.datetime.combine(day, dt.time.min).astimezone()
    return int(start.timestamp()), int((start + dt.timedelta(days=1)).timestamp())


class Catalog:
    """Everything a command needs to know about desks, policy and lockers."""

    def __init__(self, api, cache=None, now=time.time):
        self.api = api
        self.cache = cache
        self._now = now
        self._desks = None
        self._planids = None
        self._desk_index = None

    # -- desk identity (cached) ---------------------------------------------

    def planids(self):
        """Floors that actually have desks -- derived from what
        `floorplan-booking` returns (some floorplans carry zero desks),
        not the floor list, and cached alongside `desks()`."""
        if self._planids is None:
            self.desks()
        return self._planids

    def _cached_desks(self):
        if self.cache is None:
            return None
        payload = self.cache.get("desks", DESK_TTL_S, self._now())
        if not isinstance(payload, dict) or not payload.get("desks"):
            return None
        return ([Desk.from_dict(d) for d in payload["desks"]],
                list(payload.get("planids") or []))

    def desks(self, refresh=False):
        """Every desk on every floor, by identity. One call per non-empty
        floorplan, so this is the expensive one -- hence the 30-day cache."""
        if self._desks is not None and not refresh:
            return self._desks

        if not refresh:
            cached = self._cached_desks()
            if cached:
                self._desks, self._planids = cached
                self._desk_index = None
                return self._desks

        found, planids = [], []
        day = dt.date.today()
        start, finish = day_bounds(day)
        for planid in self._candidate_planids():
            info = self.api.floorplan_booking(planid, day, start, finish)
            rows = (info or {}).get("desks") or []
            if not rows:
                continue                      # a floor with no desks (§5.3)
            planids.append(planid)
            floor = (info or {}).get("name")
            for row in rows:
                if not isinstance(row, dict) or not row.get("key"):
                    continue
                found.append(Desk(
                    row["key"], row.get("cid"), row.get("planid", planid),
                    row.get("groupid"), floor,
                    tuple(t.get("name") for t in (row.get("desktags") or [])
                          if isinstance(t, dict) and t.get("name"))))

        self._desks, self._planids = found, planids
        self._desk_index = None
        if self.cache is not None and found:
            self.cache.put("desks",
                           {"desks": [d.as_dict() for d in found],
                            "planids": planids}, self._now())
        return found

    def _candidate_planids(self):
        info = self.api.floorplan_list()
        rows = info if isinstance(info, list) else (info or {}).get("floorplans")
        out = []
        for row in rows or []:
            if isinstance(row, dict) and isinstance(row.get("planid"), int):
                out.append(row["planid"])
        return out or [1, 3, 4, 5]

    def desk_keys(self):
        """For `desks.match_desk`, which matches a token against real keys."""
        return [d.key for d in self.desks()]

    def tag_map(self):
        """For `desks.fmt_desk`'s `tags` param. Untagged desks are omitted
        rather than mapped to `()` -- `fmt_desk` treats a missing key the
        same way."""
        return {d.key: d.tags for d in self.desks() if d.tags}

    def cached_tag_map(self):
        """Like `tag_map()`, but NEVER triggers `desks()`'s live fetch (and
        therefore never forces a login) -- returns `{}` on a cold cache.
        For callers that must keep `fs status`'s "never logs in" contract,
        e.g. `fs desks`'s listings."""
        if self._desks is not None:
            return {d.key: d.tags for d in self._desks if d.tags}
        cached = self._cached_desks()
        if not cached:
            return {}
        desks, _planids = cached
        return {d.key: d.tags for d in desks if d.tags}

    def desk_by_key(self, key):
        """O(1) after the first call -- `availability()` calls this once
        per desk row per date, so a linear scan would be O(n^2). Built
        lazily, invalidated wherever `self._desks` is reassigned. Keys
        assumed unique across floors; `setdefault` keeps the first match
        if one is ever seen twice."""
        if self._desk_index is None:
            # Local dict, not `self._desk_index` directly: `self.desks()`
            # resets `self._desk_index` to None as a side effect, which
            # would clobber entries added before it fired.
            index = {}
            for d in self.desks():
                index.setdefault(d.key, d)
            self._desk_index = index
        return self._desk_index.get(key)

    # -- availability (never cached) ----------------------------------------

    def availability(self, day, planids=None):
        """Every desk's state for one day: free, and advance-bookable BY
        YOU. Deliberately not cached -- see the module docstring.
        `reserved` alone reliably says whether a desk is taken.

        `planids`, when given, restricts the floors queried --
        `bookable()` passes only the floors its `keys` can be on, avoiding
        wasted `floorplan-booking` calls. Defaults to every floor, which
        is what `fs at`/`fs find` need (whole building, not a preference
        list).
        """
        start, finish = day_bounds(day)
        states = {}
        for planid in (self.planids() if planids is None else planids):
            info = self.api.floorplan_booking(planid, day, start, finish)
            floor = (info or {}).get("name")
            bookings = (info or {}).get("bookings") or {}
            for row in (info or {}).get("desks") or []:
                if not isinstance(row, dict) or not row.get("key"):
                    continue
                desk = self.desk_by_key(row["key"]) or Desk(
                    row["key"], row.get("cid"), row.get("planid", planid),
                    row.get("groupid"), floor)
                # `desks[]` says WHO/WHICH booking; `confirmed` only lives
                # on the full record in the sibling `bookings` dict.
                # Matched by `bkid`, not `records[0]` -- nothing guarantees
                # only one record per key server-side (e.g. a stale one
                # left alongside the current booking).
                records = bookings.get(row["key"]) or []
                bkid = row.get("bkid")
                match = next((r for r in records if isinstance(r, dict)
                             and r.get("bkid") == bkid), None) \
                    if bkid is not None else None
                if match is None and records and isinstance(records[0], dict):
                    match = records[0]
                confirmed = bool(match.get("confirmed")) if match else False
                states[desk.key] = DeskState(
                    desk,
                    free=not row.get("reserved"),
                    book_advance=bool(row.get("book_advance")),
                    bkid=row.get("bkid"), uid=row.get("uid"),
                    confirmed=confirmed)
        return states

    def bookable(self, day, keys=None):
        """The desks worth offering, in the order given if one is given.
        This is the method `fs book` should call, not `availability` --
        the filter that matters is `bookable`, not `free`.

        When `keys` is given, `availability()` is only asked about the
        floors those keys are actually on (via `desk_by_key`, no extra
        call), not every floor in the building.
        """
        planids = None
        if keys is not None:
            wanted = set()
            for key in keys:
                desk = self.desk_by_key(key)
                if desk is not None:
                    wanted.add(desk.planid)
            planids = sorted(wanted)
        states = self.availability(day, planids=planids)
        if keys is None:
            return [s for s in states.values() if s.bookable]
        out = []
        for key in keys:
            state = states.get(key)
            if state is not None and state.bookable:
                out.append(state)
        return out

    # -- own identity (cached like desk identity) ----------------------------

    def own_uid(self, refresh=False):
        """This account's `uid`, discovered rather than configured -- the
        only source is a row in `booking-list`; a brand-new account with
        zero bookings has none to read it from, and `user-search` can't
        safely stand in (its hits carry no `email` to match against
        `config.toml`)."""
        if self.cache is not None and not refresh:
            cached = self.cache.get("own-uid", DESK_TTL_S, self._now())
            if isinstance(cached, str) and cached:
                return cached
        for row in self.api.booking_list() or []:
            uid = row.get("uid") if isinstance(row, dict) else None
            if uid:
                if self.cache is not None:
                    self.cache.put("own-uid", uid, self._now())
                return uid
        return None

    def own_ugroupid(self, refresh=False):
        """The account's real `ugroupid` -- the value every policy lookup
        must use, never a hardcoded default. `GET /app/user?uid=<uid>`
        returns it directly. Cached like desk identity.
        """
        if self.cache is not None and not refresh:
            cached = self.cache.get("own-ugroupid", DESK_TTL_S, self._now())
            if isinstance(cached, int):
                return cached
        uid = self.own_uid(refresh=refresh)
        if not uid:
            return None
        info = self.api.user(uid) or {}
        ugroupid = info.get("ugroupid")
        if isinstance(ugroupid, int) and self.cache is not None:
            self.cache.put("own-ugroupid", ugroupid, self._now())
        return ugroupid

    # -- occupant names (cached like desk identity) --------------------------

    def cached_user_name(self, uid):
        """A previously resolved `uid -> display name`, straight from the
        persistent cache -- no API call, `None` on a miss. Written by
        `at_cmd.occupant_name`; `fs at`/`fs map` both read, so a name
        resolved by either is available to the other immediately (`fs
        map`'s live view can show it on the first frame, not a background
        fetch later). One entry (`"user-names"`) holds the whole
        `{uid: name}` map, TTL'd as a whole like `desks()`.
        """
        if self.cache is None or not uid:
            return None
        names = self.cache.get("user-names", DESK_TTL_S, self._now())
        return names.get(uid) if isinstance(names, dict) else None

    def remember_user_name(self, uid, name):
        """Persist one resolved `uid -> name`, merged into whatever's
        already cached. Never called for the API-failure fallback (a
        bare uid isn't a real name, and caching it would block a future
        genuine lookup)."""
        if self.cache is None or not uid or not name:
            return
        names = self.cache.get("user-names", DESK_TTL_S, self._now())
        if not isinstance(names, dict):
            names = {}
        names = dict(names)
        names[uid] = name
        self.cache.put("user-names", names, self._now())

    # -- policy (cached briefly) --------------------------------------------

    def policy(self, refresh=False):
        """`book_day_start` and `book_advance_mins`, from the server.
        Read rather than hardcoded because `book_day_start` is what makes
        `booking-create` succeed -- the server refuses anything off-slot.
        Read once for the account's own `ugroupid`, not per desk group.
        """
        ugroupid = self.own_ugroupid(refresh=refresh)
        if ugroupid is None:
            return {}
        name = f"policy-{ugroupid}"
        if self.cache is not None and not refresh:
            cached = self.cache.get(name, POLICY_TTL_S, self._now())
            if isinstance(cached, dict):
                return cached
        info = self.api.desk_group_settings(ugroupid, ugroupid) or {}
        if self.cache is not None and info:
            self.cache.put(name, info, self._now())
        return info

    def book_day_start_mins(self):
        return (self.policy() or {}).get("book_day_start")

    def window_days(self):
        """The booking window in whole days, from `book_advance_mins`."""
        mins = (self.policy() or {}).get("book_advance_mins")
        return int(mins // (24 * 60)) if isinstance(mins, int) else None

    # -- lockers (cached daily) ---------------------------------------------

    def lockers(self, refresh=False):
        """Locker reservations, `finish` being the expiry. Cached for a
        day -- the expiry deadline is ~6 months out, so a day's staleness
        is immaterial, and a warning that cost a round trip every command
        would just get turned off."""
        if self.cache is not None and not refresh:
            cached = self.cache.get("lockers", LOCKER_TTL_S, self._now())
            if isinstance(cached, list):
                return cached
        rows = [r for r in self.api.res_list() if not r.get("released")]
        if self.cache is not None:
            self.cache.put("lockers", rows, self._now())
        return rows

    # -- desk geometry (cached like desk identity) ---------------------------

    def _cached_floorplan_raw(self, planid, refresh=False):
        """Shared cache entry backing `deskpolys()`/`floor_image_size()`
        -- one fetch for both, cached like `desks()`: image dimensions
        and poly geometry are identity-shaped, not permission data."""
        name = f"floorplan-raw-{planid}"
        if self.cache is not None and not refresh:
            cached = self.cache.get(name, DESK_TTL_S, self._now())
            if isinstance(cached, dict) and "polys" in cached:
                return cached
        day = dt.date.today()
        start, finish = day_bounds(day)
        info = self.api.floorplan_booking(planid, day, start, finish) or {}
        polys = {}
        for poly in ((info.get("deskpolys") or {}).get("polys") or []):
            if isinstance(poly, dict) and poly.get("id"):
                polys[poly["id"]] = poly
        payload = {"polys": polys, "imgwidth": info.get("imgwidth"),
                   "imgheight": info.get("imgheight")}
        if self.cache is not None and polys:
            self.cache.put(name, payload, self._now())
        return payload

    def deskpolys(self, planid, refresh=False):
        """Desk id -> raw poly rect for every `deskpolys` entry on
        `planid`, UNFILTERED against the desk catalog -- some polys have
        no catalog desk behind them. Callers filter against `desks()`
        themselves before treating an entry as real."""
        return dict(self._cached_floorplan_raw(planid, refresh)["polys"])

    def floor_image_size(self, planid, refresh=False):
        """`(imgwidth, imgheight)` in pixels -- `map_cmd.py`'s hand-traced
        fraction tables need this to convert. Shares `deskpolys()`'s
        cached fetch, not a second call."""
        raw = self._cached_floorplan_raw(planid, refresh)
        return raw.get("imgwidth"), raw.get("imgheight")
