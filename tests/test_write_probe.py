"""The Phase 0c cleanup path, exercised offline.

`scripts/probes/write_probe.py` makes real bookings on the user's live
account, and the only thing standing between a bug in it and a stranded
booking is `cleanup()` and its baseline diff. "Self-cleaning and verified"
is a claim about code nobody has run, so it gets run here -- against the
loopback fixture, with a booking store that actually changes when
something is released.

The failure really being guarded against is a FALSE CLEAN: cleanup reporting
success while a booking it created survives. That one is undetectable from
the report afterwards, which is exactly why it needs a test in front of it.
"""

import importlib
import json
import pathlib
import sys

import pytest

PROBES = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "probes"


@pytest.fixture
def probe(stack, monkeypatch):
    """Import write_probe and point its HTTP helpers at the loopback server."""
    sys.path.insert(0, str(PROBES))
    try:
        module = importlib.import_module("write_probe")
        fs = importlib.import_module("_fs_common")
        monkeypatch.setattr(fs, "ORIGIN", stack.origin)
        module.CREATED.clear()
        module.RESULTS.clear()
        yield module
    finally:
        sys.path.remove(str(PROBES))
        for name in ("write_probe", "_fs_common", "_okta_common"):
            sys.modules.pop(name, None)


@pytest.fixture
def session(stack):
    """A session the fixture server will accept: both cookies plus CSRF."""
    import requests
    from conftest import CSRF_TOKEN, REAL_ID
    s = requests.Session()
    s.cookies.set("id", REAL_ID, domain="127.0.0.1", path="/")
    s.cookies.set("MYSLSRV", "api-nz-b1", domain="127.0.0.1", path="/")
    s.headers["x-csrf-token"] = CSRF_TOKEN
    return s


class BookingStore:
    """A stand-in for the server's booking state: releasing a bkid actually
    removes it, so the cleanup diff has something real to observe."""

    def __init__(self, records):
        self.records = {r["bkid"]: dict(r) for r in records}
        self.released = []

    def list_response(self, _raw=None, _query=None):
        return {"result": True, "type": "response",
                "info": [r for r in self.records.values()
                         if not r.get("released")]}

    def release_response(self, raw, _query=None):
        bkid = str(json.loads(raw)["bkid"])
        self.released.append(bkid)
        if bkid in self.records:
            self.records[bkid]["released"] = 1
            return {"result": True, "message": "Booking released"}
        return {"result": False, "message": "No such booking"}

    def wire(self, stack):
        stack.state["api"]["booking-list"] = self.list_response
        stack.state["api"]["booking-release"] = self.release_response
        return self


def rec(bkid, key="L5.D.217A", start=1800000000):
    return {"bkid": bkid, "key": key, "cid": 3, "start": start,
            "bktype": "advance", "released": 0}


# --- all_bookings ----------------------------------------------------------

def test_all_bookings_covers_every_bktype_the_manual_lists(probe, session,
                                                           stack):
    # A bktype missing from the union is a booking the cleanup diff cannot
    # see -- and the report would then say CLEAN while one survived.
    seen = []
    stack.state["api"]["booking-list"] = lambda _raw, query: (
        seen.append(query.get("bktype", [None])[0])
        or {"result": True, "info": []})
    probe.all_bookings(session)
    for bktype in ("advance", "adhoc", "repeat", "fixed", "virtual", None):
        assert bktype in seen, f"{bktype!r} never queried"


def test_all_bookings_unions_and_drops_released(probe, session, stack):
    store = BookingStore([rec("1"), rec("2", "L5.D.235A")]).wire(stack)
    store.records["2"]["released"] = 1
    assert set(probe.all_bookings(session)) == {"1"}


# --- cleanup ---------------------------------------------------------------

def test_cleanup_releases_what_it_created_and_reports_clean(probe, session,
                                                            stack):
    store = BookingStore([rec("pre-1"), rec("test-a"),
                          rec("test-b")]).wire(stack)
    probe.CREATED.extend(["test-a", "test-b"])

    probe.cleanup(session, {"pre-1": rec("pre-1")})

    assert sorted(store.released) == ["test-a", "test-b"]
    assert "CLEAN" in [o for _p, o, _d in probe.RESULTS], probe.RESULTS
    # The user's own booking is untouched -- the point of the baseline.
    assert store.records["pre-1"]["released"] == 0


def test_cleanup_catches_a_stray_it_never_tracked(probe, session, stack):
    # The dangerous case: a booking created without a usable bkid in the
    # response, so CREATED never learned about it.
    store = BookingStore([rec("pre-1"), rec("untracked-stray")]).wire(stack)

    probe.cleanup(session, {"pre-1": rec("pre-1")})       # CREATED is empty

    assert "untracked-stray" in store.released, \
        "a stray booking was left on the account and reported as clean"
    assert "CLEAN" in [o for _p, o, _d in probe.RESULTS]


def test_cleanup_reports_incomplete_when_a_release_fails(probe, session,
                                                         stack):
    stack.state["api"]["booking-list"] = lambda *_a: {
        "result": True, "info": [rec("wont-go-away")]}
    stack.state["api"]["booking-release"] = lambda *_a: {
        "result": False, "message": "nope"}
    probe.CREATED.append("wont-go-away")

    probe.cleanup(session, {})

    assert "INCOMPLETE" in [o for _p, o, _d in probe.RESULTS], probe.RESULTS
    # And it must name the booking, so the user can go and remove it.
    assert any("wont-go-away" in d for _p, _o, d in probe.RESULTS)


def test_cleanup_flags_collateral_damage(probe, session, stack):
    # A booking that existed BEFORE the probe having gone missing is the
    # worst outcome available, and must never be reported as clean.
    BookingStore([]).wire(stack)

    probe.cleanup(session, {"pre-1": rec("pre-1")})

    assert "COLLATERAL DAMAGE" in [o for _p, o, _d in probe.RESULTS], \
        probe.RESULTS


def test_cleanup_never_releases_a_pre_existing_booking(probe, session, stack):
    store = BookingStore([rec("pre-1"), rec("pre-2")]).wire(stack)

    probe.cleanup(session, {"pre-1": rec("pre-1"), "pre-2": rec("pre-2")})

    assert store.released == [], "cleanup released a booking it did not create"


# --- create() tracks by diff -----------------------------------------------

def test_create_tracks_a_booking_even_with_no_bkid_in_the_response(
        probe, session, stack):
    # The response shape is not trusted: a before/after diff decides what
    # gets released.
    store = BookingStore([]).wire(stack)

    def creating(_raw, _query=None):
        store.records["new-1"] = rec("new-1")
        return {"result": True, "message": "Desk reserved"}      # no bkid

    stack.state["api"]["booking-create"] = creating
    bkid, data = probe.create(session, 1800000000, "L5.D.217A", 3)

    assert data["result"] is True
    assert "new-1" in probe.CREATED, "an untracked booking would be stranded"
    assert bkid == "new-1"


def test_create_records_nothing_when_the_server_refuses(probe, session,
                                                        stack):
    BookingStore([]).wire(stack)
    stack.state["api"]["booking-create"] = lambda *_a: {
        "result": False, "message": "User desk limit reached (Group)"}

    bkid, data = probe.create(session, 1800000000, "L5.D.217A", 3)

    assert bkid is None and data["result"] is False
    assert probe.CREATED == []
