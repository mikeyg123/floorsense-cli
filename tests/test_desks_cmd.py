"""`fs desks [<name>] [add|remove|set|delete] [<desk>...]` -- built on
`plan.py`'s confirm/execute pipeline, `args.peel` for the name/verb
grammar, and `desks.match_desk` to resolve each item against the live
catalog before any plan is built (a typo'd desk must fail fast, never get
silently stored).
"""

import datetime as dt
import io

import pytest

from fs_cli.commands.desks_cmd import cmd_desks
from fs_cli.config import Config, load
from fs_cli.errors import ExitCode, NotFound, UsageError
from fs_cli.render import Output

TODAY = dt.date(2026, 8, 21)
DESK_KEYS = ["L5.D.217A", "L5.D.235A", "L5.D.301B"]


class FakeCatalog:
    def __init__(self, tags=None):
        self._tags = tags or {}

    def desk_keys(self):
        return list(DESK_KEYS)

    def tag_map(self):
        return dict(self._tags)

    def cached_tag_map(self):
        # A real `Catalog.cached_tag_map()` reads from the on-disk/in-memory
        # cache only, never the network -- `FakeCatalog` has no such
        # distinction, so it just returns the same tags `tag_map()` would.
        return dict(self._tags)


class Args:
    # `yes=True` by default -- confirm()'s interactive loop is exercised in
    # test_plan.py; these tests are about desks_cmd's own plan-building
    # logic, and pytest's captured stdin isn't a tty, so anything short of
    # --yes would raise UserRejected before ever reaching the assertion.
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
    def __init__(self, out, config, directory, args, catalog):
        self.out, self.config, self.directory = out, config, directory
        self.args, self.catalog = args, catalog


def run(config, args, directory, json_mode=False, catalog=None):
    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(today=TODAY, stdout=stdout, stderr=stderr, json_mode=json_mode)
    ctx = Ctx(out, config, directory, args, catalog or FakeCatalog())
    code = cmd_desks(ctx)
    out.finish()
    return code, stdout.getvalue(), stderr.getvalue()


@pytest.fixture
def cfg_dir(tmp_path):
    d = tmp_path / "fs"
    d.mkdir()
    return d


class ScriptedStdin:
    """A single Enter keypress -- applies the plan's default selection,
    same convention as `test_book.py`'s interactive-table test."""

    def isatty(self):
        return True

    def readline(self):
        return "\n"


# -- no name: list configured group names ------------------------------------

def test_no_name_lists_group_names(cfg_dir):
    cfg = Config(groups={"favourite": ["L5.D.217A"], "backup": []})
    code, stdout, _ = run(cfg, Args([]), cfg_dir)
    assert code == ExitCode.OK
    assert "favourite" in stdout and "backup" in stdout


def test_no_name_lists_each_groups_desks_too(cfg_dir):
    cfg = Config(groups={"favourite": ["L5.D.217A", "L5.D.235A"]})
    code, stdout, _ = run(cfg, Args([]), cfg_dir)
    assert code == ExitCode.OK
    assert "217A" in stdout and "235A" in stdout


def test_no_name_lists_show_tags(cfg_dir):
    cfg = Config(groups={"favourite": ["L5.D.217A"]})
    catalog = FakeCatalog(tags={"L5.D.217A": ("quiet",)})
    code, stdout, _ = run(cfg, Args([]), cfg_dir, catalog=catalog)
    assert code == ExitCode.OK
    assert "[quiet]" in stdout


def test_name_no_verb_lists_show_tags(cfg_dir):
    cfg = Config(groups={"favourite": ["L5.D.217A"]})
    catalog = FakeCatalog(tags={"L5.D.217A": ("quiet",)})
    code, stdout, _ = run(cfg, Args(["favourite"]), cfg_dir, catalog=catalog)
    assert code == ExitCode.OK
    assert "[quiet]" in stdout


class NoNetworkCatalog:
    """`cached_tag_map()` behaves like a real `Catalog` on a cold cache
    (returns `{}`, no network); `tag_map()` blows up if called at all --
    pins that the two pure-listing paths use the cache-only read, never
    the one that can force a login (PLAN.md's code-review finding, the
    part `test_delete_never_touches_the_catalog` above doesn't cover:
    `fs desks` bare and `fs desks <name>` with no verb)."""
    def desk_keys(self):
        raise AssertionError("catalog touched for a verb that names no desks")

    def tag_map(self):
        raise AssertionError("live tag_map() called on a pure-listing path")

    def cached_tag_map(self):
        return {}


def test_no_name_listing_never_forces_a_live_catalog_fetch(cfg_dir):
    cfg = Config(groups={"favourite": ["L5.D.217A"]})
    code, stdout, _ = run(cfg, Args([]), cfg_dir, catalog=NoNetworkCatalog())
    assert code == ExitCode.OK
    assert "favourite" in stdout


def test_name_no_verb_listing_never_forces_a_live_catalog_fetch(cfg_dir):
    cfg = Config(groups={"favourite": ["L5.D.217A"]})
    code, stdout, _ = run(cfg, Args(["favourite"]), cfg_dir,
                          catalog=NoNetworkCatalog())
    assert code == ExitCode.OK
    assert "217A" in stdout


def test_no_name_json_includes_each_groups_desks(cfg_dir):
    import json
    cfg = Config(groups={"favourite": ["L5.D.217A", "L5.D.235A"]})
    _, stdout, _ = run(cfg, Args([]), cfg_dir, json_mode=True)
    payload = json.loads(stdout)
    assert payload["group_desks"]["favourite"] == ["L5.D.217A", "L5.D.235A"]


def test_no_name_when_none_configured_says_so(cfg_dir):
    code, stdout, _ = run(Config(), Args([]), cfg_dir)
    assert code == ExitCode.OK
    assert "no desk groups" in stdout.lower()


# -- name, no verb: list members ----------------------------------------------

def test_name_no_verb_lists_members(cfg_dir):
    cfg = Config(groups={"favourite": ["L5.D.217A", "L5.D.235A"]})
    code, stdout, _ = run(cfg, Args(["favourite"]), cfg_dir)
    assert code == ExitCode.OK
    assert "217A" in stdout and "235A" in stdout


def test_name_no_verb_unconfigured_group_is_not_found(cfg_dir):
    with pytest.raises(NotFound):
        run(Config(), Args(["ghost"]), cfg_dir)


def test_name_with_items_but_no_verb_is_a_usage_error(cfg_dir):
    with pytest.raises(UsageError):
        run(Config(), Args(["favourite", "217A"]), cfg_dir)


def test_name_with_items_but_no_verb_names_the_verbs(cfg_dir):
    # Friendlier than the old "'x' with no verb is a usage error" wording --
    # the message itself should say what to do, not just that it's wrong.
    with pytest.raises(UsageError) as exc_info:
        run(Config(), Args(["favourite", "217A"]), cfg_dir)
    assert "add" in str(exc_info.value) and "remove" in str(exc_info.value)


# -- verb-first grammar (`fs desks add favourite 217A`) ----------------------

def test_verb_can_come_before_the_name(cfg_dir):
    cfg = Config(groups={"favourite": []})
    code, _, _ = run(cfg, Args(["add", "favourite", "217A"]), cfg_dir)
    assert code == ExitCode.OK
    reloaded = load(cfg_dir)
    assert reloaded.groups["favourite"] == ["L5.D.217A"]


def test_a_group_literally_named_like_a_verb_keeps_name_first_meaning(cfg_dir):
    # `known_names` disambiguation: a group actually called "add" must not
    # be reinterpreted as the verb -- `fs desks add 217A` with such a group
    # configured stays "name 'add', no verb, one item" -- a usage error, not
    # a silent add to some other group.
    cfg = Config(groups={"add": ["L5.D.217A"]})
    with pytest.raises(UsageError):
        run(cfg, Args(["add", "217A"]), cfg_dir)


# -- add -----------------------------------------------------------------

def test_add_resolves_and_saves_the_canonical_key(cfg_dir):
    cfg = Config(groups={"favourite": []})
    code, _, _ = run(cfg, Args(["favourite", "add", "217A"]), cfg_dir)
    assert code == ExitCode.OK
    reloaded = load(cfg_dir)
    assert reloaded.groups["favourite"] == ["L5.D.217A"]


def test_add_to_a_new_group_creates_it(cfg_dir):
    cfg = Config()
    run(cfg, Args(["fresh", "add", "217A"]), cfg_dir)
    reloaded = load(cfg_dir)
    assert reloaded.groups["fresh"] == ["L5.D.217A"]


def test_add_unresolvable_desk_raises_not_found_before_any_write(cfg_dir):
    cfg = Config(groups={"favourite": []})
    with pytest.raises(NotFound):
        run(cfg, Args(["favourite", "add", "999Z"]), cfg_dir)
    assert not (cfg_dir / "config.toml").exists()


def test_add_already_present_is_a_noop_row(cfg_dir, monkeypatch):
    # --yes skips rendering the table entirely (test_book.py's convention),
    # so seeing the NOOP reason needs the interactive path, scripted to
    # just hit enter on the default selection.
    monkeypatch.setattr("sys.stdin", ScriptedStdin())
    cfg = Config(groups={"favourite": ["L5.D.217A"]})
    code, stdout, _ = run(cfg, Args(["favourite", "add", "217A"], yes=False),
                          cfg_dir)
    assert code == ExitCode.OK
    assert "already" in stdout.lower()
    reloaded = load(cfg_dir)
    assert reloaded.groups.get("favourite", ["L5.D.217A"]) == ["L5.D.217A"]


def test_add_the_same_desk_twice_produces_one_row():
    # Group G: `_resolve_items`/`named_list_actions` didn't dedupe input
    # tokens, so `fs desks favourite add 217a 217a` produced two identical
    # CREATE rows in the confirm table instead of one.
    from fs_cli.commands.desks_cmd import named_list_actions
    actions = named_list_actions(
        "add", current=[], items=[("L5.D.217A", "217A"), ("L5.D.217A", "217A")],
        make_add_run=lambda k, label: (lambda: None),
        make_remove_run=lambda k, label: (lambda: None))
    assert len(actions) == 1


def test_add_with_no_items_is_a_usage_error(cfg_dir):
    with pytest.raises(UsageError):
        run(Config(), Args(["favourite", "add"]), cfg_dir)


# -- remove --------------------------------------------------------------

def test_remove_drops_the_key_and_saves(cfg_dir):
    cfg = Config(groups={"favourite": ["L5.D.217A", "L5.D.235A"]})
    run(cfg, Args(["favourite", "remove", "217A"]), cfg_dir)
    reloaded = load(cfg_dir)
    assert reloaded.groups["favourite"] == ["L5.D.235A"]


def test_remove_absent_desk_is_a_noop_row(cfg_dir, monkeypatch):
    monkeypatch.setattr("sys.stdin", ScriptedStdin())
    cfg = Config(groups={"favourite": ["L5.D.235A"]})
    code, stdout, _ = run(cfg, Args(["favourite", "remove", "217A"],
                                    yes=False), cfg_dir)
    assert code == ExitCode.OK
    assert "not a member" in stdout.lower()


# -- set -------------------------------------------------------------------

def test_set_replaces_the_whole_list(cfg_dir):
    cfg = Config(groups={"favourite": ["L5.D.217A"]})
    run(cfg, Args(["favourite", "set", "235A", "301B"]), cfg_dir)
    reloaded = load(cfg_dir)
    assert reloaded.groups["favourite"] == ["L5.D.235A", "L5.D.301B"]


def test_set_unchanged_member_is_a_noop_row(cfg_dir, monkeypatch):
    monkeypatch.setattr("sys.stdin", ScriptedStdin())
    cfg = Config(groups={"favourite": ["L5.D.217A"]})
    code, stdout, _ = run(cfg, Args(["favourite", "set", "217A"], yes=False),
                          cfg_dir)
    assert code == ExitCode.OK
    assert "unchanged" in stdout.lower()


# -- delete ----------------------------------------------------------------

def test_delete_removes_the_whole_group(cfg_dir):
    cfg = Config(groups={"favourite": ["L5.D.217A"]})
    run(cfg, Args(["favourite", "delete"]), cfg_dir)
    reloaded = load(cfg_dir)
    assert "favourite" not in reloaded.groups


def test_delete_unconfigured_group_is_not_found(cfg_dir):
    with pytest.raises(NotFound):
        run(Config(), Args(["ghost", "delete"]), cfg_dir)


def test_delete_with_items_is_a_usage_error(cfg_dir):
    cfg = Config(groups={"favourite": ["L5.D.217A"]})
    with pytest.raises(UsageError):
        run(cfg, Args(["favourite", "delete", "217A"]), cfg_dir)


class ExplodingCatalog:
    """Pins that `delete` never touches the catalog -- deleting a group
    never displays a tag, so `tag_map()` (which `cmd_desks` used to fetch
    unconditionally, forcing a login even here) must not run for this verb
    (PLAN.md's code-review finding)."""
    def tag_map(self):
        raise AssertionError("catalog touched for a verb that shows no tags")

    def desk_keys(self):
        raise AssertionError("catalog touched for a verb that names no desks")


def test_delete_never_touches_the_catalog(cfg_dir):
    cfg = Config(groups={"favourite": ["L5.D.217A"]})
    run(cfg, Args(["favourite", "delete"]), cfg_dir, catalog=ExplodingCatalog())



# -- --yes bypasses the confirm prompt ---------------------------------------

def test_yes_applies_without_a_prompt(cfg_dir):
    cfg = Config(groups={"favourite": []})
    code, _, _ = run(cfg, Args(["favourite", "add", "217A"], yes=True), cfg_dir)
    assert code == ExitCode.OK
    reloaded = load(cfg_dir)
    assert reloaded.groups["favourite"] == ["L5.D.217A"]
