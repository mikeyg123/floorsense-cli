# Experiment: browser-less login via Okta's Authentication API

## Outcome (2026-08-20): browser-less login CONFIRMED, all four steps

`requests` + `keyring`, no Playwright, no Chromium. One MFA tap per login
is the only human involvement.

**Correction 1 — "blocked at step 1".** An earlier version concluded from
a browser-traffic capture (`experiments/01_discover_okta.py`) that this
org was OIE-only and the Classic API dead. Wrong: an org's *widget* being
on OIE says nothing about what a direct caller can do. `POST
/api/v1/authn` returns a healthy `200` with a `stateToken` and the factor
list. Note `401 E0000004 "Authentication failed"` is generic — it does
not distinguish a wrong password from a disabled endpoint, and a typo
nearly ended the investigation here.

**Correction 2 — "a browser is required for the SSO handoff".** The
bigger one. `GET /oauth2/v1/authorize` with a valid `sid` returns `200`
Sign-In Widget HTML rather than a `302`, which was diagnosed as
"silent SSO completion is client-side JS, therefore a browser is
mandatory". A Playwright `page.goto()` workaround was built, worked, and
its success was treated as confirmation.

It confirmed nothing. `page.goto()` working proves a browser *can*
complete the hop, not that raw HTTP *can't*.
`experiments/06_browserless_probe.py` tested the alternatives against a
live session, printing the outgoing `Cookie` header to prove `sid` was
actually being sent. Result:

| Variant | Outcome |
|---|---|
| plain `GET /oauth2/v1/authorize`, `sid` provably sent | `200` widget HTML — the symptom is real |
| **`&prompt=none`** | **`302` → `/app/oidc/callback?code=…` → `/app/site-auth` → `/app/site`** |

`prompt=none` is the OIDC parameter meaning "complete silently or fail —
never render UI"; by spec it cannot return HTML, so there is no JS to
execute. Followed by a real `/app/booking-list` call returning
`result: true`.

**Lesson, and it is the same lesson twice:** a working workaround is not
evidence that the thing it works around is necessary. Both wrong turns
here came from treating a successful detour as proof that the direct road
was closed. Test the direct road.

Steps confirmed:

- **Step 1 — password.** `POST /api/v1/authn` → `stateToken` + factors.
- **Step 2 — push MFA.** Poll the push factor's `verify` link;
  `MFA_CHALLENGE` → `SUCCESS` + `sessionToken`. Number-matching (when
  required) is at `_embedded.factor._embedded.challenge.correctAnswer` —
  nested `_embedded`, and absent for the first couple of polls. It is a
  device-state artifact, not required on every run.
- **Step 3 — session exchange.** `sessionCookieRedirect` → reusable Okta
  `sid`; `authorize?...&prompt=none` → Floorsense session. No browser.
- **Step 4 — bot detection.** Nothing observed across runs on two
  separate days. On analysis the concern was largely misconceived: it was
  a hypothesis written before any testing, and one of the three things it
  watched for (a CAPTCHA) cannot occur on the API path at all — CAPTCHA
  is a Sign-In Widget feature. Correct password + a real push approval
  every time are the strongest positive signals a risk engine can see.
  Full reasoning in `okta-auth-manual.md` §7. Downgraded from an open
  experiment to a documented note.

**Net effect on the "drop Playwright" goal:** achieved completely. The
dependency list is `requests` + `keyring`. `04_session_exchange.py` is
kept as the historical browser-based version; `06_browserless_probe.py`
is the one to copy.

**Still open:**
- Sustained-use risk behaviour. Several runs across two days were clean,
  which is a weak signal; high-frequency use is simply unmeasured. Not
  expected to be a problem (§7 of the Okta manual), but unmeasured is
  unmeasured.
- Whether the Okta `sid` outlives the Floorsense session (~60–70 min).
  If it does, a re-login inside that window may need no MFA tap at all —
  just a replay of `authorize?...&prompt=none`. Untested, and the
  cheapest remaining win.

Raw captured request log from the (since-superseded) discovery step:
`experiments/01_discover_okta.requests.jsonl`. Exact `/app/login` request
shape captured live: `experiments/05_capture_login_request.jsonl`.

## Why

Even in the best case from the current SSO-persistence experiment, a full
login is still needed occasionally (first-ever run, and whenever Okta's own
session eventually expires). Playwright works but is a heavy dependency
(~300MB Chromium download) and a worse "hand this to someone else" story.
Okta has a plain REST API for the whole login+push-MFA flow — no browser
needed at all, and it doesn't change the push-to-watch experience.

**To kick off in a future prompt:** "Run the Okta AuthN API experiment,
see okta-authn-api-experiment.md."

## What to test, in order (stop at the first wall)

1. **Is classic AuthN API even reachable for this org?**
   `POST https://<org>.okta.com/api/v1/authn` with `{username, password}`.
   - Gets a normal JSON response with `stateToken` + factors → proceed.
   - Gets blocked/404/forced-widget-redirect → this org uses OIE
     (Identity Engine) or has the embedded API disabled. Stop and fall back
     to Playwright as the only path (already proven viable by the current
     experiment, modulo its result).

2. **Push MFA over the API.**
   `POST /api/v1/authn/factors/{factorId}/verify` with the `stateToken`,
   targeting the Okta Verify push factor. Confirm the push actually lands on
   the Watch (should be identical to normal). Poll the same endpoint every
   ~2s; confirm it flips `MFA_CHALLENGE` → `SUCCESS` with a `sessionToken`
   after tapping Approve.

3. **Exchange for real session cookies.**
   Use the `sessionToken` to establish an actual Okta session (documented
   redirect-based exchange, not just holding the token), then hit
   `my.floorsense.nz` and confirm the Floorsense cookie pair (`id` +
   `MYSLSRV`) comes back same as the browser-based flow always has.

4. **Bot-detection check.** Watch for any step-up challenge, CAPTCHA, or
   risk-based extra prompt that the browser flow doesn't trigger — the raw
   API can look more "scripted" to Okta's risk engine even with valid
   creds+push.

## Outcome → decision

- **All three steps clean** → browser-less becomes the primary login
  mechanism. Drop the Playwright dependency entirely (or demote it to an
  optional fallback). Much lighter `pip install`, no browser popping up
  ever, same push-to-Watch UX.
- **Blocked at step 1 (OIE / API disabled)** → Playwright stays mandatory.
  Whatever the SSO-persistence experiment concluded about unattended
  viability stands as the final answer.
- **Works but flagged at step 4 (extra risk challenges)** → still worth
  having as an option, but not a clean win; would need a fallback to
  Playwright specifically when the API path gets challenged.

## Setup notes

- No new heavy dependency — just `requests`/`httpx`, already assumed in the
  CLI's stack.
- Needs your actual Okta org hostname (the `https://<org>.okta.com` behind
  the `my.floorsense.nz` redirect) — grab it from the login redirect chain,
  either from a HAR already on hand or by watching the address bar during
  the next manual login.
- Credentials for step 1 should come from macOS Keychain via the `keyring`
  package, not typed inline — same conclusion as the current session's
  discussion on credential storage.
