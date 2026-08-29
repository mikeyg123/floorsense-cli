"""Shared bits for the Okta AuthN experiment scripts."""

import getpass
import json
import sys
import time

import keyring
import requests

ORG_HOST = "example-corp.okta.com"
AUTHN_URL = f"https://{ORG_HOST}/api/v1/authn"
KEYCHAIN_SERVICE = "floorsense-okta"

POLL_INTERVAL_S = 2
POLL_TIMEOUT_S = 90


def get_password(username: str, save_to_keychain: bool, aliases=()) -> str:
    """`username` is the OKTA login -- which is not necessarily the email an
    application's own login form asks for. `aliases` lets an older Keychain
    entry (e.g. one saved under the email) still be found."""
    if not save_to_keychain:
        for account in (username, *aliases):
            existing = keyring.get_password(KEYCHAIN_SERVICE, account)
            if existing:
                print(f"Using password from Keychain "
                      f"({KEYCHAIN_SERVICE!r} / {account!r}).")
                return existing

    password = getpass.getpass(f"Okta password for {username}: ")
    if save_to_keychain:
        keyring.set_password(KEYCHAIN_SERVICE, username, password)
        print(f"Saved to Keychain under service {KEYCHAIN_SERVICE!r}.")
    return password


def extract_challenge_number(body):
    """Number-matching challenge (new/unrecognized device): Okta wants you
    to tap the number shown here on their device, not just "Yes, it's me".
    Confirmed
    live: lives at _embedded.factor._embedded.challenge.correctAnswer (note
    the nested _embedded, easy to miss) -- and isn't present on the very
    first poll, only once the device has registered the challenge, so keep
    polling rather than expecting it immediately."""
    factor = body.get("_embedded", {}).get("factor", {})
    challenge = factor.get("_embedded", {}).get("challenge", {})
    return challenge.get("correctAnswer")


def login_with_push_mfa(username: str, password: str) -> str:
    """Full classic-API login: password (step 1) + push MFA incl.
    number-matching (step 2). Returns the sessionToken on success, exits
    the process on any failure or timeout (this is experiment-script code,
    not library code -- fine to be blunt here)."""
    print(f"\nPOST {AUTHN_URL}")
    resp = requests.post(
        AUTHN_URL,
        json={"username": username, "password": password},
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=15,
    )
    body = resp.json()
    if resp.status_code != 200 or body.get("status") != "MFA_REQUIRED":
        print(f"Step 1 (password) failed: {resp.status_code} {body}")
        sys.exit(1)

    state_token = body["stateToken"]
    factors = body["_embedded"]["factors"]
    push_factor = next((f for f in factors if f["factorType"] == "push"), None)
    if push_factor is None:
        print("No push factor found among:", [f["factorType"] for f in factors])
        sys.exit(1)

    verify_url = push_factor["_links"]["verify"]["href"]
    print(f"Password OK. Triggering push via {verify_url}")

    resp = requests.post(
        verify_url,
        json={"stateToken": state_token},
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=15,
    )
    body = resp.json()
    print(f"Initial verify status: {resp.status_code} / {body.get('status')}")

    # Okta returns `_links.next` with name "poll" -- there is no `_links.poll`
    # key. The lookup here used to read "poll", never matched, and silently
    # used the verify_url fallback -- which happens to be the same URL, so it
    # worked by accident. Confirmed against a captured MFA_CHALLENGE body.
    poll_url = body.get("_links", {}).get("next", {}).get("href", verify_url)
    print(f"Polling {poll_url} every {POLL_INTERVAL_S}s.")

    shown_number = False
    dumped_body = False
    number = extract_challenge_number(body)
    if number is not None:
        print(f"\n>>> Number-matching challenge: tap **{number}** on your "
              f"device. <<<\n")
        shown_number = True
    else:
        print("Tap \"Yes, it's me\" on your device (Okta Verify) now.")

    deadline = time.time() + POLL_TIMEOUT_S
    poll_count = 0
    while time.time() < deadline:
        poll_count += 1
        resp = requests.post(
            poll_url,
            json={"stateToken": state_token},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=15,
        )
        body = resp.json()
        status = body.get("status")
        print(f"  [{int(deadline - time.time())}s left] status={status}")

        if not shown_number:
            number = extract_challenge_number(body)
            if number is not None:
                print(f"\n>>> Number-matching challenge: tap **{number}** on "
                      f"your device. <<<\n")
                shown_number = True
            elif poll_count == 5 and not dumped_body:
                # Confirmed live: number-matching is NOT required on every
                # run -- it tracks the Okta Verify device's state, not this
                # client's. A plain approve/deny push never carries a
                # correctAnswer field at all, so this is informational, not
                # an error. Dump once for reference, then stop.
                dumped_body = True
                print("    (no number-matching challenge on this run -- "
                      "plain approve/deny push. Sample poll body:)")
                print(json.dumps(body, indent=2))

        if status == "SUCCESS":
            print("sessionToken acquired.")
            return body["sessionToken"]
        if status not in ("MFA_CHALLENGE", None):
            print(f"\nUnexpected terminal status: {status}")
            print(body)
            sys.exit(1)

        time.sleep(POLL_INTERVAL_S)

    print(f"\nTimed out after {POLL_TIMEOUT_S}s waiting for push approval.")
    sys.exit(1)
