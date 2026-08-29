"""Session lifecycle: cache the cookie pair, detect death, re-login once.

`floorsense-api-manual.md` §3 is blunt about the constraint this module exists
to absorb: the session dies **~60-70 minutes after login regardless of
activity**. It is an absolute cap, not an idle timeout, and pinging cannot
extend it -- a fixed-interval experiment showed six clean successes at 10-min
gaps and death on the seventh. So there is no keep-alive here and there never
should be. §10: treat "session probably dead" as a normal state, not an error.

Three traps from the manuals are load-bearing here, and each has a test:

  * the `id` cookie is stored and replayed **verbatim**. It is an Express
    signed cookie already containing percent-encoding (`s%3A...`);
    re-encoding it produces `{"result": false, "message": "not logged in"}`,
    indistinguishable from expiry (§4.5).
  * `MYSLSRV` is sent alongside, always. Sessions live in memory on one
    node and this is what routes back to it; omitting it yields the *same*
    "not logged in" message from a node that never heard of your session
    (§3). An auth-shaped symptom with a routing cause.
  * the CSRF token is re-scraped **after** login, never reused from the
    pre-auth page (§4.5).
"""

import json
import os
import time

import requests

from urllib.parse import urlparse

from .auth import (BROWSER_HEADERS, FLOORSENSE_ORIGIN, cookies_for,
                   floorsense_login, login_with_push_mfa, scrape_csrf)
from .errors import CommError, InvalidCredentials, LoginRequired
from .wire import log_request

__all__ = ["Session", "SessionStore", "NOT_LOGGED_IN"]

NOT_LOGGED_IN = "not logged in"

# The cap is ~60-70 min (§3). Probing a cookie pair older than this is a
# wasted round-trip, so don't -- but still probe younger ones, because the
# cap is approximate and a session can die early.
MAX_AGE_S = 55 * 60

FILE_MODE = 0o600


class SessionStore:
    """`session.json` -- machine-owned, 0600, holds the live cookie pair.

    A stored pair is a live authenticated session for the account (§11), so
    it is written 0600 from the moment it exists rather than written and
    then chmodded.
    """

    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            payload = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict) or not payload.get("cookies"):
            return None
        return payload

    def save(self, cookies, created=None):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {"created": int(created or time.time()),
                   "cookies": dict(cookies)}
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     FILE_MODE)
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)

    def clear(self):
        try:
            self.path.unlink()
        except OSError:
            pass


def _restore(cookies, origin):
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    host = urlparse(origin).hostname
    for name, value in cookies.items():
        # Verbatim. Never quote(), never re-encode -- see the module docstring.
        s.cookies.set(name, value, domain=host, path="/")
    return s


def _is_html(resp):
    if "json" in resp.headers.get("content-type", ""):
        return False
    return True


class Session:
    """The only way the rest of the tool talks to Floorsense.

    `get()`/`post()` wrap every call so that a mid-command session death
    triggers exactly ONE re-login and retry, then gives up. One, not a loop:
    a genuinely rejected credential would otherwise re-prompt forever.
    """

    def __init__(self, config, store, on_message=print, allow_login=True,
                 password_provider=None, origin=FLOORSENSE_ORIGIN,
                 now=time.time, verbose=None, on_invalid_credentials=None,
                 on_login_success=None, force_login=False):
        self.config = config
        self.store = store
        self.on_message = on_message
        self.allow_login = allow_login
        self.password_provider = password_provider
        self.origin = origin
        self.verbose = verbose
        self._now = now
        self._session = None
        self.csrf = None
        self.logged_in_this_run = False
        #: `--save-password`'s effect is entirely inside `_fresh_login` --
        #: that's where `password_provider` is called and `on_login_success`
        #: fires. `force_login` makes `ensure()` skip the cache check and go
        #: straight to a fresh login, so the flag always gets its prompt-and-
        #: confirm cycle regardless of what's cached. `allow_login=False`
        #: still wins over this -- see `ensure()`.
        self.force_login = force_login
        #: `okta_user -> None`. Called only when Okta rejected the login
        #: itself (`InvalidCredentials` -- wrong password/username), never
        #: on `LOCKED_OUT` or a post-password MFA failure, where the
        #: password was never the problem. `cli.py` wires this to forget
        #: the stored password so the NEXT run prompts fresh, rather than
        #: failing the same way forever (`Session.call`'s one-retry rule
        #: already forbids retrying within this run).
        self.on_invalid_credentials = on_invalid_credentials
        #: `(okta_user, password) -> None`. Called only once login is fully
        #: confirmed -- after `_probe` proves the landing URL AND cookies,
        #: not merely after Okta accepted the password -- so a typed
        #: password never reaches the keychain on the strength of an
        #: unconfirmed login.
        self.on_login_success = on_login_success

    # -- establishing a session ---------------------------------------------

    def _probe(self, s):
        """Live? Returns the fresh CSRF token, or None.

        Asserts on the landing URL: `/app/` redirects to `/app/site` when
        authenticated and `/app/login` when not, and BOTH carry a csrf meta
        tag -- so the token's presence proves nothing on its own (§3).
        """
        try:
            resp = s.get(f"{self.origin}/app/", timeout=30)
        except requests.RequestException:
            return None
        if not (resp.url or "").rstrip("/").endswith("/app/site"):
            return None
        return scrape_csrf(resp.text)

    def _from_cache(self):
        payload = self.store.load()
        if not payload:
            return None
        if self._now() - payload.get("created", 0) >= MAX_AGE_S:
            return None
        s = _restore(payload["cookies"], self.origin)
        csrf = self._probe(s)
        if not csrf:
            return None
        self.csrf = csrf
        s.headers["x-csrf-token"] = csrf
        return s

    def _fresh_login(self):
        if not self.allow_login:
            raise LoginRequired("no valid session and --no-login was given",
                                hint="Run any fs command without --no-login "
                                     "to log in.")
        okta_user = self.config.okta_user
        email = self.config.email
        if not okta_user or not email:
            # Defensive: `cli.py`'s `main()` runs first-run setup whenever
            # `okta_user` is missing, for every command but `reset`, before
            # a `Session` is even constructed -- so this shouldn't be
            # reachable from the CLI itself. The hint below must name a
            # command that actually prompts for `okta_user` (not `fs
            # status`, which doesn't), since this is exactly the state `fs
            # reset --full` leaves behind (it clears `okta_user` but leaves
            # config.toml in place).
            raise LoginRequired("no Okta username configured",
                                hint="Run any `fs` command other than "
                                     "`reset` to be prompted for one.")

        password = self.password_provider(okta_user)
        self.on_message("Logging in via Okta...")
        try:
            token = login_with_push_mfa(self.config.okta_org, okta_user,
                                        password, on_message=self.on_message)
        except InvalidCredentials:
            # Okta rejected the password itself -- not LOCKED_OUT, not a
            # post-password MFA failure (both raise something else; see
            # `InvalidCredentials`'s docstring). If that password came from
            # the keychain, it is now confirmed dead, and leaving it there
            # would make every future run fail the exact same way with no
            # way out -- `Session.call`'s one-retry rule already forbids
            # retrying within this run, so the fix has to be "next time".
            if self.on_invalid_credentials:
                self.on_invalid_credentials(okta_user)
            raise
        s = floorsense_login(email, token, self.config.okta_org, self.origin)

        # CSRF is session-scoped and login replaced the session, so the token
        # scraped during login is stale. Re-scrape against the authenticated
        # one -- reusing the pre-auth token yields the HTML CSRF page (§4.5).
        csrf = self._probe(s)
        if not csrf:
            # Seen live, intermittently and in bursts (2026-08-28) -- Okta's
            # push MFA and the SSO hop both succeeded, but the post-login
            # `/app/` probe landed somewhere other than `/app/site`. Neither
            # a bad password (that fails earlier, inside `login_with_push_
            # mfa`) nor a code bug (that would be consistent, not bursty)
            # fits the pattern -- more likely a transient Floorsense-side
            # redirect hiccup, with the burst being the user's own retries
            # landing in the same window. Not proven live; `--verbose`
            # (`wire.py`) captures the actual redirect chain next time this
            # fires. Message kept generic on purpose: whatever the cause,
            # "try again" is the only actionable remedy from here.
            raise CommError(
                "login succeeded but Floorsense redirected somewhere "
                "unexpected",
                hint="This is usually transient -- try again.")
        self.csrf = csrf
        s.headers["x-csrf-token"] = csrf
        self.store.save(cookies_for(s, self.origin))
        self.logged_in_this_run = True
        # Only NOW is the login actually confirmed (this repo's standing
        # rule: assert on something true only if it worked, not a proxy for
        # it) -- so only now, not right after Okta accepted the password, is
        # it safe to let the password reach the keychain.
        if self.on_login_success:
            self.on_login_success(okta_user, password)
        return s

    def ensure(self):
        """Get a live session: cached if possible, fresh login otherwise.

        `force_login` (`--save-password`) skips the cache check entirely --
        `_from_cache()` never calls `password_provider` or fires
        `on_login_success`, so a live cached session would otherwise return
        straight from there without ever prompting. `_fresh_login` still
        checks `allow_login` first, so `--no-login` together with
        `--save-password` still refuses rather than forcing a login.
        """
        if self._session is not None:
            return self._session
        if self.force_login:
            self._session = self._fresh_login()
        else:
            self._session = self._from_cache() or self._fresh_login()
        return self._session

    def is_live(self):
        """For `fs status` -- never triggers a login."""
        try:
            return self._from_cache() is not None
        except (OSError, ValueError):
            return False

    # -- calling --------------------------------------------------------------

    def _decode(self, resp):
        """Never `.json()` without checking it isn't HTML: the CSRF failure
        response is an HTML page and throws on parse, surfacing as a
        confusing parse error rather than the actual problem (§7)."""
        if _is_html(resp):
            body = (resp.text or "").lstrip()
            if "CSRF Check Failed" in body:
                raise CommError("the server rejected the CSRF token",
                                hint="The session may have rotated; try again.")
            raise CommError(f"expected JSON from {resp.url}, got "
                            f"{resp.headers.get('content-type', 'nothing')!r}")
        try:
            return resp.json()
        except ValueError as e:
            raise CommError(f"malformed JSON from {resp.url}") from e

    def _dead(self, data):
        return (isinstance(data, dict) and data.get("result") is False
                and NOT_LOGGED_IN in str(data.get("message", "")).lower())

    def _xhr_headers(self, extra=None):
        """What the web UI sends on every `/app/*` call, on BOTH verbs.

        It lives here rather than in `api.py` for the same reason redaction
        lives in `wire.py`: no call site can then forget it. The experiment
        client sends both headers on every call and works; a missing
        `Referer` has been observed producing an HTML error page instead of
        JSON, which surfaces as a CommError about content-type rather than
        as the actual problem.
        """
        headers = {"X-Requested-With": "XMLHttpRequest",
                   "Referer": f"{self.origin}/app/site"}
        headers.update(extra or {})
        return headers

    def _once(self, method, path, params=None, body=None):
        s = self.ensure()
        url = f"{self.origin}/app/{path.lstrip('/')}"
        try:
            if method == "GET":
                resp = s.get(url, params=params, timeout=30,
                             headers=self._xhr_headers())
            else:
                resp = s.post(url, json=body, timeout=30,
                              headers=self._xhr_headers(
                                  {"Content-Type": "application/json"}))
        except requests.RequestException as e:
            raise CommError(f"could not reach Floorsense: {e}") from e
        log_request(self.verbose, method, resp.url, resp.status_code,
                    dict(resp.request.headers))
        return self._decode(resp)

    def call(self, method, path, params=None, body=None):
        """One re-login and retry on a dead session, then give up."""
        data = self._once(method, path, params, body)
        if not self._dead(data):
            return data

        # Exactly one retry. The manual is explicit that this state is
        # ordinary rather than exceptional -- but a second failure means
        # something else is wrong, and looping would just re-prompt forever.
        self.store.clear()
        self._session = None
        self.csrf = None
        data = self._once(method, path, params, body)
        if self._dead(data):
            raise LoginRequired("the session died and re-login did not help")
        return data

    def get(self, path, params=None):
        return self.call("GET", path, params=params)

    def post(self, path, body=None):
        return self.call("POST", path, body=body)
