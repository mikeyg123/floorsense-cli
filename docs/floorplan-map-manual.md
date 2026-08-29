# Floorplan ASCII map — reference

Everything learned building a working ASCII/Unicode floorplan renderer
for both floors (Level 5 / planid 3, Level 6 / planid 4), captured so
the next session doesn't re-derive it. The renderer shipped as `fs
map`/`src/fs_cli/commands/map_cmd.py` (phases 1-2, see below); the
original prototype it was ported from, `experiments/floorplan_ascii.py`
(gitignored, per this project's convention — see `CLAUDE.md`), is now
historical/reference only.

## Where this sits relative to the plan

The original ask (see chat history) was staged in three phases, all now
shipped:

1. **Produce a viable static ASCII map per floor.** ✅ Done for both
   floors — this doc's subject.
2. Show the user's own desk location on the map (or a cropped section).
   ✅ Done -- shipped as `fs map`.
3. Color desks by live status (free/booked/restricted/yours) and add
   arrow-key cursor navigation to book a desk directly from the map.
   **Both done.**

## Data sources — no browser needed

Contrary to the initial assumption, none of this needed Playwright or
any browser automation:

- **Desk positions** come from `floorplan-booking`'s
  `info.deskpolys.polys[]` — an exact pixel-rectangle per desk, keyed by
  desk id (matches the catalog `key`). This is real, load-bearing
  geometry, not something to eyeball from an image.
- **Locker positions** are in the same `deskpolys.lockPolys[]` — dropped
  from the renderer per user feedback (didn't read well as ASCII), but
  the geometry access pattern is documented below in case that changes.
- **The floorplan background image** is fetched with an ordinary
  authenticated GET to `{origin}/app/{imgpath}`, where `imgpath` comes
  from `floorplan-booking`'s `info.imgpath` (e.g.
  `floorplans/floorplan_level5_v130.png`).
  Reachable with the project's own `Session` machinery — no new auth
  code needed:

  ```python
  from fs_cli import config as config_mod
  from fs_cli.session import Session, SessionStore
  from fs_cli.api import LiveApi

  cfg = config_mod.load(directory, on_repair=print)
  session = Session(cfg, SessionStore(directory / "session.json"),
                     on_message=print, allow_login=False)
  api = LiveApi(session)
  info = api.floorplan_booking(planid, day, start, finish)
  session.ensure()
  raw = session._session.get(session.origin + "/app/" + info["imgpath"])
  # raw.content is the PNG bytes
  ```

  `Session.get()`/`.call()` can't be used for the image itself — it
  always tries to JSON-decode the response and raises `CommError` on
  `image/png`. Drop to the underlying `requests.Session` (`session._session`)
  for the raw fetch, same requests-lib call the rest of the module
  already makes.
- **Structural detail not in any API payload** (walls, partitions,
  pillars, named non-desk areas) has to come from actually looking at
  that fetched image — there's no geometry for it. `roomPolys` and
  `otherPolys` (siblings of `polys`/`lockPolys` in `deskpolys`) were
  checked and are empty on both floors in this deployment; don't assume
  they're populated elsewhere without checking again.
- **Which `deskpolys.polys[]` entries are real bookable desks**: not all
  of them. Level 6 has 3 poly entries (`L6.D.100`, `L6.D.101`,
  `L6.D.102`) with geometry but **no entry in `info.desks[]`** (the
  actual catalog per `floorsense-api-manual.md` §5.3) — confirmed with
  the user to be unbookable collab-space furniture, not desks. Level 5
  has no such mismatch (262 polys, 262 catalog desks, exact match) —
  don't assume it's clean elsewhere; always filter
  `deskpolys.polys[]` down to `{d["key"] for d in info["desks"]}`
  before treating an entry as a real desk. See `desk_centers()`'s
  docstring in the script.

## Why per-floor scale can't be a shared constant

The two floors' images are captured at very different pixel densities —
confirmed from the data, not assumed:

| | Level 5 | Level 6 |
|---|---|---|
| Image size | 3041×2013 | 8244×5824 |
| Typical desk depth (short side) | ~37px | ~78.8px |
| Desk count | 262 | 103 |

37→78.8 is a ~2.13× jump, while the image dimensions jumped ~2.7×— the
two floors' exports don't share a pixel-per-real-world-unit scale, so
any renderer has to derive its scale from **each floor's own desk
geometry**, never from a shared constant or from a target output width.
`row_pitch()` (median of `min(width, height)` across all desks on that
floor) is that per-floor reference unit.

## Rendering approach (what worked, after several wrong turns)

The wrong turns are worth keeping because the next session (or the next
floor, if the building layout ever changes) will hit the same ones:

1. **Deriving scale from a target output width collapses back-to-back
   rows.** If the chosen resolution puts less than one full character
   row between two real desk rows, they round onto the same row and
   half the desks silently vanish. Fix: derive the row scale from the
   real row pitch itself (`row_pitch()`), not from "what fits in N
   columns."
2. **Rounding each desk's raw pixel position independently surfaces
   real-world layout imprecision as visible misalignment.** An annex
   desk like `L5.D.193A` sitting a few px off from where its neighbours
   "should" put it will round to a different character column than
   desks it's meant to line up with — and no amount of chasing
   individual cases converges, because each fix surfaces another.
   A **clustering-based** jitter-removal pass (snap nearby raw values
   together) makes it worse in a subtler way: with enough desks on a
   floor, chaining through unrelated ones can silently bridge two
   *genuinely distinct* rows into one. The fix that actually worked:
   place desks **by block**, not by individual position. Cluster desks
   into physical pods (proximity flood-fill), then render each block
   from a **fixed template** anchored at the block's own top-left corner
   — every desk in a block gets a rank-based offset (its local row/column
   rank within that one cluster, not its raw coordinate), so the whole
   block is perfectly evenly spaced regardless of how imprecise the real
   desk positions are individually.
3. **A block's orientation (paired horizontal rows vs. a vertical run of
   back-to-back pairs) can't be decided by counting local rows/columns
   alone.** A 2-column-by-2-row block is a genuine tie by count. The
   desks' own width vs. height decides it correctly with no tie
   possible — a landscape desk (wider than tall) belongs to a
   horizontal-row block, a portrait one to a vertical block.
4. **A locker bank that looks L-shaped in a naive polygon fill is often
   several separate rectangular banks traced as one polygon**, connected
   corner-to-corner by a thin (~15-20px) leader line — presumably an
   artifact of however the source CAD/export tool emitted the shape.
   A connector edge is far longer than any real bank edge (>230px on
   this floor's data, safely above the widest real edge (~210px) and
   below the shortest connector (~260px)); cutting the polygon's ring at
   those long edges recovers the real per-bank rectangles. (This ended
   up unused in the final renderer — lockers were dropped entirely on
   user feedback — but the cutting technique is worth keeping in mind
   for any other multi-part polygon in this data.)
5. **Character cells aren't square.** A terminal glyph is roughly twice
   as tall as it is wide, so a 1:1 world-unit-to-character mapping makes
   a real square floor plate look tall and narrow. `col_scale =
   row_scale * 2` (twice the horizontal resolution) corrects for this —
   confirmed as "looks right" only after actually rendering and
   checking, not derived from font metrics.
6. **A floor doesn't need to be rendered edge-to-edge.** Level 6's
   admin/services core (WC, lifts, the void and everything above it) and
   an office wing on one side have no bookable desks at all and aren't
   worth the map width. A crop — `(x0, y0, x1, y1)` as fractions of the
   full image — combined with the fact that every other fraction-based
   table in the script (walls, partitions, room boxes, zone names) is
   still in full-image terms, means out-of-crop geometry just computes
   to a negative or past-the-edge character position and the existing
   bounds checks quietly drop it. No separate "cropped wall shape" table
   needed.

## What the renderer draws, and from what

| Element | Source | Notes |
|---|---|---|
| Desks | `deskpolys.polys[]`, filtered to `info.desks[]` catalog keys | Block-clustered, fixed-template placement (see above) |
| Outer wall | Hand-traced from the fetched image, as a polygon of `(x_frac, y_frac)` points | Furniture bounding box is a safe *lower bound* to check a guess against — real desks never sit outside the true wall |
| Internal partitions | Hand-traced short line segments, drawn with a gap at both ends (`gap_ends=1`) | A sealed line reads as "no way through"; real partitions have a walkway around them |
| Room boxes (VOID, LIFT/STAIRS, MEETING ROOMS) | Hand-traced rectangles | Plain outline only — "no bookable desks in here" is the whole point, no interior detail needed |
| Zone name labels (Zone A, Zone B, Zone C, Zone D, Quiet Zone) | Hand-traced bounding boxes, matched against a desk's centroid | Verified against real desk counts the user supplied (Zone A 32, Zone C 25, Zone B 12) rather than trusted on sight |
| Lockers | ~~`deskpolys.lockPolys[]`~~ | **Removed** — didn't read well as ASCII, per direct feedback |

Every hand-traced table lives in `src/fs_cli/floorplans/example_workplace.py`
(`WALL_BY_FLOOR`, `PARTITIONS_BY_FLOOR`, `ROOM_BOXES_BY_FLOOR`,
`ZONE_NAMES_BY_FLOOR`, and the rest) — split out from `map_cmd.py` so a
contributor adding another workplace's map can copy that one module
rather than editing the renderer. Each is a first-look estimate
refined through several rounds of "run it, tell me what's wrong" against
someone who actually knows the office — not something derivable from
the API data alone. Two smaller correction mechanisms exist for the
same reason:

- `ZONE_LABEL_NUDGE_BY_FLOOR` — a manual column nudge for one zone
  label's *text*, when the label collided with a wall despite the
  block's own position being correct.
- `BLOCK_ADJUSTMENTS_BY_FLOOR` — a manual `(row_delta, col_delta)` per
  block, keyed by the block's reading-order number that `fs map` used to
  print (temporarily, during the correction pass — not printed in the
  finished map). The mechanism is still live even though the numbers
  aren't displayed anymore, since corrections may still come up.

## Level 5 / Level 6 specifics worth knowing before touching either

- Level 5's building genuinely **narrows below Zone A** — nothing sits left
  of x≈0.235 (image fraction) below that row. Confirmed against real
  desk positions, not assumed from the image alone.
- Level 6's building **folds in at the bottom-left** for an open outdoor
  terrace — the opposite corner from level 5's fold, and lower down
  (confirmed against the image only, no desks near there to cross-check
  against).
- Level 6's "meeting rooms" (Dairy Flat/Leigh/Whenuapai/Piha and
  Silverdale/Mount Eden/Huapai/South Head) are true meeting **rooms**
  with round tables — no bookable desks inside them at all. Their name
  labels happen to sit outside the room's own footprint in the source
  image. The real bookable desk cluster nearby is a **separate area**,
  not inside those rooms — got this backwards once already; don't
  re-derive it from the visual layout without checking desk coordinates
  against the room's actual bounding box first.

## Picking this back up

Phases 1-2 (`fs map`'s static render + own-desk highlight) and phase
3a (live per-desk status colour: free/restricted/booked/yours) are
both done. `render_floor` takes `desk_states` (a
`Catalog.availability()` result) and returns a third value,
`desk_kind_positions`, alongside `grid`/`highlight_positions` --
`cmd_map` is the only place that turns a "kind"
(free/restricted/booked/yours) into an `Output` colour method
(`STYLE_METHOD_BY_KIND`), matching `render_floor`'s existing
no-colour-opinion contract.

Phases 1-2, 3a (live-status colour), and 3b (cursor navigation,
book/release from the map) are all shipped. Nothing further is currently
planned for this renderer; if a future phase 3c (date-switching inside
the live view, check-in from the map, multi-desk actions) comes up, it
gets its own spec the same way 3a/3b did rather than being folded into
`map_cmd.py` without one.

**Post-3b polish (direct user feedback, not its own spec -- bounded
fixes to what 3b already shipped):** the cursor was invisible (nothing
distinguished it from another desk of the same kind), the per-keypress
redraw flickered, and the status line had no way to say who has a
"booked" desk. Fixed in `map_cmd.py`:

- `_render_lines`'s `cursor_pos` argument reverse-videos the cursor's
  own cell (`Output.reverse`, new in `render.py`), layered over its
  existing kind colour. Gated on `out.color` like every other kind
  colour in this view -- with colour off the cursor goes back to being
  unmarked, an existing gap this didn't newly create but is worth
  knowing about if it comes up again.
- The redraw loop now writes into the alternate screen buffer (entered
  once, left once) with a cursor-home plus per-line clear-to-end-of-line
  instead of a full `\x1b[2J` every keypress. Leaving the alt screen
  restores the primary buffer, which would otherwise wipe the map off
  screen on quit -- the last frame is reprinted once, plainly, right
  after, so quitting leaves the same thing on screen it always did.
- `_prefetch_occupants` fills a `uid -> name` cache via `at_cmd.py`'s
  `occupant_name` (promoted from `_occupant`, same lookup/cache/
  fallback-to-uid shape, now shared rather than duplicated) for the
  cursor's own desk AND its four `nearest_desk` neighbours, each as a
  background thread (`_spawn_thread` by default, injectable via
  `_run_live`'s `spawn` param the same way `read_key` already is). Only
  fetching the cursor's own desk was tried first and didn't work in
  practice: the redraw loop blocks on the next keypress right after
  drawing a frame, so a fetch that only starts on arrival can never
  finish in time to be shown before the user has already moved on.
  Pre-fetching the neighbours means a name is usually cached (or
  in-flight with a human's reaction time to land) by the time an arrow
  key actually reaches it.
- That background fetch runs through `_occupant_api`, not `ctx.api` --
  a second `Session` sharing the same on-disk cookie store but built
  with `allow_login=False`. `ctx.api`'s ordinary session re-logs-in
  (prompting for a password, possibly an MFA push) on a session that
  died mid-browse, and a background thread hitting that path while the
  main thread owns the tty in cbreak mode is a hang risk, not a
  cosmetic one -- `allow_login=False` makes a dead session raise
  instead, which `occupant_name`'s existing `except Exception` already
  swallows.

`desk_states` had to be threaded through `_run_live` and `_refresh`
(both previously didn't carry it) to make any of the occupant-name work
possible; it's optional (`None` by every call site that hasn't opted
in) so this cost no existing test a rewrite.

**Post-3b, round 2 (direct user feedback, still bounded fixes, not a
new spec):** floor-switching, date-switching, and mouse support added
to the live view, all reusing existing machinery rather than adding new
plumbing:

- Digit keys `0`-`9` switch floor via `floor_aliases()`'s bare-digit
  tokens (see "Generalising floor names/aliases" below) -- only `5`/`6`
  exist on this deployment, so every other digit (and the current
  floor's own digit) is a no-op. Reuses `_refresh(ctx, planid, target_date)` unchanged --
  it already took `planid`/`target_date` as plain params for the
  post-write refresh, so a floor switch is just calling it with a
  different `planid`. `_initial_cursor_key` re-picks the cursor
  afterwards since the old key won't exist in the new floor's
  `desk_positions`.
- `PgUp`/`PgDn` step the date by one day (floor held fixed), same
  `_refresh` reuse with a different `target_date`. `PgDn` clamps at
  `out.today` -- going further back is a no-op, not an error; there's
  no forward clamp, since the server (not this loop) governs how far
  ahead booking actually works. Before the blocking `_refresh` call,
  the CURRENT frame is redrawn once with its status line swapped for
  `Loading {date}...` so a slow fetch isn't a silent pause.
- A first "header" line (`Level 5 -- Wed 27 Aug`, `header_line()`) was
  added above the grid on every frame -- floor and date are otherwise
  only inferable from what's on screen. This shifts every grid row down
  by one (`HEADER_LINES = 1`), which the mouse-click math below has to
  account for.
- **Mouse clicks move the cursor.** Enabled via SGR extended mouse mode
  (`\x1b[?1000h\x1b[?1006h` alongside entering the alt screen,
  `\x1b[?1000l\x1b[?1006l` alongside leaving it) -- the older X10 mouse
  protocol encodes coordinates as `32 + n` in a single byte, which caps
  out around column/row 223 and corrupts on any wider terminal; SGR
  reports plain decimal coordinates with no such ceiling, which
  `keyread.py`'s `\x1b[<Cb;Cx;CyM` parsing assumes.
  - `keyread._decode_escape_sequence` had to be generalised from "read
    exactly one byte after `[`" (fine for a bare arrow key, which has no
    parameters) to "read parameter bytes (digits, `;`, `<`) until a
    non-parameter terminator byte" -- this is also what let `PgUp`/
    `PgDn` (`\x1b[5~`/`\x1b[6~`, a parameter byte before the terminator)
    ride the same decoder, not a separate one.
  - A mouse report decodes to `("mouse", button, col, row, pressed)` --
    `pressed` is `final == "M"` (press) vs `"m"` (release); a qualifying
    click is `pressed and (button & 3) == 0 and not (button & 32)`: bare
    left button, not a motion/drag event (bit 5). Anything else (release,
    drag, wheel, right/middle click) is a deliberate no-op, not an error
    -- important, because BEFORE this was handled explicitly, any
    unrecognised escape sequence fell through to `"esc"` and quit the
    whole live view; a release event immediately after every click would
    have made mouse support unusable if that fallback had been left in
    place.
  - `desk_at_click(term_col, term_row, left, pos_to_key, header_lines)`
    is the pure inverse of `_render_lines`'s placement: subtract
    `header_lines` off the 1-based terminal row, add the frame's crop
    `left` to the 1-based terminal column, then look up `(row, col)` in
    `pos_to_key` (`desk_positions` inverted the other way, rebuilt once
    per frame in `_run_live`). Off-grid or empty-space clicks return
    `None`, treated as a no-op same as an out-of-range arrow move.
  - SGR mouse reporting is a real terminal feature (iTerm2, modern
    xterm, Terminal.app, Windows Terminal, kitty, Alacritty all support
    it) but not universal -- a terminal that doesn't understand
    `\x1b[?1006h` simply never sends the sequence, so a click there is
    silently inert rather than broken; nothing in `_run_live` assumes
    mouse support is present.

**Post-3b, round 3 (direct user feedback, still bounded fixes):**

- `_status_line`'s "booked by {occupant}" wording dropped the word
  "booked" -- redundant once a name is right there, since the desk's own
  colour/glyph already says "booked"; the line only needed to add WHO.
  Now just `"{label} -- {occupant} -- ..."`.
- `n`/`p` added as aliases for `PgDn`/`PgUp` (day forward/back) -- not
  every keyboard/terminal makes Page Up/Down comfortable one-handed while
  the other hand is on the arrow keys.
- The move/day/floor/quit key hints are now spelled out on every
  `_status_line` branch (`_NAV_HINT` -- `[↑↓←→] move  [n/p] day  [5/6]
  floor  [q] quit`), not just move/quit. They weren't discoverable
  before -- day- and floor-switching only ever existed in `_run_live`'s
  own docstring.
- The real terminal cursor is now hidden for the live view's whole
  lifetime (`\x1b[?25l` alongside entering the alt screen, `\x1b[?25h`
  restoring it on the way out) -- left visible, it sat blinking at the
  end of the status line and read as an invitation to type, which
  nothing in this loop ever reads.
- The cursor cell now blinks (`Output.blink`, new in `render.py`, SGR
  `\x1b[5m`) layered on top of the existing reverse video -- feedback was
  that reverse alone still wasn't obvious enough. Best-effort: blink
  support isn't universal (some terminals disable it outright) and
  there's no portable way to detect that from here.
- Every redraw now ends with `\x1b[J` (erase from the cursor to the end
  of the screen), not just each line's own `\x1b[K`. A per-line clear
  only ever erases stray characters to the RIGHT of what that frame just
  drew -- it did nothing about a taller PREVIOUS frame leaving extra rows
  BELOW a shorter new one, which a floor switch to a smaller map
  (Level 5's 56 rows vs Level 6's 32, in the shipped fixtures) produces
  every time. `\x1b[J`, not `\x1b[2J` (the full clear already rejected
  above for flickering) -- it only erases what's below the cursor, never
  the frame just drawn.

**Post-3b, round 4 (direct user feedback, still bounded fixes):**

- **Initial cursor position gained a middle rung.** `_initial_cursor_key`
  was "yours" desk, else reading-order-first-visible -- nothing in
  between. Now: yours, else the first FREE desk in the configured
  default group's own preference order (`fs book`'s ranking, resolved via
  `resolve_group_keys` the same tolerant way `fs book`/`fs at` do -- a
  stale config entry warns and drops rather than taking the map down),
  else the original top-leftmost fallback. Landing on a group member
  that's actually booked/restricted on THIS floor/date was considered and
  rejected -- skip it and keep looking rather than start somewhere you
  can't book.
- **The cursor no longer resets on a date change.** `PgUp`/`PgDn`/`n`/`p`
  used to re-run `_initial_cursor_key` after every `_refresh`, which felt
  like losing your place -- the floor (and so `desk_positions`' layout)
  hasn't changed at all on a date change, only which desks are free/
  booked. The cursor now only moves if its desk genuinely isn't in the
  new `desk_positions` (shouldn't happen on an unchanged floor; kept as a
  defensive fallback). A floor switch (digit key) still re-picks, since
  that IS a different set of desks.
- **`"Loading floor..."`** prints once before `cmd_map`'s first fetch
  (cold-cache worst case is 5 uncached `floorplan_booking` calls per this
  module's own docstring) and as an interim frame during a digit-key
  floor switch inside the live view, mirroring the interim `"Loading
  {date}..."` frame PgUp/PgDn already had.
- **Own bookings and the cursor are bolder.** `"yours"` desks are now
  `out.bold` on top of their existing `attention` colour even off-cursor
  -- direct feedback was that a booked-by-you desk blended into every
  other yellow cell. The cursor cell gains the same `out.bold`, layered
  on top of its existing reverse+blink, for the same "still doesn't stand
  out enough" reason blink itself was added for in round 3.
- **Level 5 map data corrections:** the Zone A zone's own internal divider
  partition (leftmost line in that zone) didn't correspond to a real
  partition and is removed. The horizontal 6-desk block about halfway
  down the right wall (`BLOCK_ADJUSTMENTS_BY_FLOOR`'s block 16) sat one
  column right of the groups directly above and below it along that same
  wall and is now shifted left to match.
- **Vertical scroll, keeping the header and status line always on
  screen.** `_render_lines` gained `top`/`bottom` -- a row-axis window
  exactly like `left`/`right`'s column one, sliced the same way but
  computed by REUSING `crop_window` itself rather than a second
  implementation: the function's maths ("how much of one dimension fits,
  keeping a required position in view, preferring the low end") is
  axis-agnostic despite the column-flavoured parameter names. `_run_live`
  computes the grid's available row budget as `rows - HEADER_LINES - 1`
  (the header line and the status/confirm line are outside the crop, so
  they always stay on screen) and follows `cursor_pos[0]` the same way
  the column crop already followed `cursor_pos[1]` -- arrow-key vertical
  movement scrolls for free from the same mechanism. This also fixed a
  second reported symptom that turned out to be the SAME bug wearing
  different clothes: "the cursor disappears when the map scrolls, and
  only reappears once scrolled back to the top" was never the crop logic
  losing track of the cursor (the horizontal follow never did) -- it was
  that NO vertical crop existed at all, so a floor taller than the
  terminal overflowed into the terminal's own native scrollback, which
  has no idea where "the cursor" is and just carries everything
  (including the header line) up and off screen as it goes.
- **Resize mid-session is picked up on the next redraw.** `columns` (and
  now `rows`) used to be measured once, in `cmd_map`, before entering the
  live loop at all, and then reused unchanged for the loop's entire
  lifetime -- a terminal resized mid-session kept getting cropped to
  whatever size it USED to be. `_run_live` takes an injectable `term_size`
  callable, called fresh at the top of every frame; its default just
  replays the fixed `columns`/`rows` it was constructed with (so every
  existing test, none of which pass `term_size`, is unaffected), while
  `cmd_map`'s real call site passes `lambda: terminal_size(sys.stdout)` --
  a new sibling of `terminal_columns` that returns both dimensions --
  so a real resize is live on the very next keypress.
- **Scroll gained run-way and hysteresis; `crop_window` split into a new
  `scroll_window`.** Reported symptom: the cursor still disappeared while
  scrolling, and the map felt like it "doesn't scroll far enough" -- the
  wall the cursor was walking towards never came into view. Root cause:
  `crop_window` is *stateless*, recomputed from only the cursor's current
  position on every single frame, with no memory of the previous window.
  Two consequences fell out of that: the far edge sat exactly one cell
  past the cursor with zero run-way, so the cursor read as pinned to the
  screen edge; and because nothing was remembered between frames, moving
  the cursor even one cell back off that edge collapsed the whole window
  back to its left/top anchor instead of holding position -- there was no
  "stay put until near the opposite edge" behaviour, because staying put
  was never on the table. `scroll_window` (`map_cmd.py`) is the stateful
  fix: it takes the previous frame's `(left, right)` and only moves it
  once the cursor gets within `SCROLL_MARGIN` (3) cells of an edge, and
  then only far enough to restore that margin -- not to recenter. A
  window that hasn't hit either margin doesn't move at all, which is what
  makes "don't scroll back until the cursor nears the opposite edge" true
  for free. `crop_window` itself is untouched and still backs the phase
  1-2/3a static (non-live) render path, which has no per-frame state to
  carry; `_run_live` now keeps `col_window`/`row_window` as loop-local
  state across frames instead of calling `crop_window` fresh each time.
  A resize (window width no longer matching the current terminal size) is
  detected and re-anchors around the cursor with margin run-way rather
  than reusing a `prev` sized for the old terminal.

## What happens on an unfamiliar building (no customisation tables)

This came up as a direct question ("what if the user works in a
different building?") before it came up as a bug, so it's worth
recording the answer even though nothing was broken: `render_floor`'s
hand-traced tables (`WALL_BY_FLOOR`, `PARTITIONS_BY_FLOOR`,
`ROOM_BOXES_BY_FLOOR`, `ZONE_NAMES_BY_FLOOR`, `BLOCK_ADJUSTMENTS_BY_FLOOR`,
`ZONE_LABEL_NUDGE_BY_FLOOR`, `CROP_BY_FLOOR`) are all looked up as
`TABLE.get(planid, <empty>)` -- a `planid` none of them know about just
renders with no walls/partitions/room boxes/zone labels, not a crash.
These tables now live in `src/fs_cli/floorplans/example_workplace.py`,
not `map_cmd.py` -- a contributor with a different building's floorplan
copies that module, re-derives its own tables against their images (see
the sections above for how), and points `map_cmd.py`'s import at it.
There's no selection mechanism between multiple such modules yet
(nothing has needed one -- see that package's docstring); building one is
a separate, later piece of work.
The desk placement itself (`desk_centers`/`row_pitch`/`cluster`/
`local_ranks`, this doc's "Rendering approach" section above) has no
per-floor hardcoding at all -- scale is derived from that floor's own
desk geometry every time. Confirmed by literally feeding Level 5's real
desk polygons through `render_floor` once with its real `planid` and
once with a `planid` in none of the tables: identical grid size and
desk placement, structural decoration only difference.

### Generalising floor names/aliases

What WAS hardcoded to this workplace, separately from the rendering
above, was floor *selection*: a module-level `FLOOR_ALIASES` dict
mapping the literal strings `"5"`/`"level5"`/`"6"`/`"level6"` to this
deployment's two planids, plus a matching `_LEVEL_NAME` dict for the
header line's display text, plus `resolve_floor`'s no-bookings fallback
returning the `DEFAULT_PLANID` constant (this workplace's Level 5)
regardless of what building the account actually belongs to.

Replaced with `floor_names(catalog)`/`floor_aliases(names)`
(`map_cmd.py`), derived from each floor's own `Desk.floor` (the
floorplan's real `name` field returned by `floorplan-booking`, e.g.
`"Level 5"` -- already fetched and cached by `catalog.desks()`, no new
API calls):

- `floor_names(catalog)` -> `{planid: name}`, one entry per non-empty
  floor.
- `floor_aliases(names)` -> `{token: planid}`: a floor's name
  lowercased/despaced is always an alias (`"Level 5"` -> `"level5"`);
  if the name's last word is a bare integer, that integer alone is also
  an alias (`"5"`) -- reproduces this workplace's exact two spellings
  while generalising to any building's own floor names.
- `classify_token`/`header_line` take these as parameters now instead
  of reading module globals -- neither function has an opinion of its
  own on what a floor is called.
- `resolve_floor`'s final fallback (no bookings at all) is now
  `catalog.planids()[0]`, falling back to the `DEFAULT_PLANID` constant
  only if the catalog itself has nothing to offer (a genuinely empty
  account) -- the old hardcoded fallback could return a planid that
  doesn't even exist on a different building's floor set.

**Known, accepted gap:** if a single building has two floors that
happen to share the exact same name (e.g. two towers each with a
"Level 5"), both derive the same alias and whichever planid
`catalog.desks()` iterates first silently claims the token -- no error,
just routes to the wrong tower. Not auto-disambiguated on purpose (rare
within one building, and today's code already tolerates other
ambiguity the same way, e.g. `_NoDesk` for a stale desk key) -- if it
ever bites a real deployment, the fix is a small per-building override
table, the same shape as `CROP_BY_FLOOR` etc., not something to build
ahead of a real case.

The live view's digit-key floor switch (`0`-`9`) only ever benefits from
a floor's bare-digit alias, same as before -- a floor name that doesn't
end in a number (e.g. `"Mezzanine"`) is simply unreachable by a single
keypress, an existing limitation `floor_aliases` inherits rather than
introduces (see `_NAV_HINT`'s docstring in `map_cmd.py`).
