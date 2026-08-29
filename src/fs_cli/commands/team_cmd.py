"""`fs team [<name>] [add|remove|set|delete] [<person>...]` -- one team
grammar for every team, `following` included.

A team member is always a real, looked-up person: `add`/`remove`/`set`
resolve every typed token via `api.user_search` (an ambiguous hit uses
`plan.pick_one`; zero hits is `NotFound`) and store the result as
`{uid, name}`, never the raw string typed. Diffing (current vs. desired,
for the add/remove/set table) keys on `uid`, mirroring `desks_cmd.py`'s
`named_list_actions`, which this module reuses rather than re-deriving.

The verb may come before the name instead of after it (`fs team add crew
jane` == `fs team crew add jane`) -- same `peel()`/`known_names`
disambiguation `desks_cmd.py` documents, with `cfg.teams` plus `following`
as the collision set a real team name is checked against.

`following` is not a special code path any more -- it's a team like any
other, just backed by the server (`friend-create`/`friend-delete`, §5.2)
instead of a `config.toml` list. `_backend()` is the only place that
branches on which: it hands back `current` items plus add/remove `run()`
factories, and everything from there (resolve, diff, build the plan,
confirm, execute) is one code path. Listing shows `(stored on server)`
next to `following`'s name so it's clear while viewing or editing that
this one team's membership isn't in the config file.

The one place `following` stays asymmetric, deliberately: `delete` on an
ordinary team removes the named entry from `config.toml`; `following` has
no entry to remove, so `delete` there means "unfollow everyone" instead --
implemented as `named_list_actions("set", current, [], ...)`, the same
diff that already turns "desired is empty" into a RELEASE row per current
member, so this isn't new logic either.
"""

import datetime as dt

from .. import config as config_mod
from ..args import peel, reject_forced, split_list
from ..errors import ExitCode, NotFound, UsageError
from ..plan import Action, ActionPlan, Kind, confirm, execute, pick_one
from .desks_cmd import VERB_GERUND, named_list_actions, named_list_deleter

__all__ = ["cmd_team"]

FOLLOWING = "following"


# -- resolving a typed name to a real person -----------------------------

def resolve_person(api, out, name, today, prefer_uids=None):
    """Resolve a typed name to `(uid, name)` via a fuzzy `user_search`.

    `prefer_uids`, when given (`remove`'s current team members), narrows
    the server's hits to ones that are actually in that set before
    offering a choice -- a name that's ambiguous server-wide (multiple
    "Jane"s) is often unambiguous once scoped to who's actually on the
    team, so a single team-member hit is used directly instead of still
    being run past the picker. Falls back to the full hit list when
    none of them match (typo, or removing someone never added) so that
    case still gets the normal ambiguous-name/no-match handling.
    """
    start = int(dt.datetime.combine(today, dt.time.min).astimezone().timestamp())
    finish = int(dt.datetime.combine(today, dt.time.max).astimezone().timestamp())
    hits = [h for h in api.user_search(name, start, finish)
            if isinstance(h, dict)]
    if not hits:
        raise NotFound(f"no user matching {name!r}")
    candidates = hits
    if prefer_uids:
        narrowed = [h for h in hits if h.get("uid") in prefer_uids]
        if narrowed:
            candidates = narrowed
    if len(candidates) == 1:
        chosen = candidates[0]
    else:
        chosen = pick_one(
            candidates, out,
            lambda h: f"{h.get('name') or h.get('uid')} "
                     f"({h.get('desc') or h.get('uid')})",
            stdin=None)
    return chosen.get("uid"), chosen.get("name") or chosen.get("uid")


# -- config-backed team storage -------------------------------------------

def _entry_key(entry):
    if isinstance(entry, dict):
        return entry.get("uid") or (entry.get("name") or "").strip().lower()
    if isinstance(entry, str):
        return entry.strip().lower()
    return None


def _current_team_items(entries):
    items = []
    for entry in entries or []:
        if isinstance(entry, dict) and entry.get("name"):
            items.append((_entry_key(entry), entry["name"]))
        elif isinstance(entry, str) and entry.strip():
            items.append((_entry_key(entry), entry))
    return items


def _team_saver(cfg, directory, out, name, uid, member_name, add):
    def run():
        entries = [e for e in cfg.teams.get(name, []) if _entry_key(e) != uid]
        if add:
            entries.append({"uid": uid, "name": member_name})
        cfg.teams[name] = entries
        config_mod.save(cfg, directory, on_repair=out.warn)
    return run


# -- server-backed `following` ---------------------------------------------

def _current_following(api):
    summary = api.booking_summary(days=1) or {}
    users = [u for u in (summary.get("users") or []) if isinstance(u, dict)]
    return [(u.get("uid"), u.get("name") or u.get("uid")) for u in users]


# -- picking a backend -----------------------------------------------------

def _backend(ctx, name):
    """`(current, add_run, remove_run)` for `name` -- the one place that
    knows `following` is server-backed. Everything downstream of this is
    the same code for every team."""
    out, cfg, api = ctx.out, ctx.config, ctx.api
    if name == FOLLOWING:
        def follow_add(uid, _label):
            return lambda: api.friend_create(uid)

        def follow_remove(uid, _label):
            return lambda: api.friend_delete(uid)
        return _current_following(api), follow_add, follow_remove

    def team_add(uid, label):
        return _team_saver(cfg, ctx.directory, out, name, uid, label, add=True)

    def team_remove(uid, label):
        return _team_saver(cfg, ctx.directory, out, name, uid, label, add=False)
    return _current_team_items(cfg.teams.get(name, [])), team_add, team_remove


# -- listing ----------------------------------------------------------------

def _print_team_line(out, name, current):
    label = f"{name} (stored on server)" if name == FOLLOWING else name
    if current:
        out.print(f"{label}: " + ", ".join(n for _, n in current))
    elif name == FOLLOWING:
        out.print(f"{label}: not following anyone")
    else:
        out.print(f"{label}: (empty)")


def _list_team_members(out, name, current):
    _print_team_line(out, name, current)
    out.emit({"team": name,
             "members": [{"uid": uid, "name": n} for uid, n in current]})


def _list_all_teams(ctx):
    """Bare `fs team`: one team per line, its members alongside it -- not
    just the team names, so a custom team doesn't look empty until you ask
    about it by name specifically."""
    out = ctx.out
    all_names = sorted(set(ctx.config.teams) | {FOLLOWING})
    teams = {}
    for name in all_names:
        current, _, _ = _backend(ctx, name)
        _print_team_line(out, name, current)
        teams[name] = [{"uid": uid, "name": n} for uid, n in current]
    out.emit({"teams": all_names, "team_members": teams})


# -- the command --------------------------------------------------------

def cmd_team(ctx):
    out, cfg, api = ctx.out, ctx.config, ctx.api
    # `fs team` (including `following`) resolves names directly via
    # `peel()`/`user_search`, never through `args.bind()`, so these flags
    # would otherwise be silently ignored rather than doing anything.
    reject_forced(ctx.args, "fs team")
    name, verb, rest = peel(ctx.args.args,
                            known_names=set(cfg.teams) | {FOLLOWING})

    if name is None:
        _list_all_teams(ctx)
        return ExitCode.OK

    is_following = name.strip().lower() == FOLLOWING
    if is_following:
        name = FOLLOWING

    if verb is None:
        if rest:
            raise UsageError(
                f"{name!r} needs add, remove, set, or delete before the "
                f"name list",
                hint=f"e.g. `fs team {name} add {rest[0]}`.")
        if not is_following and name not in cfg.teams:
            raise NotFound(f"no team {name!r} configured")
        current, _, _ = _backend(ctx, name)
        _list_team_members(out, name, current)
        return ExitCode.OK

    current, add_run, remove_run = _backend(ctx, name)

    if verb == "delete":
        if rest:
            raise UsageError("`delete` does not take names")
        if is_following:
            out.intent("Unfollowing everyone")
            actions = named_list_actions("set", current, [], add_run, remove_run)
            plan = ActionPlan(actions, subject_header="Person")
        else:
            if name not in cfg.teams:
                raise NotFound(f"no team {name!r} configured")
            out.intent(f"Deleting team {name!r}")
            action = Action(name, f"{len(cfg.teams[name])} member(s)", None,
                            Kind.RELEASE, run=named_list_deleter(
                                cfg, ctx.directory, out, cfg.teams, name))
            plan = ActionPlan([action], subject_header="Team")
    else:
        if not rest:
            raise UsageError(f"`{verb}` needs at least one name")
        # "Removing team 'crew'" reads like the team itself is being
        # deleted -- `delete` already owns that phrasing -- so `remove`
        # gets its own intent line rather than the shared VERB_GERUND one.
        out.intent(f"Removing from team {name!r}" if verb == "remove"
                  else f"{VERB_GERUND[verb]} team {name!r}")
        names = split_list(rest)
        # Scope the fuzzy match to who's actually in the team for
        # `remove` -- see `resolve_person`'s `prefer_uids` docstring.
        prefer_uids = {uid for uid, _ in current} if verb == "remove" else None
        items = [resolve_person(api, out, n, out.today, prefer_uids=prefer_uids)
                for n in names]
        actions = named_list_actions(verb, current, items, add_run, remove_run)
        plan = ActionPlan(actions, subject_header="Person")

    plan = confirm(plan, out, yes=ctx.args.yes, stdin=None)
    return execute(plan, out)
