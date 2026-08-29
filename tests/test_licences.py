"""`fs --licences` must mention every package that actually ships in
dist/fs. Walks the real runtime dependency graph from pyproject.toml's
`dependencies` (not `[dev]`) via installed package metadata, so a new
transitive dependency with no matching notice fails this test rather than
silently shipping unlicensed.
"""
import re
from importlib.metadata import PackageNotFoundError, distribution

import pytest

from fs_cli.cli import licence_notices

try:
    from packaging.requirements import Requirement
except ImportError:
    Requirement = None

#: `pyproject.toml`'s `[project] dependencies` -- the roots of the graph
#: that actually ships, not `[project.optional-dependencies] dev`.
ROOT_DEPENDENCIES = ["requests", "keyring", "tomli-w"]


def _runtime_closure():
    """Every distribution reachable from `ROOT_DEPENDENCIES`, markers
    evaluated against whatever interpreter runs this test -- an unmet
    marker (Windows/Linux-only keyring backends, requests' optional
    extras, a backport only needed below some Python version) is correctly
    excluded rather than demanding a notice for something that won't
    actually be installed here. `scripts/build-release.sh` builds on the
    pinned 3.11 floor for the same reason (its own header) -- this test
    running under a different interpreter can in principle see a slightly
    different closure than that build; the build itself is the definitive
    source of truth if the two ever disagree.
    """
    seen = set()
    stack = list(ROOT_DEPENDENCIES)
    while stack:
        name = stack.pop()
        key = name.lower().replace("_", "-")
        if key in seen:
            continue
        seen.add(key)
        try:
            dist = distribution(name)
        except PackageNotFoundError:
            continue
        for req_str in dist.requires or []:
            req = Requirement(req_str)
            if req.marker and not req.marker.evaluate():
                continue
            stack.append(req.name)
    return seen


def _normalise(text):
    return re.sub(r"[-_.]", "", text.lower())


@pytest.mark.skipif(Requirement is None, reason="packaging not installed")
def test_every_bundled_dependency_has_a_licence_notice():
    notices = _normalise(licence_notices())
    missing = sorted(pkg for pkg in _runtime_closure()
                     if _normalise(pkg) not in notices)
    assert not missing, (
        f"{missing} would ship in dist/fs with no entry in "
        "THIRD-PARTY-NOTICES.txt / src/fs_cli/_third_party_notices.txt -- "
        "add it and re-run scripts/gen-third-party-notices.py before "
        "releasing.")
