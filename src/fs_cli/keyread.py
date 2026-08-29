"""Single-keypress reading for `plan.py`'s prompts.

On a real terminal in canonical (line-buffered) mode, nothing reaches the
process until Enter is pressed -- not a UI choice but what the terminal
itself delivers, so a plain `stdin.readline()` would force `y`/`n`/`q`/Esc
to all need a trailing Enter, with a lone Esc keypress sitting in the
terminal's line buffer showing up as a literal `^]`/`^[` glyph until then.

`read_choice` below switches the terminal into cbreak mode (character-at-a-
time, no local echo) for the duration of one read, so a single-key answer
(`y`, `n`, `q`, Esc, or a bare Enter) can act the instant it's pressed. A
digit still has to accumulate into a full line before it means anything --
`fs book`'s multi-row prompt takes "1 2 3", not one row at a time -- so
typing a digit switches this same read into ordinary line-editing (echoed
back one character at a time, since termios' own echo is off) until Enter.

Falls back to a plain `stdin.readline()` whenever the terminal can't be put
in cbreak mode at all: not a real, `termios`-capable file descriptor (a
`StringIO` or a test double with no `fileno()`), or genuinely not a
terminal (`os.isatty` false). This is deliberately the SAME check
`_is_interactive` elsewhere in `plan.py` approximates via `isatty()` alone
-- but here it's not enough on its own, since a test double can answer
`isatty() -> True` (to exercise the interactive-prompt code paths) while
still being an `io.StringIO`-shaped object `termios` cannot touch. Every
existing test's `ScriptedStdin` is exactly this shape, and keeps working
via the fallback without needing to know this module exists.
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

__all__ = ["read_choice", "read_key", "capable"]

#: How long to wait, after a lone `\x1b` byte, for more bytes before
#: deciding it really was a bare Esc keypress rather than the first byte of
#: an arrow-key (or other) escape sequence -- a real terminal sends the rest
#: of that sequence within microseconds of the Esc byte, so this only ever
#: adds a perceptible delay to an actual bare Esc, which is the case where
#: there's nothing more coming to wait for anyway.
_ESC_GRACE_S = 0.05

_ARROW_BY_FINAL_BYTE = {"A": "up", "B": "down", "C": "right", "D": "left"}


def _decode_mouse_params(params, pressed):
    """The SGR mouse report body -- everything between the sequence's
    leading `<` and its terminating `M`/`m` -- as
    `("mouse", button, col, row, pressed)`, `col`/`row` 1-based exactly as
    the terminal reports them. `map_cmd._run_live` does the grid-coordinate
    translation (header lines, crop offset); this only parses the wire
    format. Malformed params (should never happen from a real terminal)
    return `None`, folded into "unrecognised escape sequence" by the
    caller same as any other."""
    parts = params.split(";")
    if len(parts) != 3:
        return None
    try:
        button, col, row = (int(p) for p in parts)
    except ValueError:
        return None
    return ("mouse", button, col, row, pressed)


def _decode_escape_sequence(read_byte):
    """Given `read_byte()` -- a callable returning the next available raw
    byte (or `b""` if nothing is pending) -- decode the bytes following a
    leading `\\x1b` already consumed by the caller into one of:
    a direction (`"up"`/`"down"`/`"left"`/`"right"`) for a bare arrow key,
    `"pgup"`/`"pgdn"` for Page Up/Down (`\\x1b[5~`/`\\x1b[6~`), an SGR mouse
    report as `("mouse", button, col, row, pressed)` (see
    `_decode_mouse_params`), or `None` if what follows isn't a recognised
    sequence (a bare Esc, or some other escape sequence this reader
    doesn't need to understand). Pure given its byte source --
    independently testable with a fake queue, no real fd needed.

    Reads one byte at a time until it hits a non-parameter byte (anything
    other than a digit, `;`, or the mouse report's leading `<`) -- that
    byte is the sequence's final/terminator byte. A bare arrow key has no
    parameter bytes at all (the very first byte read here already IS the
    final byte), which is why the empty-params branch is the plain
    `_ARROW_BY_FINAL_BYTE` lookup this function always did."""
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
    `readline()` path. Public (not `_fd_capable`) so callers outside this
    module -- `map_cmd.cmd_map`'s live/static branch -- can reuse the
    exact same check rather than duplicating it."""
    if termios is None or tty is None:
        return False
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
    (lowercased already), a bare Esc (`"\\x1b"`), a bare Enter (`""`), or an
    accumulated line ending in Enter for anything else (a digit sequence).

    Returns `None` on EOF -- callers raise `UserRejected` on that.

    `out.prompt` (not a raw `stdout.write`) does the character echo while
    accumulating a line, matching every other line this module's caller
    writes and keeping `--json`'s "no prompt at all" contract intact should
    this ever be reached in that mode (it currently can't be -- `confirm`
    already raises before reading any input under `--json` -- but `prompt`
    being the one write path here means it stays true if that ever
    changes).

    Every branch below runs with local echo off (cbreak mode clears
    `ECHO`), and none of them writes the newline a real terminal would
    otherwise supply on its own: an immediate keypress (`y`/`n`/`a`/`q`,
    bare Esc, bare Enter) is never echoed at all, and even the accumulated-
    digit-line path only echoes the digits themselves (`out.prompt(ch)`
    above), never the Enter that submits it. Left alone, whatever the
    caller prints right after `confirm()` returns lands appended to the
    prompt line instead of starting its own -- so every non-EOF return
    here writes one `\n` first. The EOF (`None`) return doesn't: there was
    no answer to echo, and the caller's `UserRejected` message gets its
    own line regardless.
    """
    fd = capable(stdin)
    if fd is False:
        line = stdin.readline()
        return None if line == "" else line.strip().lower()

    old = termios.tcgetattr(fd)
    try:
        # `TCSANOW`, not `tty.setcbreak`'s own `TCSAFLUSH` default (nor
        # `TCSADRAIN`) -- both of those wait for this fd's pending OUTPUT to
        # finish transmitting before applying the change, and `confirm()`
        # always writes the prompt line immediately before calling this
        # function. That combination reproducibly hung forever on a pty
        # whose far end (the pty master, here) hadn't drained anything yet
        # -- `TCSANOW` applies the mode change immediately with no such
        # wait, which is also the more correct choice: nothing about
        # switching into cbreak mode needs to wait on prior output at all.
        tty.setcbreak(fd, termios.TCSANOW)
        no_echo = termios.tcgetattr(fd)
        no_echo[3] &= ~termios.ECHO         # lflags
        termios.tcsetattr(fd, termios.TCSANOW, no_echo)

        buf = []
        # A fresh decoder per call, fed one raw byte at a time: `decode()`
        # returns `""` while a multi-byte UTF-8 sequence is still assembling
        # and the full character only once the last continuation byte
        # arrives. Decoding each raw byte independently with
        # `bytes.decode(errors="replace")` instead would treat every
        # continuation byte as invalid on its own, turning one typed
        # non-ASCII character (an accented name at a `pick_one` prompt)
        # into 2-3 U+FFFD replacement glyphs.
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
                    # The first byte of some longer escape sequence (an
                    # arrow key, most likely) -- drain and discard the rest
                    # of IT ONLY, a byte at a time, stopping the instant
                    # nothing more is immediately pending. A single
                    # `os.read(fd, 8)` here would over-read: a real arrow
                    # key's terminal-generated bytes (`\x1b[A`, 2 more) all
                    # land in one burst with the ESC, but so can a genuinely
                    # separate keystroke typed a moment later if it beats
                    # this function back to the fd -- capping the drain to
                    # "still arriving with no gap" is what keeps that next,
                    # unrelated keystroke from being silently eaten too.
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
        # Same reasoning as the `TCSANOW` above -- restoring canonical mode
        # must not wait on a drain either.
        termios.tcsetattr(fd, termios.TCSANOW, old)


def read_key(stdin):
    """One raw keypress for `map_cmd._run_live`'s redraw loop: `"up"`,
    `"down"`, `"left"`, `"right"` for an arrow key, `"pgup"`/`"pgdn"` for
    Page Up/Down, `("mouse", button, col, row, pressed)` for an SGR mouse
    report (see `_decode_mouse_params`), `"enter"` for a bare Enter,
    `"esc"` for a bare Esc, or the lowercased single character typed
    otherwise (`"q"`, ...). Returns `None` on EOF.

    Shares `read_choice`'s cbreak-mode plumbing (same `TCSANOW`
    reasoning, same `capable()` fd guard, same per-call `try/finally`
    restore to canonical mode) but, unlike `read_choice`, DECODES an
    escape sequence into a direction (or a page/mouse event) instead of
    draining and discarding it -- `_run_live` needs to know which key or
    click it was, where `read_choice`'s prompts never do.

    Falls back to `stdin.readline()` when `capable()` is `False`,
    mapping an empty line to `"enter"`, a lone `\\x1b` to `"esc"`, and
    anything else to its stripped, lowercased text -- the non-terminal
    test-double path (`map_cmd`'s own callers only reach this function
    after already checking `keyread.capable(stdin)` themselves, so this
    branch is exercised by tests, not by `cmd_map` at runtime).
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
