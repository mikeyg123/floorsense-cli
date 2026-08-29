"""Per-workplace floorplan map data lives here, one module per workplace.

`fs map` (`..commands.map_cmd`) renders desk maps from a set of
hand-traced constant tables -- outer wall outline, internal partitions,
room boxes, zone names, and manual position corrections -- because a
floorplan image has no machine-readable geometry of its own; someone
has to eyeball it once and record what they saw. Those tables are
specific to one physical building, not to the `fs` codebase, so they
live in their own module here rather than in `map_cmd.py` alongside the
(building-agnostic) rendering logic that reads them.

`example_workplace.py` is the only module today (this project's own
account). To add another workplace: copy it, replace the planids
(found via `fs desks` or the catalog) and every `*_BY_FLOOR` table's
values for your building's floorplan images, and see
`docs/floorplan-map-manual.md` for how each table is derived. There is
no selection mechanism yet -- `map_cmd.py` imports one module directly
-- because there has only ever been one contributed workplace; that's
worth building the day a second one shows up, not before.
"""
