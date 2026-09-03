"""`FS_NO_TTY` -- an escape hatch that forces `keyread.capable()` to
`False` regardless of the real terminal, so the plain `readline()`
fallback (what Windows takes today, and what any non-tty stdin takes) can
be exercised deliberately from a real terminal, without faking a whole
non-tty environment."""

import pytest

from fs_cli import keyread


class RealTtyStdin:
    def fileno(self):
        return 0


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("FS_NO_TTY", raising=False)


@pytest.fixture
def genuinely_capable(monkeypatch):
    """Makes `keyread.capable()` see a real, `termios`-manageable
    terminal on fd 0 -- pytest's own stdin usually isn't one, so without
    this a `capable()` test would pass for the wrong reason (the real fd
    already failing `os.isatty`/`tcgetattr`, not `FS_NO_TTY`)."""
    monkeypatch.setattr(keyread.os, "isatty", lambda fd: True)
    monkeypatch.setattr(keyread.termios, "tcgetattr", lambda fd: [0] * 6 + [[0] * 32])


def test_capable_is_false_when_fs_no_tty_is_set_even_over_a_real_terminal(
        monkeypatch, genuinely_capable):
    monkeypatch.setenv("FS_NO_TTY", "1")
    assert keyread.capable(RealTtyStdin()) is False


def test_capable_is_unaffected_when_fs_no_tty_is_unset(genuinely_capable):
    assert keyread.capable(RealTtyStdin()) == 0


class ScriptedStdin(RealTtyStdin):
    """A `RealTtyStdin` that also serves scripted lines -- the shape
    `read_key`/`read_choice` need once `FS_NO_TTY` pushes them onto the
    `readline()` path."""

    def __init__(self, line):
        self._line = line

    def readline(self):
        return self._line


def test_read_key_falls_back_to_readline_when_fs_no_tty_is_set(
        monkeypatch, genuinely_capable):
    monkeypatch.setenv("FS_NO_TTY", "1")
    assert keyread.read_key(ScriptedStdin("up\n")) == "up"


def test_read_choice_falls_back_to_readline_when_fs_no_tty_is_set(
        monkeypatch, genuinely_capable):
    monkeypatch.setenv("FS_NO_TTY", "1")

    class Out:
        def prompt(self, text):
            pass

    assert keyread.read_choice(ScriptedStdin("y\n"), Out(),
                               immediate=("y", "n")) == "y"
