#!/usr/bin/env bash
# Builds the distributable `fs` -- a single extensionless `shiv` zipapp with
# its dependencies bundled. See README.md's "Running the build" section
# for why shiv and not `uv tool install` or a PyInstaller-style standalone
# binary: this stays
# small because it bundles only this project's Python dependencies
# (requests, keyring, tomli-w), not a whole interpreter -- the tradeoff is
# that it still needs a `python3` already on the user's PATH at run time.
#
# Two portability traps found by actually running the built artifact on a
# different interpreter than the one that built it -- neither shows up
# testing on the build machine alone:
#
#   1. MUST build with python3.11, the floor of `requires-python`, not
#      whatever `python3` happens to resolve to. pip resolves a dependency's
#      version markers against the *build* interpreter, not the one the
#      zipapp eventually runs under. `keyring` -> `jaraco.context` ->
#      `backports.tarfile` is declared `python_version < "3.12"`; building
#      on 3.13 correctly omits it for 3.13, but the resulting bundle then
#      hard-fails importing `keyring` (and so every command) on 3.11/3.12,
#      which this project claims to support. Building on the floor version
#      pulls it in; it's a harmless no-op extra on newer interpreters.
#   2. MUST force `charset_normalizer` to build from source
#      (`--no-binary`). Its wheel ships a compiled Cython extension
#      (`.cpython-3XX-<platform>.so`) -- fine on the exact interpreter/OS
#      that built it, a hard ImportError on any other one, which breaks the
#      "same file on macOS/Linux/Windows" claim this exists to make true.
#      Its `setup.py` only compiles the extension when
#      `CHARSET_NORMALIZER_USE_CYTHON=1` is set (it isn't here), so building
#      from sdist gives the pure-Python fallback it already ships.
#
# Verify both after any dependency bump:
#   unzip -l dist/fs | grep -i backports      # must be non-empty
#   unzip -l dist/fs | grep -E '\.(so|pyd|dylib)$'   # must be empty
#
# Uncompressed by design (not `shiv`'s default): a compressed zipapp has to
# unzip itself into a cache dir on first run, adding a one-time ~100-200ms
# stall the first time each Python/shiv-hash combination is seen. Trading a
# larger file for that being avoided entirely was the choice made when this
# was speced out.
#
# Run from anywhere; always builds from this checkout, not whatever is on
# PATH. Uses its own venv (build/.venv311) pinned to python3.11, separate
# from whatever dev venv you're using day to day -- so the build never
# silently rides in on a newer interpreter you happen to have active.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if ! command -v python3.11 >/dev/null 2>&1; then
    echo "python3.11 not found -- the build must run on requires-python's" >&2
    echo "floor version (see this script's header for why). Install it, e.g.:" >&2
    echo "  brew install python@3.11" >&2
    exit 1
fi

build_venv="$repo_root/build/.venv311"
if [[ ! -x "$build_venv/bin/python3" ]]; then
    mkdir -p "$repo_root/build"
    python3.11 -m venv "$build_venv"
fi
"$build_venv/bin/pip" install -q -e '.[dev]'

mkdir -p dist
"$build_venv/bin/python3" -m shiv \
    --entry-point fs_cli.cli:main \
    --output-file dist/fs \
    --python "/usr/bin/env python3" \
    --uncompressed \
    --no-binary charset_normalizer \
    "$repo_root"

echo "built dist/fs"
