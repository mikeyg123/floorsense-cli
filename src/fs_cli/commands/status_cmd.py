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


def cmd_status(ctx):
    """Identity and session validity. No side effects, and never logs in --
    `fs status` reporting "not logged in" must not be the thing that makes
    you log in."""
    out, cfg = ctx.out, ctx.config
    reject_forced(ctx.args, "fs status")
    live = ctx.session.is_live()
    fs_version = version("floorsense-cli")

    # Left-hand labels are dimmed so the eye lands on the values -- each
    # keeps its trailing padding OUTSIDE the wrap so colouring it can't
    # shift where the value starts.
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
        # Sorted -- `fs desks`/`fs team` (no args) list these
        # alphabetically too; a different order here would be a
        # gratuitous inconsistency.
        out.print(f"{out.label('Desk groups')}    {', '.join(sorted(cfg.groups))}")
    # `following` always shown alongside configured teams, agreeing with
    # `fs team` (no args). NAME only, never membership -- that needs a
    # `booking-summary` call, and `fs status` must never trigger a login.
    out.print(f"{out.label('Teams')}          "
             f"{', '.join(sorted(set(cfg.teams) | {'following'}))}")
    out.print()
    out.print(f"{out.label('Version')}        fs {fs_version}")

    out.emit({"okta_user": cfg.okta_user, "email": cfg.email,
              "okta_org": cfg.okta_org, "session_live": live,
              "password_stored": bool(has_password),
              "office_days": list(cfg.office_days),
              "book_ahead_days": cfg.book_ahead_days,
              "default_group": cfg.default_group,
              "groups": {k: list(v) for k, v in cfg.groups.items()},
              "teams": {k: team_member_names(v)
                        for k, v in cfg.teams.items()},
              "version": fs_version})
    return ExitCode.OK
