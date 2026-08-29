"""Envelope handling and refusal classification.

These tests are mostly about the ways the server says NO, because that is
where `floorsense-api-manual.md` records the project's actual bugs: four
distinct failure shapes under one status code, and five distinct refusals
under one `code: 64`.
"""

import datetime as dt

import pytest

from fs_cli import api
from fs_cli.errors import BusinessRuleRefused, CommError


class Recorder(api.Api):
    """An Api whose transport is a canned reply, so the endpoint methods can
    be checked for what they SEND as well as what they return."""

    def __init__(self, reply=None):
        self.reply = reply if reply is not None else {"result": True, "info": []}
        self.calls = []

    def _get(self, path, params=None):
        self.calls.append(("GET", path, params))
        return self.reply

    def _post(self, path, body=None):
        self.calls.append(("POST", path, body))
        return self.reply


# -- the envelope ----------------------------------------------------------

def test_success_yields_info():
    assert api.unwrap({"result": True, "info": {"a": 1}}) == {"a": 1}


def test_a_bare_array_is_returned_not_unwrapped():
    """`tag-list` has no envelope at all (§7 case 4). Reaching for `.get` on
    a list is how that becomes an AttributeError somewhere unhelpful."""
    assert api.unwrap([]) == []
    assert api.unwrap([{"x": 1}]) == [{"x": 1}]


def test_a_refusal_with_a_code_is_a_business_rule():
    with pytest.raises(BusinessRuleRefused) as caught:
        api.unwrap({"result": False, "code": 64,
                    "message": "User desk limit reached (Group)"})
    assert "desk limit" in str(caught.value).lower()


def test_a_refusal_without_a_code_is_a_comm_error():
    """`{"result": false, "message": "Invalid planid"}` is a malformed
    request, not a policy decision -- different exit code, different remedy."""
    with pytest.raises(CommError):
        api.unwrap({"result": False, "message": "Invalid planid"})


def test_a_non_object_response_does_not_crash_obscurely():
    with pytest.raises(CommError):
        api.unwrap("not json at all")


# -- code: 64, five meanings (§8) ------------------------------------------

@pytest.mark.parametrize("message,kind", [
    ("User desk limit reached (Group)", api.DESK_LIMIT),
    ("You cannot make an advance booking for this desk",
     api.NOT_BOOKABLE_DESK),
    ("Requested booking duration not possible", api.NO_VALID_SLOT),
    ("Advance bookings cannot be made this far in the future",
     api.OUTSIDE_WINDOW),
])
def test_each_refusal_is_told_apart_by_its_message(message, kind):
    assert api.refusal_kind(message) == kind


def test_booking_update_reports_the_wrapped_reason_not_the_wrapper():
    """`booking-update` wraps whatever the real reason was (§8), so the
    specific cause has to win over the generic wrapper -- otherwise every
    update failure looks identical."""
    assert api.refusal_kind(
        "Booking update failed: User desk limit reached (Group) (64)") \
        == api.DESK_LIMIT
    assert api.refusal_kind("Booking update failed: something new (64)") \
        == api.UPDATE_FAILED


def test_an_unrecognised_refusal_is_not_silently_classified():
    assert api.refusal_kind("Some new rule nobody has seen") == api.UNKNOWN
    assert api.refusal_kind(None) == api.UNKNOWN


def test_the_desk_limit_hint_tells_the_user_what_to_do():
    """It is the one code-64 refusal that is a real answer rather than a
    sign the client offered something invalid."""
    with pytest.raises(BusinessRuleRefused) as caught:
        api.unwrap({"result": False, "code": 64,
                    "message": "User desk limit reached (Group)"})
    assert "rebook" in (caught.value.hint or "").lower()


def test_no_valid_slot_hint_says_today_when_request_day_is_today():
    """The default hint text (PLAN.md item 11's same-day case)."""
    with pytest.raises(BusinessRuleRefused) as caught:
        api.unwrap({"result": False, "code": 64,
                    "message": "Requested booking duration not possible"},
                   request_day=dt.date.today())
    assert "today" in (caught.value.hint or "").lower()


def test_no_valid_slot_hint_says_passed_when_request_day_is_in_the_past():
    """PLAN.md item 11: the server collapses same-day and genuinely-past
    refusals to the identical message, but the client sent the date and
    can tell them apart from that."""
    past = dt.date.today() - dt.timedelta(days=7)
    with pytest.raises(BusinessRuleRefused) as caught:
        api.unwrap({"result": False, "code": 64,
                    "message": "Requested booking duration not possible"},
                   request_day=past)
    hint = (caught.value.hint or "").lower()
    assert "already passed" in hint
    assert "today" not in hint


def test_no_valid_slot_hint_falls_back_to_today_wording_with_no_request_day():
    """Callers with no date to pass (e.g. `booking_release`) must still get
    the pre-existing wording."""
    with pytest.raises(BusinessRuleRefused) as caught:
        api.unwrap({"result": False, "code": 64,
                    "message": "Requested booking duration not possible"})
    assert "today" in (caught.value.hint or "").lower()


def test_booking_update_also_gets_the_past_date_hint():
    """`booking_update` (REPLACE rows) hits the identical `NO_VALID_SLOT`
    collapse `booking_create` does -- it must thread `request_day` through
    to `unwrap` too, not just the create path."""
    past = dt.date.today() - dt.timedelta(days=7)
    r = Recorder({"result": False, "code": 64,
                  "message": "Requested booking duration not possible"})
    with pytest.raises(BusinessRuleRefused) as caught:
        r.booking_update("1", "L5.D.187", 2, day=past)
    hint = (caught.value.hint or "").lower()
    assert "already passed" in hint
    assert "today" not in hint


# -- what the calls send ---------------------------------------------------

def test_booking_create_never_sends_finish():
    """§8: the server computes `finish`. Sending one was never needed, and
    the field being absent is load-bearing enough to pin."""
    r = Recorder({"result": True, "info": {"bkid": "1"}})
    r.booking_create(1787601600, "L5.D.187", 2)
    _, path, body = r.calls[0]
    assert path == "booking-create"
    assert "finish" not in body
    assert body == {"type": "advance", "start": 1787601600,
                    "key": "L5.D.187", "cid": 2}


def test_booking_list_is_called_bare():
    """0b: the bare call returns the same records as `?bktype=advance`,
    while `repeat` and `adhoc` return nothing."""
    r = Recorder({"result": True, "info": []})
    r.booking_list()
    assert r.calls == [("GET", "booking-list", None)]


def test_booking_summary_asks_for_the_capped_window():
    r = Recorder({"result": True, "info": {"days": []}})
    r.booking_summary()
    _, _, params = r.calls[0]
    assert params["days"] == 15
    assert params["friend"] == "true"


def test_floorplan_booking_sends_the_date_in_the_odd_format():
    """The one parameter in this API that isn't a unix timestamp (§5)."""
    r = Recorder({"result": True, "info": {"desks": []}})
    r.floorplan_booking(3, dt.date(2026, 8, 24), 100, 200)
    _, _, params = r.calls[0]
    assert params["date"] == "24/08/2026"
    assert params["start"] == 100 and params["finish"] == 200


def test_friend_calls_use_the_apis_vocabulary_not_the_uis():
    """The UI says "following"; the API says "friend" (§5.2)."""
    r = Recorder({"result": True, "info": {}})
    r.friend_create("123")
    r.friend_delete("123")
    assert [c[1] for c in r.calls] == ["friend-create", "friend-delete"]
    assert all(c[2] == {"target": "123"} for c in r.calls)


# -- the start timestamp ---------------------------------------------------

def test_book_start_is_local_midnight_plus_book_day_start():
    """§8: stored verbatim, so it must be exact."""
    start = api.book_start_for(dt.date(2026, 8, 25), 480)
    when = dt.datetime.fromtimestamp(start)
    assert (when.hour, when.minute) == (8, 0)
    assert when.date() == dt.date(2026, 8, 25)


def test_a_missing_book_day_start_refuses_to_guess():
    """Guessing 480 would work on this org and fail silently on another --
    and 'silently' means every booking refused with a duration error."""
    with pytest.raises(CommError):
        api.book_start_for(dt.date(2026, 8, 25), None)


def test_records_does_not_flatten_the_keyed_dicts():
    """§5.1 warns that `floorplan-booking.bookings` is keyed by desk key and
    `userbookings` by uid. A flatten-everything helper turns both into
    plausible nonsense, so `records` deliberately does not recurse."""
    assert api.records([{"a": 1}, "junk"]) == [{"a": 1}]
    keyed = {"L5.D.28": [{"bkid": "1"}], "L5.D.29": [{"bkid": "2"}]}
    assert api.records(keyed) == [keyed]
