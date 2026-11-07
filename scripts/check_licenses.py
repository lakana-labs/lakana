"""Fail the build when a dependency carries a copyleft or source-available licence.

Covers Python (installed distributions) and npm (any ``package-lock.json`` in the
repository) under one policy and one ``ALLOWLIST``.

Lakana ships under Apache-2.0 and its default dependency set is kept fully
permissive, so the licence gate stays meaningful rather than exception-ridden.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN: dict[str, tuple[str, ...]] = {
    # Order matters: the specific families must be tested before the general one.
    # "GNU Affero General Public License" and "GNU Lesser General Public License"
    # both contain "general public license", so AGPL and LGPL come first. The SPDX
    # short identifiers "agpl" and "lgpl" (PEP 639 licence expressions, e.g.
    # "AGPL-3.0-only") likewise both contain "gpl", so they too must be tested
    # before the bare "gpl" needle. Do not add a bare "bsl" needle: BSL-1.0 is the
    # (permissive) Boost Software License, distinct from BUSL, the Business Source
    # License.
    "AGPL": ("affero", "agpl"),
    "LGPL": ("lesser general public", "lgpl"),
    "GPL": ("general public license", "gpl"),
    "BUSL": ("business source", "busl"),
    "SSPL": ("server side public", "sspl"),
    # Weak (file-level) copyleft, not strong copyleft like the GPL family above,
    # but still not "fully permissive" per the no-copyleft rule, so the gate
    # catches these the same way. Each pairs the prose licence name (as reported
    # by package metadata, e.g. certifi's "Mozilla Public License 2.0 (MPL 2.0)")
    # with the SPDX short identifier (e.g. hypothesis's own "MPL-2.0"), the same
    # two-needle-per-family shape as the strong-copyleft entries above. EUPL gets
    # both the American ("license") and the official EU ("licence") spelling,
    # since either could appear in the wild.
    "MPL": ("mozilla public license", "mpl"),
    "EPL": ("eclipse public license", "epl"),
    "CDDL": ("common development and distribution license", "cddl"),
    "EUPL": ("european union public license", "european union public licence", "eupl"),
    # The SIL Open Font License is font-level copyleft (a modified font must stay under
    # the OFL). Accepted only for FONT_PACKAGES on the npm side; everywhere else it fails.
    "OFL": ("open font license", "ofl"),
}

# Bundled, unmodified, never subset: IBM Plex reserves the name "Plex" (OFL-1.1 with a
# Reserved Font Name), so only the complete upstream woff2 files ship (M7 part-1 design).
FONT_PACKAGES: frozenset[str] = frozenset({"@ibm/plex-sans", "@ibm/plex-mono"})

ALLOWLIST: frozenset[str] = frozenset(
    {
        "hypothesis",
        "certifi",
        "pathspec",
        "axe-core",
        "@axe-core/playwright",
        "lightningcss",
        "lightningcss-android-arm64",
        "lightningcss-darwin-arm64",
        "lightningcss-darwin-x64",
        "lightningcss-freebsd-x64",
        "lightningcss-linux-arm-gnueabihf",
        "lightningcss-linux-arm64-gnu",
        "lightningcss-linux-arm64-musl",
        "lightningcss-linux-x64-gnu",
        "lightningcss-linux-x64-musl",
        "lightningcss-win32-arm64-msvc",
        "lightningcss-win32-x64-msvc",
    }
)
"""Distributions explicitly permitted despite their licence. Each entry needs a
comment justifying it, and adding one is a deliberate decision.

* ``hypothesis`` — MPL-2.0, a `dev` dependency-group only (property-based testing,
  see `docs/agent/testing.md`). It is never a runtime dependency of any of the
  four packages and is never shipped.
* ``certifi`` — MPL-2.0, but *not* dev-only: `lakana-worker` depends on it directly
  (the last place its query process looks for a CA bundle, `lakana_worker.ca_bundle`),
  and it is a transitive dependency of `requests`, which `pyiceberg` (a `lakana-core`
  default dependency) requires for its REST catalog client, and of `httpx`, so it is
  present in the default, shipped dependency set (confirmed via ``uv export --no-dev``).
  The worker only calls `certifi.where()`. MPL-2.0 is file-level copyleft —
  its share-alike obligation attaches only to modifications of certifi's own
  files (a CA bundle plus a loader), never to code that merely imports it — so
  bundling it alongside Apache-2.0 code imposes no obligation on Lakana's own
  source. Recorded here rather than silently passed so this reasoning is
  visible the next time someone re-derives what the gate actually allows.
* ``pathspec`` — MPL-2.0, a transitive dependency of ``hatchling``, itself a
  `dev` dependency-group-only addition that lets
  `packages/lakana/tests/test_packaging.py` unit-test
  `packages/lakana/hatch_build.py`'s metadata hook directly, instead of only
  through a full `uv build`. `hatchling` is already an implicit PEP 517 build
  dependency of every package (`[build-system].requires`); declaring it (and
  therefore `pathspec`) explicitly in `dev` changes nothing about what ships —
  neither is a runtime dependency of any of the four packages.
* ``axe-core`` and ``@axe-core/playwright`` — MPL-2.0, npm, test-time only (Playwright's
  accessibility checks), unmodified, never in the built bundle (dependency-cruiser's
  ``not-to-dev-dep`` rule keeps ``ui/src`` from importing them).
* ``lightningcss`` (and its ``lightningcss-<platform>`` packages) — MPL-2.0, npm, Vite 8's
  CSS transformer and minifier: a build tool used unmodified whose output is our own CSS;
  it is never shipped.
"""


def _needle_matches(needle: str, lowered: str) -> bool:
    """Match one needle: phrases by substring, short ids as whole tokens.

    Parameters
    ----------
    needle
        A lower-case needle from `FORBIDDEN`.
    lowered
        The lower-cased licence text.

    Returns
    -------
    bool
        Whether the needle matches. A space-free needle (an SPDX-style id such
        as ``mpl``) must start at a token boundary and end at a non-letter, an
        optional ``v``-version, or the end, so ``Simplified BSD`` is not MPL.
    """
    if " " in needle:
        return needle in lowered
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?:v?\d|[^a-z]|$)", lowered) is not None


def classify(license_text: str) -> str | None:
    """Match a licence string against the forbidden families.

    Licences that grant nothing (``UNLICENSED``) or are opaque
    (``SEE LICENSE IN ...``, ``LicenseRef-*``) also fail, as ``NO-GRANT`` and
    ``OPAQUE``, so they need an allowlist entry like any other exception.

    Parameters
    ----------
    license_text
        Licence name as reported by package metadata.

    Returns
    -------
    str or None
        The forbidden family matched, or ``None`` when acceptable.
    """
    lowered = license_text.lower().strip()
    if lowered == "unlicensed":
        return "NO-GRANT"
    if lowered.startswith(("see license", "see licence", "licenseref-")):
        return "OPAQUE"
    for family, needles in FORBIDDEN.items():
        if any(_needle_matches(needle, lowered) for needle in needles):
            # LGPL contains "general public license" too, so the more specific
            # families are checked first by dict ordering.
            return family
    return None


def find_violations(
    entries: list[dict[str, str]], allowlist: frozenset[str]
) -> list[tuple[str, str, str]]:
    """Find every entry with a forbidden licence, skipping the allowlist.

    Parameters
    ----------
    entries
        Distributions as reported by ``pip-licenses --format=json``: each a
        dict with at least ``"Name"`` and ``"License"`` keys.
    allowlist
        Distribution names exempted regardless of their reported licence.

    Returns
    -------
    list of tuple of str
        ``(name, license_text, family)`` for each violation, in input order.
    """
    return [
        (entry["Name"], entry["License"], family)
        for entry in entries
        if entry["Name"] not in allowlist and (family := classify(entry["License"])) is not None
    ]


SKIP_DIRS = frozenset({".git", ".venv", "node_modules", ".worktrees", "__pycache__"})
UNSUPPORTED_LOCKFILES = ("pnpm-lock.yaml", "yarn.lock")
_TOKEN = re.compile(r"\(|\)|[^\s()]+")


def classify_expression(expression: str) -> str | None:
    """Classify an SPDX licence expression such as ``MIT OR GPL-3.0``.

    ``OR`` passes when any alternative is acceptable; ``AND`` fails when any
    term is forbidden. Terms without an operator between them (prose licence
    names) are joined and classified as one licence.

    Parameters
    ----------
    expression
        The licence string, optionally with ``AND``/``OR`` and parentheses.

    Returns
    -------
    str or None
        The forbidden family that makes the expression unusable, or ``None``.

    Raises
    ------
    ValueError
        If the expression is malformed (empty term, unbalanced parenthesis,
        trailing tokens); malformed input never passes.
    """
    tokens = _TOKEN.findall(expression)
    pos = 0

    def parse_or() -> str | None:
        nonlocal pos
        results = [parse_and()]
        while pos < len(tokens) and tokens[pos] == "OR":
            pos += 1
            results.append(parse_and())
        return None if any(r is None for r in results) else results[0]

    def parse_and() -> str | None:
        nonlocal pos
        results = [parse_atom()]
        while pos < len(tokens) and tokens[pos] == "AND":
            pos += 1
            results.append(parse_atom())
        return next((r for r in results if r is not None), None)

    def parse_atom() -> str | None:
        nonlocal pos
        if pos < len(tokens) and tokens[pos] == "(":
            pos += 1
            result = parse_or()
            if pos >= len(tokens) or tokens[pos] != ")":
                raise ValueError(f"missing ')' in licence expression {expression!r}")
            pos += 1
            return result
        words: list[str] = []
        while pos < len(tokens) and tokens[pos] not in {"(", ")", "AND", "OR"}:
            words.append(tokens[pos])
            pos += 1
        if not words:
            raise ValueError(f"empty term in licence expression {expression!r}")
        return classify(" ".join(words))

    result = parse_or()
    if pos < len(tokens):
        raise ValueError(f"unexpected {tokens[pos]!r} in licence expression {expression!r}")
    return result


def find_lockfiles(root: Path) -> tuple[list[Path], list[Path]]:
    """Locate npm lockfiles under ``root``, skipping vendored directories.

    Parameters
    ----------
    root
        Directory to walk.

    Returns
    -------
    tuple of list of Path
        ``(package-lock.json files, unsupported lockfiles)``; the latter are
        pnpm/yarn lockfiles, which this check cannot read.
    """
    npm: list[Path] = []
    unsupported: list[Path] = []
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = sorted(d for d in subdirs if d not in SKIP_DIRS)
        for name in sorted(files):
            if name == "package-lock.json":
                npm.append(Path(directory) / name)
            elif name in UNSUPPORTED_LOCKFILES:
                unsupported.append(Path(directory) / name)
    return npm, unsupported


def find_npm_violations(
    lock: dict[str, object], allowlist: frozenset[str]
) -> list[tuple[str, str, str]]:
    """Find every npm dependency with a forbidden or missing licence.

    Parameters
    ----------
    lock
        A parsed ``package-lock.json`` (``lockfileVersion`` 2 or 3).
    allowlist
        Package names exempted regardless of licence.

    Returns
    -------
    list of tuple of str
        ``(name, license_text, family)``; a missing licence is reported as
        ``("<name>", "<missing>", "UNKNOWN")``.

    Raises
    ------
    ValueError
        If the lockfile has no ``packages`` map (``lockfileVersion`` 1), since
        it then carries no licence data.
    """
    packages = lock.get("packages")
    if not isinstance(packages, dict):
        raise ValueError(f"lockfileVersion {lock.get('lockfileVersion')} has no licence data")
    violations: list[tuple[str, str, str]] = []
    for key, entry in packages.items():
        if key == "":
            continue
        tail = key.rsplit("node_modules/", 1)[-1]
        if not isinstance(entry, dict):
            violations.append((tail, repr(entry), "UNPARSEABLE"))
            continue
        # Links and workspace members (keys outside node_modules/) are first-party.
        if entry.get("link") is True or "node_modules/" not in key:
            continue
        alias = entry.get("name")
        name = alias if isinstance(alias, str) and alias else tail
        if name in allowlist:
            continue
        licence = entry.get("license")
        if isinstance(licence, dict):
            licence = licence.get("type")
        if isinstance(licence, list) and licence:
            terms = [t.get("type") if isinstance(t, dict) else t for t in licence]
            if not all(isinstance(t, str) and t for t in terms):
                violations.append((name, repr(licence), "UNPARSEABLE"))
                continue
            licence = " OR ".join(terms)
        if (
            licence is None
            or licence in ((), [], "")
            or (isinstance(licence, str) and not licence.strip())
        ):
            violations.append((name, "<missing>", "UNKNOWN"))
        elif not isinstance(licence, str):
            violations.append((name, str(licence), "UNPARSEABLE"))
        else:
            try:
                family = classify_expression(licence)
            except ValueError:
                violations.append((name, licence, "UNPARSEABLE"))
                continue
            if family == "OFL" and name in FONT_PACKAGES:
                continue
            if family is not None:
                violations.append((name, licence, family))
    return violations


def check_npm(root: Path, allowlist: frozenset[str]) -> list[str]:
    """Check every npm lockfile under ``root``.

    Parameters
    ----------
    root
        Repository root.
    allowlist
        Package names exempted regardless of licence.

    Returns
    -------
    list of str
        One message per problem; empty when there is no lockfile or all pass.
    """
    npm, unsupported = find_lockfiles(root)
    problems = [
        f"unsupported lockfile {path.relative_to(root)} (only package-lock.json is checked)"
        for path in unsupported
    ]
    for path in npm:
        rel = path.relative_to(root)
        try:
            violations = find_npm_violations(json.loads(path.read_text()), allowlist)
        except ValueError as error:
            problems.append(f"{rel}: {error}")
            continue
        problems.extend(f"{rel}: {name}: {text} ({family})" for name, text, family in violations)
    return problems


PIP_LICENSES_COMMAND = (
    "uv",
    "run",
    "--all-groups",
    "--all-extras",
    "--all-packages",
    "pip-licenses",
    "--format=json",
    "--with-urls",
)
"""What the gate runs: every dependency group, every extra of every workspace package.

Groups alone miss an extra's dependencies (`lakana[assets]`): their licences would never be
judged."""


def main() -> int:
    """Check every Python distribution of every dependency group and extra, and every npm lockfile.

    Returns
    -------
    int
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    raw = subprocess.run(
        list(PIP_LICENSES_COMMAND), capture_output=True, text=True, check=True, cwd=REPO_ROOT
    ).stdout
    violations = find_violations(json.loads(raw), ALLOWLIST)

    npm_problems = check_npm(REPO_ROOT, ALLOWLIST)

    if violations or npm_problems:
        print("Forbidden dependency licences:", file=sys.stderr)
        for name, license_text, family in violations:
            print(f"  {name}: {license_text} ({family})", file=sys.stderr)
        for problem in npm_problems:
            print(f"  npm {problem}", file=sys.stderr)
        return 1

    print("All dependency licences are permissive.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
