# Okta authentication: a working technical manual

Reusable reference for programmatically authenticating against Okta,
written from live experimentation against a real org (`example-corp.
okta.com`, 2026-08-19) rather than from the docs alone. Vendor-neutral
where possible; org-specific findings are called out as such.

The headline finding, and the reason this document exists: **an Okta
org can run its browser sign-in on Identity Engine (OIE) while still
accepting the "legacy" Classic Authentication API for direct callers.**
The two paths coexist. Assuming otherwise costs you a working
integration.

---

## 1. The two auth paths

| | **Classic AuthN API** | **OIE / Identity Engine** |
|---|---|---|
| Entry point | `POST /api/v1/authn` | `POST /idp/idx/introspect`, then `/idp/idx/identify`, `/idp/idx/challenge`, … |
| Shape | One JSON call per step, `stateToken` threads them | Stateful "remediation" objects; each response tells you the next allowed step |
| State carrier | `stateToken` (~5 min TTL) | `stateHandle` |
| Ends with | `sessionToken` (one-shot, short-lived) | `interaction_code` → OAuth token exchange |
| Scriptability | High — plain REST, trivial to drive from `requests` | Low — designed for the Sign-In Widget to drive |
| Status | Called "legacy", still enabled on many orgs | Current default for new orgs |

Both can be live on the same org simultaneously. Which one the *browser*
uses tells you nothing about which one *you* can use.

### Finding your org host

Everything below needs `<org>.okta.com`, and it is not usually
advertised. Cheapest ways to get it, in order:

1. **From the app's own SSO redirect.** POST the app's login/SSO-start
   endpoint with redirects disabled; the `Location` header is the Okta
   authorize URL, and its host is your org host. This is the same request
   you need for §4 step 2 anyway, so you get it for free.
2. **From the address bar.** Log in manually and watch where the browser
   bounces to before the app loads.
3. **From a HAR capture** of a real login (DevTools → Network → "Save all
   as HAR"), searching for `okta.com`.

Vanity domains exist (`login.company.com` CNAME'd to Okta). If you see
one, use it verbatim — the API paths are identical, and the cookies are
set on that domain.

### How to tell what an org supports

Do **not** infer it from browser traffic. The reliable test is one HTTP
call:

```bash
curl -X POST https://<org>.okta.com/api/v1/authn \
  -H 'Content-Type: application/json' -H 'Accept: application/json' \
  -d '{"username":"user@example.com","password":"..."}'
```

Interpreting the response:

| Response | Meaning |
|---|---|
| `200` + `stateToken` + `_embedded.factors` | Classic works. Use it. |
| `200` + `status: SUCCESS` + `sessionToken` | Classic works, no MFA required. |
| `404` | Endpoint genuinely not exposed for this org. |
| `403` / `E0000038` "not supported" | Classic explicitly disabled. |
| `401` / `E0000004` "Authentication failed" | **Ambiguous — see below.** |

> ⚠️ **`E0000004` is the single biggest trap here.** Okta deliberately
> returns the same generic "Authentication failed" for a wrong password
> *and* for other rejections, leaking nothing about which. During this
> experiment a mistyped password produced `401 E0000004`, which looked
> exactly like "classic is disabled" and nearly ended the investigation
> at the wrong conclusion. **Always retry a `401` with a
> known-correct password before treating it as a capability signal.**

### Why browser-traffic inference fails

Capturing a real SSO login on this org showed: OAuth2/OIDC
authorization-code + PKCE → Okta Sign-In Widget → `POST
/idp/idx/introspect`. Zero calls to `/api/v1/authn`. The natural
reading — "this org is OIE-only, classic is dead" — was **wrong**. A
direct `POST /api/v1/authn` on the same org returned a perfectly
healthy `200` with a `stateToken` and the full factor list.

The lesson generalizes: *what the widget does* reflects a product
decision about the browser experience. *What the API accepts* is a
separate switch. Test the API directly.

---

## 2. Classic AuthN flow, end to end

### Which username?

`/api/v1/authn` wants the **Okta login**, which is frequently *not* the
email address the downstream application asks for. On the org behind this
document the Okta login is `firstname.lastname` while the app's own login
form takes `firstname.lastname@company.com`.

Keep the two identifiers separate in code — same string or not, they
belong to different systems and there is no rule that they match:

```python
okta_username = "jamie.baker"                  # authenticates against Okta
app_username  = "jamie.baker@example.com"     # only ever sent to the app
```

Many orgs configure the email as a **login alias**, so the email often
works too — it did here, which is exactly why the conflation went
unnoticed through several runs. Don't rely on it: an org that hasn't
enabled aliases returns `401 E0000004`, which is the same generic error
as a wrong password (§1), so this misconfiguration is close to
undiagnosable from the response alone.

Keychain entries are keyed by username, so switching from one to the
other silently stops finding a stored password. Look the account up
under both before prompting.

### Step 1 — Primary auth (password)

```python
resp = requests.post(
    f"https://{ORG_HOST}/api/v1/authn",
    json={"username": username, "password": password},
    headers={"Accept": "application/json", "Content-Type": "application/json"},
)
body = resp.json()
# body["status"]      -> "MFA_REQUIRED" | "SUCCESS" | "MFA_ENROLL" | ...
# body["stateToken"]  -> thread this through every subsequent call
```

Response (trimmed, real shape):

```jsonc
{
  "stateToken": "00Hk4I99seoq_...",
  "expiresAt": "2026-08-19T08:29:02.000Z",   // ~5 minutes
  "status": "MFA_REQUIRED",
  "_embedded": {
    "user": { "id": "00uexampleuseridxxxx", "profile": { "login": "..." } },
    "factors": [
      {
        "id": "opfwukoqsu40bRfpW4x7",
        "factorType": "push",                 // Okta Verify push
        "provider": "OKTA",
        "profile": { "name": "Mike's Brain", "platform": "IOS", ... },
        "_links": { "verify": { "href": ".../factors/opfw.../verify" } }
      },
      {
        "id": "ostwukoqssa3xvanO4x7",
        "factorType": "token:software:totp",  // Okta Verify TOTP code
        "_links": { "verify": { "href": ".../factors/ostw.../verify" } }
      }
    ],
    "policy": { "allowRememberDevice": false, ... }
  }
}
```

Notes:
- **`stateToken` expires in ~5 minutes.** Don't persist it between
  process runs; chain steps 1→2 in one execution.
- Never hardcode factor IDs — select from `_embedded.factors` by
  `factorType` and follow `_links.verify.href`. IDs differ per user and
  can change on re-enrollment.
- `policy.allowRememberDevice: false` (as on this org) means there is no
  "trust this device" escape hatch — every run needs a live factor
  challenge.

### Step 2 — Push MFA with polling

Trigger, then poll the *same* endpoint:

```python
resp = requests.post(verify_url, json={"stateToken": state_token}, headers=H)
# -> status "MFA_CHALLENGE", factorResult "WAITING"

while True:                       # poll every ~2s, cap ~60-90s
    resp = requests.post(poll_url, json={"stateToken": state_token}, headers=H)
    status = resp.json()["status"]
    if status == "SUCCESS":       # -> body["sessionToken"]
        break
    if status != "MFA_CHALLENGE": # REJECTED / TIMEOUT / etc.
        fail()
    time.sleep(2)
```

The poll URL comes from the challenge response's `_links`. The real
shape, captured live:

```jsonc
"_links": {
  "next":   { "name": "poll", "href": ".../api/v1/authn/factors/<id>/verify" },
  "resend": [ { "name": "push", "href": ".../verify/resend" } ]
}
```

There is **no `_links.poll` key** — it is `_links.next`, whose `name` is
`"poll"`, and whose `href` equals the `verify` URL in practice. Code that
looks up `_links["poll"]` matches nothing and silently falls through to
whatever default it was given; if that default is the verify URL the
whole thing still works, by accident. (Exactly this happened in the
experiment scripts here and went unnoticed for the same reason.) Read
`_links.next.href`, keep the verify URL as the fallback, and don't
mistake "it works" for "the lookup matched".

`factorResult` values seen: `WAITING` → then `SUCCESS`. Also possible:
`REJECTED` (user denied), `TIMEOUT` (push expired).

### Step 2b — Number-matching challenge (easy to miss)

For new/unrecognized devices, Okta Verify shows **three numbers on the
device** and the user must tap the one the login screen displays. Since
there is no login screen here, **your script must print it.**

It lives at:

```
_embedded.factor._embedded.challenge.correctAnswer     # -> e.g. 23
```

Two traps, both hit during this experiment:

1. **The nested `_embedded`.** It is `factor._embedded.challenge`, not
   `factor.embedded.challenge`. An earlier guess used the latter,
   silently extracted nothing, and left the user staring at a device
   prompt with no number to match.
2. **It is not present on the first poll.** The initial `verify`
   response and the first poll response omit it entirely; it appears
   ~2 polls in, once the device has registered the challenge. Code that
   checks once and gives up will never see it.

Robust extraction:

```python
def extract_challenge_number(body):
    factor = body.get("_embedded", {}).get("factor", {})
    return factor.get("_embedded", {}).get("challenge", {}).get("correctAnswer")

# check on EVERY poll iteration until found, not just the first response
```

**UX note:** prompt with *"tap 'Yes, it's me' on your device"* and
*"tap **N** on your device"*. Don't say "your Watch" — Okta Verify runs
on phones, watches, and desktops.

**It is not required on every run.** Observed directly: the first API
login prompted number-matching; a later login from a fresh client (new
browser context, new `DT` cookie, `allowRememberDevice: false`) got a
plain approve/deny push with no `correctAnswer` field anywhere in the
poll bodies. So it tracks the *Okta Verify device's* enrollment state,
not the calling client's. Handle both: display the number when it
appears, prompt for a plain approval when it doesn't, and never block
waiting for a field that may never arrive.

### Step 3 — `sessionToken` → real session cookie

The `sessionToken` from step 2 is **one-shot and short-lived**. It is
not a session; it is a bearer voucher you redeem for one.

Redeem it via the documented redirect endpoint:

```
GET https://<org>.okta.com/login/sessionCookieRedirect
      ?token=<sessionToken>
      &redirectUrl=<url-encoded destination>
```

This responds `302` and sets the real Okta session cookies:

| Cookie | Role |
|---|---|
| `sid` | The actual Okta session identifier |
| `xids` | Companion session cookie, set alongside `sid` |
| `DT` | Device token — feeds device recognition / risk scoring |
| `JSESSIONID` | Java app-server session |
| `idx` | Identity Engine state (present even when you authed via Classic) |
| `proximity_<hash>` | Device-proximity/risk signal |

That `idx` cookie appearing after a *Classic* login is a good
illustration of the coexistence: the org runs both stacks, and a
Classic-issued session is still a first-class session to the OIE side.

---

## 3. The `/oauth2/v1/authorize` trap — and `prompt=none`

The symptom that costs a day, and the one-parameter fix.

Having redeemed a `sessionToken` and holding a valid `sid` cookie, the
obvious next move is to request the OIDC `/oauth2/v1/authorize` URL and
follow the redirects to the app's callback. Over raw HTTP it appears to
fail:

```
GET /oauth2/v1/authorize?client_id=...&code_challenge=...   [valid sid sent]
  -> 200 OK, Sign-In Widget HTML          # NOT a 302 to the callback
```

The page it returns is the Okta Sign-In Widget bootstrapping an Identity
Engine interaction — its HTML contains `runLoginPage`, the `okta-sign-in`
bundle, and a fresh `stateToken`. On the default path, Okta really does
hand SSO completion to **client-side JavaScript**: the widget loads,
observes the existing session, and performs the redirect itself. A raw
HTTP client executes no JS, so the chain dead-ends on a `200` that looks
exactly like a login prompt while your session is in fact perfectly good.

### The fix: `prompt=none`

Add the standard OIDC parameter that means *"complete this silently or
fail — never render UI"*:

```
GET /oauth2/v1/authorize?client_id=...&code_challenge=...&prompt=none
  -> 302 https://app.example.com/callback?code=<code>&state=<state>
```

By specification this can only return a redirect: a code on success, or
`?error=login_required` if there is no usable session. There is no HTML
path, so there is nothing for JavaScript to do, and a plain HTTP client
walks the whole chain. **Confirmed live (2026-08-20)**, both the redirect
and a subsequent authenticated API call.

`prompt=none` also fails *better*: without a session you get an explicit
`login_required` error instead of a login page you have to detect by
sniffing HTML.

> ### Correction — how this was got wrong first
>
> The original conclusion here was "silent SSO completion is client-side
> JS, therefore a browser is required for this hop," and a Playwright
> `page.goto()` was built to work around it. That workaround **does**
> work, and its success was treated as confirmation.
>
> It confirmed nothing. `page.goto()` succeeding proves a browser *can*
> do it, not that raw HTTP *can't* — and the alternatives were never
> tested. When they finally were, in one probe against a live session
> with the `sid` cookie provably present in the outgoing `Cookie` header,
> `prompt=none` completed the chain with no browser at all and the
> ~300MB dependency evaporated.
>
> The generalisable error: **a working workaround is not evidence that
> the thing it works around is necessary.** Before accepting "X is
> impossible without Y," check the protocol for the feature designed for
> exactly your case. OIDC had one.

---

## 4. Non-interactive login, end to end

Everything above assembled into one chain. Pure HTTP — `requests` and
`keyring`, no browser, no browser engine, nothing headless.

### What this does and does not eliminate

- **Gone:** the browser dependency entirely. No Chromium download, no
  Playwright, no window, no page navigation, no typing or clicking.
- **Still there:** one physical MFA approval tap on the user's device,
  because the org requires a live factor challenge
  (`policy.allowRememberDevice: false` here means device trust never
  accumulates).

So the login is fully scriptable and runs anywhere Python does. It is
*not* unattended: somebody taps a phone once per login. For most CLI
purposes that is the right trade — the session then lasts as long as the
app allows, and every call inside that window is plain HTTP.

### Prerequisites

```bash
python3 -m venv .venv && .venv/bin/pip install requests keyring
```

That is the whole dependency list.

### Token lifetimes, and why the step order is what it is

The order below is not stylistic. Someone tidying this code will be
tempted to move step 2 earlier (it looks like setup); doing so breaks
the flow intermittently, in a way that only shows up when the user is
slow to tap.

| Token | Lifetime | Covers |
|---|---|---|
| `stateToken` | ~5 min | steps 1a → 1b (password → MFA) |
| `sessionToken` | **one-shot**, short-lived | step 4 only |
| authorize URL (`state` + `code_challenge`) | per-attempt | steps 2 → 5 |
| Okta `sid` cookie | hours (org policy) | reusable — see below |

- The **human wait** — potentially 30s+ while the user finds their
  device and taps — happens during the `stateToken` window, which is the
  only token with slack in it.
- The `sessionToken` does not exist until *after* the tap. That is
  deliberate: human latency can never burn it.
- Once you have it, steps 2 → 4 must run uninterrupted. Don't prompt for
  anything, don't `sleep`, don't mint the authorize URL early and hold it.
- The **`sid` cookie is reusable**, which is what makes the whole thing
  cheap to test: one tap yields an Okta session you can drive as many
  authorize exchanges through as you like.

### The chain

Throughout, `s` is a plain `requests.Session()`. Its cookie jar handles
the cross-domain hops (Okta → app) correctly on its own.

**1. Authenticate over the Classic AuthN API** (§2) — password, then push
MFA with polling and number-matching. Yields a one-shot `sessionToken`.

**2. Mint a *fresh* SP-initiated authorize URL.** Trigger the target
application's own "start SSO" endpoint with redirects disabled, and read
the `Location` header:

```python
resp = s.post(app_sso_start, data={...}, allow_redirects=False)
authorize_url = resp.headers["location"]   # https://<org>.okta.com/oauth2/v1/authorize?...
```

> ⚠️ **Never replay a captured authorize URL.** `state` and the PKCE
> `code_challenge` are generated per attempt and stored server-side
> against the app's pre-auth session cookie. A URL copied from an earlier
> capture, or from a previous run, will be rejected or dead-end. Mint a
> new one every login.

The exact request shape here is application-specific: endpoint, body
encoding, CSRF delivery, and which fields must be present all vary.
**Capture it, don't guess** — both projects behind this document lost a
debugging round to invented request shapes (JSON where the server wanted
form-encoding; a CSRF token in a header where it wanted a form field).

Two ways to capture, cheapest first:

- **HAR export** — DevTools → Network → perform a real login →
  "Save all as HAR" → read the login POST's headers and body. Note HAR
  files contain live cookies and tokens; treat them as credential
  material.
- **A request logger** — run a real login under Playwright with
  `page.on("request", ...)` dumping method, URL, headers, and
  `post_data` to a file. (Useful once, for discovery; not needed at
  runtime.)

**3. Build the session-cookie exchange URL.** Both query parameters need
URL-encoding, and `redirectUrl` needs `safe=''` so its own `:` and `/`
are escaped too:

```python
from urllib.parse import quote

exchange_url = (
    f"https://{ORG_HOST}/login/sessionCookieRedirect"
    f"?token={quote(session_token)}"
    f"&redirectUrl={quote(authorize_url, safe='')}"
)
```

**4. Redeem it, with redirects disabled.** This converts the one-shot
token into a real, reusable Okta session:

```python
resp = s.get(exchange_url, allow_redirects=False)
# -> 302, and Set-Cookie contains `sid` (plus xids, DT, JSESSIONID, idx...)
assert any(c.name == "sid" for c in s.cookies)
```

Check `sid` actually landed. If it didn't, the token was already used or
has expired, and every later hop fails in a way that looks like something
else's fault.

**5. Complete the handoff with `prompt=none`** (§3) — the step that
removes the browser:

```python
resp = s.get(authorize_url + "&prompt=none", allow_redirects=True)
# 302 -> /callback?code=...&state=...  -> app session established
```

`requests` follows the whole chain: Okta issues the code, the app's
callback exchanges it using its stored PKCE verifier, and the app sets
its own session cookies.

**6. Verify, then harvest.**

```python
ok = (urlparse(resp.url).netloc == APP_HOST
      and set(REQUIRED_COOKIES) <= {c.name for c in s.cookies})
cookies = {c.name: c.value for c in s.cookies if APP_HOST in (c.domain or "")}
```

Assert on **both** the landing URL and the cookies — see §5 for why
cookie names alone are a false positive. Better still, make a real
authenticated API call and check it succeeds.

Because everything runs in one `requests.Session`, there is no harvest
step needed at all if you simply keep using `s`.

## 5. Verification discipline

Two false conclusions were reached and corrected during this
experiment. Both are worth guarding against by default.

**1. Don't infer API capability from browser behavior.** (§1.) Test the
endpoint.

**2. Don't treat the presence of a cookie *name* as proof of login.**
An early version of the step-3 script checked only that cookies named
`id` and `MYSLSRV` existed, and declared success. They did exist — as
stale *pre-authentication* values the app sets on its login page. The
login had not completed. The fix is to assert on something that can only
be true after success:

```python
ok = (page.url.startswith(APP_ORIGIN)          # actually landed on the app
      and page.url != login_url                # not still on the login page
      and {"id", "MYSLSRV"} <= cookie_names)   # AND has the cookies
```

Better still, assert the cookie *value changed* from its pre-auth value,
or make an authenticated API call and check it succeeds.

**3. When a redirect chain stalls, walk it by hand.** Following hops
one at a time with `max_redirects=0`, printing status + `Location` +
`Set-Cookie` per hop, is what localized the JS problem to a specific
hop. Transparent redirect-following hides exactly the information you
need.

---

## 6. Reference implementation

Two parts. **Part A** is pure HTTP (`requests` + `keyring`) and covers
§2 — password, push MFA, number-matching — returning a `sessionToken`.
**Part B** covers §4 — turning that token into real application session
cookies. No browser in either part.

### Part A — Okta authentication (no browser)

```python
import getpass, time, keyring, requests

ORG_HOST = "your-org.okta.com"
AUTHN_URL = f"https://{ORG_HOST}/api/v1/authn"
KEYCHAIN_SERVICE = "myapp-okta"


def get_password(username, save_to_keychain=False):
    """Interactive by default; --save-to-keychain opts into persistence."""
    if not save_to_keychain:
        existing = keyring.get_password(KEYCHAIN_SERVICE, username)
        if existing:
            return existing
    password = getpass.getpass(f"Okta password for {username}: ")
    if save_to_keychain:
        keyring.set_password(KEYCHAIN_SERVICE, username, password)
    return password


def extract_challenge_number(body):
    factor = body.get("_embedded", {}).get("factor", {})
    return factor.get("_embedded", {}).get("challenge", {}).get("correctAnswer")


def login_with_push_mfa(username, password):
    """Returns a one-shot sessionToken. Raises on failure."""
    H = {"Accept": "application/json", "Content-Type": "application/json"}

    body = requests.post(AUTHN_URL, json={"username": username,
                                          "password": password},
                         headers=H, timeout=15).json()
    if body.get("status") != "MFA_REQUIRED":
        raise RuntimeError(f"unexpected status: {body.get('status')} {body}")

    state_token = body["stateToken"]
    push = next(f for f in body["_embedded"]["factors"]
                if f["factorType"] == "push")

    body = requests.post(push["_links"]["verify"]["href"],
                         json={"stateToken": state_token},
                         headers=H, timeout=15).json()
    # `_links.next` (name: "poll"), NOT `_links.poll` — see §2 step 2.
    poll_url = body.get("_links", {}).get("next", {}).get("href") \
        or push["_links"]["verify"]["href"]

    shown = False
    deadline = time.time() + 90
    while time.time() < deadline:
        body = requests.post(poll_url, json={"stateToken": state_token},
                             headers=H, timeout=15).json()
        status = body.get("status")

        if not shown:                       # check EVERY poll, not just once
            n = extract_challenge_number(body)
            if n is not None:
                print(f">>> Tap {n} on your device. <<<")
                shown = True

        if status == "SUCCESS":
            return body["sessionToken"]
        if status != "MFA_CHALLENGE":
            raise RuntimeError(f"MFA failed: {status} {body}")
        time.sleep(2)

    raise TimeoutError("no MFA approval within 90s")
```

Credential storage: prompt interactively by default, with an explicit
opt-in flag to persist to the OS keychain (`keyring` → macOS Keychain,
Windows Credential Locker, Secret Service on Linux). Never inline
secrets in source or shell history.

> Note the branch in `get_password` above: passing `save_to_keychain`
> **skips the keyring lookup entirely** and always re-prompts. That is
> intentional — the flag means "capture and store a password", so it has
> to ask for one — but it surprises people who expect a
> `--save-to-keychain` flag to be idempotent. Run it once with the flag,
> then never again.

### Part B — `sessionToken` → application session cookies

Continues from Part A, in the same `requests.Session`. `app_sso_start` /
`app_sso_start_form` are application-specific (§4 step 2); everything
else is generic Okta.

```python
from urllib.parse import quote, urlparse


def exchange_for_app_session(s, session_token, app_host, app_sso_start,
                             app_sso_start_form, required_cookies):
    """Complete SSO into the app. Returns {cookie_name: value}, or raises.

    `s` is a requests.Session already used for the app's pre-auth requests
    (the app stores its PKCE state against that session's cookie).
    """
    # 2. Mint a FRESH authorize URL (never replay a captured one).
    resp = s.post(app_sso_start, data=app_sso_start_form,
                  allow_redirects=False)
    authorize_url = resp.headers.get("location", "")
    if "okta.com" not in authorize_url:
        raise RuntimeError(f"no Okta redirect from SSO start: "
                           f"{resp.status_code} {resp.text[:300]}")

    # 3-4. Redeem the one-shot token for a real (reusable) Okta session.
    exchange_url = (f"https://{ORG_HOST}/login/sessionCookieRedirect"
                    f"?token={quote(session_token)}"
                    f"&redirectUrl={quote(authorize_url, safe='')}")
    s.get(exchange_url, allow_redirects=False)
    if not any(c.name == "sid" for c in s.cookies):
        raise RuntimeError("sessionToken not accepted (already used or "
                           "expired) — no sid cookie was set")

    # 5. THE hop. prompt=none makes Okta redirect instead of rendering the
    #    Sign-In Widget, so no JavaScript — and no browser — is involved.
    resp = s.get(authorize_url + "&prompt=none", allow_redirects=True)

    # 6. Verify the landing: URL *and* cookies, never either alone.
    names = {c.name for c in s.cookies}
    if urlparse(resp.url).netloc != app_host or not set(required_cookies) <= names:
        raise RuntimeError(f"SSO did not complete — ended at {resp.url}")

    return {c.name: c.value for c in s.cookies if app_host in (c.domain or "")}
```

The returned dict is a **live authenticated session** for the account.
Treat it as credential material: don't log it, and if you cache it to
disk, `chmod 600`. Usually you don't need it at all — just keep using
`s`, which already holds the cookies.

### Putting it together

Parts A and B in one runnable script. The only app-specific values are
the four constants at the top:

```python
import argparse

APP_HOST         = "app.example.com"
APP_SSO_START    = f"https://{APP_HOST}/login"       # the SSO-trigger endpoint
REQUIRED_COOKIES = ("session", "SRVAFFINITY")        # what "logged in" looks like
APP_SSO_FORM     = {"method": "oidc", "username": ""}  # captured, not guessed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--username", required=True,
                    help="the identifier the APP's login form wants")
    ap.add_argument("--okta-username",
                    help="the Okta login, if it differs (§2)")
    ap.add_argument("--save-to-keychain", action="store_true",
                    help="prompt for the password and store it; run once")
    args = ap.parse_args()

    # Okta's login is not necessarily the app's (§2, "Which username?").
    okta_username = args.okta_username or args.username.split("@")[0]
    password = get_password(okta_username, args.save_to_keychain,
                            aliases=(args.username,))

    # Human tap happens here, inside the ~5 min stateToken window.
    session_token = login_with_push_mfa(okta_username, password)

    # From here on: no prompts, no sleeps — the sessionToken is one-shot.
    s = requests.Session()
    s.get(APP_SSO_START)          # warm up: the app sets its pre-auth cookie
    form = {**APP_SSO_FORM, "username": args.username}
    cookies = exchange_for_app_session(
        s, session_token, APP_HOST, APP_SSO_START, form, REQUIRED_COOKIES,
    )
    print(f"authenticated: {sorted(cookies)}")
    return s              # keep using it — the session IS the credential


if __name__ == "__main__":
    main()
```

First run (stores the password, then prompts for the MFA tap):

```bash
.venv/bin/python login.py --username you@example.com --save-to-keychain
```

Every run after that — no password prompt, just the tap:

```bash
.venv/bin/python login.py --username you@example.com
```

**Expected output of a working run**, in order: `MFA_CHALLENGE` while
polling → a push on the device → `SUCCESS` with a `sessionToken` → a
`302` from `sessionCookieRedirect` with `sid` in `Set-Cookie` → a `302`
from `authorize?...&prompt=none` carrying `code=` → the app's callback →
the cookie names printed. If any of those is missing, §8 maps the symptom
to the cause.

---

## 7. Risk, bot detection, and how you look to Okta

### Is there a bot check to worry about?

The concern that direct API auth "looks scripted" to Okta's risk engine
is intuitive and, on the evidence, unfounded for the ordinary case. It
was carried as an open question through this project's experiments and
never produced a single observation. It is worth stating plainly why,
because the reasoning matters more than the null result.

**One correction to the question itself:** watching for a CAPTCHA on the
API path is incoherent. CAPTCHA in Okta is a **Sign-In Widget** feature —
a browser-rendered challenge. A raw API caller cannot be shown one. If
Okta wants to stop you it returns an error code, not a puzzle.

**What actually exists:**

| Mechanism | What it does | Triggered by |
|---|---|---|
| **ThreatInsight** | IP reputation; flags or blocks IPs with credential-stuffing history. Operates on `/api/v1/authn` specifically | The IP's reputation — not your client library |
| **Behavior detection / risk scoring** | Scores new device, new IP, new geo, improbable travel; feeds sign-on policies that can force step-up or deny | Novelty in the *authentication context* |
| **Rate limits** | Per-org, per-endpoint caps (hundreds/min) | Volume |
| **Account lockout** | After N **failed** password attempts | Failures, not attempts |

**Why a legitimate scripted login scores well.** Risk engines evaluate
the authentication, not the HTTP client. A script using stored correct
credentials plus a real push approval generates the strongest positive
signals available:

- Correct password every time — no failed-attempt accumulation, which is
  the primary credential-stuffing signature.
- A physical MFA approval on an enrolled device on **every** login. This
  is the heaviest positive signal there is; risk engines exist to compel
  exactly this, and you are volunteering it unprompted.
- Stable IP and geography, no improbable travel.
- Volume orders of magnitude below any rate limit.

The one imperfect signal: a fresh process doesn't persist the `DT`
device-token cookie, so you look like a new device each login. On an org
with `allowRememberDevice: false` this costs nothing — device trust never
accumulates for anyone. Persisting `DT` between runs is an option if you
want to reduce novelty, not a requirement.

**Observed:** several runs across two separate days, zero step-up
challenges, zero errors, zero lockouts. A light test — treat sustained
high-frequency use as unmeasured.

### If you ever do trip something

The shapes to recognise, since several are ambiguous:

- `LOCKED_OUT` as the `status` — a real Classic AuthN state, unmistakable
- `E0000047` — rate limit exceeded
- A `403` where you expected a challenge — the likely shape of a
  ThreatInsight block (exact code unconfirmed; don't hardcode one)
- A step-up policy surfaces as an unexpected `status` in the state
  machine, not as an error
- `E0000004` — the usual trap. Wrong password *and* several other
  rejections. Never read it as a bot-detection signal without first
  retrying with a known-good password (§1)

### Don't spoof the User-Agent

A plain `requests` client identifies itself as `python-requests/x.y`.
**Leave it that way.**

Changing it to mimic Chrome is a deliberate step to look like something
you are not — that is detection evasion, and it changes the character of
the work from "scripting my own login" to "disguising it". Nothing about
an honest client needs hiding: you authenticate as yourself, with your own
password, approving each login on your own device. Every `/api/v1/authn`
call lands in Okta's System Log either way, and a `python-requests` entry
with a clean MFA approval is a far better thing for an admin to find than
a forged browser string.

(Browser-ish `Accept` / `User-Agent` headers on the *application's* own
endpoints are a different matter — those are content negotiation, getting
HTML where HTML is expected, not concealment.)

### Policy, which is the real constraint

Automating around an interactive MFA control an employer deliberately
deployed can sit in a gray area against acceptable-use policy, even when
you only ever authenticate as yourself with your own credentials and your
own approval tap. That is a conversation to have deliberately, not a
default assumption — and it, rather than any detection mechanism, is the
actual limit on this approach.

## 8. Quick diagnostic table

| Symptom | Likely cause |
|---|---|
| `401 E0000004` on `/api/v1/authn` | Wrong password — **retry before concluding anything else** |
| `403 E0000038` | Classic AuthN genuinely disabled for the org |
| `404` on `/api/v1/authn` | Endpoint not exposed; OIE-only org |
| `E0000011` invalid token | `stateToken` expired (~5 min) — restart the flow |
| Poll never leaves `MFA_CHALLENGE` | User hasn't approved; or number-matching is required and you never displayed the number |
| `correctAnswer` always `None` | Wrong path (needs nested `_embedded`), or only checked the first response |
| `/oauth2/v1/authorize` returns `200` HTML with a valid `sid` | You omitted `prompt=none`. Okta rendered the Sign-In Widget, which completes SSO in JavaScript. Add `&prompt=none` (§3) |
| `/oauth2/v1/authorize?...&prompt=none` returns `302` with `?error=login_required` | `prompt=none` working as designed — there is no usable Okta session. The `sid` cookie is missing, expired, or wasn't sent |
| `sessionCookieRedirect` returns widget HTML instead of a `302`, no `sid` in `Set-Cookie` | The `sessionToken` was already redeemed or has expired — it is one-shot. Don't hold it across a prompt or a sleep (§4) |
| Landing poll times out despite a valid `sid` being set | The authorize URL was stale or replayed. `state`/`code_challenge` are per-attempt — mint a fresh one via the app's SSO-start endpoint (§4 step 2) |
| Widget HTML returned and no `sid` in the outgoing `Cookie` header | The request is simply unauthenticated — a login page is the correct response. Fix the cookie jar before theorising about JavaScript |
| App reports "not logged in" despite fresh cookies | Missing a companion cookie (load-balancer affinity etc.) — check the app's own requirements |
