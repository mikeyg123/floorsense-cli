"""Token classification and free-order parameter binding.

Two stages, and the split is what makes free-order arguments fall out for
free rather than needing per-command ordering logic:

  1. **Tokenize.** Peel the fixed leading positionals (a list name, then a
     verb if there is one), THEN split the remainder as a list. Peeling
     first is what stops `fs team crew add jane doe, bob smith` from
     producing `["crew add jane doe", "bob smith"]`.

  2. **Classify** each remaining token by ordered predicate, and let each
     command declare which TYPES it accepts. Binding is by type, so
     `fs at 5.235 mon tues` and `fs at mon tues 5.235` are the same command.

Precedence is date > group > team > desk > name. That ordering is a
deliberate, documented decision rather than an accident: it makes `fs find
mon` mean Monday and never Monica, which is surprising exactly once and
deterministic forever after. `--name` is the escape hatch.
"""

from dataclasses import dataclass, field
from enum import Enum

from .dates import is_date_token, parse_date
from .desks import match_desk, normalise
from .errors import NotFound, UsageError

__all__ = ["TokenType", "Vocabulary", "classify", "split_list", "peel",
           "bind", "reject_forced", "LIST_VERBS"]

LIST_VERBS = ("add", "remove", "set", "delete")

#: name -> attribute on the parsed argv namespace, for `reject_forced`.
_FORCED_FLAGS = (("--date", "date"), ("--desk", "desk"),
                 ("--name", "name"), ("--group", "group"),
                 ("--all", "all"))


class TokenType(Enum):
    DATE = "date"
    GROUP = "group"
    TEAM = "team"
    DESK = "desk"
    NAME = "name"


@dataclass
class Vocabulary:
    """Everything needed to decide what a bare word means. Assembled from
    config plus the desk catalog, and passed in rather than looked up, so
    this module stays pure and testable."""
    today: object
    groups: dict = field(default_factory=dict)
    teams: dict = field(default_factory=dict)
    desk_keys: list = field(default_factory=list)

    def has_group(self, token):
        return token.strip().lower() in {g.lower() for g in self.groups}

    def has_team(self, token):
        return token.strip().lower() in {t.lower() for t in self.teams}

    def has_desk(self, token):
        want = normalise(token)
        if not want:
            return False
        return any(normalise(k) == want or normalise(k).endswith(want)
                   for k in self.desk_keys)


def classify(token, vocab):
    """One token -> one TokenType. Ordered predicates; first hit wins."""
    if is_date_token(token, vocab.today):
        return TokenType.DATE
    if vocab.has_group(token):
        return TokenType.GROUP
    if vocab.has_team(token):
        return TokenType.TEAM
    if vocab.has_desk(token):
        return TokenType.DESK
    return TokenType.NAME


def split_list(tokens):
    """Split the trailing argument list.

    Commas are optional except to disambiguate names containing spaces --
    so: if a comma appears anywhere, commas are the separator and whitespace
    is part of the name; otherwise each `tokens` entry is already one item.

    The no-comma path used to re-join every token with a space and split the
    result back apart on whitespace -- harmless when the shell handed over
    several single-word tokens (`fs release mon tue` -> the same three
    tokens either way), but destructive the moment one of those tokens was a
    *quoted* multi-word argument: the shell delivers `fs list "nathan k"` as
    one token, `"nathan k"`, and the join-then-resplit flattened it right
    back into two, silently turning a quoted name into two bare-word
    searches. Each `tokens` entry is already an atomic shell argument by the
    time it gets here -- trust that boundary instead of re-deriving it.
    """
    if not tokens:
        return []
    joined = " ".join(tokens)
    if "," in joined:
        return [part.strip() for part in joined.split(",") if part.strip()]
    return [t.strip() for t in tokens if t.strip()]


def peel(tokens, verbs=LIST_VERBS, known_names=()):
    """Peel the fixed leading positionals off `team`/`desks`.

    Returns (name, verb, rest). `verb` is None when neither position 2 nor
    (see below) position 1 holds one -- which the caller turns into a
    usage error rather than an implicit replace. That is the point of
    having verbs at all: nothing destructive should be reachable by
    forgetting a word.

    Name-first (`fs team crew add jane`) is tried first and always wins
    when it matches -- token 2 being a verb literal is enough, regardless
    of `known_names`. Verb-first (`fs team add crew jane`) is accepted
    too, but only when it's unambiguous: token 1 must be a verb literal
    AND NOT itself a real configured name. `known_names` is what makes
    that call -- pass the caller's current group/team names (case folds
    internally) so a group or team someone genuinely named "add" or "set"
    keeps meaning what it always meant (name-first, verb missing, a usage
    error prompting for one) rather than being silently reinterpreted.
    """
    if not tokens:
        return None, None, []
    name = tokens[0]
    if len(tokens) > 1 and tokens[1].lower() in verbs:
        return name, tokens[1].lower(), list(tokens[2:])
    if len(tokens) > 1 and name.lower() in verbs:
        known = {n.strip().lower() for n in known_names}
        if name.lower() not in known:
            return tokens[1], name.lower(), list(tokens[2:])
    return name, None, list(tokens[1:])


def _resolve(token, ttype, vocab):
    """Turn a classified token into the value the command actually wants:
    a date object, a real catalog desk key, or the canonical group/team name."""
    if ttype is TokenType.DATE:
        value = parse_date(token, vocab.today)
        if value is None:
            raise UsageError(f"{token!r} is not a date")
        return value
    if ttype is TokenType.DESK:
        return match_desk(token, vocab.desk_keys)
    if ttype is TokenType.GROUP:
        found = next((g for g in vocab.groups
                     if g.lower() == token.strip().lower()), None)
        if found is None:
            raise NotFound(f"no group {token!r} configured")
        return found
    if ttype is TokenType.TEAM:
        found = next((t for t in vocab.teams
                     if t.lower() == token.strip().lower()), None)
        if found is None:
            raise NotFound(f"no team {token!r} configured")
        return found
    return token


def bind(tokens, vocab, accepts, forced=None):
    """Classify and group tokens by type.

    `accepts` is the set of types this command understands; anything else is
    a usage error naming the offending token, rather than being silently
    dropped. `forced` carries the explicit `--date/--desk/--name/--group`
    escape hatches, which skip classification but are still resolved -- so
    `--desk 5235a` still becomes a real catalog key.
    """
    out = {t: [] for t in accepts}

    for ttype, raw_values in (forced or {}).items():
        if ttype not in out:
            raise UsageError(f"this command does not take a {ttype.value}")
        for raw in raw_values:
            try:
                out[ttype].append(_resolve(raw, ttype, vocab))
            except NotFound as e:
                raise UsageError(str(e)) from e

    for token in tokens:
        ttype = classify(token, vocab)
        if ttype not in out:
            accepted = ", ".join(sorted(t.value for t in accepts))
            raise UsageError(
                f"{token!r} looks like a {ttype.value}, "
                f"which this command doesn't take",
                hint=f"It takes: {accepted}.")
        try:
            out[ttype].append(_resolve(token, ttype, vocab))
        except NotFound as e:
            raise UsageError(str(e)) from e

    return out


def reject_forced(args, command):
    """Raise a `UsageError` naming any `--date`/`--desk`/`--name`/`--group`/
    `--all` flag given to a command that has no grammar to route it into.

    `status`, `office-days`, `desks`, and `team` never call `bind()` --
    `desks`/`team` use `peel()` plus direct resolution instead, and
    `status`/`office-days` take no free-order tokens at all. Without this,
    those escape-hatch flags are simply never read by such a command, so
    `fs status --all` or `fs office-days --desk 217a` parse fine and do
    nothing -- exactly the "silently accept a typo" failure mode
    `bind()`'s own unknown-token error, and `match_desk`'s ambiguous-key
    error, exist to avoid everywhere else. `book`/`release`/`find`/`at`
    don't need this: each already rejects the specific forced types it
    doesn't accept, inline, as part of its own grammar check.
    """
    given = [flag for flag, attr in _FORCED_FLAGS if getattr(args, attr)]
    if given:
        raise UsageError(f"{command} does not take {', '.join(given)}")
