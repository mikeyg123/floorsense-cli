"""`keyread.read_choice` against a REAL pty -- the one thing `ScriptedStdin`
(used everywhere else in `test_plan.py`) can't exercise, since it answers
`isatty() -> True` without being a `termios`-manageable file descriptor at
all. These tests launch a small child SUBPROCESS with its stdin/stdout
attached to a pty slave, write bytes into the pty master, and assert on
what came back -- the only way to prove a lone keypress (no trailing Enter)
actually returns immediately rather than sitting in a line buffer.

A subprocess, not `pty.fork()`, deliberately: forking the pytest process
itself is fragile under pytest's own capture machinery (threads and
redirected file descriptors do not survive a raw `fork()` cleanly) and
intermittently hung this suite outright. A subprocess sidesteps all of
that -- the child is a clean interpreter with nothing but this module on
its path.

Skipped outright on a platform with no `pty` module (Windows) -- `keyread`
itself already falls back to `readline()` there via its own `termios`
import guard, so there is nothing pty-specific to prove on such a
platform.
"""

import os
import pty
import subprocess
import sys
import time


SRC = os.path.join(os.path.dirname(__file__), "..", "src")

_CHILD = """
import sys
sys.path.insert(0, {src!r})
from fs_cli import keyread

class Out:
    def prompt(self, text):
        sys.stdout.write(text)
        sys.stdout.flush()

stdin = sys.stdin
result = keyread.read_choice(stdin, Out(), immediate={immediate!r})
sys.stdout.write("\\nGOT:" + repr(result) + "\\n")
sys.stdout.flush()
""".strip()


def _run_in_pty(send, immediate=("y", "n", "a", "q")):
    """Spawn the child with its stdio on a pty slave, feed `send` into the
    master once the child is up, and return everything the master read
    back."""
    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD.format(src=SRC, immediate=immediate)],
        stdin=slave_fd, stdout=slave_fd, stderr=subprocess.STDOUT,
        close_fds=True)
    os.close(slave_fd)
    try:
        time.sleep(0.3)                    # let the child reach cbreak mode
        os.write(master_fd, send)
        time.sleep(0.3)
        chunks = []
        try:
            while True:
                chunk = os.read(master_fd, 4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except OSError:
            pass
        proc.wait(timeout=5)
    finally:
        os.close(master_fd)
        if proc.poll() is None:
            proc.kill()
    return b"".join(chunks).decode()


def test_a_bare_keypress_returns_without_enter():
    assert "GOT:'y'" in _run_in_pty(b"y")


def test_a_bare_keypress_still_leaves_the_cursor_on_a_new_line():
    """Regression: cbreak mode has local echo off, so an immediate keypress
    (no Enter, nothing else typed) was never followed by anything that
    advanced the terminal to a new line -- whatever the caller printed
    right after `confirm()` returned landed appended to the prompt text
    instead of starting its own line. `read_choice` now writes that
    newline itself before returning. The child always writes its own
    leading `\\nGOT:`, so a fix shows up as back-to-back newlines here."""
    out = _run_in_pty(b"y")
    assert "\r\n\r\nGOT:'y'" in out or "\n\nGOT:'y'" in out


def test_a_bare_esc_cancels_without_enter():
    assert "GOT:'\\x1b'" in _run_in_pty(b"\x1b")


def test_a_bare_enter_returns_an_empty_choice():
    assert "GOT:''" in _run_in_pty(b"\r")


def test_digits_still_accumulate_until_enter():
    """The one case `read_choice` must NOT act on the first keystroke --
    `_confirm_many`'s "1 2 3" selection needs the whole line."""
    assert "GOT:'1 2'" in _run_in_pty(b"1 2\r")


def test_backspace_edits_the_accumulating_line():
    assert "GOT:'13'" in _run_in_pty(b"12\x7f3\r")


def test_a_multi_byte_utf8_character_decodes_as_one_character():
    """The bug this pins: reading one raw byte at a time and decoding each
    independently (`bytes.decode(errors="replace")`) treats every
    continuation byte of a multi-byte UTF-8 character as invalid on its
    own, turning one typed character into several U+FFFD replacement
    glyphs. `é` (`b"\\xc3\\xa9"`, two bytes) must accumulate as the single
    character it is."""
    assert "GOT:'é'" in _run_in_pty("é\r".encode("utf-8"))


def test_an_arrow_key_is_not_mistaken_for_a_bare_esc():
    """An arrow key sends `\\x1b` followed immediately by more bytes
    (`\\x1b[A` for up, here) -- `read_choice` must drain and ignore the
    whole sequence rather than treating the leading `\\x1b` as a cancel.
    The follow-up `y` is written as a genuinely separate keystroke (its own
    `os.write`, after the sequence has had time to be drained) -- a real
    terminal always emits an escape sequence's bytes in one uninterrupted
    burst, and a human never types a *distinct* next key within the same
    handful of milliseconds, so this is the realistic case to prove rather
    than one indistinguishable, at the byte level, from the sequence
    itself."""
    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD.format(src=SRC, immediate=("y", "n"))],
        stdin=slave_fd, stdout=slave_fd, stderr=subprocess.STDOUT,
        close_fds=True)
    os.close(slave_fd)
    try:
        time.sleep(0.3)
        os.write(master_fd, b"\x1b[A")
        time.sleep(0.3)                # let the drain settle before "y"
        os.write(master_fd, b"y")
        time.sleep(0.3)
        chunks = []
        try:
            while True:
                chunk = os.read(master_fd, 4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except OSError:
            pass
        proc.wait(timeout=5)
    finally:
        os.close(master_fd)
        if proc.poll() is None:
            proc.kill()
    assert "GOT:'y'" in b"".join(chunks).decode()


def test_capable_is_false_for_a_plain_non_tty_object():
    import io
    from fs_cli import keyread
    assert keyread.capable(io.StringIO()) is False


def test_capable_returns_the_real_fd_over_a_pty():
    """`keyread.capable` must return the actual fd number (truthy, and
    usable by `os.read`), not a bare `True` -- `read_choice`'s
    `fd = capable(stdin)` line already relies on getting the fd back."""
    child = (
        "import sys\n"
        f"sys.path.insert(0, {SRC!r})\n"
        "from fs_cli import keyread\n"
        "sys.stdout.write('CAPABLE:' + repr(keyread.capable(sys.stdin)))\n"
        "sys.stdout.flush()\n"
    )
    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen([sys.executable, "-c", child],
                            stdin=slave_fd, stdout=slave_fd,
                            stderr=subprocess.STDOUT, close_fds=True)
    os.close(slave_fd)
    try:
        time.sleep(0.3)                    # let the child execute and output
        chunks = []
        try:
            while True:
                chunk = os.read(master_fd, 4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except OSError:
            pass
        proc.wait(timeout=5)
    finally:
        os.close(master_fd)
    out = b"".join(chunks).decode()
    assert "CAPABLE:False" not in out
    assert "CAPABLE:" in out


_KEY_CHILD = """
import sys
sys.path.insert(0, {src!r})
from fs_cli import keyread
result = keyread.read_key(sys.stdin)
sys.stdout.write("GOT:" + repr(result) + "\\n")
sys.stdout.flush()
""".strip()


def _run_key_in_pty(send):
    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, "-c", _KEY_CHILD.format(src=SRC)],
        stdin=slave_fd, stdout=slave_fd, stderr=subprocess.STDOUT,
        close_fds=True)
    os.close(slave_fd)
    try:
        time.sleep(0.3)
        os.write(master_fd, send)
        time.sleep(0.3)
        chunks = []
        try:
            while True:
                chunk = os.read(master_fd, 4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except OSError:
            pass
        proc.wait(timeout=5)
    finally:
        os.close(master_fd)
        if proc.poll() is None:
            proc.kill()
    return b"".join(chunks).decode()


def test_read_key_decodes_the_four_arrow_keys():
    assert "GOT:'up'" in _run_key_in_pty(b"\x1b[A")
    assert "GOT:'down'" in _run_key_in_pty(b"\x1b[B")
    assert "GOT:'right'" in _run_key_in_pty(b"\x1b[C")
    assert "GOT:'left'" in _run_key_in_pty(b"\x1b[D")


def test_read_key_returns_enter_for_a_bare_enter():
    assert "GOT:'enter'" in _run_key_in_pty(b"\r")


def test_read_key_returns_esc_for_a_bare_esc():
    assert "GOT:'esc'" in _run_key_in_pty(b"\x1b")


def test_read_key_returns_the_lowercased_character_otherwise():
    assert "GOT:'q'" in _run_key_in_pty(b"Q")


def test_decode_escape_sequence_recognises_all_four_arrows():
    from fs_cli.keyread import _decode_escape_sequence
    for final, direction in (b"A", "up"), (b"B", "down"), \
                            (b"C", "right"), (b"D", "left"):
        bytes_left = [b"[", final]
        assert _decode_escape_sequence(lambda bl=bytes_left: bl.pop(0)) == direction


def test_decode_escape_sequence_returns_none_for_a_bare_esc():
    from fs_cli.keyread import _decode_escape_sequence
    assert _decode_escape_sequence(lambda: b"") is None


def test_decode_escape_sequence_returns_none_for_an_unrecognised_sequence():
    from fs_cli.keyread import _decode_escape_sequence
    bytes_left = [b"[", b"Z"]
    assert _decode_escape_sequence(lambda: bytes_left.pop(0)) is None


def test_decode_escape_sequence_recognises_pgup_and_pgdn():
    from fs_cli.keyread import _decode_escape_sequence
    bytes_left = [b"[", b"5", b"~"]
    assert _decode_escape_sequence(lambda: bytes_left.pop(0)) == "pgup"
    bytes_left = [b"[", b"6", b"~"]
    assert _decode_escape_sequence(lambda: bytes_left.pop(0)) == "pgdn"


def test_decode_escape_sequence_parses_an_sgr_mouse_press():
    from fs_cli.keyread import _decode_escape_sequence
    # ESC [ < 0 ; 12 ; 5 M -- left-button press at col 12, row 5.
    bytes_left = list(b"[<0;12;5M")
    bytes_left = [bytes([b]) for b in bytes_left]
    assert (_decode_escape_sequence(lambda: bytes_left.pop(0))
           == ("mouse", 0, 12, 5, True))


def test_decode_escape_sequence_parses_an_sgr_mouse_release():
    from fs_cli.keyread import _decode_escape_sequence
    bytes_left = [bytes([b]) for b in b"[<0;12;5m"]
    assert (_decode_escape_sequence(lambda: bytes_left.pop(0))
           == ("mouse", 0, 12, 5, False))


def test_decode_escape_sequence_returns_none_for_malformed_mouse_params():
    from fs_cli.keyread import _decode_escape_sequence
    bytes_left = [bytes([b]) for b in b"[<0;12M"]   # missing the row field
    assert _decode_escape_sequence(lambda: bytes_left.pop(0)) is None


def test_read_key_decodes_pgup_and_pgdn():
    assert "GOT:'pgup'" in _run_key_in_pty(b"\x1b[5~")
    assert "GOT:'pgdn'" in _run_key_in_pty(b"\x1b[6~")


def test_read_key_decodes_an_sgr_mouse_click():
    assert ("GOT:('mouse', 0, 12, 5, True)"
           in _run_key_in_pty(b"\x1b[<0;12;5M"))
