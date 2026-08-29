"""Tables, colour, and the two formatting rules every command obeys.

Consistency across commands matters more than any individual layout, so:

  * a date is ALWAYS rendered by `Output.fmt_date` -- `Today`, `Tomorrow`,
    `Monday 24th Aug`; never a raw dd/mm/yyyy;
  * a desk is ALWAYS rendered by `Output.fmt_desk` -- short form plus group
    rank, `5.217A (1st preferred)`;
  * warnings go to stderr and data to stdout, so `fs list | ...` stays clean
    and the locker banner can never corrupt a pipe.

`--json` inverts that last rule deliberately: a consumer reading only stdout
must still see the warnings, so they become a `warnings` array in the payload
rather than vanishing down a stderr the caller isn't reading.
"""

import datetime as dt
import json
import re
import sys

from . import dates as _dates
from . import desks as _desks

__all__ = ["Output", "table", "supports_color"]

#: Strips SGR colour codes so column widths are measured by what the
#: terminal actually draws, not by the byte count of the escape sequence.
#: `Output.identifier`/`label`/etc. colour a cell's text before it ever
#: reaches `table()` -- a bare `len()` on that string counts the invisible
#: `\x1b[1m...\x1b[0m` wrapper as real characters, over-widening that
#: column and pushing every later cell in the row (and the header, which
#: is never coloured the same way) out of alignment with it.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_ANSI = {"bold": "1", "dim": "2", "reverse": "7", "blink": "5", "red": "31",
         "green": "32", "yellow": "33", "blue": "34"}

#: Meaning -> `_ANSI` key. This is the one place that decides what colour a
#: *kind* of thing gets -- every command reaches colour through `Output`'s
#: semantic methods below (`identifier`/`label`/`muted`/`good`/`attention`/
#: `danger`), never a raw `.red()`/`.green()` call, so reconfiguring the
#: palette means editing this dict, not hunting through `commands/*.py` and
#: `plan.py` for every place that had an opinion about "good" or "bad".
#:
#:   identifier -- the thing a table is scanned for: a desk key (`fmt_desk`).
#:   label      -- scaffolding, not data: field labels, table headers.
#:   muted      -- present but not actionable: BLOCKED/NOOP plan rows,
#:                 "not stored".
#:   good       -- state is fine: live session, checked in, password stored.
#:   attention  -- needs action soon, not yet urgent: locker notice/warning,
#:                 not checked in.
#:   danger     -- blocking or wrong: locker banner, `fs: <error>`, a failed
#:                 row in `plan.execute`.
THEME = {
    "identifier": "bold",
    "label": "dim",
    "muted": "dim",
    "good": "green",
    "attention": "yellow",
    "danger": "red",
}


def supports_color(stream=None):
    """Colour is interactive-only. Piping into a file or another program
    should produce plain text without the caller having to ask."""
    stream = stream or sys.stdout
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def table(headers, rows, indent="", row_style=None, cell_style=None,
          header_style=None):
    """A plain aligned table. The last column is never padded, so lines have
    no trailing whitespace -- which keeps golden transcripts and copy-paste
    honest.

    `headers` may be `None`/empty/all-blank for a header-less listing (`fs
    list`, `fs find`, `fs at` all want this -- a header row is noise on
    output that's usually three lines).

    `row_style(index, line)` -> `str`, if given, is applied to each data row
    (0-based `index` into `rows`) after alignment -- e.g. dimming a row that
    isn't selectable. It never runs on the header line, and it sees the
    already-padded, already-joined text, so wrapping it in ANSI can't perturb
    the column widths computed from raw length. Because of that, `row_style`
    is only safe for whole-line effects (or for colouring the LAST column,
    which is never padded) -- it has no way to find a non-last cell's
    boundaries once everything is one joined string.

    `cell_style(row_index, col_index, padded_cell, raw_cell)` -> `str`, if
    given, is applied to ONE cell -- BEFORE it's joined with its
    neighbours, but AFTER it's padded to `widths[col_index]` (computed from
    `raw_cell`'s un-ANSI'd length) -- so it can colour any column, not just
    the last, without perturbing any column's width.

    `header_style(line)` -> `str`, if given, is applied to the header line
    the same way `row_style` is applied to a data line -- AFTER alignment,
    on the whole already-joined string -- so dimming the header can't
    perturb the widths computed from the raw header/cell text either. Never
    runs when there's no header row to show.
    """
    if not rows:
        return ""
    cells = [[("" if c is None else str(c)) for c in row] for row in rows]
    ncols = max(len(r) for r in cells)
    cells = [r + [""] * (ncols - len(r)) for r in cells]
    heads = [str(h) for h in (headers or [])][:ncols]
    heads += [""] * (ncols - len(heads))

    def vlen(s):
        return len(_ANSI_RE.sub("", s))

    widths = [max(vlen(heads[i]), *(vlen(r[i]) for r in cells))
              for i in range(ncols)]

    def vljust(s, width):
        return s + " " * (width - vlen(s))

    def line(row, row_index=None):
        parts = []
        for i in range(ncols - 1):
            padded = vljust(row[i], widths[i])
            if cell_style is not None and row_index is not None:
                padded = cell_style(row_index, i, padded, row[i])
            parts.append(padded)
        last = row[ncols - 1]
        if cell_style is not None and row_index is not None:
            last = cell_style(row_index, ncols - 1, last, last)
        parts.append(last)
        return (indent + "   ".join(parts)).rstrip()

    out = []
    if any(heads):
        head_line = line(heads)
        out.append(header_style(head_line) if header_style else head_line)
    for i, r in enumerate(cells):
        text = line(r, row_index=i)
        out.append(row_style(i, text) if row_style else text)
    return "\n".join(out)


def _jsonable(value):
    """ISO-8601 strings rather than unix ints, per the --json contract."""
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


class Output:
    """Everything a command prints goes through one of these.

    Commands never touch `print` or `sys.stderr` directly; routing text vs
    JSON, and stdout vs stderr, is this object's single responsibility.
    """

    def __init__(self, today, now=None, groups=None, tags=None, color=False,
                 json_mode=False, stdout=None, stderr=None):
        self.today = today
        self.now = now
        if self.now is None and today is not None:
            self.now = dt.datetime.combine(today, dt.time.min)
        self.groups = groups or {}
        # Set by each desk-displaying command once it has `ctx.catalog` in
        # hand (`catalog.tag_map()`) -- catalog is lazy/per-command, unlike
        # `groups` which comes from config and is set once in `cli.py`.
        self.tags = tags or {}
        self.json_mode = json_mode
        # ANSI inside a JSON string is never what the caller wanted.
        self.color = bool(color) and not json_mode
        self._stdout = stdout or sys.stdout
        self._stderr = stderr or sys.stderr
        self._warnings = []
        self._payload = {}

    # -- the two formatting rules -------------------------------------------

    def fmt_date(self, d):
        return _dates.fmt_date(d, self.today)

    def fmt_desk(self, key):
        return self.identifier(
            _desks.fmt_desk(key, self.groups, self.tags.get(key)))

    def fmt_dates(self, dates):
        """Several dates through `fmt_date`, comma-joined -- the shape every
        intent line needs (`"Today, Tomorrow"`), pulled out once rather
        than reimplemented per command."""
        return ", ".join(self.fmt_date(d) for d in dates)

    # -- colour -------------------------------------------------------------

    def _wrap(self, code, text):
        if not self.color:
            return text
        return f"\x1b[{_ANSI[code]}m{text}\x1b[0m"

    def bold(self, text):
        return self._wrap("bold", text)

    def dim(self, text):
        return self._wrap("dim", text)

    def red(self, text):
        return self._wrap("red", text)

    def green(self, text):
        return self._wrap("green", text)

    def yellow(self, text):
        return self._wrap("yellow", text)

    def blue(self, text):
        return self._wrap("blue", text)

    def reverse(self, text):
        """Reverse (swap fg/bg) video, not a semantic colour -- `fs map`'s
        live view uses this to mark the cursor cell itself, layered on top
        of whatever semantic colour that cell already has (free/restricted/
        booked/yours), so the cursor stays visible under every kind."""
        return self._wrap("reverse", text)

    def blink(self, text):
        """SGR blink (`\\x1b[5m`), not a semantic colour -- layered on top
        of `reverse` for `fs map`'s live-view cursor cell, per direct user
        feedback that a static reverse-video cell wasn't obvious enough.
        Depends on terminal support (most modern terminals honour it;
        a few disable blink outright) -- there's no portable way to detect
        that from here, so this is a best-effort visual aid, not a
        guarantee."""
        return self._wrap("blink", text)

    # -- semantic colour ------------------------------------------------

    def style(self, name, text):
        """Colour by MEANING (`"good"`, `"danger"`, `"identifier"`, ...),
        looked up in `THEME` at the top of this module -- the one place
        that decides which raw colour a meaning gets today. Prefer the
        named convenience methods below at call sites; this is what they're
        built on."""
        return self._wrap(THEME[name], text)

    def identifier(self, text):
        return self.style("identifier", text)

    def label(self, text):
        return self.style("label", text)

    def muted(self, text):
        return self.style("muted", text)

    def good(self, text):
        return self.style("good", text)

    def attention(self, text):
        return self.style("attention", text)

    def danger(self, text):
        return self.style("danger", text)

    # -- writing ------------------------------------------------------------

    def print(self, text=""):
        """Human-readable output. Suppressed entirely in --json mode, where
        the payload is the output."""
        if not self.json_mode:
            print(text, file=self._stdout)

    def intent(self, text):
        """States what a command resolved to do, before it acts (e.g.
        "Showing bookings for Jane Doe, Tuesday 25th Aug") -- also how
        `-next`-style relative dates stop being ambiguous: the resolved day
        is spelled out before anything else prints.

        Goes to stderr, not stdout, in text mode -- this module's own rule
        is "warnings go to stderr and data to stdout, so `fs list | ...`
        stays clean" (module docstring), and an intent line is neither
        (DECISIONS.md's Group A retrospective): it's not a row
        `fs list | head -1` should ever return. Suppressed entirely
        under --json, unlike `warn` -- a
        --json consumer already gets the resolved values in the payload
        itself, so there's no `warnings`-array equivalent to preserve it
        in. Kept as its own method rather than a bare `out.print`/`out.warn`
        so intent lines stay a single greppable, independently
        restyleable call shape."""
        if not self.json_mode:
            print(text, file=self._stderr)

    def prompt(self, text):
        """An interactive prompt line: written WITHOUT a trailing newline, so
        the user types right after it (`... [q]uit > `) instead of on the
        line below it. `plan.py`'s `confirm()`/`pick_one()` are the only
        callers -- both already refuse to reach this in --json mode (there is
        no prompt to show a non-interactive consumer), but this stays
        suppressed there too, matching `print`'s contract."""
        if not self.json_mode:
            self._stdout.write(text)
            self._stdout.flush()

    def warn(self, text):
        """stderr in text mode; a `warnings` entry in --json mode."""
        if self.json_mode:
            self._warnings.append(text)
        else:
            print(text, file=self._stderr)

    def emit(self, payload):
        """Contribute to the --json object. Merged, not printed, so several
        calls still produce exactly one JSON document."""
        self._payload.update(payload)

    def finish(self):
        """Flush the JSON document. Exactly one call, at the end of the
        command, whether or not anything was emitted."""
        if not self.json_mode:
            return
        doc = _jsonable(dict(self._payload))
        doc["warnings"] = list(self._warnings)
        print(json.dumps(doc, indent=2), file=self._stdout)
