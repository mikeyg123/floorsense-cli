"""Desk key identity: normalisation, matching, printing.

`floorsense-api-manual.md` §6: "Key formats vary by site -- don't assume a
parseable structure." That warning shapes this whole module.

So normalisation is used **only for comparison, never for reconstruction**.
`match_desk` compares normalised forms but always returns a key taken verbatim
from the catalog, because only a real key is valid against the API. The one
place we do rewrite a key -- `fmt_desk` -- is display-only, and falls back to
printing the key unchanged the moment it doesn't recognise the shape.
"""

import re

from .errors import NotFound

__all__ = ["normalise", "match_desk", "fmt_desk", "resolve_group_keys"]

# Separators and the site-prefix letters. Stripping these is what makes
# `L5.D.235A`, `5.235A` and `5235a` all compare equal.
_NOISE_RE = re.compile(r"[LD.\-_ ]", re.IGNORECASE)

# `L<floor>.D.<desk>` -- the format this deployment uses. Anything else is
# left alone by fmt_desk.
_LONG_KEY_RE = re.compile(r"^L(\d+)\.D\.(.+)$", re.IGNORECASE)

_ORDINALS = ["1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "8th", "9th",
             "10th"]


def normalise(key):
    """A comparison form only. Never feed the result back to the API."""
    return _NOISE_RE.sub("", key or "").upper()


def match_desk(token, catalog):
    """Resolve a user's token against real desk keys.

    Exact normalised match wins; failing that, a suffix match (so `410A`
    finds `L5.D.410A`). More than one candidate is an error that lists them
    rather than silently picking one -- booking the wrong desk is worse than
    asking again.
    """
    want = normalise(token)
    if not want:
        raise NotFound(f"{token!r} is not a desk")

    exact = [k for k in catalog if normalise(k) == want]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise NotFound(f"{token!r} matches several desks: "
                       f"{', '.join(sorted(exact))}",
                       hint="Use the full desk key.")

    suffix = [k for k in catalog if normalise(k).endswith(want)]
    if len(suffix) == 1:
        return suffix[0]
    if len(suffix) > 1:
        raise NotFound(f"{token!r} matches several desks: "
                       f"{', '.join(sorted(suffix))}",
                       hint="Use the full desk key.")

    raise NotFound(f"no desk matching {token!r}")


def _rank(key, groups):
    """(group name, 1-based position) for the first group containing `key`,
    or None. `groups` is ordered, and so is each group's list -- that order
    IS the booking preference, so position is meaningful, not incidental."""
    for name, keys in (groups or {}).items():
        for i, k in enumerate(keys):
            if normalise(k) == normalise(key):
                return name, i + 1
    return None


def resolve_group_keys(raw_keys, desk_keys, out):
    """A config group's hand-typed entries -> real catalog keys, in order.

    Config groups are hand-editable (PLAN.md), so a stale or mistyped entry
    must not take the whole command down -- it's dropped with a warning
    rather than raised, the same tolerance `config.office_day_indexes`
    already gives a typo'd weekday. Shared by `fs book` and `fs at` rather
    than each keeping its own copy (PLAN.md's "still open" duplication note).
    """
    resolved = []
    for raw in raw_keys:
        try:
            resolved.append(match_desk(raw, desk_keys))
        except NotFound:
            out.warn(f"desk {raw!r} in config not found in the catalog "
                     "-- skipping it")
    return resolved


def fmt_desk(key, groups=None, tags=None):
    """`L5.D.217A` -> `5.217A`, plus group rank when it has one:
    `5.217A (1st preferred)`, plus tags (quiet/window/etc.) when it has
    those: `5.217A (1st preferred) [quiet, window]`. Tags come from
    `Catalog.tag_map()`, sourced from `floorplan-booking`'s `desktags` --
    display only, same as everything else in this function."""
    m = _LONG_KEY_RE.match(key or "")
    short = f"{m.group(1)}.{m.group(2)}" if m else key

    rank = _rank(key, groups)
    if rank is None:
        out = short
    else:
        name, position = rank
        ordinal = (_ORDINALS[position - 1] if position <= len(_ORDINALS)
                   else f"{position}th")
        out = f"{short} ({ordinal} {name})"

    if tags:
        out = f"{out} [{', '.join(tags)}]"
    return out
