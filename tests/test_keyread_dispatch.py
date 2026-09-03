"""`keyread.py`'s dispatch onto `keyread_windows` when `termios` isn't
importable -- pins the exact condition Windows hits (`keyread.termios is
None`), per docs/windows-interactive-map-plan.md's testing note. Runs on
any platform by monkeypatching `keyread.termios`/`keyread.tty` directly,
same trick the module's own `except ImportError` fallback relies on.
"""

import io

import pytest

from fs_cli import keyread, keyread_windows


class Out:
    def __init__(self):
        self.written = []

    def prompt(self, text):
        self.written.append(text)


class ScriptedStdin:
    def fileno(self):
        return 0


@pytest.fixture(autouse=True)
def force_no_termios(monkeypatch):
    monkeypatch.setattr(keyread, "termios", None)
    monkeypatch.setattr(keyread, "tty", None)
    # `keyread_windows.capable` checks the real fd, not `stdin.isatty()`
    # (see its docstring) -- fake that check too so these dispatch tests
    # don't depend on the test runner's own fd 0 being a tty.
    monkeypatch.setattr(keyread_windows.os, "isatty", lambda fd: True)


def test_capable_is_false_when_termios_and_msvcrt_are_both_unavailable():
    """The condition every non-Windows CI box actually hits: `termios`
    forced off (as on Windows), but this machine has no `msvcrt` either --
    must fall back to `False`, same as before this dispatch existed."""
    assert keyread.capable(io.StringIO()) is False


def test_capable_delegates_to_the_windows_backend(monkeypatch):
    monkeypatch.setattr(keyread_windows, "msvcrt", object())
    assert keyread.capable(ScriptedStdin()) is True


def test_read_key_delegates_to_the_windows_backend(monkeypatch):
    class FakeMsvcrt:
        def getwch(self):
            return "q"

    monkeypatch.setattr(keyread_windows, "msvcrt", FakeMsvcrt())
    assert keyread.read_key(ScriptedStdin()) == "q"


def test_read_choice_delegates_to_the_windows_backend(monkeypatch):
    class FakeMsvcrt:
        def getwch(self):
            return "y"

    monkeypatch.setattr(keyread_windows, "msvcrt", FakeMsvcrt())
    assert keyread.read_choice(ScriptedStdin(), Out(), immediate=("y", "n")) == "y"
