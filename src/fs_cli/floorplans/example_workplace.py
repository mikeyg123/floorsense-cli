"""Floorplan map data for this project's own Floorsense account -- Level
5 and Level 6 of one building. A worked example: the constant tables a
contributor copies and re-derives to add their own workplace's map. See
`docs/floorplan-map-manual.md` for how each table below was derived.

Every table is keyed by `planid` (this account's catalog floor IDs: 3 =
Level 5, 4 = Level 6).
"""

PLANID_LEVEL5 = 3
PLANID_LEVEL6 = 4
#: Last-resort fallback for `resolve_floor`/`floor_names`/`header_line`,
#: used only when the catalog has nothing to offer (an empty account).
DEFAULT_PLANID = PLANID_LEVEL5

# Outer wall, per floor: ordered polygon vertices as (x_frac, y_frac) of
# image width/height, eyeballed from the floorplan image. Closed
# automatically (last point connects back to first).
WALL_BY_FLOOR = {
    PLANID_LEVEL5: [
        (0.015, 0.020), (0.435, 0.020),   # top edge to first notch
        (0.475, 0.000), (0.755, 0.000),   # notch up, flat top
        (0.795, 0.030), (0.985, 0.030),   # notch down, flat top
        (0.985, 0.985),                   # right edge (sawtooth simplified straight)
        (0.235, 0.985),                   # bottom edge, lower section only
        (0.235, 0.235),                   # up the lower section's left wall
        (0.015, 0.235),                   # step out to the wider Zone A
                                           # section -- building narrows
                                           # below here, confirmed against
                                           # real desk positions
    ],
    PLANID_LEVEL6: [
        # Fold is at the BOTTOM here (not below the desk area like level
        # 5's): an outdoor terrace cuts into the bottom-left corner, so
        # the left wall steps right before continuing down.
        (0.215, 0.085), (0.60, 0.085), (0.63, 0.02), (0.87, 0.02),
        (0.90, 0.06), (0.99, 0.06), (0.99, 0.81), (0.3675, 0.81),
        (0.3675, 0.715), (0.215, 0.715),
    ],
}

# Sub-region actually worth printing, as (x0, y0, x1, y1) image fractions.
# Level 6's admin/services core (WC, lifts, void) and the office wing past
# the meeting rooms have no bookable desks and aren't worth the width.
CROP_BY_FLOOR = {
    PLANID_LEVEL6: (0.20, 0.43, 0.80, 0.83),
}

# Internal partitions -- short straight runs between desk zones (not full
# building walls), drawn with a one-character gap at both ends.
# (x0_frac, y0_frac, x1_frac, y1_frac) per segment.
PARTITIONS_BY_FLOOR = {
    PLANID_LEVEL5: [
        (0.28, 0.05, 0.28, 0.21),     # V / Tech Point boundary
        (0.2522, 0.4595, 0.3272, 0.4595),
        (0.2522, 0.6433, 0.3272, 0.6433),
    ],
    PLANID_LEVEL6: [],
}

# Manual column nudge for a zone name label, keyed by zone name -- for
# when the label itself runs into a wall/block. Doesn't move any desk.
ZONE_LABEL_NUDGE_BY_FLOOR = {
    PLANID_LEVEL5: {"Customer Excellence": -5},
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

# Manual position corrections, by the block number reading-order assigns.
# (row_delta, col_delta) added to the block's computed anchor.
BLOCK_ADJUSTMENTS_BY_FLOOR = {
    PLANID_LEVEL5: {
        12: (0, 1), 15: (0, 1), 21: (0, 1), 27: (0, 1),
        16: (0, -1),  # lines up with the groups above/below it on the right wall
    },
    PLANID_LEVEL6: {},
}

ZONE_NAMES_BY_FLOOR = {
    PLANID_LEVEL5: [
        ("V", 0.00, 0.05, 0.28, 0.22),
        ("Tech Point", 0.28, 0.05, 0.40, 0.22),
        ("Customer Excellence", 0.76, 0.03, 1.00, 0.26),
        ("Zone D", 0.22, 0.28, 0.40, 0.33),
    ],
    PLANID_LEVEL6: [
        ("Quiet Zone", 0.20, 0.42, 0.36, 0.90),
    ],
}
