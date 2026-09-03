"""`FixtureApi` -- the same endpoint surface, served from Phase 0 captures.

Real, captured responses instead of hand-written stubs -- a stub drifts
from the server it stands in for; a recording, replayed, keeps the suite
an honest check against the actual floor.

**Matching is driven by each fixture's own `request` block, not a
filename map** -- the index is derived from the recordings themselves,
so a re-capture can't silently desync it.

Parameters split into two kinds:

  * **discriminating** -- `planid`, `groupid`, `bktype`: different
    values mean genuinely different responses, so they must match.
  * **volatile** -- `date`, `start`, `finish`, `days`, `tz`: whatever
    the capture day happened to be. Ignored for matching, so a fixture
    stays usable after the day it was recorded.
"""

import json
import pathlib

from .api import Api
from .errors import CommError

__all__ = ["FixtureApi", "FIXTURE_DIR"]

FIXTURE_DIR = pathlib.Path(__file__).resolve().parents[2] / "tests" / "fixtures"

#: Params that were an artefact of when the capture ran, not of what was
#: asked. See the module docstring.
VOLATILE_PARAMS = {"date", "start", "finish", "days", "tz", "team", "friend",
                   "bkid", "uid", "name", "desc"}

#: A one-day window, to the second: `user-search` was captured with
#: `start`/`finish` 86399s apart.
ONE_DAY_S = 86400


def _key(method, path):
    return (method.upper(), "/app/" + path.strip("/").removeprefix("app/"))


def _window(params):
    """`start`/`finish` are volatile as *values* but their WIDTH is not:
    `user-search`'s `future[]` is bounded by the window (§5.2), so a
    one-day capture and a 14-day capture legitimately differ. The width
    is bucketed and matched; the absolute timestamps stay ignored.
    """
    start, finish = (params or {}).get("start"), (params or {}).get("finish")
    if not isinstance(start, int) or not isinstance(finish, int):
        return None
    return "day" if finish - start <= ONE_DAY_S else "wide"


#: Endpoints where the WIDTH of the requested window changes the answer.
#: `floorplan-booking` is deliberately absent -- it's always a single day.
WINDOW_SENSITIVE = {"/app/user-search"}


def _discriminating(path, params):
    out = {k: str(v) for k, v in (params or {}).items()
           if k not in VOLATILE_PARAMS}
    if path in WINDOW_SENSITIVE:
        window = _window(params)
        if window:
            out["~window"] = window
    return out


class FixtureApi(Api):
    """Reads. Writes are refused rather than faked -- a fake write would
    have to invent a response. See `live_write_api.py` for the test
    double that DOES simulate a write's effect, wrapping this class
    rather than replacing it. Reaching a write here is a bug, and says so.
    """

    def __init__(self, directory=None):
        self.directory = pathlib.Path(directory or FIXTURE_DIR)
        self._index = {}
        self._load()

    def _load(self):
        if not self.directory.is_dir():
            # Only reachable from a non-editable install, where the
            # source tree isn't shipped. Say that, not "endpoint missing".
            raise CommError(
                f"no fixtures at {self.directory}",
                hint="FixtureApi reads the Phase 0 captures from the "
                     "source tree; run the test suite from a checkout, "
                     "not an installed build.")
        for path in sorted(self.directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text())
            except (OSError, ValueError) as e:
                raise CommError(f"unreadable fixture {path.name}: {e}") from e
            request = payload.get("request")
            if not isinstance(request, dict) or not request.get("path"):
                continue
            key = _key(request.get("method", "GET"), request["path"])
            # A POST carries its parameters in the body, so that is where its
            # discriminators live -- `user-search`'s window is a body field.
            sent = request.get("params") or request.get("body")
            self._index.setdefault(key, []).append(
                (_discriminating(key[1], sent), payload, path.name))

    def _match(self, method, path, params=None):
        candidates = self._index.get(_key(method, path))
        if not candidates:
            raise CommError(
                f"no fixture for {method} {path}",
                hint="Re-run experiments/07_capture_shapes.py to capture it.")

        wanted = _discriminating(_key(method, path)[1], params)
        # Most specific first: a fixture pinning `planid` beats a bare
        # one, so `floorplan-booking?planid=3` can't be answered by
        # planid 1's capture just because it loaded first.
        ranked = sorted(candidates, key=lambda c: -len(c[0]))
        matches = [c for c in ranked
                   if all(wanted.get(k) == v for k, v in c[0].items())]
        if matches:
            best = [c for c in matches if len(c[0]) == len(matches[0][0])]
            # An unresolved tie between DIFFERENT captures is an error,
            # not a coin toss -- identical bodies tie harmlessly.
            distinct = {json.dumps(c[1].get("body"), sort_keys=True)
                        for c in best}
            if len(distinct) > 1:
                raise CommError(
                    f"ambiguous fixtures for {method} {path}: "
                    f"{sorted(c[2] for c in best)}",
                    hint="They match the same request but hold different "
                         "responses. Add a discriminating parameter or "
                         "remove one.")
            return best[0][1]
        raise CommError(
            f"no fixture for {method} {path} with {wanted}",
            hint=f"Captured variants: "
                 f"{[c[0] for c in candidates]}")

    def _body(self, method, path, params=None):
        payload = self._match(method, path, params)
        if "body" not in payload:
            raise CommError(f"fixture for {method} {path} has no body")
        return payload["body"]

    def _get(self, path, params=None):
        return self._body("GET", path, params)

    def _post(self, path, body=None):
        return self._body("POST", path, body)

    # -- writes are not simulated -------------------------------------------

    def _refuse_write(self, what):
        raise CommError(
            f"FixtureApi asked to {what}",
            hint="This is a bug: writes must go through a write-capable "
                 "test double (see live_write_api.py), never this class.")

    def booking_create(self, start, key, cid, day=None):
        self._refuse_write(f"book {key}")

    def booking_update(self, bkid, key, cid, day=None):
        self._refuse_write(f"move booking {bkid} to {key}")

    def booking_release(self, bkid):
        self._refuse_write(f"release booking {bkid}")

    def booking_confirm(self, bkid):
        self._refuse_write(f"confirm booking {bkid}")

    def friend_create(self, uid):
        self._refuse_write(f"follow {uid}")

    def friend_delete(self, uid):
        self._refuse_write(f"unfollow {uid}")
