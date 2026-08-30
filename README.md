# floorsense-cli

[![CI](https://github.com/mikeyg123/floorsense-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/mikeyg123/floorsense-cli/actions/workflows/ci.yml)
[![Licence: LGPL-2.1](https://img.shields.io/badge/licence-LGPL--2.1-blue.svg)](LICENSE)
[![Latest release](https://img.shields.io/github/v/release/mikeyg123/floorsense-cli?label=latest)](https://github.com/mikeyg123/floorsense-cli/releases/latest)

Because all the best intelligences love a CLI - even those that sit at desks.

`fs` — desk booking at the command line, via
[Floorsense](https://my.floorsense.nz), without opening a browser.

**[⬇ Download the latest `fs` executable](https://github.com/mikeyg123/floorsense-cli/releases/latest/download/fs)**
(always the newest release; see [Getting started](#getting-started) below).

**Requirements:** Python 3.11+ on your `PATH`. Runs on macOS, Linux, and
Windows via a bash-like shell (Git Bash/WSL/MSYS2).

## Contents

- [Getting started](#getting-started)
- [Usage](#usage)
- [Notes for your workplace](#notes-for-your-workplace)
- [Development Setup](#development-setup)
- [Running the build](#running-the-build)
- [Testing](#testing)
- [Source overview](#source-overview)
- [Releasing](#releasing)
- [Licence and acknowledgements](#licence-and-acknowledgements)
- [Documentation](#documentation)
- [Status](#status)
- [Credits](#credits)

## Getting started

1. [Download `fs`](https://github.com/mikeyg123/floorsense-cli/releases/latest/download/fs),
   drop it somewhere on your `PATH` (e.g. `~/bin` or `/usr/local/bin`), and make it executable. 
   Run directly from a shell — no install, no admin rights required. On Windows, run it from a
   bash-like shell if you have one, or create a launcher that runs it as a
   `python3` script in a terminal window.
2. Make sure `python3` (3.11+) is on your `PATH` — `fs` is a self-contained
   zipapp but still needs your system's Python.
3. Run any command. The first run walks you through setup (Okta username +
   MFA: tap "Yes, it's me" or select the correct number challenge). If your
   workplace's Floorsense isn't at `https://my.floorsense.nz`, pass `--url`
   — see `fs help` for the full info.
4. Run `fs --version` any time to check which build you have — handy when
   filing a bug report.

### Usage

```bash
fs list                       # show your upcoming bookings
fs office-days tue wed fri    # set your office-days
fs desks set preferred 2.166 2.80 2.217   # set your preferred desks
fs book                       # book/upgrade to your best available desks on your office days
fs map                        # see where you are and book desks on an interactive 
                              # scrollable map inside your terminal
fs team set officers picard riker data geordi worf troy bev   # set your team
fs list officers              # find your team
fs help                       # see what else you can do

fs list tasha tomorrow        # see a colleague's bookings by name(s) and date(s)
fs book 5.123 tue             # book desk 5.123 for Tuesday
fs book --yes                 # auto book/upgrade your preferred desks on your regular 
                              # office days
fs checkin                    # check in to today's booking
fs release                    # release today's desk booking
fs release tue-next           # un-book Tuesday of next week
fs at 2.123 3rd               # see who's sitting at a desk/group on a date
fs office-days                # see/set your own regular office days
fs help book                  # full help for a single command
```

The order of many parameters is flexible when unambiguous, and there are
many ways to specify a date — see the built-in help for details, or read
the source.

## Notes for your workplace

The latest version has been developed for use at my workplace. Floorsense
allows a lot of customisation, so you may need to tune things a little. In
particular:

- Login is via Okta: after entering your company email at
  `https://my.floorsense.nz`, it expects to be redirected to Okta.
- Desk booking is only allowed for whole days up to 10 days ahead. On the
  day, booking is only possible before 8:30.
- No support for repeat booking, delegation, or other features.
- There are additional map features and layout hints for the author's
  building. The tool can only make a best effort to draw maps for new
  buildings and floors.

## Development Setup

From a local checkout (same Python requirement as above):

```bash
python3 -m venv .venv
source .venv/bin/activate
fs --help
```

## Running the build

`fs` builds as a single extensionless executable — a `shiv`-built zipapp
with its dependencies bundled — for someone with Python already installed
to download, `chmod +x`, and drop on their `PATH`: no `pip install`, no
admin rights, same file on macOS, Linux, and Windows-via-bash (Git
Bash/WSL/MSYS2). It still needs a `python3` (3.11+) already on the target
machine's `PATH` — this is not a standalone binary.

```bash
scripts/build-release.sh   # needs python3.11 on PATH; builds its own
                            # venv under build/, writes dist/fs
scripts/smoke-test.sh      # builds + runs it against a scratch
                            # HOME/PATH with no editable install in reach
```

Linux builds of `dist/fs` don't use the OS keychain, as the required
binaries made the package too large — `fs` always prompts for the password
there. `pip install` usage is unaffected.

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

Each module's docstring names the specific behaviour or bug it encodes —
read those before changing one.

## Releasing

Release builds are manual only — nothing runs on merge or push. From the
GitHub Actions tab, run **Release build** with a version number (or
`gh workflow run release.yml -f version=X.Y.Z`). It bumps
`pyproject.toml`, tags `vX.Y.Z`, builds `dist/fs`, and publishes it as a
downloadable asset on a GitHub Release.

## Licence and acknowledgements

`fs` is licensed under the GNU Lesser General Public License v2.1 (see
`LICENSE`). It bundles several third-party open-source packages under
their own permissive licences (Apache-2.0, MIT, BSD-3-Clause, MPL-2.0) —
see `NOTICE` and `THIRD-PARTY-NOTICES.txt`, or run `fs --licences` to
print the same information from the built tool. Full licence-type
reference texts are under `LICENSES/`.

Built with, among others: [Requests](https://requests.readthedocs.io/),
[keyring](https://github.com/jaraco/keyring), and
[tomli-w](https://github.com/hukkin/tomli-w).

## Documentation

| File | What it holds |
|---|---|
| [`docs/okta-authn-api-experiment.md`](docs/okta-authn-api-experiment.md) | Details of the browser-free Okta login |
| [`docs/okta-auth-manual.md`](docs/okta-auth-manual.md) | Okta reference details discovered during development: Classic AuthN vs OIE, push MFA, the full browser-free login recipe |
| [`docs/floorsense-api-manual.md`](docs/floorsense-api-manual.md) | Floorsense API reference written as part of the dvelopment of this tool: session model, endpoints, data shapes, business rules |
| [`docs/floorplan-map-manual.md`](docs/floorplan-map-manual.md) | `fs map`'s data sources, per-floor rendering approach, and details of what worked and didn't |

## Status

Under active development.

## Credits

Author: Mike Gould — initial version August 2026
