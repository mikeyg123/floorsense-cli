"""`fs desks [<name>] [add|remove|set|delete] [<desk>...]` -- built on
`plan.py`'s confirm/execute pipeline (its own docstring names `team` and
`desks` as intended consumers) and `args.peel` for the name/verb grammar.

No name lists the configured group names. A name with no verb lists that
group's members (read-only) -- `NotFound` if it isn't configured, since an
unconfigured name here is a typo, not "empty". A name WITH items but no
verb is a `UsageError`: PLAN.md's list grammar is explicit that a bare name
plus a list is never an implicit replace.

The verb may come before the name instead of after it -- `fs desks add
preferred 217a` means the same as `fs desks preferred add 217a` -- via
`peel()`'s `known_names` disambiguation: unambiguous only when the first
token is a verb literal that ISN'T also a real configured group name, so a
group someone genuinely named "add" keeps its name-first meaning rather
than being silently reinterpreted.

`add`/`remove`/`set` diff the current membership against what was asked
for and hand the result to `plan.py` as an ordinary confirm/execute plan --
`CREATE` for a newly-added desk, `RELEASE` for a dropped one, `NOOP` when
the request already matches reality (already a member / not a member /
unchanged), so `fs desks quiet-corner set 217a 235a` shows exactly what will
and won't change before anything is written, the same as `fs book`.

Each row's `run()` mutates `cfg.groups[name]` in place and saves
`config.toml` immediately, rather than computing a final list and saving
once after `execute()` returns -- that's what makes a partially-confirmed
plan (some rows toggled off) save exactly what was actually applied,
mirroring how `book`/`release`'s rows each make one HTTP call rather than
the command computing a final booking set and writing it in one shot.

Desk tokens are resolved through `desks.match_desk` against the live
catalog BEFORE any plan is built -- a typo'd desk must fail fast (`NotFound`,
exit 8) rather than getting silently written to `config.toml`, and the
canonical catalog key is what gets stored, never the raw token the user
typed. `named_list_actions` (this module) is generic over "what a row's
identity/label/add-or-remove callback are" precisely so `team_cmd.py` can
reuse it rather than re-deriving the same diff.
"""

from .. import config as config_mod
from ..args import peel, reject_forced
from ..desks import fmt_desk, match_desk
from ..errors import ExitCode, NotFound, UsageError
from ..plan import Action, ActionPlan, Kind, confirm, execute

__all__ = ["cmd_desks", "named_list_actions", "named_list_deleter",
          "VERB_GERUND"]

#: `add`/`remove`/`set` -> the intent-line verb ("Adding"/"Removing"/
#: "Setting") -- a plain `.capitalize() + "ing"` mangles two of the three
#: ("Removeing", "Seting"), so this is spelled out rather than derived.
#: Shared with `team_cmd.py`, which reuses `named_list_actions` for the same
#: add/remove/set grammar.
VERB_GERUND = {"add": "Adding", "remove": "Removing", "set": "Setting"}


def named_list_actions(verb, current, items, make_add_run, make_remove_run):
    """The shared add/remove/set diff, generic over what's being listed.

    `current` and `items` are both `(key, label)` pairs -- `key` is what
    identity is compared on (a canonical desk key, a uid, a lowercased
    name), `label` is what's shown. For `add`/`remove`, `items` is the
    list the verb names; for `set`, `items` is the FULL desired list.
    `make_add_run(key, label)`/`make_remove_run(key, label)` build the
    `Action.run` callable for a row that turns out to need one -- NOOP rows
    never call either, since there is nothing to run.
    """
    current_by_key = dict(current)
    # Dedupe the requested items by key, first occurrence wins -- otherwise
    # `fs desks <group> add 217a 217a` (or, via `team_cmd.py`'s reuse of
    # this function, `fs team <team> add jane jane`) produces two identical
    # rows in the confirm table instead of one.
    seen = set()
    deduped = []
    for key, label in items:
        if key not in seen:
            seen.add(key)
            deduped.append((key, label))
    items = deduped

    if verb == "add":
        actions = []
        for key, label in items:
            if key in current_by_key:
                actions.append(Action(label, "member", None, Kind.NOOP,
                                      reason="already a member"))
            else:
                actions.append(Action(label, None, "added", Kind.CREATE,
                                      run=make_add_run(key, label)))
        return actions

    if verb == "remove":
        actions = []
        for key, label in items:
            if key in current_by_key:
                actions.append(Action(current_by_key[key], "member", None,
                                      Kind.RELEASE,
                                      run=make_remove_run(key, label)))
            else:
                actions.append(Action(label, None, None, Kind.NOOP,
                                      reason="not a member"))
        return actions

    if verb == "set":
        desired_by_key = dict(items)
        actions = []
        for key, label in items:
            if key in current_by_key:
                actions.append(Action(label, "member", "member", Kind.NOOP,
                                      reason="unchanged"))
            else:
                actions.append(Action(label, None, "added", Kind.CREATE,
                                      run=make_add_run(key, label)))
        for key, label in current:
            if key not in desired_by_key:
                actions.append(Action(label, "member", None, Kind.RELEASE,
                                      run=make_remove_run(key, label)))
        return actions

    raise ValueError(f"unknown verb {verb!r}")


def _current_items(keys, tags):
    return [(k, fmt_desk(k, tags=tags.get(k))) for k in keys]


def _resolve_items(tokens, desk_keys, tags):
    return [(key, fmt_desk(key, tags=tags.get(key))) for key in
            (match_desk(t, desk_keys) for t in tokens)]


def _list_group_names(out, groups, tags):
    if not groups:
        out.print("No desk groups configured.")
        out.emit({"groups": [], "group_desks": {}})
        return
    for name in sorted(groups):
        keys = groups[name]
        desks = (", ".join(fmt_desk(k, tags=tags.get(k)) for k in keys)
                 if keys else "(empty)")
        out.print(f"{name}: {desks}")
    out.emit({"groups": sorted(groups),
             "group_desks": {name: list(groups[name]) for name in groups}})


def _list_group_members(out, groups, name, tags):
    if name not in groups:
        raise NotFound(f"no group {name!r} configured")
    keys = groups[name]
    if keys:
        out.print(f"{name}: " + ", ".join(
            fmt_desk(k, tags=tags.get(k)) for k in keys))
    else:
        out.print(f"{name}: (empty)")
    out.emit({"group": name, "desks": list(keys)})


def _saver(cfg, directory, out, name, key, add):
    """`add=True` appends `key` to `cfg.groups[name]` (creating the group
    if this is its first member); `add=False` drops it. Saves immediately
    -- see the module docstring on why this isn't batched."""
    def run():
        keys = list(cfg.groups.get(name, []))
        if add:
            if key not in keys:
                keys.append(key)
        else:
            keys = [k for k in keys if k != key]
        cfg.groups[name] = keys
        config_mod.save(cfg, directory, on_repair=out.warn)
    return run


def named_list_deleter(cfg, directory, out, target_dict, name):
    """`run()` for a whole-group/whole-team `delete`: drop `name` from
    `target_dict` (`cfg.groups` or `cfg.teams`) and save. Generic over
    which dict for the same reason `named_list_actions` is generic over
    add/remove/set -- `team_cmd.py`'s `following`-less teams reuse this
    rather than carrying a byte-for-byte copy that only differs in which
    `cfg` attribute it closes over."""
    def run():
        target_dict.pop(name, None)
        config_mod.save(cfg, directory, on_repair=out.warn)
    return run


def cmd_desks(ctx):
    out, cfg, catalog = ctx.out, ctx.config, ctx.catalog
    # `fs desks` resolves its desk tokens directly (via `match_desk` on
    # `rest`), not through `args.bind()` -- so the `--desk`/`--date`/`--name`/
    # `--group`/`--all` classifier escape hatches are never wired in here at
    # all, and would otherwise be silently ignored rather than doing anything.
    reject_forced(ctx.args, "fs desks")
    name, verb, rest = peel(ctx.args.args, known_names=cfg.groups.keys())

    if name is None:
        # `cached_tag_map()`, not `tag_map()` -- a bare `fs desks` is a pure
        # listing (the desks it shows are already stored in `config.toml`
        # by key) and must keep `fs status`'s "never triggers a login"
        # contract. A tag is shown when cheaply known from cache; a cold
        # cache means no tags, not a forced live fetch.
        _list_group_names(out, cfg.groups, catalog.cached_tag_map())
        return ExitCode.OK

    if verb is None:
        if rest:
            raise UsageError(
                f"{name!r} needs add, remove, set, or delete before the "
                f"desk list",
                hint=f"e.g. `fs desks {name} add {rest[0]}`.")
        # Same reasoning as the bare-listing branch above.
        _list_group_members(out, cfg.groups, name, catalog.cached_tag_map())
        return ExitCode.OK

    if verb == "delete":
        # No tags needed -- deleting a group never displays one, so this is
        # the one verb that must NOT force a `tag_map()`/catalog fetch (and
        # therefore never forces a login) the way the other branches do.
        if rest:
            raise UsageError("`delete` does not take desk arguments")
        if name not in cfg.groups:
            raise NotFound(f"no group {name!r} configured")
        out.intent(f"Deleting desk group {name!r}")
        action = Action(name, f"{len(cfg.groups[name])} desk(s)", None,
                        Kind.RELEASE, run=named_list_deleter(
                            cfg, ctx.directory, out, cfg.groups, name))
        plan = ActionPlan([action], subject_header="Group")
    else:
        if not rest:
            raise UsageError(f"`{verb}` needs at least one desk")
        out.intent(f"{VERB_GERUND[verb]} desk group {name!r}")
        desk_keys = catalog.desk_keys()
        tags = catalog.tag_map()
        items = _resolve_items(rest, desk_keys, tags)
        current = _current_items(cfg.groups.get(name, []), tags)

        def add_run(key, _label):
            return _saver(cfg, ctx.directory, out, name, key, add=True)

        def remove_run(key, _label):
            return _saver(cfg, ctx.directory, out, name, key, add=False)

        actions = named_list_actions(verb, current, items, add_run, remove_run)
        plan = ActionPlan(actions, subject_header="Desk")

    plan = confirm(plan, out, yes=ctx.args.yes, stdin=None)
    return execute(plan, out)
