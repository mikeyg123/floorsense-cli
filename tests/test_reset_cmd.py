"""`fs reset [--full]` -- clears session.json/cache.json (discovered/session
state) always; --full also clears config.toml's [identity] and the
keychain password. No `plan.py` pipeline -- a single y/n confirmation
borrowing `plan.confirm`'s json/non-interactive guards.
"""

import datetime as dt
import io
import json

import pytest

from fs_cli import auth
from fs_cli.commands.reset_cmd import cmd_reset
from fs_cli.config import Config, load
from fs_cli.errors import ExitCode, UsageError, UserRejected
from fs_cli.render import Output
from fs_cli.session import SessionStore

TODAY = dt.date(2026, 8, 21)


class Args:
    def __init__(self, args=(), yes=False, full=False, date=(), desk=(),
                 group=(), name=(), all=False):
        self.args = list(args)
        self.yes = yes
        self.full = full
        self.date = list(date)
        self.desk = list(desk)
        self.group = list(group)
        self.name = list(name)
        self.all = all


class FakeSession:
    def __init__(self, path):
        self.store = SessionStore(path)


class Ctx:
    def __init__(self, out, config, directory, args, session):
        self.out, self.config, self.directory = out, config, directory
        self.args, self.session = args, session


class ScriptedStdin:
    def __init__(self, line="y\n", tty=True):
        self._line = line
        self._tty = tty

    def isatty(self):
        return self._tty

    def readline(self):
        return self._line


def run(config, args, directory, json_mode=False, stdin=None):
    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=TODAY, stdout=stdout, stderr=stderr,
                json_mode=json_mode)
    session = FakeSession(directory / "session.json")
    ctx = Ctx(out, config, directory, args, session)
    code = cmd_reset(ctx, stdin=stdin if stdin is not None else io.StringIO())
    out.finish()
    return code, stdout.getvalue(), stderr.getvalue()


@pytest.fixture
def cfg_dir(tmp_path):
    d = tmp_path / "fs"
    d.mkdir()
    return d


def _write(path, payload):
    path.write_text(json.dumps(payload))


# -- nothing to do ------------------------------------------------------

def test_nothing_to_reset_when_no_state(cfg_dir):
    code, stdout, _ = run(Config(), Args(yes=True), cfg_dir)
    assert code == ExitCode.OK
    assert "Nothing to reset" in stdout


# -- rejects arguments this command doesn't take -------------------------

def test_rejects_positional_args(cfg_dir):
    with pytest.raises(UsageError):
        run(Config(), Args(["favourite"], yes=True), cfg_dir)


def test_rejects_forced_flags(cfg_dir):
    with pytest.raises(UsageError):
        run(Config(), Args(yes=True, desk=["217a"]), cfg_dir)


# -- default: clears session + cache, leaves config alone ----------------

def test_clears_session_and_cache_with_yes(cfg_dir):
    _write(cfg_dir / "session.json", {"created": 1, "cookies": {"id": "x"}})
    _write(cfg_dir / "cache.json", {"desks": {"at": 1, "value": []}})
    cfg = Config(okta_user="jane.doe", okta_org="acme.okta.com",
                office_days=["monday"], groups={"favourite": ["217a"]})
    code, stdout, _ = run(cfg, Args(yes=True), cfg_dir)
    assert code == ExitCode.OK
    assert not (cfg_dir / "session.json").exists()
    assert not (cfg_dir / "cache.json").exists()
    assert "Reset." in stdout


def test_default_leaves_config_toml_untouched(cfg_dir):
    _write(cfg_dir / "session.json", {"created": 1, "cookies": {"id": "x"}})
    cfg = Config(okta_user="jane.doe", okta_org="acme.okta.com",
                email_domain="acme.com", office_days=["monday"],
                groups={"favourite": ["217a"]})
    from fs_cli import config as config_mod
    config_mod.save(cfg, cfg_dir)
    run(cfg, Args(yes=True), cfg_dir)
    reloaded = load(cfg_dir)
    assert reloaded.okta_user == "jane.doe"
    assert reloaded.okta_org == "acme.okta.com"
    assert reloaded.office_days == ["monday"]
    assert reloaded.groups == {"favourite": ["217a"]}


def test_json_emits_what_was_cleared(cfg_dir):
    _write(cfg_dir / "session.json", {"created": 1, "cookies": {"id": "x"}})
    code, stdout, _ = run(Config(), Args(yes=True), cfg_dir, json_mode=True)
    doc = json.loads(stdout)
    assert doc["session"] is True
    assert doc["cache"] is False
    assert doc["identity"] is False
    assert doc["password"] is False


# -- confirmation guards, same shape as plan.confirm ----------------------

def test_json_without_yes_is_rejected(cfg_dir):
    _write(cfg_dir / "session.json", {"created": 1, "cookies": {"id": "x"}})
    with pytest.raises(UserRejected):
        run(Config(), Args(), cfg_dir, json_mode=True)
    assert (cfg_dir / "session.json").exists()


def test_non_interactive_without_yes_is_rejected(cfg_dir):
    _write(cfg_dir / "session.json", {"created": 1, "cookies": {"id": "x"}})
    with pytest.raises(UserRejected):
        run(Config(), Args(), cfg_dir,
            stdin=ScriptedStdin(tty=False))
    assert (cfg_dir / "session.json").exists()


def test_declining_the_prompt_leaves_state_alone(cfg_dir):
    _write(cfg_dir / "session.json", {"created": 1, "cookies": {"id": "x"}})
    with pytest.raises(UserRejected):
        run(Config(), Args(), cfg_dir, stdin=ScriptedStdin("n\n"))
    assert (cfg_dir / "session.json").exists()



def test_accepting_the_prompt_clears_state(cfg_dir):
    _write(cfg_dir / "session.json", {"created": 1, "cookies": {"id": "x"}})
    code, _, _ = run(Config(), Args(), cfg_dir, stdin=ScriptedStdin("y\n"))
    assert code == ExitCode.OK
    assert not (cfg_dir / "session.json").exists()


# -- --full also clears identity + keychain password -----------------------

def test_full_clears_identity(cfg_dir):
    from fs_cli import config as config_mod
    cfg = Config(okta_user="jane.doe", okta_org="acme.okta.com",
                email_domain="acme.com", office_days=["monday"])
    config_mod.save(cfg, cfg_dir)
    run(cfg, Args(yes=True, full=True), cfg_dir)
    reloaded = load(cfg_dir)
    assert reloaded.okta_user is None
    assert reloaded.okta_org == config_mod.DEFAULT_ORG
    assert reloaded.email_domain == config_mod.DEFAULT_DOMAIN
    # preferences untouched
    assert reloaded.office_days == ["monday"]


def test_full_forgets_stored_password(cfg_dir, monkeypatch):
    forgotten = []
    monkeypatch.setattr(auth, "has_stored_password", lambda *a, **k: True)
    monkeypatch.setattr(auth, "forget_password",
                        lambda user, *a, **k: forgotten.append(user))
    cfg = Config(okta_user="jane.doe", okta_org="acme.okta.com")
    from fs_cli import config as config_mod
    config_mod.save(cfg, cfg_dir)
    code, stdout, _ = run(cfg, Args(yes=True, full=True), cfg_dir)
    assert code == ExitCode.OK
    assert forgotten == ["jane.doe"]


def test_full_without_identity_or_password_is_a_noop_for_them(cfg_dir):
    # no okta_user configured, so --full has nothing identity/password-side
    # to clear even though it's set
    _write(cfg_dir / "session.json", {"created": 1, "cookies": {"id": "x"}})
    code, stdout, _ = run(Config(), Args(yes=True, full=True), cfg_dir,
                          json_mode=True)
    doc = json.loads(stdout)
    assert doc["identity"] is False
    assert doc["password"] is False
