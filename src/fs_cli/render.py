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

__all__ = ["Output", "table", "supports_color", "visible_len"]

#: Strips SGR colour codes so column widths are measured by what the
#: terminal actually draws -- a bare `len()` counts the invisible
#: `\x1b[1m...\x1b[0m` wrapper as real characters, over-widening the column.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_ANSI = {"bold": "1", "dim": "2", "reverse": "7", "blink": "5", "red": "31",
         "green": "32", "yellow": "33", "blue": "34", "magenta": "35"}

#: Meaning -> `_ANSI` key. The one place that decides what colour a *kind*
#: of thing gets -- every command reaches colour through `Output`'s
#: semantic methods below, never a raw `.red()`/`.green()` call.
#:
#:   identifier -- the thing a table is scanned for: a desk key (`fmt_desk`).
#:   label      -- scaffolding, not data: field labels, table headers.
#:   muted      -- present but not actionable: BLOCKED/NOOP plan rows.
#:   good       -- state is fine: live session, checked in, password stored.
#:   attention  -- needs action soon: locker notice/warning, not checked in.
#:   danger     -- blocking or wrong: locker banner, `fs: <error>`.
#:   teammate   -- `fs map`'s occupied-desk highlight for a
#:                 `show_team_on_map` teammate -- ANSI colour 5 (magenta),
#:                 chosen over a 256-colour orange for portability.
THEME = {
    "identifier": "bold",
    "label": "dim",
    "muted": "dim",
    "good": "green",
    "attention": "yellow",
    "danger": "red",
    "teammate": "magenta",
}


def visible_len(text):
    """`len()`, blind to SGR colour codes. Shared by `table()`'s
    column-width math and `map_cmd.py`'s status-line layout."""
    return len(_ANSI_RE.sub("", text))


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
    """A plain aligned table. The last column is never padded, so lines
    have no trailing whitespace.

    `headers` may be `None`/empty/all-blank for a header-less listing
    (`fs list`/`find`/`at` -- a header row is noise on 3-line output).

    `row_style(index, line)`, if given, is applied to each data row
    after alignment, on the whole joined text -- safe only for whole-line
    effects or the LAST column (never padded), since there's no way to
    find a non-last cell's boundary once it's one joined string.

    `cell_style(row_index, col_index, padded_cell, raw_cell)`, if given,
    is applied to ONE cell before it's joined but after padding, so it
    can colour any column without perturbing widths.

    `header_style(line)`, if given, is applied the same way `row_style`
    is, to the header line only.
    """
    if not rows:
        return ""
    cells = [[("" if c is None else str(c)) for c in row] for row in rows]
    ncols = max(len(r) for r in cells)
    cells = [r + [""] * (ncols - len(r)) for r in cells]
    heads = [str(h) for h in (headers or [])][:ncols]
    heads += [""] * (ncols - len(heads))

    widths = [max(visible_len(heads[i]), *(visible_len(r[i]) for r in cells))
              for i in range(ncols)]

    def vljust(s, width):
        return s + " " * (width - visible_len(s))

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
    """Everything a command prints goes through one of these. Commands
    never touch `print`/`sys.stderr` directly -- routing text vs JSON,
    stdout vs stderr, is this object's job."""

    def __init__(self, today, now=None, groups=None, tags=None, color=False,
                 json_mode=False, stdout=None, stderr=None):
        self.today = today
        self.now = now
        if self.now is None and today is not None:
            self.now = dt.datetime.combine(today, dt.time.min)
        self.groups = groups or {}
        # Set by each desk-displaying command once it has `ctx.catalog`
        # (catalog is lazy/per-command, unlike `groups`, set once in cli.py).
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
        """Several dates through `fmt_date`, comma-joined
        (`"Today, Tomorrow"`) -- pulled out once rather than
        reimplemented per command."""
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

    def magenta(self, text):
        return self._wrap("magenta", text)

    def reverse(self, text):
        """Reverse video, not a semantic colour -- `fs map`'s live view
        layers this on the cursor cell's own colour so it stays visible
        under every kind."""
        return self._wrap("reverse", text)

    def blink(self, text):
        """SGR blink, layered on top of `reverse` for the live-view
        cursor cell since a static reverse-video cell is too easy to
        miss. Best-effort -- some terminals disable blink outright."""
        return self._wrap("blink", text)

    # -- semantic colour ------------------------------------------------

    def style(self, name, text):
        """Colour by MEANING, looked up in `THEME`. Prefer the named
        convenience methods below at call sites; this is what they're
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

    def teammate(self, text):
        return self.style("teammate", text)

    # -- writing ------------------------------------------------------------

    def print(self, text=""):
        """Human-readable output. Suppressed entirely in --json mode, where
        the payload is the output."""
        if not self.json_mode:
            print(text, file=self._stdout)

    def intent(self, text):
        """States what a command resolved to do, before it acts (e.g.
        "Showing bookings for Jane Doe, Tuesday 25th Aug"). Goes to
        stderr in text mode -- not a data row `fs list | head -1` should
        return. Suppressed entirely under --json; a --json consumer
        already gets the resolved values in the payload."""
        if not self.json_mode:
            print(text, file=self._stderr)

    def prompt(self, text):
        """An interactive prompt line: written WITHOUT a trailing newline,
        so the user types right after it (`... [q]uit > `). Stays
        suppressed under --json too, matching `print`'s contract."""
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
        """Contribute to the --json object. Merged, not printed, so
        several calls still produce exactly one JSON document."""
        self._payload.update(payload)

    def finish(self):
        """Flush the JSON document. Exactly one call, at the end of the
        command."""
        if not self.json_mode:
            return
        doc = _jsonable(dict(self._payload))
        doc["warnings"] = list(self._warnings)
        print(json.dumps(doc, indent=2), file=self._stdout)
