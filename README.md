# floorsense-cli

`fs` — desk booking at the command line, via
[Floorsense](https://my.floorsense.nz), without opening a browser.

Floorsense sits behind Okta SSO, and doing weekly desk booking through
its web UI is slow and easy to forget. `fs` logs in over plain HTTP
(no browser automation), keeps a short-lived session cached locally,
and exposes the same actions the web app offers as CLI subcommands.

## Getting started

Requires Python 3.11+ (uses stdlib `tomllib`).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
fs --help
```

First run walks you through identity setup (your Floorsense email is
all that's asked for; Okta org and login are derived from it). If your
workplace's Floorsense isn't at `https://my.floorsense.nz`, pass
`--url` — see `fs help` for the full flag/command grammar.

## Running the build

`fs` also builds as a single extensionless executable — a
`shiv`-built zipapp with its dependencies bundled — for someone with
Python already installed to download, `chmod +x`, and drop on their
`PATH`: no `pip install`, no admin rights, same file on macOS, Linux,
and Windows-via-bash (Git Bash/WSL/MSYS2). It still needs a `python3`
(3.11+) already on the target machine's `PATH` — this is not a
standalone binary.

```bash
scripts/build-release.sh   # needs python3.11 on PATH; builds its own
                            # venv under build/, writes dist/fs
scripts/smoke-test.sh      # builds + runs it against a scratch
                            # HOME/PATH with no editable install in reach
```

A tagged release build (versioned, built, and published as a GitHub
Release) only happens when manually triggered from the Actions tab —
see [Releasing](#releasing) below.

## Testing

```bash
pytest
```

`scripts/probes/write_probe.py` is a deliberately hostile, self-cleaning
probe that makes real bookings against your live Floorsense account to
verify server behaviour — not part of the normal test run;
`tests/test_write_probe.py` exercises its cleanup path offline.

## Source overview

```
src/fs_cli/
  cli.py           argv -> command dispatch, global flags, exit codes
  auth.py          Okta login + Floorsense session exchange (no browser)
  session.py       session lifecycle: cache, expiry, one re-login retry
  api.py           one method per Floorsense endpoint
  catalog.py       desk/floorplan cache
  plan.py          confirm/execute pipeline shared by every write command
  args.py          free-order token classification (date/desk/name/group)
  config.py        config.toml + keychain-backed identity/preferences
  fixtures.py      captured-response backend for the test suite
  commands/        one module per subcommand (book, list, map, team, ...)
```

Each module's docstring names the specific behaviour or bug it encodes
— read those before changing one.

## Releasing

Release builds are manual only — nothing runs on merge or push. From
the GitHub Actions tab, run **Release build** with a version number
(or `gh workflow run release.yml -f version=X.Y.Z`). It bumps
`pyproject.toml`, tags `vX.Y.Z`, builds `dist/fs`, and publishes it as
a downloadable asset on a GitHub Release.

## Licence and acknowledgements

`fs` is licensed under the GNU Lesser General Public License v2.1 (see
`LICENSE`). It bundles several third-party open-source packages under
their own permissive licences (Apache-2.0, MIT, BSD-3-Clause, MPL-2.0)
— see `NOTICE` and `THIRD-PARTY-NOTICES.txt`, or run `fs --licences`
to print the same information from the built tool. Full licence-type
reference texts are under `LICENSES/`.

Built with, among others: [Requests](https://requests.readthedocs.io/),
[keyring](https://github.com/jaraco/keyring), and
[tomli-w](https://github.com/hukkin/tomli-w).

## Documentation

| File | What it holds |
|---|---|
| [`docs/okta-authn-api-experiment.md`](docs/okta-authn-api-experiment.md) | Write-up of the browser-free login experiment and its outcome |
| [`docs/okta-auth-manual.md`](docs/okta-auth-manual.md) | Okta reference: Classic AuthN vs OIE, push MFA, the full browser-free login recipe |
| [`docs/floorsense-api-manual.md`](docs/floorsense-api-manual.md) | Floorsense API reference: session model, endpoints, data shapes, business rules |
| [`docs/floorplan-map-manual.md`](docs/floorplan-map-manual.md) | `fs map`'s data sources, per-floor rendering approach, and the wrong-turn/fix pairs behind it |

## Status

Under active development.
