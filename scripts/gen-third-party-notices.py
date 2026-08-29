#!/usr/bin/env python3
"""Generate THIRD-PARTY-NOTICES.txt and LICENSES/*.txt from the packages
actually bundled into dist/fs (build/.venv311/lib/python3.11/site-packages).
Re-run this whenever the dependency set changes."""
import pathlib
import re

VENV = pathlib.Path("build/.venv311/lib/python3.11/site-packages")
OUT_NOTICES = pathlib.Path("THIRD-PARTY-NOTICES.txt")
LICENSES_DIR = pathlib.Path("LICENSES")
#: Canonical (SPDX) licence-type boilerplate for LICENSES/, one file per
#: licence name used by RUNTIME_PACKAGES below. Deliberately NOT derived
#: from whatever LICENSE file a given package happens to ship -- a
#: package's own LICENSE file is reproduced verbatim in
#: THIRD-PARTY-NOTICES.txt already (that's correct, it's that package's
#: actual notice), but it isn't necessarily the licence's generic template:
#: certifi's LICENSE, for instance, is a ca-bundle.crt notice, not MPL-2.0
#: boilerplate. LICENSES/ is meant to answer "what does an Apache-2.0/MIT/
#: etc licence generally say", so it's sourced from a fixed reference copy
#: instead of "whichever package's LICENSE file was found first".
LICENSE_TEMPLATES_DIR = pathlib.Path("scripts/license-templates")
#: Packaged copy `fs --licences` reads at runtime (importlib.resources) --
#: kept in sync with the repo-root copy by this same script.
PACKAGED_COPY = pathlib.Path("src/fs_cli/_third_party_notices.txt")
NOTICE_FILE = pathlib.Path("NOTICE")

# Only the runtime packages shiv actually bundles (see build-release.sh's
# output) -- not pytest/ruff/shiv/etc, which never ship in dist/fs.
RUNTIME_PACKAGES = [
    "requests", "urllib3", "idna", "certifi", "charset_normalizer",
    "keyring", "jaraco.classes", "jaraco.context", "jaraco.functools",
    "importlib_metadata", "zipp", "more_itertools", "backports.tarfile",
    "tomli_w",
]

LICENSE_NAMES = {
    "requests": "Apache-2.0", "urllib3": "MIT", "idna": "BSD-3-Clause",
    "certifi": "MPL-2.0", "charset_normalizer": "MIT", "keyring": "MIT",
    "jaraco.classes": "MIT", "jaraco.context": "MIT",
    "jaraco.functools": "MIT", "importlib_metadata": "Apache-2.0",
    "zipp": "MIT", "more_itertools": "MIT", "backports.tarfile": "MIT",
    "tomli_w": "MIT",
}


def find_dist_info(pkg):
    candidates = list(VENV.glob(f"{pkg}-*.dist-info")) + \
        list(VENV.glob(f"{pkg.replace('.', '_')}-*.dist-info")) + \
        list(VENV.glob(f"{pkg.replace('_', '.')}-*.dist-info")) + \
        list(VENV.glob(f"{pkg.replace('_', '-')}-*.dist-info"))
    if not candidates:
        raise SystemExit(f"no dist-info found for {pkg}")
    return candidates[0]


def read_metadata(dist_info):
    text = (dist_info / "METADATA").read_text(errors="replace")
    version = re.search(r"^Version: (.+)$", text, re.M)
    home = re.search(r"^(?:Home-page|Project-URL: Source,?) ?: ?(.+)$",
                     text, re.M)
    return (version.group(1).strip() if version else "?",
            home.group(1).strip() if home else "")


def find_license_text(dist_info):
    for name in ("LICENSE", "LICENSE.txt", "LICENSE.rst", "LICENSE.md"):
        for base in (dist_info, dist_info / "licenses"):
            f = base / name
            if f.is_file():
                return f.read_text(errors="replace")
    return None


def find_notice_text(dist_info):
    for name in ("NOTICE", "NOTICE.txt"):
        for base in (dist_info, dist_info / "licenses"):
            f = base / name
            if f.is_file():
                return f.read_text(errors="replace")
    return None


def main():
    entries = []
    license_names_seen = set()
    for pkg in RUNTIME_PACKAGES:
        di = find_dist_info(pkg)
        version, home = read_metadata(di)
        license_name = LICENSE_NAMES[pkg]
        license_names_seen.add(license_name)
        text = find_license_text(di)
        notice = find_notice_text(di)
        entries.append((pkg, version, home, license_name, text, notice))

    lines = [
        "Third-party notices",
        "====================",
        "",
        "fs bundles the following third-party packages, each unmodified",
        "under its own licence. Every package's own copyright notice and",
        "full licence text is reproduced below, verbatim from its",
        "distribution. Generic licence-type templates also live in",
        "LICENSES/.",
        "",
        "=" * 72,
        "",
    ]
    for pkg, version, home, license_name, text, notice in entries:
        lines.append(f"{pkg} {version} -- {license_name}")
        if home:
            lines.append(home)
        lines.append("")
        if notice:
            lines.append(notice.strip())
            lines.append("")
        if text:
            lines.append(text.rstrip())
        lines.append("")
        lines.append("=" * 72)
        lines.append("")

    notices_text = "\n".join(lines)
    OUT_NOTICES.write_text(notices_text)
    print(f"wrote {OUT_NOTICES} ({OUT_NOTICES.stat().st_size} bytes)")

    LICENSES_DIR.mkdir(exist_ok=True)
    for license_name in sorted(license_names_seen):
        template = LICENSE_TEMPLATES_DIR / f"{license_name}.txt"
        if not template.is_file():
            raise SystemExit(
                f"no canonical template for {license_name} -- add "
                f"{template} (see LICENSE_TEMPLATES_DIR's comment)")
        (LICENSES_DIR / f"{license_name}.txt").write_text(
            template.read_text())
        print(f"wrote LICENSES/{license_name}.txt")

    # What `fs --licences` prints at runtime: NOTICE's pointer text plus
    # the full third-party notices, one file so there's one resource to
    # embed and read.
    combined = NOTICE_FILE.read_text() + "\n" + "=" * 72 + "\n\n" + notices_text
    PACKAGED_COPY.write_text(combined)
    print(f"wrote {PACKAGED_COPY} ({PACKAGED_COPY.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
