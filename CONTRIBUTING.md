# Contributing

Thanks for considering a contribution to `fs`.

## Before you start

For anything beyond a small fix, open an issue first to discuss the change —
this is a small, opinionated CLI and not every feature request is a good
fit. See `CLAUDE.md` for the architecture and the reasoning behind how the
codebase is laid out; most modules carry a docstring explaining *why*
they're shaped the way they are, so read it before changing behavior rather
than guessing.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]'
```

## Before opening a PR

```bash
ruff check .
pytest
```

Both run in CI (`.github/workflows/ci.yml`) against every PR to `main`, and
a PR won't be merged if either fails.

- Add or update tests for behavior changes — see "Testing" in `CLAUDE.md`
  for how fixtures and the loopback auth server work.
- Keep comments/docstrings minimal: only note gotchas (an API quirk, a bug
  being worked around), not what the code visibly does.
- Don't run `scripts/probes/write_probe.py` — it makes real bookings
  against a live account and is for the maintainer's own verification only.

## Pull requests

- Target `main`.
- Keep PRs focused; unrelated cleanup makes review slower, not faster.
- One of the GitHub Actions workflows (`release.yml`) is manual-dispatch
  only and requires maintainer approval to run — you won't trigger a
  release build from a PR.

## Reporting bugs / requesting features

Use [GitHub Issues](https://github.com/mikeyg123/floorsense-cli/issues).
For security issues, see [SECURITY.md](SECURITY.md) instead — please don't
open a public issue for those.
