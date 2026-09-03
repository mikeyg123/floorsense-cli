"""`keyread_windows` -- the `msvcrt` backend `keyread.py` delegates to
when `termios` isn't importable (Windows). No real Windows box in CI, so
every test drives it through a fake `msvcrt` swapped onto the module,
mirroring how `test_keyread.py`/the module itself fake out `termios`.
"""

import pytest

from fs_cli import keyread_windows


class FakeMsvcrt:
    """Feeds `getwch()` one queued character at a time; `isatty`-style
    console-availability is implied by `keyread_windows.msvcrt` being
    non-`None` at all, matching the real module (no separate check)."""

    def __init__(self, chars):
        self._chars = list(chars)

    def getwch(self):
        return self._chars.pop(0)


class Out:
    def __init__(self):
        self.written = []

    def prompt(self, text):
        self.written.append(text)


class ScriptedStdin:
    """`isatty() -> True` with no `fileno()` at all -- the shape every
    `ScriptedStdin`/`StringIO` test double elsewhere in this suite has.
    `capable()` must not be fooled by this the way it can't be on POSIX
    (see `keyread.capable`'s own docstring) -- `msvcrt.getwch()` reads
    the real console regardless of what `stdin` is, so a scripted test
    stdin passing this check would hang a test on a real Windows box
    reading from nothing a test controls."""

    def __init__(self, is_tty=True):
        self._is_tty = is_tty

    def isatty(self):
        return self._is_tty


class RealConsoleStdin:
    """The shape real interactive stdin has: `isatty()` and a `fileno()`
    that `os.isatty()` also affirms."""

    def __init__(self, fd, is_tty=True):
        self._fd = fd
        self._is_tty = is_tty

    def fileno(self):
        return self._fd


@pytest.fixture(autouse=True)
def restore_msvcrt():
    original = keyread_windows.msvcrt
    yield
    keyread_windows.msvcrt = original


def test_capable_is_false_when_msvcrt_is_unavailable():
    keyread_windows.msvcrt = None
    assert keyread_windows.capable(RealConsoleStdin(0)) is False


def test_capable_is_false_for_a_stdin_with_no_fileno():
    """A `ScriptedStdin`/`StringIO`-shaped double answering `isatty() ->
    True` with no real fd must not pass -- see `ScriptedStdin`'s
    docstring above for why."""
    keyread_windows.msvcrt = FakeMsvcrt([])
    assert keyread_windows.capable(ScriptedStdin(is_tty=True)) is False


def test_capable_is_false_when_the_real_fd_is_not_a_tty(monkeypatch):
    keyread_windows.msvcrt = FakeMsvcrt([])
    monkeypatch.setattr(keyread_windows.os, "isatty", lambda fd: False)
    assert keyread_windows.capable(RealConsoleStdin(0)) is False


def test_capable_is_true_for_a_real_console_fd(monkeypatch):
    keyread_windows.msvcrt = FakeMsvcrt([])
    monkeypatch.setattr(keyread_windows.os, "isatty", lambda fd: True)
    assert keyread_windows.capable(RealConsoleStdin(0)) is True


def test_read_key_decodes_the_four_arrow_keys():
    for scan, direction in ((72, "up"), (80, "down"), (75, "left"), (77, "right")):
        keyread_windows.msvcrt = FakeMsvcrt(["\xe0", chr(scan)])
        assert keyread_windows.read_key() == direction


def test_read_key_decodes_pgup_and_pgdn():
    keyread_windows.msvcrt = FakeMsvcrt(["\xe0", chr(73)])
    assert keyread_windows.read_key() == "pgup"
    keyread_windows.msvcrt = FakeMsvcrt(["\xe0", chr(81)])
    assert keyread_windows.read_key() == "pgdn"


def test_read_key_treats_a_00_prefix_the_same_as_e0():
    """Older BIOS/console layers prefix function/arrow keys with `\\x00`
    instead of `\\xe0` -- `msvcrt` docs note both occur in practice."""
    keyread_windows.msvcrt = FakeMsvcrt(["\x00", chr(72)])
    assert keyread_windows.read_key() == "up"


def test_read_key_skips_an_unmapped_extended_key_and_reads_the_next_one():
    """F1 (scan 59) has no token in this vocabulary -- must not be
    mistaken for EOF/quit; the loop reads on to the next real key."""
    keyread_windows.msvcrt = FakeMsvcrt(["\xe0", chr(59), "q"])
    assert keyread_windows.read_key() == "q"


def test_read_key_returns_enter_for_a_bare_enter():
    keyread_windows.msvcrt = FakeMsvcrt(["\r"])
    assert keyread_windows.read_key() == "enter"


def test_read_key_returns_esc_for_a_bare_esc():
    keyread_windows.msvcrt = FakeMsvcrt(["\x1b"])
    assert keyread_windows.read_key() == "esc"


def test_read_key_returns_the_lowercased_character_otherwise():
    keyread_windows.msvcrt = FakeMsvcrt(["Q"])
    assert keyread_windows.read_key() == "q"


def test_read_key_returns_none_for_ctrl_c():
    """`msvcrt.getwch()` doesn't check for keyboard interrupts (CPython
    docs) -- Ctrl+C arrives as a literal `\\x03` character, not a raised
    `KeyboardInterrupt`. Without this, `_run_live`'s loop (which quits on
    `None`/`"q"`/`"esc"`) has no way out of the alt-screen on Ctrl+C."""
    keyread_windows.msvcrt = FakeMsvcrt(["\x03"])
    assert keyread_windows.read_key() is None


def test_read_choice_returns_an_immediate_keypress_without_enter():
    keyread_windows.msvcrt = FakeMsvcrt(["y"])
    out = Out()
    assert keyread_windows.read_choice(out, immediate=("y", "n")) == "y"


def test_read_choice_returns_a_bare_esc():
    keyread_windows.msvcrt = FakeMsvcrt(["\x1b"])
    out = Out()
    assert keyread_windows.read_choice(out, immediate=("y", "n")) == "\x1b"


def test_read_choice_returns_an_empty_string_for_a_bare_enter():
    keyread_windows.msvcrt = FakeMsvcrt(["\r"])
    out = Out()
    assert keyread_windows.read_choice(out, immediate=("y", "n")) == ""


def test_read_choice_accumulates_digits_until_enter():
    keyread_windows.msvcrt = FakeMsvcrt(list("1 2\r"))
    out = Out()
    assert keyread_windows.read_choice(out, immediate=("y", "n")) == "1 2"


def test_read_choice_backspace_edits_the_accumulating_line():
    keyread_windows.msvcrt = FakeMsvcrt(list("12\x083\r"))
    out = Out()
    assert keyread_windows.read_choice(out, immediate=("y", "n")) == "13"


def test_read_choice_ignores_an_extended_key_and_keeps_accumulating():
    keyread_windows.msvcrt = FakeMsvcrt(["1", "\xe0", chr(72), "2", "\r"])
    out = Out()
    assert keyread_windows.read_choice(out, immediate=("y", "n")) == "12"


def test_read_choice_returns_none_for_ctrl_c():
    """Same reasoning as `read_key`'s Ctrl+C test -- `plan.py` treats a
    `None` return as EOF and raises `UserRejected`."""
    keyread_windows.msvcrt = FakeMsvcrt(["\x03"])
    out = Out()
    assert keyread_windows.read_choice(out, immediate=("y", "n")) is None
