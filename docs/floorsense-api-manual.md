# Floorsense internal web API: a working technical manual

Everything learned about `my.floorsense.nz` across two projects
(`floorsense-autobook`, `floorsense-cli`) and several rounds of live
traffic capture and direct experimentation, through **2026-08-21**.
Sufficient to build a new client without redoing the discovery work.

Every claim here says how it was established. **"Confirmed by writing"**
means a live call was made and its response recorded; **"captured live"**
means observed in real traffic; anything reasoned from documentation or from
adjacent evidence says so. That distinction is load-bearing — §8 and §5.2
both record conclusions that were reached, believed, and then overturned,
and in each case the tell was that the evidence had been inferred rather
than exercised.

Floorsense (Smartalock) is a desk/locker booking system. This document
covers the **internal cloud web app** protocol — the one the web UI
itself speaks. There is a separate, unrelated public REST API; see §9,
and do not conflate the two.

---

## 1. Two systems, don't conflate them

| | **Internal web app** (this document) | **Public REST API** (postman.com/floorsense) |
|---|---|---|
| Host | `my.floorsense.nz` | Your org's own master-controller host |
| Path prefix | `/app/...` | bare, e.g. `/booking-create` |
| Auth | Two-cookie session (§3) | `POST /login` → 1h Bearer JWT |
| Body encoding | JSON | `application/x-www-form-urlencoded` |
| Booking-type field | `type` | `bktype` |
| Needs SSO/MFA? | Yes, for the initial session | No — separate credential system |

They share concepts (booking types, `cid`/`planid`/`key` desk
addressing, unix timestamps) but are **not interchangeable** — the field
names differ, and credentials for one do not work on the other. The
public API's credential system appears to be for a different
(service-account / on-prem controller) deployment model and may not be
reachable at all for an SSO-based org.

---

## 2. Architecture in one paragraph

A conventional server-rendered app (Apache, Express-style session
cookies, jQuery front end) at `my.floorsense.nz/app/`, fronted by
company SSO (Okta OIDC, see §4), with sessions held **in memory on a
specific backend node**. Requests are JSON over `/app/*` endpoints,
protected by a CSRF token lifted from a `<meta>` tag. Responses use a
consistent `{"result": bool, ...}` envelope — except when they don't
(§7).

---

## 3. Authentication: the two-cookie session

Every authenticated request needs **both** cookies, together:

```
Cookie: id=s%3A<session-id>.<signature>; MYSLSRV=api-nz-b1
```

### `id`
The actual session identifier. `HttpOnly`, `Secure`, `SameSite=Lax`.
Format is an Express-style signed cookie (`s:` prefix, URL-encoded, dot
signature). Reissued with a fresh 24h `Expires` on every authenticated
response.

Because it is `HttpOnly`, JavaScript (`document.cookie`) **cannot** read
it. Any browser-automation approach must use an API that reaches real
cookie storage — Playwright's `context.cookies()`, a WebView's cookie
store, or DevTools — not JS evaluation in the page.

### `MYSLSRV`
Backend sticky-session / load-balancer affinity cookie. Values look like
`api-nz-a1`, `api-nz-a2`, `api-nz-b1` (NZ API nodes).

> ⚠️ **This cookie cost real debugging time and will cost you the same
> if you skip it.** Sessions live in memory on one specific node.
> `MYSLSRV` is what routes you back to *that* node. Send a perfectly
> valid, freshly issued `id` cookie **without** `MYSLSRV` and you get
> routed to an arbitrary node that has never heard of your session:
>
> ```json
> {"result": false, "message": "not logged in"}
> ```
>
> This is indistinguishable from genuine session expiry by inspection —
> it looks exactly like an auth problem and is actually a routing
> problem. **Always send both cookies.**

### Session lifetime — an absolute ~60–70 minute cap

Confirmed by direct measurement, and this is the single most
consequential operational fact about the system:

- The session dies **~60–70 minutes after login, regardless of
  activity.** It is an absolute cap, not an idle timeout.
- The clinching experiment: fixed 10-minute pings with zero gaps — six
  clean successes (60 min), death on the 7th (70 min). Pinging cannot
  keep it alive.
- The 24h `Expires` on the `Set-Cookie` header is **cosmetic**. It does
  not reflect real lifetime.
- Earlier testing suggested a ~27–35 min *idle* timeout. Those
  measurements were real but measured the wrong thing (idle gaps that
  also happened to fall inside the absolute window). A fixed-interval
  test is what distinguishes the two hypotheses.
- This aligns with the public API docs stating its JWT "lasts 1 hour" —
  plausibly the same underlying mechanism.

**Practical consequence:** no keep-alive strategy works. Staying
authenticated beyond ~an hour requires a *fresh login* — a full
`id`+`MYSLSRV` reissue — which means fresh SSO+MFA. This is the
constraint that killed the phone-based `floorsense-autobook` design
(iOS Shortcuts can't trigger sub-hourly) and that shapes any unattended
automation: **plan around re-login, not around session persistence.**

### CSRF token

A `<meta name="csrf" content="...">` tag embedded in the app's HTML
pages. Fetch `GET /app/` (redirects to `/app/site` when authenticated,
`/app/login` when not — **both carry a valid token**).

Delivery differs per endpoint, and getting this wrong yields a confusing
failure:

| Endpoint kind | How to send the token |
|---|---|
| `GET /app/config`, general XHR | `x-csrf-token` **header** |
| `POST /app/login` (SSO trigger) | `csrftoken` **form field** |
| `POST /app/booking-*` (JSON APIs) | `x-csrf-token` **header** |

So the login POST is the odd one out — same token, different delivery.
Sending it only as a header there returns an HTML `CSRF Check Failed`
page (§7).

Token appears **session-scoped**, not per-request-rotating (unchanged
across ~10 min of requests; differs between login sessions). Re-fetch
each run rather than caching long-term — its behavior over a full
session lifetime is untested.

---

## 4. Login flow (Okta OIDC SSO)

`my.floorsense.nz` delegates auth to the org's Okta tenant via
OAuth2/OIDC authorization-code + PKCE. This section is the complete,
working, non-interactive login — enough to rebuild it without repeating
the discovery. The Okta half of the mechanics (Classic AuthN, push MFA,
`sessionToken` redemption, the client-side-JS hop) is documented in
`okta-auth-manual.md` §2–§4; only the Floorsense-specific parts are
spelled out here.

### 4.0 Reproducing this from scratch

Everything needed, in order. Verified working 2026-08-19 and 2026-08-20.

**Known-good constants for this deployment:**

| | |
|---|---|
| App origin | `https://my.floorsense.nz` |
| Okta org host | `example-corp.okta.com` |
| OIDC client id | `0oadd1b8iaxx2VSdr4x7` (minted by the app; you never send it yourself) |
| Redirect URI | `https://my.floorsense.nz/app/oidc/callback` |
| Session cookies | `id` + `MYSLSRV` — both required (§3) |
| Okta login | `firstname.lastname` — **not** the email (see below) |
| Floorsense form login | `firstname.lastname@example.com` — `login_hint` only |
| Landing URL on success | `https://my.floorsense.nz/app/site` |

**Setup:**

```bash
python3 -m venv .venv
.venv/bin/pip install requests keyring
```

No browser, no Playwright, no Chromium — see §4.3.

**Credentials:** the Okta password goes in the OS keychain via `keyring`,
never inline. Store it once — note that the `--save-to-keychain` flag in
`okta-auth-manual.md` §6 deliberately re-prompts rather than reusing a
stored value, so pass it on the first run only.

**MFA:** every login triggers a real push
(`policy.allowRememberDevice: false` on this org — no "trust this device"
escape hatch). Someone has to tap the device. Sometimes it is a plain
"Yes, it's me"; sometimes it is number-matching, where the script must
print the number for them (`okta-auth-manual.md` §2b). Unattended
operation is therefore off the table — see §11.

**Run:**

```bash
.venv/bin/python 06_browserless_probe.py --username you@example.com
```

**A working run looks like this** — if yours diverges, the step it
diverges at maps to a row in §4.6:

```
POST https://example-corp.okta.com/api/v1/authn
Password OK. Triggering push via .../factors/opfw.../verify
Initial verify status: 200 / MFA_CHALLENGE
  [88s left] status=MFA_CHALLENGE          <- tap the device now
  [72s left] status=SUCCESS
sessionToken acquired.
POST https://my.floorsense.nz/app/login
  -> 302  location: https://example-corp.okta.com/oauth2/v1/authorize?...
GET  https://example-corp.okta.com/login/sessionCookieRedirect?token=...
  -> 302, okta cookies in jar: ['DT','JSESSIONID','proximity_...','sid','xids']
GET  /oauth2/v1/authorize?...&prompt=none          <- the browser-free hop
    302 -> /app/oidc/callback?code=...&state=...
    302 -> /app/site-auth
    302 -> /app/site
    200 https://my.floorsense.nz/app/site
  ended on floorsense host: True
  floorsense cookies now: ['MYSLSRV', 'id']
  body.get('result')=True   <- authenticated API call, no browser involved
```

Total elapsed is dominated by how fast the tap happens; the machine-only
portion is a few seconds.

### 4.1 The chain, captured live

```
GET  /app/                       -> redirects to /app/login
GET  /app/login                  -> HTML, carries csrf meta tag
GET  /app/config?username=<email>    [x-csrf-token header]
POST /app/login                      [form-urlencoded]
  -> 302 to https://<org>.okta.com/oauth2/v1/authorize
        ?client_id=<id>&scope=openid email profile offline_access
        &response_type=code&redirect_uri=https://my.floorsense.nz/app/oidc/callback
        &login_hint=<email>&state=<uuid>
        &code_challenge=<pkce>&code_challenge_method=S256
  ... Okta authenticates the user ...
  -> back to /app/oidc/callback -> /app/site   [id + MYSLSRV now set]
```

`state` and `code_challenge` are **per-attempt** — you cannot replay a
captured authorize URL. You must POST `/app/login` to mint a fresh one
each login.

### 4.2 The two requests that are easy to get wrong

Both were guessed wrong on the first attempt and cost a debugging round.
Real captured values live in
`floorsense-cli/experiments/05_capture_login_request.jsonl`.

**`GET /app/config`** — CSRF token as a **header**:

```
GET /app/config?username=jamie.baker%40example.com
x-csrf-token: <token from the meta tag>
x-requested-with: XMLHttpRequest
referer: https://my.floorsense.nz/app/login
```

**`POST /app/login`** — CSRF token as a **form field**, body
form-urlencoded, **not JSON**:

```
content-type: application/x-www-form-urlencoded
origin: https://my.floorsense.nz
referer: https://my.floorsense.nz/app/login

method=oidc&captchatoken=&csrftoken=<token>&username=<email>&password=&remember=1
```

Same token, two different delivery mechanisms, one endpoint apart.
Sending it as a header on the login POST returns an HTML
`CSRF Check Failed` page (§7), not JSON.

`password` is empty and `captchatoken` is empty: with `method=oidc` the
app never handles credentials itself, it only needs to know who you
claim to be so it can set `login_hint` on the authorize URL.

> ⚠️ **The `username` here is not your Okta username.** Floorsense's form
> takes the full email (`jamie.baker@example.com`) and uses it purely to
> populate `login_hint`; the actual authentication happens at Okta under
> a different login (`jamie.baker`). Passing the email to
> `/api/v1/authn` happens to work on this org because the email is
> configured as a login alias — but the two values are conceptually
> distinct, and code that treats them as one string will break on any org
> without that alias, with a generic `401` as the only clue
> (`okta-auth-manual.md` §2, "Which username?"). A reCAPTCHA
script loads on the login page but was never required in observed flows.

### 4.3 Complete working login — no browser

**Confirmed live 2026-08-20**, end to end: `requests` and `keyring` only,
no Playwright, no Chromium, landing on `/app/site` and returning
`result: true` from a real API call. Reference source:
`floorsense-cli/experiments/06_browserless_probe.py`.

The one hop that used to need a browser — `GET /oauth2/v1/authorize` —
works over plain HTTP once `&prompt=none` is appended. See
`okta-auth-manual.md` §3 for why, and for the wrong turn that made this
look impossible for a while.

Requires `login_with_push_mfa` and `get_password` from
`okta-auth-manual.md` §6 Part A.

```python
import re
import requests
from urllib.parse import quote, urlparse

ORG_HOST = "example-corp.okta.com"
ORIGIN   = "https://my.floorsense.nz"
HOST     = "my.floorsense.nz"

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/151.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def login(app_username, okta_username, password):
    """Returns an authenticated requests.Session. Raises on failure.

    app_username: the email Floorsense's form wants (login_hint only).
    okta_username: the Okta login -- NOT necessarily the same (§4.2).
    """
    # 1. Okta password + push MFA. The human tap happens here, while only
    #    the ~5 min stateToken is ticking; the one-shot sessionToken does
    #    not exist yet, so a slow tap cannot burn it.
    session_token = login_with_push_mfa(okta_username, password)

    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)

    # 2. Scrape the CSRF token from the login page.
    csrf = re.search(r'<meta name="csrf" content="([^"]+)"',
                     s.get(f"{ORIGIN}/app/login").text)
    if not csrf:
        raise RuntimeError("no csrf meta tag on /app/login")
    csrf = csrf.group(1)

    # 3. Mimic the real flow's config lookup (token as a HEADER here).
    s.get(f"{ORIGIN}/app/config", params={"username": app_username},
          headers={"x-csrf-token": csrf, "X-Requested-With": "XMLHttpRequest",
                   "Referer": f"{ORIGIN}/app/login"})

    # 4. Trigger SSO to mint a FRESH authorize URL (token as a FIELD here).
    #    state + code_challenge are stored server-side against this
    #    session's pre-auth `id` cookie, so it must be the same session.
    resp = s.post(
        f"{ORIGIN}/app/login",
        data={"method": "oidc", "captchatoken": "", "csrftoken": csrf,
              "username": app_username, "password": "", "remember": "1"},
        headers={"Origin": ORIGIN, "Referer": f"{ORIGIN}/app/login",
                 "Content-Type": "application/x-www-form-urlencoded"},
        allow_redirects=False,
    )
    authorize_url = resp.headers.get("location", "")
    if "okta.com" not in authorize_url:
        raise RuntimeError(f"no Okta redirect from /app/login: "
                           f"{resp.status_code} {resp.text[:300]}")

    # 5. Redeem the one-shot sessionToken for a reusable Okta session.
    #    quote(..., safe='') matters: redirectUrl's own : and / must be
    #    escaped or Okta won't round-trip it.
    s.get(f"https://{ORG_HOST}/login/sessionCookieRedirect"
          f"?token={quote(session_token)}"
          f"&redirectUrl={quote(authorize_url, safe='')}",
          allow_redirects=False)
    if not any(c.name == "sid" for c in s.cookies):
        raise RuntimeError("Okta rejected the sessionToken (no sid cookie)")

    # 6. The hop that used to need Chromium. prompt=none forces a redirect
    #    instead of the Sign-In Widget, so there is no JS to execute.
    #    requests follows: authorize -> /app/oidc/callback?code=...
    #                   -> /app/site-auth -> /app/site
    resp = s.get(authorize_url + "&prompt=none", allow_redirects=True)

    # 7. Verify: landing URL *and* cookies. Neither alone (§4.4).
    names = {c.name for c in s.cookies if HOST in (c.domain or "")}
    if urlparse(resp.url).netloc != HOST or not {"id", "MYSLSRV"} <= names:
        raise RuntimeError(f"login did not complete — ended at {resp.url}")

    return s
```

The full redirect chain that step 6 walks, captured live:

```
302  /oauth2/v1/authorize?...&prompt=none
  -> https://my.floorsense.nz/app/oidc/callback?code=Mib57oF...&state=f5...
302  /app/oidc/callback
  -> https://my.floorsense.nz/app/site-auth
302  /app/site-auth
  -> https://my.floorsense.nz/app/site
200  /app/site
```

Without `prompt=none` that first hop returns `200` and the Okta Sign-In
Widget's HTML instead — even with a perfectly valid `sid` cookie provably
sent. That is the whole difference.

### 4.4 Verifying login actually succeeded

The login page sets an `id` cookie **before** authentication. Checking
that cookies named `id`/`MYSLSRV` merely exist produces false positives —
this exact mistake declared victory on a run that was still sitting on
the Okta widget.

The values from one real run make it concrete. `POST /app/login` (the SSO
trigger, before any authentication has happened) responds with:

```
set-cookie: id=s%3AyXxunJKyjbFnHXxwoDofa6gqWk9bVrfX...; HttpOnly; Secure; SameSite=Lax
```

and the session that actually works is a **different** value:
`id=s%3ABAv0n9xWaPrc...`. Same name, same cookie slot, useless vs. valid.
Assert on the landing URL as well:

```python
ok = (urlparse(resp.url).netloc == "my.floorsense.nz"
      and {"id", "MYSLSRV"} <= {c.name for c in s.cookies})
```

Strongest check of all is a real authenticated call (§4.5) returning
`result: true`.

### 4.5 Using the session

`login()` hands back the same `requests.Session` it authenticated, so
there is no handoff step — just keep using it until the ~60–70 min cap
(§3):

```python
s = login(app_username, okta_username, get_password(okta_username, False))

# CSRF is session-scoped (§3) and login replaced the session, so the token
# scraped during login is stale. Re-scrape against the authenticated one.
csrf = re.search(r'<meta name="csrf" content="([^"]+)"',
                 s.get(f"{ORIGIN}/app/").text).group(1)
s.headers["x-csrf-token"] = csrf

r = s.get(f"{ORIGIN}/app/booking-list", params={"bktype": "repeat", "days": 30})
data = r.json()            # check it isn't HTML first — see §7
```

Reusing the pre-auth token instead of re-scraping produces the HTML
`CSRF Check Failed` page (§7) — which throws on `.json()` and looks like
a parsing bug rather than an auth one.

**If you need to persist the session** across processes, the cookie pair
is all that matters:

```python
pair = {c.name: c.value for c in s.cookies if "floorsense" in (c.domain or "")}
```

> ⚠️ Store and restore the `id` value **verbatim**. It is an Express
> signed cookie and already contains percent-encoding (`s%3A...`).
> Re-encoding it — which some HTTP clients will do if you route it
> through a URL-encoding path — produces a cookie the server can't
> verify, and you get `{"result": false, "message": "not logged in"}`,
> indistinguishable from expiry.

### 4.6 Per-hop failure modes

| Symptom | Cause |
|---|---|
| No `<meta name="csrf">` in `/app/login` HTML | Got a redirect or an error page instead — check status, and that you requested `/app/login` not `/app/` |
| `POST /app/login` returns `200` HTML instead of `302` | Wrong body encoding (JSON instead of form), or the CSRF token sent as a header rather than a `csrftoken` field |
| HTML page saying `CSRF Check Failed` | Same as above; also see §7 — this response is **not** JSON and will throw if parsed |
| `sessionCookieRedirect` sets no `sid` | `sessionToken` already used or expired — it is one-shot; don't hold it across a prompt |
| `/oauth2/v1/authorize` returns `200` HTML, chain stops on Okta | You omitted `&prompt=none`. Okta rendered the Sign-In Widget, which completes SSO in JavaScript (`okta-auth-manual.md` §3) |
| `/oauth2/v1/authorize?...&prompt=none` redirects with `?error=login_required` | No usable Okta session — `sid` missing, expired, or not sent. Check the outgoing `Cookie` header before theorising |
| Lands on `/app/login` rather than `/app/site` | Session established with Okta but Floorsense rejected the callback — usually a stale/replayed authorize URL |
| Later API calls return `{"result": false, "message": "not logged in"}` | Missing `MYSLSRV` (§3), or a re-encoded `id` value (4.5) |

### 4.7 Discovering the Okta org from just the email

**Confirmed live 2026-08-21.** The Okta org host (`example-corp.okta.com`)
is not derivable from the email domain by any string transformation
(`example.com` mail, unrelated-looking Okta tenant name) — a real user
does not know it and should not have to be asked for it. It doesn't need
asking, either: steps 2–4 of §4.1/§4.3 (CSRF token, `/app/config`, `POST
/app/login`) can be replayed **unauthenticated**, with an empty
`password` field, purely to read the org off the `Location` header of the
resulting redirect:

```
GET  /app/login                              -> csrf meta tag
GET  /app/config?username=<email>            [x-csrf-token header]
POST /app/login                              [form-urlencoded]
  method=oidc&captchatoken=&csrftoken=<token>&username=<email>&password=&remember=1
  -> 302 to https://<org>.okta.com/oauth2/v1/authorize?...
     ^^^^^^^^^^^^^^^^^^^^^^^^^ read the org straight off this, don't follow it
```

No password is ever sent (`method=oidc` means Floorsense never handles
credentials itself — see §4.2), so this triggers no MFA and creates no
real session; the session it does build is throwaway and discarded. A
live, unauthenticated run against the real account returned exactly this:

```
netloc: example-corp.okta.com
```

`fs`'s `auth.discover_okta_org()` is this lookup, factored to share its
request-building (`_mint_authorize_url`) with the real `floorsense_login`
so the two can't drift apart. `cli.first_run` calls it so a brand-new
setup only ever has to ask for the one thing an ordinary user actually
knows: their Floorsense email.

## 5. Endpoints

All under `https://my.floorsense.nz/app/`. JSON bodies. Session cookies
on everything; `x-csrf-token` header on state-changing calls.

### Pages / bootstrap

| Method | Path | Notes |
|---|---|---|
| GET | `/` | → `/site` (authed) or `/login` (not). Source of the CSRF meta tag. |
| GET | `/site`, `/site-floormap` | SPA views |
| GET | `/config?username=<email>` | Pre-login config lookup; `x-csrf-token` header |
| POST | `/login` | SSO trigger, form-urlencoded (§4) |
| GET | `/oidc/callback` | OIDC redirect target; takes `code` + `state`, then 302s to `/site-auth` |
| GET | `/site-auth` | Post-callback hop that establishes the session, then 302s to `/site` |

### Bookings

| Method | Path | Body / params |
|---|---|---|
| GET | `/booking-list` | List own future bookings. **Call it bare** — 0b found the bare call returns the same records as `?bktype=advance&days=30`, while `bktype=repeat` and `bktype=adhoc` return nothing. Envelope: `{"result": true, "type": ..., "info": [...]}` — records are under `info` |
| POST | `/booking-create` | `{"type":"advance","start":<ts>,"key":"<desk>","cid":<n>}` |
| POST | `/booking-release` | `{"bkid":"<id>"}` → `{"result":true,...,"message":"Booking released"}` |
| POST | `/booking-update` | `{"bkid":"<id>","cid":<n>,"key":"<new desk>"}` → `{"result":true,"message":"Booking updated"}` |
| GET | `/booking-summary?tz=Pacific/Auckland&days=15&team=true&friend=true` | **The most useful endpoint in the whole API** — see §5.1 |

`booking-create` takes no `uid` — the owner is implied by the session.

**`booking-update` is atomic** — it moves a booking to a different desk in
one call, and the `bkid` survives the move. **Confirmed by writing**
(2026-08-21, §8), not merely observed. This matters twice over: it removes
the race window a release-then-book sequence opens (someone else taking the
desk you just freed), *and* it is the only rebooking path that cannot trip
the one-booking-per-day group limit (§8) on the re-create. Use it for any
rebooking flow.

### 5.1 `booking-summary` — one call that answers almost everything

**Captured live from the web UI 2026-08-21** (HAR of a real session). Not yet
replayed from a standalone client — that is 0b's job — but the request is a
plain authenticated `GET` with no unusual headers, so there is no reason to
expect it to behave differently.

```
GET /app/booking-summary?tz=Pacific%2FAuckland&days=15&team=true&friend=true
```

This is what the web UI's own dashboard calls, and it collapses what looked
like several separate problems into one request. It returns **your own
bookings, every followed colleague, and all of their bookings, across a
15-day window** — in a single round trip.

```jsonc
{
  "result": true, "type": "response",
  "info": {
    "tz": "Pacific/Auckland",
    "users": [                     // <-- the Following list (see §5.2)
      { "uid": "10000001", "name": "Alex Example", "usertype": "user",
        "desc": "8677",            // extension number, not a job title
        "email": "alex.example@example.com",
        "ugroupid": 10, "privacy": 0, "initials": "AE",
        "friend": true }
    ],
    "days": [                      // <-- exactly `days` entries, from today
      {
        "daystart": 1787227200,    // LOCAL midnight
        "dayfinish": 1787313600,
        "year": 2026, "month": 8, "day": 21,
        "weekday": 5, "dayname": "F",
        "workday": true,           // the server's own working-day calendar
        "bookings": [ ... ],       // YOUR bookings on this day
        "userbookings": {          // everyone else's, keyed by uid
          "28827882": [ { ...booking record... } ],
          "31125061": []           // present-but-empty, not omitted
        }
      }
    ]
  }
}
```

Parameters, as sent by the UI: `tz` an IANA zone name, `days` the window
length, `team=true` and `friend=true` selecting which cohorts to include.
Only `friend=true` produced anything on this org — see §5.2 on `team`.

> ⚠️ **`days` is capped at 15, silently.** `days=30` and `days=15` returned
> **byte-identical bodies** — the same 15 `days` entries spanning the same
> dates (0b, 2026-08-21). The server does not error, does not say it
> truncated, and does not echo the requested width back. A client that asks
> for 30 and trusts the number it asked for will quietly under-report by half
> its window. Count `info.days`, never assume it matches `days`. The cap's
> source is unidentified — see §12.

> ⚠️ **`days=1` returns ZERO `days` entries — confirmed live, 2026-08-21
> account, 2026-08-25.** Not the same bug as the cap above: this is the
> bottom end, not the top. `info.users` still comes back fully populated;
> `info.days` comes back `[]`. `days=2` and up behave correctly and still
> start at today (0b: `1→0 entries`, `2→2 entries` starting today, `3`/`5`/
> `15` likewise). A client that asks for exactly today (`days=1`, the
> natural literal translation of "just today") gets an empty window and
> must not read that as "nothing scheduled" — every date lookup against an
> empty `days[]` is genuinely unanswered, not "no bookings". Ask for at
> least 2 days even when only today is wanted, and read only the entries
> for the day(s) actually needed. `fs_cli`'s `find_cmd.py::_following_rows`
> does this (`days=max(span, 2)`).

**Why this matters for a client:**

- **`fs find` needs one call, not an N+1 fan-out** for `following`: the
  occupant of a followed colleague's desk is directly available --
  `userbookings` gives uid → bookings, `users[]` gives uid → name.
  > ⚠️ **Correction, 2026-08-21 (`at_cmd.py`'s build):** this was originally
  > written to cover `fs at` too, and that's wrong. `userbookings` only ever
  > contains people you follow -- a stranger's desk is invisible to this
  > endpoint entirely, `friend=true`/`team=true` or not. `fs at` has to
  > answer for *anyone* sitting at a desk, so it reads
  > `Catalog.availability()` (`floorplan-booking`'s per-desk `uid`, §5.3)
  > instead, and pays one `/user?uid=` lookup per distinct occupant among
  > the desks actually asked about -- which the fallback framing below
  > undersells: for `fs at`, that lookup *is* the main path, just bounded
  > to a handful of desks rather than the floor. See `DECISIONS.md`.
- **One call answers many dates.** 15 days in one response, versus one
  `floorplan-booking` call per date.
- **`workday` is authoritative.** The server states which days are working
  days, so a client never has to hardcode Mon–Fri or guess public holidays.
- **`daystart` is local midnight**, which is the anchor the booking
  timestamps are expressed against — see below.

> ⚠️ A booking spanning several days appears **under each day it covers**,
> with the same `bkid`. So `start` can be *before* that day's `daystart`.
> Deduplicate by `bkid` before counting anything.

#### The `start` timestamp, derived from real data

This was the single largest unknown for `booking-create` (§12), and the
capture answers it without a single write. Across 58 colleague bookings plus
3 of the signed-in user's own:

| `bktype` | `start` | Duration |
|---|---|---|
| `advance` | **`daystart` + 28800s — local 08:00**, on 56 of 56 single-day records | 8h (53), 9h (3) |
| `adhoc` | the actual arrival time (`+8h11` in the one observed case) | 8h |

So an **advance booking runs local 08:00 → 16:00**, and `start` is
`local_midnight + 8h`, *not* midnight. A client booking a desk should send
that, and `get_desk_group_booking_settings.book_day_start` (§5, Policy) is
almost certainly the 480 that produces it.

Still to confirm by writing (0c probe 1): whether the server *requires* that
exact value or normalises whatever it is given. Derived from observation, not
yet from a write.

#### Check-in vs. sensor — `confirmed` and `occupied` are different things

| Field | Present on | Meaning |
|---|---|---|
| `confirmed` | **68 of 68** records | the user checked in. `false` on every future booking observed |
| `occupied` | **4 of 68** | the desk sensor currently detects a person |
| `occupiedtime` | the same 4 | when the sensor last saw someone |
| `confexpiry` | **never present here** | see §6 — it exists on the `booking-list` record shape |

They are genuinely distinct signals, not duplicates: `occupied` appears only
alongside `occupiedtime` and only on today's records, which is exactly what
a live sensor reading looks like. `confirmed` is on everything, including
future bookings where it is always `false` — so **check-in is meaningful
only for today**, confirming the rendering rule a client should use.

#### Checking in — writing it, `POST /app/booking-confirm`

**Not documented anywhere else in this manual before 2026-08-23, and not in
any JSON response this client reads.** Found by fetching `/app/site`'s own
front-end JS over an authenticated session (`GET
/app/js/onpage-site-desk-v2.js`) and grepping it for "confirm" —
`action_confirm_booking` calls it:

```
POST /app/booking-confirm
     {"bkid": "<id>"}
```

Same shape as `booking-release` — `bkid`, nothing else. **Confirmed live,
2026-08-23**, against the account's own real booking that day:

| Call | Response |
|---|---|
| Confirming an eligible, unconfirmed booking | `{"result": true, "message": "Booking confirmed", "info": {...same record, now "confirmed": true, "conftime": <unix>, "confmethod": 4, "confuid": "<own uid>"...}}`. Persisted — a follow-up `booking-list` read showed `confirmed: true`. |
| Confirming an already-confirmed booking | `{"result": false, "message": "Booking already confirmed"}` — **no `code` at all** |
| Confirming a booking outside its early-activate window | `{"result": false, "message": "Failed to activate selected booking. Error: Booking outside early activate window"}` — also **no `code`** |

Both refusal shapes lack `code`, so §8's `code: 64` classification
(`refusal_kind`) does not apply here — a client branching on `code` alone
would treat these as transport errors rather than business refusals unless
it checks `message` regardless of `code`'s presence.

The web UI's "Confirm Booking" button is gated client-side on two settings
that are **not** returned by `get_desk_group_booking_settings` (§5, the only
settings endpoint this manual documents as a JSON call) — they are baked
into `/app/site`'s own inline HTML instead, as a plain JS literal:

```js
const desk_group_settings = [{"groupid":11,"book_confirm_app":true},
                             {"groupid":11,"book_early_activate":240}];
```

`book_confirm_app` (bool, per desk group) gates whether check-in is offered
at all; `book_early_activate` (minutes, per desk group) is how early before
`start` a booking becomes confirmable, unless it's already `active`. A
client that wants to *pre-empt* the refusal (rather than just surface it)
needs to scrape this HTML — there is no JSON endpoint for it as of this
writing. A client that's fine letting the server refuse doesn't need to:
both refusal shapes above are informative enough to show as-is.

### 5.2 Following / starred users — read and write

**Captured live from the web UI 2026-08-21.** The Following list is fully
scriptable: it can be read, added to, and removed from.

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/booking-summary?...&friend=true` | — | `info.users[]`, each with `friend: true` |
| POST | `/friend-create` | `{"target":"<uid>"}` | `{"result":true,"type":"response","message":"Friend added"}` |
| POST | `/friend-delete` | `{"target":"<uid>"}` | `{"result":true,"type":"response","message":"Friend deleted"}` |

Both writes take the CSRF token as an **`x-csrf-token` header** with a JSON
body — the ordinary `/app/*` convention (§3), not the form-field special case
that `POST /app/login` uses.

Note the vocabulary mismatch worth encoding once in a client: the UI calls it
**following/starring**, the API calls it **friends**.

#### The round trip, proved rather than assumed

The capture happens to contain a clean controlled experiment — three
byte-identical `user-search` calls bracketing one star and one unstar:

| | `booking-summary` users | `user-search` hit for that uid |
|---|---|---|
| before `friend-create` | 22 | no `friend` key |
| after `friend-create` | 23 | `"friend": true` |
| after `friend-delete` | 22 | no `friend` key |

So the two endpoints agree, and the effect is immediately visible in both.
This is what makes `fs team following add/remove` safe to build: it is not
inferred from a plausible-looking endpoint name, it was exercised in both
directions and observed to round-trip.

> ⚠️ **`friend` is absent, not `false`, for a non-friend.** Client code must
> test `hit.get("friend")` and never `hit["friend"]`, which raises for the
> overwhelming majority of users.

#### `user-search` also carries the flag

The same `friend: true` appears on `user-search` hits, so a search result
already tells you who you follow without a second call:

```jsonc
{ "uid": "01488639", "name": "Nathan Example", "initials": "NE",
  "desc": "8367", "friend": true, "future": [] }
```

A hit only carries location fields (`cid`, `key`, `planid`, `floorname`,
`groupid`, `groupname`, `bktype`, `start`, `finish`, `confirmed`,
`occupied`, `occupiedtime`, `active`, `privacy`, `zoneid`) when that person
is **currently seated**; otherwise the record is just identity plus
`future`.

> ### Correction — `future[]` *is* window-bounded
>
> The first reading of this capture concluded that `future` "appears not to
> be populated by the window passed in `start`/`finish`", because every
> `future` array in it was empty. That was wrong, and wrong in an
> instructive way: the capture's `user-search` calls all used a **one-day**
> window (`start` and `finish` 86399s apart), so an empty `future` was the
> correct answer to the question actually being asked.
>
> A direct test (0b, 2026-08-21) settles it. Same query, two windows:
>
> | Window | `future[]` entries |
> |---|---|
> | today only | 0 |
> | 14 days | 3 |
>
> So `future[]` **is** bounded by `start`/`finish`, and one `user-search`
> call answers as many dates as the window spans. The generalisable error:
> an empty array is evidence about the *query*, not about the field.

`future[]` entries are full booking records — `key`, `cid`, `planid`,
`floorname`, `groupid`, `groupname`, `bktype`, `start`, `finish`,
`confirmed`, plus the person's `uid`/`name`/`initials`/`desc` repeated on
each one.

Both endpoints work for "where will this person be": `user-search` is
name-driven and one query deep, `booking-summary` is friend-list-driven and
returns everyone at once. Use `user-search` for `fs find <name>` and
`booking-summary` for `fs find following`. **Not `fs at`** — see the
correction above: `booking-summary` cannot answer "who's at desk X" for
anyone who isn't followed, which is the ordinary case for that command.

#### `team` — the cohort that isn't there

`booking-summary` takes `team=true` alongside `friend=true`, and every one
of the 23 users returned carried `friend: true`. Not one record came back
that was a team member and not a friend, and `GET /booking-list?bktype=team&active=1`
returned `{"result": true, "info": []}`.

That is consistent with the account simply having no teammates rather than
with the feature being absent — the parameter is clearly wired up
server-side. Either way there is nothing to read and no UI to write with, so
a client has nothing to build against. **Treat `team` as unavailable** and
revisit only if a non-empty response ever appears.

#### `tag-list`

`POST /app/tag-list` with `{"name":"<query>"}` is called by the UI alongside
every `user-search`. It returned a **bare `[]`** — not the `{"result":...}`
envelope (§7). Purpose unknown; it produced nothing on this org and nothing
depends on it.

### 5.3 The desk catalog lives in `floorplan-booking`, not `floorplan-list`

**Confirmed live 2026-08-21** (0b, from a standalone Python client).

This was the highest-risk unknown in the build plan — whether a desk catalog
could be built at all — and the answer is yes, but not from the endpoint
whose name suggests it.

`GET /app/floorplan-list` returns **no desk keys whatsoever**. Four
floorplans, each with `planid`, `name`, image geometry, `timezone`, and a
`deskRanges` array of `{startValue, endValue}` string ranges. Useful for
enumerating floors; useless for enumerating desks.

`GET /app/floorplan-booking?planid=N&date=DD/MM/YYYY&start=<ts>&finish=<ts>`
is where the desks are:

```jsonc
{
  "result": true, "type": "response",
  "future_bookings": 1,
  "bookings": {                    // keyed BY DESK KEY, not an array
    "L5.D.28": { ...full booking record, incl. uid + confexpiry... }
  },
  "info": {
    "planid": 3, "name": "Level 5", "timezone": "Pacific/Auckland",
    "imgname": "...", "imgwidth": 3690, "imgheight": 2552,
    "deskpolys": { ... },          // map geometry
    "users": [ ... ],
    "desks": [                     // <-- THE CATALOG
      { "key": "L5.D.187", "cid": 2, "planid": 3, "groupid": 6,
        "book_advance": true,      // <-- see the warning below
        "book_adhoc": true,
        "reservable": true, "reserved": false,
        "desktags": [{"tagid": 5, "name": "Quiet"}],
        "usertags": [...], "status": 25, "eui64": "00124b00...",
        "uid": "", "bkid": "",     // occupant, when there is one
        "nextbkid": "...", "nextbkstart": 1787515200, "nextbkearly": 1787500800 }
    ]
  }
}
```

Observed sizes: planid 3 (Level 5) 262 desks, planid 4 (Level 6) 103, and
planids 1 and 5 zero — those floors exist in `floorplan-list` but carry no
desks. So a catalog build is **one call per planid, skipping the empty
ones**, and `key` + `cid` + `groupid` is exactly what `booking-create`
needs.

`desktags` is a bonus the plan didn't anticipate: desks are tagged `Quiet`,
`Window` and so on, which is a natural selector for a future
`fs book --tag quiet`.

`GET /app/floorplan-today` requires a `planid` — without one it returns
`{"result": false, "message": "Invalid planid"}`.

#### `reserved` means "booked in the window you asked about"

**Established from the captured floorplans (2026-08-21), 365 desks, zero
disagreements.** The field name suggests permanence — a desk *reserved* for
someone — and it does not mean that. On a `floorplan-booking` response,
`reserved` agrees exactly with two other signals:

| Cross-check | Disagreements |
|---|---|
| `reserved` vs. the desk carrying a `bkid` | **0 of 262** on Level 5 |
| `reserved` vs. membership of the response's `bookings` dict | **0 of 262** |

So `reserved: true` is simply "this desk has a booking in the `start`/`finish`
window you passed", and one field is enough to decide whether a desk is free
on a date. Note it covers `fixed` bookings too — 5 of the 247 booked desks on
Level 5 were permanent allocations with `finish: 2147483646` — which is why
`reserved` and "permanently unavailable" look alike at a glance and are not
the same thing.

`reservable`, by contrast, was `true` on all 262 and carried no information
on this deployment.

> ⚠️ **`book_advance` is a permission, not a property of the furniture, and
> 40 of 365 desks did not have it.** A desk can be free,
> `reservable: true`, and still refuse an advance booking with:
>
> ```json
> {"result": false, "code": 64,
>  "message": "You cannot make an advance booking for this desk"}
> ```
>
> This cost two probes on the first 0c run (§8). Any client picking a desk
> to book **must filter on `book_advance`**, not merely on "is it free".
> `book_adhoc` (walk-up) was `true` on every desk observed, so the two
> permissions are independent.
>
> **How much this matters, in numbers.** On the captured day, filtering Level
> 5 on "free" alone leaves 15 desks — of which only **7 are actually
> bookable**. A picker that ignores `book_advance` would therefore choose a
> desk the server refuses **more often than not**, and the user sees that as
> the tool being broken rather than as a permission they don't have.
>
> | Floor | Desks | Free | Free **and** advance-bookable |
> |---|---|---|---|
> | Level 5 (planid 3) | 262 | 15 | **7** |
> | Level 6 (planid 4) | 103 | 9 | 9 |
>
> Level 6 having no restricted desks at all is what makes this easy to miss:
> a client tested only against Level 6 would look correct.

#### What `groupid` and `book_advance` actually mean

From the account holder, who knows the office: **some desks are permanently
reserved and unbookable, and some sit in areas allocated to specific teams —
only members of that team can book those.** `groupid` is that allocation:
observed values include 6 (`OFFICE-Default`, the general pool), 9, 11
(`Team Alpha`) and 15.

This has a consequence worth stating because it changes how the flag should
be read: **`book_advance` is almost certainly evaluated for the calling
user, not baked into the desk.** The request is authenticated, the server
knows which teams you are in, and "40 of 365 desks" is then *your* view of
the floor rather than a global fact. Two people calling `floorplan-booking`
for the same floor on the same day may well get different `book_advance`
values for the same desk.

Not directly confirmed — it would take a second account to test, which is
out of scope — but it is the reading consistent with both the field's
behaviour and how the office actually works. **The safe client behaviour is
identical either way:** treat `book_advance` as authoritative for *this*
session, re-read it with the catalog rather than caching it forever, and
never assume a desk another person can book is bookable by you.

Related: `usertags` on a desk (e.g. `firewarden`, `Zone C`)
and `desktags` (`Quiet`, `Window`) look like the mechanism behind those
allocations, but nothing here has tested how they interact with
`book_advance`.

### Floor plans / availability

| Method | Path | Notes |
|---|---|---|
| GET | `/floorplan-list` | All floors + desk-key ranges per floor |
| GET | `/floorplan-today` | Today's occupancy |
| GET | `/floorplan-booking?planid=N&date=DD%2FMM%2FYYYY&start=<ts>&finish=<ts>` | Desk availability for a date/window |

Note the `date` param is `DD/MM/YYYY`, URL-encoded — unlike everything
else here, which is unix timestamps.

### Lockers

| Method | Path | Notes |
|---|---|---|
| GET | `/res-list` | Locker reservations. **`finish` = expiry.** |
| GET | `/slave-lockerstatus?cid=N` | Aggregate availability by row type |

### Users

| Method | Path | Notes |
|---|---|---|
| GET | `/user?bkid=N&uid=N` | One user's name/email/initials/**`ugroupid`** for a booking. Not a search. |
| POST | `/user-search` | `{"start":<ts>,"finish":<ts>,"name":"<q>","desc":"<q>"}` |

`user-search` returns matched users with current location (`key`,
`planid`, `floorname`, `occupied`, `confirmed`) plus a `future` array of
upcoming bookings — this is the "find a colleague" endpoint.

> **Confirmed live 2026-08-21** (`scripts/probes/user_lookup_probe.py`):
> `/user`'s `bkid` param is **not validated for a self-lookup** — a real
> bkid, `bkid` omitted entirely, `bkid='0'`, and `bkid='1'` all returned
> the identical correct record for the requested `uid` (`ugroupid: 10`
> every time). The doc note "not a search" still holds — it needs `uid`
> — but "for a booking" overstates it: `bkid` can be omitted or dummied
> when the caller already knows its own `uid`. This is what makes
> `Catalog`'s own-`ugroupid` discovery (`PLAN.md` item 1) safe to build
> without needing a real `bkid` in hand first.
>
> The companion unknown — finding **own `uid`** with zero existing
> bookings — is only partially resolved. `user-search` hits carry no
> `email` field, so matching a config's email against a hit doesn't work.
> Guessing a "First Last" display name from the email's local part
> (`jamie.baker` → `Jamie Baker`) DID find a unique match live, agreeing
> with the reference `uid` read off `booking-list` — but the raw Okta
> login string (`jamie.baker`) got zero hits, so the guess has to be the
> *display* name, not the login. This is a workable fallback, not a
> guaranteed one: it depends on the org's display names following
> "First Last", which isn't a protocol guarantee.

### Policy

| Method | Path | Notes |
|---|---|---|
| GET | `/get_desk_group_booking_settings?groupid=N&ugroupid=N` | Booking-window policy |

Returns `{book_day_start, book_duration_type, booking_confirm_mins,
book_repeat_user, book_repeat_max_days, book_advance_mins}` — all
minutes-based. Likely the real source of truth for when the next
booking window opens, rather than guessing empirically.

---

## 6. Data shapes

### Booking record

```jsonc
{
  "key": "D403-02",        // desk key
  "bkid": "71352647",      // booking id
  "uid": "93980719",       // owning user id
  "cid": 3,                // controller id
  "planid": 6,             // floorplan id
  "bktype": "adhoc",       // adhoc | advance | fixed | repeat | virtual
  "start": 1606535017,     // unix ts
  "finish": 1606563817,
  "created": 1606535017,
  "released": 0,           // unix ts when released, 0 if active
  "active": true,
  "confirmed": true,       // has the user physically checked in
  "conftime": 1606535017,
  "confmethod": 1,
  "confuid": "93980719",
  "privacy": false,
  "groupid": 29,
  "releasecode": 0,
  "confexpiry": 0,
  "rbkid": "74412588"      // only if generated from a repeat series
}
```

### Locker reservation record

```jsonc
{
  "resid": "...", "uid": "...", "cid": 2,
  "key": "5-227", "pin": "...",
  "restype": "adhoc",
  "start": 1606535017,
  "finish": 1640000000,    // <-- expiry
  "released": 0, "confirmed": true, "planid": 6
}
```

### Booking record as returned by `booking-list` (confirmed live 2026-08-21)

```jsonc
{
  "bkid": "00853044", "uid": "47044577", "cid": 2, "planid": 3,
  "key": "L5.D.216A", "bktype": "advance", "groupid": 11,
  "start": 1787774400,        // local 08:00 on the booking day
  "finish": 1787803200,       // local 16:00 -- server-computed
  "created": 1786916787, "updated": 1786916787,
  "released": 0, "active": false,
  "confirmed": false,         // not checked in
  "conftime": 0, "confmethod": 0, "confuid": "",
  "confexpiry": 1787779800,   // 09:30 -- auto-release deadline (§8)
  "releasecode": 0, "privacy": false, "zoneid": 0
}
```

Note `active: false` on a future booking — it means "in progress right
now", not "not cancelled". Use `released == 0` to test whether a booking
still stands.

### Desk addressing

A desk is identified by `key` (e.g. `L5.D.216A`, `D403-02`) plus `cid`
(controller id). `planid` identifies the floorplan, `groupid` the desk group
that owns it (§5.3). Key formats vary by site — don't assume a parseable
structure. On this deployment both `L5.D.216A` and `L5.D.187` occur, so even
within one site the trailing letter is optional.

---

## 7. Response envelope and error handling

Success:

```json
{"result": true, "type": "response", "info": {...}}
```

sometimes with a `message` (e.g. `booking-create` →
`{"result":true,"message":"Desk reserved","info":{...}}`).

Failures come in **two structurally different shapes**, and a client
must handle both:

**1. Auth/routing failure — valid JSON:**
```json
{"result": false, "message": "not logged in"}
```

**2. CSRF failure — an HTML page, not JSON:**
```html
<html>...<p>An error has occured. If this persists please contact
support. 'CSRF Check Failed'</p>...
```

> ⚠️ Calling `JSON.parse()` / `resp.json()` on the CSRF failure **throws**.
> Check `Content-Type`, or sniff for a leading `<html`, before parsing.
> A client that assumes JSON will surface a confusing parse error
> instead of the actual problem.

**3. Business-rule failure — JSON with a `code`:**
```json
{"result": false, "code": 64, "message": "Booking update failed: <reason>"}
```

**4. No envelope at all — a bare JSON array.** `POST /app/tag-list` returns
`[]` directly (§5.2). So "is `result` true?" is not a safe universal test:
check the response is a `dict` before reaching for `result`, or a bare array
raises `AttributeError`/`TypeError` somewhere unhelpful.

---

## 8. Server-side business rules

Updated 2026-08-21 from a live write probe
(`floorsense-cli/scripts/probes/write_probe.py`, self-cleaning, verified
against a pre-probe baseline). Each rule below says how it was established.

### Creating a booking — what the server accepts

**Confirmed by writing.** `POST /app/booking-create`:

```json
{"type": "advance", "start": 1787601600, "key": "L5.D.187", "cid": 2}
```
→ `{"result": true, "message": "Desk reserved", ...}`

| Field | Value |
|---|---|
| `start` | **local midnight + `book_day_start`** — 08:00 on this org |
| `finish` | **do not send it.** The server computed 16:00 (start + 8h) on its own |
| `key`, `cid` | from the desk catalog (§5.3) |
| `type` | `advance` |

> ⚠️ **The server stores `start` verbatim — it does not normalise.** Sent
> 08:00, stored 08:00. So the client is responsible for computing the right
> value; sending "some time on that day" is not good enough. Read
> `book_day_start` from `get_desk_group_booking_settings` (§5, Policy)
> rather than hardcoding 480.

**Which group's `book_day_start` applies — answered by accident.** Desks span
`groupid` 5, 6, 9, 11 and 15 on this floor, and the policy endpoint is
per-group, so "which group do I read the booking start from?" looks like a
question a client has to answer before it can book anything. The probe
settled it without meaning to: the `book_day_start: 480` it used came from
the **`groupid=11&ugroupid=11`** settings call, while the desk it
successfully booked, `L5.D.187`, is in **`groupid: 6`**.

So either the value is uniform across desk groups here, or it is governed by
`ugroupid` — the *user's* group — rather than by the desk's. Both readings
point to the same client behaviour: **read the policy once for your own
`ugroupid` and use it for every desk**, rather than fetching per-desk-group
policy before each booking. Confirmed by writing, though not deliberately.

### `confexpiry` is a real auto-release deadline

**Confirmed by writing.** The booking created above came back with
`confexpiry: 1787607000` — **09:30 on the booking day, i.e. `start` + 90
minutes.** Every one of the account's other advance bookings carried a
non-zero `confexpiry` at 09:30 too.

So an advance booking that is not checked into by 09:30 is released. That is
the same class of silent-deadline problem as the locker, and worth surfacing
in any client: `not checked in — auto-releases 09:30`.

Note this 90 minutes does **not** come from `booking_confirm_mins`, which
was `0` on this org.

> ⚠️ **Resolved, 2026-08-23** — not from a new call, from arithmetic
> across the 7 bookings a bare `booking-list` returned (see the
> "Resolved" note under §12). `confexpiry - start` was **exactly 90
> minutes for every one of them**, while `confexpiry - created` varied
> from ~7 to ~10 days depending on how far ahead each booking had been
> made. That rules out `created` as the source and confirms it is a
> fixed 90-minute offset from `start`, applied per-booking regardless of
> when the booking was made — a server-side default that fires when
> `booking_confirm_mins` is `0`, not a value derived from any per-booking
> timestamp. Still open: whether 90 is a hardcoded server constant or a
> per-org setting under some other field name — nothing depends on
> telling those apart.

### The booking window: 10 days, enforced

**Confirmed by writing.** `book_advance_mins: 14400` = exactly 10 days, and
a booking ~60 days out was refused:

```json
{"result": false, "code": 64,
 "message": "Advance bookings cannot be made this far in the future"}
```

So §5's Policy endpoint is the authoritative source, and it agrees with the
empirical result. The *opening time* of each weekly window is still not
pinned down, but is now narrower than it was:

> ⚠️ **Narrowed, 2026-08-23 (`fs book favourite 2/9`, live account, 08:28
> local, 10 days out on an office day).** Succeeded — but this data point
> turned out not to test what it was meant to. The same-day mechanism just
> above (target `start` slipping into the past as the day goes on) can't
> apply here: the target date's own `book_day_start` is 10 days in the
> future no matter what time today's clock reads, so nothing about
> "before/after 08:00 *today*" could have made this attempt fail either
> way. The advance-window boundary is governed by the *date* difference
> (today vs. target ≤ `book_advance_mins`/1440 days), not by minute-precision
> counting from `now`, or it isn't gated on today's `book_day_start` the way
> same-day bookings are — either way, still genuinely open: no run has yet
> caught the edge-date window closed, so the exact moment it flips open
> (midnight local? some other instant on the 10th-prior day?) remains
> untested. Would need a booking attempted on the day *before* a target's
> 10-day mark, then retried the next day, to catch the transition.

### Same-day booking: refused after `book_day_start`, not categorically

**Confirmed by writing, twice.** Every attempt on the current day was
refused with:

```json
{"result": false, "code": 64,
 "message": "Requested booking duration not possible"}
```

Note what the message says: *duration*, not "same day". The first run could
not distinguish the two readings — "same-day is prohibited" versus "your
`start` is simply in the past" — because it ran at ~09:00 and every candidate
it could construct was already behind it.

**The second run (2026-08-21) separated them.** It tried a same-day start of
**16:00 local, comfortably in the future**, and got the identical refusal:

| Same-day `start` attempted | Relative to now | Result |
|---|---|---|
| 16:00 local | **future** | `code: 64` "Requested booking duration not possible" |
| 08:00 local (`book_day_start`) | past | `code: 64`, same message |

So the refusal is **not** an artefact of a stale timestamp. A future start on
the current day is refused just as firmly, which is what the "duration"
wording implies: an advance booking must begin at `book_day_start` and run
its full 8h, and no same-day start satisfies that once the day is underway.

**A client cannot book today** — this was the conclusion at the time, but it
was wrong, and the "only case still untested" line above named exactly the
gap that proved it.

> ⚠️ **Correction, 2026-08-23 (`fs book favourite today`, live account,
> 07:42 local, before `book_day_start`).** A same-day booking succeeded.
> The row confirmed and stayed booked. This reconciles cleanly with the
> two "refused" runs above rather than contradicting them: the client
> always targets `start = book_day_start`. At ~09:00 (first run) and with
> the forced 16:00 attempt (second run), `start` was already behind the
> current time either way `code: 64` fires as "duration not possible". At
> 07:42 — before 08:00 — `start` was still in the future, and the booking
> went through normally. **Same-day booking is possible, but only in the
> window before that day's `book_day_start`.** Both 2026-08-21 probes
> happened to run entirely after that window closed, which is why neither
> caught it. `fs book`'s own design (`today` never offered by the
> no-args/office-days form) is unaffected — this only matters for an
> explicit `fs book <group> today` before 08:00 local.

**Whether the tool's error is *informative* when a booking attempt is out
of bounds matters more here than the exact window edges do** — confirmed
live 2026-08-23, past 08:30:

- **Too far ahead** (`fs book favourite 6/9`, 14 days out): refused
  cleanly — server message "Advance bookings cannot be made this far in
  the future", `fs`'s own hint "That date is past the booking window.",
  exit 9. No ambiguity.
- **A genuinely past date** (`fs book favourite 16/08/2026`, a week
  behind): also refused, exit 9 — the server collapses this to the same
  "Requested booking duration not possible" message as the same-day
  case, so `fs`'s `NO_VALID_SLOT` hint ("today can't be booked") used to
  follow that message rather than the actual date requested, which
  wasn't today. **Fixed 2026-08-23** (`PLAN.md`'s "Next up" item 11):
  `api.py` now carries the requested date through to the hint and says
  "that date has already passed" whenever it's strictly before today,
  keeping the original wording for date == today.

### `code: 64` is a catch-all, not one rule

**Confirmed by writing.** Every refusal above returned `code: 64` with a
different `message`. The manual previously glossed 64 as "desk not permitted
for this booking type"; that is one of its meanings, not its meaning.

| `message` | Actual cause | Is it the user's fault? |
|---|---|---|
| `You cannot make an advance booking for this desk` | the desk's `book_advance` is `false` (§5.3) | no — the client offered a desk it should have filtered out |
| `Requested booking duration not possible` | no valid slot — same-day before opening time, or a genuinely past date | no — `today`/past dates should never have been offered |
| `Advance bookings cannot be made this far in the future` | past the 10-day window (`book_advance_mins`) | no — the window is knowable in advance |
| `User desk limit reached (Group)` | the day already holds a booking in that desk group | **yes** — this is the one a client should surface as a real answer, not swallow |
| `Booking update failed: <reason> (N)` | `booking-update` wrapping any of the above | depends on the wrapped reason |

**Branch on `message`, never on `code` alone.** Five unrelated refusals now
share `code: 64`, and the right client response differs for each: three of
them mean the client offered something it could have known was invalid, one
is a legitimate "you already have a desk that day", and the fifth is a
wrapper you have to look inside. A client that treats 64 as one condition
will either hide a real answer or report a bug in itself as a server
refusal.

### One booking per day per desk group: enforced

**Confirmed by writing, 2026-08-21 (second 0c run).** With the probe's desk
selection corrected, a second `booking-create` on a day that already had a
booking was refused:

```json
{"result": false, "code": 64, "message": "User desk limit reached (Group)"}
```

The conditions matter, because the first run's version of this test proved
nothing (see the correction below). This one held everything else constant:

| | |
|---|---|
| `start` | the *same accepted value* probe 1 used (08:00 local) — not a recomputed one |
| desk | `L5.D.187`, `book_advance: true`, which probe 1 had just successfully booked |
| group | both desks in the same `groupid` |
| day | the same future weekday |

So the only difference between the accepted call and the refused one was
that the day already had a booking. The limit is real, it is scoped to the
desk **group** (the message says so), and a client should expect it rather
than discover it.

### `booking-update` is atomic — confirmed by writing

**Confirmed by writing, 2026-08-21.** `POST /app/booking-update`
`{"bkid": "...", "cid": N, "key": "<new desk>"}` moved a booking from
`L5.D.187` to `L5.D.188` and the **same `bkid` survived the move**:

```
bkid 39280938:  L5.D.187 -> L5.D.188
```

No release, no re-create, no window in which the desk you are leaving is
free for someone else to take. §5's claim now rests on a write rather than
on observation, and rebooking should always use it.

Note the interaction with the rule above: because the day already holds a
booking, **release-then-book is not merely racy here, it is the only path
that can fail outright** — the re-create would be the day's second booking
attempt if anything went wrong in between. `booking-update` sidesteps that
entirely.

> ### Correction — two probes that proved nothing, and a third that nearly did
>
> The first 0c run reported the one-desk-per-day rule as ENFORCED and
> `booking-update` as REFUSED. **Both conclusions were wrong**, and the
> error is worth keeping.
>
> The probe booked desk `L5.D.187`, then tried a second booking on the same
> day using `L5.D.202`, and got `code: 64, "You cannot make an advance
> booking for this desk"`. That reads like a refusal of the second booking.
> It is not — `L5.D.202` has `book_advance: false` (§5.3), so **that desk
> would have been refused as the first booking of an empty day too.** The
> probe never tested the desk limit. `booking-update` then failed against
> the same invalid desk and tested nothing either.
>
> Compounding it: the two desks were in different desk groups (6 and 9),
> and the limit is scoped to the *group* — so even a valid desk would not
> have tested the rule.
>
> The generalisable error is the one this project keeps meeting from the
> other direction: **a failure that matches your hypothesis is not evidence
> for it until you have ruled out the boring explanation.** The refusal
> message was right there and said "this desk", not "this user".
>
> **Two more instances of the same class were caught by reading the fixed
> probe before running it**, and are worth recording because neither was
> the one the fix was aimed at:
>
> * The second-booking probe still computed its own `start` of local
>   midnight. Since the server stores `start` verbatim and refuses an
>   off-slot start with *"Requested booking duration not possible"*, it
>   would have been refused for its timestamp and reported as the desk
>   limit. It now reuses the `start` that was actually accepted.
> * The desk-limit probe and the `booking-update` probe both targeted the
>   same alternate desk, limit first. Had the limit *not* been enforced,
>   that booking would have occupied the desk `booking-update` was about to
>   move onto, and atomicity would have been reported REFUSED for an
>   unrelated reason. The probes now run update-first, which also frees the
>   primary desk for the limit test to aim at.
>
> The pattern across all four: **a refusal only tests the rule you have in
> mind if every other reason for refusal has been eliminated first.** Both
> of the new ones were found by asking "what else could make this call
> fail?" rather than by running it.

### Releasing

**Confirmed by writing.** `POST /app/booking-release` `{"bkid": "<id>"}` →
`{"result": true, "message": "Booking released"}`. A released booking
disappears from `booking-list` immediately.

### Lockers

- Renewal required roughly every **6 months** (release + immediately
  rebook) or the locker is **silently reassigned to someone else**. A real
  deadline with no notification — worth an aggressive proactive warning in
  any client. Booking or releasing a locker requires physical presence, so
  read-only monitoring plus an expiry warning is the practical scope.
- `res-list` carries the expiry as `finish`. It also carries a `pin`, which
  looked alarming until the account holder pointed out that **the lockers at
  this site have no PIN** — the observed `"1234"` is a placeholder the field
  is filled with, not a credential. Worth knowing before writing a redaction
  rule around it, and worth re-checking on any other deployment before
  assuming the same.

## 9. Public REST API (not used, for completeness)

Documented at `postman.com/floorsense`. Runs against your org's own
master-controller host, authenticates via `POST /login`
(username+password) returning a **Bearer JWT valid 1 hour**, takes
`application/x-www-form-urlencoded` bodies, and uses `bktype` where the
internal app uses `type`.

It is a genuinely separate credential system — Okta/SSO users do not
automatically have credentials for it, and it may not be reachable at
all for a cloud-hosted SSO deployment. Investigated and set aside; the
internal app protocol is what an SSO user can actually reach.

---

## 10. Client implementation notes

Distilled from building against this twice:

- **Always send both cookies.** (§3) The failure mode of forgetting
  `MYSLSRV` masquerades as auth expiry.
- **Don't build a keep-alive.** (§3) The ~60–70 min cap is absolute.
  Design for re-login, and for "session probably dead" as a normal state
  rather than an error.
- **Fetch CSRF fresh each run**, from `GET /app/`. Cheap, and avoids
  reasoning about token rotation.
- **Never `.json()` a response without checking it isn't HTML.** (§7)
- **Send `X-Requested-With: XMLHttpRequest` and `Referer: <origin>/app/site`
  on every `/app/*` call, on GET as well as POST.** This is what the web UI
  does, and a missing `Referer` has been observed returning an HTML error
  page where JSON was due — which then surfaces through the §7 trap above as
  a confusing content-type complaint rather than as the real problem. Put
  them at the client's single choke point, not at each call site: a header
  that every call must carry is one that no call should be able to forget.
- **Prefer `booking-update` over release-then-book.** (§5) Atomic, no
  race.
- **Filter on `book_advance` before offering a desk.** (§5.3) "Free" and
  "bookable by you" are different things, and conflating them produces a
  `code: 64` that reads like a server bug.
- **Branch on `message`, not on `code`.** (§8) `code: 64` covers at least
  four unrelated refusals.
- **Never send `finish` to `booking-create`.** (§8) The server computes it.
- **Reach for `booking-summary` first.** (§5.1) It answers "where is
  everyone I follow, over the next fortnight" in one call, including your
  own bookings and the server's own working-day calendar. Building the same
  answer out of `floorplan-booking` + `/user` is an N+1 fan-out that this
  endpoint makes unnecessary.
- **No browser is needed at all** (§4.3). If you ever do drive this from
  a browser, note the cookies are `HttpOnly`: read them via a real cookie
  API, never `document.cookie`. (§3)
- **Session reuse within a sitting** is the practical sweet spot: log in
  once (§4.3), keep the session, and every command in the following
  ~60 min runs with no MFA (§4.5). This is what makes an
  on-demand CLI pleasant even though unattended operation is off the
  table.
- **Capture traffic rather than guessing request shapes.** Both projects
  lost time to invented request formats (JSON vs form-encoding, header
  vs body CSRF). A 5-minute Playwright request-logger against a real
  login answered in one run what several guesses did not.

---

## 11. Security / policy notes

- A stored cookie pair is a **live, reusable authenticated session** for
  the account. Ensure only readable by the current user.
- Automating this reuses your own already-MFA'd session outside the
  browser. It doesn't bypass anyone else's security, but it does
  automate around an interactive MFA control the employer deliberately
  deployed — plausibly a gray area against acceptable-use policy. Worth
  a conscious decision rather than a default assumption.
- Genuinely unattended operation (no human MFA tap) is **not achievable
  client-side** — it would need an IT conversation: a service account,
  or an Okta policy exception. Attempting to engineer around that is
  where the gray area stops being gray.

---

## 12. Still unverified

Everything in §5.1-§5.3 and §8 has now been exercised from a standalone
Python client (0b/0c, 2026-08-21) unless flagged otherwise below. The two
rules that the first 0c run failed to test — the group desk limit and
`booking-update`'s atomicity — were both confirmed by writing on the second
run and have moved into §8.

**Resolved 2026-08-23** (live account, this repo's `fs` CLI, cached session
from an earlier login — see §8's corrections for detail):
- **Whether bare `booking-list` includes TODAY's booking** — yes. A direct
  call the same day a real booking existed returned it, `"active": true`,
  identically shaped to the future rows (`"active": false`). `fs list`'s
  `booking-summary.days[0]` merge for today remains correct and is not
  wrong to keep, just no longer the only way to get today.
- **Whether same-day booking is blocked by an explicit rule** — no explicit
  rule. It is a slot/duration check: `start` must be `book_day_start` and
  still in the future, so same-day booking succeeds before that day's
  `book_day_start` local time and fails after.
- **Where `confexpiry`'s 90 minutes comes from** — a fixed offset from
  `start`, not `created`. See §8's resolution note under "`confexpiry` is
  a real auto-release deadline".

**Genuinely still open:**

| Unknown | Why it is still open |
|---|---|
| Whether `book_advance` is evaluated per user | The reading in §5.3 is consistent with everything seen, but confirming it needs a second account |
| The weekly booking-window opening *time* | **Narrowed 2026-08-23** — same-day's opening/closing moment is now known (`book_day_start`, see below). The 10-day advance window's own opening moment is still unpinned: `book_advance_mins` gives the width, not when a new edge date's window flips open, and no run has yet caught it closed (a live attempt at 08:28 on an already-in-the-future edge date couldn't test it either way — see the "Narrowed" note under "The booking window" in §8). Would need a same-target-date attempt spanning midnight on the 10th-prior day to catch the transition |
| Whether `booking-create`'s `type` accepts `fixed`/`repeat` | `book_repeat_user: false` and `book_repeat_max_days: 0` on this org strongly suggest repeat booking is simply unlicensed here. Low priority |
| Whether CSRF tokens rotate over a full session lifetime | Only tested over short spans |
| What `tag-list` is for | Returns a bare `[]` (§7). Nothing depends on it |
| Whether `booking-summary`'s 15-day cap is fixed | Asking for 30 returned a byte-identical 15-day body (§5.1). `book_repeat_max_days` was `0`, so it is not that. Whether 15 is a server constant, a licence limit, or a per-org setting is unattributed — treat it as a ceiling, not a guarantee |
