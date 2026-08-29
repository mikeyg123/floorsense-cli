# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`fs` is a Python CLI for desk booking through [Floorsense](https://my.floorsense.nz),
a workplace system sitting behind Okta SSO. It logs in over plain HTTP with
no browser automation, caches a short-lived session locally, and exposes the
web app's actions as subcommands (`status`, `list`, `book`, `release`,
`checkin`, `find`, `at`, `map`, `office-days`, `team`, `desks`, `reset`).

Two reference docs under `docs/` are project-independent write-ups this code
transcribes from rather than re-derives: `okta-auth-manual.md` (the
browser-free Okta login recipe) and `floorsense-api-manual.md` (session
lifecycle, endpoint shapes, business rules). `docs/floorplan-map-manual.md`
does the same for the `fs map` ASCII renderer. When behavior here looks odd,
check whether the manual already explains it before changing it.

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]'

fs --help
fs status                              # session/identity check, never logs in

ruff check .                           # lint (F, E, B, BLE rule sets)
pytest                                 # full suite, no network involved
pytest tests/test_auth.py::test_name   # single test
pytest tests/test_auth.py -k pattern   # by pattern

scripts/build-release.sh               # builds a shiv zipapp under dist/
scripts/smoke-test.sh                  # builds + runs it against a scratch HOME
```

CI (`.github/workflows/ci.yml`) runs ruff then pytest on every PR to `main`.
`.github/workflows/release.yml` is manual-dispatch only, never triggered by
a PR or merge — tagged releases are a deliberate action from the Actions tab.

`scripts/probes/write_probe.py` makes real bookings against the live account
to verify server behavior; it's deliberately hostile and self-cleaning, and
is excluded from the normal `pytest` run. `tests/test_write_probe.py` covers
its cleanup logic offline. Only run the live probe when the user asks for it.

## Architecture

`src/fs_cli/` is a straight pipeline — argv in, HTTP out — and each module
owns one stage of it. Most modules carry a docstring explaining *why* they're
shaped the way they are (usually a specific bug or API quirk), so read a
module's docstring before changing its behavior rather than guessing.

**The pipeline, top to bottom:**

- `cli.py` — parses argv, dispatches to a `commands/` module, and turns any
  raised `FsError` into an exit code. Commands never call `sys.exit` or print
  directly; they raise and let `Output` (`render.py`) handle display.
- `args.py` — free-order token classification shared by commands that accept
  a mix of date/desk/group/team/name arguments in any order (e.g. `fs at`,
  `fs find`), with a fixed precedence (date > group > team > desk > name) so
  parsing is deterministic without the caller having to order anything.
- `auth.py` + `session.py` — the Okta → Floorsense login chain and the
  resulting session's lifecycle. The session has a hard ~55–70 minute cap
  with no keep-alive (pinging doesn't extend it); `Session.call` retries a
  dead session with one re-login, never a loop.
- `api.py` — one method per Floorsense endpoint, and the seam that absorbs
  the API's inconsistent response envelopes (success, JSON error, an HTML
  CSRF page, a bare array) into a single shape callers can rely on.
- `catalog.py` — desk/floorplan cache, split deliberately along a cache-TTL
  boundary: desk identity (key/cid/planid/groupid) is cached for 30 days,
  but permission-sensitive fields (`book_advance`, `reserved`) are refetched
  every run because caching them risks the CLI offering desks the server
  will actually refuse.
- `plan.py` — the confirm/execute pipeline shared by every mutating command
  (`book`, `release`, `team`, `desks`): compute a proposed change, show it,
  let the user select rows (or `--yes`/`--json` to skip the prompt), execute,
  report per-row results.
- `config.py` — splits state across `config.toml` (human-editable, no
  secrets), `session.json`/`cache.json` (machine-owned), and the OS keychain
  (the Okta password, never written to a file).
- `render.py`, `dates.py`, `desks.py`, `wire.py` — output formatting
  (`Output`), date-grammar parsing, desk-key normalization/matching, and
  `--verbose` request logging with mandatory secret redaction, respectively.
- `commands/` — one module per subcommand. Most are thin wrappers around
  `plan.py`/`catalog.py`/`api.py`; `map_cmd.py` is the exception, holding its
  own grammar, rendering, and crop logic in one file since nothing else
  reuses it. The per-workplace floorplan geometry (`fs map` needs hand-traced
  wall/room/desk coordinates from the floorplan image) lives separately
  under `floorplans/`, one module per workplace, so a new deployment can
  add a map without touching rendering code.

## Code style

Comments and docstrings should be minimal and concise: only note gotchas or
things that aren't obvious from reading the code (why it's shaped this way,
an API quirk, a bug it works around). Don't narrate what the code visibly
does, and don't record history — past decisions, alternatives considered, or
what a prior version did. That belongs in `docs/` or PR descriptions, not
inline.

## Testing

- `tests/fixtures/*.json` are real captured API responses, not hand-written.
  `FixtureApi` (`fixtures.py`) matches each fixture against a request by its
  *discriminating* params (e.g. `planid`, `bktype`) while ignoring *volatile*
  ones (e.g. `date`), rather than a filename map, so fixtures can't silently
  desync from what a fresh capture would produce. It also refuses to
  fabricate a response for a write it wasn't given a fixture for.
- Auth/session tests run against a real loopback HTTP server
  (`tests/conftest.py`) instead of `requests-mock`, because `requests-mock`
  can't populate a cookie jar — and cookie handling is exactly what those
  tests need to exercise.
- `scripts/probes/` holds logic shared between the live write-probe and the
  test suite. Anything test-critical belongs there, not under `experiments/`
  (gitignored, scratch-only — a fresh checkout won't have it).
