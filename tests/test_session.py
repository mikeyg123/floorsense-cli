"""Session caching, CSRF handling, and the one-retry rule.

The three traps in `session.py`'s docstring get tests here rather than
comments, because each one produces the SAME symptom -- `{"result": false,
"message": "not logged in"}` -- from three unrelated causes, and that
ambiguity is what made them expensive to diagnose the first time.
"""

import json
import os
import stat
import time

import pytest

from fs_cli.config import Config
from fs_cli.errors import AuthFailed, CommError, InvalidCredentials, LoginRequired
from fs_cli.session import MAX_AGE_S, Session, SessionStore

from conftest import CSRF_TOKEN, REAL_ID


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / "session.json")


def make_session(stack, store, **kw):
    kw.setdefault("password_provider", lambda _u: "pw")
    kw.setdefault("on_message", lambda _m: None)
    config = Config(okta_user="jamie.baker", email_domain="example.com",
                    okta_org=stack.org)
    return Session(config, store, origin=stack.origin, **kw)


# --- the store -------------------------------------------------------------

def test_session_file_is_0600_from_the_moment_it_exists(store):
    store.save({"id": REAL_ID, "MYSLSRV": "api-nz-b1"})
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600


def test_store_round_trips_the_cookie_value_untouched(store):
    store.save({"id": REAL_ID})
    assert store.load()["cookies"]["id"] == REAL_ID


def test_missing_or_corrupt_store_loads_as_none(store):
    assert store.load() is None
    store.path.write_text("{ not json")
    assert store.load() is None
    store.path.write_text('{"created": 1}')       # no cookies
    assert store.load() is None


def test_clear_removes_the_file_and_tolerates_absence(store):
    store.save({"id": "x"})
    store.clear()
    assert not store.path.exists()
    store.clear()


# --- logging in ------------------------------------------------------------

def test_a_fresh_login_caches_the_cookie_pair(stack, store):
    s = make_session(stack, store)
    s.ensure()
    cached = store.load()["cookies"]
    assert cached["id"] == REAL_ID
    assert "MYSLSRV" in cached
    assert s.logged_in_this_run is True


def test_the_csrf_token_is_rescraped_after_login(stack, store):
    # The token scraped during login belongs to the PRE-AUTH session, which
    # login replaced. Reusing it yields the HTML CSRF failure page, which
    # throws on .json() and looks like a parsing bug (§4.5).
    from conftest import PREAUTH_CSRF
    s = make_session(stack, store)
    s.ensure()
    assert s.csrf == CSRF_TOKEN
    assert s.csrf != PREAUTH_CSRF


def test_a_cached_session_avoids_a_second_login(stack, store):
    make_session(stack, store).ensure()
    logins_before = len(stack.find("POST", "/api/v1/authn"))

    second = make_session(stack, store)
    second.ensure()

    assert len(stack.find("POST", "/api/v1/authn")) == logins_before
    assert second.logged_in_this_run is False


def test_a_cached_session_past_the_cap_is_not_even_probed(stack, store):
    # The ~60-70 min cap is absolute, not an idle timeout, and no ping
    # extends it (§3). Probing an old pair is a wasted round-trip.
    make_session(stack, store).ensure()
    store.save(store.load()["cookies"], created=time.time() - MAX_AGE_S - 1)
    probes_before = len(stack.find("GET", "/app/"))

    second = make_session(stack, store)
    second.ensure()

    assert second.logged_in_this_run is True
    assert len(stack.find("GET", "/app/")) > probes_before


def test_a_dead_cached_session_falls_back_to_login(stack, store):
    store.save({"id": "s%3ASTALE.sig", "MYSLSRV": "api-nz-b1"})
    s = make_session(stack, store)
    s.ensure()
    assert s.logged_in_this_run is True


# --- forgetting a bad password, storing a confirmed one --------------------

def test_invalid_credentials_calls_on_invalid_credentials(stack, store):
    stack.state["authn"] = ("ERROR", 401)
    called = []
    s = make_session(stack, store, on_invalid_credentials=called.append)
    with pytest.raises(InvalidCredentials):
        s.ensure()
    assert called == ["jamie.baker"]


def test_locked_out_does_not_call_on_invalid_credentials(stack, store):
    # The password may be perfectly fine on a locked-out account -- forgetting
    # it here would strip a working credential from someone who then has
    # neither a stored password nor a way to log in and fix that.
    stack.state["authn"] = ("LOCKED_OUT", 200)
    called = []
    s = make_session(stack, store, on_invalid_credentials=called.append)
    with pytest.raises(AuthFailed):
        s.ensure()
    assert called == []


def test_a_post_password_mfa_failure_does_not_call_on_invalid_credentials(stack, store):
    # The push being declined happens well after Okta accepted the
    # password -- MFA_REQUIRED already proves that. Not a credential
    # problem, so nothing should be forgotten.
    stack.state["poll"] = ["REJECTED"]
    called = []
    s = make_session(stack, store, on_invalid_credentials=called.append)
    with pytest.raises(LoginRequired):
        s.ensure()
    assert called == []


def test_on_login_success_fires_only_after_the_landing_probe(stack, store):
    calls = []
    s = make_session(stack, store,
                     on_login_success=lambda u, p: calls.append((u, p)))
    s.ensure()
    assert calls == [("jamie.baker", "pw")]
    # It ran after the probe confirmed /app/site, not merely after Okta
    # accepted the password -- the probe GET must already have happened.
    assert stack.find("GET", "/app/")


def test_on_login_success_does_not_fire_on_a_failed_login(stack, store):
    stack.state["authn"] = ("ERROR", 401)
    calls = []
    s = make_session(stack, store,
                     on_login_success=lambda u, p: calls.append((u, p)))
    with pytest.raises(InvalidCredentials):
        s.ensure()
    assert calls == []


def test_no_login_flag_refuses_rather_than_prompting(stack, store):
    s = make_session(stack, store, allow_login=False)
    with pytest.raises(LoginRequired) as e:
        s.ensure()
    assert "--no-login" in str(e.value)
    assert not stack.find("POST", "/api/v1/authn"), "it logged in anyway"


# --- force_login (`--save-password` with an already-live session) ---------

def test_force_login_relogs_in_even_with_a_live_cached_session(stack, store):
    # Without force_login, `ensure()` finds the cached session live via
    # `_from_cache()` and never reaches `_fresh_login()` at all -- which is
    # exactly why `--save-password` used to silently do nothing when a
    # session was already cached: `password_provider`/`on_login_success`
    # only ever fire from inside `_fresh_login()`.
    make_session(stack, store).ensure()
    logins_before = len(stack.find("POST", "/api/v1/authn"))

    calls = []
    s = make_session(stack, store, force_login=True,
                     on_login_success=lambda u, p: calls.append((u, p)))
    s.ensure()

    assert len(stack.find("POST", "/api/v1/authn")) > logins_before
    assert s.logged_in_this_run is True
    assert calls == [("jamie.baker", "pw")]


def test_force_login_failure_leaves_the_existing_session_untouched(stack, store):
    # A typo'd password under --save-password must not cost the user their
    # still-good cached session -- `_fresh_login` raises before
    # `self.store.save(...)`, and the cache was never consulted, so the old
    # cookies on disk survive for the next, flagless run.
    make_session(stack, store).ensure()
    cached_before = store.load()["cookies"]

    stack.state["authn"] = ("ERROR", 401)
    s = make_session(stack, store, force_login=True)
    with pytest.raises(InvalidCredentials):
        s.ensure()

    assert store.load()["cookies"] == cached_before


def test_force_login_and_no_login_together_still_refuses(stack, store):
    # `allow_login=False` must win: forcing a fresh login can't override a
    # user-requested refusal to ever log in.
    make_session(stack, store).ensure()
    s = make_session(stack, store, force_login=True, allow_login=False)
    with pytest.raises(LoginRequired):
        s.ensure()


def test_no_okta_user_configured_is_login_required(stack, store):
    # cli.py's first-run gate should already have caught this before a
    # Session is ever built for a live command -- this hint is a
    # defensive fallback. It must not say "run fs status": that command
    # never asks for setup and so can never actually fix this (the
    # original wording, before it was corrected).
    s = Session(Config(okta_org=stack.org), store, origin=stack.origin,
                password_provider=lambda _u: "pw", on_message=lambda _m: None)
    with pytest.raises(LoginRequired) as e:
        s.ensure()
    assert "fs status" not in str(e.value.hint)


def test_is_live_never_triggers_a_login(stack, store):
    s = make_session(stack, store)
    assert s.is_live() is False
    assert not stack.find("POST", "/api/v1/authn")


def test_is_live_is_true_for_a_good_cached_session(stack, store):
    make_session(stack, store).ensure()
    assert make_session(stack, store).is_live() is True


# --- the cookie traps ------------------------------------------------------

def test_both_cookies_are_sent_on_every_api_call(stack, store):
    # MYSLSRV is load-balancer affinity: sessions live in memory on ONE node
    # and this is what routes back to it. A valid `id` without it reaches a
    # node that never heard of the session and answers "not logged in" --
    # an auth-shaped symptom with a routing cause (§3).
    s = make_session(stack, store)
    s.get("booking-list", {"days": 30})
    call = stack.find("GET", "/app/booking-list")[0]
    assert call["cookies"]["id"] == REAL_ID
    assert call["cookies"]["MYSLSRV"] == "api-nz-b1"


def test_the_restored_id_cookie_is_byte_identical(stack, store):
    # Restoring through any URL-encoding path re-encodes the `s%3A` prefix
    # into `s%253A`, producing a cookie the server can't verify.
    make_session(stack, store).ensure()
    second = make_session(stack, store)
    second.get("booking-list")
    call = stack.find("GET", "/app/booking-list")[0]
    assert call["cookies"]["id"] == REAL_ID
    assert "%253A" not in call["cookies"]["id"]


def test_a_session_missing_myslsrv_is_treated_as_dead(stack, store):
    # Proves the fixture reproduces the real failure, so the test above
    # isn't passing vacuously.
    store.save({"id": REAL_ID}, created=time.time())
    s = make_session(stack, store)
    s.ensure()
    assert s.logged_in_this_run is True, \
        "an id-only cookie pair should not have passed the probe"


def test_the_csrf_token_is_sent_as_a_header_on_api_calls(stack, store):
    s = make_session(stack, store)
    s.get("booking-list")
    call = stack.find("GET", "/app/booking-list")[0]
    assert call["headers"]["x-csrf-token"] == CSRF_TOKEN


# --- responses -------------------------------------------------------------

def test_a_normal_call_returns_the_envelope(stack, store):
    stack.state["api"]["booking-list"] = {"result": True, "info": [{"bkid": "1"}]}
    assert make_session(stack, store).get("booking-list")["info"][0]["bkid"] == "1"


def test_post_sends_json(stack, store):
    stack.state["api"]["booking-create"] = {"result": True}
    s = make_session(stack, store)
    s.post("booking-create", {"type": "advance", "key": "L5.D.217A"})
    call = stack.find("POST", "/app/booking-create")[0]
    assert json.loads(call["body"])["key"] == "L5.D.217A"
    assert "json" in call["headers"]["content-type"]


def test_an_html_csrf_page_is_never_parsed_as_json(stack, store):
    # `resp.json()` on this THROWS, surfacing as a parse error rather than
    # the actual problem (§7).
    s = make_session(stack, store)
    s.ensure()
    s._session.headers["x-csrf-token"] = "wrong-token"
    with pytest.raises(CommError) as e:
        s.get("booking-list")
    assert "CSRF" in str(e.value)


# --- the one-retry rule ----------------------------------------------------

def test_a_dead_session_mid_command_triggers_exactly_one_relogin(stack, store):
    s = make_session(stack, store)
    s.ensure()
    logins_before = len(stack.find("POST", "/api/v1/authn"))

    # The next call finds the session gone -- the ~60-70 min cap expiring
    # mid-command is an ordinary event, not an error (§10).
    stack.state["api"]["booking-list"] = [
        {"result": False, "message": "not logged in"},
        {"result": True, "info": ["recovered"]},
    ]
    assert s.get("booking-list")["info"] == ["recovered"]
    assert len(stack.find("POST", "/api/v1/authn")) == logins_before + 1


def test_it_gives_up_after_one_retry_rather_than_looping(stack, store):
    s = make_session(stack, store)
    s.ensure()
    logins_before = len(stack.find("POST", "/api/v1/authn"))
    stack.state["api"]["booking-list"] = [
        {"result": False, "message": "not logged in"},
        {"result": False, "message": "not logged in"},
        {"result": True, "info": ["never reached"]},
    ]
    with pytest.raises(LoginRequired):
        s.get("booking-list")
    # Exactly one: a genuinely rejected credential would otherwise re-prompt
    # forever.
    assert len(stack.find("POST", "/api/v1/authn")) == logins_before + 1


def test_the_stale_cache_is_cleared_before_the_retry(stack, store):
    s = make_session(stack, store)
    s.ensure()
    stack.state["api"]["booking-list"] = [
        {"result": False, "message": "not logged in"},
        {"result": True, "info": []},
    ]
    s.get("booking-list")
    # Whatever is cached now must be the NEW session, not the dead one.
    assert store.load() is not None
    assert s.logged_in_this_run is True


def test_an_ordinary_business_failure_is_not_mistaken_for_session_death(stack, store):
    # `{"result": false}` with a business message must NOT trigger a re-login.
    s = make_session(stack, store)
    s.ensure()
    logins_before = len(stack.find("POST", "/api/v1/authn"))
    stack.state["api"]["booking-create"] = {
        "result": False, "message": "User desk limit reached (Group)"}
    data = s.post("booking-create", {})
    assert data["result"] is False
    assert len(stack.find("POST", "/api/v1/authn")) == logins_before


def test_a_network_failure_is_a_comm_error(stack, store):
    import socket
    s = make_session(stack, store)
    s.ensure()
    # Repoint at a port with nothing behind it. Shutting the fixture down is
    # not enough -- an already-open keep-alive connection still gets served.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
    s.origin = f"http://127.0.0.1:{dead_port}"
    with pytest.raises(CommError) as e:
        s.get("booking-list")
    assert "could not reach Floorsense" in str(e.value)
