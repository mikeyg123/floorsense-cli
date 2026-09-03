"""Per-workplace floorplan map data, one module per workplace.

`fs map` renders desk maps from hand-traced constant tables (wall
outline, partitions, room boxes, zone names, position corrections) --
a floorplan image has no machine-readable geometry, so someone has to
eyeball it once and record what they saw. Building-specific, so it
lives here rather than in the (building-agnostic) `map_cmd.py`.

`example_workplace.py` is the only module today. To add another
workplace: copy it, replace the planids and every `*_BY_FLOOR` table
for your building (see `docs/floorplan-map-manual.md`). No selection
mechanism yet -- `map_cmd.py` imports one module directly -- since
there's only ever been one contributed workplace.
"""
