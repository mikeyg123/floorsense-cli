"""A loopback HTTP server that replays the real login chain.

Why a real server rather than a mock: `requests_mock` never populates the
Session cookie jar, so it cannot exercise a single one of the cookie traps
the manuals record -- the verbatim `s%3A...` `id` value, `MYSLSRV` accompanying
every call, or a mid-command session death. Those are precisely the bugs that
have cost this project debugging rounds, so they get a harness that can
actually reproduce them.

The server implements both halves of the chain (Okta paths and Floorsense
`/app/*` paths) on one origin -- their paths don't collide -- and every
failure mode from `floorsense-api-manual.md` §4.6 is reachable by flipping a
flag in `server.state`.
"""

import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

CSRF_TOKEN = "tok-authenticated"
PREAUTH_CSRF = "tok-preauth"
REAL_ID = "s%3ABAv0n9xWaPrc.sIgNaTuRe"      # the shape §4.5 warns about
PREAUTH_ID = "s%3AyXxunJKyjbFn.preauth"

MFA_REQUIRED = {
    "stateToken": "state-1", "status": "MFA_REQUIRED",
    "_embedded": {"factors": [
        {"id": "ostw1", "factorType": "token:software:totp",
         "_links": {"verify": {"href": "/api/v1/authn/factors/ostw1/verify"}}},
        {"id": "opfw123", "factorType": "push",
         "_links": {"verify": {"href": "/api/v1/authn/factors/opfw123/verify"}}},
    ]},
}


def _default_state():
    return {
        # --- Okta knobs ---
        "authn": ("MFA_REQUIRED", 200),
        "poll": ["MFA_CHALLENGE", "SUCCESS"],
        "challenge_number": None,
        "challenge_number_after": 0,     # polls before the number appears
        "session_token_valid": True,
        "prompt_none_works": True,
        "authorize_error": None,         # e.g. "login_required"
        # --- Floorsense knobs ---
        "login_post_redirects": True,
        "csrf_meta": True,
        "callback_lands_on": "/app/site",
        "authenticated": False,
        # --- API responses: path -> body, or a list consumed in order ---
        "api": {},
        # A session is only accepted when BOTH cookies arrive (§3).
        "require_myslsrv": True,
    }


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------

    def log_message(self, *args):
        pass

    @property
    def state(self):
        return self.server.state

    def _cookies(self):
        raw = self.headers.get("Cookie", "")
        out = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out

    def _record(self, body=None):
        self.server.requests.append({
            "method": self.command,
            "path": urlparse(self.path).path,
            "query": parse_qs(urlparse(self.path).query),
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "cookies": self._cookies(),
            "body": body,
        })

    def _send(self, status, body=b"", ctype="text/html", cookies=None,
              location=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if location:
            self.send_header("Location", location)
        for name, value in (cookies or {}).items():
            self.send_header("Set-Cookie", f"{name}={value}; Path=/")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, payload, status=200, cookies=None):
        self._send(status, json.dumps(payload).encode(), "application/json",
                   cookies=cookies)

    def _html(self, text, status=200, cookies=None, location=None):
        self._send(status, text.encode(), "text/html", cookies=cookies,
                   location=location)

    def _origin(self):
        return f"http://{self.headers.get('Host')}"

    def _absolute_links(self, body):
        """Okta's `_links` hrefs are absolute URLs. Rewriting them here keeps
        the module-level fixture readable while still handing the client what
        the real thing hands it."""
        import copy
        body = copy.deepcopy(body)
        for factor in body.get("_embedded", {}).get("factors", []):
            href = factor["_links"]["verify"]["href"]
            factor["_links"]["verify"]["href"] = self._origin() + href
        return body

    def _csrf_page(self, token):
        if not self.state["csrf_meta"]:
            return "<html><head></head><body>no meta</body></html>"
        return f'<html><head><meta name="csrf" content="{token}"></head></html>'

    # -- the session check every /app/* API call goes through --------------

    def _session_ok(self):
        """Both cookies, together. A valid `id` without `MYSLSRV` routes to a
        node that never heard of the session and answers "not logged in" --
        an auth-shaped symptom with a routing cause (§3)."""
        c = self._cookies()
        if c.get("id") != REAL_ID:
            return False
        if self.state["require_myslsrv"] and "MYSLSRV" not in c:
            return False
        return True

    # -- routes ------------------------------------------------------------

    def do_GET(self):
        self._record()
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)

        if path == "/app/login":
            return self._html(self._csrf_page(PREAUTH_CSRF),
                              cookies={"id": PREAUTH_ID})

        if path == "/app/config":
            return self._json({"result": True})

        if path == "/login/sessionCookieRedirect":
            if not self.state["session_token_valid"]:
                # One-shot token already spent: Okta renders the widget and
                # sets no `sid` (§4.6).
                return self._html("<html>Sign-In Widget</html>")
            return self._html("", 302, cookies={"sid": "okta-sid-1",
                                                "DT": "dt-1"},
                              location=query.get("redirectUrl", ["/"])[0])

        if path == "/oauth2/v1/authorize":
            if self.state["authorize_error"]:
                return self._html("", 302, location=(
                    f"/app/login?error={self.state['authorize_error']}"))
            if "none" not in query.get("prompt", []):
                # No prompt=none -> the Sign-In Widget's HTML, which
                # completes SSO in JavaScript. A raw client dead-ends here.
                return self._html("<html>runLoginPage okta-sign-in</html>")
            if not self.state["prompt_none_works"]:
                return self._html("", 302,
                                  location="/app/login?error=login_required")
            return self._html("", 302,
                              location="/app/oidc/callback?code=c1&state=s1")

        if path == "/app/oidc/callback":
            return self._html("", 302, location="/app/site-auth")

        if path == "/app/site-auth":
            self.state["authenticated"] = True
            return self._html("", 302, location=self.state["callback_lands_on"],
                              cookies={"id": REAL_ID})

        if path == "/app/site":
            return self._html(self._csrf_page(CSRF_TOKEN),
                              cookies={"MYSLSRV": "api-nz-b1"})

        if path == "/app/" or path == "/app":
            # Authenticated -> /app/site, otherwise -> /app/login. Both carry
            # a csrf meta tag, which is why the token alone proves nothing.
            if self._session_ok():
                return self._html("", 302, location="/app/site")
            return self._html("", 302, location="/app/login")

        if path.startswith("/app/"):
            return self._api(path)

        return self._html("not found", 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode() if length else ""
        self._record(raw)
        path = urlparse(self.path).path

        if path == "/api/v1/authn":
            status, code = self.state["authn"]
            if status == "MFA_REQUIRED":
                return self._json(self._absolute_links(MFA_REQUIRED))
            if status == "SUCCESS":
                return self._json({"status": "SUCCESS",
                                   "sessionToken": "sess-direct"})
            if status == "ERROR":
                return self._json({"errorCode": "E0000004",
                                   "errorSummary": "Authentication failed"},
                                  status=code)
            if status == "DISABLED":
                return self._json({"errorCode": "E0000038",
                                   "errorSummary": "Operation not supported"},
                                  status=code)
            return self._json({"status": status}, status=code)

        if path.startswith("/api/v1/authn/factors/"):
            seq = self.state["poll"]
            status = seq.pop(0) if len(seq) > 1 else (seq[0] if seq else
                                                     "MFA_CHALLENGE")
            body = {"status": status,
                    "_links": {"next": {"name": "poll",
                                        "href": self._origin() + path}}}
            if status == "SUCCESS":
                body["sessionToken"] = "sess-xyz"
            n = self.state["challenge_number"]
            if n is not None:
                self.state["challenge_number_after"] -= 1
                if self.state["challenge_number_after"] < 0:
                    body["_embedded"] = {"factor": {"_embedded": {
                        "challenge": {"correctAnswer": n}}}}
            return self._json(body)

        if path == "/app/login":
            if not self.state["login_post_redirects"]:
                # Wrong encoding / CSRF as a header -> an HTML page, not JSON,
                # and not a 302 (§7).
                return self._html("<html><p>An error has occured ... "
                                  "'CSRF Check Failed'</p></html>")
            fields = parse_qs(raw)
            if fields.get("csrftoken", [""])[0] != PREAUTH_CSRF:
                return self._html("<html><p>'CSRF Check Failed'</p></html>")
            origin = f"http://{self.headers.get('Host')}"
            return self._html("", 302, location=(
                f"{origin}/oauth2/v1/authorize?client_id=x&state=s1"
                f"&code_challenge=c1&redirect_uri=/app/oidc/callback"))

        if path.startswith("/app/"):
            return self._api(path, raw)

        return self._html("not found", 404)

    def _api(self, path, raw=None):
        """The `/app/*` JSON API, behind the two-cookie session check."""
        if not self._session_ok():
            return self._json({"result": False, "message": "not logged in"})
        if self.headers.get("x-csrf-token") != CSRF_TOKEN:
            return self._html("<html><p>'CSRF Check Failed'</p></html>")

        canned = self.state["api"].get(path.replace("/app/", ""))
        if callable(canned):
            # A callable lets a test model real server STATE -- a booking
            # store that actually changes when you release something --
            # rather than a fixed sequence of replies.
            return self._json(canned(raw, parse_qs(urlparse(self.path).query)))
        if isinstance(canned, list):
            return self._json(canned.pop(0) if canned else
                              {"result": True, "info": []})
        if canned is not None:
            return self._json(canned)
        return self._json({"result": True, "info": []})


class _Server(ThreadingHTTPServer):
    # Handler threads sit blocked on keep-alive connections; without these
    # two, `server_close()` joins each one and every test waits on it.
    daemon_threads = True
    block_on_close = False

    def server_bind(self):
        """Skip HTTPServer.server_bind's `getfqdn` reverse-DNS lookup.

        On a network where that lookup has to time out it costs ~35 seconds,
        once, in the first test's fixture setup -- which reads as "the test
        suite is slow" rather than "DNS is hanging".
        """
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class FakeStack:
    def __init__(self, server, thread):
        self._server = server
        self._thread = thread
        host, port = server.server_address[:2]
        self.origin = f"http://{host}:{port}"
        self.org = self.origin          # same server serves the Okta paths

    @property
    def state(self):
        return self._server.state

    @property
    def requests(self):
        return self._server.requests

    def find(self, method, path):
        return [r for r in self.requests
                if r["method"] == method and r["path"] == path]

    def shutdown(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def stack():
    server = _Server(("127.0.0.1", 0), _Handler)
    server.state = _default_state()
    server.requests = []
    # poll_interval defaults to 0.5s, which is what every `shutdown()` waits.
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    s = FakeStack(server, thread)
    yield s
    s.shutdown()
