"""argv -> command dispatch, global flags, exit-code mapping.

`main()` assembles the context, routes to a command, and turns an
exception into an exit code. Commands never call `sys.exit` or print
directly; they raise and use `Output`.
"""

import argparse
import datetime as dt
import getpass
import sys
import traceback
from collections import namedtuple
from importlib.metadata import version
from importlib.resources import files

from . import auth, config as config_mod
from .api import LiveApi
from .catalog import Catalog, CacheStore
from .commands.at_cmd import cmd_at
from .commands.book_cmd import cmd_book
from .commands.checkin_cmd import cmd_checkin
from .commands.desks_cmd import cmd_desks
from .commands.list_cmd import cmd_list
from .commands.map_cmd import cmd_map
from .commands.office_days_cmd import cmd_office_days
from .commands.release_cmd import cmd_release
from .commands.reset_cmd import cmd_reset
from .commands.status_cmd import cmd_status
from .commands.team_cmd import cmd_team
from .errors import FsError, UsageError, exit_code_for, ExitCode
from .render import Output, supports_color
from .session import Session, SessionStore

__all__ = ["main", "build_parser"]


def build_parser():
    """Global flags only -- the command and its parameters are split out by
    `split_argv`, NOT by a positional and `argparse.REMAINDER`.

    Not `argparse.REMAINDER`: that swallows every flag typed after the
    command too, so `fs book <desk> <date> --yes` would silently run
    without `args.yes` ever being set.
    """
    p = argparse.ArgumentParser(
        prog="fs", add_help=True,
        description="Desk booking, teams and desk groups, "
                     "at the command line. Run `fs help` for the command "
                     "list, `fs help <command>` for one command's full "
                     "grammar -- this --help only covers the flags below, "
                     "which apply across every command.",
        usage="fs [command] [parameters] [options]")

    # importlib.metadata resolves the version under an editable install,
    # a shiv zipapp, or a normal install alike; pyproject.toml isn't always
    # on disk to read.
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {version('floorsense-cli')}")
    p.add_argument("--licences", "--licenses", action="store_true",
                   dest="licences",
                   help="print third-party licence notices and exit")
    p.add_argument("--json", action="store_true",
                   help="machine-readable output; warnings join the payload")
    p.add_argument("--yes", "-y", action="store_true",
                   help="accept the proposed changes without prompting")
    p.add_argument("--no-login", action="store_true",
                   help="fail rather than logging in when the session is dead")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="log requests (never secrets)")
    p.add_argument("--no-color", action="store_true",
                   help="plain text output, no ANSI colour")
    p.add_argument("--user", help="the Floorsense email (login_hint only)")
    # metavar="LOGIN": the default OKTA_USER pushes past argparse's ~20-char
    # wrap threshold and lands on its own line in --help.
    p.add_argument("--okta-user", metavar="LOGIN",
                   help="the Okta login, if it differs")
    p.add_argument("--okta-org", help="the Okta org hostname, "
                                       "e.g. yourcompany.okta.com")
    p.add_argument("--url", help="the Floorsense base URL, if not "
                                  f"{auth.FLOORSENSE_ORIGIN}; changing it "
                                  "from what's stored asks for a fresh "
                                  "email/login/password")
    p.add_argument("--save-password", action="store_true",
                   help="prompt for the Okta password and store it in the "
                        "OS keychain, replacing any already stored, once "
                        "login succeeds; forces a fresh login (and MFA "
                        "push) even if a session is already cached, and "
                        "cannot be used with `fs status`")
    p.add_argument("--all", action="store_true",
                   help="[fs release] release every own booking from today "
                        "forward, same as the 'all' argument")
    p.add_argument("--full", action="store_true",
                   help="[fs reset] also clear config.toml's identity "
                        "(okta_user, okta_org, email_domain) and forget "
                        "the stored Okta password")
    p.add_argument("--no-nav", action="store_true", dest="no_nav",
                   help="[fs map] always print the static map, never "
                        "enter cursor navigation")

    # Escape hatches for the classifier's precedence rule (args.py):
    # `fs find mon` can only mean Monday, so `--name mon` means Monica.
    # `book`/`list`/`release`/`find`/`at` route these into the classifier;
    # other commands reject them via `args.reject_forced`.
    p.add_argument("--date", action="append", default=[],
                   help="force a token to be read as a date, "
                        "not a name/desk/group")
    p.add_argument("--desk", action="append", default=[],
                   help="force a token to be read as a desk, "
                        "not a date/name/group")
    p.add_argument("--name", action="append", default=[],
                   help="force a token to be read as a person's name, "
                        "not a date/desk/group")
    p.add_argument("--group", action="append", default=[],
                   help="force a token to be read as a desk group, "
                        "not a date/desk/name")
    return p


def split_argv(parser, argv):
    """Global flags anywhere, command and parameters in order.

    `parse_known_args` leaves what it doesn't own in `rest`: the first
    leftover is the command, the rest are its parameters. An unrecognised
    flag is a usage error rather than being passed through as a parameter.
    No command (bare `fs`) defaults to `help`.
    """
    options, rest = parser.parse_known_args(argv)
    unknown = [t for t in rest if t.startswith("-") and t != "-"]
    if unknown:
        raise UsageError(f"unknown option {unknown[0]!r}",
                         hint="Run `fs --help` for the available options.")
    command = rest[0] if rest else "help"
    options.command = command
    options.args = rest[1:]
    return options


# --------------------------------------------------------------------------
# first run
# --------------------------------------------------------------------------

def first_run(out, args, directory, cfg, ask=input, discover_org=None,
              origin=None, persist=True):
    """Collect the minimum needed to log in, then continue into the command
    that was actually asked for.

    Mutates `cfg` (loaded from disk, possibly blank) rather than building a
    fresh one, so preferences/groups/teams survive.

    Only the email is asked for; `okta_user` defaults to its local part and
    `okta_org` is discovered from it via `auth.discover_okta_org` (see
    `floorsense-api-manual.md` §4.2). `--user`/`--okta-user`/`--okta-org`
    skip the corresponding prompt/lookup, so a non-interactive run works.

    `origin` is the Floorsense base URL to discover against (default
    `auth.FLOORSENSE_ORIGIN`). `persist=False` (a `--url` change) sets the
    identity on `cfg` but skips `config.save`; the caller saves once login
    against the new URL succeeds.
    """
    try:
        interactive = bool(sys.stdin.isatty())
    except (AttributeError, ValueError):
        interactive = False
    discover_org = discover_org or auth.discover_okta_org
    origin = origin or auth.FLOORSENSE_ORIGIN

    email = args.user
    if not email:
        if not interactive:
            raise UsageError(
                "no configuration and nothing to go on",
                hint="Run `fs --user you@example.com status` once.")
        email = (ask("Floorsense email: ") or "").strip()
    if not email:
        raise UsageError("a Floorsense email is required")
    if "@" not in email:
        raise UsageError(f"{email!r} is not a full email address",
                         hint="Include the domain, "
                              "e.g. jane.doe@yourcompany.com.")

    okta_user = args.okta_user or email.split("@", 1)[0]

    okta_org = args.okta_org
    if not okta_org:
        out.print("Looking up your organisation's Okta host...")
        kwargs = {} if origin == auth.FLOORSENSE_ORIGIN else {"origin": origin}
        okta_org = discover_org(email, **kwargs)
        out.print(f"Found Okta org: {okta_org}")

    cfg.okta_user = okta_user
    cfg.okta_org = okta_org
    cfg.email_domain = email.split("@", 1)[1]
    cfg.floorsense_url = None if origin == auth.FLOORSENSE_ORIGIN else origin
    if persist:
        path = config_mod.save(cfg, directory, on_repair=out.warn)
        out.print(f"Wrote {path}")
    return cfg


# --------------------------------------------------------------------------
# --url handling
# --------------------------------------------------------------------------

def _resolve_identity(out, args, directory, cfg):
    """Reconcile a `--url` override and first-run identity discovery before
    a `Session` is built.

    First-run triggers on missing identity (`okta_user`), not a missing
    file -- `fs reset --full` clears identity but leaves config.toml (and
    its other sections) in place. Exempts `reset`: nothing to reset on a
    never-configured machine either way.

    A `--url` that differs from what's stored means a different Floorsense
    deployment, so the stored identity can't be assumed valid there --
    forces the same prompt as a never-configured machine. The comparison
    (and the origin handed to `first_run`/`Session`/the keychain) is
    lower-cased: scheme and host are case-insensitive, and `--url` only
    ever carries an origin, never a path, so this can't clobber anything
    path-shaped. `persist=False`: written only once login against the new
    URL succeeds (`_persist_identity_on_success`), not eagerly.

    Returns `(cfg, url_changed)`; the caller still needs `args.save_password`
    to pick between `_store_on_login_success`/`_persist_identity_on_success`
    and `origin` for `Session`/`make_password_provider`/etc, so this hands
    back the ingredients rather than the finished callback.
    """
    requested_url = args.url.rstrip("/").lower() if args.url else None
    stored_url = (cfg.floorsense_url or auth.FLOORSENSE_ORIGIN).lower()
    url_changed = (requested_url is not None
                  and requested_url != stored_url
                  and args.command != "reset")
    if url_changed:
        cfg = first_run(out, args, directory, cfg,
                        origin=requested_url, persist=False)
    elif not cfg.okta_user and args.command != "reset":
        cfg = first_run(out, args, directory, cfg)
    if args.okta_user:
        cfg.okta_user = args.okta_user
    return cfg, url_changed


# --------------------------------------------------------------------------
# password handling
# --------------------------------------------------------------------------

def make_password_provider(out, save=False, prompt=getpass.getpass,
                           origin=auth.FLOORSENSE_ORIGIN):
    """`--save-password` always prompts for a fresh password rather than
    reusing a stored one -- it's usually typed to replace a password that
    stopped working. Storing it happens in `session.py`'s
    `on_login_success`, only once login is confirmed, not here. `Session`'s
    `force_login` (wired from `args.save_password`) is what makes `ensure()`
    reach this provider even when a session is already cached.

    `origin` scopes the keychain lookup to this Floorsense deployment (see
    `auth._account_key`) so a `--url` override can't read a different
    deployment's stored password.
    """
    def provider(okta_user):
        if save:
            return prompt(f"Okta password for {okta_user}: ")
        return auth.get_password(okta_user, prompt=prompt, origin=origin)
    return provider


def _forget_on_invalid_credentials(out, origin=auth.FLOORSENSE_ORIGIN):
    """`Session.on_invalid_credentials`: Okta rejected the password itself,
    so any stored copy is confirmed dead -- forget it so the next run
    prompts fresh instead of failing the same way forever. Only wired up
    when `--save-password` was NOT given (that flag never reads the
    keychain, so a failure there is a typo in the new password, not proof
    the stored one is bad). `origin` scopes the forget to this deployment,
    same reason as `make_password_provider`.
    """
    def on_invalid_credentials(okta_user):
        if auth.has_stored_password(okta_user, origin=origin):
            auth.forget_password(okta_user, origin=origin)
    return on_invalid_credentials


def _store_on_login_success(out, save, origin=auth.FLOORSENSE_ORIGIN):
    """`Session.on_login_success`: called only once login is fully
    confirmed (`session.py`'s `_probe`, not merely Okta accepting the
    password), so a wrong guess never reaches the keychain and an
    unconfirmed login never gets treated as one that worked. `origin` scopes
    the write, same reason as `make_password_provider`."""
    def on_login_success(okta_user, password):
        if save:
            auth.store_password(okta_user, password, origin=origin)
            out.warn("Password stored in the system keychain.")
    return on_login_success


def _persist_identity_on_success(out, cfg, directory, save,
                                 origin=auth.FLOORSENSE_ORIGIN):
    """`Session.on_login_success` for a `--url` change: `first_run` set the
    new identity on `cfg` in memory but skipped `config.save` (`persist=
    False`) -- the URL, email, okta_user and okta_org are only written once
    login against the NEW url is actually confirmed, same rule as the
    password. Delegates the password half to `_store_on_login_success`
    rather than duplicating it, so the two stay in lockstep."""
    store = _store_on_login_success(out, save, origin=origin)

    def on_login_success(okta_user, password):
        path = config_mod.save(cfg, directory, on_repair=out.warn)
        out.print(f"Wrote {path}")
        store(okta_user, password)
    return on_login_success


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

#: One source of truth for dispatch and the "Try one of:" usage hint.
HANDLERS = {"status": cmd_status, "list": cmd_list, "ls": cmd_list,
            "find": cmd_list, "book": cmd_book, "release": cmd_release,
            "checkin": cmd_checkin, "at": cmd_at, "map": cmd_map,
            "office-days": cmd_office_days, "team": cmd_team,
            "teams": cmd_team, "desks": cmd_desks, "reset": cmd_reset}
COMMANDS = tuple(HANDLERS)

#: `fs help [<command>]` grammar reference -- `fs --help` only covers the
#: global flags. `(usage, summary, detail)` per command: `summary` is the
#: one-line gloss for `fs help`, `detail` the full standalone grammar for
#: `fs help <command>`.
_CommandHelp = namedtuple("_CommandHelp", "usage summary detail")

COMMAND_HELP = {
    "status": _CommandHelp(
        "fs status",
        "Identity and session validity. No side effects, and never logs in.",
        """\
fs status

    Prints your configured Okta/Floorsense identity, where config.toml
    lives, whether an Okta password is stored in the OS keychain,
    whether the current session is live, your configured office days,
    book_ahead_days, default_group, and your configured desk groups.

    Read-only. Takes no parameters -- passing --date/--desk/--name/
    --group/--all is a usage error, not a silent no-op."""),
    "list": _CommandHelp(
        "fs list [<name|team>...] [<date>...]  (aliases: fs ls, fs find)",
        "Your locker and bookings from today forward, or someone else's.",
        """\
fs list [<name|team>...] [<date>...]  (aliases: fs ls, fs find)

    No args
        Your locker and all your bookings from today forward, with
        check-in status shown on today's row only.

    A date
        Filters to just that day. Accepts today, tomorrow, a weekday
        (mon, tuesday, ...), mon-next (that weekday next week), a
        day-of-month (15, 15th), dd/mm(/yy), or yyyy-mm-dd/yyyymmdd
        (e.g. 2026-09-02, 20260902).

    A name
        Fuzzy user search; shows every match's bookings.

    A team
        Shows every member's bookings the same way.

    following
        Shows everyone you follow (`fs team following`'s membership).

    --name / --date
        Force a token that would otherwise classify as a date/desk/
        group/team to be read as a name/date instead."""),
    "book": _CommandHelp(
        "fs book [<desk>...|<group>] [<date>...] [new]",
        "Book the best free desk in a group, confirming before it writes.",
        """\
fs book [<desk>...|<group>] [<date>...] [new]

    No desk/group
        Your default desk group (default_group, 'preferred' unless
        changed).

    One or more desks
        An ad-hoc preference list, in the order typed -- a single
        desk is that list of one. Not combined with a group.

    No dates
        Your configured office days that fall strictly after today,
        within book_ahead_days (default 10) of it. Given dates accept
        today, tomorrow, a weekday (mon, tuesday, ...), mon-next (that
        weekday next week), a day-of-month (15, 15th), dd/mm(/yy), or
        yyyy-mm-dd/yyyymmdd (e.g. 2026-09-02, 20260902).

    new
        Only book dates when you don't already have a booking
        - don't change existing bookings.

    For each date, compares what you already have booked against the
    best free, bookable desk in the target and proposes a CREATE,
    REPLACE, or NOOP row. Nothing is booked until you confirm, or
    pass --yes."""),
    "release": _CommandHelp(
        "fs release [<date>...|all]",
        "Release bookings, defaulting to today.",
        """\
fs release [<date>...|all]

    No date, no 'all'
        Today.

    all (or --all)
        Every own booking from today forward.

    A date
        Accepts today, tomorrow, a weekday (mon, tuesday, ...),
        mon-next (that weekday next week), a day-of-month (15, 15th),
        dd/mm(/yy), or yyyy-mm-dd/yyyymmdd (e.g. 2026-09-02,
        20260902).

    A date with nothing booked
        A NOOP row, not an error.

    Takes no desk, group, or name -- only dates and 'all'."""),
    "checkin": _CommandHelp(
        "fs checkin",
        "Check in to today's booking, before it auto-releases.",
        """\
fs checkin

    Confirms today's booking(s) so the auto-release deadline shown by
    `fs list` ("not checked in -- auto-releases HH:MM") doesn't fire.

    No booking today, or already checked in, is a NOOP row, not an
    error.

    Takes no arguments -- check-in only ever applies to today."""),
    "at": _CommandHelp(
        "fs at <desk|group> [<date>...]",
        "Who is sitting at these desks. Read-only.",
        """\
fs at <desk|group> [<date>...]

    A desk or a configured desk group is required -- there is no
    no-args default.

    No dates
        Today.

    Dates
        Only those days. Accepts today, tomorrow, a weekday (mon,
        tuesday, ...), mon-next (that weekday next week), a
        day-of-month (15, 15th), dd/mm(/yy), or yyyy-mm-dd/yyyymmdd
        (e.g. 2026-09-02, 20260902).

    Shows whoever's booked at each desk, anyone at all, not only
    people you follow (unlike `fs list`'s 'following'). Read-only."""),
    "map": _CommandHelp(
        "fs map [<floor>] [<date>]",
        "Show a floor's desk layout. Read-only.",
        """\
fs map [<floor>] [<date>]

    No args
        Your floor, today -- cropped and highlighted around your own
        desk(s) if you have a booking covering today.

    <date> only
        Your floor for that date (e.g. `fs map mon`).

    <floor> only
        That floor (5, 6, level5, level6 -- case-insensitive), today.
        No highlight -- an explicit floor has no booking context to
        anchor on unless a booking happens to be on it too.

    <floor> and <date>, either order
        That floor for that date, highlighted if you have a booking
        there covering that date.

    Never wraps -- the map is cropped to the terminal width. Does not
    support --json: there is no structured equivalent of an ASCII map."""),
    "office-days": _CommandHelp(
        "fs office-days [<day>...]",
        "Show or replace your configured office days.",
        """\
fs office-days [<day>...]

    No args
        Prints your configured office days (empty until set).

    Given days
        mon/tue/wed/thu/fri/sat/sun, or the full name, case-
        insensitive. Replaces the whole list -- there's no add/
        remove, it's a single unordered set.

    An unrecognised day leaves the config untouched.

    `fs book`'s no-dates default reads this list."""),
    "team": _CommandHelp(
        "fs team [<name>] [add|remove|set|delete] [<person>...]  "
        "(alias: fs teams)",
        "List, or add/remove/replace/delete members of, a named team.",
        """\
fs team [<name>] [add|remove|set|delete] [<person>...]

    No name
        Lists your configured team names (plus 'following', always
        present).

    A name alone
        Lists that team's members.

    A name plus add/remove/set plus people
        Adds/removes/replaces those members. Each person is looked up
        by fuzzy search and stored resolved (uid + name); an
        ambiguous match prompts you to pick one. The verb may come
        before the name instead (`fs team set inception jerry` ==
        `fs team inception set jerry`), unless a team is genuinely
        named add/remove/set/delete, in which case name-first is the
        only reading.

    delete
        Removes the whole team.

    following
        Always exists. add/remove follow/unfollow on the server;
        delete unfollows everyone.

    Changes are shown as a plan and confirmed before they're applied,
    same as --yes/--json everywhere else."""),
    "desks": _CommandHelp(
        "fs desks [<name>] [add|remove|set|delete] [<desk>...]",
        "List, or add/remove/replace/delete desks in, a named desk group.",
        """\
fs desks [<name>] [add|remove|set|delete] [<desk>...]

    No name
        Lists your configured desk group names.

    A name alone
        Lists that group's desks, in preference order (first listed
        is tried first by `fs book`).

    A name plus add/remove/set plus desks
        Adds/removes/replaces that group's members. A name with
        desks but no verb is a usage error, not an implicit replace.
        The verb may come before the name instead (`fs desks set
        preferred 217a` == `fs desks preferred set 217a`), unless a
        group is genuinely named add/remove/set/delete, in which case
        name-first is the only reading.

    delete
        Removes the whole group.

    Desk tokens are checked against the live catalog before anything
    is written.

    default_group is the group `fs book` uses when given none of its
    own. Changes are shown as a plan and confirmed before they're
    applied."""),
    "reset": _CommandHelp(
        "fs reset [--full]",
        "Clear session and cache state; --full also clears configured "
        "identity.",
        """\
fs reset [--full]

    Clears session.json (session cookies) and cache.json (desk
    catalog, policy, locker cache) -- everything discovered or
    produced by login, which re-derives itself on the next command.

    config.toml's preferences, desk groups and teams are left alone.

    --full
        Also clears config.toml's identity (okta_user, okta_org,
        email_domain) and forgets the stored Okta password.

    Asks to confirm unless --yes is given. Works even before `fs`
    has ever been configured, unlike every other command."""),
}
# Aliases get their own entry, headed by the real command's usage, rather
# than reusing it unlabelled.
COMMAND_HELP["ls"] = _CommandHelp(
    "fs ls [<name|team>...] [<date>...]  (alias: fs list)",
    COMMAND_HELP["list"].summary,
    "fs ls is an alias for fs list:\n\n" + COMMAND_HELP["list"].detail)
COMMAND_HELP["teams"] = _CommandHelp(
    "fs teams [<name>] [add|remove|set|delete] [<person>...]  "
    "(alias: fs team)",
    COMMAND_HELP["team"].summary,
    "fs teams is an alias for fs team:\n\n" + COMMAND_HELP["team"].detail)
# `find` differs from `list` only in its no-args default (today only);
# shares find_cmd.py's resolve_targets/rows_for/render_rows.
COMMAND_HELP["find"] = _CommandHelp(
    "fs find [<name|team>...] [<date>...]  (alias: fs list)",
    COMMAND_HELP["list"].summary,
    "fs find is an alias for fs list:\n\n" + COMMAND_HELP["list"].detail)


def licence_notices():
    """The packaged copy of NOTICE + THIRD-PARTY-NOTICES.txt, read via
    `importlib.resources` so it works identically from an editable install,
    a wheel, or a shiv zipapp. Regenerate with
    `scripts/gen-third-party-notices.py` after any dependency change."""
    return files("fs_cli").joinpath("_third_party_notices.txt").read_text()


def print_command_help(out, requested):
    """`fs help` (every command's usage + one-line gloss) or `fs help
    <command>` (that command's full grammar). Unknown command name raises
    the same `UsageError` shape as `fs <unknown>`.
    """
    if not requested:
        out.print(out.bold("fs") + " -- desk booking at the command line.")
        out.print()
        out.print(out.label("Commands:"))
        for name in COMMANDS:
            if name in ("ls", "teams", "find"):   # aliases -- shown on their line
                continue
            help_ = COMMAND_HELP[name]
            out.print(f"  {out.bold(help_.usage)}")
            out.print(f"      {out.muted(help_.summary)}")
        out.print()
        out.print(out.muted("`fs help <command>` for a command's full "
                            "grammar; `fs --help` for global flags."))
        return
    name = requested[0].strip().lower()
    if name not in COMMAND_HELP:
        raise UsageError(f"unknown command {name!r}",
                         hint=f"Try one of: {', '.join(COMMANDS)}, help.")
    out.print(COMMAND_HELP[name].detail)


class Context:
    """What every command is handed. `api` and `catalog` are lazy so
    `fs status` doesn't trigger a login just by having a context built.
    """

    def __init__(self, out, config, session, directory, args, api=None,
                 cache=None):
        self.out = out
        self.config = config
        self.session = session
        self.directory = directory
        self.args = args
        self._api = api
        self._cache = cache
        self._catalog = None

    @property
    def api(self):
        if self._api is None:
            self._api = LiveApi(self.session)
        return self._api

    @property
    def catalog(self):
        if self._catalog is None:
            self._catalog = Catalog(self.api, self._cache)
        return self._catalog

    def load_tags(self):
        """`out.tags = catalog.tag_map()` in one place instead of five
        command modules."""
        self.out.tags = self.catalog.tag_map()


# --------------------------------------------------------------------------

def main(argv=None, directory=None):
    parser = build_parser()
    directory = directory or config_mod.config_dir()
    raw = argv if argv is not None else sys.argv[1:]

    # Parsed before `out` exists, so its failure has to be reported without
    # one. Everything after this point can raise normally.
    try:
        args = split_argv(parser, raw)
    except FsError as e:
        print(f"fs: {e}", file=sys.stderr)
        if e.hint:
            print(f"    {e.hint}", file=sys.stderr)
        return exit_code_for(e)

    color = (not args.no_color) and supports_color() and not args.json
    now = dt.datetime.now()
    out = Output(today=now.date(), now=now, color=color, json_mode=args.json)

    try:
        # Must work on a machine with no config.toml yet, so answered before
        # first-run setup can trip.
        # Checked before the bare-`fs`-defaults-to-`help` fallback below:
        # `fs --licences` carries no command of its own.
        if args.licences:
            out.print(licence_notices())
            out.finish()
            return ExitCode.OK

        if args.command == "help":
            print_command_help(out, args.args)
            out.finish()
            return ExitCode.OK

        # `args`-only checks, run before config/session exist so a bad
        # combination is rejected before first-run prompts for anything.
        # `--save-password` needs a real login to confirm against, which
        # `status` (never logs in) and `--no-login` (the direct
        # contradiction) both rule out.
        if args.save_password and args.command == "status":
            raise UsageError(
                "fs status never logs in, so --save-password has nothing "
                "to confirm the password against",
                hint="Run `fs --save-password <command>` with a command "
                     "that logs in, e.g. `fs --save-password list`.")
        if args.save_password and args.no_login:
            raise UsageError("--save-password and --no-login contradict "
                             "each other",
                             hint="--save-password needs to log in to "
                                  "confirm the password; drop one flag.")

        cfg = config_mod.load(directory, on_repair=out.warn)
        cfg, url_changed = _resolve_identity(out, args, directory, cfg)
        out.groups = cfg.groups

        origin = cfg.floorsense_url or auth.FLOORSENSE_ORIGIN
        on_success = (_persist_identity_on_success(out, cfg, directory,
                                                   args.save_password,
                                                   origin=origin)
                     if url_changed
                     else _store_on_login_success(out, args.save_password,
                                                 origin=origin))
        session = Session(
            cfg, SessionStore(directory / "session.json"),
            on_message=out.warn,
            allow_login=not args.no_login,
            password_provider=make_password_provider(out, args.save_password,
                                                     origin=origin),
            origin=origin,
            on_invalid_credentials=None if args.save_password
                                  else _forget_on_invalid_credentials(
                                      out, origin=origin),
            on_login_success=on_success,
            # Bypasses the cache check so a URL change (a stale session
            # from the old deployment) and --save-password both force a
            # fresh login regardless of what's cached.
            force_login=args.save_password or url_changed,
            verbose=(lambda line: print(line, file=sys.stderr))
            if args.verbose else None)

        handler = HANDLERS.get(args.command)
        if handler is None:
            raise UsageError(f"unknown command {args.command!r}",
                             hint=f"Try one of: {', '.join(COMMANDS)}, help.")
        code = handler(Context(out, cfg, session, directory, args,
                               cache=CacheStore(directory / "cache.json")))
        # Backstop for commands (office-days, reset) that never call
        # session.ensure(): --save-password reaching here without a login
        # having happened means nothing was prompted for or stored.
        if args.save_password and not session.logged_in_this_run:
            out.warn("--save-password had nothing to confirm: `fs "
                     f"{args.command}` never logs in, so no password was "
                     "prompted for or stored.")
        out.finish()
        return code

    except FsError as e:
        out.warn(out.danger(f"fs: {e}"))
        if e.hint:
            out.warn(f"    {e.hint}")
        out.finish()
        return exit_code_for(e)
    except KeyboardInterrupt:
        out.warn("")
        return ExitCode.UNEXPECTED
    except Exception as e:                            # noqa: BLE001
        # Any non-FsError must still reach out.finish() so a --json
        # consumer reading stdout sees everything; the traceback itself
        # only shows under --verbose.
        out.warn(out.danger(f"fs: unexpected error: {e}"))
        if args.verbose:
            print(traceback.format_exc(), file=sys.stderr)
        out.finish()
        return ExitCode.UNEXPECTED


if __name__ == "__main__":
    sys.exit(main())
