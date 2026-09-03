"""`Action`/`ActionPlan` and the shared confirm/execute widget.

`book`, `release`, `team` and `desks` are all the same pipeline: gather
current state, compute a proposed state, show both, let the user select,
execute the selected rows, report per row.

  * `confirm` renders the table and returns the plan with the user's
    selection applied. Nothing selectable (every row NOOP/BLOCKED) shows
    with no prompt; a single-row plan is a plain `[y]es [n]o`; anything
    larger gets one single-shot prompt where a line of digits *selects*
    those rows (not toggles -- no reprompt-and-toggle loop).
    `--yes` selects everything selectable; a non-interactive stdin
    without it exits `USER_REJECTED`.
  * `execute` is the only thing that calls `Action.run`, only for rows
    still selected. Never aborts partway -- a refused row prints `✗` and
    the rest still runs. `✗` also marks a successful `RELEASE` ("removed"
    reads as a cross, not a tick); `CREATE`/`REPLACE` print `✓`.
  * `pick_one` is a smaller, plan-unrelated prompt for `user_search`
    returning more than one hit -- shares `confirm`'s non-interactive/
    `--json` guards; a single candidate returns immediately.
"""

import re
import sys
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

from . import keyread, render
from .errors import ExitCode, FsError, UserRejected, exit_code_for

__all__ = ["Kind", "Action", "ActionPlan", "confirm", "execute", "pick_one",
          "is_interactive"]

#: A canonical-mode terminal delivers a lone Esc as an ordinary byte,
#: buffered until Enter -- a stripped line of exactly this is "Esc" without
#: raw/cbreak mode. Treated as a cancel everywhere `q` is.
ESC = "\x1b"


class Kind(Enum):
    CREATE = "CREATE"
    REPLACE = "REPLACE"
    RELEASE = "RELEASE"
    CHECKIN = "CHECKIN"
    NOOP = "NOOP"
    BLOCKED = "BLOCKED"


#: The kinds that actually change something on the server. `NOOP`/`BLOCKED`
#: rows are shown (greyed) but never selectable -- nothing to opt into.
SELECTABLE_KINDS = {Kind.CREATE, Kind.REPLACE, Kind.RELEASE, Kind.CHECKIN}

#: Kinds worth calling out separately in the summary line. `CREATE` isn't
#: named -- "N selected" already implies it when nothing else happened.
_BREAKDOWN_LABELS = ((Kind.REPLACE, "replacement"), (Kind.RELEASE, "release"))


@dataclass
class Action:
    """One row of a plan.

    `selected` defaults from `kind`: CREATE/REPLACE/RELEASE start selected
    (the point was to do them); NOOP/BLOCKED start unselected.
    """

    subject: str
    current: Optional[str]
    proposed: Optional[str]
    kind: Kind
    reason: Optional[str] = None
    selected: Optional[bool] = None
    run: Optional[Callable[[], Any]] = None
    #: What `execute()` prints alongside `subject`, e.g. the desk a date's
    #: row books/releases -- not derived from `current`/`proposed`
    #: automatically, since those mean different things for date-keyed vs
    #: name-keyed plans. Only date-keyed commands set this.
    detail: Optional[str] = None

    def __post_init__(self):
        if self.selected is None:
            self.selected = self.kind in SELECTABLE_KINDS

    @property
    def selectable(self):
        return self.kind in SELECTABLE_KINDS


class ActionPlan:
    """An ordered list of `Action`s, plus table/summary rendering and the
    selection operations the prompt drives."""

    def __init__(self, actions, subject_header="Date"):
        self.actions = list(actions)
        self.subject_header = subject_header

    def __iter__(self):
        return iter(self.actions)

    def __len__(self):
        return len(self.actions)

    def __getitem__(self, index):
        return self.actions[index]

    # -- selection ------------------------------------------------------

    def select_all(self):
        """`[a]ll` -- select every row that has something to run."""
        for a in self.actions:
            if a.selectable:
                a.selected = True

    def select_new_only(self):
        """`[n]ew bookings` -- creates only, nothing touching an existing
        booking or list entry."""
        for a in self.actions:
            a.selected = a.selectable and a.kind is Kind.CREATE

    @property
    def has_creates(self):
        """Whether `[n]ew bookings` would select anything -- shown as a
        prompt option only then (a pure-RELEASE plan like `fs release`
        would otherwise offer an option that always selects nothing)."""
        return any(a.kind is Kind.CREATE for a in self.actions)

    def toggle(self, index):
        """Flip row `index` (1-based). Silently a no-op out of range or
        on a BLOCKED/NOOP row."""
        i = index - 1
        if 0 <= i < len(self.actions) and self.actions[i].selectable:
            self.actions[i].selected = not self.actions[i].selected

    def select_only(self, indices):
        """Select exactly the given 1-based rows, deselecting the rest --
        a line of digits is a *selection*, not a sequence of toggles, so
        "2" always means "just row 2" regardless of prior state."""
        wanted = set(indices)
        for i, a in enumerate(self.actions, start=1):
            a.selected = a.selectable and i in wanted

    # -- rendering --------------------------------------------------------

    def summary(self):
        selected = [a for a in self.actions if a.selected]
        parts = [f"{len(selected)} selected"]
        counts = Counter(a.kind for a in selected)
        for kind, label in _BREAKDOWN_LABELS:
            n = counts.get(kind, 0)
            if n:
                parts.append(f"{n} {label}{'' if n == 1 else 's'}")
        return " · ".join(parts)

    def render(self, out):
        """The table. `reason` wins over `proposed` when both exist -- it
        carries "nothing available" (BLOCKED) or "already booked" (NOOP);
        CREATE/REPLACE have no reason and show what they propose.

        A BLOCKED/NOOP row's whole line is dimmed via `render.table`'s
        `row_style` hook; the header gets the same treatment via
        `header_style` so it reads as scaffolding, not data.
        """
        headers = ["", self.subject_header, "Current", "Proposed"]
        rows = []
        for i, a in enumerate(self.actions, start=1):
            proposed = a.reason if a.reason else (
                a.proposed if a.proposed is not None else "—")
            rows.append([str(i), a.subject,
                        a.current if a.current is not None else "—",
                        proposed])

        def style(index, text):
            return text if self.actions[index].selectable else out.muted(text)

        return render.table(headers, rows, indent=" ", row_style=style,
                            header_style=out.label)


def is_interactive(stdin):
    """Whether `stdin` is a real terminal a prompt can block on -- the
    guard `confirm`/`pick_one` need before reading anything. Public
    since `reset_cmd.py` reuses it too."""
    try:
        return bool(stdin.isatty())
    except (AttributeError, ValueError):
        return False


def _show_plan(plan, out):
    text = plan.render(out)
    if text:
        out.print()
        out.print(text)
    out.print()


def confirm(plan, out, yes=False, stdin=None):
    """Render `plan` and return it with the user's selection applied.
    Never calls `Action.run` -- that's `execute`'s job.

    Picks one of three shapes: 1) empty plan -- nothing to show/read;
    2) nothing selectable (every row NOOP/BLOCKED) -- shown with no
    prompt, checked ahead of `--yes`/`--json`/tty so `--yes` on an
    all-NOOP plan gets the same message rather than silently doing
    nothing; 3) something selectable -- `--yes` bypasses the prompt,
    otherwise a single row is `[y]es [n]o` (`_confirm_one`), more is the
    select-and-apply prompt (`_confirm_many`).
    """
    stdin = stdin if stdin is not None else sys.stdin

    if len(plan) == 0:
        return plan

    if not any(a.selectable for a in plan):
        _show_plan(plan, out)
        out.print(" nothing to change")
        return plan

    if yes:
        plan.select_all()
        return plan

    # `Output.print` is a no-op in --json mode -- nothing to show, so fail
    # before touching stdin at all rather than blindly consuming a line.
    if getattr(out, "json_mode", False):
        raise UserRejected(
            "--json has no prompt to show",
            hint="Re-run with --yes to accept the proposed changes.")

    if not is_interactive(stdin):
        raise UserRejected(
            "not an interactive terminal and --yes was not given",
            hint="Re-run with --yes to accept the proposed changes.")

    if len(plan) == 1:
        return _confirm_one(plan, out, stdin)
    return _confirm_many(plan, out, stdin)


def _read_choice(stdin, out, immediate=()):
    """One answer -- an immediate keypress when the terminal supports it,
    an accumulated line otherwise -- lowercased/stripped. Raises
    `UserRejected` on EOF rather than a sentinel. `immediate` is the set
    of keys this prompt should act on instantly, no Enter needed; a bare
    Esc/Enter always act immediately regardless (`keyread`'s contract).
    """
    choice = keyread.read_choice(stdin, out, immediate=immediate)
    if choice is None:
        raise UserRejected(
            "input ended before the plan was confirmed",
            hint="Re-run with --yes to accept the proposed changes "
                 "non-interactively.")
    return choice


def _confirm_one(plan, out, stdin):
    """A single-row plan is a plain yes/no question -- the `[a]ll`/digit
    machinery of `_confirm_many` would be noise. `y`/`a`/bare enter
    accept; `n`/`q`/Esc decline, all acting instantly.

    No summary line: `plan.summary()` reports what's *currently*
    selected, which for a row defaulting `selected=False` would print
    "0 selected" right above a question about to select it.
    """
    while True:
        _show_plan(plan, out)
        out.prompt(" [y]es  [n]o > ")

        choice = _read_choice(stdin, out, immediate=("y", "n", "a", "q"))
        if choice in ("", "y", "a"):
            plan.select_all()
            return plan
        if choice in ("n", "q", ESC):
            raise UserRejected("cancelled")
        out.warn(f"unrecognised input {choice!r}")


def _parse_indices(choice):
    """`"1 2"`, `"1,2"`, `"1  2,3"` -> `[1, 2]`/`[1, 2, 3]`. `[]` for
    anything with no digits -- the caller treats that as unrecognised
    input, not "select nothing"."""
    return [int(n) for n in re.findall(r"\d+", choice)]


def _confirm_many(plan, out, stdin):
    """More than one row: a single-shot prompt, not a toggle-and-reprompt
    loop. Every recognised response both decides the selection AND
    returns immediately:

      * bare enter, `a`, or `y` -- select everything selectable
      * `n` -- select new bookings only, offered only when the plan has a
        CREATE row (a pure REPLACE/RELEASE plan has nothing "new" to
        narrow to)
      * a line of digits -- select exactly those rows (`select_only`),
        not a toggle. Still accumulates into a line needing Enter (a
        selection can be "1 2 3"). Every digit out of range (e.g. "9" on
        a 5-row plan) reprompts rather than silently selecting nothing --
        a mistyped selection shouldn't look identical to "select nothing
        on purpose". Valid digits still apply if mixed with invalid ones.
      * `q` or Esc -- cancel
    """
    offer_new_only = plan.has_creates
    immediate = {"a", "y", "q"} | ({"n"} if offer_new_only else set())
    while True:
        _show_plan(plan, out)
        out.print(f" {plan.summary()}")
        new_only = "[n]ew bookings  " if offer_new_only else ""
        out.prompt(f" [a]ll  {new_only}[1-{len(plan)} ...] select  "
                  f"[enter] apply all  [q]uit > ")

        choice = _read_choice(stdin, out, immediate=immediate)
        if choice == "":
            plan.select_all()
            return plan
        if choice in ("q", ESC):
            raise UserRejected("cancelled")
        if choice in ("a", "y"):
            plan.select_all()
            return plan
        if choice == "n" and offer_new_only:
            plan.select_new_only()
            return plan
        indices = _parse_indices(choice)
        in_range = [i for i in indices if 1 <= i <= len(plan)]
        if indices and not in_range:
            # Every digit out of range -- reprompt rather than silently
            # selecting nothing with no explanation.
            out.warn(f"no row {'/'.join(str(i) for i in indices)} "
                    f"in this plan -- pick 1-{len(plan)}")
            continue
        if indices:
            plan.select_only(indices)
            return plan
        out.warn(f"unrecognised input {choice!r}")


def execute(plan, out):
    """Run every selected action in order, printing `✓`/`✗` per row, and
    return the aggregate exit code. `✗` marks both a successful RELEASE
    (a removal reads as a cross) and any failure -- read the row text,
    not just the glyph, to tell them apart. Colour (`good`/`danger`) is
    the independent "it worked"/"it didn't" channel.

    A failure never stops the rest of the plan -- five successes and one
    refusal is more useful than the refusal hiding the other four. The
    returned code is the first failure's. Not narrowed to `FsError`: a
    bug in one row's `run` must not take the rest down with it either.

    Per-row outcomes also go through `out.emit`, not just `out.print`,
    since `--json` suppresses `print` entirely.
    """
    code = ExitCode.OK
    rows = []
    for a in plan:
        if not a.selected:
            continue

        detail = a.detail
        try:
            if a.run is not None:
                a.run()
        except Exception as e:                      # noqa: BLE001
            hint = getattr(e, "hint", None)
            out.print(f" {out.danger('✗')} {a.subject}  {e}"
                      + (f"\n     {hint}" if hint else ""))
            rows.append({"subject": a.subject, "kind": a.kind.value,
                        "result": "error", "error": str(e)})
            if code == ExitCode.OK:
                code = (exit_code_for(e) if isinstance(e, FsError)
                       else ExitCode.UNEXPECTED)
        else:
            # Colour here is purely "it worked" -- the glyph shape already
            # carries "removed" (RELEASE) vs. "added/changed" (everything
            # else), see this function's docstring.
            glyph = out.good("✗" if a.kind is Kind.RELEASE else "✓")
            out.print(f" {glyph} {a.subject}"
                      + (f"  {detail}" if detail else ""))
            rows.append({"subject": a.subject, "kind": a.kind.value,
                        "result": "ok"})

    out.emit({"actions": rows})
    return code


def pick_one(candidates, out, label, stdin=None):
    """Prompt the user to choose one of several candidates, returning it.
    A single candidate returns immediately without touching `stdin`.
    Guards mirror `confirm`'s exactly: `--json` and a non-interactive
    stdin both raise `UserRejected` before reading anything.

    `label` renders one candidate for the numbered list -- kept generic
    so a person, a desk, or anything else can be picked with it.
    """
    if not candidates:
        raise ValueError("pick_one() needs at least one candidate")
    if len(candidates) == 1:
        return candidates[0]

    stdin = stdin if stdin is not None else sys.stdin

    if getattr(out, "json_mode", False):
        raise UserRejected(
            "--json has no prompt to show",
            hint="Re-run with a more specific name.")

    if not is_interactive(stdin):
        raise UserRejected(
            "not an interactive terminal",
            hint="Re-run with a more specific name.")

    while True:
        out.print()
        for i, candidate in enumerate(candidates, start=1):
            out.print(f" [{i}] {label(candidate)}")
        out.print()
        out.prompt(f" [1-{len(candidates)}] pick  [q]uit > ")

        choice = keyread.read_choice(stdin, out, immediate=("q",))
        if choice is None:
            raise UserRejected(
                "input ended before a choice was made",
                hint="Re-run with a more specific name.")

        if choice in ("q", ESC):
            raise UserRejected("cancelled")
        if choice.isdigit() and 1 <= int(choice) <= len(candidates):
            return candidates[int(choice) - 1]
        out.warn(f"unrecognised input {choice!r}")
