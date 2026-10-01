# Security Policy

## Reporting a vulnerability

Please report security issues through GitHub's **private vulnerability
reporting** instead of a public issue:

1. Go to the [Security tab](https://github.com/mikeyg123/floorsense-cli/security)
   of this repository.
2. Click **"Report a vulnerability"**.

This opens a private advisory visible only to the maintainer until a fix is
ready, so details aren't exposed before a patch is available.

If private reporting isn't enabled or doesn't work for you, open a regular
[GitHub issue](https://github.com/mikeyg123/floorsense-cli/issues) with as
few sensitive details as possible and ask for a private channel to be set up.

## Scope

`fs` authenticates against Okta and Floorsense using credentials you supply,
and caches a short-lived session locally (see `docs/okta-auth-manual.md` and
`docs/floorsense-api-manual.md`). Issues of particular interest:

- Credential or session-token handling (storage, logging, redaction)
- Anything that could leak the Okta password or session cookie, e.g. via
  `--verbose` request logging
- Supply-chain issues in the release build (`scripts/build-release.sh`)

This project is an unofficial, third-party client and is not affiliated with
Floorsense or Okta. Vulnerabilities in those platforms themselves should be
reported directly to their vendors, not here.
