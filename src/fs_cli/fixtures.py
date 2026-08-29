"""`FixtureApi` -- the same endpoint surface, served from Phase 0 captures.

This is what the test suite runs against, and what `tests/live_write_api.py`'s
`LiveWriteApi` wraps for `fs map`'s live-write tests -- real, captured
responses instead of hand-written stubs. That is deliberate: a stub written
to satisfy tests drifts from the server it stands in for and stops being
evidence of anything; a recording of the real server, replayed, keeps the
suite an honest check against the actual floor.

**Matching is driven by each fixture's own `request` block, not a filename
map.** 07 writes `{"request": {"method", "path", "params", "body"}}` into
every capture, so the index is derived from the recordings themselves and a
re-run of 07 cannot silently desync it. A filename map is a second copy of
that information, and second copies rot.

Parameters split into two kinds, which is the only subtle part:

  * **discriminating** -- `planid`, `groupid`, `bktype`: different values
    mean genuinely different responses, so they must match.
  * **volatile** -- `date`, `start`, `finish`, `days`, `tz`: these were
    whatever the capture day happened to be. Requiring them to match would
    make every fixture unusable the day after it was recorded, so they are
    ignored for matching. A test asking about next Tuesday gets the floor
    as it was on capture day, which is exactly the fidelity these captures
    were made to provide.
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
    """`start`/`finish` are volatile as *values* but their WIDTH is not.

    §5.2 records this as a correction the manual had to make: `user-search`'s
    `future[]` is bounded by the window, so the one-day capture legitimately
    returned no upcoming bookings and the 14-day one returned three. Treating
    the whole window as volatile makes those two captures indistinguishable,
    and the tie is resolved by glob order -- which silently picked the empty
    one, reproducing the exact misreading §5.2 exists to correct.

    So the width is bucketed and matched, while the absolute timestamps stay
    ignored (they are still just "whenever the capture ran").
    """
    start, finish = (params or {}).get("start"), (params or {}).get("finish")
    if not isinstance(start, int) or not isinstance(finish, int):
        return None
    return "day" if finish - start <= ONE_DAY_S else "wide"


#: Endpoints where the WIDTH of the requested window changes the answer, and
#: so has to be matched. `floorplan-booking` is deliberately absent: it is
#: always asked about a single day, and including it would mean a future
#: multi-day request failing with "no fixture" rather than matching the
#: day-shaped captures that would have answered it correctly.
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
    """Reads. Writes are refused rather than faked.

    A fake write would have to invent a response, and this class exists to
    answer from real captures, not invented ones -- see `live_write_api.py`
    for the test double that DOES need to simulate a write's effect, and
    why it wraps this class rather than replacing it. Reaching a write
    here is a bug in the caller, and says so.
    """

    def __init__(self, directory=None):
        self.directory = pathlib.Path(directory or FIXTURE_DIR)
        self._index = {}
        self._load()

    def _load(self):
        if not self.directory.is_dir():
            # Only reachable from a non-editable install, where the source
            # tree (and so `tests/fixtures/`) isn't shipped. Say that,
            # rather than reporting every endpoint as individually missing.
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
        # Most specific first: a fixture that pins `planid` beats a bare one,
        # so `floorplan-booking?planid=3` cannot be answered by planid 1's
        # capture just because it was loaded earlier.
        ranked = sorted(candidates, key=lambda c: -len(c[0]))
        matches = [c for c in ranked
                   if all(wanted.get(k) == v for k, v in c[0].items())]
        if matches:
            best = [c for c in matches if len(c[0]) == len(matches[0][0])]
            # An unresolved tie between DIFFERENT captures is an error, not a
            # coin toss. Identical bodies (the 15d/30d summary pair, which the
            # server capped to the same response) tie harmlessly, so only a
            # genuine disagreement is worth stopping for.
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
