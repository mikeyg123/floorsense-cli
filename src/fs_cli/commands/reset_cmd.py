"""`fs reset [--full]` -- wipe discovered/session state, not user-authored
config.

Clears `session.json` (cookies) and `cache.json` (desk catalog, policy,
locker cache) -- everything server-derived, which rebuilds itself with
no user effort. `cache.json`'s actual name is origin-scoped
(`config.cache_filename`), so a `--url` session's reset only ever
touches its own deployment's cache. Leaves `config.toml`'s
`[preferences]`/`[groups]`/`[teams]` alone -- hand-authored via `fs
desks`/`fs team`/`fs office-days`.

`[identity]` (`okta_user`, `email_domain`, `okta_org`) is user-typed-once
rather than server-discovered, so the default `fs reset` leaves it alone
too. `--full` also clears it and forgets the keychain password -- a full
"pretend this machine never ran `fs` before".

No `plan.py` pipeline -- one irreversible action, so this borrows
`plan.confirm`'s guard shape (refuse to prompt under `--json`/non-
interactive without `--yes`) rather than its table machinery.
"""

import sys

from .. import auth
from .. import config as config_mod
from ..args import reject_forced
from ..catalog import CacheStore
from ..errors import ExitCode, UsageError, UserRejected
from ..plan import is_interactive

__all__ = ["cmd_reset"]


def cmd_reset(ctx, stdin=None):
    out, cfg, args = ctx.out, ctx.config, ctx.args
    reject_forced(args, "fs reset")
    if args.args:
        raise UsageError("fs reset does not take arguments",
                         hint="Run `fs help reset`.")

    full = bool(getattr(args, "full", False))
    stdin = stdin if stdin is not None else sys.stdin

    origin = cfg.floorsense_url or auth.FLOORSENSE_ORIGIN
    session_path = ctx.directory / "session.json"
    # Scoped by origin like `cli.py`'s own `CacheStore` -- a `--url`
    # session clears its OWN cache, never the default deployment's.
    cache_path = ctx.directory / config_mod.cache_filename(origin)
    has_session = session_path.exists()
    has_cache = cache_path.exists()
    has_identity = full and bool(cfg.okta_user)
    has_password = full and bool(
        cfg.okta_user and auth.has_stored_password(cfg.okta_user,
                                                    origin=origin))

    targets = []
    if has_session:
        targets.append("session.json (session cookies)")
    if has_cache:
        targets.append(f"{cache_path.name} (desk catalog, policy, "
                       "locker cache)")
    if has_identity:
        targets.append("config.toml identity (okta_user, okta_org, "
                       "email_domain)")
    if has_password:
        targets.append("stored Okta password in the system keychain")

    if not targets:
        out.print("Nothing to reset.")
        out.emit({"session": False, "cache": False, "identity": False,
                  "password": False})
        return ExitCode.OK

    out.print("This will clear:")
    for t in targets:
        out.print(f"  - {t}")

    if not args.yes:
        if out.json_mode:
            raise UserRejected(
                "--json has no prompt to show",
                hint="Re-run with --yes to confirm the reset.")
        if not is_interactive(stdin):
            raise UserRejected(
                "not an interactive terminal and --yes was not given",
                hint="Re-run with --yes to confirm the reset "
                     "non-interactively.")
        out.prompt(" Proceed? [y/N] > ")
        line = stdin.readline()
        if line.strip().lower() not in ("y", "yes"):
            raise UserRejected("cancelled")

    ctx.session.store.clear()
    CacheStore(cache_path).clear()

    if has_password:
        auth.forget_password(cfg.okta_user, origin=origin)
    if has_identity:
        cfg.okta_user = None
        cfg.okta_org = config_mod.DEFAULT_ORG
        cfg.email_domain = config_mod.DEFAULT_DOMAIN
        cfg.floorsense_url = None
        config_mod.save(cfg, ctx.directory, on_repair=out.warn)

    out.print("Reset.")
    out.emit({"session": has_session, "cache": has_cache,
              "identity": has_identity, "password": has_password})
    return ExitCode.OK
