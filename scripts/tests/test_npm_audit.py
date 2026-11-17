"""Tests for the npm audit with its time-limited allowlist (``scripts/npm_audit.py``)."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

import pytest

from scripts.npm_audit import (
    ALLOWLIST,
    AUDIT,
    PRODUCTION_TREE,
    Allowed,
    _run,
    audit,
    load_allowlist,
    main,
)

ROOT = Path(__file__).resolve().parents[2]
TODAY = dt.date(2026, 11, 17)

PICOMATCH = {
    "source": 1247710,
    "name": "picomatch",
    "dependency": "picomatch",
    "title": "picomatch vulnerable to ReDoS through single-branch repeated extglobs",
    "url": "https://github.com/advisories/GHSA-fc5q-39xc-vjpf",
    "severity": "high",
    "range": "<=4.0.7",
}
LODASH = {
    "source": 1,
    "name": "lodash",
    "dependency": "lodash",
    "title": "Command Injection in lodash",
    "url": "https://github.com/advisories/GHSA-35jh-r3h4-6jhm",
    "severity": "high",
    "range": "<4.17.21",
}


def _report(*advisories: dict) -> dict:
    """Build an ``npm audit --json`` report, with a dependent entry naming the package by string.

    Parameters
    ----------
    *advisories
        The ``via`` objects, one per advisory.

    Returns
    -------
    dict
        The report.
    """
    vulnerabilities: dict[str, dict] = {}
    for advisory in advisories:
        name = advisory["dependency"]
        vulnerabilities.setdefault(name, {"name": name, "via": []})["via"].append(advisory)
        vulnerabilities[f"uses-{name}"] = {"name": f"uses-{name}", "via": [name]}
    return {"auditReportVersion": 2, "vulnerabilities": vulnerabilities}


def _tree(*names: str) -> dict:
    """Build an ``npm ls --json`` tree with `names` nested one below the other.

    Parameters
    ----------
    *names
        Package names, the first a direct dependency.

    Returns
    -------
    dict
        The tree.
    """
    tree: dict = {"name": "probe"}
    node = tree
    for name in names:
        child: dict = {"version": "1.0.0"}
        node["dependencies"] = {name: child}
        node = child
    return tree


def _entry(**changes: object) -> dict:
    """Return the picomatch allowlist entry as TOML values, with `changes` applied.

    Parameters
    ----------
    **changes
        Fields to replace.

    Returns
    -------
    dict
        The entry.
    """
    return {
        "id": "GHSA-fc5q-39xc-vjpf",
        "package": "picomatch",
        "reason": "dev tooling only",
        "issue": 709,
        "until": dt.date(2026, 12, 1),
    } | changes


def _toml(entries: Sequence[dict]) -> str:
    """Write allowlist entries as TOML.

    Parameters
    ----------
    entries
        The entries.

    Returns
    -------
    str
        The TOML text.
    """
    lines = []
    for entry in entries:
        lines.append("[[advisory]]")
        lines.extend(
            f"{key} = {json.dumps(value) if not isinstance(value, dt.date) else value}"
            for key, value in entry.items()
        )
    return "\n".join(lines) + "\n"


class FakeNpm:
    """Answers the two npm commands with canned output and records what was run."""

    def __init__(self, reports: dict[str, dict], trees: dict[str, dict] | None = None) -> None:
        self.reports = reports
        self.trees = trees or {}
        self.ls_code = 0
        self.calls: list[tuple[tuple[str, ...], str]] = []

    def __call__(self, command: Sequence[str], cwd: Path) -> tuple[int, str]:
        """Answer one npm command run in `cwd`.

        Parameters
        ----------
        command
            ``AUDIT`` or ``PRODUCTION_TREE``.
        cwd
            The directory it runs in.

        Returns
        -------
        tuple[int, str]
            npm's exit code and JSON output.
        """
        self.calls.append((tuple(command), str(cwd)))
        if tuple(command) == AUDIT:
            report = self.reports[str(cwd)]
            return (1 if report.get("vulnerabilities") else 0), json.dumps(report)
        assert tuple(command) == PRODUCTION_TREE
        return self.ls_code, json.dumps(self.trees.get(str(cwd), _tree()))


def _main(
    tmp_path: Path,
    npm: FakeNpm,
    entries: Sequence[dict] = (),
    directories: Sequence[str] = ("ui",),
    today: dt.date = TODAY,
) -> int:
    """Run `main` with an allowlist of `entries` and the fake npm.

    Parameters
    ----------
    tmp_path
        Where the allowlist is written.
    npm
        The fake npm.
    entries
        The allowlist entries.
    directories
        The directories to audit.
    today
        The date passed as ``--today``.

    Returns
    -------
    int
        The exit code.
    """
    allowlist = tmp_path / "allowlist.toml"
    allowlist.write_text(_toml(entries), encoding="utf-8")
    argv = [*directories, "--allowlist", str(allowlist), "--today", today.isoformat()]
    return main(argv, run=npm)


def test_an_allowlisted_dev_only_advisory_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The listed advisory in a package outside the production tree is let through, and said so."""
    npm = FakeNpm(
        {"ui": _report(PICOMATCH), "tools/jscpd": _report()},
        {"ui": _tree("react", "scheduler")},
    )
    assert _main(tmp_path, npm, [_entry()], ["ui", "tools/jscpd"]) == 0
    out = capsys.readouterr().out
    assert "let through: ui: GHSA-fc5q-39xc-vjpf (high) in picomatch" in out
    assert "(#709)" in out
    assert "2 lock(s) audited" in out
    # The production tree is read once, only for the directory with an allowlisted finding.
    assert npm.calls.count((PRODUCTION_TREE, "ui")) == 1
    assert (PRODUCTION_TREE, "tools/jscpd") not in npm.calls


def test_the_production_tree_is_read_once_per_directory(tmp_path: Path) -> None:
    """Two allowlisted advisories in one directory ask ``npm ls`` once."""
    fdir = PICOMATCH | {
        "dependency": "fdir",
        "name": "fdir",
        "url": "https://github.com/advisories/GHSA-2222-3333-4444",
    }
    entries = [_entry(), _entry(id="GHSA-2222-3333-4444", package="fdir")]
    npm = FakeNpm({"ui": _report(PICOMATCH, fdir)})
    assert _main(tmp_path, npm, entries) == 0
    assert npm.calls.count((PRODUCTION_TREE, "ui")) == 1


def test_an_advisory_not_in_the_allowlist_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Another advisory fails beside an allowlisted one: the allowlist swallows nothing else."""
    npm = FakeNpm({"ui": _report(PICOMATCH), "probe": _report(LODASH)})
    assert _main(tmp_path, npm, [_entry()], ["ui", "probe"]) == 1
    err = capsys.readouterr().err
    assert "FAIL: probe: GHSA-35jh-r3h4-6jhm (high) in lodash: not allowlisted." in err
    assert "picomatch" not in err


def test_the_same_advisory_in_another_package_is_not_allowlisted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An entry covers its advisory in its own package only."""
    other = PICOMATCH | {"dependency": "micromatch", "name": "micromatch"}
    assert _main(tmp_path, FakeNpm({"ui": _report(PICOMATCH, other)}), [_entry()]) == 1
    assert "in micromatch: not allowlisted" in capsys.readouterr().err


@pytest.mark.parametrize("today", [dt.date(2026, 12, 1), dt.date(2027, 1, 1)])
def test_a_lapsed_entry_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], today: dt.date
) -> None:
    """From its ``until`` date on, as osv-scanner's ``ignoreUntil``, an entry no longer passes."""
    assert _main(tmp_path, FakeNpm({"ui": _report(PICOMATCH)}), [_entry()], today=today) == 1
    assert "the allowlist entry lapsed on 2026-12-01 (#709)" in capsys.readouterr().err


def test_the_day_before_until_still_passes(tmp_path: Path) -> None:
    """The last day an entry holds is the day before its ``until``."""
    npm = FakeNpm({"ui": _report(PICOMATCH)})
    assert _main(tmp_path, npm, [_entry()], today=dt.date(2026, 11, 30)) == 0


@pytest.mark.parametrize(
    "tree",
    [_tree("picomatch"), _tree("react", "some-lib", "picomatch")],
    ids=["direct", "transitive"],
)
def test_an_entry_reaching_a_production_dependency_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tree: dict
) -> None:
    """An entry is for dev dependencies only: its package anywhere in the production tree fails."""
    npm = FakeNpm({"ui": _report(PICOMATCH)}, {"ui": tree})
    assert _main(tmp_path, npm, [_entry()]) == 1
    assert "picomatch is reachable from a production dependency (#709)" in capsys.readouterr().err


def test_an_unused_entry_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An entry no audited lock reports any more must be removed."""
    npm = FakeNpm({"ui": _report(), "tools/jscpd": _report()})
    assert _main(tmp_path, npm, [_entry()], ["ui", "tools/jscpd"]) == 1
    err = capsys.readouterr().err
    assert "GHSA-fc5q-39xc-vjpf in picomatch (#709) is reported by no audited lock" in err


def test_info_advisories_pass_and_a_url_less_advisory_gets_its_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Severity ``info`` passes, as under ``--audit-level=low``.

    An advisory without a URL is named by its npm source number.
    """
    info = LODASH | {"severity": "info"}
    assert _main(tmp_path, FakeNpm({"ui": _report(info)})) == 0
    bare = {"source": 42, "severity": "low"}
    report = {"vulnerabilities": {"left-pad": {"name": "left-pad", "via": [bare]}}}
    assert _main(tmp_path, FakeNpm({"ui": report})) == 1
    assert "ui: npm-42 (low) in left-pad: not allowlisted" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("output", "message"),
    [
        ("not json", "npm audit in ui did not print JSON"),
        ("[]", "npm audit in ui failed"),
        ('{"error": {"code": "ENOLOCK"}}', "npm audit in ui failed"),
        ('{"auditReportVersion": 2}', "no `vulnerabilities` object"),
    ],
)
def test_an_unreadable_audit_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], output: str, message: str
) -> None:
    """Every doubt about npm's report fails the audit."""
    allowlist = tmp_path / "allowlist.toml"
    allowlist.write_text("", encoding="utf-8")
    code = main(["ui", "--allowlist", str(allowlist)], run=lambda _command, _cwd: (1, output))
    assert code == 1
    assert message in capsys.readouterr().err


def test_a_failing_production_tree_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``npm ls`` exiting with an error fails rather than counting as an empty tree."""
    npm = FakeNpm({"ui": _report(PICOMATCH)})
    npm.ls_code = 1
    assert _main(tmp_path, npm, [_entry()]) == 1
    assert "npm ls --omit=dev in ui exited with 1" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("not = [toml", "allowlist.toml"),
        ("[other]\n", "only [[advisory]] tables"),
        ("advisory = 1\n", "only [[advisory]] tables"),
        ("advisory = [1]\n", "needs exactly"),
        (_toml([{"id": "GHSA-fc5q-39xc-vjpf"}]), "needs exactly"),
        (_toml([_entry(extra="x")]), "needs exactly"),
        (_toml([_entry(id="CVE-2026-1")]), "needs a GHSA id"),
        (_toml([_entry(package=" ")]), "needs a GHSA id"),
        (_toml([_entry(reason="")]), "needs a GHSA id"),
        (_toml([_entry(issue=0)]), "needs a GHSA id"),
        (_toml([_entry(issue=True)]), "needs a GHSA id"),
        (_toml([_entry(until="2026-12-01")]), "needs a GHSA id"),
        (_toml([_entry(), _entry()]), "is listed twice"),
    ],
)
def test_a_malformed_allowlist_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str, message: str
) -> None:
    """Each entry needs exactly a GHSA id, a package, a reason, an issue and an ``until`` date."""
    allowlist = tmp_path / "allowlist.toml"
    allowlist.write_text(text, encoding="utf-8")
    npm = FakeNpm({"ui": _report()})
    assert main(["ui", "--allowlist", str(allowlist)], run=npm) == 1
    assert message in capsys.readouterr().err
    assert npm.calls == []


def test_a_missing_allowlist_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An allowlist that cannot be read fails the audit."""
    assert main(["ui", "--allowlist", str(tmp_path / "absent.toml")], run=FakeNpm({})) == 1
    assert "absent.toml" in capsys.readouterr().err


def test_audit_defaults_to_running_npm(tmp_path: Path) -> None:
    """The default runner runs the command in the directory and returns its output."""
    code, output = _run([sys.executable, "-c", "import os; print(os.getcwd())"], tmp_path)
    assert (code, output.strip()) == (0, str(tmp_path))
    assert audit([], [], TODAY) == ([], [])


def test_the_checked_in_allowlist_is_valid_and_matches_osv_scanner() -> None:
    """The allowlist is well formed and ``ui/osv-scanner.toml`` ignores exactly its advisories.

    Same ids, same dates and the tracking issue in each reason, so the two scanners agree.
    """
    entries = load_allowlist(ALLOWLIST)
    osv = tomllib.loads((ROOT / "ui" / "osv-scanner.toml").read_text(encoding="utf-8"))
    ignored = {v["id"]: v for v in osv.get("IgnoredVulns", [])}
    assert {e.id for e in entries} == set(ignored)
    for entry in entries:
        assert ignored[entry.id]["ignoreUntil"] == entry.until
        assert ignored[entry.id]["reason"].strip()
        assert f"#{entry.issue}" in ignored[entry.id]["reason"]


def test_allowed_entries_are_values() -> None:
    """Two entries with the same fields are the same entry (the unused check relies on it)."""
    assert Allowed(**_entry()) == Allowed(**_entry())


def test_the_script_runs_as_a_program(tmp_path: Path) -> None:
    """The job runs it with ``python3`` from the repository root, outside any package."""
    allowlist = tmp_path / "allowlist.toml"
    allowlist.write_text("[[advisory]]\nid = 1\n", encoding="utf-8")
    done = subprocess.run(
        [sys.executable, "scripts/npm_audit.py", "ui", "--allowlist", str(allowlist)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 1
    assert "needs exactly" in done.stderr
