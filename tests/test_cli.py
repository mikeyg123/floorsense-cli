"""Command dispatch, exit codes, and secret hygiene.

The hygiene tests are the ones that matter most here: `--verbose` exists to
make debugging possible, and the thing most worth logging is exactly the
thing that must never be logged. A stored cookie pair is a live
authenticated session for the account.
"""

import json
import os

import pytest

from fs_cli import auth
from fs_cli import cli as cli_mod
from fs_cli import config as config_mod
from fs_cli.cli import (Context, main, make_password_provider,
                        split_argv, build_parser, first_run,
                        _forget_on_invalid_credentials, _store_on_login_success,
                        _persist_identity_on_success)
from fs_cli.config import Config, save
from fs_cli.errors import CommError, ExitCode, UsageError
from fs_cli.render import Output
from fs_cli.session import Session, SessionStore
from fs_cli.wire import redact_headers, redact_url

from conftest import CSRF_TOKEN, REAL_ID


@pytest.fixture
def cfgdir(tmp_path):
    d = tmp_path / "fs"
    save(Config(okta_user="jamie.baker", email_domain="example.com"), d)
    return d


# --- dispatch and exit codes -----------------------------------------------

def test_status_succeeds(cfgdir, capsys):
    assert main(["status"], directory=cfgdir) == ExitCode.OK
    assert "jamie.baker" in capsys.readouterr().out


def test_help_is_the_default_command(cfgdir, capsys):
    assert main([], directory=cfgdir) == ExitCode.OK
    assert "Commands:" in capsys.readouterr().out


def test_unknown_command_is_a_usage_error(cfgdir, capsys):
    assert main(["frobnicate"], directory=cfgdir) == ExitCode.USAGE
    assert "unknown command" in capsys.readouterr().err


def test_json_output_is_a_single_valid_document(cfgdir, capsys):
    assert main(["--json", "status"], directory=cfgdir) == ExitCode.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["okta_user"] == "jamie.baker"
    assert payload["warnings"] == []


def test_errors_go_to_stderr_so_stdout_stays_pipeable(cfgdir, capsys):
    main(["nope"], directory=cfgdir)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "fs:" in captured.err


def test_status_never_triggers_a_login(cfgdir, capsys):
    # `fs status` reporting "not logged in" must not be the thing that makes
    # you log in.
    main(["status"], directory=cfgdir)
    assert "Session        none" in capsys.readouterr().out


# --- help text --------------------------------------------------------------
# Structural checks, not pinned prose: `COMMAND_HELP` entries get rewritten
# for wording without these tests churning. What has to hold is the shape --
# every command has an entry, `fs help` prints a usage+summary line for each,
# `fs help <command>` prints that command's own (non-empty) detail, and an
# unknown command is still the same usage error as an unknown top-level
# command.

def test_help_lists_every_command_with_a_summary(cfgdir, capsys):
    from fs_cli.cli import COMMANDS, COMMAND_HELP

    assert main(["help"], directory=cfgdir) == ExitCode.OK
    out = capsys.readouterr().out
    for name in COMMANDS:
        if name in ("ls", "teams", "find"):  # aliases -- shown on their
                                              # line, not their own
            continue
        entry = COMMAND_HELP[name]
        assert entry.usage in out
        assert entry.summary in out


def test_help_command_prints_that_commands_full_detail(cfgdir, capsys):
    from fs_cli.cli import COMMANDS, COMMAND_HELP

    for name in COMMANDS:
        assert main(["help", name], directory=cfgdir) == ExitCode.OK
        assert capsys.readouterr().out.strip() == COMMAND_HELP[name].detail


def test_help_unknown_command_is_a_usage_error(cfgdir, capsys):
    assert main(["help", "frobnicate"], directory=cfgdir) == ExitCode.USAGE
    assert "unknown command" in capsys.readouterr().err


def test_help_json_mode_prints_nothing(cfgdir, capsys):
    # Same rule as every other command: an intent/help line is neither data
    # nor a warning, so it never joins the --json payload.
    assert main(["--json", "help"], directory=cfgdir) == ExitCode.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"warnings": []}


def test_licences_flag_prints_third_party_notices_and_exits(tmp_path, capsys):
    # No config.toml needed -- same shape as `help`.
    assert main(["--licences"], directory=tmp_path / "fs") == ExitCode.OK
    out = capsys.readouterr().out
    assert "Apache License" in out
    assert "MIT License" in out
    assert not (tmp_path / "fs" / "config.toml").exists()


def test_licenses_us_spelling_is_an_alias(tmp_path, capsys):
    assert main(["--licenses"], directory=tmp_path / "fs") == ExitCode.OK
    assert "Third-party notices" in capsys.readouterr().out


def test_global_help_flag_does_not_exit(capsys):
    # `add_help=False`: argparse's own `-h`/`--help` (raises SystemExit(0)
    # and prints its own plain-text formatting) is disabled -- `--help`/
    # `-h` is a plain `store_true` flag now, routed through `split_argv`
    # to `fs help`'s own coloured rendering instead (see the tests below).
    parsed = build_parser().parse_args(["--help"])
    assert parsed.help is True
    assert capsys.readouterr().out == ""


def test_help_flag_is_a_synonym_for_fs_help(cfgdir, capsys):
    assert main(["--help"], directory=cfgdir) == ExitCode.OK
    assert "Commands:" in capsys.readouterr().out


def test_short_help_flag_is_also_a_synonym(cfgdir, capsys):
    assert main(["-h"], directory=cfgdir) == ExitCode.OK
    assert "Commands:" in capsys.readouterr().out


def test_help_flag_after_a_command_shows_that_commands_grammar(cfgdir, capsys):
    from fs_cli.cli import COMMAND_HELP

    assert main(["list", "--help"], directory=cfgdir) == ExitCode.OK
    assert capsys.readouterr().out.strip() == COMMAND_HELP["list"].detail


def test_help_lists_the_global_options_too(cfgdir, capsys):
    assert main(["help"], directory=cfgdir) == ExitCode.OK
    out = capsys.readouterr().out
    assert "Options:" in out
    assert "--no-color" in out
    assert "--okta-user LOGIN" in out


def test_okta_user_flag_overrides_config(cfgdir, capsys):
    main(["--okta-user", "other.person", "--json", "status"],
         directory=cfgdir)
    assert json.loads(capsys.readouterr().out)["okta_user"] == "other.person"


# --- first run -------------------------------------------------------------
#
# Down to one real question (the Floorsense email) as of 2026-08-21 --
# `okta_org` used to be its own prompt, but nobody recognises their own Okta
# org hostname on sight, so it's now discovered live from the email via
# `auth.discover_okta_org` (an unauthenticated, no-MFA request -- see
# `test_auth.py`'s "org discovery" section for that call's own tests).
# `okta_user` is derived from the email's local part rather than asked
# separately. `--okta-org`/`--okta-user` remain as overrides. Tests here
# monkeypatch `auth.discover_okta_org` so no test hits the network.

FULL_FLAGS = ["--okta-user", "jamie.baker",
              "--okta-org", "example-corp.okta.com",
              "--user", "jamie.baker@example.com"]


def test_first_run_is_non_interactive_when_given_all_flags(tmp_path, capsys):
    # No `auth.discover_okta_org` patch needed here -- `--okta-org` given
    # explicitly means discovery is never called.
    d = tmp_path / "fs"
    assert main(FULL_FLAGS + ["--json", "status"],
                directory=d) == ExitCode.OK
    assert (d / "config.toml").exists()
    payload = json.loads(capsys.readouterr().out)
    assert payload["email"] == "jamie.baker@example.com"
    assert payload["okta_org"] == "example-corp.okta.com"


def test_first_run_derives_the_domain_from_a_given_email(tmp_path, capsys):
    d = tmp_path / "fs"
    main(["--okta-org", "example-corp.okta.com",
          "--user", "a.b@example.org", "--json", "status"], directory=d)
    assert json.loads(capsys.readouterr().out)["email"] == "a.b@example.org"


def test_first_run_writes_a_0600_config(tmp_path):
    import stat
    d = tmp_path / "fs"
    main(FULL_FLAGS + ["status"], directory=d)
    assert stat.S_IMODE(os.stat(d / "config.toml").st_mode) == 0o600


def test_first_run_refuses_without_email_when_not_interactive(
        tmp_path, capsys):
    # `["status"]`, not `[]` -- a bare `fs` now defaults to `help`, which
    # (like `reset`) never touches config or first-run at all, so it can't
    # exercise this path any more. Any command that reaches first-run does.
    d = tmp_path / "fs"
    code = main(["status"], directory=d)
    assert code == ExitCode.USAGE
    assert "no configuration" in capsys.readouterr().err.lower()
    assert not (d / "config.toml").exists()


def test_first_run_refuses_cleanly_on_closed_stdin(tmp_path, capsys,
                                                    monkeypatch):
    """Group F: `first_run()`'s `sys.stdin.isatty()` was unguarded, unlike
    `plan._is_interactive`/`reset_cmd._is_interactive`'s
    `try/except (AttributeError, ValueError)` -- closed/non-standard stdin
    on a fresh machine's first run crashed instead of raising a clean
    UsageError."""
    import sys as sys_mod

    class ClosedStdin:
        def isatty(self):
            raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(sys_mod, "stdin", ClosedStdin())
    d = tmp_path / "fs"
    # `["status"]`, not `[]` -- see the comment on the test above.
    code = main(["status"], directory=d)
    assert code == ExitCode.USAGE
    assert not (d / "config.toml").exists()


def test_first_run_rejects_a_bare_okta_user_as_the_email(tmp_path, capsys):
    # `--user` is documented as the Floorsense *email*; a bare login without
    # an `@` used to be silently completed against DEFAULT_DOMAIN.
    d = tmp_path / "fs"
    code = main(["--user", "jamie.baker", "status"], directory=d)
    assert code == ExitCode.USAGE
    assert "email address" in capsys.readouterr().err.lower()
    assert not (d / "config.toml").exists()


def test_first_run_derives_okta_user_from_the_email_local_part(
        tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(auth, "discover_okta_org",
                        lambda email: "example-corp.okta.com")
    d = tmp_path / "fs"
    assert main(["--user", "jamie.baker@example.com", "--json", "status"],
                directory=d) == ExitCode.OK
    assert json.loads(capsys.readouterr().out)["okta_user"] == "jamie.baker"


def test_first_run_okta_user_flag_overrides_the_email_derivation(
        tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(auth, "discover_okta_org",
                        lambda email: "example-corp.okta.com")
    d = tmp_path / "fs"
    main(["--okta-user", "j.baker", "--user", "jamie.baker@example.com",
          "--json", "status"], directory=d)
    assert json.loads(capsys.readouterr().out)["okta_user"] == "j.baker"


def test_first_run_discovers_okta_org_from_the_email(
        tmp_path, capsys, monkeypatch):
    seen = []

    def fake_discover(email):
        seen.append(email)
        return "example-corp.okta.com"

    monkeypatch.setattr(auth, "discover_okta_org", fake_discover)
    d = tmp_path / "fs"
    assert main(["--user", "jamie.baker@example.com", "--json", "status"],
                directory=d) == ExitCode.OK
    assert seen == ["jamie.baker@example.com"]
    assert json.loads(capsys.readouterr().out)["okta_org"] == \
        "example-corp.okta.com"


def test_first_run_okta_org_flag_skips_discovery(tmp_path, capsys,
                                                  monkeypatch):
    def boom(email):
        raise AssertionError("discovery should not run when --okta-org "
                             "is given")

    monkeypatch.setattr(auth, "discover_okta_org", boom)
    d = tmp_path / "fs"
    assert main(["--okta-org", "example-corp.okta.com",
                 "--user", "jamie.baker@example.com", "status"],
                directory=d) == ExitCode.OK


def test_reset_full_then_next_command_reprompts_for_identity(tmp_path):
    """The bug this pins: `fs reset --full` clears `okta_user` but leaves
    config.toml in place, so the old gate (`not cfg.exists`) never
    re-triggered first-run setup afterwards -- every later command hit
    session.py's "no Okta username configured" and pointed at `fs status`,
    which never asks for anything and so could never fix it. The gate now
    keys off missing identity, not a missing file."""
    from fs_cli import config as config_mod

    d = tmp_path / "fs"
    config_mod.save(Config(okta_user="jamie.baker",
                           email_domain="example.com"), d)
    assert main(["reset", "--full", "--yes"], directory=d) == ExitCode.OK
    assert (d / "config.toml").exists()
    assert config_mod.load(d).okta_user is None

    # A later command, even non-interactively via --user, must be able to
    # supply an identity again -- it used to be permanently stuck here.
    # --okta-org given explicitly: no real network discovery call in a test.
    code = main(["--user", "new.person@example.com",
                 "--okta-org", "example-corp.okta.com", "status"],
                directory=d)
    assert code == ExitCode.OK
    assert config_mod.load(d).okta_user == "new.person"


def test_reset_full_then_first_run_preserves_groups_and_teams(tmp_path):
    """`fs reset --full`'s own guarantee is that `[preferences]`/`[groups]`/
    `[teams]` survive it. First-run setup re-running afterwards must not
    silently undo that by building a blank Config from scratch instead of
    updating the one already on disk."""
    from fs_cli import config as config_mod

    d = tmp_path / "fs"
    config_mod.save(Config(okta_user="jamie.baker", email_domain="example.com",
                           office_days=["monday", "tuesday"],
                           groups={"favourite": ["L5.D.217A"]},
                           teams={"crew": ["jane"]}), d)
    main(["reset", "--full", "--yes"], directory=d)

    main(["--user", "new.person@example.com",
         "--okta-org", "example-corp.okta.com", "status"], directory=d)
    reloaded = config_mod.load(d)
    assert reloaded.office_days == ["monday", "tuesday"]
    assert reloaded.groups == {"favourite": ["L5.D.217A"]}
    assert reloaded.teams == {"crew": ["jane"]}


# --- --url: fresh identity, persisted only once login succeeds ------------

def test_url_override_sets_origin_without_persisting():
    import argparse

    out = Output(today=None)
    cfg = Config(okta_user="jamie.baker", email_domain="example.com")

    def discover(email, **_kw):
        raise AssertionError("--okta-org given: discovery should not run")

    args = argparse.Namespace(user="new.person@other.example",
                              okta_user=None, okta_org="other.okta.com")
    returned = first_run(out, args, "unused-directory", cfg,
                         discover_org=discover,
                         origin="https://other.example", persist=False)

    assert returned is cfg
    assert cfg.okta_user == "new.person"
    assert cfg.okta_org == "other.okta.com"
    assert cfg.email_domain == "other.example"
    assert cfg.floorsense_url == "https://other.example"


def test_url_change_does_not_persist_identity_until_login_succeeds(cfgdir):
    """`status` never logs in, so a `--url` change must leave config.toml
    untouched even though a fresh identity was supplied on argv."""
    before = config_mod.load(cfgdir)
    assert before.floorsense_url is None

    code = main(["--url", "https://other.example",
                 "--user", "new.person@other.example",
                 "--okta-org", "other.okta.com", "status"], directory=cfgdir)
    assert code == ExitCode.OK

    after = config_mod.load(cfgdir)
    assert after.floorsense_url is None
    assert after.okta_user == "jamie.baker"


def test_url_matching_the_stored_one_by_case_only_is_not_a_change(cfgdir):
    """Regression: origins are case-insensitive (scheme + host, no path),
    so `--url` typed with different casing than what's stored must be a
    no-op, not a forced re-identification + fresh login."""
    config_mod.save(Config(okta_user="jamie.baker", email_domain="example.com",
                          floorsense_url="https://other.example"), cfgdir)

    code = main(["--url", "HTTPS://Other.Example", "status"],
               directory=cfgdir)
    assert code == ExitCode.OK

    after = config_mod.load(cfgdir)
    # Unchanged: no forced first_run rewrote the identity.
    assert after.okta_user == "jamie.baker"
    assert after.floorsense_url == "https://other.example"


def test_cache_is_scoped_to_a_non_default_floorsense_url(tmp_path,
                                                          monkeypatch):
    """`Context.cache` must not read/write the same `cache.json` a default-
    origin run would -- desk identity is deployment-specific (see
    `config.cache_filename`)."""
    d = tmp_path / "fs"
    config_mod.save(Config(okta_user="jamie.baker", email_domain="example.com",
                          floorsense_url="https://other.example"), d)
    captured = {}

    class SpyCacheStore:
        def __init__(self, path):
            captured["path"] = path

    monkeypatch.setattr(cli_mod, "CacheStore", SpyCacheStore)
    main(["status"], directory=d)
    assert captured["path"].name == "cache-other.example.json"


def test_cache_is_the_bare_name_for_the_default_floorsense_url(cfgdir,
                                                                monkeypatch):
    captured = {}

    class SpyCacheStore:
        def __init__(self, path):
            captured["path"] = path

    monkeypatch.setattr(cli_mod, "CacheStore", SpyCacheStore)
    main(["status"], directory=cfgdir)
    assert captured["path"].name == "cache.json"


def test_persist_identity_on_success_writes_the_new_url_and_password(tmp_path):
    d = tmp_path / "fs"
    config_mod.save(Config(okta_user="jamie.baker", email_domain="example.com"), d)
    cfg = config_mod.load(d)
    cfg.okta_user, cfg.okta_org = "new.person", "other.okta.com"
    cfg.email_domain, cfg.floorsense_url = "other.example", "https://other.example"

    out = Output(today=None)
    on_success = _persist_identity_on_success(out, cfg, d, save=False)
    on_success("new.person", "typed-pw")

    reloaded = config_mod.load(d)
    assert reloaded.floorsense_url == "https://other.example"
    assert reloaded.okta_user == "new.person"
    assert reloaded.okta_org == "other.okta.com"


# --- --save-password always prompts; storage waits for confirmed login ----

def test_save_password_always_prompts_even_when_one_is_stored(monkeypatch):
    # Reverses the old idempotent design (DECISIONS.md): a stored password
    # is exactly what --save-password exists to REPLACE, most often because
    # it no longer works, so silently handing it back defeated the flag.
    monkeypatch.setattr(auth, "has_stored_password", lambda *a, **k: True)
    monkeypatch.setattr(
        auth, "get_password",
        lambda u, a=(), prompt=None, origin=auth.FLOORSENSE_ORIGIN:
            "from-keychain")
    prompted = []
    provider = make_password_provider(
        Output(today=None), save=True,
        prompt=lambda msg: prompted.append(msg) or "typed")
    assert provider("jamie.baker") == "typed"
    assert prompted == ["Okta password for jamie.baker: "]


def test_save_password_does_not_store_by_itself(monkeypatch):
    # Storage moved out of the provider entirely -- it happens in
    # session.py's on_login_success, only once login is confirmed. The
    # provider's only job now is to hand back what the user typed.
    stored = {}
    monkeypatch.setattr(auth, "store_password",
                        lambda u, p: stored.update({u: p}))
    provider = make_password_provider(Output(today=None), save=True,
                                      prompt=lambda _msg: "typed")
    assert provider("jamie.baker") == "typed"
    assert stored == {}


def test_without_save_password_the_keychain_is_used_first(monkeypatch):
    monkeypatch.setattr(
        auth, "get_password",
        lambda u, a=(), prompt=None, origin=auth.FLOORSENSE_ORIGIN:
            "from-keychain")
    provider = make_password_provider(
        Output(today=None), save=False,
        prompt=lambda _msg: pytest.fail("must not prompt"))
    assert provider("jamie.baker") == "from-keychain"


# --- storing only on confirmed login, forgetting only on a bad password ---

def test_store_on_login_success_stores_only_when_save_was_requested(monkeypatch):
    stored = {}
    monkeypatch.setattr(
        auth, "store_password",
        lambda u, p, origin=auth.FLOORSENSE_ORIGIN: stored.update({u: p}))
    on_success = _store_on_login_success(Output(today=None), save=True)
    on_success("jamie.baker", "typed")
    assert stored == {"jamie.baker": "typed"}


def test_store_on_login_success_is_a_no_op_without_save(monkeypatch):
    monkeypatch.setattr(
        auth, "store_password",
        lambda u, p: pytest.fail("must not store without --save-password"))
    on_success = _store_on_login_success(Output(today=None), save=False)
    on_success("jamie.baker", "typed")


def test_forget_on_invalid_credentials_clears_the_keychain_entry(monkeypatch):
    forgotten = []
    monkeypatch.setattr(
        auth, "has_stored_password",
        lambda u, a=(), origin=auth.FLOORSENSE_ORIGIN: True)
    monkeypatch.setattr(
        auth, "forget_password",
        lambda u, a=(), origin=auth.FLOORSENSE_ORIGIN: forgotten.append(u))
    on_invalid = _forget_on_invalid_credentials(Output(today=None))
    on_invalid("jamie.baker")
    assert forgotten == ["jamie.baker"]


def test_forget_on_invalid_credentials_is_a_no_op_when_nothing_was_stored(
        monkeypatch, capsys):
    """Regression: a typo'd password with nothing in the keychain must not
    be reported as "removed from the keychain" -- nothing was ever there
    to remove, and `forget_password` must not even be called."""
    monkeypatch.setattr(
        auth, "has_stored_password",
        lambda u, a=(), origin=auth.FLOORSENSE_ORIGIN: False)
    monkeypatch.setattr(
        auth, "forget_password",
        lambda u, a=(), origin=auth.FLOORSENSE_ORIGIN:
            pytest.fail("nothing was stored to forget"))
    on_invalid = _forget_on_invalid_credentials(Output(today=None))
    on_invalid("jamie.baker")
    err = capsys.readouterr().err
    assert "removed from the keychain" not in err


class _CapturingSession:
    """Stands in for `Session` at `main()`'s construction site.

    `ensure()` raises rather than doing anything real -- these tests only
    care about the kwargs `main()` decided to construct `Session` with, not
    about a command actually completing. Raising (instead of e.g.
    returning a stub session) means a test that accidentally exercises a
    real command path fails loudly instead of silently passing on data
    that was never fetched.
    """
    captured = None

    def __init__(self, *a, **kw):
        _CapturingSession.captured = kw

    def is_live(self):
        return False

    def ensure(self):
        raise CommError("_CapturingSession: no real session available")


def test_save_password_disables_forget_on_invalid_credentials(cfgdir, monkeypatch):
    # --save-password always prompts fresh (never reads the keychain), so a
    # typo there is a mistake in the NEW password, not proof the OLD stored
    # one (if any) is bad -- forgetting it anyway would be strictly worse
    # than doing nothing.
    #
    # Plain `list`, not `status`: `status` is now a `UsageError` alongside
    # `--save-password` (see the test below) since it never reaches a real
    # login. `list` DOES try to, so the kwargs `main()` passed to
    # `Session(...)` are captured before `_CapturingSession.ensure()`
    # raises and aborts the command.
    from fs_cli import cli as cli_mod
    monkeypatch.setattr(cli_mod, "Session", _CapturingSession)
    main(["--save-password", "list"], directory=cfgdir)
    assert _CapturingSession.captured["on_invalid_credentials"] is None


def test_without_save_password_forget_on_invalid_credentials_is_wired(cfgdir,
                                                                      monkeypatch):
    from fs_cli import cli as cli_mod
    monkeypatch.setattr(cli_mod, "Session", _CapturingSession)
    main(["status"], directory=cfgdir)
    assert _CapturingSession.captured["on_invalid_credentials"] is not None


def test_save_password_forces_force_login_on_the_session(cfgdir, monkeypatch):
    # The wiring half of the fix: `Session(...)` must actually be told to
    # bypass its cache, or the flag is back to silently doing nothing
    # whenever a session happens to be cached.
    from fs_cli import cli as cli_mod
    monkeypatch.setattr(cli_mod, "Session", _CapturingSession)
    main(["--save-password", "list"], directory=cfgdir)
    assert _CapturingSession.captured["force_login"] is True


def test_without_save_password_force_login_is_false(cfgdir, monkeypatch):
    from fs_cli import cli as cli_mod
    monkeypatch.setattr(cli_mod, "Session", _CapturingSession)
    main(["status"], directory=cfgdir)
    assert _CapturingSession.captured["force_login"] is False


def test_save_password_with_status_is_a_usage_error(cfgdir, monkeypatch):
    # `fs status` never logs in (its own docstring, `test_status_never_
    # triggers_a_login` above) -- `--save-password`'s whole effect lives
    # inside a login, so combining them would silently do nothing rather
    # than erroring, which is the exact bug this flag was reported for.
    from fs_cli import cli as cli_mod
    monkeypatch.setattr(cli_mod, "Session", _CapturingSession)
    _CapturingSession.captured = None
    assert main(["--save-password", "status"], directory=cfgdir) == \
        ExitCode.USAGE
    assert _CapturingSession.captured is None, \
        "Session was constructed -- the guard ran too late"



def test_save_password_with_no_login_is_a_usage_error(cfgdir, monkeypatch):
    # --no-login says "never log in"; --save-password now requires a fresh
    # login to do anything. That's a contradiction to report, not a
    # precedence to silently resolve either way.
    from fs_cli import cli as cli_mod
    monkeypatch.setattr(cli_mod, "Session", _CapturingSession)
    _CapturingSession.captured = None
    assert main(["--save-password", "--no-login", "list"],
               directory=cfgdir) == ExitCode.USAGE
    assert _CapturingSession.captured is None, \
        "Session was constructed -- the guard ran too late"


def test_save_password_warns_when_the_command_never_logged_in(cfgdir, monkeypatch,
                                                               capsys):
    # The general backstop: any command that reaches `Session(...)` but
    # never actually calls `ensure()` (e.g. `office-days`, `reset` -- not
    # just the two rejected explicitly above) must not exit 0 having
    # silently prompted for and stored nothing without a word about it.
    # `office-days` with no arguments is genuinely side-effect-free, so
    # this exercises the real command, not a stub.
    from fs_cli import cli as cli_mod

    class _NeverLogsIn(_CapturingSession):
        logged_in_this_run = False

    monkeypatch.setattr(cli_mod, "Session", _NeverLogsIn)
    assert main(["--save-password", "office-days"], directory=cfgdir) == \
        ExitCode.OK
    assert "had nothing to confirm" in capsys.readouterr().err


def test_save_password_no_warning_once_it_actually_logged_in(cfgdir, monkeypatch,
                                                              capsys):
    from fs_cli import cli as cli_mod

    class _LoggedIn(_CapturingSession):
        logged_in_this_run = True

        def ensure(self):
            raise CommError("stop before any real network call")

    monkeypatch.setattr(cli_mod, "Session", _LoggedIn)
    main(["--save-password", "list"], directory=cfgdir)
    assert "had nothing to confirm" not in capsys.readouterr().err




# --- secret hygiene --------------------------------------------------------

def test_redact_url_keeps_the_shape_and_loses_the_secret():
    got = redact_url("https://okta.example/login/sessionCookieRedirect"
                     "?token=SECRET123&redirectUrl=https%3A%2F%2Fx")
    assert "SECRET123" not in got
    assert "sessionCookieRedirect" in got and "token=" in got


def test_redact_url_leaves_a_query_less_url_alone():
    assert redact_url("https://x/app/site") == "https://x/app/site"


def test_redact_headers_keeps_names_and_drops_values():
    got = redact_headers({"Cookie": f"id={REAL_ID}",
                          "x-csrf-token": CSRF_TOKEN,
                          "Accept": "application/json"})
    assert REAL_ID not in str(got) and CSRF_TOKEN not in str(got)
    assert "Cookie" in got and got["Accept"] == "application/json"


def test_verbose_logging_never_emits_a_cookie_value_or_token(stack, tmp_path,
                                                             capsys):
    """The plan's verification #4, as an executable check: drive a real
    login and a real API call with --verbose on, then assert that nothing
    secret reached the captured output."""
    store = SessionStore(tmp_path / "session.json")
    logged = []
    s = Session(Config(okta_user="jamie.baker", email_domain="example.com",
                       okta_org=stack.org),
                store, origin=stack.origin,
                password_provider=lambda _u: "hunter2-the-password",
                on_message=lambda _m: None,
                verbose=logged.append)
    s.get("booking-list", {"days": 30})

    blob = "\n".join(logged) + capsys.readouterr().out
    for secret in (REAL_ID, "hunter2-the-password", CSRF_TOKEN,
                   "sess-xyz", "okta-sid-1"):
        assert secret not in blob, f"{secret!r} leaked into verbose output"
    # ...and it still logged something useful.
    assert any("booking-list" in line for line in logged)


def test_the_session_file_never_lands_world_readable(stack, tmp_path):
    import stat
    store = SessionStore(tmp_path / "session.json")
    Session(Config(okta_user="jamie.baker", email_domain="example.com",
                   okta_org=stack.org), store, origin=stack.origin,
            password_provider=lambda _u: "pw",
            on_message=lambda _m: None).ensure()
    mode = stat.S_IMODE(os.stat(store.path).st_mode)
    assert mode == 0o600, f"session.json is {mode:04o}"


def test_config_toml_never_contains_the_password(cfgdir):
    text = (cfgdir / "config.toml").read_text().lower()
    for forbidden in ("password", "token", "cookie", "secret"):
        assert forbidden not in text


# --------------------------------------------------------------------------
# Global flags must work AFTER the command, not only before it.
#
# `argparse.REMAINDER` swallowed everything following the command, flags
# included, so `fs list --yes` parsed as command `list` with `--yes` as a
# parameter and `args.yes` False. Every test in this file happened to put
# its flags first, which is why it survived 236 of them.
#
# For `fs list` that is cosmetic. For `fs book <desk> <date> --yes` it
# means a plan that should run unattended silently blocks on a prompt
# instead -- so the trailing-flag form is pinned here, not just the
# leading one.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["list", "--yes"],
    ["--yes", "list"],
    ["book", "5.217A", "mon", "--yes"],
])
def test_yes_is_honoured_wherever_it_appears(argv):
    assert split_argv(build_parser(), argv).yes is True


def test_command_parameters_survive_the_split():
    parsed = split_argv(build_parser(), ["book", "5.217A", "mon", "--yes"])
    assert parsed.command == "book"
    assert parsed.args == ["5.217A", "mon"]
    assert parsed.yes is True


def test_no_command_defaults_to_help():
    assert split_argv(build_parser(), []).command == "help"


def test_the_escape_hatch_flags_are_accepted_anywhere():
    """`fs find mon` can only mean Monday, so `--name mon` has to work."""
    parsed = split_argv(build_parser(), ["find", "--name", "mon"])
    assert parsed.command == "find" and parsed.name == ["mon"]


def test_a_mistyped_flag_is_a_usage_error_not_a_desk_name():
    """Passing `--yess` through as a parameter is how a typo becomes a
    positional argument instead of the flag the user meant."""
    with pytest.raises(UsageError):
        split_argv(build_parser(), ["book", "--yess"])


def test_json_still_emits_a_document_on_an_unexpected_error(cfgdir, capsys,
                                                             monkeypatch):
    """Group F: `main()` only caught `FsError`/`KeyboardInterrupt`, so any
    other exception skipped `out.finish()` entirely -- a `--json` run then
    emitted no JSON document at all, contradicting render.py's own "a --json
    consumer reading only stdout must still see everything" contract."""
    from fs_cli import cli as cli_mod

    def boom(ctx):
        raise KeyError("boom")

    monkeypatch.setitem(cli_mod.HANDLERS, "status", boom)
    code = main(["--json", "status"], directory=cfgdir)
    assert code == ExitCode.UNEXPECTED
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert doc is not None


def test_context_load_tags_sets_out_tags_from_the_catalog():
    """PLAN.md's "Next up" item 6: `out.tags = catalog.tag_map()` used to
    be copy-pasted into five command entry points; `Context.load_tags`
    replaces all five with one call, so it's worth pinning directly."""
    import argparse

    from fs_cli.fixtures import FixtureApi

    args = argparse.Namespace()
    ctx = Context(Output(today=None), Config(), session=None,
                 directory=None, args=args, api=FixtureApi())
    ctx.load_tags()
    assert ctx.out.tags == ctx.catalog.tag_map()
    assert ctx.out.tags                            # the fixture has tagged desks
