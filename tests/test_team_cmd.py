"""`fs team [<name>] [add|remove|set|delete] [<person>...]` -- one grammar
for every team. A member is always resolved via `user_search` and stored
as `{uid, name}`; `following` shares the exact same add/remove/set/delete
code, just backed by the server (`friend-create`/`friend-delete`) instead
of a `config.toml` list.
"""

import datetime as dt
import io

import pytest

from fs_cli.commands.team_cmd import cmd_team
from fs_cli.config import Config, load
from fs_cli.errors import ExitCode, NotFound, UsageError
from fs_cli.render import Output

TODAY = dt.date(2026, 8, 21)


class FakeApi:
    def __init__(self, summary=None, search=None):
        self._summary = summary or {"users": []}
        self._search = search or {}
        self.friended = []
        self.unfriended = []

    def booking_summary(self, **kw):
        return self._summary

    def user_search(self, name, start, finish):
        return self._search.get(name, [])

    def friend_create(self, uid):
        self.friended.append(uid)
        return {"result": True}

    def friend_delete(self, uid):
        self.unfriended.append(uid)
        return {"result": True}


class Args:
    # `yes=True` by default -- see test_desks_cmd.py's Args for why.
    def __init__(self, args=(), yes=True, date=(), desk=(),
                 group=(), name=(), all=False):
        self.args = list(args)
        self.yes = yes
        self.date = list(date)
        self.desk = list(desk)
        self.group = list(group)
        self.name = list(name)
        self.all = all


class Ctx:
    def __init__(self, out, config, directory, args, api):
        self.out, self.config, self.directory = out, config, directory
        self.args, self.api = args, api


def run(config, args, directory, json_mode=False, api=None):
    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=TODAY, stdout=stdout, stderr=stderr, json_mode=json_mode)
    ctx = Ctx(out, config, directory, args, api or FakeApi())
    code = cmd_team(ctx)
    out.finish()
    return code, stdout.getvalue(), stderr.getvalue()


@pytest.fixture
def cfg_dir(tmp_path):
    d = tmp_path / "fs"
    d.mkdir()
    return d


def hit(uid, name):
    return {"uid": uid, "name": name}


class ScriptedStdin:
    """A single Enter keypress -- applies the plan's default selection,
    same convention as `test_book.py`'s interactive-table test."""

    def isatty(self):
        return True

    def readline(self):
        return "\n"


# -- no name: list configured team names -------------------------------------

def test_no_name_lists_team_names(cfg_dir):
    cfg = Config(teams={"crew": [{"uid": "1", "name": "Jane Doe"}]})
    code, stdout, _ = run(cfg, Args([]), cfg_dir)
    assert code == ExitCode.OK
    assert "crew" in stdout and "following" in stdout


def test_no_name_lists_each_teams_members_too(cfg_dir):
    cfg = Config(teams={"crew": [{"uid": "1", "name": "Jane Doe"}]})
    code, stdout, _ = run(cfg, Args([]), cfg_dir)
    assert code == ExitCode.OK
    assert "Jane Doe" in stdout


def test_following_is_always_listed_stored_on_server(cfg_dir):
    code, stdout, _ = run(Config(), Args([]), cfg_dir)
    assert code == ExitCode.OK
    assert "following (stored on server)" in stdout.lower()


# -- name, no verb: list members ---------------------------------------------

def test_name_no_verb_lists_members(cfg_dir):
    cfg = Config(teams={"crew": [{"uid": "1", "name": "Jane Doe"},
                                 {"uid": "2", "name": "Bob Smith"}]})
    code, stdout, _ = run(cfg, Args(["crew"]), cfg_dir)
    assert code == ExitCode.OK
    assert "Jane Doe" in stdout and "Bob Smith" in stdout


def test_name_no_verb_unconfigured_team_is_not_found(cfg_dir):
    with pytest.raises(NotFound):
        run(Config(), Args(["ghost"]), cfg_dir)


def test_name_with_items_but_no_verb_is_a_usage_error(cfg_dir):
    with pytest.raises(UsageError):
        run(Config(), Args(["crew", "jane"]), cfg_dir)


def test_name_with_items_but_no_verb_names_the_verbs(cfg_dir):
    with pytest.raises(UsageError) as exc_info:
        run(Config(), Args(["crew", "jane"]), cfg_dir)
    assert "add" in str(exc_info.value) and "remove" in str(exc_info.value)


# -- verb-first grammar (`fs team add crew jane`) ----------------------------

def test_verb_can_come_before_the_name(cfg_dir):
    cfg = Config(teams={"crew": []})
    api = FakeApi(search={"Jane Doe": [hit("42", "Jane Doe")]})
    run(cfg, Args(["add", "crew", "Jane Doe,"]), cfg_dir, api=api)
    reloaded = load(cfg_dir)
    assert reloaded.teams["crew"] == [{"uid": "42", "name": "Jane Doe"}]


def test_a_team_literally_named_like_a_verb_keeps_name_first_meaning(cfg_dir):
    # Same disambiguation rule as desks_cmd.py's -- a team actually called
    # "add" isn't reinterpreted as the verb.
    cfg = Config(teams={"add": []})
    with pytest.raises(UsageError):
        run(cfg, Args(["add", "Jane Doe,"]), cfg_dir)


# -- add/remove/set/delete on an ordinary team --------------------------------

def test_add_resolves_and_saves_uid_and_name(cfg_dir):
    cfg = Config(teams={"crew": []})
    api = FakeApi(search={"Jane Doe": [hit("42", "Jane Doe")]})
    run(cfg, Args(["crew", "add", "Jane Doe,"]), cfg_dir, api=api)
    reloaded = load(cfg_dir)
    assert reloaded.teams["crew"] == [{"uid": "42", "name": "Jane Doe"}]


def test_add_supports_comma_separated_names_with_spaces(cfg_dir):
    cfg = Config(teams={"crew": []})
    api = FakeApi(search={"Jane Doe": [hit("1", "Jane Doe")],
                          "Bob Smith": [hit("2", "Bob Smith")]})
    run(cfg, Args(["crew", "add", "Jane", "Doe,", "Bob", "Smith"]), cfg_dir,
        api=api)
    reloaded = load(cfg_dir)
    assert reloaded.teams["crew"] == [{"uid": "1", "name": "Jane Doe"},
                                      {"uid": "2", "name": "Bob Smith"}]


def test_add_to_a_new_team_creates_it(cfg_dir):
    api = FakeApi(search={"Jane Doe": [hit("1", "Jane Doe")]})
    run(Config(), Args(["fresh", "add", "Jane Doe,"]), cfg_dir, api=api)
    reloaded = load(cfg_dir)
    assert reloaded.teams["fresh"] == [{"uid": "1", "name": "Jane Doe"}]


def test_add_no_match_is_not_found(cfg_dir):
    api = FakeApi(search={"ghost": []})
    with pytest.raises(NotFound):
        run(Config(teams={"crew": []}), Args(["crew", "add", "ghost"]),
            cfg_dir, api=api)


def test_add_ambiguous_name_prompts_to_pick(cfg_dir, monkeypatch):
    class PickStdin:
        def __init__(self):
            self._lines = ["2"]

        def isatty(self):
            return True

        def readline(self):
            return (self._lines.pop(0) + "\n") if self._lines else ""

    monkeypatch.setattr("sys.stdin", PickStdin())
    api = FakeApi(search={"jane": [hit("1", "Jane A"), hit("2", "Jane B")]})
    run(Config(teams={"crew": []}), Args(["crew", "add", "jane"]), cfg_dir,
        api=api)
    reloaded = load(cfg_dir)
    assert reloaded.teams["crew"] == [{"uid": "2", "name": "Jane B"}]


def test_add_already_present_is_a_noop_row(cfg_dir, monkeypatch):
    monkeypatch.setattr("sys.stdin", ScriptedStdin())
    cfg = Config(teams={"crew": [{"uid": "42", "name": "Jane Doe"}]})
    api = FakeApi(search={"Jane Doe": [hit("42", "Jane Doe")]})
    code, stdout, _ = run(cfg, Args(["crew", "add", "Jane Doe,"], yes=False),
                          cfg_dir, api=api)
    assert code == ExitCode.OK
    assert "already" in stdout.lower()


def test_remove_drops_and_saves(cfg_dir):
    cfg = Config(teams={"crew": [{"uid": "1", "name": "Jane Doe"},
                                 {"uid": "2", "name": "Bob Smith"}]})
    api = FakeApi(search={"Jane Doe": [hit("1", "Jane Doe")]})
    run(cfg, Args(["crew", "remove", "Jane Doe,"]), cfg_dir, api=api)
    reloaded = load(cfg_dir)
    assert reloaded.teams["crew"] == [{"uid": "2", "name": "Bob Smith"}]


def test_remove_says_removing_from_team(cfg_dir):
    cfg = Config(teams={"crew": [{"uid": "1", "name": "Jane Doe"}]})
    api = FakeApi(search={"Jane Doe": [hit("1", "Jane Doe")]})
    code, _, stderr = run(cfg, Args(["crew", "remove", "Jane Doe,"]),
                          cfg_dir, api=api)
    assert code == ExitCode.OK
    assert "Removing from team 'crew'" in stderr


def test_remove_narrows_ambiguous_hits_to_team_members_without_prompting(cfg_dir):
    # Server-wide "jane" is ambiguous (two hits), but only one of them is
    # actually on the team -- that one should be used directly, with no
    # picker prompt (no stdin is wired up, so a prompt would hang/fail).
    cfg = Config(teams={"crew": [{"uid": "2", "name": "Jane B"}]})
    api = FakeApi(search={"jane": [hit("1", "Jane A"), hit("2", "Jane B")]})
    code, stdout, _ = run(cfg, Args(["crew", "remove", "jane"]), cfg_dir,
                          api=api)
    assert code == ExitCode.OK
    reloaded = load(cfg_dir)
    assert reloaded.teams["crew"] == []


def test_remove_still_prompts_when_multiple_team_members_match(cfg_dir,
                                                                monkeypatch):
    class PickStdin:
        def __init__(self):
            self._lines = ["2"]

        def isatty(self):
            return True

        def readline(self):
            return (self._lines.pop(0) + "\n") if self._lines else ""

    monkeypatch.setattr("sys.stdin", PickStdin())
    cfg = Config(teams={"crew": [{"uid": "1", "name": "Jane A"},
                                 {"uid": "2", "name": "Jane B"}]})
    api = FakeApi(search={"jane": [hit("1", "Jane A"), hit("2", "Jane B")]})
    code, _, _ = run(cfg, Args(["crew", "remove", "jane"]), cfg_dir, api=api)
    assert code == ExitCode.OK
    reloaded = load(cfg_dir)
    assert reloaded.teams["crew"] == [{"uid": "1", "name": "Jane A"}]


def test_remove_absent_member_is_a_noop_row(cfg_dir, monkeypatch):
    monkeypatch.setattr("sys.stdin", ScriptedStdin())
    cfg = Config(teams={"crew": [{"uid": "2", "name": "Bob Smith"}]})
    api = FakeApi(search={"Jane Doe": [hit("1", "Jane Doe")]})
    code, stdout, _ = run(cfg, Args(["crew", "remove", "Jane Doe,"],
                                    yes=False), cfg_dir, api=api)
    assert code == ExitCode.OK
    assert "not a member" in stdout.lower()


def test_set_replaces_the_whole_list(cfg_dir):
    cfg = Config(teams={"crew": [{"uid": "1", "name": "Jane Doe"}]})
    api = FakeApi(search={"Bob Smith": [hit("2", "Bob Smith")]})
    run(cfg, Args(["crew", "set", "Bob Smith,"]), cfg_dir, api=api)
    reloaded = load(cfg_dir)
    assert reloaded.teams["crew"] == [{"uid": "2", "name": "Bob Smith"}]


def test_delete_removes_the_whole_team(cfg_dir):
    cfg = Config(teams={"crew": [{"uid": "1", "name": "Jane Doe"}]})
    run(cfg, Args(["crew", "delete"]), cfg_dir)
    reloaded = load(cfg_dir)
    assert "crew" not in reloaded.teams


def test_delete_unconfigured_team_is_not_found(cfg_dir):
    with pytest.raises(NotFound):
        run(Config(), Args(["ghost", "delete"]), cfg_dir)


def test_add_with_no_names_is_a_usage_error(cfg_dir):
    with pytest.raises(UsageError):
        run(Config(teams={"crew": []}), Args(["crew", "add"]), cfg_dir)


# -- `following`: read ---------------------------------------------------

def test_following_bare_lists_who_is_followed(cfg_dir):
    api = FakeApi(summary={"users": [{"uid": "1", "name": "Jane Doe"}]})
    code, stdout, _ = run(Config(), Args(["following"]), cfg_dir, api=api)
    assert code == ExitCode.OK
    assert "Jane Doe" in stdout
    assert "stored on server" in stdout.lower()


def test_following_bare_when_none_says_so(cfg_dir):
    api = FakeApi(summary={"users": []})
    code, stdout, _ = run(Config(), Args(["following"]), cfg_dir, api=api)
    assert code == ExitCode.OK
    assert "not following anyone" in stdout.lower()


def test_following_is_never_written_to_config(cfg_dir):
    api = FakeApi(summary={"users": [{"uid": "1", "name": "Jane Doe"}]})
    run(Config(), Args(["following"]), cfg_dir, api=api)
    assert not (cfg_dir / "config.toml").exists()


# -- `following add/remove`: resolves a uid, calls the network -------------

def test_following_add_resolves_uid_and_calls_friend_create(cfg_dir):
    api = FakeApi(summary={"users": []},
                  search={"jane": [{"uid": "42", "name": "Jane Doe"}]})
    code, stdout, _ = run(Config(), Args(["following", "add", "jane"]),
                          cfg_dir, api=api)
    assert code == ExitCode.OK
    assert api.friended == ["42"]


def test_following_add_does_not_touch_config(cfg_dir):
    api = FakeApi(summary={"users": []},
                  search={"jane": [{"uid": "42", "name": "Jane Doe"}]})
    run(Config(), Args(["following", "add", "jane"]), cfg_dir, api=api)
    assert not (cfg_dir / "config.toml").exists()


def test_following_remove_calls_friend_delete(cfg_dir):
    api = FakeApi(summary={"users": [{"uid": "42", "name": "Jane Doe"}]},
                  search={"jane": [{"uid": "42", "name": "Jane Doe"}]})
    code, _, _ = run(Config(), Args(["following", "remove", "jane"]),
                     cfg_dir, api=api)
    assert code == ExitCode.OK
    assert api.unfriended == ["42"]


def test_following_add_no_match_is_not_found(cfg_dir):
    api = FakeApi(search={"ghost": []})
    with pytest.raises(NotFound):
        run(Config(), Args(["following", "add", "ghost"]), cfg_dir, api=api)


def test_following_add_already_following_is_a_noop_row(cfg_dir, monkeypatch):
    monkeypatch.setattr("sys.stdin", ScriptedStdin())
    api = FakeApi(summary={"users": [{"uid": "42", "name": "Jane Doe"}]},
                  search={"jane": [{"uid": "42", "name": "Jane Doe"}]})
    code, stdout, _ = run(Config(),
                          Args(["following", "add", "jane"], yes=False),
                          cfg_dir, api=api)
    assert code == ExitCode.OK
    assert "already" in stdout.lower()
    assert api.friended == []


def test_following_add_ambiguous_name_prompts_to_pick(cfg_dir, monkeypatch):
    class PickStdin:
        def __init__(self):
            self._lines = ["2"]

        def isatty(self):
            return True

        def readline(self):
            return (self._lines.pop(0) + "\n") if self._lines else ""

    monkeypatch.setattr("sys.stdin", PickStdin())
    api = FakeApi(summary={"users": []}, search={"jane": [
        {"uid": "1", "name": "Jane A"}, {"uid": "2", "name": "Jane B"}]})
    code, _, _ = run(Config(), Args(["following", "add", "jane"]), cfg_dir,
                     api=api)
    assert code == ExitCode.OK
    assert api.friended == ["2"]


# -- `following delete`: unfollows everyone, same as any other team ---------

def test_following_delete_unfollows_everyone(cfg_dir):
    api = FakeApi(summary={"users": [{"uid": "1", "name": "Jane Doe"},
                                     {"uid": "2", "name": "Bob Smith"}]})
    code, _, _ = run(Config(), Args(["following", "delete"]), cfg_dir, api=api)
    assert code == ExitCode.OK
    assert sorted(api.unfriended) == ["1", "2"]


def test_following_delete_with_nobody_followed_is_a_noop(cfg_dir):
    api = FakeApi(summary={"users": []})
    code, _, _ = run(Config(), Args(["following", "delete"]), cfg_dir, api=api)
    assert code == ExitCode.OK
    assert api.unfriended == []


def test_following_delete_does_not_take_names(cfg_dir):
    with pytest.raises(UsageError):
        run(Config(), Args(["following", "delete", "jane"]), cfg_dir)


# -- --yes ----------------------------------------------------------------

def test_yes_applies_without_a_prompt(cfg_dir):
    cfg = Config(teams={"crew": []})
    api = FakeApi(search={"Jane Doe": [hit("1", "Jane Doe")]})
    code, _, _ = run(cfg, Args(["crew", "add", "Jane Doe,"], yes=True),
                     cfg_dir, api=api)
    assert code == ExitCode.OK
    reloaded = load(cfg_dir)
    assert reloaded.teams["crew"] == [{"uid": "1", "name": "Jane Doe"}]
