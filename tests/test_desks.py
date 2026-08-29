"""Desk key normalisation and matching.

`floorsense-api-manual.md` §6 warns that key formats vary by site and must not
be assumed parseable -- so normalisation is used ONLY for comparison, never to
reconstruct a key. Every function here that returns a key returns one that
came from the catalog verbatim. These tests cover both formats the manual has
actually seen: `L5.D.216A` and `D403-02`.
"""

import pytest

from fs_cli.desks import fmt_desk, match_desk, normalise
from fs_cli.errors import NotFound

CATALOG = ["L5.D.216A", "L5.D.217A", "L5.D.235A", "L5.D.301B", "L5.D.410A",
           "D403-02"]


# --- normalisation ---------------------------------------------------------

@pytest.mark.parametrize("raw", ["L5.D.235A", "5.235A", "5235a", "5235A",
                                 "l5.d.235a", "L5-D-235A", "5 235 A"])
def test_all_spellings_of_one_desk_normalise_alike(raw):
    assert normalise(raw) == "5235A"


def test_normalise_the_other_key_format():
    assert normalise("D403-02") == "40302"
    assert normalise("403-02") == "40302"
    assert normalise("40302") == "40302"


def test_normalise_is_idempotent():
    for raw in ["L5.D.235A", "D403-02", "5.217A"]:
        assert normalise(normalise(raw)) == normalise(raw)


# --- matching --------------------------------------------------------------

@pytest.mark.parametrize("token", ["L5.D.217A", "5.217A", "5217a", "5217A"])
def test_exact_normalised_match(token):
    assert match_desk(token, CATALOG) == "L5.D.217A"


def test_match_returns_the_catalog_key_verbatim():
    # The whole point: we match on a normalised form but hand back the real
    # key, because only the real key is valid against the API.
    assert match_desk("5217a", CATALOG) == "L5.D.217A"


def test_suffix_match_when_no_exact_match():
    assert match_desk("410A", CATALOG) == "L5.D.410A"


def test_exact_match_beats_suffix_match():
    catalog = ["L5.D.216A", "L6.D.5216A"]
    # "5216A" is exactly L5.D.216A and also a suffix of L6.D.5216A.
    assert match_desk("5.216A", catalog) == "L5.D.216A"


def test_ambiguous_match_lists_the_candidates():
    catalog = ["L5.D.217A", "L6.D.217A"]
    with pytest.raises(NotFound) as e:
        match_desk("217A", catalog)
    assert "L5.D.217A" in str(e.value) and "L6.D.217A" in str(e.value)


def test_no_match_raises_not_found():
    with pytest.raises(NotFound):
        match_desk("9.999Z", CATALOG)


def test_no_match_against_an_empty_catalog():
    with pytest.raises(NotFound):
        match_desk("5.217A", [])


def test_match_the_second_key_format():
    assert match_desk("403-02", CATALOG) == "D403-02"
    assert match_desk("D403-02", CATALOG) == "D403-02"


# --- printing --------------------------------------------------------------

def test_fmt_desk_drops_the_L_prefix_and_D_segment():
    assert fmt_desk("L5.D.217A") == "5.217A"


def test_fmt_desk_leaves_an_unrecognised_format_alone():
    # Never reconstruct. If it doesn't match the known shape, print it as-is.
    assert fmt_desk("D403-02") == "D403-02"


def test_fmt_desk_appends_group_rank():
    groups = {"favourite": ["L5.D.217A", "L5.D.235A", "L5.D.301B"]}
    assert fmt_desk("L5.D.217A", groups) == "5.217A (1st favourite)"
    assert fmt_desk("L5.D.235A", groups) == "5.235A (2nd favourite)"
    assert fmt_desk("L5.D.301B", groups) == "5.301B (3rd favourite)"


def test_fmt_desk_without_group_membership():
    groups = {"favourite": ["L5.D.217A"]}
    assert fmt_desk("L5.D.410A", groups) == "5.410A"


def test_fmt_desk_names_the_first_group_it_appears_in():
    groups = {"favourite": ["L5.D.217A"], "quiet-corner": ["L5.D.217A"]}
    assert fmt_desk("L5.D.217A", groups) == "5.217A (1st favourite)"


# --- tags -------------------------------------------------------------

def test_fmt_desk_appends_tags():
    assert (fmt_desk("L5.D.217A", tags=("quiet", "window"))
            == "5.217A [quiet, window]")


def test_fmt_desk_no_brackets_when_no_tags():
    assert fmt_desk("L5.D.217A", tags=()) == "5.217A"
    assert fmt_desk("L5.D.217A", tags=None) == "5.217A"


def test_fmt_desk_tags_come_after_group_rank():
    groups = {"favourite": ["L5.D.217A"]}
    assert (fmt_desk("L5.D.217A", groups, tags=("quiet",))
            == "5.217A (1st favourite) [quiet]")
