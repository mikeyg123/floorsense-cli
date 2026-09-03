# Windows support for `fs map`'s interactive view

## Status

Option A implemented: `keyread_windows.py`, dispatched from `keyread.py`
whenever `termios` isn't importable. Keyboard nav only, no mouse (see
Option A below). Option B not attempted -- real Windows testing
opportunity is limited, so the lower-risk path was taken; see Testing.
`fs map`'s drawing side needed no changes, confirmed by the user:
`cmd.exe`'s static fallback already renders colour correctly.

## Why it doesn't work today

Two separate things, often conflated:

1. **Drawing the map** (`map_cmd.py`'s `_run_live`) is plain VT/ANSI escapes --
   alt-screen (`\x1b[?1049h`), cursor hide (`\x1b[?25l`), SGR mouse tracking
   (`\x1b[?1000h\x1b[?1006h`), cursor-home/erase (`\x1b[H`, `\x1b[K`/`\x1b[J`).
   No `curses` anywhere in this codebase. **Already portable to Windows
   Terminal** (a real VT100/xterm-class emulator, on by default Win10/11) with
   zero code changes. Legacy `cmd.exe`/`conhost` needs
   `SetConsoleMode(ENABLE_VIRTUAL_TERMINAL_PROCESSING)` once at startup
   (ctypes/kernel32, ~10 lines) or it prints raw escape junk.

2. **Reading arrow keys / mouse clicks** (`keyread.py`) is built entirely on
   `termios`/`tty`/`select`/`os.read` -- POSIX-only, full stop, regardless of
   which terminal app hosts the process. `keyread.py` already degrades
   gracefully:
   ```python
   try:
       import termios
       import tty
   except ImportError:
       termios = None
       tty = None
   ```
   and `capable()` returns `False` whenever `termios is None`. **This is the
   actual gap** -- terminal choice (Windows Terminal vs `cmd.exe`) doesn't
   affect it either way.

## Two implementation options

### Option A -- `msvcrt` polling, keyboard-only (low risk, do this first)

- `msvcrt.getch()` / `msvcrt.kbhit()`, stdlib, no new dependency.
- Translate Windows scan codes (`0xE0`/`0x00` prefix + code) to the same
  `"up"`/`"down"`/`"pgup"`/etc. tokens `read_key()` already returns.
- **No mouse support** -- Windows console mouse events don't reach `msvcrt`
  at all; they're a separate Win32 console-input-buffer API.
- ~60-100 lines: a sibling backend module shaped like `keyread.py`'s
  `capable()`/`read_choice()`/`read_key()`. No real uncertainty here --
  straightforward to get right.

### Option B -- `ENABLE_VIRTUAL_TERMINAL_INPUT`, full parity (better fit, unverified)

- Recent Windows 10/11 consoles (Windows Terminal included) support a mode
  where the OS synthesizes arrow keys *and mouse events* as the same raw VT
  escape bytes a POSIX terminal sends (`\x1b[A`, SGR mouse reports). Turn it
  on via `SetConsoleMode` (ctypes/kernel32, ~20-30 lines).
- If that holds up, `_decode_escape_sequence`/`_decode_mouse_params`/the
  arrow-key table in `keyread.py` -- the actual interesting logic, and the
  bulk of the file -- need **no changes at all**.
- Still needs replacing: raw-mode setup/teardown (`tty.setcbreak`/
  `termios.tcsetattr` -> `SetConsoleMode` equivalents), and `select.select()`'s
  "is there more input pending" role for the ESC-vs-arrow-key grace period --
  `select` on Windows only works on sockets, not console handles, so this
  needs a `msvcrt.kbhit()` polling substitute instead.
- **Unverified**: whether Python actually gets clean raw single bytes out of
  a Windows console in this mode, with no other buffering/translation layer
  in the way. Prototype before committing to this as the design.
- ~150-250 lines if it works cleanly, plus a debugging pass on real Windows.

### Recommendation

Option A shipped (see Status). Option B remains the path to mouse support
if a Windows box becomes available to prototype it against.

## Package size / architecture constraint

Both options are **stdlib-only** (`msvcrt`, `ctypes` ship with every CPython
on Windows) -- no new pip dependency, negligible size change to the shiv
zipapp.

This constraint is load-bearing, not just nice-to-have:
`scripts/build-release.sh`'s own header states the whole point of this
packaging is one pure-Python file that runs unmodified on macOS/Linux/Windows,
and it actively verifies the build contains no compiled `.so`/`.pyd`/`.dylib`.
That rules out `windows-curses` (PyPI, wraps compiled PDCurses, Windows-only
via an env marker) -- it can't be bundled into the existing single
cross-platform zipapp without splitting the release into per-platform builds,
which is a real architecture change, not just a size cost. Any other
cross-platform terminal library considered (`prompt_toolkit`, `blessed`,
`urwid`) has the same problem or worse mouse fidelity than the raw SGR
parsing already in `keyread.py` -- none of them avoid writing a Windows
branch, they just hide it, usually at real dependency cost.

## Testing

- Done: `tests/test_keyread_windows.py` drives `keyread_windows.py` against
  a fake `msvcrt`; `tests/test_keyread_dispatch.py` monkeypatches
  `keyread.termios = None` to pin the exact condition Windows hits and
  confirms `keyread.py` delegates to it. Neither needs a real Windows box.
- Not done, deliberately out of scope this pass: a `windows-latest` CI job.
  `fs map`'s Windows path is unverified by CI, only by the unit tests above.
- `FS_NO_TTY=1` forces `keyread.capable()` to `False` unconditionally --
  lets the plain-`readline()` fallback (what every command takes on
  Windows today, `fs map`'s static view included) be exercised by hand
  from a real macOS/Linux terminal, without needing a non-tty stdin or an
  actual Windows box.
- Still needed, manual, on a real Windows machine (both terminals): `fs map`
  interactive nav (arrow keys, Page Up/Down -- no mouse) and the static
  fallback's rendering (colour, box-drawing glyphs) in Windows Terminal and
  legacy `cmd.exe`/`conhost`. `cmd.exe` colour rendering for the static
  view is already confirmed working by the user.
