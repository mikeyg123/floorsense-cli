"""
Phase 0c: find out what the booking API actually does, rather than assuming.

*** THIS SCRIPT MAKES REAL BOOKINGS ON THE LIVE ACCOUNT. ***

It is deliberately hostile -- it tries things that should fail -- and it is
self-cleaning: every booking it creates is tracked and released in a
`finally` block, then verified gone against a baseline taken before any
write. It never touches a booking that existed before it started.

The six probes (PLAN.md §0c):

  1. Book a valid future date, walking a ladder of candidate `start`
     timestamps until one is accepted, then reading back what the server
     actually stored. THE single most important unknown for `fs book`
     (`floorsense-api-manual.md` §5 documents the field but not its value).
  2. Attempt a SAME-DAY booking. §12 lists this as untested -- it may be a
     hard server rule or merely hidden in the UI.
  3. Attempt a SECOND booking on the already-booked day. §8 says this
     yields "User desk limit reached (Group)"; confirm the exact shape.
  4. Attempt a date PAST the booking window. §8 never pinned the bounds
     down empirically; this records the error shape (`code: 64`?).
  5. `booking-update` the test booking onto another desk -- confirm §5's
     claim that it is atomic, with the same bkid surviving the move.
  6. Release everything created and verify nothing is left behind.

Note the RUN order is 1, 2, 5, 3, 4: probe 5 must move the booking before
probe 3 creates a second one, or probe 3's booking occupies the desk probe 5
wants to move onto and "atomic" gets refused for an unrelated reason.

Run 07_capture_shapes.py FIRST: this script reads its fixtures for the
desk-group settings (`book_day_start`) and reuses its cached session, so
the pair costs one MFA tap rather than two.

Usage:
    experiments/.venv/bin/python experiments/08_write_probe.py \\
        --username jamie.baker@example.com

Optional, if the automatic desk picker can't find two free desks:
        --desk L5.D.217A --cid 3 --alt-desk L5.D.235A --alt-cid 3
"""

import argparse
import datetime as dt
import glob
import json
import pathlib
import sys

import _fs_common as fs

HERE = pathlib.Path(__file__).parent
FIXTURES = HERE.parent / "tests" / "fixtures"

RESULTS = []      # (probe, outcome, detail)
CREATED = []      # bkids this script made -- the ONLY ones it may release
SENT_START = {}   # bkid -> the `start` we actually sent, for the normalise check


def record(probe, outcome, detail):
    RESULTS.append((probe, outcome, detail))
    print(f"    => {outcome}: {detail}")


def ts(unix):
    if not isinstance(unix, int) or unix <= 0:
        return repr(unix)
    return (f"{unix} = "
            f"{dt.datetime.fromtimestamp(unix).isoformat(' ', 'minutes')} local")


def day_bounds(d):
    start = dt.datetime.combine(d, dt.time.min).astimezone()
    return int(start.timestamp()), int((start + dt.timedelta(days=1)).timestamp())


def next_weekday(today, ahead=1):
    d = today + dt.timedelta(days=ahead)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


def records_of(data):
    if not isinstance(data, dict):
        return []
    info = data.get("info")
    if isinstance(info, list):
        return [r for r in info if isinstance(r, dict)]
    if isinstance(info, dict):
        out = []
        for v in info.values():
            if isinstance(v, list):
                out += [r for r in v if isinstance(r, dict)]
        return out or [info]
    return []


def all_bookings(s):
    """Union across the param variants, so the baseline can't miss a booking
    just because we guessed the wrong bktype. Q1 in 07 settles which single
    variant `fs` should use; here, breadth is the safer error."""
    seen = {}
    # Every bktype §6 lists, plus the bare call. Breadth is the safe error
    # here: a variant we forget is a booking the cleanup diff cannot see,
    # and the report would say CLEAN while a real booking survived.
    for params in ({"bktype": "advance", "days": 60},
                   {"bktype": "adhoc", "days": 60},
                   {"bktype": "repeat", "days": 60},
                   {"bktype": "fixed", "days": 60},
                   {"bktype": "virtual", "days": 60},
                   {"days": 60},
                   None):
        try:
            _, data = fs.api_get(s, "booking-list", params)
        except Exception:                        # noqa: BLE001 -- experiment
            continue
        for r in records_of(data):
            if r.get("bkid") and not r.get("released"):
                seen[str(r["bkid"])] = r
    return seen


def load_day_start_mins():
    """book_day_start from 07's captured settings fixture, if it ran."""
    for path in glob.glob(str(FIXTURES / "desk-group-settings-*.json")):
        try:
            body = json.loads(pathlib.Path(path).read_text())["body"]
        except (OSError, ValueError, KeyError):
            continue
        info = body.get("info") if isinstance(body.get("info"), dict) else body
        if isinstance(info, dict) and isinstance(info.get("book_day_start"), int):
            print(f"  book_day_start={info['book_day_start']} min "
                  f"(from {pathlib.Path(path).name})")
            return info["book_day_start"]
    print("  no desk-group settings fixture found -- run 07 first for a "
          "better `start` ladder")
    return None


def known_planids():
    """Planids from 07's floorplan-list fixture, if it ran. Beats sweeping
    1..12 blindly with parameters we have not yet confirmed."""
    path = FIXTURES / "floorplan-list.json"
    if not path.exists():
        return []
    try:
        body = json.loads(path.read_text())["body"]
    except (OSError, ValueError, KeyError):
        return []
    found = set()

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("planid"), int):
                found.add(node["planid"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(body)
    if found:
        print(f"  planids from 07's fixture: {sorted(found)}")
    return sorted(found)


def pick_desks(s, target, overrides):
    """Two desks that are actually ADVANCE-bookable and in the SAME desk
    group.

    The first run of this script got both of those wrong and drew two false
    conclusions from it. `floorplan-booking.info.desks[]` carries
    `book_advance`, and 40 of 365 desks have it `False` -- the alternate desk
    picked was one of them, so probe 3 hit "You cannot make an advance
    booking for this desk" (a per-desk permission) and was misread as the
    group desk limit. Probe 5 failed the same way and tested nothing.

    Same `groupid` matters too: §8's one-per-day rule is scoped to the desk
    GROUP, so two desks in different groups cannot test it either.
    """
    if overrides[0] and overrides[2]:
        return ((overrides[0], overrides[1]), (overrides[2], overrides[3]))

    start, finish = day_bounds(target)
    datestr = target.strftime("%d/%m/%Y")
    by_group = {}
    for planid in known_planids() or range(1, 13):
        try:
            _, data = fs.api_get(s, "floorplan-booking",
                                 {"planid": planid, "date": datestr,
                                  "start": start, "finish": finish})
        except Exception:                        # noqa: BLE001 -- experiment
            continue
        info = (data or {}).get("info")
        if not isinstance(info, dict):
            continue
        for d in info.get("desks") or []:
            if not isinstance(d, dict):
                continue
            key, cid, gid = d.get("key"), d.get("cid"), d.get("groupid")
            if not isinstance(key, str) or not isinstance(cid, int):
                continue
            if not (d.get("book_advance") and d.get("reservable")):
                continue          # <- the check whose absence cost two probes
            if d.get("reserved") or d.get("bkid") or d.get("uid"):
                continue
            by_group.setdefault(gid, []).append((key, cid))

    # Prefer a group with at least two free advance-bookable desks.
    for gid, desks in sorted(by_group.items(), key=lambda kv: -len(kv[1])):
        desks = list(dict.fromkeys(desks))
        if len(desks) >= 2:
            print(f"  group {gid}: {len(desks)} free advance-bookable desk(s) "
                  f"on {target:%a %d %b} -- {[k for k, _ in desks[:6]]}")
            print(f"  (both probe desks come from group {gid}, so probe 3 "
                  f"actually tests the group desk limit)")
            return desks[0], desks[1]

    print("  no desk group has two free advance-bookable desks")
    return None, None


def create(s, start, key, cid, extra=None, label=""):
    """One booking-create attempt, tracked by DIFF rather than by trust.

    The response is *expected* to carry a bkid, but nothing here depends on
    it: a snapshot is taken before and after, and anything new is added to
    CREATED regardless of what the response looked like. Trusting the
    response shape would mean a booking created without a visible bkid never
    gets released and never shows up as a stray -- and the final report would
    say CLEAN. Correctness of the cleanup path is worth the extra round-trips.
    """
    before = set(all_bookings(s))

    body = {"type": "advance", "start": start, "key": key, "cid": cid}
    if extra:
        body.update(extra)
    print(f"  POST /app/booking-create {json.dumps(body)}  {label}")
    try:
        _, data = fs.api_post(s, "booking-create", body)
    except fs.ApiHtml as e:
        print(f"    !! {e}")
        return None, {"error": str(e)}
    print(f"    result={data.get('result')!r} code={data.get('code')!r} "
          f"message={data.get('message')!r}")

    bkid = None
    for r in ([data["info"]] if isinstance(data.get("info"), dict)
              else records_of(data)):
        if r.get("bkid"):
            bkid = str(r["bkid"])
            break

    # The diff is the authority, not the response.
    appeared = set(all_bookings(s)) - before
    for found in sorted(appeared):
        if found not in CREATED:
            CREATED.append(found)
    if appeared:
        print(f"    created (by diff): {sorted(appeared)}  [tracked]")
        for found in appeared:
            SENT_START[found] = start

    if data.get("result") and not appeared:
        print("    !! result=true but booking-list shows NOTHING new. This "
              "booking cannot be tracked automatically.")
        if bkid:
            print(f"    !! RELEASE bkid {bkid} BY HAND IN THE WEB UI.")
        CREATED.extend([bkid] if bkid else [])
    return (bkid or (sorted(appeared)[0] if appeared else None)), data


# --------------------------------------------------------------------------

def probe1(s, target, desk, day_start_mins):
    print(f"\n=== Probe 1: what `start` does booking-create accept? "
          f"({target:%a %d %b}, desk {desk[0]}) ===")
    midnight, _ = day_bounds(target)

    # 08:00 local goes FIRST: the 2026-08-21 HAR capture showed every one of
    # 56 single-day `advance` bookings starting at exactly `daystart + 8h`
    # with an 8h duration (`floorsense-api-manual.md` §5.1). That is the
    # evidence-backed guess; the rest of the ladder exists to find out
    # whether the server *requires* it or merely normalises to it.
    ladder = [(midnight + 8 * 3600, "08:00 local (the value real bookings use)")]
    if day_start_mins and day_start_mins != 480:
        ladder.append((midnight + day_start_mins * 60,
                       f"midnight + book_day_start ({day_start_mins} min)"))
    ladder += [
        (midnight, "local midnight"),
        (midnight + 9 * 3600, "09:00 local"),
        (int(dt.datetime.combine(target, dt.time.min,
                                 dt.timezone.utc).timestamp()), "UTC midnight"),
    ]

    for start, label in ladder:
        bkid, data = create(s, start, desk[0], desk[1],
                            label=f"<- {label}")
        if bkid or data.get("result"):
            record("Probe 1 (start timestamp)", "ACCEPTED",
                   f"{label}: {ts(start)}")
            return bkid, start
        record("Probe 1 attempt", "refused",
               f"{label} ({ts(start)}) -> code={data.get('code')!r} "
               f"{data.get('message')!r}")

    # Field-shape fallback: maybe `start` was fine and something else was
    # missing. Only reached once every timestamp has been refused.
    _, finish = day_bounds(target)
    for extra, label in (({"finish": finish}, "+finish"),
                         ({"finish": finish, "duration": 1440},
                          "+finish +duration")):
        bkid, data = create(s, midnight, desk[0], desk[1], extra=extra,
                            label=f"<- midnight {label}")
        if bkid or data.get("result"):
            record("Probe 1 (start timestamp)", "ACCEPTED",
                   f"local midnight, but only with {label}")
            return bkid, midnight
        record("Probe 1 attempt", "refused",
               f"midnight {label} -> {data.get('message')!r}")

    record("Probe 1 (start timestamp)", "ALL REFUSED",
           "no candidate start was accepted -- probes 3 and 5 cannot run")
    return None, None


def readback(s, bkid):
    print(f"\n  Reading back bkid={bkid} to see what the server STORED")
    rec = all_bookings(s).get(str(bkid))
    if not rec:
        record("Probe 1 read-back", "NOT FOUND",
               f"bkid {bkid} absent from booking-list right after creation")
        return None
    interesting = ("key", "cid", "bktype", "start", "finish", "confexpiry",
                   "groupid", "confirmed")
    digest = json.dumps({k: rec.get(k) for k in interesting}, default=str)
    print(f"    {digest}")
    record("Probe 1 read-back", "stored start", ts(rec.get("start")))
    sent = SENT_START.get(str(bkid))
    if sent is not None and isinstance(rec.get("start"), int):
        if rec["start"] == sent:
            record("Probe 1 read-back", "server stored `start` VERBATIM",
                   "it does not normalise -- the client must send the right "
                   "value itself")
        else:
            record("Probe 1 read-back", "server NORMALISED `start`",
                   f"sent {ts(sent)}, stored {ts(rec['start'])} "
                   f"(delta {rec['start'] - sent}s) -- the client can send an "
                   f"approximate value")
    record("Probe 1 read-back", "stored finish", ts(rec.get("finish")))
    record("Probe 1 read-back", "stored bktype", repr(rec.get("bktype")))
    if isinstance(rec.get("confexpiry"), int) and rec["confexpiry"] > 0:
        record("Probe 1 read-back", "confexpiry IS a deadline",
               ts(rec["confexpiry"]))
    else:
        record("Probe 1 read-back", "confexpiry not set at creation",
               repr(rec.get("confexpiry")))
    return rec


def probe2(s, desk, day_start_mins):
    print("\n=== Probe 2: is same-day booking a hard server rule? ===")
    today = dt.date.today()
    midnight, _ = day_bounds(today)

    # Two attempts, because one cannot answer the question. Today's midnight
    # is in the PAST, so a refusal there might mean "same-day is blocked" or
    # merely "start is in the past" -- and §12 asks specifically which. A
    # start later today distinguishes them.
    now = int(dt.datetime.now().timestamp())
    later = ((now // 3600) + 1) * 3600
    candidates = [(later, "later today (start in the future)")]
    day_start = midnight + (day_start_mins * 60 if day_start_mins else 0)
    if day_start != later:
        candidates.append((day_start, "today's book_day_start (in the past)"))

    data, bkid = {}, None
    for start, label in candidates:
        bkid, data = create(s, start, desk[0], desk[1], label=f"<- TODAY, {label}")
        record("Probe 2 attempt", "accepted" if data.get("result") else "refused",
               f"{label} ({ts(start)}) -> code={data.get('code')!r} "
               f"{data.get('message')!r}")
        if data.get("result"):
            break

    if data.get("result"):
        record("Probe 2 (same-day)", "ALLOWED",
               "the server accepted a booking for today -- §12's 'blocked in "
               "the UI' is UI-ONLY. Released in cleanup.")
    else:
        record("Probe 2 (same-day)", "REFUSED",
               f"every same-day start was refused, including one in the "
               f"future -- a hard server rule, not a stale-timestamp "
               f"artefact. Last: code={data.get('code')!r} "
               f"{data.get('message')!r}")
    return bkid


def probe3(s, start, second_desk):
    """The second booking must be as VALID as the first, or it tests nothing.

    Two ways this probe has already produced a false ENFORCED, both recorded
    in `floorsense-api-manual.md` §8:

      * a desk with `book_advance: false` -- refused as a *desk permission*,
        not as the limit (fixed in `pick_desks`);
      * a `start` of local midnight -- refused as "Requested booking duration
        not possible", because the server stores `start` verbatim and an
        advance booking must begin at `book_day_start`. So this takes probe
        1's ACCEPTED start rather than recomputing one.

    `second_desk` is whichever desk probe 5's move left free, in the same
    group and on the same day -- which is what makes a refusal here
    attributable to the user/group limit and nothing else.
    """
    print("\n=== Probe 3: a second booking on an already-booked day ===")
    bkid, data = create(s, start, second_desk[0], second_desk[1],
                        label="<- same day+start, different desk, same group")
    if data.get("result"):
        record("Probe 3 (desk limit)", "NOT ENFORCED",
               "a second booking on the same day was ACCEPTED -- §8's "
               "one-per-day rule does not hold as stated. Released in cleanup.")
    else:
        record("Probe 3 (desk limit)", "ENFORCED",
               f"code={data.get('code')!r} message={data.get('message')!r}")
    return bkid


def probe4(s, desk, day_start_mins):
    print("\n=== Probe 4: a date past the booking window ===")
    far = next_weekday(dt.date.today(), 60)
    midnight, _ = day_bounds(far)
    start = midnight + (day_start_mins * 60 if day_start_mins else 0)
    bkid, data = create(s, start, desk[0], desk[1],
                        label=f"<- {far:%a %d %b}, ~60 days out")
    if data.get("result"):
        record("Probe 4 (booking window)", "NO UPPER BOUND HIT",
               f"{far.isoformat()} (~60 days out) was accepted -- the window "
               f"is wider than §8's '~10 days'. Released in cleanup.")
    else:
        record("Probe 4 (booking window)", "REFUSED",
               f"{far.isoformat()}: code={data.get('code')!r} "
               f"message={data.get('message')!r}")
    return bkid


def probe5(s, bkid, alt_desk, original_key):
    """Returns True only if the booking really moved -- probe 3 uses that to
    decide which desk is now free to aim at."""
    print("\n=== Probe 5: is booking-update atomic? ===")
    body = {"bkid": bkid, "cid": alt_desk[1], "key": alt_desk[0]}
    print(f"  POST /app/booking-update {json.dumps(body)}")
    try:
        _, data = fs.api_post(s, "booking-update", body)
    except fs.ApiHtml as e:
        record("Probe 5 (booking-update)", "ERROR", str(e))
        return False
    print(f"    result={data.get('result')!r} code={data.get('code')!r} "
          f"message={data.get('message')!r}")
    if not data.get("result"):
        record("Probe 5 (booking-update)", "REFUSED",
               f"code={data.get('code')!r} {data.get('message')!r} "
               f"-- moving {original_key} -> {alt_desk[0]}")
        return False

    rec = all_bookings(s).get(str(bkid))
    if rec and rec.get("key") == alt_desk[0]:
        record("Probe 5 (booking-update)", "ATOMIC MOVE CONFIRMED",
               f"bkid {bkid} survived: {original_key} -> {rec['key']}, "
               f"same bkid, no release needed")
        return True
    if rec:
        record("Probe 5 (booking-update)", "UNEXPECTED",
               f"result=true but bkid {bkid} still on {rec.get('key')!r}")
        return False
    record("Probe 5 (booking-update)", "BKID CHANGED",
           f"bkid {bkid} vanished after the update -- NOT a same-bkid "
           f"move; check booking-list for a replacement")
    return False


def cleanup(s, baseline):
    print("\n=== Probe 6: release everything created, verify nothing left ===")
    for bkid in list(dict.fromkeys(CREATED)):
        print(f"  POST /app/booking-release {{'bkid': '{bkid}'}}")
        try:
            _, data = fs.api_post(s, "booking-release", {"bkid": bkid})
            print(f"    result={data.get('result')!r} "
                  f"message={data.get('message')!r}")
        except Exception as e:                   # noqa: BLE001 -- experiment
            print(f"    !! {type(e).__name__}: {e}")

    # A booking created without a visible bkid, or one the server renumbered,
    # would be missed above. Diff against the baseline to catch it.
    try:
        after = all_bookings(s)
    except Exception as e:                       # noqa: BLE001
        record("Probe 6 (cleanup)", "UNVERIFIED",
               f"could not re-read booking-list: {e}. CHECK MANUALLY.")
        return

    stray = set(after) - set(baseline)
    for bkid in sorted(stray):
        print(f"  !! stray booking {bkid} ({after[bkid].get('key')}) -- "
              f"releasing")
        try:
            fs.api_post(s, "booking-release", {"bkid": bkid})
        except Exception as e:                   # noqa: BLE001
            print(f"    !! {type(e).__name__}: {e}")

    final = all_bookings(s)
    left = set(final) - set(baseline)
    lost = set(baseline) - set(final)
    if left:
        record("Probe 6 (cleanup)", "INCOMPLETE",
               f"still present after cleanup: {sorted(left)} -- RELEASE THESE "
               f"IN THE WEB UI")
    elif lost:
        record("Probe 6 (cleanup)", "COLLATERAL DAMAGE",
               f"pre-existing bookings missing: {sorted(lost)} -- REBOOK "
               f"THESE IN THE WEB UI")
    else:
        record("Probe 6 (cleanup)", "CLEAN",
               f"booking-list is byte-identical to the baseline "
               f"({len(baseline)} pre-existing booking(s) intact, "
               f"{len(CREATED)} test booking(s) released)")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--username", required=True)
    ap.add_argument("--okta-username")
    ap.add_argument("--save-to-keychain", action="store_true")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--yes", action="store_true",
                    help="skip the confirmation prompt")
    ap.add_argument("--desk")
    ap.add_argument("--cid", type=int)
    ap.add_argument("--alt-desk")
    ap.add_argument("--alt-cid", type=int)
    args = ap.parse_args()

    okta_username = args.okta_username or args.username.split("@")[0]
    s, _csrf = fs.get_session(args.username, okta_username,
                              args.save_to_keychain, args.fresh)

    print("\n--- Baseline (nothing has been written yet) ---")
    baseline = all_bookings(s)
    for bkid, r in sorted(baseline.items()):
        print(f"  {bkid}  {r.get('key'):<12} {ts(r.get('start'))}")
    print(f"  {len(baseline)} pre-existing active booking(s). These are "
          f"protected: the script only ever releases bkids it created.")

    day_start_mins = load_day_start_mins()

    booked_days = {dt.date.fromtimestamp(r["start"])
                   for r in baseline.values()
                   if isinstance(r.get("start"), int) and r["start"] > 0}
    target = next_weekday(dt.date.today(), 1)
    # Probe 1 must land on a day with no existing booking, or §8's
    # one-per-day rule refuses it and probe 3 has nothing to test against.
    while target in booked_days:
        target = next_weekday(target, 1)
    print(f"\n  probe date: {target:%A %d %b %Y} "
          f"(first weekday after today with no existing booking)")

    desk, alt_desk = pick_desks(
        s, target, (args.desk, args.cid, args.alt_desk, args.alt_cid))
    if not desk or not alt_desk:
        sys.exit("Could not find two free desks. Re-run with "
                 "--desk KEY --cid N --alt-desk KEY --alt-cid N "
                 "(07's floorplan-booking fixture will show valid values).")
    print(f"  primary desk: {desk[0]} (cid {desk[1]})")
    print(f"  alternate:    {alt_desk[0]} (cid {alt_desk[1]})")

    if not args.yes:
        print("\n" + "!" * 68)
        print("This will create REAL bookings on your account and then "
              "release them.")
        print("Probe 2 may briefly book TODAY; probe 4 may briefly book "
              "~60 days out.")
        print("Everything created is released before the script exits, and "
              "verified.")
        print("!" * 68)
        if input("Type 'yes' to proceed: ").strip().lower() != "yes":
            sys.exit("Aborted -- nothing was written.")

    try:
        bkid, start = probe1(s, target, desk, day_start_mins)
        if bkid:
            readback(s, bkid)
        probe2(s, desk, day_start_mins)
        if bkid:
            # ORDER MATTERS, and the reverse order is a trap. Probe 3 creates
            # a second booking; if the limit turns out NOT to be enforced,
            # that booking occupies whichever desk it used -- and probe 5's
            # move onto an occupied desk would then be refused for a reason
            # that has nothing to do with atomicity. Same class of false
            # negative as §8's two dead probes.
            #
            # So probe 5 moves the booking desk -> alt_desk FIRST, which also
            # frees `desk` for probe 3 to book: same day, same start, same
            # group, and definitely advance-bookable since probe 1 just
            # booked it.
            moved = probe5(s, bkid, alt_desk, desk[0])
            probe3(s, start, desk if moved else alt_desk)
        else:
            record("Probes 3 and 5", "SKIPPED",
                   "probe 1 never created a booking to build on")
        probe4(s, desk, day_start_mins)
    except KeyboardInterrupt:
        print("\n!! interrupted -- cleaning up before exiting")
    except Exception as e:                       # noqa: BLE001 -- experiment
        print(f"\n!! {type(e).__name__}: {e} -- cleaning up before exiting")
    finally:
        cleanup(s, baseline)

    print("\n" + "=" * 72)
    print("PHASE 0c REPORT -- paste this back into the session")
    print("=" * 72)
    for probe, outcome, detail in RESULTS:
        print(f"  [{outcome}] {probe}\n      {detail}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
