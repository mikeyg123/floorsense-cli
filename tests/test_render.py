"""Output rendering.

Three rules apply everywhere and are what these tests defend:
  * a date is ALWAYS rendered by fmt_date -- never a raw dd/mm/yyyy;
  * a desk is ALWAYS rendered by fmt_desk -- short form plus group rank;
  * warnings go to stderr and data to stdout, so `fs list | ...` stays clean
    and the locker banner can never corrupt a pipe.
In --json mode the third rule inverts: warnings join the payload, because a
JSON consumer reading only stdout must still see them.
"""

import datetime as dt
import json

from fs_cli.render import THEME, Output, table

FRI = dt.date(2026, 8, 21)
GROUPS = {"favourite": ["L5.D.217A", "L5.D.235A"]}


def out(**kw):
    kw.setdefault("today", FRI)
    kw.setdefault("groups", GROUPS)
    kw.setdefault("color", False)
    return Output(**kw)


# --- tables ----------------------------------------------------------------

def test_table_pads_columns_to_align():
    got = table(["Date", "Desk"], [["Today", "5.217A"], ["Tomorrow", "5.235A"]])
    lines = got.splitlines()
    assert lines[1].index("5.217A") == lines[2].index("5.235A")


def test_table_with_no_rows_is_empty():
    assert table(["Date"], []) == ""


def test_table_tolerates_none_cells():
    got = table(["A", "B"], [["x", None]])
    assert "None" not in got


def test_table_does_not_pad_the_last_column():
    # Trailing whitespace makes copy-paste and golden transcripts messy.
    for line in table(["A", "B"], [["x", "y"]]).splitlines():
        assert line == line.rstrip()


def test_table_separates_columns_with_three_spaces():
    got = table(["A", "B"], [["x", "y"]])
    assert got.splitlines()[1] == "x   y"


def test_cell_style_colours_a_non_last_column_without_perturbing_widths():
    # `row_style` alone can only safely colour the LAST column -- it sees
    # the already-joined line, so wrapping ANSI around anything else risks
    # corrupting later columns if it ever has to guess at cell boundaries.
    # `cell_style(row_index, col_index, padded_cell, raw_cell)` applies
    # colour to one cell BEFORE it's joined, using the width already
    # computed from the raw (un-ANSI'd) text -- so a column after the
    # coloured one still lines up.
    def wrap(row_index, col_index, padded, raw):
        return f"[{raw}]" + padded[len(raw):] if col_index == 1 else padded

    got = table(["Day", "Desk", "Note"],
               [["Monday", "5.217A", "checked in"],
                ["Tuesday", "5.2", "not checked in"]],
               cell_style=wrap)
    lines = got.splitlines()
    # the coloured column's own trailing padding is unaffected...
    assert "[5.217A]" in lines[1] and "[5.2]" in lines[2]
    # ...and the column after it still aligns.
    assert lines[1].index("checked") == lines[2].index("not checked")


def test_header_style_dims_the_whole_header_line_without_perturbing_widths():
    # Same "whole already-joined line" safety as `row_style` -- applied
    # after alignment, so it can't shift where a data column starts.
    got = table(["Day", "Desk"], [["Monday", "5.217A"]],
               header_style=lambda line: f"<{line}>")
    lines = got.splitlines()
    assert lines[0] == "<Day      Desk>"
    assert lines[1] == "Monday   5.217A"


def test_header_style_never_runs_with_no_header_row():
    calls = []
    table(None, [["x", "y"]], header_style=lambda line: calls.append(line) or line)
    assert calls == []


# --- semantic colour --------------------------------------------------------

def test_style_looks_up_theme():
    o = out(color=True)
    assert o.style("good", "x") == o.green("x")
    assert o.style("danger", "x") == o.red("x")
    assert o.style("identifier", "x") == o.bold("x")
    assert o.style("label", "x") == o.style("muted", "x") == o.dim("x")
    assert o.style("attention", "x") == o.yellow("x")


def test_semantic_methods_match_their_theme_entry():
    o = out(color=True)
    assert o.identifier("x") == o.style("identifier", "x")
    assert o.label("x") == o.style("label", "x")
    assert o.muted("x") == o.style("muted", "x")
    assert o.good("x") == o.style("good", "x")
    assert o.attention("x") == o.style("attention", "x")
    assert o.danger("x") == o.style("danger", "x")


def test_reconfiguring_theme_changes_a_semantic_method(monkeypatch):
    # The point of THEME: reassigning one entry changes every call site that
    # goes through the semantic method, with no call site touched.
    o = out(color=True)
    monkeypatch.setitem(THEME, "danger", "blue")
    assert o.danger("x") == o.blue("x")


def test_fmt_desk_is_bold_when_colour_is_on():
    o = out(color=True)
    assert o.fmt_desk("L5.D.217A") == o.bold("5.217A (1st favourite)")


# --- the two formatting rules ----------------------------------------------

def test_dates_render_relatively():
    o = out()
    assert o.fmt_date(FRI) == "Today 21st Aug"
    assert o.fmt_date(dt.date(2026, 8, 24)) == "Monday 24th Aug"


def test_fmt_dates_joins_several():
    o = out()
    assert o.fmt_dates([FRI, dt.date(2026, 8, 24)]) == \
        "Today 21st Aug, Monday 24th Aug"


def test_desks_render_with_group_rank():
    o = out()
    assert o.fmt_desk("L5.D.217A") == "5.217A (1st favourite)"
    assert o.fmt_desk("L5.D.410A") == "5.410A"


# --- colour ----------------------------------------------------------------

def test_colour_off_produces_no_escape_codes():
    o = out(color=False)
    assert "\x1b[" not in o.bold("x") + o.dim("y") + o.green("z") + o.red("w")


def test_colour_on_produces_escape_codes():
    o = out(color=True)
    assert "\x1b[" in o.bold("x")


def test_reverse_wraps_in_the_sgr_reverse_video_code():
    o = out(color=True)
    assert o.reverse("x") == "\x1b[7mx\x1b[0m"


def test_reverse_off_produces_no_escape_codes():
    o = out(color=False)
    assert o.reverse("x") == "x"


def test_magenta_is_ansi_colour_5():
    o = out(color=True)
    assert o.magenta("x") == "\x1b[35mx\x1b[0m"


def test_teammate_is_the_magenta_semantic_style():
    # `fs map`'s `show_team_on_map` highlight -- see map_cmd.py's
    # `desk_glyph_kind`/`STYLE_METHOD_BY_KIND`.
    o = out(color=True)
    assert o.teammate("x") == o.magenta("x")


def test_json_mode_never_emits_colour():
    # Even if colour was explicitly asked for -- ANSI in a JSON string is
    # never what the caller wanted.
    o = out(color=True, json_mode=True)
    assert "\x1b[" not in o.bold("x")


# --- warnings --------------------------------------------------------------

def test_text_mode_sends_warnings_to_stderr(capsys):
    o = out()
    o.print("data line")
    o.warn("locker expires soon")
    o.finish()
    captured = capsys.readouterr()
    assert captured.out.strip() == "data line"
    assert "locker expires soon" in captured.err


def test_json_mode_puts_warnings_in_the_payload(capsys):
    o = out(json_mode=True)
    o.warn("locker expires soon")
    o.emit({"bookings": []})
    o.finish()
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["warnings"] == ["locker expires soon"]
    assert payload["bookings"] == []
    assert captured.err == ""


def test_json_mode_suppresses_ordinary_prints(capsys):
    o = out(json_mode=True)
    o.print("a human-readable table")
    o.emit({"bookings": []})
    o.finish()
    assert "human-readable" not in capsys.readouterr().out


def test_intent_goes_to_stderr_not_stdout(capsys):
    """An intent line is neither data nor a warning (PLAN.md item 12) --
    it goes to stderr like a warning, but with no --json equivalent, so a
    piped `fs list | ...` never sees it on stdout."""
    o = out()
    o.intent("Showing bookings for Jane Doe, Tuesday 25th Aug")
    captured = capsys.readouterr()
    assert "Showing bookings for Jane Doe" in captured.err
    assert captured.out == ""


def test_intent_is_suppressed_in_json_mode(capsys):
    o = out(json_mode=True)
    o.intent("Showing bookings for Jane Doe")
    o.emit({"bookings": []})
    o.finish()
    assert "Jane Doe" not in capsys.readouterr().err


def test_json_payload_has_a_warnings_key_even_when_empty(capsys):
    o = out(json_mode=True)
    o.emit({"bookings": []})
    o.finish()
    assert json.loads(capsys.readouterr().out)["warnings"] == []


# --- JSON value conventions ------------------------------------------------

def test_timestamps_serialise_as_iso_strings(capsys):
    o = out(json_mode=True)
    o.emit({"when": dt.datetime(2026, 8, 21, 9, 30), "day": FRI})
    o.finish()
    payload = json.loads(capsys.readouterr().out)
    assert payload["when"] == "2026-08-21T09:30:00"
    assert payload["day"] == "2026-08-21"


def test_json_is_emitted_once_even_with_several_emits(capsys):
    o = out(json_mode=True)
    o.emit({"a": 1})
    o.emit({"b": 2})
    o.finish()
    payload = json.loads(capsys.readouterr().out)      # would raise if doubled
    assert payload["a"] == 1 and payload["b"] == 2
