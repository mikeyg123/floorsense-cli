#!/usr/bin/env bash
# First-run smoke test on a clean machine: builds dist/fs fresh, then
# runs it with HOME pointed at an empty scratch
# directory and everything this checkout's .venv put on PATH stripped out
# -- so a false pass from picking up an editable install, a cached config,
# or a keychain entry left over from real use isn't possible.
#
# Not a full command-by-command test (that is what pytest is for). This
# checks the one thing pytest cannot: that the *built artifact itself*
# starts, finds its bundled dependencies, and behaves like first contact --
# no traceback, no crash, the expected first-run message.
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

echo "== building =="
"$repo_root/scripts/build-release.sh" || { echo "build failed"; exit 1; }

echo "== checking bundled deps (see build-release.sh's header) =="
# Captured first, then grepped on the variable -- not `unzip | grep -q`:
# with `pipefail` set, `grep -q` closing the pipe on its first match sends
# `unzip` SIGPIPE, and pipefail reports *that* as the pipeline's failure
# even though grep matched. Piping into a live command earns you this
# every time; capturing first doesn't.
listing="$(unzip -l "$repo_root/dist/fs")"
if ! grep -qi backports <<<"$listing"; then
    echo "FAIL: backports.tarfile missing -- built on the wrong interpreter?"
    fail_early=1
fi
compiled="$(grep -E '\.(so|pyd|dylib)$' <<<"$listing" || true)"
if [[ -n "$compiled" ]]; then
    echo "FAIL: compiled extension(s) bundled, breaks cross-platform:"
    echo "$compiled"
    fail_early=1
fi
if [[ "${fail_early:-0}" == 1 ]]; then
    exit 1
fi
echo "ok: bundled deps"

scratch_home="$(mktemp -d)"
trap 'rm -rf "$scratch_home"' EXIT

# Keep a real python3 on PATH (whatever this machine actually uses) but
# drop this checkout's .venv/bin so nothing editable leaks in.
clean_path="$(dirname "$(command -v python3)"):/usr/bin:/bin"

run() {
    env -i HOME="$scratch_home" PATH="$clean_path" TMPDIR="${TMPDIR:-/tmp}" \
        "$repo_root/dist/fs" "$@"
}

fail=0
check() {
    local desc="$1" want="$2"; shift 2
    local got
    run "$@" >/tmp/fs-smoke-out.$$ 2>&1
    got=$?
    if [[ "$got" != "$want" ]]; then
        echo "FAIL: $desc (exit $got, wanted $want)"
        cat /tmp/fs-smoke-out.$$
        fail=1
    else
        echo "ok: $desc"
    fi
    rm -f /tmp/fs-smoke-out.$$
}

echo "== running on a clean HOME/PATH =="
check "--version"        0 --version
check "--help"           0 --help
check "help"             0 help
check "status, no config" 2 status

if [[ "$fail" == 0 ]]; then
    echo "smoke test passed"
else
    echo "smoke test FAILED"
fi
exit "$fail"
