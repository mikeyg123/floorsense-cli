"""`fs map [<floor>] [<date>]` -- static per-floor ASCII/Unicode desk map,
own-desk highlight, terminal-width crop, live per-desk status colour,
arrow-key cursor navigation and book/release from the map. All three
phases of `docs/floorplan-map-manual.md`'s roadmap are shipped in this
module.

Everything but the per-workplace map data lives in this one module --
grammar, floor/date resolution, rendering, and crop/highlight -- rather
than the thin-command-plus-top-level-module split every other command
follows. Nothing else in the codebase will ever call this rendering
logic, so the split would buy no reuse, only an extra file to navigate.

The hand-traced constant tables (outer wall, partitions, room boxes,
zone names, planids) live in `..floorplans` instead, one module per
workplace -- see that package's docstring for why, and
`docs/floorplan-map-manual.md` for how each table is derived. This
module imports one such module directly; nothing here should be
building-specific.

The rendering pure functions below are ported near-verbatim from
`experiments/floorplan_ascii.py` (gitignored scratch prototype, signed
off as visually correct for both floors) -- `docs/floorplan-map-manual.md`
records the six wrong-turn/fix pairs this approach depends on; read it
before changing any function here.
"""

import datetime as dt
import shutil
import sys
import threading

from .. import keyread
from ..api import LiveApi, book_start_for
from ..args import reject_forced
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
from ..session import Session
from .at_cmd import occupant_name
from .book_cmd import booking_for_date, bookings_for_date
from .list_cmd import own_bookings

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
    """`{planid: display name}` for every floor with desks, derived from
    each floor's own `Desk.floor` (the floorplan's real `name` field,
    e.g. "Level 5" -- see `Catalog.desks()`) rather than hand-picked per
    deployment. This is what makes `fs map` portable to a building with
    entirely different floor names/numbering: nothing here is specific to
    this workplace's two floors, it just reads whatever the account's own
    API responses say. First desk seen for a planid wins (a floor's name
    doesn't vary desk-to-desk); a desk with no floor name at all (`None`,
    shouldn't happen in practice -- every fixture and every real response
    seen so far carries one) is skipped rather than poisoning the entry
    with a placeholder.

    Accepted consequence of deriving rather than hardcoding: if a floor's
    `name` were ever genuinely absent, it contributes no alias at all, so
    `classify_token` would silently fall through and treat what should
    have been a floor token as a date instead (e.g. `fs map 5` becoming
    "the 5th of next month" instead of "Level 5") -- a behaviour the old
    hardcoded `FLOOR_ALIASES` dict couldn't produce, since it could never
    go empty. Accepted rather than guarded against because it has never
    been observed to happen; if it ever does, `classify_token`'s
    docstring is the place to revisit, not this one.
    """
    names = {}
    for desk in catalog.desks():
        if desk.floor and desk.planid not in names:
            names[desk.planid] = desk.floor
    return names


def floor_aliases(names):
    """`{token: planid}` for `classify_token`, derived from `floor_names`'s
    output. Each floor's name, lowercased with whitespace stripped, is
    always an alias (`"Level 5"` -> `"level5"`); if the name's last
    whitespace-separated word is a bare integer, that integer alone is
    also an alias (`"5"`) -- together these reproduce this workplace's
    exact `"5"`/`"level5"` pair while generalising to any floor name a
    different building happens to use. First floor wins on a collision
    (e.g. two towers both naming a
    floor "Level 5") -- rare enough within one building not to try to
    auto-disambiguate; see docs/floorplan-map-manual.md.
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
    "free": "▢", "restricted": "■", "booked": "■", "yours": "▣",
}


STYLE_METHOD_BY_KIND = {
    "free": "good", "restricted": "muted", "booked": "danger",
    "yours": "attention",
}


def desk_glyph_kind(desk_key, highlight_keys, desk_states):
    """One desk's live-status glyph kind -- "yours" (highlight always
    wins over live state, matching phases 1-2's own `▣`-vs-`▢`
    precedence extended to colour), else "free"/"restricted"/"booked"
    from `desk_states` (a `Catalog.availability()` result), or "free"
    uniformly when `desk_states` is `None` -- phases 1-2's exact
    behaviour, preserved for every call site that doesn't opt into
    live colour at all. A key absent from a *given* (non-`None`)
    `desk_states` dict defaults to "booked" -- the conservative
    unavailable case, sharing `GLYPH_BY_KIND`'s glyph and colour with
    a desk that's actually booked -- rather than "free", since a CLI
    that can't confirm a desk is bookable should not draw it as though
    it is.
    """
    if desk_key in highlight_keys:
        return "yours"
    if desk_states is None:
        return "free"
    state = desk_states.get(desk_key)
    if state is None or not state.free:
        return "booked"
    if not state.book_advance:
        return "restricted"
    return "free"


def desk_centers(polys, catalog_keys):
    """id -> (cx, cy, w, h) in image pixel space, for real bookable desks
    only. `polys` (from `Catalog.deskpolys()`) and `catalog_keys` (from
    `{d.key for d in catalog.desks() if d.planid == planid}`) don't
    always agree -- Level 6 has 3 poly entries with no catalog entry at
    all, geometry left over from the floorplan export with nothing
    bookable behind them (docs/floorplan-map-manual.md's "which polys
    are real desks" finding). Filtering happens here, once, rather than
    in every caller.
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
    """Sort a *small, local* set of values (one cluster's worth of desks,
    not the whole floor) into rank buckets 0, 1, 2, ... by gap. Safe here
    in a way the same trick wasn't globally (see `quantize_axis`'s
    docstring): with only a handful of values from one physical pod,
    there's no unrelated desk sitting in between two real groups to chain
    them together.

    Returns (id -> rank, number of ranks).
    """
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
    """The back-to-back desk depth -- the smallest side of the median
    desk -- used as the single scale unit for both axes (deriving scale
    from a target output width instead collapses back-to-back rows onto
    one character row; see docs/floorplan-map-manual.md's wrong-turn #1).
    """
    depths = sorted(min(w, h) for _, _, w, h in centers.values())
    return depths[len(depths) // 2]


def quantize_axis(values, grid):
    """Snaps every value independently to the nearest multiple of `grid`
    -- plain rounding to a fixed, absolute lattice, not a comparison
    against neighbours (docs/floorplan-map-manual.md's wrong-turn #2:
    chaining sorted values within a threshold silently bridges two
    genuinely distinct rows whenever an unrelated desk's coordinate sits
    between them). `grid` must stay well under the real column/row pitch.
    """
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
                  highlight_keys=frozenset(), desk_states=None):
    """Render one floor's desks, walls, partitions, room boxes and zone
    labels into a character grid. Ported from
    `experiments/floorplan_ascii.py`'s `main()` -- see
    docs/floorplan-map-manual.md for the six wrong-turn/fix pairs this
    approach depends on. `highlight_keys` marks which desk keys are the
    caller's own booked desk(s). `desk_states`, a `Catalog.availability()`
    result (or `None` for phases-1-2's plain behaviour -- see
    `desk_glyph_kind`'s docstring), decides each other desk's glyph via
    `desk_glyph_kind`/`GLYPH_BY_KIND`. This function stays colour-blind --
    it decides which of 3 glyphs to draw, never an ANSI code; the caller
    (`cmd_map`) turns the returned per-cell "kind" into an `Output`
    colour method.

    Returns `(grid, highlight_positions, desk_kind_positions,
    desk_positions)` -- `grid` a `list[list[str]]`, `highlight_positions`
    a `set[(row, col)]` of just the "yours" cells (unchanged from phases
    1-2 -- still what `cmd_map`'s crop math anchors on),
    `desk_kind_positions` a `dict[(row, col), str]` covering every
    drawn desk cell, one of "free"/"restricted"/"booked"/"yours",
    `desk_positions` a `dict[str, (row, col)]` mapping every drawn desk's
    key to that same cell -- the reverse of `desk_kind_positions`, built
    from the same per-block placement loop below so there's no second
    pass over the blocks. Added for phase 3b's cursor navigation
    (`nearest_desk`/`_run_live` in this module) -- `cmd_map` inverts it
    once into `position -> key` for O(1) "what's under the cursor"
    lookups, safe because no two desk keys ever share a position (each
    cluster member gets a distinct rank-based cell).
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

    # Reading order (top-to-bottom, left-to-right) -- `BLOCK_ADJUSTMENTS_BY_FLOOR`
    # keys off this numbering, so it must stay stable.
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
                kind = desk_glyph_kind(m, highlight_keys, desk_states)
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
    """The desk `desk_positions` places nearest `cursor_pos` in
    `direction` ("up"/"down"/"left"/"right"), by
    `abs(primary_delta) + 2 * abs(perpendicular_delta)` -- a standard 2D
    directional-focus heuristic (TV/console UI focus systems), not plain
    Euclidean distance, which would let a diagonal desk win over one
    directly ahead. Candidates are filtered to the correct half-plane
    first (`dc > 0` for "right", etc. -- strict, so a desk exactly on the
    perpendicular axis never counts). Returns `None` -- never raises --
    when nothing qualifies; the caller must treat that as a no-op, per
    the design spec's Movement section.
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
    to -- no tty, or `shutil.get_terminal_size()` failing/returning a
    nonsensical value. `None` here means "show the full uncropped map",
    per the design spec's crop rules -- the one case where the wrap risk
    is accepted, since there's no width to crop to.
    """
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
    """Both usable terminal dimensions -- `(columns, rows)` -- or
    `(None, None)` when there isn't a real terminal to size against.
    Same guards as `terminal_columns` (no tty, or `shutil.get_terminal_size()`
    failing/returning something nonsensical), extended to `rows` for the
    live view's vertical crop/scroll (`_run_live`'s `term_size`) -- unlike
    `terminal_columns`, which is also called once in `cmd_map` just to
    decide whether the live view is even reachable, this is only ever
    called from inside the live redraw loop itself, once per frame, so a
    resize mid-session is picked up on the very next keypress rather than
    needing its own signal handler."""
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
    grid to, or `None` for no crop at all (`terminal_columns` is `None`).

    Despite the column-flavoured names, the maths here is axis-agnostic --
    "how much of one dimension fits, keeping a required position in view,
    preferring the low end" applies just as well to rows. `_run_live`'s
    vertical scroll reuses this exact function for that axis: `n_cols` is
    then `len(grid)`, `highlight_cols` is `{cursor_pos[0]}`, and
    `terminal_columns` is the grid rows the terminal has room for above/
    below the header and status lines.

    No highlight -- anchored at the floor's own left edge (column 0),
    extending right, clipped at `terminal_columns`.

    With a highlight -- the right edge sits just past the rightmost
    highlighted column, but never narrower than the available terminal
    width when the floor has content to fill it: it's the larger of
    "just past the highlight" and "as much of `terminal_columns` as the
    floor has" (both clamped to the floor's own rightmost content,
    `n_cols`). Without that second term, a highlight near the left edge
    would collapse the window to a sliver hugging the highlight even on
    a terminal wide enough to show the whole floor. The right edge is
    decided first; the left edge is only ever clamped at 0 (the floor's
    own left edge) -- it is never pushed narrower than that, even if
    that leaves the window narrower than `terminal_columns`.
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
    """The stateful, axis-agnostic counterpart to `crop_window` that
    `_run_live` uses for its live cursor-following scroll.

    `crop_window` is recomputed from scratch every frame purely from the
    cursor's *current* position, which has two visible bugs: the window's
    far edge sits exactly one past the cursor with no run-way, so the
    cursor reads as pinned to the screen edge with the wall it's walking
    towards never in view; and because the window has no memory of where
    it was, moving the cursor even one cell back off that edge snaps the
    whole window back to its left/top anchor instead of staying put.

    `scroll_window` fixes both by taking `prev`, the window returned last
    frame (or `None` on the first frame / after a resize), and only
    moving it when `pos` gets within `margin` cells of an edge -- and
    then only far enough to restore that margin, not to recenter. A
    window that hasn't hit either margin doesn't move at all, which is
    what makes "don't scroll back until the cursor nears the opposite
    edge" fall out for free: the window has no reason to move.

    `n_total`/`terminal_size` are `crop_window`'s `n_cols`/`terminal_columns`
    under axis-neutral names -- same axis-agnostic contract (rows just as
    well as columns), same `None`/`<= 0` meaning "no crop at all".
    """
    if terminal_size is None or terminal_size <= 0:
        return None
    width = min(terminal_size, n_total)
    if prev is None or (prev[1] - prev[0]) != width:
        # First frame, or the terminal resized (window width no longer
        # matches) -- re-anchor around `pos` with margin run-way rather
        # than centring, so a resize doesn't itself cause a jump.
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


def header_line(out, planid, target_date, names):
    """`Level 5 -- Wed 27 Aug`, the live view's first line -- floor and
    date, so neither is only inferable from what's on screen. `names`
    (a `floor_names()` result) supplies the display label; `planid`
    falls back to its bare number if it's ever outside `names` (should
    never happen from `resolve_floor`, but this is display code, not a
    place to raise over it)."""
    level = names.get(planid, f"planid {planid}")
    return f"{level} -- {out.fmt_date(target_date)}"


def desk_at_click(term_col, term_row, left, pos_to_key,
                  header_lines=HEADER_LINES, top=0):
    """The desk key under a 1-based terminal `(term_col, term_row)` mouse
    click, or `None` if it lands off the grid or on empty space --
    inverts `_render_lines`'s own placement (`header_lines` rows of
    chrome above the grid, `top` grid rows AND `left` grid columns cropped
    off the front of every visible row/column) so a raw SGR mouse report
    (`keyread.read_key`'s `"mouse"` tuple) can be turned back into a
    `desk_positions` key. `pos_to_key` is the `(row, col) -> key` reverse
    of `desk_positions` for the frame the click was drawn against. Pure
    and separately testable, same spirit as `nearest_desk`."""
    grid_row = term_row - 1 - header_lines + top
    grid_col = (term_col - 1) + left
    if grid_row < 0 or grid_col < 0:
        return None
    return pos_to_key.get((grid_row, grid_col))


def classify_token(token, today, aliases):
    """A `map` positional token as `("date", date)`, `("floor", planid)`,
    or `None` if it's neither. `aliases` is a `floor_aliases()` result --
    this function has no opinion of its own on what a floor is called, so
    it works unchanged on any building's floor set.

    Floor wins over date, not the other way around -- the design spec's
    prose says "date-shaped wins over floor", but that's wrong whenever a
    floor alias happens to also be date-shaped: bare `"5"`/`"6"` (this
    workplace's two floors) ARE date-shaped, since `dates.parse_date`
    treats a lone 1-31 number as a day-of-month token
    (`parse_date("5", today)` returns the next 5th of a month, not
    `None`) -- confirmed by running it, not assumed from reading the
    regex. Date-first precedence would make `fs map 5` mean "the 5th of
    next month on the default floor", never "Level 5" -- the one
    grammar `fs map [<floor>] [<date>]`'s own two positionals promise.
    Checking `aliases` first (a collision can only happen for whatever
    tokens are actually IN it, so this can't misclassify any other date
    token) is the fix; everything else still falls through to
    `parse_date` unchanged, so `"mon"`, `"12"`, `"today"`, etc. are still
    dates whenever they aren't also a floor's own alias.
    """
    normalised = token.strip().lower()
    if normalised in aliases:
        return ("floor", aliases[normalised])
    date = parse_date(token, today)
    if date is not None:
        return ("date", date)
    return None


def _covering_desk_keys(bookings, target_date):
    """Own desk keys booked to cover `target_date`, out of `bookings`
    (already-fetched own bookings, `today`'s forward window). Plural --
    see `book_cmd.bookings_for_date`'s docstring: the one-desk-per-day
    limit is per group, so more than one covering desk is possible.
    """
    covering = bookings_for_date(bookings, target_date)
    return {b["key"] for b in covering if b.get("key")}


def resolve_floor(api, catalog, today, target_date, explicit_planid):
    """`(planid, highlight_keys)` per the design spec's "Floor resolution"
    section.

    `explicit_planid` given -- no fallback search; highlight applies only
    if a covering booking's desk is actually on that floor.

    `explicit_planid` omitted -- 1) a booking covering `target_date` sets
    both the floor and the highlight; 2) failing that, the next own
    booking from `today` onward sets the floor with NO highlight (it
    doesn't cover the requested date); 3) failing that, the catalog's
    own first floor (`catalog.planids()[0]`), no highlight -- NOT the
    `DEFAULT_PLANID` constant, which is only this workplace's Level 5 and
    would be meaningless (or simply wrong) on a different building's
    catalog. `DEFAULT_PLANID` is the last-last resort, for the genuinely
    empty case where `catalog.planids()` itself has nothing to offer.

    Fetches `own_bookings` exactly once (a single `floorplan_booking`
    round-trip) and reuses it for both the covering-booking check and the
    next-booking fallback.
    """
    bookings = own_bookings(api, today)
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
    """A never-matching stand-in for a `desk_by_key` miss, so the
    `.planid` comparisons above don't need a `None`-check at each call
    site -- a stale desk key (e.g. from a config edit) resolves to "not
    on any floor" rather than crashing.
    """
    planid = object()


_NO_DESK = _NoDesk()


def plan_for_cursor(kind, key, current):
    """The CREATE/REPLACE/RELEASE/no-op decision for Enter on the desk
    under the cursor -- pure, kept separate from `_run_live`'s actual
    `api.booking_*` call per the design spec's module-layout section.
    `current` is `book_cmd.booking_for_date`'s result for `target_date`
    (any own booking covering that date, in any group -- the same
    "current" `fs book`'s rank table uses), already fetched once by the
    caller, not re-derived here. `key` isn't read by the decision itself
    (only `kind`/`current` are) but is accepted for symmetry with the
    caller's other per-desk lookups and so a future refinement doesn't
    need a signature change.

    Returns `("release", bkid)`, `("create", None)`, `("replace", bkid)`,
    or `None` for the restricted/booked no-op. The "yours, and it's
    already this desk" case the design spec calls unreachable (that desk
    would render "yours", not "free", so `plan_for_cursor` is never
    called with `kind="free"` and a `current` on the SAME desk) isn't
    handled defensively here for the same reason.
    """
    if kind == "yours":
        return ("release", current["bkid"])
    if kind == "free":
        if current is None:
            return ("create", None)
        return ("replace", current["bkid"])
    return None


def cmd_map(ctx, stdin=None):
    """`fs map [<floor>] [<date>]` -- see the module docstring.

    Cost, stated rather than assumed (`at_cmd.py`'s habit): on a cold
    cache this is up to three cached `floorplan_booking` calls --
    `catalog.desks()` fetches every non-empty floor once each (2 on
    this deployment) to resolve `catalog_keys`, plus one more for
    `catalog.deskpolys(planid)` on the resolved floor. All three are
    cached at `DESK_TTL_S` (30 days), so only the first `fs map` run
    after a cold cache pays this. Separately, two more calls are
    uncached every run regardless of cache state: `resolve_floor`'s
    own-bookings round-trip, and `catalog.availability(target_date,
    planids=[planid])` for live per-desk status colour -- permission-
    sensitive data that can't share `deskpolys()`'s cached fetch even
    though both hit the same endpoint for the same floor/date (see
    `catalog.py`'s identity-vs-permission caching split). Worst case
    (cold cache): 5 total `floorplan_booking` calls. Warm cache (the
    common case): 2.
    """
    out, api, catalog = ctx.out, ctx.api, ctx.catalog

    if ctx.args.json:
        raise UsageError(
            "fs map does not support --json",
            hint="There is no structured equivalent of an ASCII map.")
    reject_forced(ctx.args, "fs map")

    tokens = list(ctx.args.args)
    if len(tokens) > 2:
        raise UsageError(
            "fs map takes at most a floor and a date",
            hint="e.g. `fs map`, `fs map mon`, `fs map 6`, `fs map 6 mon`.")

    # This module's own docstring quotes the worst case as 5 uncached
    # `floorplan_booking` calls -- a first-run cold cache can take a
    # perceptible moment with nothing on screen yet. Printed the same
    # unconditional way `cli.py`'s "Looking up your organisation's Okta
    # host..." is (not tty-gated, not erased after) -- the live view's
    # alt-screen entry (`_run_live`) hides it the instant the first frame
    # draws; the static path (`--no-nav`, a non-tty) just leaves it as the
    # line above the map, same as every other "fetching..." message here.
    # Printed BEFORE token classification, not after, because classifying
    # any floor token now needs `floor_names(catalog)` -- the same cold-
    # cache `catalog.desks()` fetch `resolve_floor` needs a few lines down
    # -- so the expensive part can start as early as `fs map 6` itself,
    # not just at `resolve_floor`.
    out.print("Loading floor...")

    explicit_planid = None
    explicit_date = None
    if tokens:
        # `names`/`aliases` are local to this block -- `_run_live` derives
        # its own copy (`floor_labels`/`floor_tokens`) from the same
        # cached `catalog.desks()` call, so there's no need to thread
        # these further. Guarded on `tokens` so bare `fs map` (the common
        # case) never pays for a derivation it has no tokens to classify.
        names = floor_names(catalog)
        aliases = floor_aliases(names)
        for token in tokens:
            classified = classify_token(token, out.today, aliases)
            if classified is None:
                raise UsageError(
                    f"fs map doesn't understand {token!r}",
                    hint=(f"A floor ({', '.join(sorted(names.values()))}) "
                         "or a date." if names else "A floor or a date."))
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

    planid, highlight_keys = resolve_floor(api, catalog, out.today,
                                           target_date, explicit_planid)

    polys = catalog.deskpolys(planid)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == planid}
    img_w, img_h = catalog.floor_image_size(planid)

    desk_states = catalog.availability(target_date, planids=[planid])

    grid, highlight_positions, desk_kind_positions, desk_positions = render_floor(
        planid, polys, catalog_keys, img_w, img_h,
        highlight_keys=highlight_keys, desk_states=desk_states)

    columns, rows = terminal_size(sys.stdout)
    stdin = stdin if stdin is not None else sys.stdin
    live = (not ctx.args.no_nav and columns is not None
           and keyread.capable(stdin) is not False)
    if live:
        # The configured default group, in its preference order, resolved
        # to real catalog keys the same tolerant way `fs book`/`fs at` do
        # (`resolve_group_keys` -- a stale config entry warns and drops,
        # it doesn't take the map down) -- `_initial_cursor_key`'s second
        # choice, after "yours", before the plain top-leftmost fallback.
        group_keys = resolve_group_keys(
            ctx.config.groups.get(ctx.config.default_group, []),
            [d.key for d in catalog.desks()], out)
        # Re-queries the real terminal every frame (`_run_live`'s
        # `term_size` default just replays whatever was measured here
        # once, on the way in) -- this is what makes a resize mid-session
        # take effect on the very next redraw instead of leaving every
        # subsequent frame cropped to stale dimensions.
        return _run_live(ctx, planid, target_date, grid, highlight_positions,
                         desk_kind_positions, desk_positions, columns,
                         stdin=stdin, desk_states=desk_states,
                         group_keys=group_keys, rows=rows,
                         term_size=lambda: terminal_size(sys.stdout))
    return _run_static(out, grid, highlight_positions, desk_kind_positions,
                       columns)


def _render_lines(out, grid, desk_kind_positions, left, right,
                  cursor_pos=None, top=0, bottom=None):
    """The coloured, cropped text lines for one frame -- shared by
    `_run_static`'s single print and `_run_live`'s every-keypress
    redraw, so the two never drift on how a "kind" becomes an `Output`
    colour method.

    `cursor_pos`, when given (only `_run_live` has one), gets `out.reverse`
    AND `out.blink` AND `out.bold` layered on top of its cell's own kind
    colour -- reverse alone is indistinguishable from another desk of the
    same kind at a glance, so all three stack to keep the cursor visible
    among a screenful of colour. `"yours"` desks are bolded the same way
    even when they're not the cursor, so your own bookings are also
    findable at a glance rather than blending into every other
    `attention`-coloured cell. Not used by `_run_static`, which has no
    cursor at all.

    `top`/`bottom` (default: the whole grid, `_run_static`'s only need)
    are `_run_live`'s vertical-scroll counterpart to `left`/`right` -- a
    half-open ROW window, sliced the same way, so a floor taller than the
    terminal draws only the rows currently in view. `pos` is still built
    from the ABSOLUTE row index (`r`, not an index into a pre-sliced
    sub-list), same reasoning as `left` already gets for columns --
    `desk_kind_positions`/`cursor_pos` are keyed by the grid's real
    coordinates regardless of what's currently cropped into view.
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


def _run_static(out, grid, highlight_positions, desk_kind_positions, columns):
    """Phases 1-2/3a's exact, unchanged behaviour: one crop, one print,
    one legend line, exit. This is what every non-capable terminal (and
    `--no-nav`) still gets."""
    n_cols = len(grid[0]) if grid else 0
    highlight_cols = {c for _r, c in highlight_positions}
    window = crop_window(n_cols, highlight_cols, columns)
    left, right = window if window else (0, n_cols)

    out.print("\n".join(_render_lines(out, grid, desk_kind_positions, left, right)))
    out.print(f"{out.good('▢')} free   {out.danger('■')} unavailable   "
              f"{out.attention('▣')} yours")
    return ExitCode.OK


def _initial_cursor_key(grid, desk_positions, desk_kind_positions,
                        highlight_positions, columns, group_keys=()):
    """Where the cursor starts, in order:

      1. The first (lowest-sorting) `"yours"` desk key, if there is one --
         a booking for this date on this floor.
      2. Failing that, the first desk in `group_keys` (the configured
         default group, in its preference order -- same order `fs book`
         ranks against) that's both on THIS floor's `desk_positions` and
         currently `"free"`. A group desk elsewhere, or one that's booked/
         restricted here, is skipped rather than landed on -- starting on
         a desk you can't actually book is more confusing than starting
         somewhere plain.
      3. Failing that too (no group configured, none of it on this floor,
         or none of it free), the reading-order-first desk actually
         visible in the initial static crop window -- the original
         fallback, kept as the last resort rather than replaced.

    See the design spec's "Movement" section, "Initial cursor position".
    """
    yours_keys = [key for key, pos in desk_positions.items()
                 if desk_kind_positions.get(pos) == "yours"]
    if yours_keys:
        return min(yours_keys)

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
#: Floor keys are hardcoded to `5`/`6` rather than derived from
#: `floor_aliases()` -- that function's output also holds each floor's
#: full-name spelling (`"level5"`), which isn't a live-view digit key at
#: all, and digit-key switching can only ever reach a floor whose alias
#: happens to be a single bare digit (see the digit-key handler below).
#: This hint is accurate for THIS workplace's two floors; a building with
#: different floor names/numbers may show a hint that doesn't match what
#: actually works via digit keys.
_NAV_HINT = "[↑↓←→] move  [n/p] day  [5/6] floor  [q] quit"


def _status_line(out, cursor_key, kind, message, occupant=None):
    """`occupant`, when given (only `_run_live` has one, and only once its
    background fetch has landed -- see `_spawn_occupant_lookup`), names who
    has a "booked" desk instead of the bare "unavailable" every other
    non-free, non-yours kind still gets -- as just the name (`{label} --
    {occupant}`), not "booked by {occupant}": the desk's colour/glyph
    already says "booked", so the line only needs to add WHO. `None` (no
    fetch yet, no uid to look up, or `desk_states` wasn't threaded through
    at all) keeps the "unavailable" wording, so a caller that doesn't opt
    into the feature sees no change."""
    if message is not None:
        return message
    label = out.fmt_desk(cursor_key)
    if kind == "yours":
        return f"{label} yours -- [Enter] release  {_NAV_HINT}"
    if kind == "free":
        return f"{label} free -- [Enter] book  {_NAV_HINT}"
    if kind == "booked" and occupant:
        return f"{label} -- {occupant} -- {_NAV_HINT}"
    return f"{label} unavailable -- {_NAV_HINT}"


def _confirm_line(out, action, key, target_date):
    label = out.fmt_desk(key)
    date_label = out.fmt_date(target_date)
    if action == "release":
        return (f"Release {label} booked for {date_label}? "
               f"[Enter] confirm  [Esc] cancel")
    return f"Book {label} for {date_label}? [Enter] confirm  [Esc] cancel"


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


def _refresh(ctx, planid, target_date):
    """Re-fetch own bookings and live availability after a successful
    write, and re-render -- see the design spec's Post-action refresh
    section. Returns `(grid, highlight_positions, desk_kind_positions,
    desk_positions, bookings, desk_states)`; the caller keeps its own
    `cursor_key` unchanged and re-resolves its position through the new
    `desk_positions` on the next loop iteration. `desk_states` is returned
    too (not just consumed here) so the occupant-name background fetch
    keeps reading live `uid`s after a write, not a stale pre-write set."""
    out, api, catalog = ctx.out, ctx.api, ctx.catalog
    bookings = own_bookings(api, out.today)
    covering_keys = _covering_desk_keys(bookings, target_date)
    highlight_keys = {k for k in covering_keys
                      if (catalog.desk_by_key(k) or _NO_DESK).planid == planid}
    desk_states = catalog.availability(target_date, planids=[planid])
    polys = catalog.deskpolys(planid)
    catalog_keys = {d.key for d in catalog.desks() if d.planid == planid}
    img_w, img_h = catalog.floor_image_size(planid)
    grid, highlight_positions, desk_kind_positions, desk_positions = \
        render_floor(planid, polys, catalog_keys, img_w, img_h,
                    highlight_keys=highlight_keys, desk_states=desk_states)
    return (grid, highlight_positions, desk_kind_positions, desk_positions,
           bookings, desk_states)


def _spawn_thread(target, args):
    """Default `spawn` for `_run_live`'s occupant-name fetch -- a real
    background thread. Tests inject a synchronous stand-in (same
    injectable-dependency pattern as `read_key`/`stdin`) so a fetch's
    result is deterministically visible in one scripted frame instead of
    depending on real thread timing."""
    threading.Thread(target=target, args=args, daemon=True).start()


def _fetch_occupant_if_needed(api, own_uid, desk_states, key, kind,
                              names, pending, spawn):
    """Kick off (never block on) the one `/user` lookup a "booked" desk's
    status line wants -- see `_run_live`'s docstring on why this can't run
    inline. `names` is the `uid -> display name` cache `_status_line`
    reads from; `pending` is the set of uids already in flight, so
    re-visiting (or pre-fetching, see `_prefetch_occupants`) the same
    booked desk twice before the first fetch lands doesn't stack
    duplicate lookups. `desk_states` may be `None` (every call site that
    hasn't opted into this feature) -- a no-op in that case, exactly like
    phase 3b's behaviour before this existed."""
    if desk_states is None or kind != "booked":
        return
    state = desk_states.get(key)
    uid = state.uid if state is not None else None
    if not uid or uid in names or uid in pending:
        return
    pending.add(uid)

    def _fetch(uid=uid, bkid=state.bkid):
        try:
            occupant_name(api, own_uid, uid, bkid, names)
        finally:
            pending.discard(uid)

    spawn(_fetch, ())


def _prefetch_occupants(api, own_uid, desk_states, desk_kind_positions,
                        desk_positions, cursor_key, cursor_pos, names,
                        pending, spawn):
    """Fetch the cursor's own desk AND its four `nearest_desk` neighbours.

    Fetching only the cursor's own desk (phase 3b's first cut at this
    feature) meant a booked desk's name only ever started loading on
    arrival -- and since the redraw loop blocks on the next keypress
    right after drawing, that result could never be shown before the
    user had already arrowed on to somewhere else. Pre-fetching the
    neighbours too means a name is usually already cached (or at least
    already in flight, with a human's reaction time to finish in) by the
    time an arrow key actually lands there -- the status line shows it on
    the very first frame after the move instead of never."""
    kind = desk_kind_positions.get(cursor_pos)
    _fetch_occupant_if_needed(api, own_uid, desk_states, cursor_key, kind,
                              names, pending, spawn)
    for direction in ("up", "down", "left", "right"):
        neighbour_key = nearest_desk(cursor_pos, desk_positions, direction)
        if neighbour_key is None:
            continue
        neighbour_kind = desk_kind_positions.get(desk_positions[neighbour_key])
        _fetch_occupant_if_needed(api, own_uid, desk_states, neighbour_key,
                                  neighbour_kind, names, pending, spawn)


def _occupant_api(ctx):
    """The `Api` the background occupant-name fetch calls through --
    deliberately NOT `ctx.api` when a real session is behind it.

    `ctx.api`'s `Session` re-logs-in on a dead session by default
    (`allow_login=True`), which means an interactive password prompt (and
    possibly an MFA push) -- fine on the main thread mid-command, but a
    background thread hitting that path while the main thread owns the
    tty in cbreak mode inside the alternate screen is a real hang/
    corruption risk, not just a cosmetic one. This builds a SECOND
    `Session` sharing the same on-disk cookie store (so it doesn't need
    to log in again itself, most of the time) but constructed with
    `allow_login=False` -- a dead session then raises `LoginRequired`
    instead of prompting, which `occupant_name`'s existing `except
    Exception` already swallows.

    Falls back to `ctx.api` unchanged when `ctx` has no `.session` at all
    -- every test double `Ctx` in this module's tests, none of which
    carry the real background-relogin risk this guards against."""
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
              term_size=None):
    """The redraw loop behind arrow-key navigation and book/release from
    `fs map`'s live view. Kept as thin as possible: every decision
    (which desk an arrow lands on, what Enter means) is delegated to
    `nearest_desk`/`plan_for_cursor` -- this function only sequences
    keypresses, draws frames, and calls `_do_write`/`_refresh` around a
    confirm gesture.

    Each frame is written into the alternate screen buffer (entered once
    on the way in, left once on the way out) with a cursor-home plus a
    per-line clear-to-end-of-line, not a full-screen clear every keypress,
    to avoid a visible flicker. `_render_lines`'s `cursor_pos` argument
    makes the cursor itself visible by reverse-videoing and blinking that
    one cell. The alt-screen entry sequence also hides the REAL terminal
    cursor (`\\x1b[?25l`, restored with `\\x1b[?25h` on the way out) -- left
    visible, it sits blinking at the end of the status line and reads as
    an invitation to type, which nothing here ever reads.

    `desk_states` (a `Catalog.availability()` result, threaded through
    from `cmd_map` and re-threaded from `_refresh` after every write) is
    what lets the status line say who has a "booked" desk -- see
    `_prefetch_occupants` (fetches the cursor's own desk and its four
    neighbours, so a name is usually ready before an arrow key actually
    lands there) and `_occupant_api` (a second, `allow_login=False`
    session for that background fetch, so a session dying mid-browse
    can't trigger an interactive relogin prompt from a daemon thread
    while this loop owns the tty). Optional and `None`-safe: a caller
    that doesn't pass `desk_states` gets the plain "unavailable" wording,
    no lookup attempted.

    `read_key`/`stdin`/`initial_cursor_key`/`spawn` are all injectable
    (defaults: `keyread.read_key`, `sys.stdin`, the design spec's own
    initial-cursor rule, a real background thread) so tests can drive
    this loop with a scripted list of symbolic keys, a known starting
    desk, and a synchronous fetch instead of a real terminal and real
    concurrency.

    Digit keys switch floor and PgUp/PgDn (or `n`/`p`, added as easier-to-
    reach aliases -- not every keyboard/terminal makes PgUp/PgDn
    comfortable one-handed) step the date, both via the same
    `_refresh(ctx, planid, target_date)` post-write already uses -- it
    takes both as plain params, so a floor/date change is just a call
    with a different one, then the cursor is re-picked with
    `_initial_cursor_key` since the old key isn't guaranteed to exist in
    the new `desk_positions`. PgDn/`n` is clamped so it never steps
    earlier than `out.today`; there's no equivalent clamp going forward --
    the server, not this loop, is what actually governs the booking
    window.

    Every frame -- including the interim "Loading ..." one drawn while a
    day/floor change is in flight -- ends with `\\x1b[J` (erase from the
    cursor to the end of the screen) right after the redraw, not just each
    line's own `\\x1b[K`. A per-line clear only ever erases stray
    characters to the RIGHT of what this frame just drew; it does nothing
    about a taller PREVIOUS frame leaving extra rows below a shorter new
    one, which is exactly what a floor switch to a smaller map produces
    (Level 5 and Level 6 render different `n_rows`). `\\x1b[J`, not
    `\\x1b[2J` (a full clear from the top, already rejected -- see this
    function's docstring above on why redraws never blank the whole
    screen): it only erases what's below the cursor, i.e. only ever the
    stale leftover, never the frame just drawn above it.

    A `("mouse", button, col, row, pressed)` key (SGR mouse report, see
    `keyread.read_key`) moves the cursor to whatever desk is under a
    qualifying left-button press (`button & 3 == 0`, not a motion/drag
    event) via `desk_at_click`; anything else -- release, drag, wheel,
    right/middle click -- is a no-op, same as an unrecognised key.
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
    # `floor_labels`/`floor_tokens`: this loop's own copy of `cmd_map`'s
    # `names`/`aliases` -- `catalog.desks()` is cached, so re-deriving here
    # costs nothing, and it keeps `_run_live` independently correct for
    # tests that call it directly rather than through `cmd_map`. Named
    # differently from the (unrelated) `names` local below -- that one is
    # the uid->occupant-name cache, not a floor lookup.
    floor_labels = floor_names(catalog)
    floor_tokens = floor_aliases(floor_labels)
    book_day_start = catalog.book_day_start_mins()
    bookings = own_bookings(api, out.today)
    try:
        own_uid = catalog.own_uid()
    except Exception:                             # noqa: BLE001
        # Same "a name is a nicety" spirit as `occupant_name` itself --
        # this call happens once, outside any per-frame error handling,
        # so it must not take the whole live view down with it.
        own_uid = None
    names = {}              # uid -> display name, filled in by background fetches
    pending = set()          # uids with a fetch already in flight
    confirming = None      # (action, bkid) while the confirm line is up
    message = None         # last write's result line, shown once
    last_lines = None       # the most recent frame, reprinted once on exit

    # `\x1b[?25l` also hides the real terminal cursor -- left visible and
    # blinking at the bottom of the status line, it reads as "you can type
    # a message here", which nothing in this loop ever reads from.
    # Restored (`\x1b[?25h`) in the `finally` below so quitting never
    # leaves a real terminal hidden-cursor.
    out.prompt("\x1b[?1049h\x1b[?1000h\x1b[?1006h\x1b[?25l")
    # Injectable so tests keep the exact `columns`/`rows` they passed in on
    # every frame (the default closes over those two fixed values and
    # never changes them); the real `cmd_map` call site passes one that
    # re-queries `shutil.get_terminal_size()` fresh every frame instead,
    # so a mid-session resize is picked up rather than cropping frozen
    # forever to the size measured on the way in -- see `terminal_size`.
    term_size = term_size or (lambda: (columns, rows))
    # Persisted across frames so `scroll_window` can tell "cursor still
    # comfortably inside the current window" from "cursor just crossed the
    # margin" -- see its docstring for why a fresh `crop_window` call every
    # frame can't do that.
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

            # Vertical counterpart to the column scroll above, reusing the
            # exact same function (see `scroll_window`'s docstring) --
            # `HEADER_LINES` (the one line above the grid) plus 1 (the
            # status/confirm line below it) is how many terminal rows the
            # grid itself does NOT get to use. `None` (no known terminal
            # height, e.g. piped output) means "show every row", same as
            # the column scroll's own no-width fallback.
            n_rows = len(grid)
            grid_rows_budget = (rows - HEADER_LINES - 1) if rows else None
            row_window = scroll_window(row_window, cursor_pos[0], n_rows,
                                       grid_rows_budget)
            top, bottom = row_window if row_window else (0, n_rows)

            pos_to_key = {pos: key for key, pos in desk_positions.items()}

            kind = desk_kind_positions.get(cursor_pos)
            _prefetch_occupants(bg_api, own_uid, desk_states,
                                desk_kind_positions, desk_positions,
                                cursor_key, cursor_pos, names, pending, spawn)

            lines = [header_line(out, planid, target_date, floor_labels)]
            lines += _render_lines(out, grid, desk_kind_positions, left, right,
                                   cursor_pos=cursor_pos, top=top, bottom=bottom)
            if confirming is not None:
                action, _bkid = confirming
                lines.append(_confirm_line(out, action, cursor_key,
                                           target_date))
            else:
                state = desk_states.get(cursor_key) if desk_states else None
                occupant = names.get(state.uid) if state else None
                lines.append(_status_line(out, cursor_key, kind, message,
                                          occupant))
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
                        ctx, planid, target_date)
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
                            ctx, planid, target_date)
                else:
                    confirming = (action, bkid)
                continue

            if isinstance(key, str) and key.isdigit():
                new_planid = floor_tokens.get(key)
                if new_planid is not None and new_planid != planid:
                    planid = new_planid
                    # Same "show a loading line before the blocking fetch"
                    # treatment PgUp/PgDn's date switch already gets below --
                    # a floor switch re-fetches `deskpolys`/`availability`
                    # for the new floor too, and was otherwise a silent
                    # pause on the OLD floor's frame.
                    out.prompt("\x1b[H")
                    loading = lines[:-1] + [" Loading floor..."]
                    out.print("\n".join(line + "\x1b[K" for line in loading))
                    out.prompt("\x1b[J")
                    (grid, highlight_positions, desk_kind_positions,
                     desk_positions, bookings, desk_states) = _refresh(
                        ctx, planid, target_date)
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
                    ctx, planid, target_date)
                # The floor hasn't changed, so `desk_positions` is the same
                # layout -- only each desk's KIND (free/booked/yours/etc.)
                # may differ on the new date. Keep the cursor sitting on
                # the same desk rather than re-picking with
                # `_initial_cursor_key`; only re-pick if it genuinely
                # isn't there any more (defensive -- shouldn't happen on
                # an unchanged floor).
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
        # Leaving the alt screen restores the primary buffer, which would
        # otherwise wipe the map off screen -- reprint the last frame,
        # plainly (no cursor-positioning codes; this is going into normal
        # scrollback now, not being redrawn in place), so quitting doesn't
        # change what the user is left looking at.
        if last_lines is not None:
            out.print("\n".join(last_lines))
