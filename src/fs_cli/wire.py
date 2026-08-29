"""`--verbose` wire logging, with redaction that is not optional.

Logs method, URL, status and header NAMES. Never header values, because the
values are the session: the cookie pair is a live authenticated session for
the account (`floorsense-api-manual.md` §11), and the Okta `sessionToken`
travels in a query parameter where a naive URL log would publish it.

Redaction lives here rather than at each call site so that adding a new call
cannot accidentally opt out of it.
"""

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

__all__ = ["redact_url", "redact_headers", "log_request", "SECRET_PARAMS",
           "SECRET_HEADERS"]

#: Query parameters whose values are credentials in this protocol.
#:
#: `state` and `code_challenge` are deliberately NOT here. They are not
#: secrets, and a stale or replayed authorize URL is one of the failure modes
#: §4.6 describes -- redacting them would blind the log to exactly the bug
#: --verbose exists to help diagnose.
SECRET_PARAMS = {"token", "password", "code", "sessiontoken", "csrftoken",
                 "id_token", "access_token"}

SECRET_HEADERS = {"cookie", "set-cookie", "x-csrf-token", "authorization"}

REDACTED = "<redacted>"


def redact_url(url):
    """Keep the shape, lose the secrets. `?token=abc` -> `?token=<redacted>`,
    so a verbose log still tells you which call was made."""
    parts = urlparse(url)
    if not parts.query:
        return url
    query = [(k, REDACTED if k.lower() in SECRET_PARAMS else v)
             for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunparse(parts._replace(query=urlencode(query)))


def redact_headers(headers):
    """Header names are useful for debugging (was the CSRF header sent at
    all?); header values never are."""
    out = {}
    for name, value in (headers or {}).items():
        out[name] = REDACTED if name.lower() in SECRET_HEADERS else value
    return out


def log_request(verbose, method, url, status=None, headers=None):
    """`verbose` is a callable or None, so the caller never branches."""
    if not verbose:
        return
    line = f"{method} {redact_url(url)}"
    if status is not None:
        line += f" -> {status}"
    if headers:
        line += f"  headers: {sorted(redact_headers(headers))}"
    verbose(line)
