"""`fs map [<floor>] [<date>]` -- static per-floor ASCII map, own-desk
highlight, terminal-width crop. See docs/floorplan-map-manual.md before
touching rendering logic.
"""

import datetime as dt
import io
import pathlib

import pytest

from fs_cli.catalog import CacheStore, Catalog, DeskState
from fs_cli.commands.map_cmd import (
    GLYPH_BY_KIND, PLANID_LEVEL5, PLANID_LEVEL6, classify_token, cluster,
    cmd_map, crop_window, desk_at_click, desk_centers, desk_glyph_kind,
    draw_box, draw_polyline, floor_aliases, floor_names, header_line,
    local_ranks, nearest_desk, plan_for_cursor, quantize_axis, render_floor,
    resolve_floor, row_pitch, scroll_window, terminal_columns,
)
from live_write_api import LiveWriteApi
from fs_cli.commands.map_cmd import (
    _run_live, _status_line, _initial_cursor_key, _render_lines)
from fs_cli.errors import ExitCode, UsageError
from fs_cli.fixtures import FixtureApi
from fs_cli.render import Output

GOLDEN_DIR = pathlib.Path(__file__).parent / "golden"
TODAY = dt.date(2026, 8, 25)   # a Tuesday
MON = dt.date(2026, 8, 31)

#: Stand-in for what `floor_names(catalog)` derives from a real deployment's
#: desks -- used wherever a test needs floor tokens/labels but has no real
#: `Catalog` in scope (unit tests for `classify_token`/`header_line`
#: themselves). Integration tests that DO have a real `catalog` call
#: `floor_names(catalog)` directly instead, so they'd catch a regression in
#: derivation itself, not just in the two functions that consume it.
TEST_FLOOR_NAMES = {PLANID_LEVEL5: "Level 5", PLANID_LEVEL6: "Level 6"}
TEST_FLOOR_ALIASES = floor_aliases(TEST_FLOOR_NAMES)


def test_desk_centers_computes_the_bounding_box_midpoint():
    polys = {"A": {"id": "A", "points": [
        {"x": 0, "y": 0}, {"x": 10, "y": 0},
        {"x": 10, "y": 20}, {"x": 0, "y": 20}]}}
    centers = desk_centers(polys, {"A"})
    assert centers["A"] == (5.0, 10.0, 10, 20)


def test_desk_centers_drops_polys_not_in_the_catalog():
    # Level 6's 3 ghost polys (no catalog entry) must not become desks.
    polys = {"ghost": {"id": "ghost", "points": [
        {"x": 0, "y": 0}, {"x": 1, "y": 1}]}}
    assert desk_centers(polys, {"real-desk-only"}) == {}


def test_cluster_groups_desks_within_gap_and_separates_ones_further_out():
    centers = {
        "A": (0, 0, 10, 10), "B": (12, 0, 10, 10),   # 2px gap -- touches
        "C": (100, 0, 10, 10),                        # far away -- alone
    }
    clusters = cluster(centers, gap=5)
    groups = sorted(sorted(g) for g in clusters)
    assert groups == [["A", "B"], ["C"]]


def test_local_ranks_buckets_by_gap_not_by_absolute_value():
    values = {"a": 0, "b": 1, "c": 50, "d": 51}
    ranks, n = local_ranks(values, threshold=5)
    assert n == 2
    assert ranks["a"] == ranks["b"]
    assert ranks["c"] == ranks["d"]
    assert ranks["a"] != ranks["c"]


def test_nearest_desk_finds_the_clear_winner_in_each_direction():
    positions = {
        "right": (0, 10), "left": (0, -10),
        "down": (10, 0), "up": (-10, 0),
    }
    cursor = (0, 0)
    assert nearest_desk(cursor, positions, "right") == "right"
    assert nearest_desk(cursor, positions, "left") == "left"
    assert nearest_desk(cursor, positions, "down") == "down"
    assert nearest_desk(cursor, positions, "up") == "up"


def test_nearest_desk_breaks_ties_with_the_perpendicular_weight():
    # From (0, 0) moving right: "A" is 4 cols over, 0 rows off
    # (score = 4). "B" is 2 cols over, 2 rows off (score = 2 + 2*2 = 6).
    # A clear winner once weighted, even though B is closer on the column
    # axis alone.
    positions = {"A": (0, 4), "B": (2, 2)}
    assert nearest_desk((0, 0), positions, "right") == "A"


def test_nearest_desk_prefers_the_weighted_winner_over_raw_euclidean_distance():
    # "close" is Euclidean-nearer (row=1, col=1 -> sqrt(2)) but scores
    # 1 + 2*1 = 3. "far" is Euclidean-further (row=0, col=3) but scores
    # 3 + 2*0 = 3 too... make far strictly better: row=0, col=2 -> score 2.
    positions = {"close": (1, 1), "far": (0, 2)}
    assert nearest_desk((0, 0), positions, "right") == "far"


def test_nearest_desk_returns_none_with_no_candidate_in_that_direction():
    positions = {"left_only": (0, -5)}
    assert nearest_desk((0, 0), positions, "right") is None


def test_nearest_desk_excludes_desks_exactly_on_the_perpendicular_axis():
    # dc == 0 for a "right" search must not count as a candidate -- the
    # filter table in the design spec is a strict inequality (dc > 0).
    positions = {"same_column": (5, 0)}
    assert nearest_desk((0, 0), positions, "right") is None


def test_row_pitch_is_the_median_short_side():
    centers = {"a": (0, 0, 40, 100), "b": (0, 0, 30, 90), "c": (0, 0, 50, 80)}
    assert row_pitch(centers) == 40


def test_quantize_axis_snaps_to_the_nearest_grid_multiple():
    values = {"a": 12, "b": 18, "c": 41}
    assert quantize_axis(values, grid=10) == {"a": 10, "b": 20, "c": 40}


def test_draw_polyline_draws_a_closed_rectangle():
    grid = [[" "] * 6 for _ in range(4)]
    draw_polyline(grid, [(0, 0), (0, 5), (3, 5), (3, 0)])
    # Top and bottom edges are mostly horizontal (corners may be claimed by
    # perpendicular segments due to draw order; a box-drawing char is fine).
    assert grid[0][1:5] == list("────")
    assert grid[3][0:5] == list("─────")
    # Left and right edges are vertical
    assert grid[1][0] == "│" and grid[1][5] == "│"


def test_draw_polyline_leaves_a_gap_at_both_ends():
    grid = [[" "] * 10 for _ in range(1)]
    draw_polyline(grid, [(0, 0), (0, 9)], closed=False, gap_ends=2)
    assert grid[0][0] == " " and grid[0][1] == " "
    assert grid[0][9] == " " and grid[0][8] == " "
    assert grid[0][5] == "─"


def test_draw_box_draws_an_outline_and_centers_the_label():
    grid = [[" "] * 12 for _ in range(4)]
    draw_box(grid, 0, 0, 3, 11, label="HI")
    assert "".join(grid[0]).strip("─") == ""
    assert "HI" in "".join(grid[1])


def test_draw_box_label_overwrites_whatever_is_under_it():
    grid = [[" "] * 5 for _ in range(3)]
    grid[1][1] = "X"
    draw_box(grid, 0, 0, 2, 4, label="AB")
    assert "".join(grid[1]) == "│AB │"


def test_desk_glyph_kind_highlight_always_wins():
    # A highlighted desk is "yours" even though its own state says
    # booked -- highlight precedence must beat live state, not just
    # glyph precedence (phases 1-2's own rule, extended to colour).
    states = {"A": DeskState(desk=None, free=False, book_advance=False)}
    assert desk_glyph_kind("A", {"A"}, states) == "yours"


def test_desk_glyph_kind_free_and_bookable():
    states = {"A": DeskState(desk=None, free=True, book_advance=True)}
    assert desk_glyph_kind("A", set(), states) == "free"


def test_desk_glyph_kind_free_but_not_book_advance_is_restricted():
    states = {"A": DeskState(desk=None, free=True, book_advance=False)}
    assert desk_glyph_kind("A", set(), states) == "restricted"


def test_desk_glyph_kind_not_free_is_booked():
    states = {"A": DeskState(desk=None, free=False, book_advance=True)}
    assert desk_glyph_kind("A", set(), states) == "booked"


def test_desk_glyph_kind_missing_from_a_given_dict_defaults_to_booked():
    # `desk_states` was supplied (live-colour mode is on) but this
    # particular key isn't in it -- the conservative default, same as
    # an actually-booked desk, not "free".
    assert desk_glyph_kind("ghost", set(), {}) == "booked"


def test_desk_glyph_kind_none_desk_states_means_legacy_free_everywhere():
    # Phases-1-2 backward compatibility: omitting `desk_states`
    # entirely (not an empty dict -- `None`) must not turn on
    # live-colour classification at all.
    assert desk_glyph_kind("A", set(), None) == "free"


def test_glyph_by_kind_has_exactly_the_four_kinds():
    assert set(GLYPH_BY_KIND) == {"free", "restricted", "booked", "yours"}
    assert GLYPH_BY_KIND["free"] == "▢"
    assert GLYPH_BY_KIND["restricted"] == "■"
    assert GLYPH_BY_KIND["booked"] == "■"
    assert GLYPH_BY_KIND["yours"] == "▣"


def _catalog(tmp_path):
    return Catalog(FixtureApi(), CacheStore(tmp_path / "cache.json"))


def _render(tmp_path, planid, highlight_keys=frozenset(), desk_states=None):
    catalog = _catalog(tmp_path)
    polys = catalog.deskpolys(planid)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == planid}
    img_w, img_h = catalog.floor_image_size(planid)
    return render_floor(planid, polys, catalog_keys, img_w, img_h,
                        highlight_keys=highlight_keys, desk_states=desk_states)


def _synthetic_desk_row(keys_and_x):
    # 4 desks in one row, 10 wide x 20 tall, spaced 20px apart on
    # centre -- well within cluster gap (short_side*1.1 = 11 with a
    # 10px short side) so they land in one block, and well past the
    # column-rank threshold (short_side*0.6 = 6) so each gets its own
    # column rank. Planid 999 is deliberately not in any *_BY_FLOOR
    # table, so no wall/partition/room-box/zone-label drawing muddies
    # the desk-only assertions below.
    polys = {}
    for key, x in keys_and_x:
        polys[key] = {"id": key, "points": [
            {"x": x, "y": 0}, {"x": x + 10, "y": 0},
            {"x": x + 10, "y": 20}, {"x": x, "y": 20}]}
    return polys


@pytest.mark.parametrize("planid,golden_name,desk_count", [
    (PLANID_LEVEL5, "map-level5.txt", 262),
    (PLANID_LEVEL6, "map-level6.txt", 103),
])
def test_render_floor_matches_the_signed_off_prototype_output(
        tmp_path, planid, golden_name, desk_count):
    grid, highlight_positions, _kinds, desk_positions = _render(tmp_path, planid)

    assert highlight_positions == set()
    assert sum(row.count("▢") for row in grid) == desk_count
    assert len(desk_positions) == desk_count

    rendered_lines = ["".join(row).rstrip() for row in grid]
    expected_lines = (GOLDEN_DIR / golden_name).read_text().splitlines()
    # Compare as lists of lines, not `"\n".join(rendered_lines) ==
    # golden_text.rstrip("\n")` (the brief's original form): both floors'
    # grids end in wholly-blank rows (1 for level5, 3 for level6), and
    # `"\n".join` is lossy for exactly one trailing blank element --
    # `"\n".join([..., "x", ""])` == `"...x\n"`, indistinguishable from
    # `"\n".join([..., "x"])`, so the last blank row silently vanishes
    # from `rendered` while surviving in the golden file (each row was
    # captured via its own `print()` call, blank ones included, so the
    # file has an explicit trailing "\n" per row). Comparing line-lists
    # (`.splitlines()` on the golden text, no join at all on the grid
    # side) avoids that asymmetry and checks every row, blank trailing
    # ones included, on both sides. Confirmed this is a test-construction
    # issue, not a rendering bug: `render_floor`'s grid dimensions match
    # `experiments/floorplan_ascii.py`'s own printed "grid WxH" header
    # exactly, and running that prototype directly reproduces the golden
    # files byte-for-byte.
    assert rendered_lines == expected_lines


def test_render_floor_ghost_polys_never_become_desks(tmp_path):
    # Level 6's 3 ghost polys (no catalog entry) must not inflate the
    # desk count -- confirmed present in `polys` but absent from the
    # rendered glyph count.
    catalog = _catalog(tmp_path)
    polys = catalog.deskpolys(PLANID_LEVEL6)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == PLANID_LEVEL6}
    assert len(polys) > len(catalog_keys)

    grid, _highlight, _kinds, _positions = _render(tmp_path, PLANID_LEVEL6)
    assert sum(row.count("▢") for row in grid) == len(catalog_keys)


def test_render_floor_marks_a_highlighted_desk_with_a_distinct_glyph(tmp_path):
    catalog = _catalog(tmp_path)
    some_key = next(iter(
        d.key for d in catalog.desks() if d.planid == PLANID_LEVEL5))

    grid, highlight_positions, _kinds, _positions = _render(tmp_path, PLANID_LEVEL5,
                                        highlight_keys={some_key})

    assert len(highlight_positions) == 1
    r, c = next(iter(highlight_positions))
    assert grid[r][c] == "▣"
    # Every other desk is untouched -- still the ordinary glyph.
    assert sum(row.count("▢") for row in grid) == 261


def test_render_floor_classifies_each_desk_glyph_from_desk_states():
    polys = _synthetic_desk_row(
        [("A", 0), ("B", 20), ("C", 40), ("D", 60)])
    catalog_keys = {"A", "B", "C", "D"}
    desk_states = {
        "A": DeskState(desk=None, free=False, book_advance=False),
        "B": DeskState(desk=None, free=True, book_advance=True),
        "C": DeskState(desk=None, free=True, book_advance=False),
        # "D" deliberately absent -> defaults to "booked".
    }
    grid, highlight_positions, kinds, desk_positions = render_floor(
        999, polys, catalog_keys, img_w=100, img_h=100,
        highlight_keys={"A"}, desk_states=desk_states)

    assert len(highlight_positions) == 1
    assert sorted(kinds.values()) == ["booked", "free", "restricted", "yours"]
    assert set(desk_positions) == {"A", "B", "C", "D"}
    flat = "".join("".join(row) for row in grid)
    assert flat.count("▣") == 1
    assert flat.count("▢") == 1
    assert flat.count("■") == 2


def test_render_floor_desk_states_none_keeps_the_legacy_free_glyph():
    polys = _synthetic_desk_row([("A", 0), ("B", 20)])
    catalog_keys = {"A", "B"}
    grid, highlight_positions, kinds, desk_positions = render_floor(
        999, polys, catalog_keys, img_w=100, img_h=100)

    assert highlight_positions == set()
    assert set(kinds.values()) == {"free"}
    assert set(desk_positions) == {"A", "B"}
    flat = "".join("".join(row) for row in grid)
    assert flat.count("▢") == 2
    assert flat.count("■") == 0


def test_render_floor_desk_positions_is_the_reverse_of_desk_kind_positions(tmp_path):
    grid, _highlight, kinds, positions = _render(tmp_path, PLANID_LEVEL5)
    assert set(positions.values()) == set(kinds)
    for _key, pos in positions.items():
        assert pos in kinds
        assert grid[pos[0]][pos[1]] == GLYPH_BY_KIND[kinds[pos]]


class _FakeStream:
    def __init__(self, is_tty):
        self._is_tty = is_tty

    def isatty(self):
        return self._is_tty


def test_terminal_columns_is_none_when_not_a_tty():
    assert terminal_columns(_FakeStream(False)) is None


def test_terminal_columns_is_none_when_isatty_raises():
    class Broken:
        def isatty(self):
            raise ValueError("boom")
    assert terminal_columns(Broken()) is None


def test_terminal_columns_reads_shutil_when_a_tty(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "get_terminal_size",
                        lambda **kwargs: shutil.os.terminal_size((100, 40)))
    assert terminal_columns(_FakeStream(True)) == 100


def test_terminal_columns_is_none_on_a_nonsensical_size(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "get_terminal_size",
                        lambda **kwargs: shutil.os.terminal_size((0, 40)))
    assert terminal_columns(_FakeStream(True)) is None


def test_crop_window_is_none_with_no_terminal_width():
    assert crop_window(n_cols=200, highlight_cols=set(), terminal_columns=None) is None


def test_crop_window_anchors_left_with_no_highlight():
    assert crop_window(n_cols=200, highlight_cols=set(), terminal_columns=80) == (0, 80)


def test_crop_window_fits_whole_floor_when_narrower_than_terminal():
    assert crop_window(n_cols=50, highlight_cols=set(), terminal_columns=80) == (0, 50)


def test_crop_window_clips_right_past_the_highlighted_desk():
    # rightmost highlighted col = 120, terminal = 80 -> right edge just
    # past 120, window extends left from there.
    left, right = crop_window(n_cols=200, highlight_cols={100, 120},
                              terminal_columns=80)
    assert right == 121
    assert left == 41
    assert right - left == 80


def test_crop_window_clamps_left_at_the_floors_own_edge():
    # rightmost highlighted col near the left edge, but the terminal has
    # 80 columns of room and the floor has 200 columns of content --
    # the window must use the full available terminal width rather than
    # collapsing to a sliver hugging the highlight. Anchored left (0)
    # since the highlight already falls inside the first 80 columns.
    left, right = crop_window(n_cols=200, highlight_cols={10},
                              terminal_columns=80)
    assert left == 0
    assert right == 80


def test_crop_window_right_edge_never_exceeds_the_floors_own_content():
    left, right = crop_window(n_cols=50, highlight_cols={49},
                              terminal_columns=80)
    assert right == 50
    assert left == 0


def test_scroll_window_is_none_with_no_terminal_size():
    assert scroll_window(prev=None, pos=5, n_total=200, terminal_size=None) is None


def test_scroll_window_first_frame_anchors_left_with_margin():
    # No `prev` yet -- cursor near the left edge, so the anchor is still 0
    # (never negative), same as `crop_window`'s left clamp.
    assert scroll_window(prev=None, pos=1, n_total=200, terminal_size=80) == (0, 80)


def test_scroll_window_first_frame_gives_the_cursor_runway():
    # Cursor already well inside the floor on the very first frame --
    # window is anchored with `margin` cells of space *before* the cursor,
    # not jammed against either edge.
    left, right = scroll_window(prev=None, pos=50, n_total=200, terminal_size=80)
    assert left == 50 - 4
    assert right == left + 80


def test_scroll_window_holds_still_while_cursor_stays_inside_the_margin():
    # Cursor moves but never gets within `margin` of either edge of the
    # current window -- the window must not move at all. This is the
    # bug fix: `crop_window` used to recompute (and often snap back to
    # its left anchor) on every single frame regardless of how far the
    # cursor was from either edge.
    prev = (0, 80)
    assert scroll_window(prev, pos=10, n_total=200, terminal_size=80) == prev
    assert scroll_window(prev, pos=75, n_total=200, terminal_size=80) == prev


def test_scroll_window_scrolls_forward_with_runway_past_the_cursor():
    # Cursor crosses into the right margin -- window advances just enough
    # to restore the margin, so a few cells *past* the cursor (the "far
    # wall") come into view rather than the cursor sitting on the very
    # last visible cell.
    prev = (0, 80)
    left, right = scroll_window(prev, pos=78, n_total=200, terminal_size=80)
    assert right == 78 + 4 + 1
    assert left == right - 80


def test_scroll_window_does_not_scroll_back_until_cursor_nears_the_far_margin():
    # Window has already scrolled forward. Moving the cursor back but
    # still short of the *opposite* (left) margin must not move the
    # window at all -- only once the cursor gets within `margin` of the
    # window's own left edge does it scroll back, and only enough to
    # restore that margin.
    scrolled = (41, 121)
    assert scroll_window(scrolled, pos=90, n_total=200, terminal_size=80) == scrolled
    left, right = scroll_window(scrolled, pos=42, n_total=200, terminal_size=80)
    assert left == 42 - 4
    assert right == left + 80


def test_scroll_window_never_exceeds_the_floors_own_content():
    left, right = scroll_window(prev=(0, 50), pos=49, n_total=50, terminal_size=80)
    assert (left, right) == (0, 50)


def test_scroll_window_reanchors_on_resize():
    # Window width no longer matches `terminal_size` (a resize happened)
    # -- re-anchor around the cursor with runway rather than trying to
    # reuse a `prev` window sized for the old terminal.
    left, right = scroll_window(prev=(0, 80), pos=100, n_total=200, terminal_size=60)
    assert right - left == 60
    assert left == 100 - 4


def test_classify_token_recognises_a_date():
    kind, value = classify_token("mon", TODAY, TEST_FLOOR_ALIASES)
    assert kind == "date"
    assert value == MON


def test_classify_token_recognises_every_floor_spelling():
    for token, expected in [("5", 3), ("6", 4), ("level5", 3),
                            ("LEVEL6", 4), ("Level5", 3)]:
        assert classify_token(token, TODAY, TEST_FLOOR_ALIASES) == ("floor", expected)


def test_classify_token_returns_none_for_garbage():
    assert classify_token("bogus", TODAY, TEST_FLOOR_ALIASES) is None


def test_classify_token_non_floor_dates_are_unaffected_by_floor_precedence():
    # "mon" isn't a floor alias, so it still falls through to parse_date
    # and classifies as a date -- floor-first precedence only intercepts
    # the two literal collisions ("5"/"6"), not every date token.
    assert classify_token("mon", TODAY, TEST_FLOOR_ALIASES)[0] == "date"


def test_classify_token_a_non_floor_day_of_month_is_still_a_date():
    # "12" isn't a floor spelling on THIS floor set, so day-of-month
    # parsing still applies -- pins the boundary: only whatever's actually
    # in `aliases` is taken out of the date namespace, not every bare
    # number.
    assert classify_token("12", TODAY, TEST_FLOOR_ALIASES)[0] == "date"


def test_classify_token_uses_whatever_aliases_it_is_given():
    # classify_token itself no longer knows about "5"/"6"/Level anything --
    # it just consults whatever alias dict the caller derived. A building
    # with entirely different floor names/numbers works with zero code
    # changes here.
    aliases = {"groundfloor": 101, "mezzanine": 102}
    assert classify_token("GroundFloor", TODAY, aliases) == ("floor", 101)
    assert classify_token("5", TODAY, aliases)[0] == "date"  # not a floor here


class FakeDeskWithFloor:
    def __init__(self, key, planid, floor):
        self.key, self.planid, self.floor = key, planid, floor


class FakeCatalogWithDesks:
    """Just enough of `Catalog` for `floor_names`/`floor_aliases` --
    `desks()` returning objects with `.planid`/`.floor`, the same shape
    `Catalog.desks()` returns real `Desk` objects in."""

    def __init__(self, desks):
        self._desks = desks

    def desks(self):
        return self._desks


def test_floor_names_derives_display_name_from_each_floors_own_desks():
    catalog = FakeCatalogWithDesks([
        FakeDeskWithFloor("A", 3, "Level 5"),
        FakeDeskWithFloor("B", 4, "Level 6"),
        FakeDeskWithFloor("C", 4, "Level 6"),   # same floor, repeated name
    ])
    assert floor_names(catalog) == {3: "Level 5", 4: "Level 6"}


def test_floor_names_skips_desks_with_no_floor_name():
    catalog = FakeCatalogWithDesks([FakeDeskWithFloor("A", 3, None)])
    assert floor_names(catalog) == {}


def test_floor_aliases_reproduces_this_workplaces_existing_spellings():
    # Pins the exact behaviour `FLOOR_ALIASES` used to hardcode, now
    # derived instead of hand-picked.
    assert floor_aliases({3: "Level 5", 4: "Level 6"}) == {
        "level5": 3, "5": 3, "level6": 4, "6": 4,
    }


def test_floor_aliases_generalises_to_an_unfamiliar_buildings_floor_names():
    # A different building's floor names/numbering -- no "Level N" in
    # sight -- still produces usable tokens with no code change.
    aliases = floor_aliases({101: "Ground Floor", 102: "Mezzanine"})
    assert aliases == {"groundfloor": 101, "mezzanine": 102}


def test_floor_aliases_only_adds_a_bare_number_alias_when_the_name_ends_in_one():
    aliases = floor_aliases({101: "Mezzanine"})
    assert aliases == {"mezzanine": 101}    # no bare-number alias at all


def test_floor_aliases_first_floor_wins_on_a_same_building_name_collision():
    # Two towers both calling a floor "Level 5" -- documented, not fixed:
    # whichever planid iterates first claims the token.
    aliases = floor_aliases({3: "Level 5", 99: "Level 5"})
    assert aliases["level5"] == 3


class FakeDesk:
    def __init__(self, key, planid):
        self.key, self.planid = key, planid


class FakeCatalog:
    def __init__(self, desks_by_key, planids=(PLANID_LEVEL5,)):
        self._desks = desks_by_key
        self._planids = planids

    def desk_by_key(self, key):
        return self._desks.get(key)

    def planids(self):
        return list(self._planids)


class FakeApi:
    def __init__(self, booking_list=None, booking_summary=None):
        self._booking_list = booking_list or []
        self._booking_summary = booking_summary or {"days": []}

    def booking_list(self):
        return list(self._booking_list)

    def booking_summary(self):
        return self._booking_summary


def _booking(bkid, key, start, finish=None):
    return {"bkid": bkid, "key": key, "start": start,
            "finish": finish or start, "released": False}


def _ts(day):
    return int(dt.datetime.combine(day, dt.time(9, 0)).timestamp())


def test_resolve_floor_explicit_floor_has_no_fallback_and_no_highlight():
    api = FakeApi()
    catalog = FakeCatalog({})
    planid, highlight = resolve_floor(api, catalog, TODAY, TODAY,
                                      explicit_planid=4)
    assert planid == 4
    assert highlight == set()


def test_resolve_floor_explicit_floor_highlights_when_the_booking_matches():
    api = FakeApi(booking_list=[_booking(1, "L6.D.05", _ts(TODAY))])
    catalog = FakeCatalog({"L6.D.05": FakeDesk("L6.D.05", 4)})
    planid, highlight = resolve_floor(api, catalog, TODAY, TODAY,
                                      explicit_planid=4)
    assert planid == 4
    assert highlight == {"L6.D.05"}


def test_resolve_floor_explicit_floor_ignores_a_booking_on_the_other_floor():
    api = FakeApi(booking_list=[_booking(1, "L5.D.05", _ts(TODAY))])
    catalog = FakeCatalog({"L5.D.05": FakeDesk("L5.D.05", 3)})
    planid, highlight = resolve_floor(api, catalog, TODAY, TODAY,
                                      explicit_planid=4)
    assert planid == 4
    assert highlight == set()


def test_resolve_floor_no_floor_given_uses_the_covering_bookings_floor():
    api = FakeApi(booking_list=[_booking(1, "L6.D.05", _ts(TODAY))])
    catalog = FakeCatalog({"L6.D.05": FakeDesk("L6.D.05", 4)})
    planid, highlight = resolve_floor(api, catalog, TODAY, TODAY,
                                      explicit_planid=None)
    assert planid == 4
    assert highlight == {"L6.D.05"}


def test_resolve_floor_falls_back_to_the_next_booking_with_no_highlight():
    api = FakeApi(booking_list=[_booking(1, "L6.D.05", _ts(MON))])
    catalog = FakeCatalog({"L6.D.05": FakeDesk("L6.D.05", 4)})
    # Target date (TODAY) has no covering booking, but a future one exists.
    planid, highlight = resolve_floor(api, catalog, TODAY, TODAY,
                                      explicit_planid=None)
    assert planid == 4
    assert highlight == set()


def test_resolve_floor_defaults_to_the_catalogs_first_floor_with_no_bookings_at_all():
    api = FakeApi()
    catalog = FakeCatalog({}, planids=(3,))
    planid, highlight = resolve_floor(api, catalog, TODAY, TODAY,
                                      explicit_planid=None)
    assert planid == 3
    assert highlight == set()


def test_resolve_floor_default_fallback_is_not_hardcoded_to_this_workplace():
    # A different building's catalog, whose only floor is nothing like
    # this workplace's planid 3/4 -- the fallback must follow the
    # catalog, not a baked-in constant.
    api = FakeApi()
    catalog = FakeCatalog({}, planids=(701,))
    planid, highlight = resolve_floor(api, catalog, TODAY, TODAY,
                                      explicit_planid=None)
    assert planid == 701
    assert highlight == set()


def test_resolve_floor_falls_back_to_the_level5_constant_if_the_catalog_has_no_floors():
    # Genuinely nothing to go on (empty account/catalog) -- falls back to
    # the module constant rather than crashing.
    api = FakeApi()
    catalog = FakeCatalog({}, planids=())
    planid, highlight = resolve_floor(api, catalog, TODAY, TODAY,
                                      explicit_planid=None)
    assert planid == PLANID_LEVEL5
    assert highlight == set()


def test_resolve_floor_multi_desk_covering_booking_highlights_only_its_own_floor():
    api = FakeApi(booking_list=[
        _booking(1, "L6.D.05", _ts(TODAY)),
        _booking(2, "L6.D.06", _ts(TODAY)),
    ])
    catalog = FakeCatalog({"L6.D.05": FakeDesk("L6.D.05", 4),
                           "L6.D.06": FakeDesk("L6.D.06", 4)})
    planid, highlight = resolve_floor(api, catalog, TODAY, TODAY,
                                      explicit_planid=None)
    assert planid == 4
    assert highlight == {"L6.D.05", "L6.D.06"}


class Config:
    def __init__(self, book_ahead_days=10, day_opening_time=None):
        self.groups = {}
        self.teams = {}
        self.book_ahead_days = book_ahead_days
        self.day_opening_time = day_opening_time

    def booking_window(self, today, now):
        from fs_cli.config import _booking_window
        return _booking_window(today, now, self.book_ahead_days,
                               self.day_opening_time)

    def classify_booking_date(self, day, today, now):
        from fs_cli.config import _classify_booking_date
        return _classify_booking_date(day, today, now, self.book_ahead_days,
                                      self.day_opening_time)


class Args:
    def __init__(self, args=(), json=False, date=(),
                 desk=(), group=(), name=(), all=False, no_nav=False,
                 yes=False):
        self.args = list(args)
        self.json = json
        self.date = list(date)
        self.desk = list(desk)
        self.group = list(group)
        self.name = list(name)
        self.all = all
        self.no_nav = no_nav
        self.yes = yes


class Ctx:
    def __init__(self, out, config, api, catalog, args):
        self.out, self.config, self.api, self.catalog, self.args = \
            out, config, api, catalog, args

    def load_tags(self):
        self.out.tags = self.catalog.tag_map()


def run(catalog, api, args, today, color=False):
    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=today, color=color, stdout=stdout, stderr=stderr)
    code = cmd_map(Ctx(out, Config(), api, catalog, args))
    out.finish()
    return code, stdout.getvalue(), stderr.getvalue()


def _catalog_and_api(tmp_path):
    api = FixtureApi()
    return Catalog(api, CacheStore(tmp_path / "cache.json")), api


def test_fs_map_with_no_args_defaults_to_level5_no_bookings(tmp_path):
    catalog, api = _catalog_and_api(tmp_path)
    code, out, _err = run(catalog, api, Args(), today=dt.date(2026, 9, 1))
    assert code == ExitCode.OK
    assert "Zone A" in out            # Level 5 zone label present
    assert "▢" in out


def test_fs_map_prints_a_loading_line_before_fetching_the_floor(tmp_path):
    catalog, api = _catalog_and_api(tmp_path)
    code, out, _err = run(catalog, api, Args(), today=dt.date(2026, 9, 1))
    assert code == ExitCode.OK
    assert out.startswith("Loading floor...")


def test_fs_map_date_only_resolves_the_floor_from_bookings(tmp_path):
    # `fs map <date>` -- no floor token at all. With no covering/future
    # booking, resolve_floor falls back to Level 5 (confirmed directly
    # via resolve_floor before writing this assertion).
    catalog, api = _catalog_and_api(tmp_path)
    code, out, _err = run(catalog, api, Args(args=["mon"]),
                          today=dt.date(2026, 8, 21))
    assert code == ExitCode.OK
    assert "Zone A" in out            # Level 5 zone label present


def test_fs_map_floor_and_date_are_order_independent(tmp_path):
    # `fs map mon 5` == `fs map 5 mon` -- token classification must not
    # depend on position.
    catalog, api = _catalog_and_api(tmp_path)
    code1, out1, _err1 = run(catalog, api, Args(args=["mon", "5"]),
                             today=dt.date(2026, 8, 21))
    code2, out2, _err2 = run(catalog, api, Args(args=["5", "mon"]),
                             today=dt.date(2026, 8, 21))
    assert code1 == ExitCode.OK
    assert code2 == ExitCode.OK
    assert out1 == out2


def test_fs_map_explicit_floor_6(tmp_path):
    catalog, api = _catalog_and_api(tmp_path)
    code, out, _err = run(catalog, api, Args(args=["6"]),
                          today=dt.date(2026, 8, 21))
    assert code == ExitCode.OK
    assert "MEETING ROOMS" in out   # Level 6's room box label


def test_fs_map_rejects_json():
    with pytest.raises(UsageError):
        cmd_map(Ctx(Output(today=TODAY, json_mode=True), Config(),
                    api=None, catalog=None, args=Args(json=True)))


def test_fs_map_rejects_more_than_two_tokens(tmp_path):
    catalog, api = _catalog_and_api(tmp_path)
    with pytest.raises(UsageError):
        run(catalog, api, Args(args=["5", "mon", "extra"]),
           today=dt.date(2026, 8, 21))


def test_fs_map_rejects_two_dates(tmp_path):
    catalog, api = _catalog_and_api(tmp_path)
    with pytest.raises(UsageError):
        run(catalog, api, Args(args=["mon", "tue"]),
           today=dt.date(2026, 8, 21))


def test_fs_map_rejects_an_unrecognised_token(tmp_path):
    catalog, api = _catalog_and_api(tmp_path)
    with pytest.raises(UsageError):
        run(catalog, api, Args(args=["bogus"]), today=dt.date(2026, 8, 21))


def test_fs_map_rejects_forced_desk_flag(tmp_path):
    catalog, api = _catalog_and_api(tmp_path)
    with pytest.raises(UsageError):
        run(catalog, api, Args(desk=["L5.D.04"]), today=dt.date(2026, 8, 21))


def test_fs_map_highlights_own_desk_through_a_narrow_crop(tmp_path, monkeypatch):
    # A real captured booking covers 2026-08-24 on L5.D.236A (Level 5) --
    # verified directly via resolve_floor before writing this assertion,
    # per this repo's "assert on something that can only be true if it
    # actually worked" rule. Forcing a narrow terminal width (so `left`
    # in cmd_map's crop is > 0) exercises the `left + i` offset -- a
    # highlight at column 0 wouldn't catch an off-by-one there.
    import fs_cli.commands.map_cmd as map_cmd_module
    monkeypatch.setattr(map_cmd_module, "terminal_columns",
                        lambda stream: 20)
    catalog, api = _catalog_and_api(tmp_path)
    code, out, _err = run(catalog, api, Args(),
                          today=dt.date(2026, 8, 24))
    assert code == ExitCode.OK
    assert "▣" in out


def test_fs_map_crop_uses_full_available_width_around_a_left_side_highlight(
        tmp_path, monkeypatch):
    # Regression test for the crop_window sliver bug: a real captured
    # booking on L5.D.236A (2026-08-24) lands at grid column 28 on Level
    # 5's 166-column grid -- near the left edge. With a wide terminal
    # available, crop_window must use as much of it as the floor has
    # content for, not collapse to a ~29-column sliver hugging the
    # highlight.
    import fs_cli.commands.map_cmd as map_cmd_module
    monkeypatch.setattr(map_cmd_module, "terminal_columns",
                        lambda stream: 120)
    catalog, api = _catalog_and_api(tmp_path)
    code, out, _err = run(catalog, api, Args(), today=dt.date(2026, 8, 24))
    assert code == ExitCode.OK
    lines = out.splitlines()
    widest_line = max(len(line) for line in lines)
    assert widest_line > 60   # not collapsed to a ~29-col sliver


def _map_and_legend(out):
    """Split `cmd_map`'s stdout into (map body, legend line) -- the
    legend is `cmd_map`'s own final `out.print` call, so it's always
    the last line."""
    lines = out.rstrip("\n").split("\n")
    return "\n".join(lines[:-1]), lines[-1]


def test_cmd_map_prints_a_legend_line(tmp_path):
    catalog, api = _catalog_and_api(tmp_path)
    code, out, _err = run(catalog, api, Args(), today=dt.date(2026, 9, 1))
    assert code == ExitCode.OK
    _map_body, legend = _map_and_legend(out)
    assert "▢" in legend and "free" in legend
    assert "■" in legend and "unavailable" in legend
    assert "▣" in legend and "yours" in legend


def test_cmd_map_glyph_counts_match_live_availability(tmp_path):
    # Level 5's captured fixture: 7 free-bookable, 8 restricted, 247
    # booked desks (confirmed directly against the fixture body before
    # writing this assertion, per this repo's verification-discipline
    # rule) -- no covering booking on this date, so no "yours" override.
    catalog, api = _catalog_and_api(tmp_path)
    code, out, _err = run(catalog, api, Args(), today=dt.date(2026, 9, 1))
    assert code == ExitCode.OK
    map_body, _legend = _map_and_legend(out)
    assert map_body.count("▢") == 7
    assert map_body.count("■") == 255
    assert map_body.count("▣") == 0


def test_cmd_map_own_desk_overrides_its_booked_state_glyph(tmp_path):
    # L5.D.236A is genuinely reserved=True in the fixture (it's the
    # user's own real captured booking, confirmed directly against the
    # fixture body) -- without the "yours" override it would render
    # "unavailable" like any other booked desk. Confirms highlight
    # precedence beats live state end-to-end through cmd_map, not just
    # in desk_glyph_kind's own unit tests.
    catalog, api = _catalog_and_api(tmp_path)
    code, out, _err = run(catalog, api, Args(), today=dt.date(2026, 8, 24))
    assert code == ExitCode.OK
    map_body, _legend = _map_and_legend(out)
    assert map_body.count("▣") == 1
    assert map_body.count("▢") == 7
    assert map_body.count("■") == 254   # 255 minus the now-"yours" desk


def test_cmd_map_colours_desks_by_live_status_when_color_is_on(tmp_path):
    # Same fixture/date as test_cmd_map_own_desk_overrides_its_booked_state_
    # glyph -- L5.D.236A is the user's own desk (2026-08-24) -- but built
    # with `Output(color=True)` directly (bypassing `run()`'s default
    # `color=False`) so `cmd_map`'s `STYLE_METHOD_BY_KIND` lookup actually
    # runs through `getattr(out, ...)` on a colour-emitting `Output`, which
    # no other test in this module exercises. Asserts the raw ANSI codes
    # from `render.py`'s `_ANSI`/`THEME` dicts: green (`good`, free), dim
    # (`muted`, restricted), red (`danger`, booked) and yellow
    # (`attention`, yours) all appear, and dim/red are distinct codes so
    # restricted-vs-booked colouring is actually exercised, not just their
    # shared "■" glyph.
    catalog, api = _catalog_and_api(tmp_path)
    code, out, _err = run(catalog, api, Args(), today=dt.date(2026, 8, 24),
                          color=True)
    assert code == ExitCode.OK
    assert "\x1b[32m" in out   # green -- good -- free desks
    assert "\x1b[2m" in out    # dim -- muted -- restricted desks
    assert "\x1b[31m" in out   # red -- danger -- booked desks
    assert "\x1b[33m" in out   # yellow -- attention -- yours (L5.D.236A)


def test_plan_for_cursor_free_with_no_current_booking_is_create():
    assert plan_for_cursor("free", "L5.D.05", None) == ("create", None)


def test_plan_for_cursor_free_with_a_current_booking_elsewhere_is_replace():
    current = {"bkid": "123", "key": "L5.D.99"}
    assert plan_for_cursor("free", "L5.D.05", current) == ("replace", "123")


def test_plan_for_cursor_yours_is_release():
    current = {"bkid": "123", "key": "L5.D.05"}
    assert plan_for_cursor("yours", "L5.D.05", current) == ("release", "123")


@pytest.mark.parametrize("kind", ["restricted", "booked"])
def test_plan_for_cursor_restricted_or_booked_is_a_no_op(kind):
    assert plan_for_cursor(kind, "L5.D.05", None) is None


class _NonTtyStdin:
    def isatty(self):
        return False


def test_fs_map_falls_back_to_static_when_stdin_is_not_capable(tmp_path):
    catalog, api = _catalog_and_api(tmp_path)
    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=dt.date(2026, 9, 1), stdout=stdout, stderr=stderr)
    code = cmd_map(Ctx(out, Config(), api, catalog, Args()),
                   stdin=_NonTtyStdin())
    out.finish()
    assert code == ExitCode.OK
    # Byte-identical to the plain `run()` helper's output -- same call,
    # same fixtures, same today -- proving the branch, not just that it
    # didn't crash.
    code2, out2, _err2 = run(catalog, api, Args(), today=dt.date(2026, 9, 1))
    assert stdout.getvalue() == out2


def test_fs_map_falls_back_to_static_when_terminal_columns_is_none(
        tmp_path, monkeypatch):
    import fs_cli.commands.map_cmd as map_cmd_module
    monkeypatch.setattr(map_cmd_module, "terminal_columns", lambda stream: None)
    catalog, api = _catalog_and_api(tmp_path)
    code, out, _err = run(catalog, api, Args(), today=dt.date(2026, 9, 1))
    assert code == ExitCode.OK
    assert "Zone A" in out   # still rendered -- static path, not an error


def test_fs_map_no_nav_forces_the_static_path_even_over_a_capable_stdin(
        tmp_path, monkeypatch):
    # A stdin that WOULD be capable (real fd, isatty True) is never
    # actually reached -- `--no-nav` must short-circuit before
    # `keyread.capable` is even called, so a fake with no `fileno()` at
    # all is enough to prove that.
    class _WouldBeCapableIfChecked:
        def isatty(self):
            return True
    catalog, api = _catalog_and_api(tmp_path)
    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=dt.date(2026, 9, 1), stdout=stdout, stderr=stderr)
    code = cmd_map(Ctx(out, Config(), api, catalog,
                       Args(no_nav=True)),
                   stdin=_WouldBeCapableIfChecked())
    out.finish()
    assert code == ExitCode.OK
    assert "Zone A" in stdout.getvalue()


class ScriptedKeys:
    """A `read_key`-shaped callable that returns one scripted symbolic
    key per call (`"up"`, `"enter"`, `"q"`, ...), `None` once exhausted
    -- `_run_live`'s injectable `read_key` reads via `keyread`'s cbreak
    path in production, so this stands in for a real terminal the same
    way `plan.py`'s `ScriptedStdin` stands in for line-based input."""

    def __init__(self, keys):
        self._keys = list(keys)

    def __call__(self, stdin):
        return self._keys.pop(0) if self._keys else None


def test_run_live_quits_cleanly_on_q(tmp_path):
    catalog, api = _catalog_and_api(tmp_path)
    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=dt.date(2026, 9, 1), stdout=stdout, stderr=stderr)
    planid, highlight_keys = resolve_floor(api, catalog, out.today,
                                           out.today, None)
    polys = catalog.deskpolys(planid)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == planid}
    img_w, img_h = catalog.floor_image_size(planid)
    desk_states = catalog.availability(out.today, planids=[planid])
    grid, highlight_positions, desk_kind_positions, desk_positions = \
        render_floor(planid, polys, catalog_keys, img_w, img_h,
                    highlight_keys=highlight_keys, desk_states=desk_states)

    ctx = Ctx(out, Config(), api, catalog, Args())
    code = _run_live(ctx, planid, out.today, grid, highlight_positions,
                     desk_kind_positions, desk_positions, columns=80,
                     read_key=ScriptedKeys(["q"]),
                     initial_cursor_key=next(iter(desk_positions)))
    assert code == ExitCode.OK


def test_run_live_moves_the_cursor_with_an_arrow_key(tmp_path):
    catalog, api = _catalog_and_api(tmp_path)
    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=dt.date(2026, 9, 1), stdout=stdout, stderr=stderr)
    planid, highlight_keys = resolve_floor(api, catalog, out.today,
                                           out.today, None)
    polys = catalog.deskpolys(planid)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == planid}
    img_w, img_h = catalog.floor_image_size(planid)
    desk_states = catalog.availability(out.today, planids=[planid])
    grid, highlight_positions, desk_kind_positions, desk_positions = \
        render_floor(planid, polys, catalog_keys, img_w, img_h,
                    highlight_keys=highlight_keys, desk_states=desk_states)

    start_key = next(iter(desk_positions))
    target_key = nearest_desk(desk_positions[start_key], desk_positions, "right")
    assert target_key is not None   # test fixture must have a real neighbour

    ctx = Ctx(out, Config(), api, catalog, Args())
    code = _run_live(ctx, planid, out.today, grid, highlight_positions,
                     desk_kind_positions, desk_positions, columns=200,
                     read_key=ScriptedKeys(["right", "q"]),
                     initial_cursor_key=start_key)
    assert code == ExitCode.OK
    # The final status line names the desk the cursor moved TO, not the
    # one it started on.
    assert out.fmt_desk(target_key) in stdout.getvalue()
    assert out.fmt_desk(target_key) != out.fmt_desk(start_key)  # sanity


def test_run_live_marks_the_cursor_cell_with_reverse_video(tmp_path):
    # The cursor was previously invisible -- nothing distinguished it from
    # any other desk of the same kind. Reverse video on just the cursor's
    # own cell, layered over its existing kind colour.
    catalog, api = _catalog_and_api(tmp_path)
    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=dt.date(2026, 9, 1), stdout=stdout, stderr=stderr,
                color=True)
    planid, highlight_keys = resolve_floor(api, catalog, out.today,
                                           out.today, None)
    polys = catalog.deskpolys(planid)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == planid}
    img_w, img_h = catalog.floor_image_size(planid)
    desk_states = catalog.availability(out.today, planids=[planid])
    grid, highlight_positions, desk_kind_positions, desk_positions = \
        render_floor(planid, polys, catalog_keys, img_w, img_h,
                    highlight_keys=highlight_keys, desk_states=desk_states)

    ctx = Ctx(out, Config(), api, catalog, Args())
    code = _run_live(ctx, planid, out.today, grid, highlight_positions,
                     desk_kind_positions, desk_positions, columns=200,
                     read_key=ScriptedKeys(["q"]),
                     initial_cursor_key=next(iter(desk_positions)))
    assert code == ExitCode.OK
    assert "\x1b[7m" in stdout.getvalue()


def test_run_live_enters_and_leaves_the_alternate_screen_without_a_full_clear(
        tmp_path):
    # `\x1b[2J` (full clear) every keypress is what flickered -- the fix
    # is the alternate screen buffer entered once and left once, with
    # per-frame redraws never blanking the whole screen.
    catalog, api = _catalog_and_api(tmp_path)
    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=dt.date(2026, 9, 1), stdout=stdout, stderr=stderr)
    planid, highlight_keys = resolve_floor(api, catalog, out.today,
                                           out.today, None)
    polys = catalog.deskpolys(planid)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == planid}
    img_w, img_h = catalog.floor_image_size(planid)
    desk_states = catalog.availability(out.today, planids=[planid])
    grid, highlight_positions, desk_kind_positions, desk_positions = \
        render_floor(planid, polys, catalog_keys, img_w, img_h,
                    highlight_keys=highlight_keys, desk_states=desk_states)

    ctx = Ctx(out, Config(), api, catalog, Args())
    code = _run_live(ctx, planid, out.today, grid, highlight_positions,
                     desk_kind_positions, desk_positions, columns=200,
                     read_key=ScriptedKeys(["right", "q"]),
                     initial_cursor_key=next(iter(desk_positions)))
    assert code == ExitCode.OK
    output = stdout.getvalue()
    assert "\x1b[?1049h" in output    # entered the alt screen once
    assert "\x1b[?1049l" in output    # left it once, on quit
    assert "\x1b[2J" not in output    # never a full-screen clear


def test_run_live_ends_on_eof_without_raising(tmp_path):
    catalog, api = _catalog_and_api(tmp_path)
    out = Output(today=dt.date(2026, 9, 1), stdout=io.StringIO(),
                 stderr=io.StringIO())
    planid, highlight_keys = resolve_floor(api, catalog, out.today,
                                           out.today, None)
    polys = catalog.deskpolys(planid)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == planid}
    img_w, img_h = catalog.floor_image_size(planid)
    desk_states = catalog.availability(out.today, planids=[planid])
    grid, highlight_positions, desk_kind_positions, desk_positions = \
        render_floor(planid, polys, catalog_keys, img_w, img_h,
                    highlight_keys=highlight_keys, desk_states=desk_states)

    ctx = Ctx(out, Config(), api, catalog, Args())
    code = _run_live(ctx, planid, out.today, grid, highlight_positions,
                     desk_kind_positions, desk_positions, columns=80,
                     read_key=ScriptedKeys([]),   # empty -> None on first call
                     initial_cursor_key=next(iter(desk_positions)))
    assert code == ExitCode.OK


def _live_ctx_for_write_tests(tmp_path, yes=False, now=None,
                              config=None):
    api = LiveWriteApi()
    catalog = Catalog(api, CacheStore(tmp_path / "cache.json"))
    today = dt.date(2026, 9, 1)
    out = Output(today=today, now=now, stdout=io.StringIO(),
                stderr=io.StringIO())
    planid = 3   # PLANID_LEVEL5
    highlight_keys = set()
    polys = catalog.deskpolys(planid)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == planid}
    img_w, img_h = catalog.floor_image_size(planid)
    desk_states = catalog.availability(today, planids=[planid])
    grid, highlight_positions, desk_kind_positions, desk_positions = \
        render_floor(planid, polys, catalog_keys, img_w, img_h,
                    highlight_keys=highlight_keys, desk_states=desk_states)
    ctx = Ctx(out, config or Config(), api, catalog,
             Args(yes=yes))
    return ctx, api, planid, today, grid, highlight_positions, \
        desk_kind_positions, desk_positions


def test_run_live_books_a_free_desk_through_the_confirm_gesture(tmp_path):
    ctx, api, planid, today, grid, hp, kp, dp = _live_ctx_for_write_tests(tmp_path)
    free_desk = "L5.D.226"

    code = _run_live(ctx, planid, today, grid, hp, kp, dp, columns=200,
                     read_key=ScriptedKeys(["enter", "enter", "q"]),
                     initial_cursor_key=free_desk)

    assert code == ExitCode.OK
    assert len(api._own) == 1
    row = next(iter(api._own.values()))
    assert row["key"] == free_desk


def test_run_live_cancelling_a_confirm_makes_no_write(tmp_path):
    ctx, api, planid, today, grid, hp, kp, dp = _live_ctx_for_write_tests(tmp_path)
    free_desk = "L5.D.226"

    code = _run_live(ctx, planid, today, grid, hp, kp, dp, columns=200,
                     read_key=ScriptedKeys(["enter", "esc", "q"]),
                     initial_cursor_key=free_desk)

    assert code == ExitCode.OK
    assert api._own == {}


def test_run_live_book_then_refresh_then_release_then_quit(tmp_path):
    ctx, api, planid, today, grid, hp, kp, dp = _live_ctx_for_write_tests(tmp_path)
    free_desk = "L5.D.226"

    # enter,enter -> confirm and book. enter,enter -> confirm and
    # release (the cursor stays on the same desk key, now "yours" after
    # the post-write refresh). q -> quit.
    code = _run_live(ctx, planid, today, grid, hp, kp, dp, columns=200,
                     read_key=ScriptedKeys(
                         ["enter", "enter", "enter", "enter", "q"]),
                     initial_cursor_key=free_desk)

    assert code == ExitCode.OK
    assert api._own == {}   # booked, then released -- back to empty


def test_run_live_yes_skips_the_confirm_line(tmp_path):
    ctx, api, planid, today, grid, hp, kp, dp = _live_ctx_for_write_tests(
        tmp_path, yes=True)
    free_desk = "L5.D.226"

    # A single "enter" is the write itself under --yes -- no second
    # confirming keypress needed.
    code = _run_live(ctx, planid, today, grid, hp, kp, dp, columns=200,
                     read_key=ScriptedKeys(["enter", "q"]),
                     initial_cursor_key=free_desk)

    assert code == ExitCode.OK
    assert len(api._own) == 1



def test_run_live_booking_today_after_the_opening_time_is_blocked(tmp_path):
    today = dt.date(2026, 9, 1)
    now = dt.datetime.combine(today, dt.time(9, 0))
    ctx, api, planid, _today, grid, hp, kp, dp = _live_ctx_for_write_tests(
        tmp_path, now=now)
    free_desk = "L5.D.226"

    code = _run_live(ctx, planid, today, grid, hp, kp, dp, columns=200,
                     read_key=ScriptedKeys(["enter", "enter", "q"]),
                     initial_cursor_key=free_desk)

    assert code == ExitCode.OK
    assert api._own == {}
    assert ("cannot book for today after the day opening time"
           in ctx.out._stdout.getvalue())


def test_run_live_booking_today_before_the_opening_time_still_books(
        tmp_path):
    today = dt.date(2026, 9, 1)
    now = dt.datetime.combine(today, dt.time(8, 0))
    ctx, api, planid, _today, grid, hp, kp, dp = _live_ctx_for_write_tests(
        tmp_path, now=now)
    free_desk = "L5.D.226"

    code = _run_live(ctx, planid, today, grid, hp, kp, dp, columns=200,
                     read_key=ScriptedKeys(["enter", "enter", "q"]),
                     initial_cursor_key=free_desk)

    assert code == ExitCode.OK
    assert len(api._own) == 1


def test_run_live_booking_a_date_beyond_the_advance_window_is_blocked(
        tmp_path):
    ctx, api, planid, today, grid, hp, kp, dp = _live_ctx_for_write_tests(
        tmp_path, config=Config(book_ahead_days=10))
    free_desk = "L5.D.226"
    too_far = today + dt.timedelta(days=30)

    code = _run_live(ctx, planid, too_far, grid, hp, kp, dp, columns=200,
                     read_key=ScriptedKeys(["enter", "enter", "q"]),
                     initial_cursor_key=free_desk)

    assert code == ExitCode.OK
    assert api._own == {}
    assert ("advance bookings cannot be made this far in the future"
           in ctx.out._stdout.getvalue())


def test_run_live_restricted_or_booked_desk_ignores_enter(tmp_path):
    ctx, api, planid, today, grid, hp, kp, dp = _live_ctx_for_write_tests(tmp_path)
    # A desk that's neither free nor yours in the fixture -- pick any
    # position whose kind is "booked" or "restricted".
    busy_key = next(key for key, pos in dp.items()
                    if kp.get(pos) in ("booked", "restricted"))

    code = _run_live(ctx, planid, today, grid, hp, kp, dp, columns=200,
                     read_key=ScriptedKeys(["enter", "q"]),
                     initial_cursor_key=busy_key)

    assert code == ExitCode.OK
    assert api._own == {}   # Enter was a no-op -- no confirm line, no write


def test_run_live_shows_a_booked_desks_occupant_once_the_background_fetch_lands(
        tmp_path):
    # Feedback: the status line should say who has a booked desk, without
    # the arrow-key loop blocking on the `/user` lookup. `spawn` is
    # injected synchronously here (same pattern as `read_key`) so the
    # fetch's result is deterministically visible in the one frame this
    # test scripts, rather than depending on real thread timing.
    class _NamedApi(LiveWriteApi):
        def user(self, uid, bkid=None):
            return {"name": "A Stranger"}

    api = _NamedApi()
    catalog = Catalog(api, CacheStore(tmp_path / "cache.json"))
    today = dt.date(2026, 9, 1)
    out = Output(today=today, stdout=io.StringIO(), stderr=io.StringIO())
    planid = PLANID_LEVEL5
    polys = catalog.deskpolys(planid)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == planid}
    img_w, img_h = catalog.floor_image_size(planid)
    desk_states = catalog.availability(today, planids=[planid])
    grid, highlight_positions, desk_kind_positions, desk_positions = \
        render_floor(planid, polys, catalog_keys, img_w, img_h,
                    highlight_keys=set(), desk_states=desk_states)
    busy_key = next(key for key, pos in desk_positions.items()
                    if desk_kind_positions.get(pos) == "booked"
                    and desk_states[key].uid)

    def spawn(target, args):
        target(*args)

    ctx = Ctx(out, Config(), api, catalog, Args())
    code = _run_live(ctx, planid, today, grid, highlight_positions,
                     desk_kind_positions, desk_positions, columns=200,
                     read_key=ScriptedKeys(["q"]),
                     initial_cursor_key=busy_key,
                     desk_states=desk_states, spawn=spawn)

    assert code == ExitCode.OK
    assert "A Stranger" in out._stdout.getvalue()


def test_prefetch_occupants_also_fetches_the_four_neighbours():
    # The whole point of prefetching: by the time an arrow key actually
    # lands on a booked neighbour, its lookup should already be in
    # flight (or done) rather than starting fresh on arrival -- see the
    # advisor-flagged gap where a fetch that only starts on arrival is
    # never visible during ordinary arrow-key browsing (the redraw loop
    # blocks on the next keypress before a background result can ever be
    # shown).
    from fs_cli.commands.map_cmd import _prefetch_occupants

    class _State:
        def __init__(self, uid, bkid):
            self.uid, self.bkid = uid, bkid

    desk_positions = {"A": (0, 0), "B": (0, 10), "C": (0, -10),
                      "D": (10, 0), "E": (-10, 0)}
    desk_kind_positions = {(0, 0): "free", (0, 10): "booked",
                           (0, -10): "booked", (10, 0): "restricted",
                           (-10, 0): "yours"}
    desk_states = {"B": _State("u-b", "bk-b"), "C": _State("u-c", "bk-c")}
    scheduled = []
    names, pending = {}, set()

    _prefetch_occupants(api=None, own_uid=None, desk_states=desk_states,
                        desk_kind_positions=desk_kind_positions,
                        desk_positions=desk_positions, cursor_key="A",
                        cursor_pos=(0, 0), names=names, pending=pending,
                        spawn=lambda target, args: scheduled.append(target))

    # Only the two booked neighbours (B, C) have anything to fetch --
    # "A" itself is free (no uid), "D" is restricted and "E" is yours,
    # neither of which carries an occupant to look up.
    assert pending == {"u-b", "u-c"}
    assert len(scheduled) == 2


def test_prefetch_occupants_does_not_duplicate_an_in_flight_fetch():
    from fs_cli.commands.map_cmd import _prefetch_occupants

    class _State:
        def __init__(self, uid, bkid):
            self.uid, self.bkid = uid, bkid

    desk_positions = {"A": (0, 0), "B": (0, 10)}
    desk_kind_positions = {(0, 0): "free", (0, 10): "booked"}
    desk_states = {"B": _State("u-b", "bk-b")}
    scheduled = []
    names, pending = {}, {"u-b"}   # already in flight from a prior frame

    _prefetch_occupants(api=None, own_uid=None, desk_states=desk_states,
                        desk_kind_positions=desk_kind_positions,
                        desk_positions=desk_positions, cursor_key="A",
                        cursor_pos=(0, 0), names=names, pending=pending,
                        spawn=lambda target, args: scheduled.append(target))

    assert scheduled == []


class _FakeSession:
    def __init__(self):
        self.config = "cfg"
        self.store = "store"
        self.origin = "https://example.test"


def test_occupant_api_never_reuses_the_main_session_that_can_relogin():
    # A background thread hitting a dead session must not be able to
    # trigger `Session._fresh_login`'s interactive password/MFA prompt --
    # the main thread owns the tty in cbreak mode at that point. The
    # background api gets its OWN `Session`, sharing the same on-disk
    # cookie store (so it doesn't need to log in again itself) but built
    # with `allow_login=False`, so a dead session raises instead of
    # prompting.
    import types

    from fs_cli.commands.map_cmd import _occupant_api

    ctx = types.SimpleNamespace(session=_FakeSession(), api="main-api")
    bg_api = _occupant_api(ctx)
    assert bg_api is not ctx.api
    assert bg_api.session.allow_login is False
    assert bg_api.session.config == "cfg"
    assert bg_api.session.store == "store"


def test_occupant_api_falls_back_to_ctx_api_with_no_real_session(tmp_path):
    # Every test double `Ctx` in this module has no `.session` at all --
    # falls back unchanged, since none of them carry the real
    # background-relogin risk this guards against.
    from fs_cli.commands.map_cmd import _occupant_api

    catalog, api = _catalog_and_api(tmp_path)
    out = Output(today=dt.date(2026, 9, 1), stdout=io.StringIO(),
                stderr=io.StringIO())
    ctx = Ctx(out, Config(), api, catalog, Args())
    assert _occupant_api(ctx) is api


def test_run_live_reprints_the_last_frame_after_leaving_the_alternate_screen(
        tmp_path):
    # Leaving the alt screen (`\x1b[?1049l`) restores the primary buffer,
    # which would otherwise wipe the map off screen on quit -- the old
    # `2J`-only redraw left it sitting in the scrollback. Reprint the
    # final frame once, plainly, after leaving, so quitting doesn't
    # change what the user sees.
    catalog, api = _catalog_and_api(tmp_path)
    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=dt.date(2026, 9, 1), stdout=stdout, stderr=stderr)
    planid, highlight_keys = resolve_floor(api, catalog, out.today,
                                           out.today, None)
    polys = catalog.deskpolys(planid)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == planid}
    img_w, img_h = catalog.floor_image_size(planid)
    desk_states = catalog.availability(out.today, planids=[planid])
    grid, highlight_positions, desk_kind_positions, desk_positions = \
        render_floor(planid, polys, catalog_keys, img_w, img_h,
                    highlight_keys=highlight_keys, desk_states=desk_states)
    start_key = next(iter(desk_positions))

    ctx = Ctx(out, Config(), api, catalog, Args())
    code = _run_live(ctx, planid, out.today, grid, highlight_positions,
                     desk_kind_positions, desk_positions, columns=200,
                     read_key=ScriptedKeys(["q"]),
                     initial_cursor_key=start_key)
    assert code == ExitCode.OK
    output = stdout.getvalue()
    after_leaving = output.rsplit("\x1b[?1049l", 1)[1]
    assert out.fmt_desk(start_key) in after_leaving


def test_run_live_without_desk_states_still_shows_plain_booked(tmp_path):
    # `desk_states=None` (every existing call site until `cmd_map` is
    # updated) must keep behaving exactly as before -- no crash, no
    # occupant lookup attempted.
    ctx, api, planid, today, grid, hp, kp, dp = _live_ctx_for_write_tests(tmp_path)
    busy_key = next(key for key, pos in dp.items() if kp.get(pos) == "booked")

    code = _run_live(ctx, planid, today, grid, hp, kp, dp, columns=200,
                     read_key=ScriptedKeys(["q"]),
                     initial_cursor_key=busy_key)

    assert code == ExitCode.OK
    assert "unavailable" in ctx.out._stdout.getvalue()


def test_header_line_names_the_floor_and_date():
    out = Output(today=TODAY, stdout=io.StringIO(), stderr=io.StringIO())
    line = header_line(out, PLANID_LEVEL5, TODAY, TEST_FLOOR_NAMES)
    assert line == f"Level 5 -- {out.fmt_date(TODAY)}"


def test_header_line_level6():
    out = Output(today=TODAY, stdout=io.StringIO(), stderr=io.StringIO())
    header = header_line(out, PLANID_LEVEL6, TODAY, TEST_FLOOR_NAMES)
    assert header.startswith("Level 6")


def test_header_line_falls_back_to_the_bare_planid_if_unnamed():
    out = Output(today=TODAY, stdout=io.StringIO(), stderr=io.StringIO())
    assert header_line(out, 999, TODAY, TEST_FLOOR_NAMES).startswith("planid 999")


def test_desk_at_click_maps_a_terminal_click_to_the_desk_under_it():
    # header_lines=1 (the default), left=5: a click at terminal (col=8,
    # row=3) lands on grid (row 1, col 8-1+5=12).
    pos_to_key = {(1, 12): "L5.D.05"}
    assert desk_at_click(8, 3, left=5, pos_to_key=pos_to_key) == "L5.D.05"


def test_desk_at_click_returns_none_off_the_grid():
    pos_to_key = {(1, 12): "L5.D.05"}
    assert desk_at_click(8, 3, left=5, pos_to_key=pos_to_key,
                         header_lines=0) is None   # now lands on (2, 12)


def test_desk_at_click_returns_none_on_empty_space():
    assert desk_at_click(1, 2, left=0, pos_to_key={}) is None


def test_run_live_shows_the_header_line(tmp_path):
    catalog, api = _catalog_and_api(tmp_path)
    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=dt.date(2026, 9, 1), stdout=stdout, stderr=stderr)
    planid, highlight_keys = resolve_floor(api, catalog, out.today,
                                           out.today, None)
    polys = catalog.deskpolys(planid)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == planid}
    img_w, img_h = catalog.floor_image_size(planid)
    desk_states = catalog.availability(out.today, planids=[planid])
    grid, highlight_positions, desk_kind_positions, desk_positions = \
        render_floor(planid, polys, catalog_keys, img_w, img_h,
                    highlight_keys=highlight_keys, desk_states=desk_states)

    ctx = Ctx(out, Config(), api, catalog, Args())
    code = _run_live(ctx, planid, out.today, grid, highlight_positions,
                     desk_kind_positions, desk_positions, columns=200,
                     read_key=ScriptedKeys(["q"]),
                     initial_cursor_key=next(iter(desk_positions)))
    assert code == ExitCode.OK
    header = header_line(out, planid, out.today, floor_names(catalog))
    assert header in stdout.getvalue()


def test_run_live_digit_key_switches_to_an_existing_different_floor(tmp_path):
    catalog, api = _catalog_and_api(tmp_path)
    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=dt.date(2026, 9, 1), stdout=stdout, stderr=stderr)
    planid, highlight_keys = resolve_floor(api, catalog, out.today,
                                           out.today, PLANID_LEVEL5)
    polys = catalog.deskpolys(planid)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == planid}
    img_w, img_h = catalog.floor_image_size(planid)
    desk_states = catalog.availability(out.today, planids=[planid])
    grid, highlight_positions, desk_kind_positions, desk_positions = \
        render_floor(planid, polys, catalog_keys, img_w, img_h,
                    highlight_keys=highlight_keys, desk_states=desk_states)

    ctx = Ctx(out, Config(), api, catalog, Args())
    code = _run_live(ctx, planid, out.today, grid, highlight_positions,
                     desk_kind_positions, desk_positions, columns=200,
                     read_key=ScriptedKeys(["6", "q"]),
                     initial_cursor_key=next(iter(desk_positions)))
    assert code == ExitCode.OK
    output = stdout.getvalue()
    assert "Loading floor..." in output
    assert header_line(out, PLANID_LEVEL6, out.today, floor_names(catalog)) in output


def test_run_live_digit_key_for_a_nonexistent_floor_is_a_no_op(tmp_path):
    catalog, api = _catalog_and_api(tmp_path)
    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=dt.date(2026, 9, 1), stdout=stdout, stderr=stderr)
    planid, highlight_keys = resolve_floor(api, catalog, out.today,
                                           out.today, PLANID_LEVEL5)
    polys = catalog.deskpolys(planid)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == planid}
    img_w, img_h = catalog.floor_image_size(planid)
    desk_states = catalog.availability(out.today, planids=[planid])
    grid, highlight_positions, desk_kind_positions, desk_positions = \
        render_floor(planid, polys, catalog_keys, img_w, img_h,
                    highlight_keys=highlight_keys, desk_states=desk_states)

    ctx = Ctx(out, Config(), api, catalog, Args())
    code = _run_live(ctx, planid, out.today, grid, highlight_positions,
                     desk_kind_positions, desk_positions, columns=200,
                     read_key=ScriptedKeys(["3", "q"]),
                     initial_cursor_key=next(iter(desk_positions)))
    assert code == ExitCode.OK
    header = header_line(out, PLANID_LEVEL5, out.today, floor_names(catalog))
    assert header in stdout.getvalue()


def test_run_live_pgdn_advances_the_date_and_shows_a_loading_line(tmp_path):
    catalog, api = _catalog_and_api(tmp_path)
    stdout, stderr = io.StringIO(), io.StringIO()
    today = dt.date(2026, 9, 1)
    out = Output(today=today, stdout=stdout, stderr=stderr)
    planid, highlight_keys = resolve_floor(api, catalog, out.today,
                                           out.today, PLANID_LEVEL5)
    polys = catalog.deskpolys(planid)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == planid}
    img_w, img_h = catalog.floor_image_size(planid)
    desk_states = catalog.availability(out.today, planids=[planid])
    grid, highlight_positions, desk_kind_positions, desk_positions = \
        render_floor(planid, polys, catalog_keys, img_w, img_h,
                    highlight_keys=highlight_keys, desk_states=desk_states)

    ctx = Ctx(out, Config(), api, catalog, Args())
    code = _run_live(ctx, planid, today, grid, highlight_positions,
                     desk_kind_positions, desk_positions, columns=200,
                     read_key=ScriptedKeys(["pgdn", "q"]),
                     initial_cursor_key=next(iter(desk_positions)))
    assert code == ExitCode.OK
    output = stdout.getvalue()
    tomorrow = today + dt.timedelta(days=1)
    assert f"Loading {out.fmt_date(tomorrow)}" in output
    assert header_line(out, planid, tomorrow, floor_names(catalog)) in output


def test_render_lines_bolds_a_yours_desk_even_off_cursor():
    out = Output(today=dt.date(2026, 9, 1), color=True,
                 stdout=io.StringIO(), stderr=io.StringIO())
    grid = ["A"]
    desk_kind_positions = {(0, 0): "yours"}
    lines = _render_lines(out, grid, desk_kind_positions, 0, 1)
    assert "\x1b[1m" in lines[0]   # the bold SGR code


def test_render_lines_bolds_the_cursor_cell_too():
    out = Output(today=dt.date(2026, 9, 1), color=True,
                 stdout=io.StringIO(), stderr=io.StringIO())
    grid = ["A"]
    desk_kind_positions = {(0, 0): "free"}
    lines = _render_lines(out, grid, desk_kind_positions, 0, 1,
                          cursor_pos=(0, 0))
    assert "\x1b[1m" in lines[0]


def test_initial_cursor_key_prefers_a_yours_desk_over_the_group_fallback():
    desk_positions = {"A": (0, 1), "B": (0, 3)}
    desk_kind_positions = {(0, 1): "free", (0, 3): "yours"}
    key = _initial_cursor_key(["........"], desk_positions,
                              desk_kind_positions, set(), None,
                              group_keys=["A"])
    assert key == "B"


def test_initial_cursor_key_falls_back_to_first_free_desk_in_the_group():
    """No booking for the day -- land on the first FREE desk in the
    configured default group's own preference order, not just the first
    group member regardless of whether it's actually bookable."""
    desk_positions = {"A": (0, 1), "B": (0, 3), "C": (0, 5)}
    desk_kind_positions = {(0, 1): "booked", (0, 3): "free", (0, 5): "free"}
    key = _initial_cursor_key(["........"], desk_positions,
                              desk_kind_positions, set(), None,
                              group_keys=["A", "B", "C"])
    assert key == "B"


def test_initial_cursor_key_falls_back_to_top_leftmost_when_group_has_none_free():
    desk_positions = {"A": (0, 5), "B": (0, 1)}
    desk_kind_positions = {(0, 5): "booked", (0, 1): "booked"}
    key = _initial_cursor_key(["........"], desk_positions,
                              desk_kind_positions, set(), None,
                              group_keys=["A"])
    assert key == "B"


def test_run_live_scrolls_vertically_to_keep_the_cursor_visible(tmp_path):
    """Regression: with no vertical crop at all, a floor taller than the
    terminal just overflowed -- the terminal's OWN native scroll kept up,
    carrying the header line off the top, and the cursor (drawn at
    whatever ABSOLUTE row it's actually on) only came back into view once
    the terminal was scrolled back to the top by hand. A short `rows`
    here must produce a frame with fewer printed lines than the full grid
    height, while still containing the cursor's reverse-video code."""
    catalog, api = _catalog_and_api(tmp_path)
    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=dt.date(2026, 9, 1), color=True,
                stdout=stdout, stderr=stderr)
    planid, highlight_keys = resolve_floor(api, catalog, out.today,
                                           out.today, PLANID_LEVEL5)
    polys = catalog.deskpolys(planid)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == planid}
    img_w, img_h = catalog.floor_image_size(planid)
    desk_states = catalog.availability(out.today, planids=[planid])
    grid, highlight_positions, desk_kind_positions, desk_positions = \
        render_floor(planid, polys, catalog_keys, img_w, img_h,
                    highlight_keys=highlight_keys, desk_states=desk_states)
    assert len(grid) > 15   # the fixture's floor must actually be taller
                            # than the short terminal this test simulates

    # A desk near the BOTTOM of the grid -- the case a fixed, un-refreshed
    # crop would have gotten wrong (only ever showing the top of the map).
    bottom_key = max(desk_positions, key=lambda k: desk_positions[k][0])

    ctx = Ctx(out, Config(), api, catalog, Args())
    code = _run_live(ctx, planid, out.today, grid, highlight_positions,
                     desk_kind_positions, desk_positions, columns=200,
                     rows=10, read_key=ScriptedKeys(["q"]),
                     initial_cursor_key=bottom_key)
    assert code == ExitCode.OK
    # The exit sequence has no `\x1b[H` before it, so it would otherwise run
    # straight into the last interactive frame in the same split chunk --
    # peel it off first (see the resize test's identical comment).
    interactive, _exit_reprint = stdout.getvalue().split(
        "\x1b[?25h\x1b[?1000l\x1b[?1006l\x1b[?1049l")
    last_frame = interactive.split("\x1b[H")[-1]
    # Drop the trailing `\x1b[J` (erase-to-end-of-screen, on its own "line"
    # since the print before it already ended in "\n") -- not a content
    # line, just the redraw's own end-of-frame erase sequence.
    printed_lines = [ln for ln in last_frame.split("\n") if ln != "\x1b[J"]
    assert len(printed_lines) <= 10   # HEADER_LINES(1) + 8 grid rows + status(1)
    assert "\x1b[7m" in last_frame   # cursor's reverse video, still present


def test_run_live_picks_up_a_mid_session_resize(tmp_path):
    """A `term_size` that changes between frames (standing in for a real
    resize mid-session, which `cmd_map` wires to a fresh
    `shutil.get_terminal_size()` call every frame) must be reflected on
    the very next redraw -- not stuck on whatever `columns` measured on
    the way in."""
    catalog, api = _catalog_and_api(tmp_path)
    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=dt.date(2026, 9, 1), stdout=stdout, stderr=stderr)
    planid, highlight_keys = resolve_floor(api, catalog, out.today,
                                           out.today, PLANID_LEVEL5)
    polys = catalog.deskpolys(planid)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == planid}
    img_w, img_h = catalog.floor_image_size(planid)
    desk_states = catalog.availability(out.today, planids=[planid])
    grid, highlight_positions, desk_kind_positions, desk_positions = \
        render_floor(planid, polys, catalog_keys, img_w, img_h,
                    highlight_keys=highlight_keys, desk_states=desk_states)
    n_cols = len(grid[0])
    assert n_cols > 40   # the fixture's floor must actually be wide enough

    sizes = iter([(200, 100), (200, 100), (20, 100)])   # shrinks on frame 3

    ctx = Ctx(out, Config(), api, catalog, Args())
    code = _run_live(ctx, planid, out.today, grid, highlight_positions,
                     desk_kind_positions, desk_positions, columns=200,
                     rows=100, read_key=ScriptedKeys(["right", "right", "q"]),
                     initial_cursor_key=next(iter(desk_positions)),
                     term_size=lambda: next(sizes))
    assert code == ExitCode.OK
    # The exit sequence has no `\x1b[H` before it, so the LAST chunk below
    # is really "3rd redraw" + "exit reprint" run together -- split that
    # off by the known alt-screen-exit codes first, before splitting the
    # interactive frames apart on `\x1b[H`.
    interactive, _exit_reprint = stdout.getvalue().split(
        "\x1b[?25h\x1b[?1000l\x1b[?1006l\x1b[?1049l")
    frames = interactive.split("\x1b[H")[1:]
    assert len(frames) == 3
    widths = [max(len(ln) for ln in f.split("\n")) for f in frames]
    # The frame drawn once the terminal has "shrunk" must be narrower than
    # the ones drawn while it was still wide -- proof the loop re-queried
    # `term_size` rather than reusing the `columns` it started with.
    assert widths[-1] < widths[0]


def test_run_live_pgdn_keeps_the_cursor_on_the_same_desk(tmp_path):
    """Regression: a date change used to re-pick the cursor from scratch
    (`_initial_cursor_key`) every time, which felt like losing your place
    since the floor -- and so `desk_positions`' layout -- hasn't changed at
    all, only which desks are free/booked on the new date."""
    catalog, api = _catalog_and_api(tmp_path)
    stdout, stderr = io.StringIO(), io.StringIO()
    today = dt.date(2026, 9, 1)
    out = Output(today=today, stdout=stdout, stderr=stderr)
    planid, highlight_keys = resolve_floor(api, catalog, out.today,
                                           out.today, PLANID_LEVEL5)
    polys = catalog.deskpolys(planid)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == planid}
    img_w, img_h = catalog.floor_image_size(planid)
    desk_states = catalog.availability(out.today, planids=[planid])
    grid, highlight_positions, desk_kind_positions, desk_positions = \
        render_floor(planid, polys, catalog_keys, img_w, img_h,
                    highlight_keys=highlight_keys, desk_states=desk_states)

    # Deliberately NOT the desk `_initial_cursor_key` would pick on its own
    # (yours/group/top-leftmost) -- some other desk in the layout, so a
    # silent re-pick after `pgdn` would show up as a different label.
    chosen = list(desk_positions)[-1]

    ctx = Ctx(out, Config(), api, catalog, Args())
    code = _run_live(ctx, planid, today, grid, highlight_positions,
                     desk_kind_positions, desk_positions, columns=200,
                     read_key=ScriptedKeys(["pgdn", "q"]),
                     initial_cursor_key=chosen)
    assert code == ExitCode.OK
    output = stdout.getvalue()
    tomorrow = today + dt.timedelta(days=1)
    assert f"Loading {out.fmt_date(tomorrow)}" in output
    assert out.fmt_desk(chosen) in output


def test_run_live_pgup_is_clamped_at_today(tmp_path):
    catalog, api = _catalog_and_api(tmp_path)
    stdout, stderr = io.StringIO(), io.StringIO()
    today = dt.date(2026, 9, 1)
    out = Output(today=today, stdout=stdout, stderr=stderr)
    planid, highlight_keys = resolve_floor(api, catalog, out.today,
                                           out.today, PLANID_LEVEL5)
    polys = catalog.deskpolys(planid)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == planid}
    img_w, img_h = catalog.floor_image_size(planid)
    desk_states = catalog.availability(out.today, planids=[planid])
    grid, highlight_positions, desk_kind_positions, desk_positions = \
        render_floor(planid, polys, catalog_keys, img_w, img_h,
                    highlight_keys=highlight_keys, desk_states=desk_states)

    ctx = Ctx(out, Config(), api, catalog, Args())
    # Already showing `today` -- PgUp would go to yesterday, which the
    # clamp forbids, so this must stay on `today`'s header with no
    # "Loading" line ever printed.
    code = _run_live(ctx, planid, today, grid, highlight_positions,
                     desk_kind_positions, desk_positions, columns=200,
                     read_key=ScriptedKeys(["pgup", "q"]),
                     initial_cursor_key=next(iter(desk_positions)))
    assert code == ExitCode.OK
    output = stdout.getvalue()
    assert "Loading" not in output
    assert header_line(out, planid, today, floor_names(catalog)) in output


def test_run_live_mouse_click_moves_the_cursor_to_the_desk_under_it(tmp_path):
    catalog, api = _catalog_and_api(tmp_path)
    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=dt.date(2026, 9, 1), stdout=stdout, stderr=stderr)
    planid, highlight_keys = resolve_floor(api, catalog, out.today,
                                           out.today, PLANID_LEVEL5)
    polys = catalog.deskpolys(planid)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == planid}
    img_w, img_h = catalog.floor_image_size(planid)
    desk_states = catalog.availability(out.today, planids=[planid])
    grid, highlight_positions, desk_kind_positions, desk_positions = \
        render_floor(planid, polys, catalog_keys, img_w, img_h,
                    highlight_keys=highlight_keys, desk_states=desk_states)

    start_key = next(iter(desk_positions))
    target_key = nearest_desk(desk_positions[start_key], desk_positions, "right")
    assert target_key is not None
    target_row, target_col = desk_positions[target_key]
    # Reverse `desk_at_click`'s math with left=0, header_lines=1 (both
    # this call's actual values) to get the terminal coords to script.
    term_col, term_row = target_col + 1, target_row + 1 + 1

    ctx = Ctx(out, Config(), api, catalog, Args())
    code = _run_live(ctx, planid, out.today, grid, highlight_positions,
                     desk_kind_positions, desk_positions, columns=200,
                     read_key=ScriptedKeys(
                         [("mouse", 0, term_col, term_row, True), "q"]),
                     initial_cursor_key=start_key)
    assert code == ExitCode.OK
    assert out.fmt_desk(target_key) in stdout.getvalue()


def test_run_live_mouse_release_is_a_no_op(tmp_path):
    catalog, api = _catalog_and_api(tmp_path)
    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=dt.date(2026, 9, 1), stdout=stdout, stderr=stderr)
    planid, highlight_keys = resolve_floor(api, catalog, out.today,
                                           out.today, PLANID_LEVEL5)
    polys = catalog.deskpolys(planid)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == planid}
    img_w, img_h = catalog.floor_image_size(planid)
    desk_states = catalog.availability(out.today, planids=[planid])
    grid, highlight_positions, desk_kind_positions, desk_positions = \
        render_floor(planid, polys, catalog_keys, img_w, img_h,
                    highlight_keys=highlight_keys, desk_states=desk_states)

    start_key = next(iter(desk_positions))
    target_key = nearest_desk(desk_positions[start_key], desk_positions, "right")
    assert target_key is not None
    target_row, target_col = desk_positions[target_key]
    term_col, term_row = target_col + 1, target_row + 1 + 1

    ctx = Ctx(out, Config(), api, catalog, Args())
    code = _run_live(ctx, planid, out.today, grid, highlight_positions,
                     desk_kind_positions, desk_positions, columns=200,
                     read_key=ScriptedKeys(
                         [("mouse", 0, term_col, term_row, False), "q"]),
                     initial_cursor_key=start_key)
    assert code == ExitCode.OK
    assert out.fmt_desk(start_key) in stdout.getvalue()
    assert out.fmt_desk(target_key) not in stdout.getvalue()


def test_status_line_names_the_occupant_without_the_word_booked():
    # Feedback: "booked by <name>" was redundant once a name is shown at
    # all -- the desk's own colour/glyph already says "booked"; the line
    # only needs to add WHO.
    out = Output(today=dt.date(2026, 9, 1), stdout=io.StringIO(),
                stderr=io.StringIO())
    line = _status_line(out, "5.12", "booked", None, occupant="Jane Doe")
    assert "Jane Doe" in line
    assert "booked by" not in line


def test_status_line_lists_the_day_and_floor_keys():
    # Feedback: the day (PgUp/PgDn/n/p) and floor (5/6) keys weren't
    # discoverable at all -- only move/quit were ever shown.
    out = Output(today=dt.date(2026, 9, 1), stdout=io.StringIO(),
                stderr=io.StringIO())
    line = _status_line(out, "5.12", "free", None)
    assert "[n/p] day" in line
    assert "[5/6] floor" in line


def test_run_live_n_advances_the_date_like_pgdn(tmp_path):
    catalog, api = _catalog_and_api(tmp_path)
    stdout, stderr = io.StringIO(), io.StringIO()
    today = dt.date(2026, 9, 1)
    out = Output(today=today, stdout=stdout, stderr=stderr)
    planid, highlight_keys = resolve_floor(api, catalog, out.today,
                                           out.today, PLANID_LEVEL5)
    polys = catalog.deskpolys(planid)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == planid}
    img_w, img_h = catalog.floor_image_size(planid)
    desk_states = catalog.availability(out.today, planids=[planid])
    grid, highlight_positions, desk_kind_positions, desk_positions = \
        render_floor(planid, polys, catalog_keys, img_w, img_h,
                    highlight_keys=highlight_keys, desk_states=desk_states)

    ctx = Ctx(out, Config(), api, catalog, Args())
    code = _run_live(ctx, planid, today, grid, highlight_positions,
                     desk_kind_positions, desk_positions, columns=200,
                     read_key=ScriptedKeys(["n", "q"]),
                     initial_cursor_key=next(iter(desk_positions)))
    assert code == ExitCode.OK
    output = stdout.getvalue()
    tomorrow = today + dt.timedelta(days=1)
    assert f"Loading {out.fmt_date(tomorrow)}" in output
    assert header_line(out, planid, tomorrow, floor_names(catalog)) in output


def test_run_live_p_is_clamped_at_today_like_pgup(tmp_path):
    catalog, api = _catalog_and_api(tmp_path)
    stdout, stderr = io.StringIO(), io.StringIO()
    today = dt.date(2026, 9, 1)
    out = Output(today=today, stdout=stdout, stderr=stderr)
    planid, highlight_keys = resolve_floor(api, catalog, out.today,
                                           out.today, PLANID_LEVEL5)
    polys = catalog.deskpolys(planid)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == planid}
    img_w, img_h = catalog.floor_image_size(planid)
    desk_states = catalog.availability(out.today, planids=[planid])
    grid, highlight_positions, desk_kind_positions, desk_positions = \
        render_floor(planid, polys, catalog_keys, img_w, img_h,
                    highlight_keys=highlight_keys, desk_states=desk_states)

    ctx = Ctx(out, Config(), api, catalog, Args())
    code = _run_live(ctx, planid, today, grid, highlight_positions,
                     desk_kind_positions, desk_positions, columns=200,
                     read_key=ScriptedKeys(["p", "q"]),
                     initial_cursor_key=next(iter(desk_positions)))
    assert code == ExitCode.OK
    output = stdout.getvalue()
    assert "Loading" not in output
    assert header_line(out, planid, today, floor_names(catalog)) in output


def test_run_live_hides_the_real_terminal_cursor(tmp_path):
    # Feedback: the real terminal cursor, left blinking at the bottom of
    # the status line, reads as "you can type a message here" -- nothing
    # in this loop ever reads typed text.
    catalog, api = _catalog_and_api(tmp_path)
    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=dt.date(2026, 9, 1), stdout=stdout, stderr=stderr)
    planid, highlight_keys = resolve_floor(api, catalog, out.today,
                                           out.today, None)
    polys = catalog.deskpolys(planid)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == planid}
    img_w, img_h = catalog.floor_image_size(planid)
    desk_states = catalog.availability(out.today, planids=[planid])
    grid, highlight_positions, desk_kind_positions, desk_positions = \
        render_floor(planid, polys, catalog_keys, img_w, img_h,
                    highlight_keys=highlight_keys, desk_states=desk_states)

    ctx = Ctx(out, Config(), api, catalog, Args())
    code = _run_live(ctx, planid, out.today, grid, highlight_positions,
                     desk_kind_positions, desk_positions, columns=200,
                     read_key=ScriptedKeys(["q"]),
                     initial_cursor_key=next(iter(desk_positions)))
    assert code == ExitCode.OK
    output = stdout.getvalue()
    assert "\x1b[?25l" in output    # hidden on the way in
    assert "\x1b[?25h" in output    # restored on the way out


def test_run_live_marks_the_cursor_cell_with_blink_too(tmp_path):
    # Feedback: reverse video alone wasn't obvious enough.
    catalog, api = _catalog_and_api(tmp_path)
    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=dt.date(2026, 9, 1), stdout=stdout, stderr=stderr,
                color=True)
    planid, highlight_keys = resolve_floor(api, catalog, out.today,
                                           out.today, None)
    polys = catalog.deskpolys(planid)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == planid}
    img_w, img_h = catalog.floor_image_size(planid)
    desk_states = catalog.availability(out.today, planids=[planid])
    grid, highlight_positions, desk_kind_positions, desk_positions = \
        render_floor(planid, polys, catalog_keys, img_w, img_h,
                    highlight_keys=highlight_keys, desk_states=desk_states)

    ctx = Ctx(out, Config(), api, catalog, Args())
    code = _run_live(ctx, planid, out.today, grid, highlight_positions,
                     desk_kind_positions, desk_positions, columns=200,
                     read_key=ScriptedKeys(["q"]),
                     initial_cursor_key=next(iter(desk_positions)))
    assert code == ExitCode.OK
    assert "\x1b[5m" in stdout.getvalue()


def test_run_live_redraw_erases_leftover_rows_from_a_taller_previous_frame(
        tmp_path):
    # Level 5 renders taller than Level 6 -- switching down to it
    # previously left the tail of the bigger map on screen below the
    # smaller one, since each frame's own `\x1b[K` only ever clears to the
    # RIGHT of what it drew, never rows a taller previous frame left
    # below. `\x1b[J` after every redraw fixes that.
    catalog, api = _catalog_and_api(tmp_path)
    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=dt.date(2026, 9, 1), stdout=stdout, stderr=stderr)
    planid, highlight_keys = resolve_floor(api, catalog, out.today,
                                           out.today, PLANID_LEVEL5)
    polys = catalog.deskpolys(planid)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == planid}
    img_w, img_h = catalog.floor_image_size(planid)
    desk_states = catalog.availability(out.today, planids=[planid])
    grid, highlight_positions, desk_kind_positions, desk_positions = \
        render_floor(planid, polys, catalog_keys, img_w, img_h,
                    highlight_keys=highlight_keys, desk_states=desk_states)

    ctx = Ctx(out, Config(), api, catalog, Args())
    code = _run_live(ctx, planid, out.today, grid, highlight_positions,
                     desk_kind_positions, desk_positions, columns=200,
                     read_key=ScriptedKeys(["6", "q"]),
                     initial_cursor_key=next(iter(desk_positions)))
    assert code == ExitCode.OK
    # Every redraw -- the initial frame AND the post-switch one -- erases
    # to end of screen, not just a one-off fix bolted on for this case.
    assert stdout.getvalue().count("\x1b[J") >= 2
