"""Okta login and the Floorsense session exchange. No browser anywhere.

This module is a transcription, not a design. The chain it walks is
`okta-auth-manual.md` §6 Part A (password + push MFA + number-matching) into
`floorsense-api-manual.md` §4.3 (the browser-free Floorsense exchange). Both
were established by live experiment and both have already cost this project a
wrong turn; nothing here re-derives them.

The four traps encoded here, each of which has bitten before:

  * `prompt=none` on `/oauth2/v1/authorize`. Without it Okta returns 200 and
    the Sign-In Widget's HTML, which completes SSO in JavaScript -- and a
    ~300MB Playwright dependency got built around that before someone tried
    the one parameter that removes it (`okta-auth-manual.md` §3).
  * The CSRF token is a HEADER on `/app/config` and a FORM FIELD on
    `POST /app/login`. Same token, one endpoint apart (§4.2).
  * The poll URL is `_links.next` (whose *name* is "poll"), not
    `_links.poll` -- which matches nothing and silently falls through to a
    default that happens to work (`okta-auth-manual.md` §2).
  * Success is asserted on the landing URL AND the cookies, never cookie
    names alone: `/app/login` sets a pre-auth `id` cookie, so name-only
    checks are a known false positive (§4.4).
"""

import getpass
import re
import time
from urllib.parse import quote, urlparse

import keyring
import requests

from .errors import AuthFailed, CommError, InvalidCredentials, LoginRequired

__all__ = ["KEYCHAIN_SERVICE", "get_password", "store_password",
           "has_stored_password", "forget_password", "login_with_push_mfa",
           "floorsense_login", "discover_okta_org", "scrape_csrf",
           "okta_origin", "cookies_for", "FLOORSENSE_ORIGIN",
           "FLOORSENSE_HOST"]

KEYCHAIN_SERVICE = "floorsense-okta"

FLOORSENSE_ORIGIN = "https://my.floorsense.nz"
FLOORSENSE_HOST = "my.floorsense.nz"

POLL_INTERVAL_S = 2
POLL_TIMEOUT_S = 90

# Browser-ish headers on the APPLICATION's endpoints: content negotiation,
# getting HTML where HTML is expected. The Okta calls below deliberately keep
# the honest `python-requests` UA -- spoofing it there would be detection
# evasion, not scripting (`okta-auth-manual.md` §7).
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/151.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
}

_JSON_HEADERS = {"Accept": "application/json",
                 "Content-Type": "application/json"}


# --------------------------------------------------------------------------
# password storage
# --------------------------------------------------------------------------

def okta_origin(org_host):
    """Config stores a bare host; every call needs an origin. Vanity domains
    (`login.company.com` CNAME'd to Okta) are used verbatim -- the API paths
    are identical and the cookies are set on that domain."""
    if "://" in org_host:
        return org_host.rstrip("/")
    return f"https://{org_host}"


def cookies_for(session, origin):
    """The cookies belonging to one origin.

    Matching is on hostname, not on a hardcoded domain substring, so the same
    code works against a vanity Okta domain and against a test server. A
    cookie with no domain set belongs to the host that issued it.
    """
    host = urlparse(origin).hostname or ""
    out = {}
    for c in session.cookies:
        domain = (c.domain or "").lstrip(".")
        if not domain or domain == host or host.endswith("." + domain):
            out[c.name] = c.value
    return out


def _account_key(username, origin=FLOORSENSE_ORIGIN):
    """Scope the keychain account by Floorsense origin, so `--url` pointing
    at a second deployment can't collide with the default one.

    Keyed on `username` alone would mean two different Floorsense
    deployments that happen to share an Okta local part (e.g. the same
    person, or two people with the same local part, at the default org and
    at a `--url`-overridden one) silently read and overwrite each other's
    password. The default origin keeps the bare username as its key so
    existing keychain entries stay valid; only a non-default origin gets the
    host prefixed on.
    """
    origin = origin or FLOORSENSE_ORIGIN
    if origin == FLOORSENSE_ORIGIN:
        return username
    host = urlparse(origin).hostname or origin
    return f"{host}:{username}"


def _read_password(account_key):
    """`keyring.get_password`, treating "no backend available" the same as
    "nothing stored" rather than crashing.

    A machine with no OS keychain provider reachable (headless Linux with no
    Secret Service/D-Bus session, e.g. a CI runner) makes every backend
    non-viable, and `keyring` raises `NoKeyringError` from `get_password`
    instead of returning `None` the way "account not found" does. Read
    paths -- `fs status` in particular, which must never crash -- can't tell
    those two states apart anyway, so both mean "not stored" here. Writes
    (`store_password`) deliberately do NOT get this treatment: an explicit
    `--save-password` with nowhere to put it is a real failure the user
    should see, not one to swallow.
    """
    try:
        return keyring.get_password(KEYCHAIN_SERVICE, account_key)
    except keyring.errors.NoKeyringError:
        return None


def has_stored_password(username, aliases=(), origin=FLOORSENSE_ORIGIN):
    for account in (username, *aliases):
        if account and _read_password(_account_key(account, origin)):
            return True
    return False


def get_password(username, aliases=(), prompt=getpass.getpass,
                 origin=FLOORSENSE_ORIGIN):
    """Keychain first, prompt only if it isn't there.

    This is the DEFAULT (no `--save-password`) path only --
    `cli.py`'s `make_password_provider` calls this exact function when
    `--save-password` isn't given, and bypasses it entirely (always
    prompting instead) when it is.

    `aliases` exists because keychain entries are keyed by username: someone
    who first stored the password under their email would otherwise silently
    stop being found after switching to the Okta login (§2). `origin` scopes
    the lookup the same way `store_password` scopes the write -- see
    `_account_key`.
    """
    for account in (username, *aliases):
        if not account:
            continue
        existing = _read_password(_account_key(account, origin))
        if existing:
            return existing
    return prompt(f"Okta password for {username}: ")


def store_password(username, password, origin=FLOORSENSE_ORIGIN):
    keyring.set_password(KEYCHAIN_SERVICE, _account_key(username, origin),
                         password)


def forget_password(username, aliases=(), origin=FLOORSENSE_ORIGIN):
    for account in (username, *aliases):
        if not account:
            continue
        try:
            keyring.delete_password(KEYCHAIN_SERVICE,
                                    _account_key(account, origin))
        except (keyring.errors.PasswordDeleteError,
                keyring.errors.NoKeyringError):
            # PasswordDeleteError: nothing was stored under this account --
            # already the state we wanted. NoKeyringError: no backend to
            # delete from means nothing is stored anywhere reachable either
            # -- same as `_read_password`'s reasoning, this is a forget, not
            # a save, so there's nothing for the user to act on.
            pass


# --------------------------------------------------------------------------
# Okta: password + push MFA  (okta-auth-manual.md §2, §6 Part A)
# --------------------------------------------------------------------------

def extract_challenge_number(body):
    """Number-matching lives at `_embedded.factor._embedded.challenge
    .correctAnswer`.

    Two traps, both hit before: the nested `_embedded` (an earlier guess used
    `factor.embedded` and silently extracted nothing, leaving the user
    staring at a device prompt with no number), and the fact that it is NOT
    present on the first poll -- it appears once the device has registered
    the challenge. So callers must check on every iteration.
    """
    factor = (body or {}).get("_embedded", {}).get("factor", {})
    return factor.get("_embedded", {}).get("challenge", {}).get("correctAnswer")


def _authn_error(status, body):
    code = (body or {}).get("errorCode")
    summary = (body or {}).get("errorSummary", "")
    if code == "E0000038":
        # Classic AuthN disabled for the org. Not a credential problem, so
        # not AUTH_FAILED -- there is nothing the user can retype.
        return CommError(
            "this Okta org has disabled the Classic AuthN API, which is the "
            "only path fs can use without a browser",
            hint="This needs an Okta admin, not a different password.")
    if status == 401 or code == "E0000004":
        # E0000004 is generic on purpose: wrong password AND several other
        # rejections share it (§1) -- Okta doesn't distinguish a bad
        # password from a bad username, so neither do we. `InvalidCredentials`,
        # not plain `AuthFailed`: `session.py` forgets a stored password on
        # this specifically, and must be able to tell it apart from
        # `LOCKED_OUT` below, where the password isn't the problem.
        return InvalidCredentials(
            "invalid Okta username or password",
            hint="Run again to be prompted for a new password, or "
                 "`fs --save-password <command>` to store one.")
    return CommError(f"Okta returned {status}: {summary or body}")


def login_with_push_mfa(org, username, password, on_message=print,
                        sleep=time.sleep, timeout_s=POLL_TIMEOUT_S):
    """Password, then push MFA with polling. Returns a one-shot sessionToken.

    The human wait happens here, inside the ~5 min `stateToken` window --
    the only token with slack in it. The `sessionToken` does not exist until
    after the tap, so a slow tap can never burn it (§4, token lifetimes).
    """
    origin = okta_origin(org)
    authn_url = f"{origin}/api/v1/authn"
    try:
        resp = requests.post(authn_url,
                             json={"username": username, "password": password},
                             headers=_JSON_HEADERS, timeout=15)
        body = resp.json()
    except requests.RequestException as e:
        raise CommError(f"could not reach Okta at {origin}: {e}") from e
    except ValueError as e:
        raise CommError(
            f"Okta returned a non-JSON response ({resp.status_code})") from e

    status = body.get("status")
    if resp.status_code != 200:
        raise _authn_error(resp.status_code, body)
    if status == "LOCKED_OUT":
        raise AuthFailed("this Okta account is locked out")
    if status == "SUCCESS":
        # No MFA required. Unexpected on this org, but perfectly valid.
        return body["sessionToken"]
    if status != "MFA_REQUIRED":
        raise LoginRequired(f"Okta returned an unexpected status: {status}")

    state_token = body["stateToken"]
    factors = body.get("_embedded", {}).get("factors", [])
    # Never hardcode factor ids -- they differ per user and change on
    # re-enrollment. Select by type and follow `_links.verify.href` (§2).
    push = next((f for f in factors if f.get("factorType") == "push"), None)
    if push is None:
        raise LoginRequired(
            "no Okta Verify push factor is enrolled on this account",
            hint=f"Factors available: "
                 f"{', '.join(f.get('factorType', '?') for f in factors)}")

    verify_url = push["_links"]["verify"]["href"]
    body = requests.post(verify_url, json={"stateToken": state_token},
                         headers=_JSON_HEADERS, timeout=15).json()

    # `_links.next` -- whose *name* is "poll". There is no `_links.poll`
    # key; code that looks one up matches nothing and silently uses whatever
    # default it was handed (§2 step 2).
    poll_url = (body.get("_links", {}).get("next", {}).get("href")
                or verify_url)

    shown = False
    number = extract_challenge_number(body)
    if number is not None:
        on_message(f'Tap {number} on your device.')
        shown = True
    else:
        on_message('Tap "Yes It\'s Me" on your device.')

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        body = requests.post(poll_url, json={"stateToken": state_token},
                             headers=_JSON_HEADERS, timeout=15).json()
        status = body.get("status")

        if not shown:
            # Check EVERY poll: the number is absent from the first response
            # and appears once the device registers the challenge. It also
            # may never appear at all -- number-matching tracks the Okta
            # Verify device's state, not this client's -- so never block
            # waiting for it.
            number = extract_challenge_number(body)
            if number is not None:
                on_message(f"Tap {number} on your device.")
                shown = True

        if status == "SUCCESS":
            return body["sessionToken"]
        if status == "MFA_CHALLENGE":
            sleep(POLL_INTERVAL_S)
            continue
        if status == "REJECTED":
            raise LoginRequired("the push was declined on the device")
        if status == "TIMEOUT":
            raise LoginRequired("the push expired before it was approved")
        raise LoginRequired(f"MFA ended with status {status!r}")

    raise LoginRequired(f"no MFA approval within {timeout_s}s")


# --------------------------------------------------------------------------
# Floorsense: sessionToken -> app session  (floorsense-api-manual.md §4.3)
# --------------------------------------------------------------------------

def scrape_csrf(html):
    m = re.search(r'<meta name="csrf" content="([^"]+)"', html or "")
    return m.group(1) if m else None


def _mint_authorize_url(app_username, origin):
    """Steps 2-4 of the Floorsense login chain (`floorsense-api-manual.md`
    §4.1/§4.3): CSRF token off `/app/login`'s HTML, the config lookup, then
    POST `/app/login` to mint a fresh OIDC authorize URL -- returned as the
    redirect `Location` header, unfollowed. `password` is sent empty and
    Okta is never contacted here; Floorsense only needs to know who you
    claim to be so it can set `login_hint`. Shared by `floorsense_login`
    (which keeps the session and follows the URL into the real Okta
    exchange) and `discover_okta_org` (which only wants the org host off
    the redirect and discards the session).
    """
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)

    # 2. CSRF token from the login page.
    csrf = scrape_csrf(s.get(f"{origin}/app/login", timeout=30).text)
    if not csrf:
        raise CommError("no csrf meta tag on /app/login",
                        hint="Got a redirect or an error page instead (§4.6).")

    # 3. The real flow's config lookup. Token as a HEADER here.
    s.get(f"{origin}/app/config", params={"username": app_username},
          headers={"x-csrf-token": csrf, "X-Requested-With": "XMLHttpRequest",
                   "Referer": f"{origin}/app/login"}, timeout=30)

    # 4. Mint a FRESH authorize URL. Token as a FORM FIELD here -- sending it
    #    as a header returns an HTML "CSRF Check Failed" page, not a 302.
    #    `state` and `code_challenge` are per-attempt and stored server-side
    #    against this session's pre-auth `id` cookie, so a captured or
    #    replayed URL is dead on arrival.
    resp = s.post(f"{origin}/app/login",
                  data={"method": "oidc", "captchatoken": "",
                        "csrftoken": csrf, "username": app_username,
                        "password": "", "remember": "1"},
                  headers={"Origin": origin, "Referer": f"{origin}/app/login",
                           "Content-Type": "application/x-www-form-urlencoded"},
                  allow_redirects=False, timeout=30)
    return s, resp.headers.get("location", ""), resp.status_code


def discover_okta_org(email, origin=FLOORSENSE_ORIGIN):
    """Learn which Okta org this Floorsense deployment delegates auth to,
    from nothing but the login email -- no `okta_org` needs to be known or
    typed up front, which most users would not recognise anyway (it isn't
    derivable from the email domain: `example.com` mail, but
    `example-corp.okta.com` for Okta -- confirmed live 2026-08-21).

    A throwaway, unauthenticated replay of `_mint_authorize_url`: mint a
    CSRF token, POST `/app/login` with the email and no password, and read
    the org straight off the redirect's `Location` header rather than
    following it anywhere -- no MFA, no session created, safe to call as
    often as needed. The session it builds is discarded; the real login
    (`floorsense_login`) mints its own fresh state/PKCE pair and cannot
    reuse this one (`floorsense-api-manual.md` §4.3's "steps 2->5 must run
    uninterrupted").
    """
    _, authorize_url, status = _mint_authorize_url(email, origin)
    host = urlparse(authorize_url).netloc
    if not host:
        raise CommError(
            f"/app/login did not redirect to an Okta org ({status})",
            hint="Check this is the email you log into Floorsense with.")
    return host


def floorsense_login(app_username, session_token, org,
                     origin=FLOORSENSE_ORIGIN):
    """Turn a one-shot Okta sessionToken into an authenticated Floorsense
    session. `floorsense-api-manual.md` §4.3, transcribed.

    Steps 2->5 must run uninterrupted: no prompts, no sleeps. The
    sessionToken is one-shot and the authorize URL is per-attempt.
    """
    okta = okta_origin(org)
    okta_host = urlparse(okta).netloc

    s, authorize_url, status = _mint_authorize_url(app_username, origin)
    # Compared against the configured Okta host, not the substring
    # "okta.com": vanity domains are real and would fail that check.
    if urlparse(authorize_url).netloc != okta_host:
        raise CommError(
            f"/app/login did not redirect to Okta ({status})",
            hint="Wrong body encoding, or the CSRF token sent as a header "
                 "rather than a csrftoken field (§4.6).")

    # 5. Redeem the one-shot sessionToken for a reusable Okta session.
    #    safe='' matters: redirectUrl's own : and / must be escaped too.
    s.get(f"{okta}/login/sessionCookieRedirect"
          f"?token={quote(session_token)}"
          f"&redirectUrl={quote(authorize_url, safe='')}",
          allow_redirects=False, timeout=30)
    if not any(c.name == "sid" for c in s.cookies):
        raise LoginRequired(
            "Okta did not accept the session token",
            hint="It is one-shot -- it was already used or has expired.")

    # 6. Completes the SSO hop over plain HTTP via `prompt=none`.
    resp = s.get(authorize_url + "&prompt=none", allow_redirects=True,
                 timeout=30)
    if "error=login_required" in (resp.url or ""):
        raise LoginRequired("Okta reported no usable session (login_required)")

    # 7. Landing URL AND cookies. Neither alone -- /app/login sets a pre-auth
    #    `id` cookie, so cookie names are a known false positive (§4.4).
    names = set(cookies_for(s, origin))
    if urlparse(resp.url).netloc != urlparse(origin).netloc:
        raise CommError(f"login did not complete -- ended at {resp.url}")
    if not {"id", "MYSLSRV"} <= names:
        raise CommError(
            f"login landed on {resp.url} but without a session cookie pair",
            hint="Usually a stale or replayed authorize URL (§4.6).")

    return s
