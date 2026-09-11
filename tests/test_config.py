"""Config load/save, permission repair, and the first-run heuristics.

Everything here is exercised against a tmp_path config dir -- no test may
touch the user's real ~/.config/fs, and none reads the OS keychain.
"""

import datetime as dt
import os
import stat

import pytest

from fs_cli.auth import FLOORSENSE_ORIGIN
from fs_cli.config import (Config, DEFAULT_DAY_OPENING_TIME,
                           DEFAULT_GROUP_NAME, DEFAULT_SHOW_TEAM_ON_MAP,
                           cache_filename, default_email, guess_okta_user,
                           load, save)


def cfg_dir(tmp_path):
    d = tmp_path / "fs"
    d.mkdir()
    return d


# --- the dot-in-shell-user heuristic ---------------------------------------

def test_shell_user_with_a_dot_is_used():
    # `jamie.baker` looks like an Okta login; `mike` doesn't.
    assert guess_okta_user("jamie.baker") == "jamie.baker"


def test_shell_user_without_a_dot_is_rejected():
    assert guess_okta_user("mike") is None
    assert guess_okta_user("") is None
    assert guess_okta_user(None) is None


def test_shell_user_with_more_than_one_dot_is_still_accepted():
    assert guess_okta_user("mary.anne.smith") == "mary.anne.smith"


def test_default_email_appends_the_configured_domain():
    assert default_email("jamie.baker", "example.com") == \
        "jamie.baker@example.com"


def test_default_email_leaves_an_address_alone():
    assert default_email("jamie.baker@example.com", "example.com") == \
        "jamie.baker@example.com"


# --- round-tripping --------------------------------------------------------

def test_missing_config_loads_as_empty(tmp_path):
    c = load(cfg_dir(tmp_path))
    assert c.okta_user is None
    assert c.groups == {} and c.teams == {}
    assert c.exists is False
    assert c.default_group == DEFAULT_GROUP_NAME
    assert c.show_team_on_map == DEFAULT_SHOW_TEAM_ON_MAP == "following"


def test_round_trip(tmp_path):
    d = cfg_dir(tmp_path)
    c = Config(okta_user="jamie.baker", email_domain="example.com",
               okta_org="example-corp.okta.com",
               office_days=["monday", "thursday"], book_ahead_days=10,
               default_group="quiet-corner",
               show_team_on_map="crew",
               groups={"quiet-corner": ["L5.D.217A", "L5.D.235A"]},
               teams={"crew": [{"uid": "93980719", "name": "Jane Doe"}]})
    save(c, d)
    back = load(d)
    assert back.okta_user == "jamie.baker"
    assert back.office_days == ["monday", "thursday"]
    assert back.default_group == "quiet-corner"
    assert back.show_team_on_map == "crew"
    assert back.groups["quiet-corner"] == ["L5.D.217A", "L5.D.235A"]
    assert back.teams["crew"][0]["name"] == "Jane Doe"
    assert back.exists is True


def test_default_group_defaults_when_config_toml_predates_the_setting(
        tmp_path):
    """A hand-edited or older config.toml with no `default_group` key under
    `[preferences]` must not crash `load()` -- it falls back to
    `DEFAULT_GROUP_NAME`, same tolerance as a missing `book_ahead_days`."""
    d = cfg_dir(tmp_path)
    (d / "config.toml").write_text(
        '[identity]\nokta_user = "jamie.baker"\n\n[preferences]\n')
    assert load(d).default_group == DEFAULT_GROUP_NAME
    assert load(d).show_team_on_map == DEFAULT_SHOW_TEAM_ON_MAP


# --- cache_filename: origin-scoped so `--url` can't reuse another
# deployment's desk catalog -------------------------------------------------

def test_cache_filename_is_the_bare_name_for_the_default_origin():
    assert cache_filename(FLOORSENSE_ORIGIN) == "cache.json"


def test_cache_filename_is_the_bare_name_for_no_origin():
    # `cfg.floorsense_url or auth.FLOORSENSE_ORIGIN` never actually
    # passes None, but a bare default is the safe empty-input answer.
    assert cache_filename(None) == "cache.json"


def test_cache_filename_is_scoped_by_host_for_a_different_origin():
    assert (cache_filename("https://other.example.com")
            == "cache-other.example.com.json")


def test_cache_filename_differs_between_two_non_default_origins():
    a = cache_filename("https://one.example.com")
    b = cache_filename("https://two.example.com")
    assert a != b != "cache.json"




def test_saved_config_is_valid_toml_a_human_can_edit(tmp_path):
    d = cfg_dir(tmp_path)
    save(Config(okta_user="jamie.baker",
                groups={"quiet-corner": ["L5.D.410A"]}), d)
    text = (d / "config.toml").read_text()
    assert "[identity]" in text and "[groups]" in text
    assert "quiet-corner" in text


def test_config_holds_no_secrets(tmp_path):
    # The whole reason config.toml and session.json are separate files: this
    # one must stay safe to open, diff or paste.
    d = cfg_dir(tmp_path)
    save(Config(okta_user="jamie.baker"), d)
    text = (d / "config.toml").read_text().lower()
    for forbidden in ("password", "token", "cookie", "secret"):
        assert forbidden not in text


def test_email_is_derived_not_stored(tmp_path):
    d = cfg_dir(tmp_path)
    save(Config(okta_user="jamie.baker", email_domain="example.com"), d)
    assert load(d).email == "jamie.baker@example.com"


# --- permissions -----------------------------------------------------------

def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_save_creates_the_dir_0700_and_the_file_0600(tmp_path):
    d = tmp_path / "fs"          # deliberately not pre-created
    save(Config(okta_user="jamie.baker"), d)
    assert _mode(d) == 0o700
    assert _mode(d / "config.toml") == 0o600


def test_load_repairs_loose_permissions_and_says_so(tmp_path):
    d = cfg_dir(tmp_path)
    save(Config(okta_user="jamie.baker"), d)
    os.chmod(d, 0o755)
    os.chmod(d / "config.toml", 0o644)

    notes = []
    load(d, on_repair=notes.append)

    assert _mode(d) == 0o700
    assert _mode(d / "config.toml") == 0o600
    assert notes, "a silent permission repair is a repair nobody learns from"
    assert any("0600" in n or "0700" in n for n in notes)


def test_load_is_quiet_when_permissions_are_already_right(tmp_path):
    d = cfg_dir(tmp_path)
    save(Config(okta_user="jamie.baker"), d)
    notes = []
    load(d, on_repair=notes.append)
    assert notes == []


# --- validation ------------------------------------------------------------

def test_office_days_are_normalised_to_weekday_indexes(tmp_path):
    c = Config(office_days=["Monday", "thu", "TUES"])
    assert c.office_day_indexes() == [0, 1, 3]      # sorted, deduped


def test_unknown_office_day_is_ignored_rather_than_crashing(tmp_path):
    # A hand-edited config.toml is expected; a typo must not make every
    # command explode.
    assert Config(office_days=["monday", "funday"]).office_day_indexes() == [0]


def test_corrupt_toml_is_a_usage_error_naming_the_file(tmp_path):
    from fs_cli.errors import UsageError
    d = cfg_dir(tmp_path)
    (d / "config.toml").write_text("this is not [ valid toml")
    with pytest.raises(UsageError) as exc:
        load(d)
    assert "config.toml" in str(exc.value)


def test_next_office_days_walks_forward_from_today():
    c = Config(office_days=["monday", "tuesday", "thursday"],
               book_ahead_days=10)
    fri = dt.date(2026, 8, 21)
    got = c.next_office_days(fri)
    # Strictly after today, within book_ahead_days.
    assert got[0] == dt.date(2026, 8, 24)
    assert all(d > fri for d in got)
    assert all((d - fri).days <= 10 for d in got)
    assert got == sorted(got)


def test_next_office_days_is_empty_when_none_configured():
    assert Config(office_days=[]).next_office_days(dt.date(2026, 8, 21)) == []


# --- day_opening_time / classify_booking_date -------------------------

TODAY = dt.date(2026, 8, 21)


def _at(hh, mm):
    return dt.datetime.combine(TODAY, dt.time(hh, mm))


def test_day_opening_time_defaults_and_is_always_saved(tmp_path):
    d = cfg_dir(tmp_path)
    c = Config(okta_user="jamie.baker")
    assert c.day_opening_time == DEFAULT_DAY_OPENING_TIME == "08:05"
    save(c, d)
    text = (d / "config.toml").read_text()
    assert 'day_opening_time = "08:05"' in text


def test_day_opening_time_is_preserved_when_hand_added(tmp_path):
    d = cfg_dir(tmp_path)
    (d / "config.toml").write_text(
        '[identity]\nokta_user = "jamie.baker"\n\n'
        '[preferences]\nday_opening_time = "07:00"\n')
    c = load(d)
    assert c.day_opening_time == "07:00"
    save(c, d)
    assert load(d).day_opening_time == "07:00"
    assert 'day_opening_time = "07:00"' in (d / "config.toml").read_text()


def test_day_opening_time_missing_key_loads_as_the_default_and_gets_written(
        tmp_path):
    """A hand-edited config.toml with no `day_opening_time` key must not
    crash `load()` -- it falls back to `DEFAULT_DAY_OPENING_TIME`, same
    tolerance as a missing `book_ahead_days`, and the next save writes it
    in like every other preference."""
    d = cfg_dir(tmp_path)
    (d / "config.toml").write_text(
        '[identity]\nokta_user = "jamie.baker"\n\n[preferences]\n')
    c = load(d)
    assert c.day_opening_time == DEFAULT_DAY_OPENING_TIME
    save(c, d)
    assert 'day_opening_time = "08:05"' in (d / "config.toml").read_text()


def test_classify_booking_date_rejects_a_past_date():
    c = Config()
    yesterday = TODAY - dt.timedelta(days=1)
    assert c.classify_booking_date(yesterday, TODAY, _at(9, 0)) == "past"


def test_classify_booking_date_before_default_cutoff_allows_today():
    c = Config(book_ahead_days=10)
    assert c.classify_booking_date(TODAY, TODAY, _at(8, 0)) == "ok"


def test_classify_booking_date_after_default_cutoff_blocks_today():
    c = Config(book_ahead_days=10)
    assert c.classify_booking_date(TODAY, TODAY, _at(9, 0)) == "closed"


def test_classify_booking_date_window_shifts_with_the_cutoff():
    c = Config(book_ahead_days=10)
    before = TODAY + dt.timedelta(days=9)
    after = TODAY + dt.timedelta(days=10)
    assert c.classify_booking_date(before, TODAY, _at(8, 0)) == "ok"
    assert c.classify_booking_date(before + dt.timedelta(days=1),
                                   TODAY, _at(8, 0)) == "too_far"
    assert c.classify_booking_date(after, TODAY, _at(9, 0)) == "ok"
    assert c.classify_booking_date(after + dt.timedelta(days=1),
                                   TODAY, _at(9, 0)) == "too_far"


def test_classify_booking_date_honours_a_configured_cutoff():
    c = Config(book_ahead_days=10, day_opening_time="07:00")
    assert c.classify_booking_date(TODAY, TODAY, _at(6, 59)) == "ok"
    assert c.classify_booking_date(TODAY, TODAY, _at(7, 0)) == "closed"


def test_invalid_day_opening_time_falls_back_to_the_default():
    c = Config(book_ahead_days=10, day_opening_time="not-a-time")
    assert c.classify_booking_date(TODAY, TODAY, _at(8, 0)) == "ok"
    assert c.classify_booking_date(TODAY, TODAY, _at(9, 0)) == "closed"


def test_day_opening_time_never_appears_in_status_source():
    import inspect
    from fs_cli.commands import status_cmd
    assert "day_opening_time" not in inspect.getsource(status_cmd)
