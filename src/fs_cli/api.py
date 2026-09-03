"""One method per endpoint, and one place where the envelope is understood.

Everything above this module works in Python data; everything below it
works in HTTP. Exists mainly to make three traps unrepeatable:

  * **The envelope is not uniform.** §7 lists four shapes: success with
    `info`, a JSON failure with `message`, an HTML CSRF page, and a bare
    array (`tag-list`). `unwrap` checks it's looking at a dict before
    reaching for a key.
  * **`code: 64` is a catch-all covering five unrelated refusals** (§8),
    of which only the group desk limit is a legitimate answer to give
    the user. `refusal_kind` is the single place that branches on
    `message`.
  * **`booking-create` must not be sent a `finish`** (§8) -- the server
    computes it. `book_start_for` is the only way this module lets a
    caller build a `start`.

HTTP-level requirements (cookies, CSRF header, XHR headers) live in
`session.py`. This module never sets a header, so a new endpoint here
can't quietly opt out of them.
"""

import datetime as dt

from .errors import BusinessRuleRefused, CommError

__all__ = ["Api", "LiveApi", "unwrap", "records", "refusal_kind",
           "book_start_for", "DESK_LIMIT", "NOT_BOOKABLE_DESK",
           "NO_VALID_SLOT", "OUTSIDE_WINDOW", "UPDATE_FAILED", "UNKNOWN"]

# The five §8 refusals, named so callers compare against a constant rather
# than re-typing a server string they'd get subtly wrong.
DESK_LIMIT = "desk_limit"
NOT_BOOKABLE_DESK = "not_bookable_desk"
NO_VALID_SLOT = "no_valid_slot"
OUTSIDE_WINDOW = "outside_window"
UPDATE_FAILED = "update_failed"
UNKNOWN = "unknown"

# Matched against a lowercased `message`. Order matters only in that
# `update failed` wraps one of the others, so it is checked last -- its
# message contains the wrapped reason and would otherwise shadow it.
_REFUSALS = (
    ("user desk limit reached", DESK_LIMIT),
    ("cannot make an advance booking for this desk", NOT_BOOKABLE_DESK),
    ("duration not possible", NO_VALID_SLOT),
    ("this far in the future", OUTSIDE_WINDOW),
    ("booking update failed", UPDATE_FAILED),
)


def refusal_kind(message):
    """Classify a `code: 64` by its `message`, the only part that carries
    meaning (§8). A `booking-update` failure wraps the underlying reason,
    so that's looked for first and the wrapper only reported when
    nothing more specific matches.
    """
    text = (message or "").lower()
    for needle, kind in _REFUSALS:
        if kind is not UPDATE_FAILED and needle in text:
            return kind
    return UPDATE_FAILED if "booking update failed" in text else UNKNOWN


def unwrap(data, what="the server", request_day=None):
    """`{"result": true, "info": ...}` -> the `info`. Anything else raises.
    A bare array (§7 case 4) is returned as-is -- `tag-list` has no
    envelope. `request_day`, when known, disambiguates `NO_VALID_SLOT`'s
    hint -- see `_hint_for`.
    """
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        raise CommError(f"{what} returned {type(data).__name__}, not an object")

    if data.get("result") is True:
        return data.get("info")

    message = str(data.get("message") or "refused with no message")
    code = data.get("code")
    if code is not None:
        # A business rule, not a transport problem: the request was
        # well-formed and the server declined it on policy grounds.
        raise BusinessRuleRefused(message,
                                   hint=_hint_for(refusal_kind(message), request_day))
    raise CommError(f"{what} refused the request: {message}")


_HINTS = {
    DESK_LIMIT: "You already have a desk booked that day -- rebook it "
                "instead of booking a second.",
    NOT_BOOKABLE_DESK: "That desk does not allow advance booking for you.",
    NO_VALID_SLOT: "Advance bookings start at the day's opening time, so "
                   "today can't be booked.",
    OUTSIDE_WINDOW: "That date is past the booking window.",
}

# NO_VALID_SLOT's usual hint assumes a same-day refusal, but the server
# collapses "today, before opening time" and "a date already in the
# past" to the identical "duration not possible" message (§8/§12). Can't
# tell the two apart from the response, but can from the date sent.
_PAST_DATE_HINT = "That date has already passed."


def _hint_for(kind, request_day=None):
    if kind == NO_VALID_SLOT and request_day is not None \
            and request_day < dt.date.today():
        return _PAST_DATE_HINT
    return _HINTS.get(kind)


def records(info):
    """The record list out of an `info`, whatever shape it arrived in --
    `booking-list` puts an array directly under `info`; other endpoints
    put a dict. Deliberately NOT recursive: `floorplan-booking.bookings`
    and `booking-summary.userbookings` are dicts keyed by desk/uid, and a
    flatten-everything helper would produce nonsense from both. Callers
    that want those read them by name.
    """
    if isinstance(info, list):
        return [r for r in info if isinstance(r, dict)]
    if isinstance(info, dict):
        return [info]
    return []


def book_start_for(day, book_day_start_mins):
    """The exact `start` a `booking-create` needs: local midnight + the
    group's `book_day_start` (§8). The server stores this **verbatim** --
    an approximate value is refused with "duration not possible".
    `book_day_start_mins` comes from the policy endpoint, not a
    hardcoded 480, since it's a per-group setting.
    """
    if not isinstance(book_day_start_mins, int):
        raise CommError("no book_day_start from the policy endpoint; refusing "
                        "to guess a booking start time")
    midnight = dt.datetime.combine(day, dt.time.min).astimezone()
    return int(midnight.timestamp()) + book_day_start_mins * 60


class Api:
    """The endpoint surface, shared by the live and fixture backends.
    Subclasses provide `_get`/`_post`; everything else -- paths,
    parameter names, which calls unwrap -- is defined once here so the
    test suite exercises the same call shapes the live path does.
    """

    #: §5.1: the server caps this at 15 whatever you ask for, silently.
    SUMMARY_DAYS = 15

    def _get(self, path, params=None):
        raise NotImplementedError

    def _post(self, path, body=None):
        raise NotImplementedError

    # -- reads ---------------------------------------------------------------

    def booking_list(self):
        """Own future bookings. Called BARE: 0b established the bare call
        returns the same records as `?bktype=advance&days=30`, while
        `repeat` and `adhoc` return nothing at all."""
        return records(unwrap(self._get("booking-list"), "booking-list"))

    def booking_summary(self, tz="Pacific/Auckland", days=None):
        """Own bookings + everyone followed + their bookings + the
        server's working-day calendar, in one call (§5.1). `days` is
        capped at 15 server-side with no indication -- callers must
        count `info["days"]` rather than trusting what they asked for.
        """
        return unwrap(self._get("booking-summary", {
            "tz": tz, "days": days or self.SUMMARY_DAYS,
            "team": "true", "friend": "true"}), "booking-summary")

    def floorplan_list(self):
        """Floors. Carries NO desk keys -- the catalog is in
        `floorplan-booking` (§5.3)."""
        return unwrap(self._get("floorplan-list"), "floorplan-list")

    def floorplan_booking(self, planid, day, start, finish):
        """A floor's desks and their bookings for one day. `date` is
        `DD/MM/YYYY` -- the one parameter in this API that isn't a unix
        timestamp (§5).

        The envelope carries `bookings` as a SIBLING of `info`, not
        inside it (§5.3) -- full booking records `unwrap` alone would
        discard, so it's folded into the returned `info` here, once.
        """
        data = self._get("floorplan-booking", {
            "planid": planid, "date": day.strftime("%d/%m/%Y"),
            "start": start, "finish": finish})
        info = unwrap(data, "floorplan-booking")
        if isinstance(info, dict):
            info = dict(info)
            info["bookings"] = (data or {}).get("bookings") or {}
        return info

    def res_list(self):
        """Locker reservations. `finish` is the expiry (§5)."""
        return records(unwrap(self._get("res-list"), "res-list"))

    def user_search(self, name, start, finish):
        """Find a colleague. `future[]` IS bounded by the window given, so a
        one-day window answers only about today (§5.2)."""
        return records(unwrap(self._post("user-search", {
            "start": start, "finish": finish, "name": name, "desc": name}),
            "user-search"))

    def user(self, uid, bkid=None):
        """One user's identity, including `ugroupid`. `bkid` needs to be
        a real booking id only per the endpoint's "for a booking" framing
        -- a self-lookup by `uid` works with `bkid` omitted."""
        params = {"uid": uid}
        if bkid is not None:
            params["bkid"] = bkid
        return unwrap(self._get("user", params), "user")

    def desk_group_settings(self, groupid, ugroupid):
        """Booking policy: `book_day_start`, `book_advance_mins`. The source
        of truth for both the booking start time and the window width."""
        return unwrap(self._get("get_desk_group_booking_settings",
                                {"groupid": groupid, "ugroupid": ugroupid}),
                      "get_desk_group_booking_settings")

    # -- writes --------------------------------------------------------------

    def booking_create(self, start, key, cid, day=None):
        """Book a desk. No `finish` parameter, and there must not be one --
        the server computes it. `start` must be exact -- see
        `book_start_for`. `day` is passed through to `unwrap` only so it
        can tell a past-date refusal from a same-day one (`_hint_for`);
        it plays no part in the request itself.
        """
        return unwrap(self._post("booking-create", {
            "type": "advance", "start": start, "key": key, "cid": cid}),
            "booking-create", request_day=day)

    def booking_update(self, bkid, key, cid, day=None):
        """Move a booking to another desk. Atomic, same `bkid` (§8) -- the
        only correct way to rebook, since release-then-book opens a race
        and risks hitting the one-per-day group limit. `day` threaded
        through to `unwrap` for the same reason `booking_create` takes it.
        """
        return unwrap(self._post("booking-update",
                                 {"bkid": bkid, "key": key, "cid": cid}),
                      "booking-update", request_day=day)

    def booking_release(self, bkid):
        return unwrap(self._post("booking-release", {"bkid": bkid}),
                      "booking-release")

    def booking_confirm(self, bkid):
        """Check in to today's booking -- undocumented, found in the web
        UI's own JS. Same shape as `booking_release`: `{"bkid": ...}`.

        The web UI gates the "Confirm Booking" button on settings baked
        into `/app/site`'s HTML, not returned by any JSON endpoint here.
        The server enforces the same rule and refuses cleanly
        ("...outside early activate window", "Booking already
        confirmed") with no `code`, so `unwrap` raises a plain
        `CommError`, not `BusinessRuleRefused` -- deliberately not
        special-cased, since this module can't tell "too early" apart
        from any other refusal shape."""
        return unwrap(self._post("booking-confirm", {"bkid": bkid}),
                      "booking-confirm")

    def friend_create(self, uid):
        """Star a colleague. The UI says "following"; the API says "friend"
        (§5.2)."""
        return unwrap(self._post("friend-create", {"target": uid}),
                      "friend-create")

    def friend_delete(self, uid):
        return unwrap(self._post("friend-delete", {"target": uid}),
                      "friend-delete")


class LiveApi(Api):
    """The real thing. Every call goes through `Session`, which owns cookies,
    CSRF, the XHR headers, and the one-retry-on-dead-session rule."""

    def __init__(self, session):
        self.session = session

    def _get(self, path, params=None):
        return self.session.get(path, params=params)

    def _post(self, path, body=None):
        return self.session.post(path, body=body)
