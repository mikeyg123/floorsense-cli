"""Session lifecycle: cache the cookie pair, detect death, re-login once.

The session dies **~60-70 minutes after login regardless of activity**
(§3) -- an absolute cap, not an idle timeout, and pinging cannot extend
it. So there is no keep-alive here. §10: treat "session probably dead" as
normal, not an error.

Three load-bearing traps, each with a test:

  * the `id` cookie is stored and replayed **verbatim** -- it's an
    Express signed cookie already containing percent-encoding
    (`s%3A...`); re-encoding it produces the same "not logged in" as
    expiry (§4.5).
  * `MYSLSRV` is sent alongside, always -- sessions live in memory on
    one node, and omitting it yields the *same* "not logged in" from a
    node that never heard of your session (§3).
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

# The cap is ~60-70 min (§3) -- don't probe a cookie pair older than this,
# but still probe younger ones since a session can die early.
MAX_AGE_S = 55 * 60

FILE_MODE = 0o600


class SessionStore:
    """`session.json` -- machine-owned, 0600, holds the live cookie pair.
    Written 0600 from the moment it exists (§11), never chmodded after.
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
        # Verbatim -- never quote(), never re-encode (module docstring).
        s.cookies.set(name, value, domain=host, path="/")
    return s


def _is_html(resp):
    if "json" in resp.headers.get("content-type", ""):
        return False
    return True


class Session:
    """The only way the rest of the tool talks to Floorsense.

    `get()`/`post()` wrap every call so a mid-command session death
    triggers exactly ONE re-login and retry, then gives up -- a
    genuinely rejected credential would otherwise re-prompt forever.
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
        #: `force_login` makes `ensure()` skip the cache check and go
        #: straight to a fresh login, so `--save-password` always gets
        #: its prompt-and-confirm cycle. `allow_login=False` still wins
        #: over this -- see `ensure()`.
        self.force_login = force_login
        #: `okta_user -> None`. Called only when Okta rejected the login
        #: itself, never on LOCKED_OUT or a post-password MFA failure.
        #: `cli.py` wires this to forget the stored password so the NEXT
        #: run prompts fresh.
        self.on_invalid_credentials = on_invalid_credentials
        #: `(okta_user, password) -> None`. Called only once login is
        #: fully confirmed, not merely after Okta accepted the password.
        self.on_login_success = on_login_success

    # -- establishing a session ---------------------------------------------

    def _probe(self, s):
        """Live? Returns the fresh CSRF token, or None. Asserts on the
        landing URL: `/app/` redirects to `/app/site` when authenticated
        and `/app/login` when not, and BOTH carry a csrf meta tag -- so
        the token's presence alone proves nothing (§3).
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
            # `okta_user` is missing, so this shouldn't be reachable from
            # the CLI itself -- this is the state `fs reset --full` leaves
            # behind (clears `okta_user`, leaves config.toml in place).
            raise LoginRequired("no Okta username configured",
                                hint="Run any `fs` command other than "
                                     "`reset` to be prompted for one.")

        password = self.password_provider(okta_user)
        self.on_message("Logging in via Okta...")
        try:
            token = login_with_push_mfa(self.config.okta_org, okta_user,
                                        password, on_message=self.on_message)
        except InvalidCredentials:
            # Okta rejected the password itself, not LOCKED_OUT or a
            # post-password MFA failure. If that password came from the
            # keychain it's now confirmed dead -- forget it so the fix
            # is "next time", not every run failing the same way.
            if self.on_invalid_credentials:
                self.on_invalid_credentials(okta_user)
            raise
        s = floorsense_login(email, token, self.config.okta_org, self.origin)

        # CSRF is session-scoped and login replaced the session, so the
        # token scraped during login is stale -- reusing it yields the
        # HTML CSRF page (§4.5).
        csrf = self._probe(s)
        if not csrf:
            # Push MFA and the SSO hop can both succeed but the post-login
            # `/app/` probe lands somewhere other than `/app/site` -- a
            # transient Floorsense-side redirect. `--verbose` captures the
            # actual redirect chain if this needs diagnosing.
            raise CommError(
                "login succeeded but Floorsense redirected somewhere "
                "unexpected",
                hint="This is usually transient -- try again.")
        self.csrf = csrf
        s.headers["x-csrf-token"] = csrf
        self.store.save(cookies_for(s, self.origin))
        self.logged_in_this_run = True
        # Only NOW is login actually confirmed -- not right after Okta
        # accepted the password -- so only now is it safe for the
        # password to reach the keychain.
        if self.on_login_success:
            self.on_login_success(okta_user, password)
        return s

    def ensure(self):
        """Get a live session: cached if possible, fresh login otherwise.
        `force_login` (`--save-password`) skips the cache check entirely
        -- otherwise a live cached session would return without ever
        prompting. `_fresh_login` still checks `allow_login` first, so
        `--no-login` + `--save-password` still refuses.
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
        """Never `.json()` without checking it isn't HTML: the CSRF
        failure response is an HTML page and throws on parse (§7)."""
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
        Lives here, not `api.py`, so no call site can forget it -- a
        missing `Referer` produces an HTML error page instead of JSON.
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

        # Exactly one retry -- a second failure means something else is
        # wrong, and looping would just re-prompt forever.
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
