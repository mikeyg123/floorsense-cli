"""The login chain, replayed end to end against a real loopback server.

Every §4.6 failure mode gets a test, and so does every trap the manuals
record -- because each of those was, at some point, real. A test that fails
if someone "tidies" the code back into the broken shape is the only durable
form these findings have.

No network: the fixture server is on 127.0.0.1.
"""

import pytest

from fs_cli.auth import (discover_okta_org, extract_challenge_number,
                         floorsense_login, login_with_push_mfa, okta_origin,
                         scrape_csrf)
from fs_cli.errors import AuthFailed, CommError, InvalidCredentials, LoginRequired

from conftest import PREAUTH_CSRF, REAL_ID


def push(stack, **kw):
    return login_with_push_mfa(stack.org, "jamie.baker", "pw",
                               on_message=kw.pop("on_message",
                                                 lambda _: None),
                               sleep=lambda _: None, **kw)


def full_login(stack, token="sess-xyz"):
    return floorsense_login("jamie.baker@example.com", token, stack.org,
                            origin=stack.origin)


# --- origins ---------------------------------------------------------------

def test_okta_origin_from_a_bare_host():
    assert okta_origin("example-corp.okta.com") == \
        "https://example-corp.okta.com"


def test_okta_origin_leaves_a_url_alone():
    # Vanity domains (`login.company.com` CNAME'd to Okta) are real, and a
    # substring check for "okta.com" would reject them.
    assert okta_origin("https://login.company.com") == \
        "https://login.company.com"


# --- number matching -------------------------------------------------------

def test_challenge_number_needs_the_nested_embedded():
    body = {"_embedded": {"factor": {"_embedded":
            {"challenge": {"correctAnswer": 23}}}}}
    assert extract_challenge_number(body) == 23


def test_the_shallow_path_finds_nothing():
    # The bug that once left a user staring at a device prompt with no number:
    # `factor.embedded` instead of `factor._embedded`.
    shallow = {"_embedded": {"factor": {"embedded":
               {"challenge": {"correctAnswer": 23}}}}}
    assert extract_challenge_number(shallow) is None
    assert extract_challenge_number({}) is None
    assert extract_challenge_number(None) is None


# --- push MFA --------------------------------------------------------------

def test_successful_push_returns_the_session_token(stack):
    stack.state["poll"] = ["MFA_CHALLENGE", "MFA_CHALLENGE", "SUCCESS"]
    assert push(stack) == "sess-xyz"


def test_the_push_factor_is_selected_by_type_not_position(stack):
    # TOTP is listed first. Hardcoding an index or an id would challenge the
    # wrong factor -- and ids differ per user and change on re-enrollment.
    push(stack)
    assert stack.find("POST", "/api/v1/authn/factors/opfw123/verify")
    assert not stack.find("POST", "/api/v1/authn/factors/ostw1/verify")


def test_the_number_is_shown_when_it_appears_on_a_later_poll(stack):
    # It is absent from the first response and appears once the device has
    # registered the challenge. Code that checks once never sees it.
    stack.state["poll"] = ["MFA_CHALLENGE"] * 3 + ["SUCCESS"]
    stack.state["challenge_number"] = 42
    stack.state["challenge_number_after"] = 2
    said = []
    push(stack, on_message=said.append)
    assert any("42" in s for s in said), said


def test_a_plain_push_never_blocks_waiting_for_a_number(stack):
    # Number-matching tracks the Okta Verify DEVICE's state, not this
    # client's, so it may never arrive at all.
    said = []
    push(stack, on_message=said.append)
    assert any("Yes It's Me" in s for s in said), said
    assert push.__doc__ is None      # (keeps linters from folding this away)


def test_wrong_password_is_invalid_credentials(stack):
    stack.state["authn"] = ("ERROR", 401)
    # InvalidCredentials specifically, not just its AuthFailed base --
    # session.py's forget-on-failure logic keys off this distinction (it
    # must NOT fire for test_locked_out_is_auth_failed below).
    with pytest.raises(InvalidCredentials) as e:
        push(stack)
    # E0000004 is generic on purpose -- the message must not over-diagnose.
    assert "username" in str(e.value)


def test_classic_authn_disabled_is_not_reported_as_a_bad_password(stack):
    # E0000038 means an admin turned the API off. Telling the user to retype
    # their password sends them somewhere that cannot help.
    stack.state["authn"] = ("DISABLED", 403)
    with pytest.raises(CommError) as e:
        push(stack)
    assert "admin" in str(e.value.hint)


def test_locked_out_is_auth_failed_but_not_invalid_credentials(stack):
    # The password may be perfectly fine here -- an account lockout is not
    # a credential problem, so this must NOT be `InvalidCredentials`
    # (session.py forgets a stored password on that specifically).
    stack.state["authn"] = ("LOCKED_OUT", 200)
    with pytest.raises(AuthFailed) as e:
        push(stack)
    assert not isinstance(e.value, InvalidCredentials)


@pytest.mark.parametrize("status", ["REJECTED", "TIMEOUT"])
def test_declined_or_expired_push_is_login_required(stack, status):
    stack.state["poll"] = ["MFA_CHALLENGE", status]
    with pytest.raises(LoginRequired):
        push(stack)


def test_mfa_polling_times_out(stack):
    stack.state["poll"] = ["MFA_CHALLENGE"]
    with pytest.raises(LoginRequired):
        push(stack, timeout_s=0)


def test_no_mfa_required_still_yields_a_token(stack):
    stack.state["authn"] = ("SUCCESS", 200)
    assert push(stack) == "sess-direct"


# --- the Floorsense exchange (§4.3) ----------------------------------------

def test_the_full_chain_lands_authenticated(stack):
    s = full_login(stack)
    names = {c.name for c in s.cookies}
    assert {"id", "MYSLSRV"} <= names


def test_the_id_cookie_is_kept_verbatim(stack):
    # An Express signed cookie already containing percent-encoding. Re-encode
    # it and the server can't verify it, giving "not logged in" -- which is
    # indistinguishable from expiry (§4.5).
    s = full_login(stack)
    assert s.cookies.get("id") == REAL_ID
    assert "%3A" in s.cookies.get("id"), "the s%3A prefix was mangled"


def test_the_csrf_token_is_a_form_field_on_the_login_post(stack):
    # Same token, two deliveries, one endpoint apart. As a header here it
    # returns an HTML "CSRF Check Failed" page rather than a 302 (§4.2).
    full_login(stack)
    post = stack.find("POST", "/app/login")[0]
    assert f"csrftoken={PREAUTH_CSRF}" in post["body"]
    assert "x-csrf-token" not in post["headers"]


def test_the_csrf_token_is_a_header_on_config(stack):
    full_login(stack)
    cfg = stack.find("GET", "/app/config")[0]
    assert cfg["headers"]["x-csrf-token"] == PREAUTH_CSRF


def test_login_post_is_form_encoded_not_json(stack):
    full_login(stack)
    post = stack.find("POST", "/app/login")[0]
    assert post["headers"]["content-type"] == \
        "application/x-www-form-urlencoded"


def test_prompt_none_is_appended(stack):
    # Without it Okta returns 200 and the Sign-In Widget's HTML, which
    # completes SSO in JavaScript -- the observation a ~300MB Playwright
    # dependency was once built around (`okta-auth-manual.md` §3).
    full_login(stack)
    authorize = stack.find("GET", "/oauth2/v1/authorize")[0]
    assert authorize["query"]["prompt"] == ["none"]


def test_without_prompt_none_the_chain_would_dead_end(stack):
    # Proves the fixture reproduces the real symptom, so the test above is
    # testing something rather than passing vacuously.
    import requests
    r = requests.get(f"{stack.origin}/oauth2/v1/authorize?client_id=x")
    assert r.status_code == 200 and "okta-sign-in" in r.text


def test_the_authorize_url_is_minted_fresh_each_login(stack):
    # `state` and `code_challenge` are per-attempt and stored server-side
    # against the pre-auth `id` cookie. A replayed URL is dead on arrival.
    full_login(stack)
    assert len(stack.find("POST", "/app/login")) == 1
    assert stack.find("GET", "/oauth2/v1/authorize")


def test_missing_csrf_meta_tag_is_reported_clearly(stack):
    stack.state["csrf_meta"] = False
    with pytest.raises(CommError) as e:
        full_login(stack)
    assert "csrf" in str(e.value).lower()


def test_login_post_returning_html_instead_of_a_302(stack):
    stack.state["login_post_redirects"] = False
    with pytest.raises(CommError) as e:
        full_login(stack)
    assert "did not redirect" in str(e.value)
    assert "csrftoken field" in str(e.value.hint)


def test_a_spent_session_token_sets_no_sid(stack):
    stack.state["session_token_valid"] = False
    with pytest.raises(LoginRequired) as e:
        full_login(stack, token="already-used")
    assert "one-shot" in str(e.value.hint)


def test_login_required_from_prompt_none(stack):
    # prompt=none working as designed: no usable Okta session yields an
    # explicit error rather than a login page you must sniff for.
    stack.state["prompt_none_works"] = False
    with pytest.raises(LoginRequired):
        full_login(stack)


def test_landing_on_the_login_page_is_not_success(stack):
    # Cookie names alone are a known false positive: /app/login sets a
    # pre-auth `id`, and an early version of this check declared victory on a
    # run still sitting on the Okta widget (§4.4).
    stack.state["callback_lands_on"] = "/app/login"
    with pytest.raises(CommError) as e:
        full_login(stack)
    assert "session cookie pair" in str(e.value)


# --- org discovery (2026-08-21: cli.first_run stopped asking for this) ----
#
# A throwaway, unauthenticated replay of the login page's own lookup --
# `_mint_authorize_url` shared with `floorsense_login` above. Confirmed live
# against the real account (PLAN.md) before being wired into first_run.

def test_discover_okta_org_reads_the_redirect_host(stack):
    from urllib.parse import urlparse
    host = urlparse(stack.origin).netloc
    assert discover_okta_org("jamie.baker@example.com",
                             origin=stack.origin) == host


def test_discover_okta_org_sends_no_password(stack):
    from urllib.parse import parse_qs
    discover_okta_org("jamie.baker@example.com", origin=stack.origin)
    post = stack.find("POST", "/app/login")[0]
    assert parse_qs(post["body"], keep_blank_values=True)["password"] == [""]


def test_discover_okta_org_uses_the_email_as_login_hint(stack):
    from urllib.parse import parse_qs
    discover_okta_org("jamie.baker@example.com", origin=stack.origin)
    post = stack.find("POST", "/app/login")[0]
    assert parse_qs(post["body"])["username"] == ["jamie.baker@example.com"]


def test_discover_okta_org_raises_when_login_post_does_not_redirect(stack):
    # Same failure mode as `floorsense_login` hitting a CSRF/encoding
    # problem -- discovery shares the same POST and should surface the
    # same way rather than a confusing "no org found".
    stack.state["login_post_redirects"] = False
    with pytest.raises(CommError) as e:
        discover_okta_org("jamie.baker@example.com", origin=stack.origin)
    assert "did not redirect" in str(e.value)


def test_scrape_csrf_handles_junk():
    assert scrape_csrf('<meta name="csrf" content="abc">') == "abc"
    assert scrape_csrf("<html>nothing</html>") is None
    assert scrape_csrf("") is None
    assert scrape_csrf(None) is None
