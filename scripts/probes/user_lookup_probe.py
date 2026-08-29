"""
Resolve PLAN.md's two open unknowns blocking step 7's `ugroupid` fix.

*** READ-ONLY. This script makes no bookings and no writes of any kind. ***

1. Can **own uid** be found with zero existing bookings? Every capture so
   far had bookings to read `uid` off of (PLAN.md, "Still open"). Tests
   whether `user-search` by the configured email works as a fallback, by
   deliberately NOT reading uid from booking-list first -- pretending this
   is a brand-new account.
2. Does `GET /app/user?bkid=<bkid>&uid=<uid>` need a REAL `bkid`, or does a
   pure self-lookup work with it omitted or dummied? The doc note says
   "not a search", and the only capture on file (`user-lookup.json`) used a
   real bkid, so this is unverified. Tries: real bkid (control, should
   match the known-good fixture), bkid omitted, bkid="0".

Uses the cached session from write_probe.py/07 if one is warm -- costs at
most one MFA tap, and zero if a probe ran recently.

Usage:
    .venv/bin/python scripts/probes/user_lookup_probe.py \\
        --username jamie.baker@example.com
"""

import argparse
import sys

import _fs_common as fs

RESULTS = []


def record(probe, outcome, detail):
    RESULTS.append((probe, outcome, detail))
    print(f"    => {outcome}: {detail}")


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


def probe_uid_via_user_search(s, email):
    """Simulates a brand-new account: find own uid WITHOUT reading it off
    an existing booking. §5.2's user-search hits carry uid/name/initials/
    desc/friend/future -- notably NOT email -- so a fresh account's config
    (which only has an email) can't match a hit by email. The only handle
    available is guessing the display name from the email's local part
    (`jamie.baker` -> `Jamie Baker`) and matching on that instead. If this
    fails to find a unique match, that itself is the finding: user-search
    is not a safe uid-discovery fallback for a brand-new account."""
    print("\n=== Probe A: own uid with zero bookings assumed "
          "(user-search fallback) ===")
    local_part = email.split("@")[0]
    guessed_name = local_part.replace(".", " ").replace("_", " ").title()
    for q in (local_part, guessed_name):
        try:
            _, data = fs.api_post(s, "user-search",
                                  {"name": q, "start": 0, "finish": 0})
        except fs.ApiHtml as e:
            record("Probe A", "ERROR", f"query {q!r}: {e}")
            continue
        hits = records_of(data)
        exact = [h for h in hits
                if str(h.get("name", "")).lower() == guessed_name.lower()]
        if len(exact) == 1:
            uid = exact[0].get("uid")
            record("Probe A", "FOUND",
                   f"query name={q!r} -> {len(hits)} hit(s), exactly one "
                   f"named {guessed_name!r}, uid={uid!r}")
            return uid
        if hits:
            record("Probe A attempt", "ambiguous or no exact match",
                   f"query name={q!r} -> {len(hits)} hit(s): "
                   f"{[h.get('name') for h in hits][:5]}")
        else:
            record("Probe A attempt", "no hits", f"query name={q!r}")
    record("Probe A (own uid via user-search)", "NOT FOUND",
           "guessing the display name from the email local part did not "
           "yield a unique match -- user-search hits carry no email field, "
           "so this is NOT a reliable fallback for a brand-new account")
    return None


def probe_uid_via_bookings(s):
    """The known-working path today, for comparison against Probe A."""
    print("\n=== (reference) own uid via booking-list, for comparison ===")
    try:
        _, data = fs.api_get(s, "booking-list", {"days": 60})
    except fs.ApiHtml as e:
        print(f"    !! {e}")
        return None
    for r in records_of(data):
        if r.get("uid"):
            print(f"    uid from booking-list: {r['uid']!r}")
            return r["uid"]
    print("    no bookings found -- this account genuinely has none right now")
    return None


def probe_user_lookup(s, uid, real_bkid):
    print(f"\n=== Probe B: does GET /app/user need a real bkid? "
          f"(uid={uid}) ===")
    trials = []
    if real_bkid:
        trials.append(("real bkid (control)", {"bkid": real_bkid, "uid": uid}))
        trials.append(("bkid omitted", {"uid": uid}))
        trials.append(("bkid='0'", {"bkid": "0", "uid": uid}))
        trials.append(("bkid='1'", {"bkid": "1", "uid": uid}))
    else:
        trials.append(("bkid omitted (no real bkid available)", {"uid": uid}))
        trials.append(("bkid='0'", {"bkid": "0", "uid": uid}))

    for label, params in trials:
        try:
            resp, data = fs.api_get(s, "user", params)
        except fs.ApiHtml as e:
            record(f"Probe B ({label})", "HTML/ERROR", str(e))
            continue
        info = data.get("info") if isinstance(data, dict) else None
        got_uid = info.get("uid") if isinstance(info, dict) else None
        if data.get("result") and got_uid == uid:
            ugroupid = info.get("ugroupid") if isinstance(info, dict) else None
            record(f"Probe B ({label})", "WORKS",
                   f"status={resp.status_code} -> correct self record "
                   f"(ugroupid={ugroupid!r})")
        elif data.get("result"):
            record(f"Probe B ({label})", "WRONG RECORD",
                   f"result=true but uid={got_uid!r} != requested {uid!r}")
        else:
            record(f"Probe B ({label})", "REFUSED",
                   f"code={data.get('code')!r} message={data.get('message')!r}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--username", required=True,
                    help="the account's email, e.g. jamie.baker@example.com")
    ap.add_argument("--okta-username")
    ap.add_argument("--save-to-keychain", action="store_true")
    ap.add_argument("--fresh", action="store_true")
    args = ap.parse_args()

    okta_username = args.okta_username or args.username.split("@")[0]
    s, _ = fs.get_session(args.username, okta_username,
                          args.save_to_keychain, args.fresh)

    found_uid = probe_uid_via_user_search(s, args.username)
    reference_uid = probe_uid_via_bookings(s)

    if found_uid and reference_uid and found_uid != reference_uid:
        record("Cross-check", "MISMATCH",
               f"user-search found {found_uid!r}, booking-list found "
               f"{reference_uid!r} -- investigate before trusting either")
    elif found_uid and reference_uid:
        record("Cross-check", "AGREE", f"both paths found uid {found_uid!r}")

    uid = found_uid or reference_uid
    if not uid:
        sys.exit("Could not determine own uid by any path -- Probe B "
                 "cannot run. See RESULTS above for what was tried.")

    real_bkid = None
    try:
        _, data = fs.api_get(s, "booking-list", {"days": 60})
        for r in records_of(data):
            if r.get("uid") == uid and r.get("bkid"):
                real_bkid = r["bkid"]
                break
    except fs.ApiHtml:
        pass
    if not real_bkid:
        print("\n  (no real bkid available -- account has no bookings right "
              "now; Probe B's 'control' trial will be skipped)")

    probe_user_lookup(s, uid, real_bkid)

    print("\n" + "=" * 72)
    print("USER LOOKUP PROBE REPORT -- paste this back into the session")
    print("=" * 72)
    for probe, outcome, detail in RESULTS:
        print(f"  [{outcome}] {probe}\n      {detail}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
