"""Desk identity, desk availability, booking policy, and the locker -- with
the cache boundary drawn where the data actually differs.

The one decision this module encodes, and the reason it isn't just a dict of
TTLs: **desk identity and desk permission have completely different
lifetimes, and caching them together is a bug.**

  * `key`, `cid`, `planid`, `groupid` describe the furniture. They change
    when the office is rearranged, so 30 days is generous and safe.
  * `book_advance`, `reserved`, and occupancy describe *this session's view
    of this day*. `book_advance` is almost certainly evaluated per user
    (`floorsense-api-manual.md` §5.3) and `reserved` is literally "someone
    has booked it in the window you asked about". Cached for even an hour,
    they would have `fs book` offering desks the server then refuses -- which
    the user experiences as the tool being broken.

So `desks()` is cached and `availability(day)` never is. The numbers say why
this matters: on the captured Level 5, 15 of 262 desks were free, but only 7
of those 15 would accept an advance booking. A picker that filters on "free"
alone is wrong more often than it is right.
"""

import datetime as dt
import json
import os
import time

__all__ = ["Catalog", "Desk", "DeskState", "CacheStore", "DESK_TTL_S"]

#: Furniture moves rarely. Long, because the cost of a stale entry is one
#: failed match and a re-read, not a wrong booking.
DESK_TTL_S = 30 * 24 * 3600

#: The locker is checked at most daily -- §8's renewal deadline is ~6 months
#: away, so a day's staleness cannot matter, and this keeps ordinary commands
#: at zero extra calls.
LOCKER_TTL_S = 24 * 3600

#: Policy (`book_day_start`, `book_advance_mins`) changes about as often as
#: the office does, but it is one cheap call and being wrong about
#: `book_day_start` means every booking is refused. A day is the compromise.
POLICY_TTL_S = 24 * 3600

FILE_MODE = 0o600


class Desk:
    """Identity only. Deliberately carries no `book_advance` and no
    `reserved`: this object is cached for a month, and a permission stored on
    it would be read a month later as though it still meant something."""

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
    offer a desk, and it is the AND of the two conditions that get conflated:
    nobody has it, and the server will let *you* have it.
    """

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
    beside things that do, so it is written with the same care.

    Every entry is `{"at": <unix>, "value": ...}`. A corrupt or unreadable
    cache is treated as an empty one: a cache that can fail the command it
    was meant to speed up is worse than no cache.
    """

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
    """Local midnight to local midnight. `floorplan-booking` wants the window
    as unix timestamps and the date as `DD/MM/YYYY` -- `api.py` handles the
    second, this handles the first."""
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
        """Floors that actually have desks.

        `floorplan-list` enumerates four floorplans here and two of them
        carry zero desks (§5.3), so the list is derived from what
        `floorplan-booking` returns rather than from the floor list -- and
        cached alongside the desks, since re-deriving it costs the same calls
        that building the catalog does.
        """
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
        same as an empty tuple, and there's no reason to carry one of every
        desk in this deployment's ~365 for the ~19 that have any."""
        return {d.key: d.tags for d in self.desks() if d.tags}

    def cached_tag_map(self):
        """Like `tag_map()`, but NEVER triggers `desks()`'s live fetch (and
        therefore never forces a login) -- reads only what's already in
        memory this run or on disk via `_cached_desks()`, returning `{}` on
        a cold cache instead of falling through to the network.

        For callers that must keep `fs status`'s "never triggers a login"
        contract even on a listing path that would *like* to show a tag if
        one happens to be cheaply known -- `desks_cmd.py`'s bare `fs desks`
        and `fs desks <name>` (no verb) listings."""
        if self._desks is not None:
            return {d.key: d.tags for d in self._desks if d.tags}
        cached = self._cached_desks()
        if not cached:
            return {}
        desks, _planids = cached
        return {d.key: d.tags for d in desks if d.tags}

    def desk_by_key(self, key):
        """O(1) after the first call -- `availability()` calls this once per
        desk row per floor per requested date, so a linear scan here becomes
        O(n^2) work multiplied by `book_ahead_days` for `fs book`/`fs at`/
        `fs find`'s date loops. The index is built lazily (not inside
        `desks()` itself, which plenty of callers use without ever needing
        it) and invalidated wherever `self._desks` is (re)assigned.

        Keys are assumed unique across floors. Built with `setdefault`
        rather than a `{d.key: d ...}` comprehension so a key seen twice
        (e.g. a desk double-listed across two floorplans during a floor
        migration) keeps the first one instead of silently becoming
        last-match-wins."""
        if self._desk_index is None:
            # Built in a local dict, not `self._desk_index` directly: a
            # first call's `self.desks()` populates `self._desks` as a side
            # effect, which resets `self._desk_index` to None itself (see
            # `desks()`) -- assigning into it while that's still in flight
            # would have the reset clobber entries added before it fired.
            index = {}
            for d in self.desks():
                index.setdefault(d.key, d)
            self._desk_index = index
        return self._desk_index.get(key)

    # -- availability (never cached) ----------------------------------------

    def availability(self, day, planids=None):
        """Every desk's state for one day: free, and advance-bookable BY YOU.

        Deliberately not cached and deliberately not memoised across days --
        see the module docstring. `reserved` is the field that says whether
        the desk is taken in the window asked about: across all 262 desks of
        the captured Level 5 it agreed exactly with both the presence of a
        `bkid` on the desk and membership of the response's `bookings` dict,
        with zero disagreements, so one field is enough.

        `planids`, when given, restricts the floors actually queried --
        `bookable()` passes only the floors its `keys` can possibly be on,
        which is the whole of `fs book`'s per-date cost: one
        `floorplan-booking` call per floor per date otherwise, most of them
        wasted work fetching floors the target group has no desk on at all.
        Defaults to every floor (`self.planids()`) when omitted, which is
        what every caller other than `bookable()` needs -- `fs at`/`fs find`
        answer for the whole building, not a preference list.
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
                # `desks[]`'s own `uid`/`bkid` say WHO and WHICH booking;
                # `confirmed` (check-in) only lives on the full record in the
                # sibling `bookings` dict (api.py's `floorplan_booking`).
                # Matched by `bkid`, not just `records[0]`: every captured
                # fixture only ever carries one record per desk key, but
                # nothing guarantees that holds server-side (e.g. a stale,
                # not-yet-cleaned-up record sitting alongside the current
                # one) -- reading the record that matches the desk row's own
                # `bkid` is what keeps `confirmed` describing the booking
                # `fs at`/`fs list` are actually about, not whichever one the
                # server happened to list first.
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

        This is the method `fs book` should call and `availability` is the
        one it should not: the filter that matters is `bookable`, not `free`.

        When `keys` is given, `availability()` is only asked about the
        floors those keys are actually on -- resolved via `desk_by_key`
        (identity, cached for a month, so this costs no extra call) rather
        than every floor in the building. `fs book`'s target is normally one
        desk or a handful in one or two groups, so this is the difference
        between one `floorplan-booking` call per date and one per
        floor-per-date: on the captured deployment (2 non-empty floors),
        half the calls for a single-floor target, and it only grows with
        floor count. A `keys` list whose desks don't resolve at all (a stale
        config entry, say) queries no floor rather than every floor for
        nothing -- `availability` already returns `{}` in that case, so
        the empty-list result here is unchanged from before.
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
        """This account's `uid`, discovered rather than configured.

        The only source today is a row in `booking-list` -- a brand-new
        account with zero bookings has none to read it from, and
        `user-search` cannot safely stand in: its hits carry no `email`
        field, so there is nothing in `config.toml` to match a hit against
        (confirmed live, `scripts/probes/user_lookup_probe.py`;
        `floorsense-api-manual.md`'s Users section). That gap is still open
        -- see PLAN.md's "Still open" table.
        """
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
        must use, never a hardcoded default (DECISIONS.md's groupid-footgun
        entry: `groupid=11, ugroupid=11` was simply wrong; this account's
        real value is 10).

        `GET /app/user?uid=<uid>` (no `bkid` needed -- confirmed live, see
        `api.user`) returns it directly. Cached like desk identity: a
        user's group changes about as often as the furniture does.
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

    # -- policy (cached briefly) --------------------------------------------

    def policy(self, refresh=False):
        """`book_day_start` and `book_advance_mins`, from the server.

        Read rather than hardcoded because `book_day_start` is what makes a
        `booking-create` succeed at all: the server stores `start` verbatim
        and refuses anything off-slot (§8).

        Both `groupid` and `ugroupid` are the account's own `ugroupid` --
        `floorsense-api-manual.md`'s note on `groupid=11 vs ugroupid=10`:
        "read the policy once for your own `ugroupid` and use it for every
        desk", not one policy call per desk group.
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
        """The booking window in whole days. `book_advance_mins` was 14400 =
        exactly 10 days, confirmed against a refusal at ~60 days (§8)."""
        mins = (self.policy() or {}).get("book_advance_mins")
        return int(mins // (24 * 60)) if isinstance(mins, int) else None

    # -- lockers (cached daily) ---------------------------------------------

    def lockers(self, refresh=False):
        """Locker reservations, `finish` being the expiry.

        Cached for a day so the expiry warning costs nothing on ordinary
        commands -- the deadline it guards against is ~6 months out, so a
        day's staleness is immaterial and a warning that costs a round trip
        on every command would be turned off.
        """
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
        """Shared cache entry backing `deskpolys()`/`floor_image_size()` --
        one `floorplan-booking` fetch for both, cached at `DESK_TTL_S` like
        `desks()`: image dimensions and desk poly geometry are both
        identity-shaped properties of the floorplan asset, not permission
        data. Internal -- not part of `Catalog`'s public surface.
        """
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
        `planid`, UNFILTERED against the desk catalog -- Level 6 carries 3
        ghost polys with no catalog desk behind them (§"which polys are
        real desks" in `docs/floorplan-map-manual.md`). Callers filter
        against `desks()` themselves before treating an entry as a real
        desk to render. Cached at `DESK_TTL_S` like `desks()` -- exact
        pixel geometry is identity-shaped, not permission data.
        """
        return dict(self._cached_floorplan_raw(planid, refresh)["polys"])

    def floor_image_size(self, planid, refresh=False):
        """`(imgwidth, imgheight)` in pixels for `planid`'s floorplan
        image -- every hand-traced fraction table `map_cmd.py` uses
        (walls, partitions, room boxes, crop) is in image-fraction terms,
        so rendering needs this to convert. Shares `deskpolys()`'s cached
        fetch, not a second call.
        """
        raw = self._cached_floorplan_raw(planid, refresh)
        return raw.get("imgwidth"), raw.get("imgheight")
