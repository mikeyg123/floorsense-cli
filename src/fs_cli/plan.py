"""`Action`/`ActionPlan` and the shared confirm/execute widget.

`book`, `release`, `team` and `desks` are all the same pipeline: gather
current state, compute a proposed state, show both, let the user select,
execute the selected rows, report per row. Every command that changes
anything is built on this module rather than re-deriving the loop, which is
the whole point of getting it right before any of those commands exist.

Three functions do the interactive work and neither `confirm` nor
`pick_one` runs anything by itself:

  * `confirm` renders the table and returns the plan with selection
    reflecting the user's choice. It picks one of three shapes rather than
    always running the same loop: a plan
    with nothing selectable (every row NOOP/BLOCKED) is shown and returned
    with no prompt at all, since there's nothing to decide; a single-row
    plan is a plain `[y]es [n]o` question; anything larger gets one
    single-shot prompt where a line of digits *selects* those rows (not
    toggles — each response finalises and applies immediately, there is no
    reprompt-and-toggle loop) and `[a]ll`/`[n]ew only`/`[enter]` remain
    shortcuts for the whole set. `q`, `n` (outside the y/n form) and a bare
    Esc keystroke (`\x1b`, sent when a real terminal buffers a lone Esc
    until the next Enter) all cancel. `--yes` bypasses all of this and
    selects everything selectable; a non-interactive stdin without `--yes`
    exits `USER_REJECTED` rather than blocking on a read that will never
    come.
  * `execute` is the only thing that calls `Action.run`, and only for rows
    still selected after `confirm`. It never aborts partway through -- a
    refused row is reported with `✗` and the rest of the plan still runs,
    which is what lets `fs book` report five successes and one refusal
    instead of the first refusal hiding the other four. `✗` is not purely
    a failure glyph, though: a successful `RELEASE` (removing a booking,
    team member, or desk) also prints `✗`, since "removed" reads more
    naturally as a cross than a tick -- `CREATE`/`REPLACE` are the only
    kinds that print `✓` on success. `--json`'s per-row `"result"` is
    unaffected either way; the glyph choice is text-mode display only.
  * `pick_one` is a smaller, unrelated-to-a-plan prompt: `fs team following
    add <name>` needs it because `user_search` can return more than one
    hit for a name, and DECISIONS.md calls for a pick rather than the
    error-listing-candidates `desks.match_desk` uses for an ambiguous desk
    key -- a person is worth one extra round-trip to get right, unlike a
    desk where re-typing a fuller key is just as fast. It shares
    `confirm`'s non-interactive/`--json` guards rather than re-deriving
    them, and a single candidate returns immediately without touching
    stdin at all -- there is nothing to pick between.
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

#: A terminal in canonical (line-buffered) mode still delivers a lone Esc
#: keypress as an ordinary byte -- it just waits for the following Enter
#: before handing the line to `readline()`, so a stripped line consisting of
#: exactly this character is what "the user pressed Esc" looks like without
#: raw/cbreak mode. Treated as a cancel everywhere `q` is.
ESC = "\x1b"


class Kind(Enum):
    CREATE = "CREATE"
    REPLACE = "REPLACE"
    RELEASE = "RELEASE"
    CHECKIN = "CHECKIN"
    NOOP = "NOOP"
    BLOCKED = "BLOCKED"


#: The kinds that actually change something on the server. `NOOP` and
#: `BLOCKED` rows are shown -- the brief's "if you've already booked the
#: same desk, say so" is exactly a NOOP row appearing greyed in the table --
#: but there is nothing for the user to opt into, so they are never
#: selectable.
SELECTABLE_KINDS = {Kind.CREATE, Kind.REPLACE, Kind.RELEASE, Kind.CHECKIN}

#: Kinds worth calling out separately in the summary line. `CREATE` is not
#: named because "N selected" already implies it when nothing more specific
#: happened -- a plan of pure creates prints "3 selected" with no breakdown.
_BREAKDOWN_LABELS = ((Kind.REPLACE, "replacement"), (Kind.RELEASE, "release"))


@dataclass
class Action:
    """One row of a plan.

    `selected` defaults from `kind` when not given explicitly: CREATE,
    REPLACE and RELEASE start selected (the point of the command was to do
    them); NOOP and BLOCKED start unselected (there is nothing to run).
    """

    subject: str
    current: Optional[str]
    proposed: Optional[str]
    kind: Kind
    reason: Optional[str] = None
    selected: Optional[bool] = None
    run: Optional[Callable[[], Any]] = None
    #: What `execute()` prints alongside `subject` on the result line, e.g.
    #: the desk a date's row books/releases/checks in. Not derived from
    #: `current`/`proposed` automatically -- those mean "desk" for
    #: `book`/`release`/`checkin`'s date-per-row plans but "member"/"added"
    #: for `desks`/`team`'s name-per-row plans, where `subject` already IS
    #: the desk/person and repeating it here would be noise (or, worse,
    #: print the wrong thing -- see the module's `execute` docstring). Only
    #: the date-keyed commands set this; everyone else leaves it `None`.
    detail: Optional[str] = None

    def __post_init__(self):
        if self.selected is None:
            self.selected = self.kind in SELECTABLE_KINDS

    @property
    def selectable(self):
        return self.kind in SELECTABLE_KINDS


class ActionPlan:
    """An ordered list of `Action`s, plus the table/summary rendering and the
    selection operations the toggle loop drives."""

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
        """`[n]ew bookings` -- creates only, nothing that touches an existing
        booking or list entry."""
        for a in self.actions:
            a.selected = a.selectable and a.kind is Kind.CREATE

    @property
    def has_creates(self):
        """Whether `[n]ew bookings` would select anything at all -- shown as
        a prompt option only then. `fs release`'s plan is RELEASE rows only
        (nothing is ever created by releasing), so the option there was
        offering a selection that could never differ from "select nothing",
        which read as broken rather than merely unused."""
        return any(a.kind is Kind.CREATE for a in self.actions)

    def toggle(self, index):
        """Flip row `index` (1-based, matching what's printed). Silently a
        no-op out of range or on a BLOCKED/NOOP row -- there is nothing
        there to select, so a stray digit shouldn't raise."""
        i = index - 1
        if 0 <= i < len(self.actions) and self.actions[i].selectable:
            self.actions[i].selected = not self.actions[i].selected

    def select_only(self, indices):
        """Select exactly the given 1-based rows, deselecting everything
        else -- the fallback (non-curses) prompt's `1 2 <enter>` behaviour:
        a line of digits is a *selection*, not a sequence of toggles, so
        typing "2" always means "just row 2" regardless of what was
        selected before. An index that's out of range or names a
        BLOCKED/NOOP row is silently ignored, same as `toggle`."""
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
        """The table. `reason` wins over `proposed` when both exist -- it's
        what carries "nothing available" on a BLOCKED row and "already
        booked (1st preferred)" on a NOOP one; CREATE/REPLACE rows have no
        reason and simply show what they propose.

        A BLOCKED/NOOP row has nothing to select, and dimming its whole line
        is how the table shows that -- done via `render.table`'s
        `row_style` hook rather than by hand, so this doesn't need its own
        copy of the alignment loop `list_cmd`/`find_cmd`/`at_cmd` already
        share. The header row gets the same
        whole-line treatment via `header_style` -- `out.label`, `render.py`'s
        `THEME`-backed method for scaffolding text -- so headers read as
        scaffolding too, not as data competing with the rows beneath them.
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
    """Whether `stdin` is a real terminal a prompt can block on -- the guard
    `confirm`/`pick_one` both need before reading anything. Public (not
    `_`-prefixed) because `reset_cmd.py` reuses it for the exact same
    guard shape rather than carrying its own byte-for-byte copy."""
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
    """Render `plan` and return it with the user's selection applied. Never
    calls `Action.run` -- that's `execute`'s job, kept separate so a caller
    can inspect the confirmed plan before anything happens.

    Picks one of three shapes, in this order:

      1. Empty plan -- nothing to show, nothing to read. `fs release` on a
         week with no bookings, or `fs book` when every date fell outside
         the window, both produce this.
      2. Nothing selectable (every row NOOP/BLOCKED) -- shown, but there is
         nothing to decide, so no prompt at all. Checked ahead of
         `--yes`/`--json`/tty so a `--yes` run on an all-NOOP plan gets the
         same "nothing to change" message rather than silently doing
         nothing.
      3. Something selectable -- `--yes` bypasses the prompt outright;
         otherwise a single-row plan is a plain `[y]es [n]o` question
         (`_confirm_one`), anything larger gets the single-shot
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

    # `Output.print` is a no-op in --json mode (render.py's contract: a
    # stdout-only consumer sees the JSON document, not interleaved text), so
    # there is nothing to show and a stray digit means blindly picking a
    # prompt it never saw. Fail the same way a dead stdin does, and before
    # touching stdin at all -- consuming a scripted line here would make the
    # command behave differently depending on flags that shouldn't matter.
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
    """One answer -- a single immediate keypress (`keyread.read_choice`) when
    the terminal supports it, an accumulated line otherwise -- lowercased
    and stripped. Raises `UserRejected` on EOF rather than returning a
    sentinel, since every caller's next step on EOF is identical.

    `immediate` is the set of single keys (already lowercased) this
    particular prompt should act on the instant they're pressed, with no
    Enter needed -- `y`/`n`/`a`/`q` for `_confirm_one`, `a`/`y`/`q` (plus
    `n` when offered) for `_confirm_many`. A bare Esc and a bare Enter
    always act immediately regardless of what's passed -- `keyread`'s own
    contract, not something each caller has to opt into.
    """
    choice = keyread.read_choice(stdin, out, immediate=immediate)
    if choice is None:
        raise UserRejected(
            "input ended before the plan was confirmed",
            hint="Re-run with --yes to accept the proposed changes "
                 "non-interactively.")
    return choice


def _confirm_one(plan, out, stdin):
    """A single-row plan is a plain yes/no question -- there is nothing to
    select between, so the `[a]ll`/digit machinery of `_confirm_many` would
    just be noise. `y`/`a`/bare enter accept; `n`/`q`/Esc decline -- and any
    of those five act the instant they're pressed (`keyread.read_choice`),
    no trailing Enter needed on a real terminal.

    No summary line here, deliberately -- `plan.summary()` reports what's
    *currently* selected, which for a row defaulting to `selected=False`
    would print "0 selected" directly above a question that's about to
    select it. The row's own Current/Proposed columns already say what a
    "yes" does; a summary of a decision not yet made would only confuse
    the one prompt shape this redesign was meant to simplify.
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
    """`"1 2"`, `"1,2"`, `"1  2,3"` -> `[1, 2]`/`[1, 2, 3]`. Returns `[]`
    for anything with no digits in it at all, which the caller treats as
    unrecognised input rather than "select nothing"."""
    return [int(n) for n in re.findall(r"\d+", choice)]


def _confirm_many(plan, out, stdin):
    """More than one row: a single-shot prompt, not a toggle-and-reprompt
    loop. Every recognised response both decides the selection *and*
    returns immediately:

      * bare enter, `a`, or `y` -- select everything selectable
      * `n` -- select new bookings only (`ActionPlan.select_new_only`),
        offered only when the plan actually has a CREATE row -- a plan of
        pure REPLACE/RELEASE rows (e.g. `fs release`) has nothing "new" to
        narrow down to, and offering the option there just meant "select
        nothing" with no visible reason why
      * a line of digits -- select exactly those rows (`select_only`), not
        a toggle, so "2" always means "just row 2" regardless of the
        default selection. Typing the first digit does NOT act immediately
        the way the letter shortcuts do -- a selection can be several rows
        ("1 2 3"), so it still accumulates into a line and needs Enter to
        submit, same as it always has. If every parsed digit is out of
        range (e.g. "9" on a 5-row plan), that is treated the same as
        unrecognised input and reprompts, rather than silently applying
        `select_only([])` -- an empty selection from a genuine out-of-range
        typo would otherwise look identical to "select nothing on
        purpose", with `execute()` then quietly doing nothing. A mix of
        valid and invalid digits ("1 9") still applies the valid ones,
        same as `select_only` always has.
      * `q` or Esc -- cancel

    `a`/`y`/`q`/`n` (when offered) act the instant they're pressed
    (`keyread.read_choice`); only unrecognised input, and an all-out-of-
    range digit line, reprompts -- no other case loops.
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
            # Every parsed digit was out of range (e.g. "9" on a 5-row
            # plan) -- `select_only` would silently deselect everything,
            # which reads identically to "the user chose to select
            # nothing" instead of "the user mistyped a row number".
            # Reprompt like any other unrecognised input, rather than
            # returning an empty selection with no explanation.
            out.warn(f"no row {'/'.join(str(i) for i in indices)} "
                    f"in this plan -- pick 1-{len(plan)}")
            continue
        if indices:
            plan.select_only(indices)
            return plan
        out.warn(f"unrecognised input {choice!r}")


def execute(plan, out):
    """Run every selected action in order, printing `✓`/`✗` per row, and
    return the aggregate exit code. The glyph on a successful row is `✗`
    for `RELEASE` (a removal reads better as a cross than a tick) and `✓`
    for everything else; a failed row is always `✗` regardless of kind, so
    `✗` alone doesn't distinguish "removed" from "refused" -- read the row
    text, not just the glyph, to tell them apart. Colour is a second,
    independent channel from shape: `out.good`/`out.danger`
    (`render.THEME`) mark "it worked"/"it didn't", regardless of which
    glyph shape is on the row.

    A failure never stops the rest of the plan -- `book` reporting five
    successes and one refusal is more useful than the refusal hiding the
    other four. The returned code is the first failure's; later failures of
    a different kind don't overwrite it, since the first is what the user
    saw first and is most likely to act on. The catch is deliberately not
    narrowed to `FsError`: a bug in one row's `run` must not take the rest
    of the plan down with it, any more than a refusal does.

    Per-row outcomes also go through `out.emit`, not just `out.print`: text
    mode gets the `✓`/`✗` lines, but `--json` suppresses `print` entirely
    (render.py's contract -- a stdout-only consumer must see everything), so
    without this a `--json` run would report the whole plan and nothing
    about what happened to each row in it.

    A plan where every row is BLOCKED/NOOP (nothing selected) returns `OK`
    here -- there was nothing to do, which isn't a failure at this layer.
    PLAN.md reserves exit 6/`NO_DESK` for "nothing available"; deciding that
    happened is `fs book`'s job at plan-construction time, not this
    function's -- it would have to guess *why* nothing was selected, which
    it has no way to know.
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
            # The glyph's shape already carries "removed" (RELEASE) vs.
            # "added/changed" (everything else) -- see this function's
            # docstring -- so the colour here is purely "it worked" (good),
            # never a second encoding of which kind of row this was.
            glyph = out.good("✗" if a.kind is Kind.RELEASE else "✓")
            out.print(f" {glyph} {a.subject}"
                      + (f"  {detail}" if detail else ""))
            rows.append({"subject": a.subject, "kind": a.kind.value,
                        "result": "ok"})

    out.emit({"actions": rows})
    return code


def pick_one(candidates, out, label, stdin=None):
    """Prompt the user to choose one of several candidates, returning it.

    A single candidate returns immediately without touching `stdin` --
    there is nothing to pick between, so `fs team following add jane` (one
    hit) never blocks on a prompt the way `fs team following add smith`
    (several) does. Guards mirror `confirm`'s exactly: `--json` has no
    prompt to show, and a non-interactive stdin without a `--yes`-shaped
    fallback has nothing to consume, so both raise `UserRejected` before
    reading anything rather than blocking on input that will never come.

    `label` renders one candidate for the numbered list -- kept generic so
    a person, a desk, or anything else can be picked between with the same
    function.
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
