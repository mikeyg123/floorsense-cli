"""Token classification and free-order binding.

The design claim being tested: because tokens are classified by TYPE and then
bound by type rather than position, `fs at 5.235 mon tues` and
`fs at mon tues 5.235` must produce identical bindings with no per-command
ordering logic anywhere.
"""

import datetime as dt

import pytest

from fs_cli.args import TokenType as T
from fs_cli.args import Vocabulary, bind, classify, split_list, peel
from fs_cli.errors import UsageError

FRI = dt.date(2026, 8, 21)

VOCAB = Vocabulary(
    today=FRI,
    groups={"favourite": ["L5.D.217A"], "quiet-corner": ["L5.D.410A"]},
    teams={"crew": [], "following": []},
    desk_keys=["L5.D.217A", "L5.D.235A", "L5.D.410A", "D403-02"],
)


# --- classification precedence ---------------------------------------------

@pytest.mark.parametrize("token,expected", [
    ("mon", T.DATE),
    ("28th", T.DATE),
    ("25/12", T.DATE),
    ("favourite", T.GROUP),
    ("quiet-corner", T.GROUP),
    ("crew", T.TEAM),
    ("following", T.TEAM),
    ("5.235A", T.DESK),
    ("403-02", T.DESK),
    ("jane", T.NAME),
    ("Bob Smith", T.NAME),
])
def test_classification(token, expected):
    assert classify(token, VOCAB) is expected


def test_a_group_named_like_a_date_is_still_a_date():
    # Documented shadowing consequence. `team`/`desks` warn at creation time,
    # and --group is the escape hatch; precedence itself stays deterministic.
    vocab = Vocabulary(today=FRI, groups={"monday": []}, teams={},
                       desk_keys=[])
    assert classify("monday", vocab) is T.DATE


def test_a_team_named_like_a_number_is_still_a_date():
    vocab = Vocabulary(today=FRI, groups={}, teams={"28": []}, desk_keys=[])
    assert classify("28", vocab) is T.DATE


def test_group_beats_team_beats_desk():
    vocab = Vocabulary(today=FRI, groups={"x": []}, teams={"x": []},
                       desk_keys=["X"])
    assert classify("x", vocab) is T.GROUP


def test_classification_is_case_insensitive_for_groups_and_teams():
    assert classify("FAVOURITE", VOCAB) is T.GROUP
    assert classify("Crew", VOCAB) is T.TEAM


# --- list splitting --------------------------------------------------------

def test_whitespace_splits_when_there_are_no_commas():
    assert split_list(["jane", "bob"]) == ["jane", "bob"]


def test_commas_split_and_allow_names_with_spaces():
    assert split_list(["jane", "doe,", "bob", "smith"]) == \
        ["jane doe", "bob smith"]


def test_comma_splitting_tolerates_spacing():
    assert split_list(["jane", ",", "bob"]) == ["jane", "bob"]
    assert split_list(["jane,bob"]) == ["jane", "bob"]


def test_trailing_comma_produces_no_empty_token():
    assert split_list(["jane,", "bob,"]) == ["jane", "bob"]


def test_empty_input():
    assert split_list([]) == []


def test_a_quoted_multiword_token_survives_without_a_comma():
    # `fs list "nathan k"` -- the shell hands this over as ONE argv token.
    # Regression: the old join-then-resplit-on-whitespace implementation
    # flattened it right back into two bare-word tokens.
    assert split_list(["nathan k"]) == ["nathan k"]


# --- peeling leading positionals -------------------------------------------

VERBS = ("add", "remove", "set", "delete")


def test_peel_name_and_verb():
    name, verb, rest = peel(["crew", "add", "jane", "doe"], VERBS)
    assert (name, verb, rest) == ("crew", "add", ["jane", "doe"])


def test_peel_without_a_verb():
    assert peel(["crew"], VERBS) == ("crew", None, [])


def test_peel_nothing():
    assert peel([], VERBS) == (None, None, [])


def test_peel_leaves_a_non_verb_second_token_in_the_rest():
    # `fs team crew jane` -- a usage error, but peel's job is only to report
    # accurately that no verb was given. cli.py turns that into the error.
    assert peel(["crew", "jane"], VERBS) == ("crew", None, ["jane"])


def test_peeling_happens_before_comma_splitting():
    # The reason peel exists: without it, `crew add jane doe, bob smith`
    # joins into one string and splits into ["crew add jane doe", ...].
    name, verb, rest = peel(["crew", "add", "jane", "doe,", "bob", "smith"],
                            VERBS)
    assert split_list(rest) == ["jane doe", "bob smith"]


def test_peel_accepts_the_verb_first():
    # `fs team add crew jane` == `fs team crew add jane` -- unambiguous
    # since "crew" isn't a configured name that could itself be a verb.
    assert peel(["add", "crew", "jane"], VERBS) == ("crew", "add", ["jane"])


def test_peel_name_first_wins_when_both_positions_are_verb_literals():
    # `fs team add remove jane` -- token 2 being a verb literal is checked
    # first, so this reads as name="add", verb="remove", same as it always
    # has; verb-first parsing never gets a chance to fire here.
    assert peel(["add", "remove", "jane"], VERBS) == ("add", "remove", ["jane"])


def test_peel_verb_first_is_suppressed_by_a_real_name_collision():
    # A team/group genuinely named "add" keeps its old meaning -- name,
    # no verb -- rather than being silently reinterpreted as the verb.
    assert peel(["add", "jane"], VERBS, known_names=["add"]) == \
        ("add", None, ["jane"])


def test_peel_verb_first_needs_at_least_two_tokens():
    # A lone verb-shaped token with nothing after it is still just a name
    # with no verb -- there's nothing to peel it away from.
    assert peel(["add"], VERBS) == ("add", None, [])


# --- free-order binding ----------------------------------------------------

def test_binding_is_order_independent():
    accepts = (T.DESK, T.DATE)
    a = bind(["5.235A", "mon", "tues"], VOCAB, accepts)
    b = bind(["mon", "tues", "5.235A"], VOCAB, accepts)
    assert a == b
    assert a[T.DESK] == ["L5.D.235A"]
    assert a[T.DATE] == [dt.date(2026, 8, 24), dt.date(2026, 8, 25)]


def test_binding_preserves_order_within_a_type():
    got = bind(["tues", "mon"], VOCAB, (T.DATE,))
    assert got[T.DATE] == [dt.date(2026, 8, 25), dt.date(2026, 8, 24)]


def test_binding_resolves_desks_to_real_catalog_keys():
    got = bind(["5235a"], VOCAB, (T.DESK,))
    assert got[T.DESK] == ["L5.D.235A"]


def test_binding_rejects_a_type_the_command_does_not_accept():
    with pytest.raises(UsageError) as e:
        bind(["crew"], VOCAB, (T.DATE, T.DESK))
    assert "crew" in str(e.value)


def test_binding_an_empty_arg_list():
    got = bind([], VOCAB, (T.DATE, T.DESK))
    assert got[T.DATE] == [] and got[T.DESK] == []


def test_forced_types_bypass_classification():
    # The `--name mon` escape hatch: the whole reason it exists is that `mon`
    # would otherwise classify as a date and could never mean Monica.
    got = bind([], VOCAB, (T.NAME, T.DATE), forced={T.NAME: ["mon"]})
    assert got[T.NAME] == ["mon"]
    assert got[T.DATE] == []


def test_forced_dates_are_still_parsed():
    got = bind([], VOCAB, (T.DATE,), forced={T.DATE: ["mon"]})
    assert got[T.DATE] == [dt.date(2026, 8, 24)]


def test_forced_desks_are_still_resolved():
    got = bind([], VOCAB, (T.DESK,), forced={T.DESK: ["5235a"]})
    assert got[T.DESK] == ["L5.D.235A"]


def test_a_forced_date_that_is_not_a_date_is_a_usage_error():
    with pytest.raises(UsageError):
        bind([], VOCAB, (T.DATE,), forced={T.DATE: ["jane"]})


def test_a_forced_group_that_does_not_exist_is_a_usage_error():
    # Group F: `_resolve()` used a bare `next()` with no default here, so a
    # typo'd `--group` name raised an unhandled StopIteration instead of a
    # usage error.
    with pytest.raises(UsageError):
        bind([], VOCAB, (T.GROUP,), forced={T.GROUP: ["nonesuch"]})


def test_a_forced_team_that_does_not_exist_is_a_usage_error():
    with pytest.raises(UsageError):
        bind([], VOCAB, (T.TEAM,), forced={T.TEAM: ["nonesuch"]})


def test_a_plain_token_that_classifies_but_fails_to_resolve_is_a_usage_error():
    """PLAN.md's "Next up" item 2: `classify()`'s `has_group` and
    `_resolve()`'s own group lookup are independently-written predicates
    with no shared source of truth. They agree today, so this can't
    actually happen through the public vocabulary -- but if they ever
    diverge (e.g. `has_group` grows fuzzy matching before `_resolve`
    does), the plain-token path must still surface a `UsageError` (exit
    2), the same as the forced-flag path already guarantees for the
    identical mistake, not a bare `NotFound` (exit 8)."""
    class LenientVocab(Vocabulary):
        def has_group(self, token):
            return True  # accepts anything, unlike the real predicate

    vocab = LenientVocab(today=FRI, groups=VOCAB.groups, teams=VOCAB.teams,
                         desk_keys=VOCAB.desk_keys)
    with pytest.raises(UsageError):
        bind(["nonesuch"], vocab, (T.GROUP,))
