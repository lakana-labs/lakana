"""Tests for the dependency licence check."""

import ast
import itertools
import json
from pathlib import Path

import pytest

from scripts.check_licenses import (
    ALLOWLIST,
    classify,
    classify_expression,
    find_lockfiles,
    find_npm_violations,
    find_violations,
    main,
)


def test_permissive_licenses_are_allowed() -> None:
    """MIT, BSD, and Apache licences pass."""
    assert classify("MIT License") is None
    assert classify("BSD-3-Clause") is None
    assert classify("Apache Software License") is None


def test_copyleft_licenses_are_rejected() -> None:
    """GPL-family licences are matched."""
    assert classify("GNU General Public License v3 (GPLv3)") == "GPL"
    assert classify("GNU Affero General Public License v3") == "AGPL"
    assert classify("GNU Lesser General Public License (LGPL)") == "LGPL"


def test_source_available_licenses_are_rejected() -> None:
    """Business Source and SSPL are not open source and are matched."""
    assert classify("Business Source License 1.1") == "BUSL"
    assert classify("Server Side Public License") == "SSPL"


def test_unknown_license_is_allowed() -> None:
    """An unrecognised licence string does not fail the build on its own."""
    assert classify("Zope Public License") is None


def test_spdx_short_identifiers_are_rejected() -> None:
    """SPDX licence expressions (PEP 639), not just prose names, are matched."""
    assert classify("GPL-3.0-or-later") == "GPL"
    assert classify("LGPL-3.0-or-later") == "LGPL"
    assert classify("AGPL-3.0-only") == "AGPL"
    assert classify("SSPL-1.0") == "SSPL"
    assert classify("BUSL-1.1") == "BUSL"


def test_boost_license_is_not_confused_with_business_source() -> None:
    """BSL-1.0, the permissive Boost Software License, must not match BUSL."""
    assert classify("BSL-1.0") is None
    assert classify("Boost Software License 1.0") is None


def test_weak_copyleft_licenses_are_rejected() -> None:
    """MPL, EPL, CDDL, and EUPL are weak copyleft and are matched.

    Real licence strings from the current dependency set: ``pip-licenses``
    reports certifi as "Mozilla Public License 2.0 (MPL 2.0)" and hypothesis
    as "MPL-2.0" (confirmed with `uv run pip-licenses --format=json`).
    """
    assert classify("Mozilla Public License 2.0 (MPL 2.0)") == "MPL"
    assert classify("MPL-2.0") == "MPL"
    assert classify("Eclipse Public License 2.0") == "EPL"
    assert classify("EPL-2.0") == "EPL"
    assert classify("Common Development and Distribution License 1.0") == "CDDL"
    assert classify("CDDL-1.0") == "CDDL"
    assert classify("European Union Public License 1.2") == "EUPL"
    assert classify("European Union Public Licence 1.2") == "EUPL"
    assert classify("EUPL-1.2") == "EUPL"


def test_allowlisted_name_passes_despite_forbidden_license() -> None:
    """An ALLOWLIST entry is exempted from an otherwise-matching licence.

    certifi's real ``pip-licenses`` entry ("Mozilla Public License 2.0 (MPL
    2.0)") would otherwise violate the MPL family added above; ``find_violations``
    must skip it because it is in the allowlist passed in.
    """
    entries = [
        {"Name": "certifi", "License": "Mozilla Public License 2.0 (MPL 2.0)"},
        {"Name": "hypothesis", "License": "MPL-2.0"},
    ]
    assert find_violations(entries, frozenset({"certifi", "hypothesis"})) == []


def test_non_allowlisted_name_with_forbidden_license_is_reported() -> None:
    """A forbidden licence still fails when the distribution isn't allowlisted."""
    entries = [{"Name": "some-gpl-tool", "License": "GNU General Public License v3"}]
    assert find_violations(entries, frozenset({"hypothesis"})) == [
        ("some-gpl-tool", "GNU General Public License v3", "GPL")
    ]


def _lock(packages: dict[str, dict[str, str]], version: int = 3) -> dict[str, object]:
    """Build a minimal package-lock.json document.

    Parameters
    ----------
    packages
        ``node_modules/<name>`` keys mapped to their lockfile entries.
    version
        The ``lockfileVersion``.

    Returns
    -------
    dict
        The lockfile as parsed JSON, with a root entry added.
    """
    return {
        "lockfileVersion": version,
        "packages": {"": {"name": "app", "version": "1.0.0"}, **packages},
    }


def test_expression_or_passes_when_any_alternative_is_permissive() -> None:
    """``MIT OR GPL-3.0`` is usable under MIT; AND needs every term to pass."""
    assert classify_expression("MIT OR GPL-3.0") is None
    assert classify_expression("(MIT OR GPL-3.0-only) AND Apache-2.0") is None
    assert classify_expression("GPL-3.0-only OR AGPL-3.0-or-later") == "GPL"
    assert classify_expression("MIT AND AGPL-3.0-only") == "AGPL"
    assert classify_expression("AGPL-3.0") == "AGPL"
    assert classify_expression("MIT") is None
    assert classify_expression("GNU General Public License v3") == "GPL"


def test_npm_lock_reports_copyleft_and_unknown_licences() -> None:
    """AGPL and a missing licence fail; MIT and ``MIT OR GPL-3.0`` pass."""
    lock = _lock(
        {
            "node_modules/ok": {"license": "MIT"},
            "node_modules/bad": {"license": "AGPL-3.0"},
            "node_modules/either": {"license": "MIT OR GPL-3.0"},
            "node_modules/nolic": {"version": "1.0.0"},
            "node_modules/a/node_modules/@scope/nested": {"license": "GPL-3.0-only"},
        }
    )
    assert find_npm_violations(lock, frozenset()) == [
        ("bad", "AGPL-3.0", "AGPL"),
        ("nolic", "<missing>", "UNKNOWN"),
        ("@scope/nested", "GPL-3.0-only", "GPL"),
    ]


def test_npm_lock_allowlist_exempts_weak_copyleft_only_when_listed() -> None:
    """An MPL entry fails unless its package name is allowlisted."""
    lock = _lock({"node_modules/weak": {"license": "MPL-2.0"}})
    assert find_npm_violations(lock, frozenset()) == [("weak", "MPL-2.0", "MPL")]
    assert find_npm_violations(lock, frozenset({"weak"})) == []


def test_npm_lock_v1_is_refused() -> None:
    """A v1 lockfile carries no licences, so it cannot be checked."""
    with pytest.raises(ValueError, match="lockfileVersion"):
        find_npm_violations({"lockfileVersion": 1, "dependencies": {}}, frozenset())


def test_find_lockfiles_skips_vendored_dirs_and_flags_unsupported(tmp_path: Path) -> None:
    """package-lock.json is found; node_modules is skipped; pnpm/yarn are unsupported."""
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui" / "package-lock.json").write_text("{}")
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "package-lock.json").write_text("{}")
    (tmp_path / "pnpm-lock.yaml").write_text("")
    (tmp_path / "ui" / "yarn.lock").write_text("")
    (tmp_path / "ui" / "package.json").write_text("{}")
    npm, unsupported = find_lockfiles(tmp_path)
    assert npm == [tmp_path / "ui" / "package-lock.json"]
    assert unsupported == [tmp_path / "pnpm-lock.yaml", tmp_path / "ui" / "yarn.lock"]


def _stub_python_side(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the Python half of ``main`` see an empty, clean environment."""
    monkeypatch.setattr(
        "scripts.check_licenses.subprocess.run",
        lambda *a, **k: type("R", (), {"stdout": "[]"})(),
    )
    monkeypatch.setattr("sys.argv", ["check_licenses"])


def test_main_fails_on_python_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A copyleft Python distribution fails the build and is named with its family."""
    listing = [{"Name": "badpkg", "License": "GNU General Public License v3", "URL": ""}]
    monkeypatch.setattr(
        "scripts.check_licenses.subprocess.run",
        lambda *a, **k: type("R", (), {"stdout": json.dumps(listing)})(),
    )
    monkeypatch.setattr("sys.argv", ["check_licenses"])
    monkeypatch.setattr("scripts.check_licenses.REPO_ROOT", tmp_path)

    assert main() == 1
    err = capsys.readouterr().err
    assert "Forbidden dependency licences:" in err
    assert "badpkg" in err


def test_main_is_a_noop_for_npm_without_lockfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No lockfile: only the Python check runs and passes."""
    _stub_python_side(monkeypatch)
    monkeypatch.setattr("scripts.check_licenses.REPO_ROOT", tmp_path)
    assert main() == 0


def test_main_fails_on_npm_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An AGPL npm dependency fails the build and is named."""
    _stub_python_side(monkeypatch)
    monkeypatch.setattr("scripts.check_licenses.REPO_ROOT", tmp_path)
    (tmp_path / "package-lock.json").write_text(
        json.dumps(_lock({"node_modules/shepherd.js": {"license": "AGPL-3.0"}}))
    )
    assert main() == 1
    assert "shepherd.js" in capsys.readouterr().err


def test_main_passes_clean_npm_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A permissive lockfile passes."""
    _stub_python_side(monkeypatch)
    monkeypatch.setattr("scripts.check_licenses.REPO_ROOT", tmp_path)
    (tmp_path / "package-lock.json").write_text(
        json.dumps(_lock({"node_modules/driver.js": {"license": "MIT"}}))
    )
    assert main() == 0


def test_main_refuses_unsupported_and_old_lockfiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """pnpm/yarn lockfiles and v1 package-locks are errors, never silent passes."""
    _stub_python_side(monkeypatch)
    monkeypatch.setattr("scripts.check_licenses.REPO_ROOT", tmp_path)
    (tmp_path / "yarn.lock").write_text("")
    (tmp_path / "package-lock.json").write_text(json.dumps({"lockfileVersion": 1}))
    assert main() == 1
    err = capsys.readouterr().err
    assert "unsupported lockfile" in err
    assert "lockfileVersion" in err


@pytest.mark.parametrize(
    "bad",
    [
        "MIT OR",
        "OR MIT",
        "MIT AND",
        "(MIT OR GPL-3.0",
        "MIT OR (GPL-3.0",
        "()",
        "",
        "(MIT OR GPL-3.0))",
    ],
)
def test_malformed_expressions_fail_closed(bad: str) -> None:
    """A malformed expression raises instead of passing."""
    with pytest.raises(ValueError):
        classify_expression(bad)


def test_npm_malformed_licence_is_unparseable() -> None:
    """A malformed licence string is a violation of family UNPARSEABLE."""
    lock = _lock({"node_modules/m": {"license": "MIT OR"}})
    assert find_npm_violations(lock, frozenset()) == [("m", "MIT OR", "UNPARSEABLE")]


def test_workspaces_and_links_are_not_missing_licences() -> None:
    """Workspace members and links are first-party; node_modules entries still count."""
    lock = _lock(
        {
            "packages/ui": {"name": "ui", "version": "1.0.0"},
            "node_modules/ui": {"resolved": "packages/ui", "link": True},
            "node_modules/dep": {"version": "1.0.0"},
        }
    )
    assert find_npm_violations(lock, frozenset()) == [("dep", "<missing>", "UNKNOWN")]


def test_npm_legacy_and_odd_license_shapes() -> None:
    """Legacy arrays are OR-joined, objects use type, junk is reported, not a crash."""
    lock = _lock({})
    packages = lock["packages"]
    assert isinstance(packages, dict)
    packages.update(
        {
            "node_modules/arr": {"license": ["MIT", "GPL-3.0"]},
            "node_modules/arrbad": {"license": [{"type": "GPL-3.0"}]},
            "node_modules/arrjunk": {"license": [5, {"other": "x"}]},
            "node_modules/obj": {"license": {"type": "MIT"}},
            "node_modules/num": {"license": 5},
            "node_modules/empty": {"license": []},
            "node_modules/notdict": "oops",
        }
    )
    assert find_npm_violations(lock, frozenset()) == [
        ("arrbad", "GPL-3.0", "GPL"),
        ("arrjunk", "[5, {'other': 'x'}]", "UNPARSEABLE"),
        ("num", "5", "UNPARSEABLE"),
        ("empty", "<missing>", "UNKNOWN"),
        ("notdict", "'oops'", "UNPARSEABLE"),
    ]


def test_simplified_bsd_is_not_mpl() -> None:
    """Short ids match as whole tokens, not substrings."""
    for ok in [
        "Simplified BSD",
        "BSD-2-Clause-Simplified",
        "Apache-2.0",
        "Unlicense",
        "0BSD",
        "CC0-1.0",
        "BlueOak-1.0.0",
        "Zlib",
        "Python-2.0",
        "Simplified BSD License",
    ]:
        assert classify(ok) is None, ok
    assert classify("GPLv3") == "GPL"
    assert classify("LGPLv2.1") == "LGPL"
    assert classify("GPL-2.0-only WITH Classpath-exception-2.0") == "GPL"
    assert classify("MPL 2.0") == "MPL"


def test_opaque_and_no_grant_licences_fail() -> None:
    """UNLICENSED, SEE LICENSE IN and LicenseRef-* fail unless allowlisted."""
    assert classify("UNLICENSED") == "NO-GRANT"
    assert classify("SEE LICENSE IN LICENSE.md") == "OPAQUE"
    assert classify("LicenseRef-Custom") == "OPAQUE"
    assert classify_expression("MIT OR UNLICENSED") is None
    lock = _lock({"node_modules/p": {"license": "UNLICENSED"}})
    assert find_npm_violations(lock, frozenset()) == [("p", "UNLICENSED", "NO-GRANT")]
    assert find_npm_violations(lock, frozenset({"p"})) == []


def test_allowlist_prefers_entry_name_over_key() -> None:
    """An aliased install is allowlisted by its real package name."""
    lock = _lock({"node_modules/alias": {"name": "certifi", "license": "MPL-2.0"}})
    assert find_npm_violations(lock, frozenset({"certifi"})) == []
    assert find_npm_violations(lock, frozenset({"alias"})) == [("certifi", "MPL-2.0", "MPL")]


def test_nested_agpl_package_is_refused() -> None:
    """Shepherd.js-style AGPL deep in node_modules fails."""
    lock = _lock({"node_modules/a/node_modules/shepherd.js": {"license": "AGPL-3.0"}})
    assert find_npm_violations(lock, frozenset()) == [("shepherd.js", "AGPL-3.0", "AGPL")]


def test_main_inspects_every_dependency_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """The environment ``pip-licenses`` reads includes the `docs` and every other group."""
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> object:
        commands.append(command)
        return type("R", (), {"stdout": "[]"})()

    monkeypatch.setattr("scripts.check_licenses.subprocess.run", fake_run)
    monkeypatch.setattr("sys.argv", ["check_licenses"])
    main()
    assert commands
    assert commands[0][:5] == ["uv", "run", "--all-groups", "--all-extras", "--all-packages"]
    assert "pip-licenses" in commands[0]


def _wheel(directory: Path, name: str, licence: str) -> Path:
    """Write a minimal wheel whose metadata names a licence.

    Parameters
    ----------
    directory
        Where to write it.
    name
        The distribution name.
    licence
        Its `License:` metadata.

    Returns
    -------
    Path
        The wheel.
    """
    import base64
    import hashlib
    import zipfile

    files = {
        f"{name}/__init__.py": b"",
        f"{name}-0.1.dist-info/METADATA": (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: 0.1\nLicense: {licence}\n"
        ).encode(),
        f"{name}-0.1.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    record = [
        f"{path},sha256={base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()},{len(data)}"
        for path, data in files.items()
    ]
    record.append(f"{name}-0.1.dist-info/RECORD,,")
    files[f"{name}-0.1.dist-info/RECORD"] = ("\n".join(record) + "\n").encode()
    wheel = directory / f"{name}-0.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, data in files.items():
            archive.writestr(path, data)
    return wheel


@pytest.mark.integration
def test_a_dependency_only_an_extra_pulls_in_is_judged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A copyleft package reachable only through an optional extra fails the gate."""
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    _wheel(wheels, "copyleftpkg", "GNU General Public License v3")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        'name = "probe"\nversion = "0"\nrequires-python = ">=3.11"\n'
        "dependencies = []\n"
        "[project.optional-dependencies]\n"
        'fake = ["copyleftpkg"]\n'
        "[dependency-groups]\n"
        'dev = ["pip-licenses"]\n'
        "[tool.uv]\npackage = false\n"
        "[tool.uv.sources]\n"
        'copyleftpkg = { path = "wheels/copyleftpkg-0.1-py3-none-any.whl" }\n'
    )
    monkeypatch.setattr("scripts.check_licenses.REPO_ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["check_licenses"])
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)

    assert main() == 1
    assert "copyleftpkg" in capsys.readouterr().err


def allowlist_docstring() -> str:
    """Return the docstring literal that follows the ``ALLOWLIST`` assignment.

    Returns
    -------
    str
        The text of the string expression right after ``ALLOWLIST = ...``.

    Raises
    ------
    AssertionError
        If there is no ``ALLOWLIST`` assignment.
    """
    source = Path(__file__).resolve().parents[1] / "check_licenses.py"
    body = ast.parse(source.read_text()).body
    for node, following in itertools.pairwise(body):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "ALLOWLIST"
        ):
            assert isinstance(following, ast.Expr)
            assert isinstance(following.value, ast.Constant)
            return str(following.value.value)
    raise AssertionError("ALLOWLIST not found")


def test_the_plex_fonts_pass_under_ofl() -> None:
    """The two bundled IBM Plex font packages pass under OFL-1.1."""
    lock = _lock({"node_modules/@ibm/plex-sans": {"license": "OFL-1.1"}})
    assert find_npm_violations(lock, ALLOWLIST) == []


@pytest.mark.parametrize(
    "licence",
    ["OFL-1.1", "OFL-1.0", "OFL-1.1-RFN", "OFL-1.1-no-RFN", "SIL Open Font License 1.1"],
)
def test_ofl_outside_a_font_package_fails(licence: str) -> None:
    """OFL is refused for any package that is not a listed font package."""
    lock = _lock({"node_modules/left-pad": {"license": licence}})
    assert find_npm_violations(lock, ALLOWLIST) == [("left-pad", licence, "OFL")]


def test_ofl_as_one_alternative_of_a_permissive_or_passes() -> None:
    """``MIT OR OFL-1.1`` passes: one permissive alternative is enough."""
    lock = _lock({"node_modules/left-pad": {"license": "MIT OR OFL-1.1"}})
    assert find_npm_violations(lock, ALLOWLIST) == []


def test_a_font_package_is_exempt_from_ofl_only() -> None:
    """Any other family on a font package still fails."""
    lock = _lock({"node_modules/@ibm/plex-mono": {"license": "MPL-2.0"}})
    assert find_npm_violations(lock, ALLOWLIST) == [("@ibm/plex-mono", "MPL-2.0", "MPL")]


def test_python_metadata_gets_no_font_exemption() -> None:
    """The Python side classifies OFL and exempts nothing."""
    assert classify("SIL Open Font License 1.1 (OFL-1.1)") == "OFL"
    entries = [{"Name": "@ibm/plex-sans", "License": "OFL-1.1"}]
    assert find_violations(entries, ALLOWLIST) == [("@ibm/plex-sans", "OFL-1.1", "OFL")]


@pytest.mark.parametrize(
    "name", ["axe-core", "@axe-core/playwright", "lightningcss", "lightningcss-linux-x64-gnu"]
)
def test_the_dev_only_mpl_tools_pass_the_npm_check(name: str) -> None:
    """The allowlisted dev-only MPL tools pass."""
    lock = _lock({f"node_modules/{name}": {"license": "MPL-2.0"}})
    assert find_npm_violations(lock, ALLOWLIST) == []


def test_every_allowlist_entry_is_justified_in_the_docstring() -> None:
    """Each entry has its own bullet; one covers lightningcss and its platform packages."""
    doc = allowlist_docstring()
    for name in ALLOWLIST:
        assert f"``{name}``" in doc or name.startswith("lightningcss-")
