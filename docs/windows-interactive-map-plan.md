# Windows support for `fs map`'s interactive view

## Status

Not started. `fs map` on Windows always falls back to the static (non-scrolling)
view today -- by design, not by accident: `keyread.capable()` returns `False`
unconditionally there (see below), so `_run_live` is never entered. This is a
plan for actually implementing it, for whoever picks it up next.

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

Prototype Option B first, in `experiments/` (gitignored scratch space, this
repo's existing convention -- see CLAUDE.md) against a real Windows Terminal
session. If the raw-byte read doesn't come through clean, fall back to
Option A: keyboard-only nav is still a real improvement over always-static,
just missing mouse-click desk selection.

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

- Unit-testable without Windows: `keyread.capable()` already has coverage for
  the non-tty fallback; add a test that monkeypatches
  `keyread.termios = None` directly, pinning the exact condition Windows
  hits (currently only exercised indirectly via a non-tty `StringIO`).
- CI: add a `windows-latest` job to `.github/workflows/ci.yml`'s matrix
  running the existing `pytest` suite -- confirms the package imports
  cleanly and `fs map` falls back to static without crashing, on a real
  Windows interpreter. Doesn't exercise the new interactive backend itself
  (no real terminal on a CI runner).
- Manual, on a real Windows machine, both terminals: `fs map` interactive
  nav (arrow keys, Page Up/Down, mouse click-to-select if Option B lands),
  and the static fallback's rendering (colour, box-drawing glyphs) in both
  Windows Terminal and legacy `cmd.exe`/`conhost`.
