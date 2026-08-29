"""Configuration: `config.toml` (human, no secrets) plus machine-owned files.

The brief wanted config both hand-editable AND able to hold tokens, which is
a tension only until you split the files:

    ~/.config/fs/config.toml    human-editable, no secrets, safe to paste
    ~/.config/fs/session.json   machine-owned: the live cookie pair
    ~/.config/fs/cache.json     machine-owned: desk catalog, locker, policy

The Okta password lives in the OS keychain via `keyring` and never in any
file. So `config.toml` can be opened, diffed and shared without a second
thought, and the credential material lives in files nobody hand-edits.

Directory `0700`, files `0600`, repaired on every load with a printed note
when they were wrong -- a silent repair is one nobody learns from.
"""

import datetime as dt
import os
import pathlib
import stat
import sys
import tomllib
from dataclasses import dataclass, field

import tomli_w

from .dates import WEEKDAYS
from .errors import UsageError

__all__ = ["Config", "config_dir", "load", "save", "guess_okta_user",
           "default_email", "DEFAULT_ORG", "DEFAULT_DOMAIN",
           "DEFAULT_GROUP_NAME", "BOOKING_BLOCK_REASONS"]

#: Placeholders, not a working default for any deployment -- every Okta org
#: and email domain differs, so these can never be a *correct* guess, only
#: a stand-in that keeps `default_email()`/`Config()` from producing `None`
#: or a blank string when a `Config` is built without going through
#: first-run (tests, an already-existing but hand-edited config.toml
#: missing the key). First run itself (`cli.first_run`) never falls back
#: to either silently -- it asks for both explicitly (interactive) or
#: refuses outright (non-interactive) instead.
DEFAULT_ORG = "example.okta.com"
DEFAULT_DOMAIN = "example.com"
DEFAULT_BOOK_AHEAD_DAYS = 10

#: The `[groups]` name `fs book`/`fs at`/etc. reach for when given no desk
#: or group of their own. Hand-editable like any other group name -- read
#: via `Config.default_group`, not hardcoded elsewhere.
DEFAULT_GROUP_NAME = "preferred"

DIR_MODE = 0o700
FILE_MODE = 0o600


def config_dir():
    """`~/.config/fs` on macOS/Linux, `%APPDATA%\\fs` on Windows."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or (pathlib.Path.home() / "AppData"
                                             / "Roaming")
        return pathlib.Path(base) / "fs"
    base = os.environ.get("XDG_CONFIG_HOME") or (pathlib.Path.home()
                                                 / ".config")
    return pathlib.Path(base) / "fs"


def guess_okta_user(shell_user):
    """The Okta login looks like `firstname.lastname`, so a shell user
    containing a dot is a good guess and one without is not: `jdoe` is not
    an Okta login, `jane.doe` probably is. Returning None means "ask"."""
    if not shell_user or "." not in shell_user:
        return None
    return shell_user


def default_email(okta_user, domain=DEFAULT_DOMAIN):
    """Floorsense's form wants the full email, and it is only ever used to
    set `login_hint` (`floorsense-api-manual.md` §4.2). Deriving it means the
    user only ever types `jane.doe`."""
    if not okta_user:
        return None
    if "@" in okta_user:
        return okta_user
    return f"{okta_user}@{domain}"


@dataclass
class Config:
    okta_user: str = None
    email_domain: str = DEFAULT_DOMAIN
    okta_org: str = DEFAULT_ORG
    #: None means the built-in default (`auth.FLOORSENSE_ORIGIN`); only set
    #: when `--url` pointed somewhere else.
    floorsense_url: str = None
    office_days: list = field(default_factory=list)
    book_ahead_days: int = DEFAULT_BOOK_AHEAD_DAYS
    default_group: str = DEFAULT_GROUP_NAME
    groups: dict = field(default_factory=dict)
    teams: dict = field(default_factory=dict)
    day_opening_time: str = None
    #: False when no config.toml was on disk -- triggers first-run setup.
    exists: bool = False

    @property
    def email(self):
        return default_email(self.okta_user, self.email_domain)

    def office_day_indexes(self):
        """Configured weekdays as sorted, deduped Monday=0 indexes.

        Unknown values are dropped rather than raising: `config.toml` is
        meant to be hand-edited, and one typo must not make every command
        explode.
        """
        seen = {WEEKDAYS[d.strip().lower()] for d in self.office_days
                if d and d.strip().lower() in WEEKDAYS}
        return sorted(seen)

    def next_office_days(self, today):
        """Office days strictly after today, within `book_ahead_days`.

        This is what `fs book` uses when given no dates. Strictly after,
        because today is not bookable.
        """
        wanted = set(self.office_day_indexes())
        if not wanted:
            return []
        out = []
        for offset in range(1, max(0, self.book_ahead_days) + 1):
            d = today + dt.timedelta(days=offset)
            if d.weekday() in wanted:
                out.append(d)
        return out

    def booking_window(self, today, now):
        return _booking_window(today, now, self.book_ahead_days,
                               self.day_opening_time)

    def classify_booking_date(self, day, today, now):
        return _classify_booking_date(day, today, now, self.book_ahead_days,
                                      self.day_opening_time)


_DEFAULT_OPENING_TIME = dt.time(8, 35)


def _opening_time(day_opening_time):
    if day_opening_time:
        try:
            h, m = day_opening_time.split(":")
            return dt.time(int(h), int(m))
        except (TypeError, ValueError):
            pass
    return _DEFAULT_OPENING_TIME


def _booking_window(today, now, book_ahead_days, day_opening_time):
    before_opening = now.time() < _opening_time(day_opening_time)
    start = today if before_opening else today + dt.timedelta(days=1)
    span = max(0, book_ahead_days) - (1 if before_opening else 0)
    end = today + dt.timedelta(days=max(0, span))
    return start, end


def _classify_booking_date(day, today, now, book_ahead_days, day_opening_time):
    if day < today:
        return "past"
    start, end = _booking_window(today, now, book_ahead_days, day_opening_time)
    if day < start:
        return "closed"
    if day > end:
        return "too_far"
    return "ok"


BOOKING_BLOCK_REASONS = {
    "past": "can't book past date",
    "closed": "cannot book for today after the day opening time",
    "too_far": "advance bookings cannot be made this far in the future",
}


# --------------------------------------------------------------------------
# permissions
# --------------------------------------------------------------------------

def _repair(path, want, on_repair):
    """Fix loose permissions, reporting what was changed. A stored cookie
    pair is a live authenticated session (§11), so this is not cosmetic."""
    try:
        current = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return
    if current == want:
        return
    try:
        os.chmod(path, want)
    except OSError:
        return
    if on_repair:
        on_repair(f"Fixed permissions on {path} ({current:04o} -> {want:04o})")


def ensure_dir(directory, on_repair=None):
    directory = pathlib.Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    _repair(directory, DIR_MODE, on_repair)
    return directory


# --------------------------------------------------------------------------
# load / save
# --------------------------------------------------------------------------

def load(directory=None, on_repair=None):
    """Read `config.toml`, repairing permissions on the way past.

    A missing file is not an error -- it is the first-run signal, and comes
    back as a default Config with `exists=False`.
    """
    directory = pathlib.Path(directory or config_dir())
    path = directory / "config.toml"
    if not path.exists():
        return Config()

    _repair(directory, DIR_MODE, on_repair)
    _repair(path, FILE_MODE, on_repair)

    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise UsageError(f"{path} is not valid TOML: {e}",
                         hint="Fix it by hand, or delete it to start over.") \
            from e
    except OSError as e:
        raise UsageError(f"could not read {path}: {e}") from e

    identity = raw.get("identity", {})
    prefs = raw.get("preferences", {})
    return Config(
        okta_user=identity.get("okta_user"),
        email_domain=identity.get("email_domain", DEFAULT_DOMAIN),
        okta_org=identity.get("okta_org", DEFAULT_ORG),
        floorsense_url=identity.get("floorsense_url"),
        office_days=list(prefs.get("office_days", [])),
        book_ahead_days=int(prefs.get("book_ahead_days",
                                      DEFAULT_BOOK_AHEAD_DAYS)),
        default_group=prefs.get("default_group", DEFAULT_GROUP_NAME),
        groups={k: list(v) for k, v in (raw.get("groups") or {}).items()},
        teams={k: list(v) for k, v in (raw.get("teams") or {}).items()},
        day_opening_time=prefs.get("day_opening_time"),
        exists=True,
    )


def save(config, directory=None, on_repair=None):
    """Write `config.toml` at 0600 in a 0700 directory. Never writes a
    secret -- there is nowhere in this structure for one to go."""
    directory = ensure_dir(directory or config_dir(), on_repair)
    path = directory / "config.toml"

    doc = {
        "identity": {k: v for k, v in (
            ("okta_user", config.okta_user),
            ("email_domain", config.email_domain),
            ("okta_org", config.okta_org),
            ("floorsense_url", config.floorsense_url),
        ) if v is not None},
        "preferences": {
            "office_days": list(config.office_days),
            "book_ahead_days": int(config.book_ahead_days),
            "default_group": config.default_group,
        },
        "groups": {k: list(v) for k, v in (config.groups or {}).items()},
        "teams": {k: list(v) for k, v in (config.teams or {}).items()},
    }
    if config.day_opening_time is not None:
        doc["preferences"]["day_opening_time"] = config.day_opening_time

    # 0600 from the moment it exists, rather than written-then-chmodded.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    with os.fdopen(fd, "wb") as fh:
        fh.write(tomli_w.dumps(doc).encode())
    _repair(path, FILE_MODE, on_repair)
    config.exists = True
    return path
