"""`fs office-days [<day>...]` -- a straight `config.toml` write, no
`plan.py` pipeline (nothing to confirm), closer in shape to `fs status`
than to `fs book`.

No args -> read-only, prints what's configured. Args -> replaces the whole
list (no add/remove/set/delete grammar -- that verb grammar is reserved for
`fs team`/`fs desks`; this is a simple list like `okta_user`).
"""

import datetime as dt
import io

import pytest

from fs_cli.commands.office_days_cmd import cmd_office_days
from fs_cli.config import Config, load
from fs_cli.errors import ExitCode, UsageError
from fs_cli.render import Output

TODAY = dt.date(2026, 8, 21)


class Args:
    def __init__(self, args=(), date=(), desk=(), group=(), name=(),
                 all=False):
        self.args = list(args)
        self.date = list(date)
        self.desk = list(desk)
        self.group = list(group)
        self.name = list(name)
        self.all = all


class Ctx:
    def __init__(self, out, config, directory, args):
        self.out, self.config, self.directory, self.args = (
            out, config, directory, args)


def run(config, args, directory, json_mode=False):
    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=TODAY, stdout=stdout, stderr=stderr, json_mode=json_mode)
    code = cmd_office_days(Ctx(out, config, directory, args))
    out.finish()
    return code, stdout.getvalue(), stderr.getvalue()


@pytest.fixture
def cfg_dir(tmp_path):
    d = tmp_path / "fs"
    d.mkdir()
    return d


# -- no args: read-only ------------------------------------------------------

def test_no_args_prints_configured_days(cfg_dir):
    cfg = Config(office_days=["monday", "tuesday", "thursday"])
    code, stdout, _ = run(cfg, Args([]), cfg_dir)
    assert code == ExitCode.OK
    assert "Monday" in stdout and "Tuesday" in stdout and "Thursday" in stdout


def test_no_args_when_none_configured_says_so(cfg_dir):
    code, stdout, _ = run(Config(), Args([]), cfg_dir)
    assert code == ExitCode.OK
    assert "none configured" in stdout


def test_no_args_does_not_write_config(cfg_dir):
    cfg = Config(office_days=["monday"])
    run(cfg, Args([]), cfg_dir)
    assert not (cfg_dir / "config.toml").exists()


def test_no_args_json_emits_raw_lowercase_values(cfg_dir):
    cfg = Config(office_days=["monday", "tuesday"])
    _, stdout, _ = run(cfg, Args([]), cfg_dir, json_mode=True)
    assert '"monday"' in stdout
    assert '"Monday"' not in stdout


# -- with args: replace the whole list --------------------------------------

def test_setting_days_replaces_the_list_and_saves(cfg_dir):
    cfg = Config()
    code, stdout, _ = run(cfg, Args(["mon", "wed"]), cfg_dir)
    assert code == ExitCode.OK
    reloaded = load(cfg_dir)
    assert reloaded.office_days == ["monday", "wednesday"]


def test_setting_days_accepts_short_spellings_and_canonicalizes(cfg_dir):
    run(Config(), Args(["tue", "thurs"]), cfg_dir)
    reloaded = load(cfg_dir)
    assert reloaded.office_days == ["tuesday", "thursday"]


def test_setting_days_dedupes(cfg_dir):
    run(Config(), Args(["mon", "monday"]), cfg_dir)
    reloaded = load(cfg_dir)
    assert reloaded.office_days == ["monday"]


def test_setting_days_sorts_by_weekday(cfg_dir):
    run(Config(), Args(["fri", "mon"]), cfg_dir)
    reloaded = load(cfg_dir)
    assert reloaded.office_days == ["monday", "friday"]


def test_setting_days_replaces_previous_configuration(cfg_dir):
    cfg = Config(office_days=["saturday"])
    run(cfg, Args(["mon"]), cfg_dir)
    reloaded = load(cfg_dir)
    assert reloaded.office_days == ["monday"]


def test_setting_days_prints_confirmation(cfg_dir):
    code, stdout, _ = run(Config(), Args(["mon", "tue"]), cfg_dir)
    assert code == ExitCode.OK
    assert "Monday" in stdout and "Tuesday" in stdout


def test_unknown_day_is_a_usage_error(cfg_dir):
    with pytest.raises(UsageError):
        run(Config(), Args(["mon", "someday"]), cfg_dir)


def test_unknown_day_does_not_write_config(cfg_dir):
    with pytest.raises(UsageError):
        run(Config(), Args(["someday"]), cfg_dir)
    assert not (cfg_dir / "config.toml").exists()
