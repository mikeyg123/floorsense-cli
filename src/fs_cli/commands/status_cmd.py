"""`fs status` -- identity and session validity, no side effects.

Follows the same `commands/` module-per-command shape as every other
command.
"""

from importlib.metadata import version

from .. import auth
from ..args import reject_forced
from ..errors import ExitCode
from .find_cmd import team_member_names

__all__ = ["cmd_status"]

#: Same source `cli.py`'s `--version` reads (`importlib.metadata`, not a
#: parse of pyproject.toml -- see `build_parser`'s comment for why: this
#: resolves the same way editable, `uv tool install`-ed, or zipapp'd).
#: The repo URL isn't in installed metadata by default the same way, so
#: it's a plain constant here, mirroring `auth.py`'s `FLOORSENSE_ORIGIN`.
REPO_URL = "https://github.com/example-org/floorsense-cli"


def cmd_status(ctx):
    """Identity and session validity. No side effects, and never logs in --
    `fs status` reporting "not logged in" must not be the thing that makes
    you log in."""
    out, cfg = ctx.out, ctx.config
    reject_forced(ctx.args, "fs status")
    live = ctx.session.is_live()
    fs_version = version("floorsense-cli")

    # Left-hand labels are dimmed (`out.label`, `render.THEME`'s scaffolding
    # colour) so the eye lands on the values, not the column of names --
    # each label keeps its literal trailing padding OUTSIDE the wrap, so
    # colouring it can't shift where the value starts.
    out.print(f"{out.label('Okta user')}      {cfg.okta_user or '(not configured)'}")
    out.print(f"{out.label('Floorsense')}     {cfg.email or '(not configured)'}")
    out.print(f"{out.label('Okta org')}       {cfg.okta_org}")
    out.print(f"{out.label('Config')}         {ctx.directory / 'config.toml'}")
    origin = cfg.floorsense_url or auth.FLOORSENSE_ORIGIN
    has_password = cfg.okta_user and auth.has_stored_password(cfg.okta_user,
                                                              origin=origin)
    password_note = out.good("in keychain") if has_password else out.muted("not stored")
    out.print(f"{out.label('Password')}       {password_note}")
    session_note = (out.good("live") if live
                    else out.attention("none -- next command will log in"))
    out.print(f"{out.label('Session')}        {session_note}")
    if cfg.office_days:
        out.print(f"{out.label('Office days')}    {', '.join(cfg.office_days)}")
    out.print(f"{out.label('Book ahead')}     {cfg.book_ahead_days} days")
    out.print(f"{out.label('Default group')}  {cfg.default_group}")
    if cfg.groups:
        # Sorted -- `fs desks`/`fs team` (no args) list these alphabetically
        # too; showing the same config in a different order here would be a
        # gratuitous inconsistency between two views of the same data.
        out.print(f"{out.label('Desk groups')}    {', '.join(sorted(cfg.groups))}")
    # `following` always shown alongside configured teams -- it's Floorsense's
    # own server-backed team (`fs team following`), not a `config.toml`
    # entry, but `fs team` (no args) already lists it the same way and this
    # line should agree with that rather than making it look configured-only.
    # Its NAME only, never its membership -- listing members would mean a
    # `booking-summary` call, and `fs status` must never trigger a login
    # (this module's own docstring/contract) or make a network call at all.
    out.print(f"{out.label('Teams')}          "
             f"{', '.join(sorted(set(cfg.teams) | {'following'}))}")
    out.print()
    out.print(f"{out.label('Version')}        fs {fs_version}  "
             f"{out.muted(REPO_URL)}")

    out.emit({"okta_user": cfg.okta_user, "email": cfg.email,
              "okta_org": cfg.okta_org, "session_live": live,
              "password_stored": bool(has_password),
              "office_days": list(cfg.office_days),
              "book_ahead_days": cfg.book_ahead_days,
              "default_group": cfg.default_group,
              "groups": {k: list(v) for k, v in cfg.groups.items()},
              "teams": {k: team_member_names(v)
                        for k, v in cfg.teams.items()},
              "version": fs_version, "repository": REPO_URL})
    return ExitCode.OK
