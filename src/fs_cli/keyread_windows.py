"""`msvcrt`-based single-keypress reading -- `keyread.py`'s sibling for
Windows, where `termios`/`tty` don't exist. Keyboard-only: Windows console
mouse events don't reach `msvcrt` at all (a separate Win32
console-input-buffer API), so there's no mouse-click desk selection here,
just arrow/PgUp/PgDn/Enter/Esc nav (see docs/windows-interactive-map-plan.md).

`msvcrt.getwch()` reads straight from the console, ignoring whatever `stdin`
object is passed around elsewhere in this codebase, and needs no cbreak-mode
setup/teardown (no local echo, no line buffering, by default) -- so unlike
`keyread.py` there's no fd/termios-state plumbing to carry between calls.

An arrow/function/nav key isn't an escape sequence here the way it is over
a real terminal -- it's a literal `\\x00` or `\\xe0` byte followed by a scan
code, both returned from separate `getwch()` calls. That also means a lone
Esc (`\\x1b`) is never ambiguous with a multi-byte sequence, so -- unlike
`keyread.read_choice`'s `_ESC_GRACE_S` drain -- nothing here needs to wait
and see what follows it.

`msvcrt.getwch()` also doesn't check for keyboard interrupts (CPython
docs) -- Ctrl+C arrives as a literal `\\x03` byte, not a raised
`KeyboardInterrupt`, so both functions below treat it (and `\\x04`) as
EOF/quit explicitly; POSIX cbreak mode leaves `ISIG` on and gets a real
`SIGINT` instead.
"""

import os

try:
    import msvcrt
except ImportError:                      # pragma: no cover -- non-Windows
    msvcrt = None

__all__ = ["capable", "read_choice", "read_key"]

#: Scan codes following a `\x00`/`\xe0` prefix byte, for the keys
#: `map_cmd._run_live` actually acts on. Anything else in that family
#: (F-keys, Home/End/Insert/Delete, ...) is read and discarded.
_ARROW_BY_SCAN = {72: "up", 80: "down", 75: "left", 77: "right",
                  73: "pgup", 81: "pgdn"}


def capable(stdin):
    """Whether `msvcrt` keyboard reads are usable here at all -- `msvcrt`
    importable, and `stdin` an actual console (matches `keyread.capable`'s
    role as the live/static-view gate, even though `stdin` itself is never
    touched again below).

    Checks `stdin`'s real fd via `os.isatty`, not just `stdin.isatty()` --
    same reasoning as the POSIX `capable()`'s own docstring: a test double
    can answer `isatty() -> True` while having no real fd at all, and
    `msvcrt.getwch()` below reads the actual console regardless of what
    `stdin` is, so trusting a scripted `isatty()` here would hang reading
    from a real console instead of the fake input a test controls."""
    if msvcrt is None:
        return False
    try:
        fd = stdin.fileno()
    except (AttributeError, OSError, ValueError):
        return False
    try:
        return os.isatty(fd)
    except (OSError, ValueError):
        return False


def read_choice(out, immediate=()):
    """Windows counterpart to `keyread.read_choice` -- same return
    contract (an immediate key from `immediate`, a bare Esc/Enter, or an
    accumulated line), driven by `msvcrt.getwch()` instead of raw
    termios/`os.read`."""
    buf = []
    while True:
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            msvcrt.getwch()              # discard the scan code -- N/A here
            continue
        if ch in ("\x03", "\x04"):       # Ctrl+C -- see module docstring
            return None
        if ch == "\x1b":
            out.prompt("\n")
            return "\x1b"
        if ch in ("\r", "\n"):
            out.prompt("\n")
            return "".join(buf).strip().lower()
        if ch == "\x08":                 # backspace
            if buf:
                buf.pop()
                out.prompt("\b \b")
            continue
        if not buf and ch.lower() in immediate:
            out.prompt("\n")
            return ch.lower()
        buf.append(ch)
        out.prompt(ch)


def read_key():
    """Windows counterpart to `keyread.read_key`: a direction,
    `"pgup"`/`"pgdn"`, `"enter"`, `"esc"`, or the lowercased character
    typed otherwise. No mouse tuple -- see module docstring."""
    while True:
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            token = _ARROW_BY_SCAN.get(ord(msvcrt.getwch()))
            if token is not None:
                return token
            continue                     # an unmapped nav/function key
        if ch in ("\x03", "\x04"):       # Ctrl+C -- see module docstring
            return None
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x1b":
            return "esc"
        return ch.lower()
