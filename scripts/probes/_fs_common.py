"""Shared Floorsense helpers for the Phase 0 capture scripts (07, 08).

Login is lifted verbatim from `floorsense-api-manual.md` §4.3 -- the
confirmed browser-free chain. Nothing here re-derives it.

Adds three things the manual's snippet doesn't cover, because both Phase 0
scripts need them:

  * a session cache, so 07 and 08 run back-to-back on ONE MFA tap
    (§3: the session is good for ~60-70 min from login, absolute);
  * `api_get`/`api_post`, which refuse to `.json()` an HTML body
    (§7: the CSRF failure page is HTML and throws on parse);
  * `write_fixture`, which stores captures verbatim (Phase 0b).
"""

import json
import os
import pathlib
import re
import time
from urllib.parse import quote, urlparse

import requests

from _okta_common import ORG_HOST, get_password, login_with_push_mfa

ORIGIN = "https://my.floorsense.nz"
HOST = "my.floorsense.nz"

HERE = pathlib.Path(__file__).parent
SESSION_CACHE = HERE / ".fs_session.json"

# The absolute cap is ~60-70 min (§3). 50 leaves room to actually do work
# before it dies mid-script.
SESSION_MAX_AGE_S = 50 * 60

# Browser-ish headers on the APPLICATION's endpoints only -- content
# negotiation, getting HTML where HTML is expected. The Okta calls in
# _okta_common.py deliberately keep the honest `python-requests` UA
# (`okta-auth-manual.md` §7, "Don't spoof the User-Agent").
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/151.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
}


# --------------------------------------------------------------------------
# login (floorsense-api-manual.md §4.3, verbatim in substance)
# --------------------------------------------------------------------------

def scrape_csrf(html):
    m = re.search(r'<meta name="csrf" content="([^"]+)"', html)
    return m.group(1) if m else None


def login(app_username, okta_username, password):
    """Returns an authenticated requests.Session. Raises on failure.

    app_username:  the email Floorsense's form wants (login_hint only).
    okta_username: the Okta login -- NOT necessarily the same (§4.2).
    """
    # 1. Okta password + push MFA. The human tap happens here, while only the
    #    ~5 min stateToken is ticking; the one-shot sessionToken does not
    #    exist yet, so a slow tap cannot burn it.
    session_token = login_with_push_mfa(okta_username, password)

    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)

    # 2. Scrape the CSRF token from the login page.
    csrf = scrape_csrf(s.get(f"{ORIGIN}/app/login", timeout=30).text)
    if not csrf:
        raise RuntimeError("no csrf meta tag on /app/login")

    # 3. Mimic the real flow's config lookup (token as a HEADER here).
    s.get(f"{ORIGIN}/app/config", params={"username": app_username},
          headers={"x-csrf-token": csrf, "X-Requested-With": "XMLHttpRequest",
                   "Referer": f"{ORIGIN}/app/login"}, timeout=30)

    # 4. Trigger SSO to mint a FRESH authorize URL (token as a FIELD here).
    #    state + code_challenge are stored server-side against this session's
    #    pre-auth `id` cookie, so it must be the same session.
    resp = s.post(
        f"{ORIGIN}/app/login",
        data={"method": "oidc", "captchatoken": "", "csrftoken": csrf,
              "username": app_username, "password": "", "remember": "1"},
        headers={"Origin": ORIGIN, "Referer": f"{ORIGIN}/app/login",
                 "Content-Type": "application/x-www-form-urlencoded"},
        allow_redirects=False, timeout=30,
    )
    authorize_url = resp.headers.get("location", "")
    if "okta.com" not in authorize_url:
        raise RuntimeError(f"no Okta redirect from /app/login: "
                           f"{resp.status_code} {resp.text[:300]}")

    # 5. Redeem the one-shot sessionToken for a reusable Okta session.
    #    quote(..., safe='') matters: redirectUrl's own : and / must be
    #    escaped or Okta won't round-trip it.
    s.get(f"https://{ORG_HOST}/login/sessionCookieRedirect"
          f"?token={quote(session_token)}"
          f"&redirectUrl={quote(authorize_url, safe='')}",
          allow_redirects=False, timeout=30)
    if not any(c.name == "sid" for c in s.cookies):
        raise RuntimeError("Okta rejected the sessionToken (no sid cookie)")

    # 6. The hop that used to need Chromium. prompt=none forces a redirect
    #    instead of the Sign-In Widget, so there is no JS to execute.
    resp = s.get(authorize_url + "&prompt=none", allow_redirects=True,
                 timeout=30)

    # 7. Verify: landing URL *and* cookies. Neither alone (§4.4).
    names = {c.name for c in s.cookies if HOST in (c.domain or "")}
    if urlparse(resp.url).netloc != HOST or not {"id", "MYSLSRV"} <= names:
        raise RuntimeError(f"login did not complete -- ended at {resp.url}")

    return s


# --------------------------------------------------------------------------
# session cache -- one MFA tap covers both Phase 0 scripts
# --------------------------------------------------------------------------

def _fs_cookies(s):
    return {c.name: c.value for c in s.cookies if HOST in (c.domain or "")}


def save_session(s):
    """Cache the cookie pair. It is a live authenticated session (§11), so
    0600, and the file is git-ignored by virtue of this dir not being a repo."""
    payload = {"created": int(time.time()), "cookies": _fs_cookies(s)}
    SESSION_CACHE.write_text(json.dumps(payload))
    os.chmod(SESSION_CACHE, 0o600)


def _restore(payload):
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    for name, value in payload["cookies"].items():
        # Verbatim. `id` is an Express signed cookie already containing
        # percent-encoding (`s%3A...`); re-encoding it yields "not logged in"
        # and looks exactly like expiry (§4.5).
        s.cookies.set(name, value, domain=HOST, path="/")
    return s


def probe(s):
    """Is this session actually live? Returns the fresh CSRF token or None.

    Asserts on the landing URL, not on cookie names -- cookie names alone are
    a known false positive (§4.4). /app/ redirects to /app/site when
    authenticated and /app/login when not; both carry a csrf meta tag, so the
    token alone proves nothing either.
    """
    try:
        resp = s.get(f"{ORIGIN}/app/", timeout=30)
    except requests.RequestException:
        return None
    if not resp.url.rstrip("/").endswith("/app/site"):
        return None
    return scrape_csrf(resp.text)


def get_session(app_username, okta_username, save_to_keychain=False,
                fresh=False):
    """Returns (session, csrf). Reuses the cached cookie pair when it is
    young enough and still live; otherwise logs in (one MFA tap)."""
    if not fresh and SESSION_CACHE.exists():
        try:
            payload = json.loads(SESSION_CACHE.read_text())
        except (ValueError, OSError):
            payload = None
        if payload:
            age = int(time.time()) - payload.get("created", 0)
            if age < SESSION_MAX_AGE_S:
                s = _restore(payload)
                csrf = probe(s)
                if csrf:
                    print(f"Reusing cached session ({age // 60} min old) "
                          f"-- no MFA tap needed.")
                    s.headers["x-csrf-token"] = csrf
                    return s, csrf
                print("Cached session is dead; logging in again.")
            else:
                print(f"Cached session is {age // 60} min old "
                      f"(cap is ~60-70 min); logging in again.")

    print(f"Okta login: {okta_username!r} | "
          f"Floorsense login_hint: {app_username!r}")
    password = get_password(okta_username, save_to_keychain,
                            aliases=(app_username,))
    print('Logging in via Okta -- tap "Yes, it\'s me" on your device '
          "when the push arrives.")
    s = login(app_username, okta_username, password)

    # CSRF is session-scoped and login replaced the session, so the token
    # scraped during login is stale. Re-scrape against the authenticated one
    # (§4.5) -- reusing the pre-auth token yields the HTML CSRF page.
    csrf = probe(s)
    if not csrf:
        raise RuntimeError("logged in but /app/ did not land on /app/site")
    s.headers["x-csrf-token"] = csrf
    save_session(s)
    print("Logged in. Session cached for the next script.")
    return s, csrf


# --------------------------------------------------------------------------
# calling the API -- never .json() blindly (§7)
# --------------------------------------------------------------------------

class ApiHtml(Exception):
    """Got an HTML page where JSON was expected -- almost always the CSRF
    failure page (§7), which throws on .json() and looks like a parse bug."""


def _decode(resp):
    ctype = resp.headers.get("content-type", "")
    body = resp.text
    if "json" not in ctype:
        head = body.lstrip()[:400]
        if head.lower().startswith("<html") or "CSRF Check Failed" in body:
            raise ApiHtml(f"HTML instead of JSON from {resp.url}: {head[:200]}")
        raise ApiHtml(f"unexpected content-type {ctype!r} from {resp.url}: "
                      f"{head[:200]}")
    return resp.json()


# The 0a capture shows the web UI sending both of these on every /app/* XHR.
# Most endpoints work without them, but `booking-summary` has only ever been
# observed with them present -- and a missing Referer producing an HTML error
# page would look exactly like the CSRF failure in §7. Cheap insurance.
def _xhr_headers(extra=None):
    headers = {"X-Requested-With": "XMLHttpRequest",
               "Referer": f"{ORIGIN}/app/site"}
    headers.update(extra or {})
    return headers


def api_get(s, path, params=None):
    resp = s.get(f"{ORIGIN}/app/{path.lstrip('/')}", params=params,
                 headers=_xhr_headers(), timeout=30)
    return resp, _decode(resp)


def api_post(s, path, body):
    resp = s.post(f"{ORIGIN}/app/{path.lstrip('/')}", json=body,
                  headers=_xhr_headers({"Content-Type": "application/json"}),
                  timeout=30)
    return resp, _decode(resp)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

def write_fixture(path, request_desc, resp, body):
    """Write one capture verbatim.

    These fixtures deliberately hold **real** data -- real uids, real
    colleagues' names, the real locker record. An earlier version
    pseudonymised all of it; that was dropped by decision of the account
    holder, whose machine this is. Two things it bought back:

      * the test suite asserts against shapes the server really produces,
        with no scrubber sitting between the capture and the assertion --
        one less thing that can quietly lie.

    They stay on this machine: this directory is deliberately not a git repo
    (see `CLAUDE.md`), so there is nothing here that pushes anywhere.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "request": request_desc,
        "status": resp.status_code if resp is not None else None,
        "content_type": (resp.headers.get("content-type")
                         if resp is not None else None),
        "body": body,
        "note": "Captured verbatim by experiments/07_capture_shapes.py. "
                "Real data, not scrubbed. Local to this machine.",
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=False))
    return path
