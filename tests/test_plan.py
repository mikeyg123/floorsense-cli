"""`plan.py` -- Action/ActionPlan and the shared confirm/execute widget.

`book`, `release`, `team` and `desks` are all the same shape: gather state,
compute a proposed state, show both, let the user select, execute selected,
report per row. This is the one place that shape is built and tested, so
every command built on top of it inherits correctness rather than
re-deriving it.
"""

import io

import pytest

from fs_cli.errors import CommError, ExitCode, UserRejected
from fs_cli.plan import Action, ActionPlan, Kind, confirm, execute, pick_one


def make_out(json_mode=False):
    from fs_cli.render import Output
    import datetime as dt
    return Output(today=dt.date(2026, 8, 21), stdout=io.StringIO(),
                  stderr=io.StringIO(), json_mode=json_mode)


class ScriptedStdin:
    """Feeds one scripted line per `readline()` call, and answers `isatty`
    truthfully as an interactive terminal would -- unlike `io.StringIO`,
    which is what makes a plain StringIO usable as the "not a TTY" case for
    free."""

    def __init__(self, lines):
        self._lines = list(lines)

    def isatty(self):
        return True

    def readline(self):
        return (self._lines.pop(0) + "\n") if self._lines else ""


# -- Action defaults ---------------------------------------------------------

def test_create_defaults_selected():
    a = Action(subject="Mon", current=None, proposed="5.217A", kind=Kind.CREATE)
    assert a.selected is True
    assert a.selectable is True


def test_replace_defaults_selected():
    a = Action(subject="Mon", current="5.235A", proposed="5.217A",
              kind=Kind.REPLACE)
    assert a.selected is True


def test_release_defaults_selected():
    a = Action(subject="Mon", current="5.235A", proposed=None,
              kind=Kind.RELEASE)
    assert a.selected is True


def test_noop_defaults_unselected():
    a = Action(subject="Mon", current="5.217A", proposed="5.217A",
              kind=Kind.NOOP, reason="already booked (1st favourite)")
    assert a.selected is False
    assert a.selectable is False


def test_blocked_defaults_unselected():
    a = Action(subject="Wed", current=None, proposed=None, kind=Kind.BLOCKED,
              reason="nothing available")
    assert a.selected is False
    assert a.selectable is False


def test_explicit_selected_overrides_default():
    a = Action(subject="Mon", current=None, proposed="5.217A",
              kind=Kind.CREATE, selected=False)
    assert a.selected is False


# -- ActionPlan: summary -----------------------------------------------------

def test_summary_counts_selected_and_replacements():
    plan = ActionPlan([
        Action("Mon", None, "5.217A", Kind.CREATE),
        Action("Tue", "5.235A", "5.217A", Kind.REPLACE),
        Action("Wed", None, None, Kind.BLOCKED, reason="nothing available"),
        Action("Thu", "5.301B", "5.217A", Kind.REPLACE),
    ])
    assert plan.summary() == "3 selected · 2 replacements"


def test_summary_singular_release():
    plan = ActionPlan([Action("Mon", "5.235A", None, Kind.RELEASE)])
    assert plan.summary() == "1 selected · 1 release"


def test_summary_all_creates_has_no_breakdown():
    plan = ActionPlan([Action("Mon", None, "5.217A", Kind.CREATE),
                       Action("Tue", None, "5.235A", Kind.CREATE)])
    assert plan.summary() == "2 selected"


def test_summary_reflects_deselection():
    plan = ActionPlan([Action("Mon", None, "5.217A", Kind.CREATE, selected=False)])
    assert plan.summary() == "0 selected"


# -- ActionPlan: rendering ----------------------------------------------------

def test_render_shows_row_numbers_and_columns():
    plan = ActionPlan([
        Action("Monday 24th Aug", None, "5.217A (1st favourite)", Kind.CREATE),
        Action("Wednesday 26th Aug", None, None, Kind.BLOCKED,
              reason="nothing available"),
    ])
    text = plan.render(make_out())
    assert "Monday 24th Aug" in text
    assert "5.217A (1st favourite)" in text
    assert "nothing available" in text
    lines = text.splitlines()
    assert lines[1].strip().startswith("1")
    assert lines[2].strip().startswith("2")


def test_render_shows_dash_for_missing_current():
    plan = ActionPlan([Action("Mon", None, "5.217A", Kind.CREATE)])
    text = plan.render(make_out())
    assert "—" in text


def test_render_uses_custom_subject_header():
    plan = ActionPlan([Action("Jane Doe", None, None, Kind.NOOP,
                             reason="already following")],
                      subject_header="Name")
    text = plan.render(make_out())
    assert "Name" in text.splitlines()[0]


# -- ActionPlan: toggling -----------------------------------------------------

def test_toggle_flips_a_selectable_row():
    plan = ActionPlan([Action("Mon", None, "5.217A", Kind.CREATE)])
    plan.toggle(1)
    assert plan.actions[0].selected is False
    plan.toggle(1)
    assert plan.actions[0].selected is True


def test_toggle_ignores_unselectable_row():
    plan = ActionPlan([Action("Wed", None, None, Kind.BLOCKED,
                             reason="nothing available")])
    plan.toggle(1)
    assert plan.actions[0].selected is False


def test_toggle_ignores_out_of_range_index():
    plan = ActionPlan([Action("Mon", None, "5.217A", Kind.CREATE)])
    plan.toggle(99)                     # must not raise
    assert plan.actions[0].selected is True


def test_select_all_selects_every_selectable_row():
    plan = ActionPlan([
        Action("Mon", None, "5.217A", Kind.CREATE, selected=False),
        Action("Tue", None, None, Kind.BLOCKED, reason="nothing available"),
    ])
    plan.select_all()
    assert plan.actions[0].selected is True
    assert plan.actions[1].selected is False   # can't select a blocked row


def test_select_new_only_deselects_replacements():
    plan = ActionPlan([
        Action("Mon", None, "5.217A", Kind.CREATE),
        Action("Tue", "5.235A", "5.217A", Kind.REPLACE),
    ])
    plan.select_new_only()
    assert plan.actions[0].selected is True
    assert plan.actions[1].selected is False


def test_select_only_selects_exactly_the_given_rows():
    plan = ActionPlan([
        Action("Mon", None, "5.217A", Kind.CREATE),
        Action("Tue", "5.235A", "5.217A", Kind.REPLACE, selected=False),
        Action("Wed", None, "5.301B", Kind.CREATE, selected=False),
    ])
    plan.select_only([2, 3])
    assert plan.actions[0].selected is False
    assert plan.actions[1].selected is True
    assert plan.actions[2].selected is True


def test_select_only_ignores_unselectable_and_out_of_range_indices():
    plan = ActionPlan([
        Action("Mon", None, "5.217A", Kind.CREATE),
        Action("Tue", None, None, Kind.BLOCKED, reason="nothing available"),
    ])
    plan.select_only([2, 99])            # must not raise
    assert plan.actions[0].selected is False
    assert plan.actions[1].selected is False


# -- confirm(): --yes ---------------------------------------------------------

def test_confirm_yes_selects_everything_selectable_without_prompting():
    plan = ActionPlan([
        Action("Mon", None, "5.217A", Kind.CREATE, selected=False),
        Action("Tue", None, None, Kind.BLOCKED, reason="nothing available"),
    ])
    result = confirm(plan, make_out(), yes=True, stdin=io.StringIO())
    assert result.actions[0].selected is True
    assert result.actions[1].selected is False


# -- confirm(): non-interactive without --yes --------------------------------

def test_confirm_without_yes_and_no_tty_exits_user_rejected():
    plan = ActionPlan([Action("Mon", None, "5.217A", Kind.CREATE)])
    with pytest.raises(UserRejected) as exc_info:
        confirm(plan, make_out(), yes=False, stdin=io.StringIO("\n"))
    assert exc_info.value.code == ExitCode.USER_REJECTED


# -- confirm(): single-row plan is a plain y/n question ----------------------

def test_confirm_single_row_enter_means_yes():
    plan = ActionPlan([Action("Mon", None, "5.217A", Kind.CREATE)])
    result = confirm(plan, make_out(), stdin=ScriptedStdin([""]))
    assert result.actions[0].selected is True


def test_confirm_single_row_y_means_yes():
    plan = ActionPlan([Action("Mon", None, "5.217A", Kind.CREATE,
                             selected=False)])
    result = confirm(plan, make_out(), stdin=ScriptedStdin(["y"]))
    assert result.actions[0].selected is True


def test_confirm_single_row_n_means_no():
    plan = ActionPlan([Action("Mon", None, "5.217A", Kind.CREATE)])
    with pytest.raises(UserRejected):
        confirm(plan, make_out(), stdin=ScriptedStdin(["n"]))


def test_confirm_single_row_esc_means_no():
    plan = ActionPlan([Action("Mon", None, "5.217A", Kind.CREATE)])
    with pytest.raises(UserRejected):
        confirm(plan, make_out(), stdin=ScriptedStdin(["\x1b"]))


def test_confirm_single_row_q_raises_user_rejected():
    plan = ActionPlan([Action("Mon", None, "5.217A", Kind.CREATE)])
    with pytest.raises(UserRejected):
        confirm(plan, make_out(), stdin=ScriptedStdin(["q"]))


def test_confirm_single_row_eof_raises_user_rejected():
    plan = ActionPlan([Action("Mon", None, "5.217A", Kind.CREATE)])
    with pytest.raises(UserRejected):
        confirm(plan, make_out(), stdin=ScriptedStdin([]))


def test_confirm_single_row_unrecognised_input_reprompts_without_crashing():
    plan = ActionPlan([Action("Mon", None, "5.217A", Kind.CREATE)])
    result = confirm(plan, make_out(), stdin=ScriptedStdin(["xyz", ""]))
    assert result.actions[0].selected is True


# -- confirm(): multi-row plan is a single-shot select-and-apply prompt ------

def test_confirm_digits_select_only_those_rows():
    plan = ActionPlan([
        Action("Mon", None, "5.217A", Kind.CREATE),
        Action("Tue", "5.235A", "5.217A", Kind.REPLACE),
    ])
    result = confirm(plan, make_out(), stdin=ScriptedStdin(["2"]))
    assert result.actions[0].selected is False
    assert result.actions[1].selected is True


def test_confirm_multiple_digits_select_that_set():
    plan = ActionPlan([
        Action("Mon", None, "5.217A", Kind.CREATE, selected=False),
        Action("Tue", "5.235A", "5.217A", Kind.REPLACE, selected=False),
    ])
    result = confirm(plan, make_out(), stdin=ScriptedStdin(["1 2"]))
    assert all(a.selected for a in result.actions)


def test_confirm_enter_selects_and_applies_everything():
    plan = ActionPlan([
        Action("Mon", None, "5.217A", Kind.CREATE, selected=False),
        Action("Tue", "5.235A", "5.217A", Kind.REPLACE, selected=False),
    ])
    result = confirm(plan, make_out(), stdin=ScriptedStdin([""]))
    assert all(a.selected for a in result.actions)


def test_confirm_all_selects_and_applies_immediately():
    plan = ActionPlan([
        Action("Mon", None, "5.217A", Kind.CREATE, selected=False),
        Action("Tue", "5.235A", "5.217A", Kind.REPLACE, selected=False),
    ])
    result = confirm(plan, make_out(), stdin=ScriptedStdin(["a"]))
    assert all(a.selected for a in result.actions)


def test_confirm_new_only_selects_and_applies_immediately():
    plan = ActionPlan([
        Action("Mon", None, "5.217A", Kind.CREATE),
        Action("Tue", "5.235A", "5.217A", Kind.REPLACE),
    ])
    result = confirm(plan, make_out(), stdin=ScriptedStdin(["n"]))
    assert result.actions[0].selected is True
    assert result.actions[1].selected is False


def test_confirm_multi_row_esc_raises_user_rejected():
    plan = ActionPlan([
        Action("Mon", None, "5.217A", Kind.CREATE),
        Action("Tue", "5.235A", "5.217A", Kind.REPLACE),
    ])
    with pytest.raises(UserRejected):
        confirm(plan, make_out(), stdin=ScriptedStdin(["\x1b"]))


def test_confirm_multi_row_q_raises_user_rejected():
    plan = ActionPlan([
        Action("Mon", None, "5.217A", Kind.CREATE),
        Action("Tue", "5.235A", "5.217A", Kind.REPLACE),
    ])
    with pytest.raises(UserRejected):
        confirm(plan, make_out(), stdin=ScriptedStdin(["q"]))


def test_confirm_multi_row_eof_raises_user_rejected():
    plan = ActionPlan([
        Action("Mon", None, "5.217A", Kind.CREATE),
        Action("Tue", "5.235A", "5.217A", Kind.REPLACE),
    ])
    with pytest.raises(UserRejected):
        confirm(plan, make_out(), stdin=ScriptedStdin([]))


def test_confirm_multi_row_unrecognised_input_reprompts_without_crashing():
    plan = ActionPlan([
        Action("Mon", None, "5.217A", Kind.CREATE),
        Action("Tue", "5.235A", "5.217A", Kind.REPLACE),
    ])
    result = confirm(plan, make_out(), stdin=ScriptedStdin(["xyz", "2"]))
    assert result.actions[0].selected is False
    assert result.actions[1].selected is True


def test_confirm_out_of_range_digit_reprompts_instead_of_selecting_nothing():
    # A digit that parses but names no row (e.g. "9" on a 2-row plan) used
    # to fall through to `select_only([9])`, which silently deselects
    # every row -- indistinguishable from "the user chose nothing" and with
    # no warning that it was actually a typo. It must reprompt instead, the
    # same as genuinely unrecognised input does.
    plan = ActionPlan([
        Action("Mon", None, "5.217A", Kind.CREATE),
        Action("Tue", "5.235A", "5.217A", Kind.REPLACE),
    ])
    result = confirm(plan, make_out(), stdin=ScriptedStdin(["9", "2"]))
    assert result.actions[0].selected is False
    assert result.actions[1].selected is True


def test_confirm_partially_out_of_range_digits_still_selects_the_valid_ones():
    # A mix of valid and invalid digits ("1 9") is not the all-invalid
    # case above -- it applies the valid selection immediately, same as
    # `select_only` always has.
    plan = ActionPlan([
        Action("Mon", None, "5.217A", Kind.CREATE),
        Action("Tue", "5.235A", "5.217A", Kind.REPLACE),
    ])
    result = confirm(plan, make_out(), stdin=ScriptedStdin(["1 9"]))
    assert result.actions[0].selected is True
    assert result.actions[1].selected is False


# -- confirm(): nothing selectable (all NOOP/BLOCKED) skips the prompt ------

def test_confirm_all_noop_skips_the_prompt_and_returns_unchanged():
    plan = ActionPlan([
        Action("Mon", "5.217A", "5.217A", Kind.NOOP,
              reason="already booked (1st favourite)"),
        Action("Tue", None, None, Kind.BLOCKED, reason="nothing available"),
    ])
    stdin = ScriptedStdin([])                   # would raise EOF if read
    result = confirm(plan, make_out(), stdin=stdin)
    assert result.actions[0].selected is False
    assert result.actions[1].selected is False


def test_confirm_all_noop_prints_nothing_to_change():
    out = make_out()
    plan = ActionPlan([Action("Mon", "5.217A", "5.217A", Kind.NOOP,
                             reason="already booked (1st favourite)")])
    confirm(plan, out, stdin=ScriptedStdin([]))
    assert "nothing to change" in out._stdout.getvalue()


def test_confirm_all_noop_skips_prompt_even_with_yes():
    plan = ActionPlan([Action("Mon", "5.217A", "5.217A", Kind.NOOP,
                             reason="already booked (1st favourite)")])
    result = confirm(plan, make_out(), yes=True, stdin=ScriptedStdin([]))
    assert result.actions[0].selected is False


def test_confirm_all_noop_skips_prompt_in_json_mode():
    # Would raise UserRejected ("--json has no prompt") if it reached the
    # json-mode guard -- nothing-to-change is decided before that check.
    plan = ActionPlan([Action("Mon", "5.217A", "5.217A", Kind.NOOP,
                             reason="already booked (1st favourite)")])
    result = confirm(plan, make_out(json_mode=True), stdin=ScriptedStdin([]))
    assert result.actions[0].selected is False


def test_confirm_all_noop_skips_prompt_non_interactive():
    # Same: would raise UserRejected ("not an interactive terminal") if it
    # reached the tty guard.
    plan = ActionPlan([Action("Mon", "5.217A", "5.217A", Kind.NOOP,
                             reason="already booked (1st favourite)")])
    result = confirm(plan, make_out(), stdin=io.StringIO())
    assert result.actions[0].selected is False


# -- execute() -----------------------------------------------------------------

def test_execute_runs_only_selected_actions():
    calls = []
    plan = ActionPlan([
        Action("Mon", None, "5.217A", Kind.CREATE, run=lambda: calls.append("mon")),
        Action("Tue", None, None, Kind.BLOCKED, reason="nothing available",
              run=lambda: calls.append("tue")),
    ])
    code = execute(plan, make_out())
    assert calls == ["mon"]
    assert code == ExitCode.OK


def test_execute_reports_success_rows():
    out = make_out()
    plan = ActionPlan([Action("Mon", None, "5.217A", Kind.CREATE, run=lambda: None)])
    execute(plan, out)
    assert "✓" in out._stdout.getvalue()
    assert "Mon" in out._stdout.getvalue()


def test_execute_reports_failure_and_continues():
    from fs_cli.errors import BusinessRuleRefused
    calls = []

    def fail():
        raise BusinessRuleRefused("desk limit reached")

    out = make_out()
    plan = ActionPlan([
        Action("Mon", None, "5.217A", Kind.CREATE, run=fail),
        Action("Tue", None, "5.235A", Kind.CREATE, run=lambda: calls.append("tue")),
    ])
    code = execute(plan, out)
    assert calls == ["tue"]                    # second row still ran
    assert code == ExitCode.BUSINESS_RULE
    text = out._stdout.getvalue()
    assert "✗" in text
    assert "✓" in text


def test_execute_reports_the_fserror_hint_on_a_failed_row():
    # Group F: the per-row failure handler printed only `str(e)`, dropping
    # `FsError.hint` -- so the refusal-kind hints `api.py` computes for
    # business-rule refusals never reached the user on this path, even
    # though `cli.py`'s top-level handler prints the hint for a whole-
    # command-aborting error.
    from fs_cli.errors import BusinessRuleRefused

    def fail():
        raise BusinessRuleRefused("desk limit reached",
                                  hint="release another desk first")

    out = make_out()
    plan = ActionPlan([Action("Mon", None, "5.217A", Kind.CREATE, run=fail)])
    execute(plan, out)
    assert "release another desk first" in out._stdout.getvalue()


def test_execute_reports_a_release_with_a_cross_not_a_tick():
    # A successful RELEASE (removing a member/desk/booking) is still a
    # removal, not a creation -- the glyph should say so, not reuse CREATE's
    # tick regardless of kind.
    out = make_out()
    plan = ActionPlan([Action("Jane Doe", "member", None, Kind.RELEASE,
                              run=lambda: None)])
    execute(plan, out)
    text = out._stdout.getvalue()
    assert "✗" in text
    assert "✓" not in text


def test_execute_returns_ok_when_nothing_selected():
    plan = ActionPlan([Action("Mon", None, None, Kind.BLOCKED,
                             reason="nothing available")])
    assert execute(plan, make_out()) == ExitCode.OK


# -- confirm()/execute(): --json --------------------------------------------

def test_confirm_json_without_yes_raises_without_reading_stdin():
    plan = ActionPlan([Action("Mon", None, "5.217A", Kind.CREATE)])
    stdin = ScriptedStdin([""])
    with pytest.raises(UserRejected):
        confirm(plan, make_out(json_mode=True), yes=False, stdin=stdin)
    assert stdin._lines == [""]                # never consumed


def test_confirm_json_with_yes_still_selects_everything():
    plan = ActionPlan([Action("Mon", None, "5.217A", Kind.CREATE,
                             selected=False)])
    result = confirm(plan, make_out(json_mode=True), yes=True,
                     stdin=io.StringIO())
    assert result.actions[0].selected is True


def test_execute_json_emits_a_row_per_executed_action():
    out = make_out(json_mode=True)
    plan = ActionPlan([Action("Mon", None, "5.217A", Kind.CREATE,
                             run=lambda: None)])
    execute(plan, out)
    out.finish()
    import json
    doc = json.loads(out._stdout.getvalue())
    assert doc["actions"] == [{"subject": "Mon", "kind": "CREATE",
                               "result": "ok"}]


def test_execute_json_records_the_error():
    def fail():
        raise CommError("boom")

    out = make_out(json_mode=True)
    plan = ActionPlan([Action("Mon", None, "5.217A", Kind.CREATE, run=fail)])
    execute(plan, out)
    out.finish()
    import json
    doc = json.loads(out._stdout.getvalue())
    assert doc["actions"][0]["result"] == "error"
    assert "boom" in doc["actions"][0]["error"]


# -- execute(): a stray non-FsError doesn't abort the rest of the plan ------

# -- empty plan --------------------------------------------------------------

def test_confirm_on_empty_plan_returns_without_reading_stdin():
    plan = ActionPlan([])
    stdin = ScriptedStdin([])                   # would raise EOF if read
    result = confirm(plan, make_out(), stdin=stdin)
    assert result.actions == []


def test_execute_on_empty_plan_returns_ok():
    assert execute(ActionPlan([]), make_out()) == ExitCode.OK


def test_execute_survives_an_unexpected_exception_in_one_row():
    calls = []
    plan = ActionPlan([
        Action("Mon", None, "5.217A", Kind.CREATE,
              run=lambda: (_ for _ in ()).throw(KeyError("oops"))),
        Action("Tue", None, "5.235A", Kind.CREATE,
              run=lambda: calls.append("tue")),
    ])
    code = execute(plan, make_out())
    assert calls == ["tue"]
    assert code == ExitCode.UNEXPECTED


# -- pick_one() ---------------------------------------------------------

def test_pick_one_single_candidate_returns_it_without_prompting():
    # Would raise EOF if it ever touched stdin -- a single candidate is
    # not a choice, so there is nothing to prompt for.
    stdin = ScriptedStdin([])
    result = pick_one(["only"], make_out(), str, stdin=stdin)
    assert result == "only"


def test_pick_one_prompts_and_returns_the_chosen_candidate():
    stdin = ScriptedStdin(["2"])
    result = pick_one(["a", "b", "c"], make_out(), str, stdin=stdin)
    assert result == "b"


def test_pick_one_reprompts_on_invalid_input():
    stdin = ScriptedStdin(["9", "abc", "1"])
    result = pick_one(["a", "b"], make_out(), str, stdin=stdin)
    assert result == "a"


def test_pick_one_q_quits():
    stdin = ScriptedStdin(["q"])
    with pytest.raises(UserRejected):
        pick_one(["a", "b"], make_out(), str, stdin=stdin)


def test_pick_one_esc_quits():
    stdin = ScriptedStdin(["\x1b"])
    with pytest.raises(UserRejected):
        pick_one(["a", "b"], make_out(), str, stdin=stdin)


def test_pick_one_eof_raises_user_rejected():
    stdin = ScriptedStdin([])
    with pytest.raises(UserRejected):
        pick_one(["a", "b"], make_out(), str, stdin=stdin)


def test_pick_one_json_mode_raises_without_reading_stdin():
    stdin = ScriptedStdin(["1"])
    with pytest.raises(UserRejected):
        pick_one(["a", "b"], make_out(json_mode=True), str, stdin=stdin)
    assert stdin._lines == ["1"]                # never consumed


def test_pick_one_non_interactive_stdin_raises_user_rejected():
    # A plain StringIO answers isatty() False, like ScriptedStdin([]) does
    # NOT -- this is the "piped, no --yes-equivalent to fall back on" case.
    with pytest.raises(UserRejected):
        pick_one(["a", "b"], make_out(), str, stdin=io.StringIO("1\n"))


def test_pick_one_empty_candidates_is_a_programmer_error():
    with pytest.raises(ValueError):
        pick_one([], make_out(), str, stdin=ScriptedStdin([]))
