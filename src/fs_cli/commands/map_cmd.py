"""`fs map [<floor>] [<date>] [<name|team>]` -- static per-floor ASCII/
Unicode desk map, own-desk highlight, terminal-width crop, live per-desk
status colour, arrow-key cursor navigation and book/release from the map.
An optional trailing name/team argument highlights matching occupied
desks cyan (`resolve_match_uids`), on top of the existing yellow "yours"
and purple `show_team_on_map` highlights.

Everything but the per-workplace map data lives here rather than the
thin-command-plus-top-level-module split other commands follow: nothing
else calls this rendering logic, so splitting it out would buy no reuse.

Hand-traced constant tables (walls, partitions, room boxes, zone names,
planids) live in `..floorplans`, one module per workplace -- see
`docs/floorplan-map-manual.md` for how each table is derived and for the
wrong-turn/fix pairs the rendering approach below depends on.
"""

import datetime as dt
import re
import shutil
import sys
import threading

from .. import keyread
from ..api import LiveApi, book_start_for
from ..args import reject_forced
from ..catalog import day_bounds
from ..config import BOOKING_BLOCK_REASONS
from ..dates import parse_date
from ..desks import resolve_group_keys
from ..errors import ExitCode, FsError, UsageError
from ..floorplans.example_workplace import (
    BLOCK_ADJUSTMENTS_BY_FLOOR,
    CROP_BY_FLOOR,
    DEFAULT_PLANID,
    PARTITION_ROW_NUDGE,
    PARTITIONS_BY_FLOOR,
    PLANID_LEVEL5,
    PLANID_LEVEL6,
    ROOM_BOXES_BY_FLOOR,
    WALL_BY_FLOOR,
    ZONE_LABEL_NUDGE_BY_FLOOR,
    ZONE_NAMES_BY_FLOOR,
)
from ..render import visible_len
from ..session import Session
from .at_cmd import occupant_name
from .book_cmd import booking_for_date, bookings_for_date
from .list_cmd import own_bookings
from .team_cmd import FOLLOWING, resolve_team_membership

__all__ = [
    "cmd_map", "render_floor", "classify_token", "resolve_floor",
    "crop_window", "terminal_columns", "terminal_size", "desk_centers",
    "cluster",
    "local_ranks", "nearest_desk", "plan_for_cursor", "row_pitch",
    "quantize_axis", "draw_polyline", "draw_box", "PLANID_LEVEL5",
    "PLANID_LEVEL6", "DEFAULT_PLANID", "desk_glyph_kind", "GLYPH_BY_KIND",
    "STYLE_METHOD_BY_KIND", "header_line", "desk_at_click", "floor_names",
    "floor_aliases",
]


def floor_names(catalog):
    """`{planid: display name}` for every floor with desks, from each
    floor's own `Desk.floor` -- keeps `fs map` portable to a building
    with different floor names/numbering. First desk seen for a planid
    wins; a desk with no floor name is skipped. If a floor's name were
    ever absent it contributes no alias, so `classify_token` would
    silently treat what should be a floor token as a date instead
    (`fs map 5` -> "5th of next month", not "Level 5") -- not observed in
    practice, not guarded against.
    """
    names = {}
    for desk in catalog.desks():
        if desk.floor and desk.planid not in names:
            names[desk.planid] = desk.floor
    return names


def floor_aliases(names):
    """`{token: planid}` for `classify_token`. Each floor's name,
    lowercased/stripped, is always an alias; if its last word is a bare
    integer, that integer is also an alias (`"Level 5"` -> `"level5"` and
    `"5"`). First floor wins on a collision.
    """
    aliases = {}
    for planid, name in names.items():
        stripped = name.strip()
        normalised = stripped.lower().replace(" ", "")
        aliases.setdefault(normalised, planid)
        last_word = stripped.split()[-1] if stripped else ""
        if last_word.isdigit():
            aliases.setdefault(last_word, planid)
    return aliases

#: How many chrome lines `_run_live` draws above the grid itself --
#: `header_line`'s one line. `desk_at_click` subtracts this off a raw
#: terminal row to land back on a grid row; kept as a named constant
#: (not a bare `1`) so the two stay in sync if a second header line is
#: ever added.
HEADER_LINES = 1


GLYPH_BY_KIND = {
    "free": "▢", "restricted": "■", "booked": "■", "yours": "▣", "team": "■",
    "match": "■",
}


STYLE_METHOD_BY_KIND = {
    "free": "good", "restricted": "muted", "booked": "danger",
    "yours": "attention", "team": "teammate", "match": "matched",
}

#: `(glyph, Output colour method, label)` triples for the three legend
#: entries that always mean the same thing -- the one place that knows
#: what the map's colour key says for those, shared by `_run_static`'s
#: own legend line and the live view's header-line legend
#: (`_legend_text`) so the two can never drift apart in wording.
#: `team`/`match` aren't here: their label is the actual configured
#: `show_team_on_map` name / typed `fs map <name|team>` token, not a
#: fixed word -- `_legend_text` builds those two itself.
_LEGEND_BASE = (
    ("▢", "good", "free"),
    ("■", "danger", "unavailable"),
)
_LEGEND_YOURS = ("▣", "attention", "yours")


def _legend_text(out, sep="   ", team_label=None, match_label=None):
    """`▢ free   ■ unavailable   ■ <team_label>   ■ <match_label>   ▣
    yours` -- `team_label`/`match_label` name the real
    `show_team_on_map` team and the real typed match target, not the
    generic words "team"/"match", and each is omitted entirely when
    there's nothing configured/typed for it to mean (`None` or blank).

    When both are given and equal (case/whitespace-insensitively), only
    `match_label` is shown -- the same name resolves to the exact same
    uid set for both (`resolve_team_uids`/`resolve_match_uids` both
    reach `team_cmd.resolve_team_membership`), so `desk_glyph_kind`'s
    own "match beats team" precedence means the "team" colour could
    never actually appear for it; showing both would just be a
    confusing duplicate.

    `sep` is the gap between entries, tightened by the live view's
    header line (shares the row with the floor/date text) versus
    `_run_static`'s own dedicated legend line.
    """
    items = list(_LEGEND_BASE)
    same_target = bool(team_label and match_label
                       and team_label.strip().lower()
                       == match_label.strip().lower())
    if team_label and not same_target:
        items.append(("■", "teammate", team_label))
    if match_label:
        items.append(("■", "matched", match_label))
    items.append(_LEGEND_YOURS)
    return sep.join(f"{getattr(out, method)(glyph)} {label}"
                    for glyph, method, label in items)


def desk_glyph_kind(desk_key, highlight_keys, desk_states, team_uids=frozenset(),
                    match_uids=frozenset()):
    """One desk's live-status glyph kind: "yours" beats "match" (the typed
    `fs map <name|team>` target) beats "team" (a `show_team_on_map`
    teammate) beats "free"/"restricted"/"booked" from `desk_states`, or
    "free" uniformly when `desk_states` is `None`. A key absent from a
    given `desk_states` defaults to "booked" -- the conservative case,
    since a CLI that can't confirm bookability shouldn't draw a desk as
    free.

    `team_uids`/`match_uids` need no per-desk name lookup -- the occupant
    `uid` is already in `desk_states` for the whole floor, so both
    highlights cover every desk from the first frame, not just wherever
    the cursor has visited (unlike the status line's lazy occupant-name
    fetch).
    """
    if desk_key in highlight_keys:
        return "yours"
    if desk_states is None:
        return "free"
    state = desk_states.get(desk_key)
    if state is None or not state.free:
        if match_uids and state is not None and state.uid in match_uids:
            return "match"
        if team_uids and state is not None and state.uid in team_uids:
            return "team"
        return "booked"
    if not state.book_advance:
        return "restricted"
    return "free"


def desk_centers(polys, catalog_keys):
    """id -> (cx, cy, w, h) in image pixel space, for real bookable desks
    only -- `polys` and `catalog_keys` don't always agree (some poly
    entries have no catalog desk behind them), so this filters once
    rather than in every caller.
    """
    out = {}
    for key, poly in polys.items():
        if key not in catalog_keys:
            continue
        pts = poly["points"]
        xs = [p["x"] for p in pts]
        ys = [p["y"] for p in pts]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        out[key] = ((x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0)
    return out


def cluster(centers, gap):
    """Union desks into pods: two desks join a cluster if their bounding
    boxes, expanded by `gap`, overlap. A cheap flood-fill -- fine for a
    few hundred desks."""
    rects = {k: (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
             for k, (cx, cy, w, h) in centers.items()}
    ids = list(rects)

    def touches(a, b):
        ax0, ay0, ax1, ay1 = rects[a]
        bx0, by0, bx1, by1 = rects[b]
        return not (ax1 + gap < bx0 or bx1 + gap < ax0
                    or ay1 + gap < by0 or by1 + gap < ay0)

    visited = set()
    clusters = []
    for start in ids:
        if start in visited:
            continue
        stack = [start]
        visited.add(start)
        members = [start]
        while stack:
            cur = stack.pop()
            for other in ids:
                if other not in visited and touches(cur, other):
                    visited.add(other)
                    members.append(other)
                    stack.append(other)
        clusters.append(members)
    return clusters


def local_ranks(values, threshold):
    """Sort one cluster's worth of values into rank buckets 0, 1, 2, ...
    by gap. Returns (id -> rank, number of ranks). Safe only because the
    set is small/local -- see `quantize_axis` for why the same trick
    isn't safe globally."""
    items = sorted(values.items(), key=lambda kv: kv[1])
    groups = [[items[0]]]
    for prev, cur in zip(items, items[1:], strict=False):
        if cur[1] - prev[1] <= threshold:
            groups[-1].append(cur)
        else:
            groups.append([cur])
    rank = {}
    for i, group in enumerate(groups):
        for key, _ in group:
            rank[key] = i
    return rank, len(groups)


def row_pitch(centers):
    """The back-to-back desk depth -- smallest side of the median desk --
    used as the scale unit for both axes. Deriving scale from a target
    output width instead collapses back-to-back rows onto one character
    row (docs/floorplan-map-manual.md's wrong-turn #1)."""
    depths = sorted(min(w, h) for _, _, w, h in centers.values())
    return depths[len(depths) // 2]


def quantize_axis(values, grid):
    """Snaps every value independently to the nearest multiple of `grid`
    -- a fixed lattice, not neighbour comparison (chaining sorted values
    within a threshold silently bridges distinct rows when an unrelated
    desk sits between them -- wrong-turn #2). `grid` must stay well under
    the real column/row pitch."""
    return {key: round(v / grid) * grid for key, v in values.items()}


def draw_polyline(grid, points, closed=True, gap_ends=0):
    """Straight segments between consecutive (row, col) points.
    Mostly-horizontal segments get '─', mostly-vertical get '│', and
    genuinely diagonal segments get '/' or '\\'.

    `gap_ends` leaves that many characters undrawn at each end of the
    whole polyline -- a partition's real-world ends aren't sealed into a
    wall, they're a walkway, and an unbroken line reads as a sealed room.
    """
    pts = list(points)
    if closed:
        pts = pts + [pts[0]]
    n_rows = len(grid)
    n_cols = len(grid[0]) if grid else 0

    def put(r, c, ch):
        if 0 <= r < n_rows and 0 <= c < n_cols and grid[r][c] == " ":
            grid[r][c] = ch

    cells = []
    for (r0, c0), (r1, c1) in zip(pts, pts[1:], strict=False):
        dr, dc = r1 - r0, c1 - c0
        steps = max(abs(dr), abs(dc), 1)
        if abs(dr) <= 1:
            ch = "─"
        elif abs(dc) <= 1:
            ch = "│"
        else:
            ch = "\\" if (dr > 0) == (dc > 0) else "/"
        for i in range(steps + 1):
            r = round(r0 + dr * i / steps)
            c = round(c0 + dc * i / steps)
            cells.append((r, c, ch))

    for r, c, ch in cells[gap_ends: len(cells) - gap_ends or None]:
        put(r, c, ch)


def draw_box(grid, r0, c0, r1, c1, label=None):
    """A plain rectangle -- for rooms with no desks in them, where the
    goal is "here's a room, don't route through it", not fidelity."""
    draw_polyline(grid, [(r0, c0), (r0, c1), (r1, c1), (r1, c0)])
    if label:
        n_cols = len(grid[0]) if grid else 0
        r = (r0 + r1) // 2
        c = max(c0 + 1, (c0 + c1) // 2 - len(label) // 2)
        for i, ch in enumerate(label):
            if c + i < min(c1, n_cols):
                grid[r][c + i] = ch


def _zone_name(planid, cx_frac, cy_frac):
    for name, x0, y0, x1, y1 in ZONE_NAMES_BY_FLOOR.get(planid, []):
        if x0 <= cx_frac <= x1 and y0 <= cy_frac <= y1:
            return name
    return None


def render_floor(planid, polys, catalog_keys, img_w, img_h,
                  highlight_keys=frozenset(), desk_states=None,
                  team_uids=frozenset(), match_uids=frozenset()):
    """Render one floor's desks, walls, partitions, room boxes and zone
    labels into a character grid. See docs/floorplan-map-manual.md for
    the wrong-turn/fix pairs this approach depends on. `highlight_keys`
    marks the caller's own desk(s); `desk_states`/`team_uids`/`match_uids`
    decide each other desk's glyph via `desk_glyph_kind`/`GLYPH_BY_KIND`.
    Stays colour-blind -- picks a glyph, never an ANSI code; `cmd_map`
    turns the returned "kind" into an `Output` colour method.

    Returns `(grid, highlight_positions, desk_kind_positions,
    desk_positions)`: `highlight_positions` is just the "yours" cells
    (what `cmd_map`'s crop math anchors on); `desk_kind_positions` maps
    every drawn cell to its kind; `desk_positions` is its reverse
    (key -> cell), for O(1) "what's under the cursor" lookups -- safe
    since no two desk keys ever share a position.
    """
    centers = desk_centers(polys, catalog_keys)

    unit = row_pitch(centers)
    row_scale = 1.0 / unit
    col_scale = row_scale * 2   # character cells are ~2x taller than wide

    crop = CROP_BY_FLOOR.get(planid)
    origin_x = crop[0] * img_w if crop else 0
    origin_y = crop[1] * img_h if crop else 0
    extent_w = (crop[2] - crop[0]) * img_w if crop else img_w
    extent_h = (crop[3] - crop[1]) * img_h if crop else img_h

    def col(x):
        return round((x - origin_x) * col_scale)

    def row(y):
        return round((y - origin_y) * row_scale)

    n_cols = round(extent_w * col_scale) + 2
    n_rows = round(extent_h * row_scale) + 2
    grid = [[" "] * n_cols for _ in range(n_rows)]

    wall = [(row(yf * img_h), col(xf * img_w))
            for xf, yf in WALL_BY_FLOOR.get(planid, [])]
    if wall:
        draw_polyline(grid, wall)

    for x0f, y0f, x1f, y1f in PARTITIONS_BY_FLOOR.get(planid, []):
        r0, c0 = row(y0f * img_h) + PARTITION_ROW_NUDGE, col(x0f * img_w)
        r1, c1 = row(y1f * img_h) + PARTITION_ROW_NUDGE, col(x1f * img_w)
        draw_polyline(grid, [(r0, c0), (r1, c1)], closed=False, gap_ends=1)

    for label, x0f, y0f, x1f, y1f in ROOM_BOXES_BY_FLOOR.get(planid, []):
        r0, c0 = row(y0f * img_h), col(x0f * img_w)
        r1, c1 = row(y1f * img_h), col(x1f * img_w)
        draw_box(grid, r0, c0, r1, c1, label)

    short_side = row_pitch(centers)
    clusters = cluster(centers, short_side * 1.1)
    block_infos = []
    for members in clusters:
        xs = {m: centers[m][0] for m in members}
        ys = {m: centers[m][1] for m in members}
        col_rank, _ = local_ranks(xs, short_side * 0.6)
        row_rank, _ = local_ranks(ys, short_side * 0.6)
        widths = [centers[m][2] for m in members]
        heights = [centers[m][3] for m in members]
        landscape = sum(widths) >= sum(heights)
        pitch_col, pitch_row = (4, 1) if landscape else (2, 2)

        anchor_row = row(min(ys.values()))
        anchor_col = col(min(xs.values()))
        cx_mid = sum(xs.values()) / len(xs)
        cy_mid = sum(ys.values()) / len(ys)
        name = _zone_name(planid, cx_mid / img_w, cy_mid / img_h)
        block_infos.append((anchor_row, anchor_col, pitch_row, pitch_col,
                             row_rank, col_rank, members, name))

    # Reading order -- `BLOCK_ADJUSTMENTS_BY_FLOOR` keys off this numbering.
    block_infos.sort(key=lambda b: (b[0], b[1]))

    adjustments = BLOCK_ADJUSTMENTS_BY_FLOOR.get(planid, {})
    block_infos = [
        ((info[0] + adjustments.get(number, (0, 0))[0],
          info[1] + adjustments.get(number, (0, 0))[1]) + info[2:])
        for number, info in enumerate(block_infos, start=1)
    ]

    highlight_positions = set()
    desk_kind_positions = {}
    desk_positions = {}
    for anchor_row, anchor_col, pitch_row, pitch_col, row_rank, col_rank, \
            members, _name in block_infos:
        for m in members:
            r = anchor_row + row_rank[m] * pitch_row
            c = anchor_col + col_rank[m] * pitch_col
            if 0 <= r < n_rows and 0 <= c < n_cols:
                kind = desk_glyph_kind(m, highlight_keys, desk_states,
                                       team_uids, match_uids)
                grid[r][c] = GLYPH_BY_KIND[kind]
                desk_kind_positions[(r, c)] = kind
                desk_positions[m] = (r, c)
                if kind == "yours":
                    highlight_positions.add((r, c))

    def put_label(r, c, text):
        for j, ch in enumerate(text):
            if 0 <= c + j < n_cols and grid[r][c + j] == " ":
                grid[r][c + j] = ch

    zones_labeled = set()
    for info in block_infos:
        anchor_row, anchor_col, name = info[0], info[1], info[-1]
        if name is None or name in zones_labeled:
            continue
        zones_labeled.add(name)
        label_row = anchor_row - 1 if anchor_row > 0 else anchor_row
        nudge = ZONE_LABEL_NUDGE_BY_FLOOR.get(planid, {}).get(name, 0)
        put_label(label_row, anchor_col + nudge, name)

    return grid, highlight_positions, desk_kind_positions, desk_positions


#: 0 = row axis is primary, 1 = col axis is primary, per direction.
_PRIMARY_AXIS = {"up": 0, "down": 0, "left": 1, "right": 1}
#: Which way along the primary axis counts as "ahead", per direction.
_SIGN = {"up": -1, "down": 1, "left": -1, "right": 1}
#: Weight on the perpendicular-axis delta in the movement score -- same
#: constant as `render_floor`'s own `col_scale = row_scale * 2` character-
#: aspect correction, reused for the same reason it was chosen there.
_PERPENDICULAR_WEIGHT = 2


def nearest_desk(cursor_pos, desk_positions, direction):
    """The desk nearest `cursor_pos` in `direction`, by
    `abs(primary_delta) + 2 * abs(perpendicular_delta)` -- a directional-
    focus heuristic, not Euclidean distance, so a diagonal desk can't win
    over one directly ahead. Candidates are filtered to the correct
    half-plane first (strict, so a desk on the perpendicular axis never
    counts). Returns `None` (never raises) when nothing qualifies.
    """
    cursor_row, cursor_col = cursor_pos
    axis = _PRIMARY_AXIS[direction]
    sign = _SIGN[direction]
    best_key, best_score = None, None
    for key, (row, col) in desk_positions.items():
        delta_row, delta_col = row - cursor_row, col - cursor_col
        primary = delta_row if axis == 0 else delta_col
        perpendicular = delta_col if axis == 0 else delta_row
        if primary * sign <= 0:
            continue
        score = abs(primary) + _PERPENDICULAR_WEIGHT * abs(perpendicular)
        if best_score is None or score < best_score:
            best_key, best_score = key, score
    return best_key


def terminal_columns(stream):
    """The usable terminal width, or `None` when there isn't one to crop
    to (no tty, or `shutil.get_terminal_size()` failing/nonsensical) --
    `None` means "show the full uncropped map"."""
    try:
        if not stream.isatty():
            return None
    except (AttributeError, ValueError):
        return None
    try:
        columns = shutil.get_terminal_size().columns
    except OSError:
        return None
    return columns if isinstance(columns, int) and columns > 0 else None


def terminal_size(stream):
    """Both usable terminal dimensions -- `(columns, rows)`, or
    `(None, None)` -- same guards as `terminal_columns`, extended to
    `rows` for the live view's vertical crop/scroll. Called once per
    frame from inside the redraw loop, so a resize is picked up on the
    next keypress with no signal handler needed."""
    try:
        if not stream.isatty():
            return None, None
    except (AttributeError, ValueError):
        return None, None
    try:
        size = shutil.get_terminal_size()
    except OSError:
        return None, None
    columns = (size.columns
               if isinstance(size.columns, int) and size.columns > 0 else None)
    rows = size.lines if isinstance(size.lines, int) and size.lines > 0 else None
    return columns, rows


def crop_window(n_cols, highlight_cols, terminal_columns):
    """The `(left, right)` half-open column window to slice the rendered
    grid to, or `None` for no crop (`terminal_columns` is `None`).
    Axis-agnostic despite the naming -- `_run_live`'s vertical scroll
    reuses this for rows.

    No highlight: anchored at column 0, clipped at `terminal_columns`.
    With one: right edge is the larger of "just past the highlight" and
    "as much of `terminal_columns` as the floor has" -- without that
    second term a highlight near the left edge would collapse the window
    to a sliver even on a wide terminal. Left edge only ever clamps at 0.
    """
    if terminal_columns is None or terminal_columns <= 0:
        return None
    if not highlight_cols:
        return (0, min(terminal_columns, n_cols))
    right = min(n_cols, max(max(highlight_cols) + 1,
                            min(terminal_columns, n_cols)))
    left = max(right - terminal_columns, 0)
    return (left, right)


#: How many cells of run-way to keep visible past the cursor (and,
#: symmetrically, before scrolling back) in `scroll_window`. Small enough
#: not to eat into the visible grid on a narrow terminal, big enough that
#: the wall just past the cursor is actually on screen rather than the
#: cursor sitting dead on the last visible cell.
SCROLL_MARGIN = 4

#: Column-scroll (left/right) run-way, one cell more than the shared
#: `SCROLL_MARGIN` -- horizontal scrolling needs the extra cell to keep a
#: wall in view where vertical scrolling doesn't. Row scrolling keeps
#: using `SCROLL_MARGIN` (see the `scroll_window` call for rows, further
#: down).
SCROLL_MARGIN_COLS = SCROLL_MARGIN + 1


def scroll_window(prev, pos, n_total, terminal_size, margin=SCROLL_MARGIN):
    """Stateful, axis-agnostic counterpart to `crop_window` for
    `_run_live`'s cursor-following scroll. A fresh `crop_window` every
    frame pins the cursor to the screen edge (no run-way) and snaps back
    to the anchor on any move off that edge, since it has no memory.

    Takes `prev` (last frame's window, `None` on the first frame/resize)
    and only moves it once `pos` gets within `margin` of an edge, just
    far enough to restore that margin -- so it doesn't scroll back until
    the cursor nears the opposite edge either.
    """
    if terminal_size is None or terminal_size <= 0:
        return None
    width = min(terminal_size, n_total)
    if prev is None or (prev[1] - prev[0]) != width:
        # First frame or resize -- re-anchor around `pos` with run-way
        # rather than centring, so a resize doesn't itself cause a jump.
        left = max(0, min(pos - margin, n_total - width))
        return (left, left + width)
    left, right = prev
    if pos >= right - margin:
        right = min(n_total, pos + margin + 1)
        left = right - width
    elif pos < left + margin:
        left = max(0, pos - margin)
        right = left + width
    return (left, right)


def header_line(out, planid, target_date, names, columns=None,
                team_label=None, match_label=None):
    """`Level 5 -- Wed 27 Aug`, the live view's first line. `names` (a
    `floor_names()` result) supplies the label; falls back to the bare
    planid if it's ever missing rather than raising.

    `columns`, when given, right-justifies the colour-key legend
    (`_legend_text`, with `team_label`/`match_label` passed straight
    through) onto this same line via `_right_justify_hint` -- the live
    view's own way to fit a legend in without spending a whole extra row
    on it; a long floor/date label clips before the legend ever would,
    same "protect the right-hand hint" rule the status line already
    follows. `columns=None` (every direct/test call, and `_run_static`'s
    separate dedicated legend line) returns just the plain floor/date
    text, unchanged.
    """
    level = names.get(planid, f"planid {planid}")
    left = f"{level} -- {out.fmt_date(target_date)}"
    if columns is None:
        return left
    return _right_justify_hint(
        out, left, _legend_text(out, sep="  ", team_label=team_label,
                                match_label=match_label),
        columns)


def desk_at_click(term_col, term_row, left, pos_to_key,
                  header_lines=HEADER_LINES, top=0):
    """The desk key under a 1-based terminal `(term_col, term_row)` mouse
    click, or `None` off-grid/on empty space -- inverts `_render_lines`'s
    placement (chrome rows above, `top`/`left` cropped off each visible
    row/column). `pos_to_key` is the `(row, col) -> key` reverse of
    `desk_positions` for the frame the click was drawn against."""
    grid_row = term_row - 1 - header_lines + top
    grid_col = (term_col - 1) + left
    if grid_row < 0 or grid_col < 0:
        return None
    return pos_to_key.get((grid_row, grid_col))


def classify_token(token, today, aliases):
    """A `map` positional token as `("date", date)`, `("floor", planid)`,
    or `None`. `aliases` is a `floor_aliases()` result.

    Floor wins over date: a floor alias like `"5"` is ALSO date-shaped
    (`parse_date` reads a lone 1-31 number as day-of-month), so checking
    `aliases` first is what keeps `fs map 5` meaning "Level 5" rather
    than "the 5th of next month". Everything else falls through to
    `parse_date` unchanged.
    """
    normalised = token.strip().lower()
    if normalised in aliases:
        return ("floor", aliases[normalised])
    date = parse_date(token, today)
    if date is not None:
        return ("date", date)
    return None


def _covering_desk_keys(bookings, target_date):
    """Own desk keys booked to cover `target_date`. Plural -- the
    one-desk-per-day limit is per group, so more than one is possible."""
    covering = bookings_for_date(bookings, target_date)
    return {b["key"] for b in covering if b.get("key")}


def resolve_floor(api, catalog, today, target_date, explicit_planid,
                  bookings=None):
    """`(planid, highlight_keys)`.

    `explicit_planid` given: no fallback search; highlight only if a
    covering booking's desk is actually on that floor.

    Omitted: 1) a booking covering `target_date` sets floor+highlight;
    2) failing that, the next own booking sets the floor with no
    highlight; 3) failing that, the catalog's own first floor, no
    highlight (`DEFAULT_PLANID` is the last resort, only when
    `catalog.planids()` has nothing at all).

    Fetches `own_bookings` once, reused for both checks -- unless
    `bookings` is given (`cmd_map` already has it, and needs the exact
    same fetch again for `_run_live`'s entry; passing it through here is
    what stops a second `own_bookings` call being wasted on this one).
    """
    bookings = (own_bookings(api, today, catalog=catalog) if bookings is None
               else bookings)
    covering_keys = _covering_desk_keys(bookings, target_date)

    if explicit_planid is not None:
        highlight = {k for k in covering_keys
                     if (catalog.desk_by_key(k) or _NO_DESK).planid
                     == explicit_planid}
        return explicit_planid, highlight

    if covering_keys:
        for key in covering_keys:
            desk = catalog.desk_by_key(key)
            if desk is not None:
                planid = desk.planid
                highlight = {k for k in covering_keys
                             if (catalog.desk_by_key(k) or _NO_DESK).planid
                             == planid}
                return planid, highlight

    for booking in bookings:
        key = booking.get("key")
        desk = catalog.desk_by_key(key) if key else None
        if desk is not None:
            return desk.planid, set()

    planids = catalog.planids()
    return (planids[0] if planids else DEFAULT_PLANID), set()


class _NoDesk:
    """Never-matching stand-in for a `desk_by_key` miss, so `.planid`
    comparisons above don't need a `None`-check at each call site."""
    planid = object()


_NO_DESK = _NoDesk()


def plan_for_cursor(kind, key, current):
    """The CREATE/REPLACE/RELEASE/no-op decision for Enter on the desk
    under the cursor -- pure, kept separate from `_run_live`'s actual
    `api.booking_*` call. `current` is `book_cmd.booking_for_date`'s
    result for `target_date`, already fetched by the caller. `key` isn't
    read by the decision itself, kept for signature symmetry.

    Returns `("release", bkid)`, `("create", None)`, `("replace", bkid)`,
    or `None` for the restricted/booked no-op.
    """
    if kind == "yours":
        return ("release", current["bkid"])
    if kind == "free":
        if current is None:
            return ("create", None)
        return ("replace", current["bkid"])
    return None


def resolve_team_uids(ctx):
    """The uid set behind `show_team_on_map`, resolved via
    `team_cmd.resolve_team_membership` (`following` goes to the server,
    anything else reads `ctx.config.teams`). Case/whitespace-tolerant on
    `following`. A misconfigured name resolves to an empty set rather
    than raising -- `fs map` still has to work, it just highlights
    nobody. A failed `following` lookup is swallowed the same way; this
    only decides a highlight colour.
    """
    name = (ctx.config.show_team_on_map or "").strip()
    if not name:
        return frozenset()
    if name.lower() == FOLLOWING:
        name = FOLLOWING
    try:
        current, _, _ = resolve_team_membership(ctx, name)
    except Exception:                             # noqa: BLE001
        return frozenset()
    return frozenset(uid for uid, _ in current if uid)


def resolve_match_uids(ctx, token, target_date):
    """The uid set behind `fs map`'s optional trailing name/team token, or
    `frozenset()` if none was given. A token matching a configured team
    (or `following`) resolves like `resolve_team_uids`, current members
    from `resolve_team_membership`. Anything else is a `user_search` name
    match -- every fuzzy hit, exactly like `find_cmd._search_rows` --
    scoped to `target_date` since that's the only day being drawn.
    Lookup failures are swallowed the same way `resolve_team_uids`
    swallows them; this only decides a highlight colour.
    """
    if not token or not token.strip():
        return frozenset()
    lname = token.strip().lower()
    team = next((t for t in ctx.config.teams if t.lower() == lname), None)
    if lname == FOLLOWING or team is not None:
        try:
            current, _, _ = resolve_team_membership(ctx, team or FOLLOWING)
        except Exception:                         # noqa: BLE001
            return frozenset()
        return frozenset(uid for uid, _ in current if uid)
    start, finish = day_bounds(target_date)
    try:
        hits = ctx.api.user_search(token, start, finish)
    except Exception:                             # noqa: BLE001
        return frozenset()
    return frozenset(str(h["uid"]) for h in hits
                     if isinstance(h, dict) and h.get("uid"))


def cmd_map(ctx, stdin=None):
    """`fs map [<floor>] [<date>] [<name|team>]` -- see the module docstring.

    Cost: cold cache is up to 3 cached `floorplan_booking` calls
    (`catalog.desks()` x2 + `deskpolys()`), cached 30 days. Own-bookings
    and `availability()` are uncached every run regardless (permission-
    sensitive). Worst case: 5 calls, +1 more (`user-search` or
    `booking-summary`) when a `<name|team>` argument is given. Warm
    cache: 2 (+1).
    """
    out, api, catalog = ctx.out, ctx.api, ctx.catalog

    if ctx.args.json:
        raise UsageError(
            "fs map does not support --json",
            hint="There is no structured equivalent of an ASCII map.")
    reject_forced(ctx.args, "fs map")

    tokens = list(ctx.args.args)
    if len(tokens) > 3:
        raise UsageError(
            "fs map takes at most a floor, a date, and a name or team",
            hint="e.g. `fs map`, `fs map mon`, `fs map 6`, `fs map 6 mon`, "
                 "`fs map inception mon`.")

    # A cold cache can take a perceptible moment with nothing on screen --
    # printed before token classification since that also needs
    # `floor_names(catalog)`'s cold-cache fetch.
    out.print("Loading floor...")

    explicit_planid = None
    explicit_date = None
    match_token = None
    if tokens:
        # Guarded on `tokens` so bare `fs map` skips a derivation it has
        # nothing to classify.
        names = floor_names(catalog)
        aliases = floor_aliases(names)
        for token in tokens:
            classified = classify_token(token, out.today, aliases)
            if classified is None:
                # Neither a floor nor a date -- the one non-floor,
                # non-date token allowed is a name/team match target.
                if match_token is not None:
                    raise UsageError(
                        "fs map only takes one name or team",
                        hint=f"Already have {match_token!r}; got {token!r} "
                             "too. A multi-word name needs quoting: "
                             '`fs map "jane doe"`.')
                match_token = token
                continue
            kind, value = classified
            if kind == "date":
                if explicit_date is not None:
                    raise UsageError(
                        "fs map only takes one date",
                        hint=f"Already have a date; got {token!r} too.")
                explicit_date = value
            else:
                if explicit_planid is not None:
                    raise UsageError(
                        "fs map only takes one floor",
                        hint=f"Already have a floor; got {token!r} too.")
                explicit_planid = value

    target_date = explicit_date or out.today
    # No `ctx.load_tags()` here, unlike every desk-listing command --
    # this command never prints a raw desk key through `out.fmt_desk`, so
    # `Output.tags` is never read.

    # Fetched once and threaded through to `resolve_floor` and (for the
    # live view) `_run_live` -- both used to independently re-fetch this,
    # doubling `own_bookings`'s own `booking-list`/`booking-summary`
    # calls on every single `fs map` invocation for no reason: nothing
    # between the two fetches could have changed what's booked.
    bookings = own_bookings(api, out.today, catalog=catalog)
    planid, highlight_keys = resolve_floor(api, catalog, out.today,
                                           target_date, explicit_planid,
                                           bookings=bookings)

    polys = catalog.deskpolys(planid)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == planid}
    img_w, img_h = catalog.floor_image_size(planid)

    desk_states = catalog.availability(target_date, planids=[planid])
    team_uids = resolve_team_uids(ctx)
    match_uids = resolve_match_uids(ctx, match_token, target_date)
    if match_token is not None and not match_uids:
        out.warn(f"no match for {match_token!r}")
    # For the legend, not the highlight itself -- the real configured
    # name / typed token, shown once, so it stays stable across a live
    # session's floor/date switches rather than flickering with whatever
    # happens to be visible on any one frame.
    team_label = (ctx.config.show_team_on_map or "").strip() or None
    match_label = (match_token or "").strip() or None

    grid, highlight_positions, desk_kind_positions, desk_positions = render_floor(
        planid, polys, catalog_keys, img_w, img_h,
        highlight_keys=highlight_keys, desk_states=desk_states,
        team_uids=team_uids, match_uids=match_uids)
    if (match_token is not None and match_uids
            and "match" not in desk_kind_positions.values()):
        # A real match, just not seated on THIS floor/date -- distinct
        # from the no-hits-at-all warning above.
        out.warn(f"{match_token!r} matches nobody on this floor for "
                f"{out.fmt_date(target_date)}")

    columns, rows = terminal_size(sys.stdout)
    stdin = stdin if stdin is not None else sys.stdin
    live = (not ctx.args.no_nav and columns is not None
           and keyread.capable(stdin) is not False)
    if live:
        # `_initial_cursor_key`'s second choice, after "yours" -- a stale
        # config entry warns and drops rather than taking the map down.
        group_keys = resolve_group_keys(
            ctx.config.groups.get(ctx.config.default_group, []),
            [d.key for d in catalog.desks()], out)
        # Re-queries every frame so a resize mid-session takes effect on
        # the next redraw instead of staying cropped to stale dimensions.
        return _run_live(ctx, planid, target_date, grid, highlight_positions,
                         desk_kind_positions, desk_positions, columns,
                         stdin=stdin, desk_states=desk_states,
                         group_keys=group_keys, rows=rows,
                         team_uids=team_uids, match_uids=match_uids,
                         term_size=lambda: terminal_size(sys.stdout),
                         team_label=team_label, match_label=match_label,
                         bookings=bookings)
    return _run_static(out, grid, highlight_positions, desk_kind_positions,
                       columns, team_label=team_label,
                       match_label=match_label)


def _render_lines(out, grid, desk_kind_positions, left, right,
                  cursor_pos=None, top=0, bottom=None):
    """The coloured, cropped text lines for one frame -- shared by
    `_run_static` and `_run_live` so the two never drift on how a "kind"
    becomes an `Output` colour method.

    `cursor_pos`, when given, gets `reverse`+`blink`+`bold` layered on
    its cell's colour (reverse alone isn't distinguishable from another
    desk of the same kind); `"yours"` desks are always bolded so your own
    bookings stay findable at a glance.

    `top`/`bottom` (default: whole grid) are the vertical-scroll
    counterpart to `left`/`right`. `pos` is built from the ABSOLUTE row
    index, not an index into the sliced sub-list -- `desk_kind_positions`/
    `cursor_pos` are keyed by real grid coordinates regardless of crop.
    """
    bottom = len(grid) if bottom is None else bottom
    lines = []
    for r in range(top, bottom):
        row_chars = grid[r]
        chunk = row_chars[left:right]
        parts = []
        for i, ch in enumerate(chunk):
            pos = (r, left + i)
            kind = desk_kind_positions.get(pos)
            styled = getattr(out, STYLE_METHOD_BY_KIND[kind])(ch) if kind else ch
            if kind == "yours":
                styled = out.bold(styled)
            parts.append(out.bold(out.blink(out.reverse(styled)))
                        if pos == cursor_pos else styled)
        lines.append("".join(parts).rstrip())
    return lines


def _run_static(out, grid, highlight_positions, desk_kind_positions, columns,
                team_label=None, match_label=None):
    """One crop, one print, one legend line, exit -- what every
    non-capable terminal (and `--no-nav`) gets. `team_label`/
    `match_label` are `cmd_map`'s real `show_team_on_map` name / typed
    match target, passed straight through to `_legend_text`."""
    n_cols = len(grid[0]) if grid else 0
    # Anchor the crop on "match" cells too, not just "yours" --
    # `render_floor`'s `highlight_positions` is deliberately "yours"-only
    # (see its docstring), so a `fs map <name>` hit with no own booking
    # would otherwise crop to column 0 and draw nothing visible.
    match_cols = {pos[1] for pos, kind in desk_kind_positions.items()
                 if kind == "match"}
    highlight_cols = {c for _r, c in highlight_positions} | match_cols
    window = crop_window(n_cols, highlight_cols, columns)
    left, right = window if window else (0, n_cols)

    out.print("\n".join(_render_lines(out, grid, desk_kind_positions, left, right)))
    out.print(_legend_text(out, team_label=team_label, match_label=match_label))
    return ExitCode.OK


def _initial_cursor_key(grid, desk_positions, desk_kind_positions,
                        highlight_positions, columns, group_keys=()):
    """Where the cursor starts, in order:

      1. The first (lowest-sorting) `"yours"` desk key, if any.
      1.5. Failing that, the first `"match"` desk key -- a `fs map
         <name|team>` hit is exactly what the user asked to land on, and
         it puts the status line's occupant name on screen immediately.
      2. Failing that, the first `group_keys` desk that's on this floor
         and currently free -- starting on a desk you can't book is more
         confusing than starting somewhere plain.
      3. Failing that, the reading-order-first desk visible in the
         initial static crop window.
    """
    yours_keys = [key for key, pos in desk_positions.items()
                 if desk_kind_positions.get(pos) == "yours"]
    if yours_keys:
        return min(yours_keys)

    match_keys = [key for key, pos in desk_positions.items()
                 if desk_kind_positions.get(pos) == "match"]
    if match_keys:
        return min(match_keys)

    for key in group_keys:
        pos = desk_positions.get(key)
        if pos is not None and desk_kind_positions.get(pos) == "free":
            return key

    n_cols = len(grid[0]) if grid else 0
    highlight_cols = {c for _r, c in highlight_positions}
    window = crop_window(n_cols, highlight_cols, columns)
    left, right = window if window else (0, n_cols)
    visible = [(pos, key) for key, pos in desk_positions.items()
              if left <= pos[1] < right]
    if not visible:
        visible = [(pos, key) for key, pos in desk_positions.items()]
    _pos, key = min(visible)
    return key


#: The move/day/floor/quit keys, shared by every `_status_line` branch.
#: This is only the FALLBACK used when no `floor_tokens` are available
#: (a direct/test call to `_status_line`) -- `_run_live` always passes
#: its own `nav_hint`, built by `_floor_switch_hint`/`_nav_hint_for` from
#: the actual floors on offer, not this hardcoded `5`/`6`.
_NAV_HINT = "[↑↓←→] move  [n/p] day  [5/6] floor  [q] quit"


def _floor_switch_hint(floor_tokens):
    """The `[5/6] floor` nav-hint segment (trailing double space, or `""`),
    built from the floors this live view can actually reach with a
    single keypress -- every `floor_aliases()` token exactly one
    character long (in practice always a digit; see that function's
    docstring), sorted for a stable display order. `""` when fewer than
    two exist: nothing worth advertising with zero or one reachable
    floor.
    """
    keys = sorted(k for k in floor_tokens if len(k) == 1)
    if len(keys) < 2:
        return ""
    return f"[{'/'.join(keys)}] floor  "


def _nav_hint_for(floor_tokens):
    """`_status_line`'s real keyboard-hint text, built once per
    `_run_live` call (`floor_tokens` is fixed for the life of that
    call) -- so a workplace with more, fewer, or differently-named
    floors than this one's `5`/`6` still gets an accurate floor-switch
    hint instead of `_NAV_HINT`'s hardcoded one.
    """
    return (f"[↑↓←→] move  [n/p] day  {_floor_switch_hint(floor_tokens)}"
            f"[q] quit")

#: Splits on an SGR colour code while keeping it in the result, so
#: `_clip_visible` below can pass every code through untouched regardless
#: of where the visible-character cut falls.
_ANSI_TOKEN_RE = re.compile(r"(\x1b\[[0-9;]*m)")


def _clip_visible(text, width):
    """`text`, truncated to at most `width` *visible* characters -- any
    embedded SGR colour code (`out.fmt_desk`/`out.good`/etc. wrap whole
    segments in one) is copied through whole no matter where the cut
    falls, so a clip can never leave a bare/half `\\x1b[1m` bleeding into
    whatever follows. `width <= 0` clips everything to nothing."""
    if width <= 0:
        return ""
    pieces = []
    seen = 0
    for token in _ANSI_TOKEN_RE.split(text):
        if _ANSI_TOKEN_RE.fullmatch(token):
            pieces.append(token)
            continue
        remaining = width - seen
        if remaining <= 0:
            continue
        pieces.append(token[:remaining])
        seen += min(len(token), remaining)
    return "".join(pieces)


def _right_justify_hint(out, left, hint, columns):
    """`left` and `hint`, laid out on one line with `hint` anchored to the
    right edge of a `columns`-wide terminal, `left` clipped with a
    trailing ellipsis when the two don't both fit. Clipping (not letting
    the terminal wrap `left`) is what keeps the status line to exactly
    one row -- a wrapped second row lands on top of the grid otherwise.

    `columns is None` (no known width) skips justify/clip entirely.
    Targets `columns - 1`, not the full width: filling the terminal's
    last column exactly is a known autowrap/erase landmine that clipped
    the hint's own last character ("quit" -> "qui").
    """
    if columns is None:
        return f"{left}  {hint}"
    columns = max(columns - 1, 1)
    hint_w = visible_len(hint)
    budget = columns - hint_w - 1          # >=1 space before the hint
    if budget < 1:
        return _clip_visible(hint, columns)   # pathological: hint alone
                                              # doesn't fit
    if visible_len(left) <= budget:
        pad = columns - visible_len(left) - hint_w
        return f"{left}{' ' * pad}{hint}"
    # A clip can end mid-colour (`left` may carry `out.fmt_desk`'s own SGR
    # codes) -- reset before the ellipsis so it can't bleed into `hint`.
    # Only when `out.color` is on: `--no-color`/piped output never has an
    # open code to close, and a bare reset byte in plain text is exactly
    # the leak this module's colour handling elsewhere avoids.
    reset = "\x1b[0m" if out.color else ""
    return f"{_clip_visible(left, budget - 1)}…{reset} {hint}"


def _status_line(out, cursor_key, kind, message, occupant=None, columns=None,
                 nav_hint=_NAV_HINT):
    """`occupant`, when given, names who has a "booked", "team" or "match"
    desk (just the name, not "booked by X" -- the glyph already says
    that). No `occupant` yet (fetch in flight) falls back to plain
    "unavailable" for all three kinds. `columns` right-justifies the
    keyboard hints (see `_right_justify_hint`); `None` falls back to
    plain concatenation. `nav_hint` is `_run_live`'s per-session hint
    (`_nav_hint_for`) -- the `_NAV_HINT` default is only for a direct/
    test call with no real floor list to build one from."""
    if message is not None:
        return message
    label = out.fmt_desk(cursor_key)
    if kind == "yours":
        left, hint = f"{label} yours", f"[Enter] release  {nav_hint}"
    elif kind == "free":
        left, hint = f"{label} free", f"[Enter] book  {nav_hint}"
    elif kind in ("booked", "team", "match") and occupant:
        left, hint = f"{label} -- {occupant}", nav_hint
    else:
        left, hint = f"{label} unavailable", nav_hint
    return _right_justify_hint(out, left, hint, columns)


def _confirm_line(out, action, key, target_date, columns=None):
    label = out.fmt_desk(key)
    date_label = out.fmt_date(target_date)
    if action == "release":
        left = f"Release {label} booked for {date_label}?"
    else:
        left = f"Book {label} for {date_label}?"
    return _right_justify_hint(out, left, "[Enter] confirm  [Esc] cancel",
                               columns)


def _do_write(ctx, action, bkid, key, planid, target_date, book_day_start):
    """Execute one Enter-confirmed CREATE/REPLACE/RELEASE against
    `ctx.api`, reusing the exact call shapes `book_cmd._creator`/
    `_updater` and `release_cmd._releaser` make -- never redone. Returns
    `(wrote, text)`: `wrote` is `True` only for a REAL write that changed
    server state (so the caller knows to refresh), `text` is the
    status-line message to show, using the same `out.good`/`out.danger`
    glyph convention `plan.execute()` uses for a written row.
    """
    out, api, catalog, cfg = ctx.out, ctx.api, ctx.catalog, ctx.config
    label = out.fmt_desk(key)

    if action in ("create", "update"):
        block = cfg.classify_booking_date(target_date, out.today, out.now)
        if block != "ok":
            return False, f" {out.danger('✗')} {label}  {BOOKING_BLOCK_REASONS[block]}"

    desk = catalog.desk_by_key(key)
    cid = desk.cid if desk else None
    try:
        if action == "release":
            api.booking_release(bkid)
            return True, f" {out.good('✗')} {label} released"
        if action == "create":
            api.booking_create(book_start_for(target_date, book_day_start),
                               key, cid, day=target_date)
        else:
            api.booking_update(bkid, key, cid, day=target_date)
        return True, f" {out.good('✓')} {label}"
    except FsError as e:
        hint = getattr(e, "hint", None)
        text = f" {out.danger('✗')} {label}  {e}"
        if hint:
            text += f"\n     {hint}"
        return False, text


def _refresh(ctx, planid, target_date, team_uids=frozenset(),
            match_uids=frozenset()):
    """Re-fetch own bookings and live availability after a successful
    write, and re-render. Returns `(grid, highlight_positions,
    desk_kind_positions, desk_positions, bookings, desk_states)`; the
    caller keeps `cursor_key` and re-resolves it against the new
    `desk_positions`. `team_uids`/`match_uids` are passed through, not
    re-resolved -- `_run_live` resolves them once on the way in, same as
    `group_keys`."""
    out, api, catalog = ctx.out, ctx.api, ctx.catalog
    bookings = own_bookings(api, out.today, catalog=catalog)
    covering_keys = _covering_desk_keys(bookings, target_date)
    highlight_keys = {k for k in covering_keys
                      if (catalog.desk_by_key(k) or _NO_DESK).planid == planid}
    desk_states = catalog.availability(target_date, planids=[planid])
    polys = catalog.deskpolys(planid)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == planid}
    img_w, img_h = catalog.floor_image_size(planid)
    grid, highlight_positions, desk_kind_positions, desk_positions = \
        render_floor(planid, polys, catalog_keys, img_w, img_h,
                    highlight_keys=highlight_keys, desk_states=desk_states,
                    team_uids=team_uids, match_uids=match_uids)
    return (grid, highlight_positions, desk_kind_positions, desk_positions,
           bookings, desk_states)


def _spawn_thread(target, args):
    """Default `spawn` for `_run_live`'s occupant-name fetch -- a real
    background thread. Tests inject a synchronous stand-in so a fetch's
    result is deterministic instead of depending on thread timing."""
    threading.Thread(target=target, args=args, daemon=True).start()


def _fetch_occupant_if_needed(api, own_uid, desk_states, key, kind,
                              names, pending, spawn, catalog=None):
    """Kick off (never block on) the one `/user` lookup a "booked", "team"
    or "match" desk's status line wants. `names` is the `uid -> display
    name` cache `_status_line` reads from; `pending` is the uids already
    in flight, so re-visiting the same desk twice doesn't stack duplicate
    lookups. `desk_states=None` (feature not opted into) is a no-op.

    `catalog`, when given, is checked synchronously first -- a local
    `cache.json` read, not a network round trip -- so a hit lands in
    `names` in time for the frame about to draw. Only a genuine miss
    falls through to the background `/user` fetch.
    """
    if desk_states is None or kind not in ("booked", "team", "match"):
        return
    state = desk_states.get(key)
    uid = state.uid if state is not None else None
    if not uid or uid in names or uid in pending:
        return
    if catalog is not None:
        cached = catalog.cached_user_name(uid)
        if cached:
            names[uid] = cached
            return
    pending.add(uid)

    def _fetch(uid=uid, bkid=state.bkid):
        try:
            occupant_name(api, own_uid, uid, bkid, names, catalog)
        finally:
            pending.discard(uid)

    spawn(_fetch, ())


def _prefetch_occupants(api, own_uid, desk_states, desk_kind_positions,
                        desk_positions, cursor_key, cursor_pos, names,
                        pending, spawn, catalog=None):
    """Fetch the cursor's own desk AND its four `nearest_desk` neighbours
    -- fetching only the cursor's desk means a lookup only starts on
    arrival, too late to show before the next keypress; pre-fetching
    neighbours means it's usually already in flight by the time an arrow
    key lands there. `catalog` lets a name resolved on an earlier run
    show up on this run's very first frame."""
    kind = desk_kind_positions.get(cursor_pos)
    _fetch_occupant_if_needed(api, own_uid, desk_states, cursor_key, kind,
                              names, pending, spawn, catalog)
    for direction in ("up", "down", "left", "right"):
        neighbour_key = nearest_desk(cursor_pos, desk_positions, direction)
        if neighbour_key is None:
            continue
        neighbour_kind = desk_kind_positions.get(desk_positions[neighbour_key])
        _fetch_occupant_if_needed(api, own_uid, desk_states, neighbour_key,
                                  neighbour_kind, names, pending, spawn,
                                  catalog)


def _occupant_api(ctx):
    """The `Api` the background occupant-name fetch calls through --
    deliberately NOT `ctx.api`. `ctx.api`'s `Session` re-logs-in on a
    dead session by default, which from a background thread while the
    main thread owns the tty in cbreak mode is a real hang/corruption
    risk. Builds a second `Session` on the same cookie store but with
    `allow_login=False`, so a dead session raises instead of prompting
    (already swallowed by `occupant_name`'s `except Exception`). Falls
    back to `ctx.api` when `ctx` has no `.session` at all (every test
    double here)."""
    session = getattr(ctx, "session", None)
    if session is None:
        return ctx.api
    bg_session = Session(session.config, session.store,
                         on_message=lambda *_a, **_kw: None,
                         allow_login=False, origin=session.origin)
    return LiveApi(bg_session)


def _run_live(ctx, planid, target_date, grid, highlight_positions,
              desk_kind_positions, desk_positions, columns,
              stdin=None, read_key=None, initial_cursor_key=None,
              desk_states=None, spawn=None, group_keys=(), rows=None,
              team_uids=frozenset(), match_uids=frozenset(), term_size=None,
              team_label=None, match_label=None, bookings=None):
    """The redraw loop behind arrow-key navigation and book/release from
    `fs map`'s live view. Kept thin: which desk an arrow lands on, what
    Enter means, is delegated to `nearest_desk`/`plan_for_cursor` -- this
    only sequences keypresses, draws frames, and calls
    `_do_write`/`_refresh` around a confirm gesture.

    Each frame goes into the alternate screen buffer (entered/left once)
    with cursor-home + per-line clear, not a full-screen clear, to avoid
    flicker. Alt-screen entry also hides the real terminal cursor
    (`\\x1b[?25l`), restored on the way out.

    `desk_states` lets the status line say who has a "booked" desk --
    see `_prefetch_occupants` and `_occupant_api` (a second,
    `allow_login=False` session, so a dead session can't trigger an
    interactive relogin prompt from a background thread while this loop
    owns the tty). Optional/`None`-safe.

    `read_key`/`stdin`/`initial_cursor_key`/`spawn` are all injectable so
    tests can drive this with scripted keys and a synchronous fetch
    instead of a real terminal.

    Digit keys switch floor; PgUp/PgDn (or `n`/`p`, easier one-handed)
    step the date -- both via `_refresh(ctx, planid, target_date)`, then
    the cursor is re-picked since the old key may not exist in the new
    `desk_positions`. PgDn/`n` is clamped at `out.today`; no clamp going
    forward -- the server governs the booking window, not this loop.

    Every frame ends with `\\x1b[J` (erase to end of screen), not just
    each line's `\\x1b[K` -- needed because a floor switch to a smaller
    map leaves a taller previous frame's extra rows behind otherwise.

    A `("mouse", button, col, row, pressed)` key moves the cursor to the
    desk under a qualifying left-button press via `desk_at_click`;
    anything else is a no-op.

    `team_label`/`match_label` are `cmd_map`'s real `show_team_on_map`
    name / typed match target -- passed through to `header_line`'s
    embedded legend on every frame, never re-derived here.

    `bookings`, when given, is `cmd_map`'s own already-fetched
    `own_bookings` result -- skips this call's own otherwise-redundant
    re-fetch (`own_bookings` calls `booking-list` AND `booking-summary`,
    so a second fetch moments after `cmd_map`'s is a full extra round
    trip on every live-view entry for data that cannot have changed in
    between). `None` (every direct/test call) still fetches it here.
    """
    out, api, catalog = ctx.out, ctx.api, ctx.catalog
    bg_api = _occupant_api(ctx)
    stdin = stdin if stdin is not None else sys.stdin
    read_key = read_key if read_key is not None else keyread.read_key
    spawn = spawn if spawn is not None else _spawn_thread
    cursor_key = (initial_cursor_key if initial_cursor_key is not None
                 else _initial_cursor_key(grid, desk_positions,
                                          desk_kind_positions,
                                          highlight_positions, columns,
                                          group_keys=group_keys))
    # This loop's own copy of `cmd_map`'s floor names/aliases --
    # `catalog.desks()` is cached, so re-deriving costs nothing and keeps
    # this independently correct for tests calling it directly.
    floor_labels = floor_names(catalog)
    floor_tokens = floor_aliases(floor_labels)
    # Built once, not per frame: `floor_tokens` is fixed for the life of
    # this call, and it's what makes the status line's floor-switch hint
    # match the actual floors on offer instead of `_NAV_HINT`'s hardcoded
    # `5`/`6`.
    nav_hint = _nav_hint_for(floor_tokens)
    book_day_start = catalog.book_day_start_mins()
    bookings = (own_bookings(api, out.today, catalog=catalog)
               if bookings is None else bookings)
    try:
        own_uid = catalog.own_uid()
    except Exception:                             # noqa: BLE001
        # A name is a nicety; must not take the whole live view down.
        own_uid = None
    names = {}              # uid -> display name, filled in by background fetches
    pending = set()          # uids with a fetch already in flight
    confirming = None      # (action, bkid) while the confirm line is up
    message = None         # last write's result line, shown once
    last_lines = None       # the most recent frame, reprinted once on exit

    # Also hides the real terminal cursor, restored in the `finally` below.
    out.prompt("\x1b[?1049h\x1b[?1000h\x1b[?1006h\x1b[?25l")
    # Injectable so tests keep fixed columns/rows; the real call site
    # re-queries the terminal every frame so a resize takes effect.
    term_size = term_size or (lambda: (columns, rows))
    # Persisted across frames so `scroll_window` can tell "still inside
    # the window" from "just crossed the margin" -- see its docstring.
    col_window = None
    row_window = None
    try:
        while True:
            columns, rows = term_size()
            cursor_pos = desk_positions[cursor_key]
            n_cols = len(grid[0]) if grid else 0
            col_window = scroll_window(col_window, cursor_pos[1], n_cols, columns,
                                       margin=SCROLL_MARGIN_COLS)
            left, right = col_window if col_window else (0, n_cols)

            # Vertical counterpart to the column scroll above --
            # `HEADER_LINES` + 1 (status/confirm line) is how many rows
            # the grid doesn't get to use.
            n_rows = len(grid)
            grid_rows_budget = (rows - HEADER_LINES - 1) if rows else None
            row_window = scroll_window(row_window, cursor_pos[0], n_rows,
                                       grid_rows_budget)
            top, bottom = row_window if row_window else (0, n_rows)

            pos_to_key = {pos: key for key, pos in desk_positions.items()}

            kind = desk_kind_positions.get(cursor_pos)
            _prefetch_occupants(bg_api, own_uid, desk_states,
                                desk_kind_positions, desk_positions,
                                cursor_key, cursor_pos, names, pending, spawn,
                                catalog)

            lines = [header_line(out, planid, target_date, floor_labels,
                                 columns, team_label=team_label,
                                 match_label=match_label)]
            lines += _render_lines(out, grid, desk_kind_positions, left, right,
                                   cursor_pos=cursor_pos, top=top, bottom=bottom)
            if confirming is not None:
                action, _bkid = confirming
                lines.append(_confirm_line(out, action, cursor_key,
                                           target_date, columns))
            else:
                state = desk_states.get(cursor_key) if desk_states else None
                occupant = names.get(state.uid) if state else None
                lines.append(_status_line(out, cursor_key, kind, message,
                                          occupant, columns,
                                          nav_hint=nav_hint))
            message = None
            last_lines = lines

            out.prompt("\x1b[H")
            out.print("\n".join(line + "\x1b[K" for line in lines))
            out.prompt("\x1b[J")     # see docstring: erases a taller PREVIOUS
                                     # frame's leftover rows below this one

            key = read_key(stdin)
            if key is None or key in ("q", "esc"):
                return ExitCode.OK

            if confirming is not None:
                action, bkid = confirming
                confirming = None
                if key != "enter":
                    continue      # any other key just cancels -- no move
                wrote, message = _do_write(ctx, action, bkid, cursor_key,
                                           planid, target_date, book_day_start)
                if wrote:
                    (grid, highlight_positions, desk_kind_positions,
                     desk_positions, bookings, desk_states) = _refresh(
                        ctx, planid, target_date, team_uids, match_uids)
                continue

            if key in ("up", "down", "left", "right"):
                candidate = nearest_desk(cursor_pos, desk_positions, key)
                if candidate is not None:
                    cursor_key = candidate
                continue

            if key == "enter":
                current = booking_for_date(bookings, target_date)
                decision = plan_for_cursor(kind, cursor_key, current)
                if decision is None:
                    continue
                action, bkid = decision
                if ctx.args.yes:
                    wrote, message = _do_write(ctx, action, bkid, cursor_key,
                                               planid, target_date,
                                               book_day_start)
                    if wrote:
                        (grid, highlight_positions, desk_kind_positions,
                         desk_positions, bookings, desk_states) = _refresh(
                            ctx, planid, target_date, team_uids, match_uids)
                else:
                    confirming = (action, bkid)
                continue

            if isinstance(key, str) and key.isdigit():
                new_planid = floor_tokens.get(key)
                if new_planid is not None and new_planid != planid:
                    planid = new_planid
                    # A floor switch re-fetches deskpolys/availability, and
                    # was otherwise a silent pause on the OLD floor's frame.
                    out.prompt("\x1b[H")
                    loading = lines[:-1] + [" Loading floor..."]
                    out.print("\n".join(line + "\x1b[K" for line in loading))
                    out.prompt("\x1b[J")
                    (grid, highlight_positions, desk_kind_positions,
                     desk_positions, bookings, desk_states) = _refresh(
                        ctx, planid, target_date, team_uids, match_uids)
                    cursor_key = _initial_cursor_key(
                        grid, desk_positions, desk_kind_positions,
                        highlight_positions, columns, group_keys=group_keys)
                continue

            if key in ("pgup", "pgdn", "n", "p"):
                forward = key in ("pgdn", "n")
                new_date = target_date + dt.timedelta(days=1 if forward else -1)
                if new_date < out.today:
                    continue        # PgDn/n-past-today clamp -- see docstring
                out.prompt("\x1b[H")
                loading = lines[:-1] + [f" Loading {out.fmt_date(new_date)}..."]
                out.print("\n".join(line + "\x1b[K" for line in loading))
                out.prompt("\x1b[J")
                target_date = new_date
                (grid, highlight_positions, desk_kind_positions,
                 desk_positions, bookings, desk_states) = _refresh(
                    ctx, planid, target_date, team_uids, match_uids)
                # Floor unchanged, so `desk_positions` is the same layout
                # -- keep the cursor on the same desk, only re-pick if it
                # genuinely isn't there any more (defensive).
                if cursor_key not in desk_positions:
                    cursor_key = _initial_cursor_key(
                        grid, desk_positions, desk_kind_positions,
                        highlight_positions, columns, group_keys=group_keys)
                continue

            if isinstance(key, tuple) and key[0] == "mouse":
                _tag, button, col, row, pressed = key
                if pressed and (button & 3) == 0 and not (button & 32):
                    candidate = desk_at_click(col, row, left, pos_to_key,
                                              top=top)
                    if candidate is not None:
                        cursor_key = candidate
                continue
            # any other key: browse-mode no-op, redraw and read again
    except KeyboardInterrupt:
        return ExitCode.OK
    finally:
        out.prompt("\x1b[?25h\x1b[?1000l\x1b[?1006l\x1b[?1049l")
        # Leaving the alt screen restores the primary buffer, wiping the
        # map -- reprint the last frame plainly into normal scrollback.
        if last_lines is not None:
            out.print("\n".join(last_lines))
