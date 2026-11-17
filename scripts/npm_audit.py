"""Audit npm locks, failing on every advisory but the time-limited ones the allowlist names.

CI ran ``npm audit --audit-level=low`` on each npm lock, so any advisory failed the job. An
advisory with no fixed release in a package only dev tooling uses would then fail every pull
request until upstream acts. This script keeps the audit strict and lets through exactly the
advisories in ``scripts/npm-audit-allowlist.toml``, each for one package, with a reason, the
issue that tracks it and a date it lapses.

For each directory given, it runs ``npm audit --json`` there and reads the advisories from the
report: an entry of ``via`` that is an object is an advisory on that package (a string names the
package it comes through, which is already reported on its own). An advisory of severity
``info`` passes, as it did under ``--audit-level=low``. The audit fails when

- an advisory is not in the allowlist (same advisory id and package);
- an allowlisted advisory's ``until`` date has come (from that day on, as osv-scanner's
  ``ignoreUntil``);
- an allowlisted advisory's package is reachable from a production dependency: its name appears
  anywhere in ``npm ls --package-lock-only --omit=dev --all --json`` of that directory, in any
  version, so a doubt counts as reachable;
- an allowlist entry is reported in none of the directories (the fix has landed, or it never
  matched).

Only the standard library is used, so the job runs it with the runner's ``python3`` and installs
nothing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

ALLOWLIST = Path(__file__).resolve().parent / "npm-audit-allowlist.toml"
"""The checked-in allowlist."""

AUDIT = ("npm", "audit", "--json")
"""The audit command, run in each directory; it reads the lock and installs nothing."""

PRODUCTION_TREE = ("npm", "ls", "--package-lock-only", "--omit=dev", "--all", "--json")
"""The production dependency tree of a directory, read from its lock."""

_ADVISORY_ID = re.compile(r"GHSA(-[23456789cfghjmpqrvwx]{4}){3}")
_ENTRY_KEYS = frozenset({"id", "package", "reason", "issue", "until"})

Runner = Callable[[Sequence[str], Path], tuple[int, str]]
"""Runs a command in a directory and returns its exit code and standard output."""


class AuditError(Exception):
    """The allowlist or an npm report cannot be read."""


@dataclass(frozen=True)
class Allowed:
    """One allowlisted advisory in one package."""

    id: str
    package: str
    reason: str
    issue: int
    until: dt.date


@dataclass(frozen=True)
class Finding:
    """One advisory npm reports for one package of one directory."""

    id: str
    package: str
    severity: str
    title: str


def load_allowlist(path: Path) -> list[Allowed]:
    """Read and check the allowlist.

    Parameters
    ----------
    path
        The TOML file with one ``[[advisory]]`` table per entry.

    Returns
    -------
    list[Allowed]
        The entries, in file order.

    Raises
    ------
    AuditError
        If the file is not valid TOML, has a key other than ``advisory``, or an entry has a
        missing, unknown or ill-typed field, or the same advisory and package twice.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AuditError(f"{path}: {exc}") from exc
    raw_entries = data.get("advisory", [])
    if set(data) - {"advisory"} or not isinstance(raw_entries, list):
        raise AuditError(f"{path}: only [[advisory]] tables are allowed")
    entries: list[Allowed] = []
    for number, raw in enumerate(raw_entries, start=1):
        if not isinstance(raw, dict) or set(raw) != _ENTRY_KEYS:
            raise AuditError(f"{path}: entry {number} needs exactly {sorted(_ENTRY_KEYS)}")
        entry = Allowed(**raw)
        valid = (
            isinstance(entry.id, str)
            and _ADVISORY_ID.fullmatch(entry.id) is not None
            and isinstance(entry.package, str)
            and entry.package.strip() != ""
            and isinstance(entry.reason, str)
            and entry.reason.strip() != ""
            and type(entry.issue) is int
            and entry.issue > 0
            and type(entry.until) is dt.date
        )
        if not valid:
            raise AuditError(
                f"{path}: entry {number} needs a GHSA id, a package, a reason, a positive "
                "issue number and an `until` date"
            )
        if any((e.id, e.package) == (entry.id, entry.package) for e in entries):
            raise AuditError(f"{path}: {entry.id} in {entry.package} is listed twice")
        entries.append(entry)
    return entries


def _json_object(output: str, what: str) -> dict:
    """Parse a command's JSON output, which must be an object without an ``error`` key.

    Parameters
    ----------
    output
        The command's standard output.
    what
        The command, for the message.

    Returns
    -------
    dict
        The parsed object.

    Raises
    ------
    AuditError
        If the output is not a JSON object, or reports an error.
    """
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise AuditError(f"{what} did not print JSON: {exc}") from exc
    if not isinstance(data, dict) or "error" in data:
        raise AuditError(f"{what} failed: {output.strip()[:2000]}")
    return data


def findings(report: dict) -> list[Finding]:
    """Read the advisories from an ``npm audit --json`` report.

    Parameters
    ----------
    report
        The parsed report (``auditReportVersion`` 2).

    Returns
    -------
    list[Finding]
        One finding per advisory and package, severity ``info`` left out.

    Raises
    ------
    AuditError
        If the report has no ``vulnerabilities`` object.
    """
    vulnerabilities = report.get("vulnerabilities")
    if not isinstance(vulnerabilities, dict):
        raise AuditError("npm audit printed no `vulnerabilities` object")
    found: list[Finding] = []
    for name, entry in vulnerabilities.items():
        for via in entry.get("via", []):
            if not isinstance(via, dict) or via.get("severity") == "info":
                continue
            url = str(via.get("url", ""))
            advisory = url.rstrip("/").rsplit("/", 1)[-1] or f"npm-{via.get('source')}"
            found.append(
                Finding(
                    id=advisory,
                    package=str(via.get("dependency") or via.get("name") or name),
                    severity=str(via.get("severity", "unknown")),
                    title=str(via.get("title", "")),
                )
            )
    return found


def production_packages(tree: dict) -> set[str]:
    """Collect every package name in an ``npm ls --omit=dev --all --json`` tree.

    Parameters
    ----------
    tree
        The parsed tree.

    Returns
    -------
    set[str]
        The names of every package reachable from a production dependency.
    """
    names: set[str] = set()
    pending = [tree]
    while pending:
        node = pending.pop()
        for name, child in node.get("dependencies", {}).items():
            names.add(name)
            pending.append(child)
    return names


def check(
    reports: dict[str, list[Finding]],
    production: Callable[[str], set[str]],
    allowlist: Sequence[Allowed],
    today: dt.date,
) -> tuple[list[str], list[str]]:
    """Judge the findings of every directory against the allowlist.

    Parameters
    ----------
    reports
        The findings of each audited directory, by its name.
    production
        Returns the production package names of a directory; asked only for a directory with
        an allowlisted finding.
    allowlist
        The allowlist entries.
    today
        The date an entry's ``until`` is compared with.

    Returns
    -------
    tuple[list[str], list[str]]
        The problems, each of which fails the audit, and the advisories let through.
    """
    problems: list[str] = []
    allowed: list[str] = []
    used: set[Allowed] = set()
    for directory, found in reports.items():
        for finding in found:
            shown = f"{directory}: {finding.id} ({finding.severity}) in {finding.package}"
            entry = next(
                (e for e in allowlist if (e.id, e.package) == (finding.id, finding.package)),
                None,
            )
            if entry is None:
                problems.append(f"{shown}: not allowlisted. {finding.title}")
                continue
            used.add(entry)
            if today >= entry.until:
                problems.append(
                    f"{shown}: the allowlist entry lapsed on {entry.until} (#{entry.issue})"
                )
            elif finding.package in production(directory):
                problems.append(
                    f"{shown}: allowlisted for dev dependencies only, but {finding.package} is "
                    f"reachable from a production dependency (#{entry.issue})"
                )
            else:
                allowed.append(f"{shown}: allowlisted until {entry.until} (#{entry.issue})")
    problems.extend(
        f"allowlist: {e.id} in {e.package} (#{e.issue}) is reported by no audited lock; "
        "remove the entry"
        for e in allowlist
        if e not in used
    )
    return problems, allowed


def _run(command: Sequence[str], cwd: Path) -> tuple[int, str]:
    """Run a command in a directory (the default `Runner`).

    Parameters
    ----------
    command
        The command and its arguments.
    cwd
        The directory to run it in.

    Returns
    -------
    tuple[int, str]
        Its exit code and standard output; standard error goes to ours.
    """
    done = subprocess.run(list(command), cwd=cwd, stdout=subprocess.PIPE, text=True, check=False)
    return done.returncode, done.stdout


def audit(
    directories: Sequence[Path],
    allowlist: Sequence[Allowed],
    today: dt.date,
    run: Runner = _run,
) -> tuple[list[str], list[str]]:
    """Audit each directory's lock and judge the findings.

    Parameters
    ----------
    directories
        Directories holding a ``package-lock.json``.
    allowlist
        The allowlist entries.
    today
        The date an entry's ``until`` is compared with.
    run
        Runs a command; replaced in tests.

    Returns
    -------
    tuple[list[str], list[str]]
        The problems and the advisories let through, as `check` returns them.

    Raises
    ------
    AuditError
        If npm's output cannot be read, or ``npm ls`` exits with an error.
    """
    reports = {}
    for directory in directories:
        _, output = run(AUDIT, directory)
        reports[str(directory)] = findings(_json_object(output, f"npm audit in {directory}"))
    trees: dict[str, set[str]] = {}

    def production(directory: str) -> set[str]:
        if directory not in trees:
            code, output = run(PRODUCTION_TREE, Path(directory))
            what = f"npm ls --omit=dev in {directory}"
            tree = _json_object(output, what)
            if code != 0:
                raise AuditError(f"{what} exited with {code}")
            trees[directory] = production_packages(tree)
        return trees[directory]

    return check(reports, production, allowlist, today)


def main(argv: Sequence[str] | None = None, run: Runner = _run) -> int:
    """Audit the directories given on the command line.

    Parameters
    ----------
    argv
        The arguments; ``sys.argv[1:]`` when ``None``.
    run
        Runs a command; replaced in tests.

    Returns
    -------
    int
        ``0`` when nothing fails the audit, ``1`` otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directories", nargs="+", type=Path, help="directories with an npm lock")
    parser.add_argument("--allowlist", type=Path, default=ALLOWLIST)
    parser.add_argument(
        "--today", type=dt.date.fromisoformat, default=dt.datetime.now(dt.UTC).date()
    )
    args = parser.parse_args(argv)
    try:
        allowlist = load_allowlist(args.allowlist)
        problems, allowed = audit(args.directories, allowlist, args.today, run)
    except AuditError as exc:
        print(f"npm audit: {exc}", file=sys.stderr)
        return 1
    for line in allowed:
        print(f"let through: {line}")
    for line in problems:
        print(f"FAIL: {line}", file=sys.stderr)
    if problems:
        return 1
    print(f"npm audit: no advisory fails ({len(args.directories)} lock(s) audited)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
