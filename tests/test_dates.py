"""Date grammar. `today` is always injected -- these tests must never depend
on the real clock, and the module must never read it.

The worked examples in PLAN.md are pinned here verbatim: from Fri 21 Aug 2026
`tue` and `tue-next` coincide (the weekend falls between), and from Mon 24 Aug
they diverge. That divergence is the entire point of the `-next` suffix, so it
gets its own tests rather than being folded into a loop.
"""

import datetime as dt

import pytest

from fs_cli.dates import fmt_date, is_date_token, parse_date, split_past

FRI = dt.date(2026, 8, 21)      # a Friday
SAT = dt.date(2026, 8, 22)      # the following Saturday
SUN = dt.date(2026, 8, 23)      # the following Sunday
MON = dt.date(2026, 8, 24)      # the following Monday


def p(token, today=FRI):
    return parse_date(token, today)


# --- today / tomorrow ------------------------------------------------------

def test_today_and_tomorrow():
    assert p("today") == FRI
    assert p("tomorrow") == dt.date(2026, 8, 22)


def test_case_insensitive():
    assert p("ToDay") == FRI
    assert p("MONDAY") == MON


# --- bare weekdays: strictly after today -----------------------------------

@pytest.mark.parametrize("token,expected", [
    ("mon", dt.date(2026, 8, 24)),
    ("monday", dt.date(2026, 8, 24)),
    ("tue", dt.date(2026, 8, 25)),
    ("tues", dt.date(2026, 8, 25)),
    ("wed", dt.date(2026, 8, 26)),
    ("thu", dt.date(2026, 8, 27)),
    ("thur", dt.date(2026, 8, 27)),
    ("thurs", dt.date(2026, 8, 27)),
    ("fri", dt.date(2026, 8, 28)),      # today is Friday -> NEXT Friday
    ("sat", dt.date(2026, 8, 22)),
    ("sun", dt.date(2026, 8, 23)),
])
def test_bare_weekday_is_strictly_after_today(token, expected):
    assert p(token) == expected


def test_today_is_never_returned_by_its_own_weekday_name():
    # PLAN.md: "`mon` on a Monday means next Monday."
    assert p("mon", today=MON) == dt.date(2026, 8, 31)


# --- the -next suffix ------------------------------------------------------

def test_next_suffix_coincides_when_a_weekend_falls_between():
    # From Fri 21 Aug: next Monday is 24 Aug; the first Tue on/after that is
    # 25 Aug -- which is also the plain `tue`. Correctly identical.
    assert p("tue-next") == dt.date(2026, 8, 25)
    assert p("tue-next") == p("tue")


def test_next_suffix_diverges_from_a_weekday():
    # From Mon 24 Aug: `tue` is tomorrow, `tue-next` is the week after.
    assert p("tue", today=MON) == dt.date(2026, 8, 25)
    assert p("tue-next", today=MON) == dt.date(2026, 9, 1)


def test_mon_next_from_a_friday():
    # "the first Monday on or after next Monday" -- next Monday itself.
    assert p("mon-next") == dt.date(2026, 8, 24)


def test_mon_next_from_a_monday():
    assert p("mon-next", today=MON) == dt.date(2026, 8, 31)


def test_next_suffix_on_a_weekend_day():
    # Next Monday is 24 Aug; the first Saturday on/after it is 29 Aug.
    assert p("sat-next") == dt.date(2026, 8, 29)


def test_next_suffix_from_a_sunday_does_not_collapse_onto_the_plain_form():
    # The bug this pins: `-next` used to anchor on the next MONDAY, which
    # from a Sunday is just tomorrow -- so `tue-next` and plain `tue` both
    # landed on the same nearby Tuesday (25 Aug), defeating the entire
    # point of `-next`. Anchoring on the next Saturday instead (29 Aug,
    # six days out) pushes `tue-next` a full week further, to 1 Sep --
    # the same date `tue-next` resolves to from the following Monday
    # (`test_next_suffix_diverges_from_a_weekday`), as it must: both days
    # sit in the same "current week" relative to the next Saturday.
    assert p("tue", today=SUN) == dt.date(2026, 8, 25)
    assert p("tue-next", today=SUN) == dt.date(2026, 9, 1)
    assert p("tue-next", today=SUN) != p("tue", today=SUN)


def test_sun_next_does_not_collapse_onto_the_plain_form():
    # The bug this pins: Saturday and Sunday are adjacent, so the
    # Saturday-anchor algorithm that works for every other weekday always
    # landed `sun-next` exactly one day after the anchor -- which is also
    # where plain `sun` already lands, for every starting weekday except
    # Saturday. `sun-next` could never reach a date `sun` alone couldn't,
    # defeating the entire point of the suffix.
    assert p("sun", today=MON) == dt.date(2026, 8, 30)
    assert p("sun-next", today=MON) == dt.date(2026, 9, 6)
    assert p("sun-next", today=MON) != p("sun", today=MON)

    assert p("sun", today=FRI) == dt.date(2026, 8, 23)
    assert p("sun-next", today=FRI) == dt.date(2026, 8, 30)
    assert p("sun-next", today=FRI) != p("sun", today=FRI)


def test_sun_next_from_a_saturday_still_rolls_a_full_week_forward():
    # The one starting day the old algorithm already got right for Sunday
    # -- pinned so the fix above doesn't regress it.
    assert p("sun", today=SAT) == dt.date(2026, 8, 23)
    assert p("sun-next", today=SAT) == dt.date(2026, 8, 30)


def test_next_suffix_from_a_saturday_rolls_a_full_week_forward():
    # `-next`'s anchor Saturday is STRICTLY after today, never today itself
    # -- from a Saturday, that's a week away (29 Aug), not today. So
    # `sat-next` from a Saturday names the Saturday AFTER that one (5 Sep,
    # two weeks out), not the trivial next Saturday `sat` alone would
    # already give.
    assert p("sat-next", today=SAT) == dt.date(2026, 9, 5)
    assert p("mon-next", today=SAT) == dt.date(2026, 8, 31)


# --- day of month ----------------------------------------------------------

@pytest.mark.parametrize("token", ["28", "28th"])
def test_day_of_month_this_month(token):
    assert p(token) == dt.date(2026, 8, 28)


def test_day_of_month_rolls_into_next_month_when_past():
    assert p("3") == dt.date(2026, 9, 3)
    assert p("3rd") == dt.date(2026, 9, 3)


def test_day_of_month_is_strictly_after_today():
    assert p("21") == dt.date(2026, 9, 21)      # today is the 21st
    assert p("22") == dt.date(2026, 8, 22)


def test_day_of_month_skips_short_months():
    # From 21 Aug, "31" -> 31 Aug. From 1 Sep, "31" must skip September.
    assert p("31") == dt.date(2026, 8, 31)
    assert p("31", today=dt.date(2026, 9, 1)) == dt.date(2026, 10, 31)


def test_day_of_month_rejects_impossible_days():
    assert p("32") is None
    assert p("0") is None


# --- explicit dd/mm forms --------------------------------------------------

def test_ddmm_this_year():
    assert p("25/12") == dt.date(2026, 12, 25)


def test_ddmm_rolls_to_next_year_when_past():
    assert p("01/01") == dt.date(2027, 1, 1)


def test_ddmm_today_stays_this_year():
    # Today is not "already past".
    assert p("21/08") == FRI


def test_ddmm_single_digits():
    assert p("5/9") == dt.date(2026, 9, 5)


def test_ddmm_explicit_years():
    assert p("25/12/27") == dt.date(2027, 12, 25)
    assert p("25/12/2027") == dt.date(2027, 12, 25)
    assert p("01/01/2020") == dt.date(2020, 1, 1)      # the past is parseable


def test_ddmm_is_day_first_not_month_first():
    # 03/04 is 3 April, never 4 March -- this is NZ, and getting it backwards
    # books the wrong day silently.
    assert p("03/04") == dt.date(2027, 4, 3)


def test_invalid_dates_are_not_dates():
    assert p("31/02") is None
    assert p("13/13") is None
    assert p("hello") is None
    assert p("") is None
    assert p("jane") is None


# --- ISO 8601 forms ---------------------------------------------------------

def test_iso_dashed():
    assert p("2026-08-25") == dt.date(2026, 8, 25)


def test_iso_compact():
    assert p("20260825") == dt.date(2026, 8, 25)


def test_iso_forms_agree_with_each_other():
    assert p("2026-12-25") == p("20261225") == dt.date(2026, 12, 25)


def test_iso_past_year_is_parseable_like_ddmmyyyy_is():
    # Same "the past is parseable" contract dd/mm/yyyy already has --
    # neither ISO form ever rolls to a later year the way bare dd/mm does.
    assert p("2020-01-01") == dt.date(2020, 1, 1)
    assert p("20200101") == dt.date(2020, 1, 1)


def test_iso_invalid_dates_are_not_dates():
    assert p("2026-13-01") is None       # no month 13
    assert p("2026-02-30") is None       # no 30 Feb
    assert p("20261301") is None
    assert p("20260230") is None


def test_iso_does_not_collide_with_day_of_month_or_ddmm():
    # `_DOM_RE` only ever matches 1-2 digits, `_SLASH_RE` needs a `/` --
    # an 8-digit token and a dashed yyyy-mm-dd token can't reach either.
    assert p("28") == dt.date(2026, 8, 28)          # still day-of-month
    assert p("25/12") == dt.date(2026, 12, 25)       # still dd/mm


def test_an_8_digit_non_date_still_falls_through_to_none():
    # The reverse direction of the collision check above: adding
    # `_ISO_COMPACT_RE` must not make some OTHER 8-digit token (a bkid, a
    # desk key typed without its letters) look like a date to the
    # classifier -- it has to fail calendar validation and fall through,
    # same as any other not-a-date token already does.
    assert p("99999999") is None
    assert p("00853044") is None      # shaped like a real captured bkid


# --- classification --------------------------------------------------------

def test_is_date_token_matches_parse():
    for token in ["today", "mon", "28th", "25/12", "tue-next"]:
        assert is_date_token(token, FRI) is True
    for token in ["jane", "5.217A", "favourite", ""]:
        assert is_date_token(token, FRI) is False


def test_a_persons_name_that_looks_like_a_weekday_is_a_date():
    # Documented consequence, not a bug: `fs find mon` can never mean Monica.
    # The escape hatch is `--name mon`, tested in test_args.py.
    assert is_date_token("mon", FRI) is True


# --- printing --------------------------------------------------------------

def test_fmt_date_relative_words():
    assert fmt_date(FRI, FRI) == "Today 21st Aug"
    assert fmt_date(dt.date(2026, 8, 22), FRI) == "Tomorrow 22nd Aug"


def test_fmt_date_weekday_form():
    assert fmt_date(MON, FRI) == "Monday 24th Aug"


def test_fmt_date_appends_year_only_when_not_current():
    assert fmt_date(dt.date(2026, 12, 25), FRI) == "Friday 25th Dec"
    assert fmt_date(dt.date(2027, 1, 1), FRI) == "Friday 1st Jan 2027"


@pytest.mark.parametrize("day,suffix", [
    (1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"),
    (11, "11th"), (12, "12th"), (13, "13th"),      # the ones naive code breaks
    (21, "21st"), (22, "22nd"), (23, "23rd"), (30, "30th"), (31, "31st"),
])
def test_ordinal_suffixes(day, suffix):
    # `today` is deliberately in a different month, so that day 1 and day 2
    # don't come back as "Today"/"Tomorrow" and skip the ordinal entirely.
    got = fmt_date(dt.date(2026, 5, day), dt.date(2026, 3, 10))
    assert suffix in got, got


def test_yesterday_is_not_special_cased():
    # Past dates print normally rather than as "Yesterday" -- nothing in the
    # tool ever shows one, so a special case would be untested surface.
    assert fmt_date(dt.date(2026, 8, 20), FRI) == "Thursday 20th Aug"


# --- split_past --------------------------------------------------------

def test_split_past_separates_before_and_on_or_after_today():
    yesterday = dt.date(2026, 8, 20)
    past, rest = split_past([yesterday, FRI, MON], FRI)
    assert past == [yesterday]
    assert rest == [FRI, MON]


def test_split_past_counts_today_as_rest():
    past, rest = split_past([FRI], FRI)
    assert past == []
    assert rest == [FRI]


def test_split_past_with_no_dates():
    assert split_past([], FRI) == ([], [])
