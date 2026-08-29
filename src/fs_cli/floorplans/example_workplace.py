"""Floorplan map data for this project's own Floorsense account -- Level 5
and Level 6 of one building. A worked example: the constant tables a
contributor copies and re-derives to add their own workplace's map. See
`floorplans/__init__.py` for the module layout and
`docs/floorplan-map-manual.md` for how each table below was derived
(coordinate conventions, the six wrong-turn/fix pairs recorded there).

Every table is keyed by `planid` (this account's catalog floor IDs: 3 =
Level 5, 4 = Level 6) rather than by source filename -- the only
mechanical change from `experiments/floorplan_ascii.py`, the gitignored
scratch prototype these tables were ported near-verbatim from.
"""

PLANID_LEVEL5 = 3
PLANID_LEVEL6 = 4
#: Last-resort fallback for `resolve_floor`/`floor_names`/`header_line`,
#: used only when the catalog itself has nothing to offer (an empty
#: account) -- never assumed to be a valid floor on every deployment. See
#: `floor_names`'s docstring for why nothing here is hardcoded per-building
#: anymore.
DEFAULT_PLANID = PLANID_LEVEL5

# Outer wall, per floor: ordered polygon vertices as (x_frac, y_frac) of
# image width/height, eyeballed from the rendered floorplan image except
# where noted. Closed automatically (last point connects back to first).
WALL_BY_FLOOR = {
    PLANID_LEVEL5: [
        (0.015, 0.020), (0.435, 0.020),   # top edge to first notch
        (0.475, 0.000), (0.755, 0.000),   # notch up, flat top
        (0.795, 0.030), (0.985, 0.030),   # notch down, flat top
        (0.985, 0.985),                   # right edge (straight -- real
                                           # wall has a window-bay sawtooth,
                                           # skipped as unneeded detail)
        (0.235, 0.985),                   # bottom edge -- only as far as
                                           # the lower section's left wall
        (0.235, 0.235),                   # up the lower section's left wall
        (0.015, 0.235),                   # step back out to the wider
                                           # upper (Zone A) section -- the
                                           # building genuinely narrows
                                           # below Zone A, confirmed against
                                           # real desk positions (nothing
                                           # sits left of x=0.235 below
                                           # this row)
    ],
    PLANID_LEVEL6: [
        # Corrected against the floorplan image: the fold here is at the
        # BOTTOM, not below the desk area like level 5's -- an open
        # outdoor terrace cuts into the bottom-left corner, so the left
        # wall stops short of the true bottom and steps right before
        # continuing down. Bottom edge sits just past the lowest real
        # desk (y=0.792, filtered to catalog-only desks -- see
        # `desk_centers`'s docstring).
        (0.215, 0.085), (0.60, 0.085), (0.63, 0.02), (0.87, 0.02),
        (0.90, 0.06), (0.99, 0.06), (0.99, 0.81), (0.3675, 0.81),
        (0.3675, 0.715), (0.215, 0.715),
    ],
}

# Sub-region actually worth printing, as (x0, y0, x1, y1) image fractions.
# Level 6's admin/services core (WC, lifts, the void and above) has no
# bookable desks and isn't worth the width; the office wing to the right
# of the meeting rooms is the same story on the right edge.
CROP_BY_FLOOR = {
    PLANID_LEVEL6: (0.20, 0.43, 0.80, 0.83),
}

# Zone names for desk pods, matched by a desk's centroid falling inside
# one of these boxes. Only the zones the floorplan image actually calls
# out with a colored highlight + label get a name here.
# Internal partitions -- short straight runs (walls/screens between desk
# zones, not full building walls), each drawn with a one-character gap at
# both ends so it reads as "a partition with a walkway around it".
# (x0_frac, y0_frac, x1_frac, y1_frac) per segment.
PARTITIONS_BY_FLOOR = {
    PLANID_LEVEL5: [
        # Zone A's own internal divider (was `(0.145, 0.06, 0.145, 0.21)`) --
        # removed per direct user feedback: it's the leftmost line in the
        # Zone A zone and doesn't correspond to a real partition there.
        (0.28, 0.05, 0.28, 0.21),     # Zone A / Zone B boundary
        (0.2522, 0.4595, 0.3272, 0.4595),
        (0.2522, 0.6433, 0.3272, 0.6433),
    ],
    PLANID_LEVEL6: [],
}

# Manual column nudge for a zone name label, keyed by the zone's own name
# -- for when the label runs into the wall or another block rather than
# the block's actual position being wrong. Doesn't move any desk.
ZONE_LABEL_NUDGE_BY_FLOOR = {
    PLANID_LEVEL5: {"Zone C": -5},
    PLANID_LEVEL6: {},
}

# One-character row offset applied to the partitions above, after they're
# otherwise placed.
PARTITION_ROW_NUDGE = 1

# Rooms with no bookable desks, rendered as a plain box (no interior
# detail). (label, x0_frac, y0_frac, x1_frac, y1_frac).
ROOM_BOXES_BY_FLOOR = {
    PLANID_LEVEL5: [
        ("VOID", 0.52, 0.30, 0.73, 0.50),
        ("LIFT/STAIRS", 0.52, 0.0784, 0.73, 0.24),
        ("MEETING ROOMS", 0.50, 0.528, 0.76, 0.70),
    ],
    PLANID_LEVEL6: [
        ("MEETING ROOMS", 0.46, 0.435, 0.63, 0.60),
    ],
}

# Manual position corrections, by the block number reading-order assigns
# -- real desk data always needs a hand-adjustment pass on top of the
# automatic placement. (row_delta, col_delta) added to the block's
# computed anchor.
BLOCK_ADJUSTMENTS_BY_FLOOR = {
    PLANID_LEVEL5: {
        12: (0, 1), 15: (0, 1), 21: (0, 1), 27: (0, 1),
        # Block 16 -- the horizontal 6-desk group about halfway down the
        # right wall (L5.D.38/39/...) -- sat one column right of the
        # groups directly above and below it along that same wall,
        # per direct user feedback. Shifted left to line back up.
        16: (0, -1),
    },
    PLANID_LEVEL6: {},
}

ZONE_NAMES_BY_FLOOR = {
    PLANID_LEVEL5: [
        ("Zone A", 0.00, 0.05, 0.28, 0.22),
        ("Zone B", 0.28, 0.05, 0.40, 0.22),
        ("Zone C", 0.76, 0.03, 1.00, 0.26),
        ("Zone D", 0.22, 0.28, 0.40, 0.33),
    ],
    PLANID_LEVEL6: [
        ("Quiet Zone", 0.20, 0.42, 0.36, 0.90),
    ],
}
