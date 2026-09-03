"""Single-keypress reading for `plan.py`'s prompts.

On a real terminal in canonical (line-buffered) mode, nothing reaches the
process until Enter is pressed -- so a plain `stdin.readline()` would
force `y`/`n`/`q`/Esc to all need a trailing Enter, with a lone Esc
keypress showing up as a literal `^]`/`^[` glyph until then.

`read_choice` switches the terminal into cbreak mode (character-at-a-
time, no local echo) for one read, so `y`/`n`/`q`/Esc/Enter act the
instant they're pressed. A digit still accumulates into a full line
(`fs book`'s prompt takes "1 2 3", not one row at a time), so typing a
digit switches into ordinary line-editing until Enter.

Falls back to a plain `stdin.readline()` whenever the terminal can't be
put in cbreak mode: not a real `termios`-capable fd, or genuinely not a
terminal. This is stricter than `plan.py`'s own `isatty()`-only check --
a test double can answer `isatty() -> True` while still being an
`io.StringIO` `termios` can't touch; every `ScriptedStdin` in the test
suite is exactly this shape.
"""

import codecs
import os
import select

try:
    import termios
    import tty
except ImportError:                      # pragma: no cover -- non-POSIX
    termios = None
    tty = None

from . import keyread_windows

__all__ = ["read_choice", "read_key", "capable"]

#: How long to wait, after a lone `\x1b` byte, before deciding it really
#: was a bare Esc rather than the first byte of an escape sequence -- a
#: real terminal sends the rest within microseconds.
_ESC_GRACE_S = 0.05

_ARROW_BY_FINAL_BYTE = {"A": "up", "B": "down", "C": "right", "D": "left"}


def _decode_mouse_params(params, pressed):
    """The SGR mouse report body as `("mouse", button, col, row,
    pressed)`, `col`/`row` 1-based as the terminal reports them.
    `map_cmd._run_live` does the grid-coordinate translation; this only
    parses the wire format. Malformed params return `None`."""
    parts = params.split(";")
    if len(parts) != 3:
        return None
    try:
        button, col, row = (int(p) for p in parts)
    except ValueError:
        return None
    return ("mouse", button, col, row, pressed)


def _decode_escape_sequence(read_byte):
    """Given `read_byte()` -- returns the next raw byte, or `b""` if
    nothing pending -- decode the bytes following a leading `\\x1b`
    already consumed by the caller into a direction, `"pgup"`/`"pgdn"`,
    an SGR mouse report, or `None` if unrecognised. Pure given its byte
    source -- testable with a fake queue, no real fd needed.

    Reads one byte at a time until a non-parameter byte (not a digit,
    `;`, or the mouse report's leading `<`) -- that's the terminator. A
    bare arrow key has no parameter bytes at all, so the first byte read
    already IS the terminator."""
    first = read_byte()
    if first != b"[":
        return None
    params = ""
    while True:
        raw = read_byte()
        if not raw:
            return None
        ch = raw.decode("ascii", errors="replace")
        if ch.isdigit() or ch in ";<":
            params += ch
            continue
        final = ch
        break
    if not params:
        return _ARROW_BY_FINAL_BYTE.get(final)
    if params == "5" and final == "~":
        return "pgup"
    if params == "6" and final == "~":
        return "pgdn"
    if params.startswith("<") and final in ("M", "m"):
        return _decode_mouse_params(params[1:], final == "M")
    return None


def capable(stdin):
    """Whether `stdin` is a real, `termios`-manageable terminal fd -- the
    guard that keeps every non-interactive/test stdin on the plain
    `readline()` path. Public so `map_cmd.cmd_map`'s live/static branch
    can reuse the exact same check.

    No `termios` at all (Windows) delegates to `keyread_windows.capable`
    instead of going straight to `False` -- its return is a bare `True`,
    not an fd, so callers below branch on `termios is None` rather than
    trusting this return value's type to tell POSIX and Windows apart.

    `FS_NO_TTY` (any non-empty value) forces `False` unconditionally --
    an escape hatch to exercise the plain `readline()` fallback (what
    Windows takes today, and what any non-tty stdin takes) from a real,
    otherwise-capable terminal, without faking a whole environment."""
    if os.environ.get("FS_NO_TTY"):
        return False
    if termios is None or tty is None:
        return keyread_windows.capable(stdin)
    try:
        fd = stdin.fileno()
    except (AttributeError, OSError, ValueError):
        return False
    try:
        if not os.isatty(fd):
            return False
        termios.tcgetattr(fd)
    except (termios.error, ValueError):
        return False
    return fd


def read_choice(stdin, out, immediate=()):
    """One prompt answer: a single immediate keypress from `immediate`
    (lowercased), a bare Esc, a bare Enter, or an accumulated line ending
    in Enter for anything else (a digit sequence). Returns `None` on EOF
    -- callers raise `UserRejected` on that.

    `out.prompt` (not raw `stdout.write`) does the character echo,
    keeping `--json`'s "no prompt at all" contract intact should this
    ever be reached in that mode.

    Every branch runs with local echo off, and none writes the newline a
    real terminal would otherwise supply -- so every non-EOF return
    writes one `\n` first, or whatever the caller prints next lands
    appended to the prompt line instead of starting its own.
    """
    fd = capable(stdin)
    if fd is False:
        line = stdin.readline()
        return None if line == "" else line.strip().lower()
    if termios is None:
        return keyread_windows.read_choice(out, immediate=immediate)

    old = termios.tcgetattr(fd)
    try:
        # `TCSANOW`, not `tty.setcbreak`'s own `TCSAFLUSH`/`TCSADRAIN` --
        # both wait for pending OUTPUT to drain first, and `confirm()`
        # always writes the prompt line right before calling this. That
        # combination reproducibly hung on a pty whose far end hadn't
        # drained yet.
        tty.setcbreak(fd, termios.TCSANOW)
        no_echo = termios.tcgetattr(fd)
        no_echo[3] &= ~termios.ECHO         # lflags
        termios.tcsetattr(fd, termios.TCSANOW, no_echo)

        buf = []
        # A fresh decoder per call, fed one raw byte at a time: `decode()`
        # returns `""` mid-sequence. Decoding each byte independently
        # instead would turn one typed non-ASCII character into 2-3
        # U+FFFD replacement glyphs.
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            raw = os.read(fd, 1)
            if raw == b"":
                return None
            ch = decoder.decode(raw)
            if not ch:
                continue                    # mid-sequence; wait for more bytes

            if ch == "\x1b":
                if select.select([fd], [], [], _ESC_GRACE_S)[0]:
                    # Likely an escape sequence -- drain and discard the
                    # rest, a byte at a time, stopping the instant nothing
                    # more is pending. A single `os.read(fd, 8)` would
                    # over-read: a genuinely separate keystroke typed a
                    # moment later could land in the same burst.
                    while select.select([fd], [], [], _ESC_GRACE_S)[0]:
                        if os.read(fd, 1) == b"":
                            break
                    continue
                out.prompt("\n")
                return "\x1b"

            if ch in ("\r", "\n"):
                out.prompt("\n")
                return "".join(buf).strip().lower()

            if ch in ("\x7f", "\x08"):      # backspace/delete
                if buf:
                    buf.pop()
                    out.prompt("\b \b")
                continue

            if not buf and ch.lower() in immediate:
                out.prompt("\n")
                return ch.lower()

            buf.append(ch)
            out.prompt(ch)
    finally:
        # Same reasoning as `TCSANOW` above -- restoring canonical mode
        # must not wait on a drain either.
        termios.tcsetattr(fd, termios.TCSANOW, old)


def read_key(stdin):
    """One raw keypress for `map_cmd._run_live`'s redraw loop: a
    direction, `"pgup"`/`"pgdn"`, an SGR mouse tuple, `"enter"`, `"esc"`,
    or the lowercased character typed otherwise. Returns `None` on EOF.

    Shares `read_choice`'s cbreak-mode plumbing but, unlike it, DECODES
    an escape sequence into a direction/event instead of discarding it --
    `_run_live` needs to know which key or click it was.

    Falls back to `stdin.readline()` when `capable()` is `False`,
    mapping an empty line to `"enter"`, a lone `\\x1b` to `"esc"`, and
    anything else to its stripped, lowercased text.
    """
    fd = capable(stdin)
    if fd is False:
        line = stdin.readline()
        if line == "":
            return None
        text = line.strip()
        if text == "":
            return "enter"
        if text == "\x1b":
            return "esc"
        return text.lower()
    if termios is None:
        return keyread_windows.read_key()

    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd, termios.TCSANOW)
        no_echo = termios.tcgetattr(fd)
        no_echo[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSANOW, no_echo)

        def read_byte():
            if not select.select([fd], [], [], _ESC_GRACE_S)[0]:
                return b""
            return os.read(fd, 1)

        raw = os.read(fd, 1)
        if raw == b"":
            return None
        if raw == b"\x1b":
            direction = _decode_escape_sequence(read_byte)
            return direction if direction is not None else "esc"
        if raw in (b"\r", b"\n"):
            return "enter"

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        ch = decoder.decode(raw)
        while not ch:
            more = os.read(fd, 1)
            if more == b"":
                return None
            ch = decoder.decode(more)
        return ch.lower()
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, old)
