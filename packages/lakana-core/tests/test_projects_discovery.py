"""Asset discovery by path convention."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from lakana_core.errors import ValidationError
from lakana_core.identifiers import validate_path_component
from lakana_core.projects.discovery import discover
from lakana_core.projects.issues import ProjectValidationError
from lakana_core.projects.limits import DeployLimits


def _write(root: Path, relative: str, text: str = "SELECT 1 AS a\n") -> Path:
    """Write a file under `root`, creating parents.

    Parameters
    ----------
    root
        The checkout root.
    relative
        POSIX path below `root`.
    text
        File content.

    Returns
    -------
    Path
        The written file.
    """
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _messages(exc: ProjectValidationError) -> list[str]:
    """Render every issue.

    Parameters
    ----------
    exc
        The raised error.

    Returns
    -------
    list of str
        One rendered line per issue.
    """
    return [issue.render() for issue in exc.issues]


def test_path_becomes_table_name_with_nested_namespaces(tmp_path: Path) -> None:
    """`assets/<ns…>/<name>.sql` → `<ns…>.<name>`, sorted by path."""
    _write(tmp_path, "assets/bronze/weather.sql")
    _write(tmp_path, "assets/gold/sales/monthly.sql")
    _write(tmp_path, "lakana.yaml", "repo: analytics\n")
    checkout = discover(tmp_path)
    assert [(a.path, a.table) for a in checkout.assets] == [
        ("assets/bronze/weather.sql", "bronze.weather"),
        ("assets/gold/sales/monthly.sql", "gold.sales.monthly"),
    ]
    assert checkout.config_text == "repo: analytics\n"


def test_missing_lakana_yaml_is_allowed(tmp_path: Path) -> None:
    """`lakana.yaml` is optional."""
    _write(tmp_path, "assets/bronze/x.sql")
    assert discover(tmp_path).config_text is None


def test_no_assets_directory_is_a_clear_error(tmp_path: Path) -> None:
    """A fresh repository fails with a readable message."""
    _write(tmp_path, "README.md", "hi")
    with pytest.raises(ProjectValidationError, match="no assets found under assets/"):
        discover(tmp_path)


def test_empty_assets_directory_is_the_same_error(tmp_path: Path) -> None:
    """An `assets/` holding no SQL is no better than none."""
    _write(tmp_path, "assets/README.md", "hi")
    with pytest.raises(ProjectValidationError, match="no assets found under assets/"):
        discover(tmp_path)


def test_other_files_are_ignored(tmp_path: Path) -> None:
    """Docs and keep-files beside assets are not assets."""
    _write(tmp_path, "assets/bronze/x.sql")
    _write(tmp_path, "assets/bronze/README.md", "notes")
    _write(tmp_path, "assets/bronze/.gitkeep", "")
    assert [a.table for a in discover(tmp_path).assets] == ["bronze.x"]


def test_python_assets_are_discovered_next_to_sql(tmp_path: Path) -> None:
    _write(tmp_path, "assets/bronze/raw.sql")
    _write(tmp_path, "assets/gold/model.py", "x = 1\n")
    _write(tmp_path, "pyproject.toml", "[project]\n")
    (tmp_path / "uv.lock").write_bytes(b"version = 1\r\n")
    checkout = discover(tmp_path)
    assert [(a.path, a.table, a.kind) for a in checkout.assets] == [
        ("assets/bronze/raw.sql", "bronze.raw", "sql"),
        ("assets/gold/model.py", "gold.model", "python"),
    ]
    assert checkout.pyproject_text == "[project]\n"
    assert checkout.lock == b"version = 1\r\n"


def test_dependency_files_are_not_read_for_a_sql_project(tmp_path: Path) -> None:
    _write(tmp_path, "assets/bronze/raw.sql")
    _write(tmp_path, "pyproject.toml", "[project]\n")
    _write(tmp_path, "uv.lock", "version = 1\n")
    checkout = discover(tmp_path)
    assert (checkout.pyproject_text, checkout.lock) == (None, None)


def test_a_uv_toml_is_refused_for_a_python_project_and_ignored_for_sql(tmp_path: Path) -> None:
    """Uv.toml changes how uv installs but is not in the venv cache key."""
    _write(tmp_path, "assets/bronze/raw.sql")
    _write(tmp_path, "uv.toml", "compile-bytecode = true\n")
    discover(tmp_path)
    _write(tmp_path, "assets/gold/model.py", "x = 1\n")
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    [message] = _messages(info.value)
    assert message.startswith("uv.toml: ")
    assert "[tool.uv]" in message and "pyproject.toml" in message


def test_a_symlinked_uv_toml_is_refused_once(tmp_path: Path) -> None:
    _write(tmp_path, "assets/gold/model.py", "x = 1\n")
    (tmp_path / "uv.toml").symlink_to("elsewhere")
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    assert len(_messages(info.value)) == 1


def test_missing_dependency_files_are_left_to_the_lockfile_check(tmp_path: Path) -> None:
    _write(tmp_path, "assets/gold/model.py", "x = 1\n")
    checkout = discover(tmp_path)
    assert (checkout.pyproject_text, checkout.lock) == (None, None)


def test_init_py_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, "assets/gold/__init__.py", "")
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    assert _messages(info.value) == [
        "assets/gold/__init__.py: every .py file under assets/ is an asset; put helper code "
        "outside assets/ (the repository root and src/ are importable)"
    ]


@pytest.mark.parametrize("name", [".sql", ".py"])
def test_a_file_named_only_by_its_extension_is_a_deploy_issue(tmp_path: Path, name: str) -> None:
    """A file with no stem is an invalid table name, never skipped silently."""
    _write(tmp_path, f"assets/gold/{name}", "x = 1\n")
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    assert _messages(info.value) == [f"assets/gold/{name}: {_stem_message()}"]


def _stem_message() -> str:
    """Return the message an empty table name produces.

    Returns
    -------
    str
        What `validate_path_component` says about an empty name.
    """
    with pytest.raises(ValidationError) as info:
        validate_path_component("", what="Table name")
    return str(info.value)


def test_a_sql_and_a_python_file_for_one_table_collide(tmp_path: Path) -> None:
    _write(tmp_path, "assets/gold/model.sql")
    _write(tmp_path, "assets/gold/model.py", "x = 1\n")
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    assert _messages(info.value) == [
        "assets/gold/model.sql: assets/gold/model.py and assets/gold/model.sql both produce "
        "table 'gold.model'"
    ]


def test_python_files_count_toward_the_asset_limit(tmp_path: Path) -> None:
    _write(tmp_path, "assets/gold/a.py", "x = 1\n")
    _write(tmp_path, "assets/gold/b.sql")
    with pytest.raises(ProjectValidationError, match="more than 1 assets"):
        discover(tmp_path, limits=DeployLimits(max_assets=1))


def test_an_oversized_python_file_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, "assets/gold/a.py", "x" * 2048)
    with pytest.raises(ProjectValidationError, match="larger than 1 KiB"):
        discover(tmp_path, limits=DeployLimits(sql_file_bytes=1024))


@pytest.mark.parametrize(
    ("name", "limit", "message"),
    [
        ("pyproject.toml", {"pyproject_bytes": 1024}, "pyproject.toml: larger than 1 KiB"),
        ("uv.lock", {"lock_bytes": 1024}, "uv.lock: larger than 1 KiB"),
    ],
)
def test_oversized_dependency_files_are_refused(
    tmp_path: Path, name: str, limit: dict[str, int], message: str
) -> None:
    _write(tmp_path, "assets/gold/a.py", "x = 1\n")
    _write(tmp_path, name, "#" * 4096)
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path, limits=DeployLimits(**limit))
    assert message in _messages(info.value)


@pytest.mark.parametrize("name", ["pyproject.toml", "uv.lock"])
def test_a_symlinked_dependency_file_is_refused(tmp_path: Path, name: str) -> None:
    _write(tmp_path, "assets/gold/a.py", "x = 1\n")
    _write(tmp_path, "elsewhere", "version = 1\n")
    (tmp_path / name).symlink_to("elsewhere")
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    assert _messages(info.value) == [f"{name}: must not be a symlink"]


def test_pyproject_must_be_utf8(tmp_path: Path) -> None:
    _write(tmp_path, "assets/gold/a.py", "x = 1\n")
    (tmp_path / "pyproject.toml").write_bytes(b"\xff")
    with pytest.raises(ProjectValidationError, match=r"pyproject\.toml is not valid UTF-8"):
        discover(tmp_path)


def test_a_non_utf8_lock_is_left_to_the_lockfile_check(tmp_path: Path) -> None:
    """The lock is kept as bytes: its digest is of the exact file."""
    _write(tmp_path, "assets/gold/a.py", "x = 1\n")
    (tmp_path / "uv.lock").write_bytes(b"\xff")
    assert discover(tmp_path).lock == b"\xff"


def test_a_symlink_anywhere_fails_a_python_project_only(tmp_path: Path) -> None:
    """Task unpacking refuses links, so deploy finds them first."""
    _write(tmp_path, "assets/bronze/raw.sql")
    _write(tmp_path, "docs/readme.md", "hello\n")
    (tmp_path / "docs" / "link.md").symlink_to("readme.md")
    (tmp_path / "linked-dir").symlink_to("docs")
    assert len(discover(tmp_path).assets) == 1
    _write(tmp_path, "assets/gold/a.py", "x = 1\n")
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    assert _messages(info.value) == [
        "docs/link.md: symlinks are not allowed in a repository with Python assets",
        "linked-dir: symlinks are not allowed in a repository with Python assets",
    ]


def test_an_unsafe_link_name_is_escaped_in_the_python_symlink_issue(tmp_path: Path) -> None:
    _write(tmp_path, "assets/gold/a.py", "x = 1\n")
    os.symlink("assets", tmp_path / "a\nb")
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    [message] = _messages(info.value)
    assert "\n" not in message
    assert message.startswith("a\\nb: symlinks are not allowed")


def test_asset_outside_a_namespace_is_refused(tmp_path: Path) -> None:
    """Every table needs a namespace."""
    _write(tmp_path, "assets/orphan.sql")
    with pytest.raises(ProjectValidationError, match="must live in a namespace directory"):
        discover(tmp_path)


@pytest.mark.parametrize(
    "relative",
    ["assets/bronze/a.b.sql", "assets/br.onze/x.sql"],
)
def test_dotted_components_are_refused(tmp_path: Path, relative: str) -> None:
    """A dot inside a component would make the dotted name ambiguous."""
    _write(tmp_path, relative)
    with pytest.raises(ProjectValidationError, match=r"must not contain a '\.'"):
        discover(tmp_path)


def test_symlinked_sql_file_is_refused(tmp_path: Path) -> None:
    """Adversarial: a symlink to /etc/passwd under assets/ is refused by name, never read."""
    (tmp_path / "assets" / "bronze").mkdir(parents=True)
    os.symlink("/etc/passwd", tmp_path / "assets" / "bronze" / "passwd.sql")
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    assert _messages(info.value) == [
        "assets/bronze/passwd.sql: symlinks are not allowed under assets/"
    ]


def test_symlinked_directory_is_refused_and_not_followed(tmp_path: Path) -> None:
    """A directory symlink is refused, and nothing beneath it is discovered."""
    outside = tmp_path / "outside"
    _write(outside, "secret.sql")
    (tmp_path / "repo" / "assets").mkdir(parents=True)
    os.symlink(outside, tmp_path / "repo" / "assets" / "bronze")
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path / "repo")
    assert _messages(info.value) == ["assets/bronze: symlinks are not allowed under assets/"]


def test_symlinked_assets_root_is_refused(tmp_path: Path) -> None:
    """`assets` itself may not be a symlink."""
    _write(tmp_path, "real/bronze/x.sql")
    os.symlink(tmp_path / "real", tmp_path / "assets")
    with pytest.raises(ProjectValidationError, match="assets: symlinks are not allowed"):
        discover(tmp_path)


def test_symlinked_lakana_yaml_is_refused(tmp_path: Path) -> None:
    """`lakana.yaml` may not be a symlink."""
    _write(tmp_path, "assets/bronze/x.sql")
    os.symlink("/etc/hostname", tmp_path / "lakana.yaml")
    with pytest.raises(
        ProjectValidationError, match=r"lakana\.yaml: lakana\.yaml must not be a symlink"
    ):
        discover(tmp_path)


def test_oversized_sql_file_is_refused(tmp_path: Path) -> None:
    """SQL files over the limit are refused without being decoded."""
    _write(tmp_path, "assets/bronze/big.sql", "SELECT 1 AS a -- " + "x" * 2048)
    with pytest.raises(ProjectValidationError, match="larger than 1 KiB"):
        discover(tmp_path, limits=DeployLimits(sql_file_bytes=1024))


def test_too_many_assets_is_refused_once(tmp_path: Path) -> None:
    """More than `max_assets` files is one issue, not one per file."""
    for index in range(4):
        _write(tmp_path, f"assets/bronze/t{index}.sql")
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path, limits=DeployLimits(max_assets=3))
    assert _messages(info.value) == ["assets: more than 3 assets"]


def test_non_utf8_file_is_refused(tmp_path: Path) -> None:
    """Assets must be UTF-8."""
    path = tmp_path / "assets" / "bronze" / "x.sql"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"SELECT '\xff' AS a")
    with pytest.raises(ProjectValidationError, match="is not valid UTF-8"):
        discover(tmp_path)


def test_every_issue_is_collected(tmp_path: Path) -> None:
    """Problems in several files are reported together."""
    _write(tmp_path, "assets/bronze/__init__.py", "")
    _write(tmp_path, "assets/top.sql")
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    assert len(info.value.issues) == 2


def test_fifo_asset_is_refused_without_blocking(tmp_path: Path) -> None:
    """Adversarial: a named pipe called x.sql must not hang the worker."""
    (tmp_path / "assets" / "bronze").mkdir(parents=True)
    os.mkfifo(tmp_path / "assets" / "bronze" / "x.sql")
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    assert _messages(info.value) == ["assets/bronze/x.sql: is not a regular file"]


def test_lakana_yaml_that_is_a_directory_is_refused(tmp_path: Path) -> None:
    """A non-file `lakana.yaml` is an issue, not silently ignored."""
    _write(tmp_path, "assets/bronze/x.sql")
    (tmp_path / "lakana.yaml").mkdir()
    with pytest.raises(ProjectValidationError, match=r"lakana\.yaml: is not a regular file"):
        discover(tmp_path)


def test_oversized_lakana_yaml_is_refused_unread(tmp_path: Path) -> None:
    """`lakana.yaml` is capped at the YAML limit before it is loaded."""
    _write(tmp_path, "assets/bronze/x.sql")
    _write(tmp_path, "lakana.yaml", "# " + "x" * 4096 + "\n")
    with pytest.raises(ProjectValidationError, match=r"lakana\.yaml: larger than 1 KiB"):
        discover(tmp_path, limits=DeployLimits(yaml_bytes=1024))


def test_symlink_swapped_in_after_the_check_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Race: the `is_symlink` pre-check is advisory; the `O_NOFOLLOW` open holds."""
    (tmp_path / "assets" / "bronze").mkdir(parents=True)
    os.symlink("/etc/passwd", tmp_path / "assets" / "bronze" / "x.sql")
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    assert _messages(info.value) == ["assets/bronze/x.sql: symlinks are not allowed under assets/"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_unreadable_file_and_directory_are_issues(tmp_path: Path) -> None:
    """OS errors on untrusted content surface as issues, never raw exceptions."""
    secret = _write(tmp_path, "assets/bronze/x.sql")
    secret.chmod(0)
    _write(tmp_path, "assets/silver/y.sql")
    (tmp_path / "assets" / "silver").chmod(0)
    try:
        with pytest.raises(ProjectValidationError) as info:
            discover(tmp_path)
    finally:
        (tmp_path / "assets" / "silver").chmod(0o755)
        secret.chmod(0o644)
    rendered = _messages(info.value)
    assert any(m.startswith("assets/bronze/x.sql: cannot be read") for m in rendered)
    assert any(m.startswith("assets/silver: cannot be read") for m in rendered)


def test_empty_stem_is_refused(tmp_path: Path) -> None:
    """A file called `.sql` has no table name."""
    _write(tmp_path, "assets/bronze/.sql")
    with pytest.raises(ProjectValidationError, match="must not be empty"):
        discover(tmp_path)


def test_invalid_files_count_toward_the_asset_limit(tmp_path: Path) -> None:
    """Refused files cannot be used to dodge `max_assets`."""
    for index in range(4):
        _write(tmp_path, f"assets/bronze/{index}.x.sql")
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path, limits=DeployLimits(max_assets=3))
    assert "assets: more than 3 assets" in _messages(info.value)


@pytest.mark.skipif(
    sys.platform == "darwin", reason="APFS refuses a file name that is not UTF-8 (EILSEQ)"
)
def test_non_utf8_filename_is_refused_and_escaped(tmp_path: Path) -> None:
    """A filename that is not UTF-8 never becomes a table name or reaches a message raw."""
    directory = tmp_path / "assets" / "bronze"
    directory.mkdir(parents=True)
    (directory / os.fsdecode(b"\xff.sql")).write_text("SELECT 1 AS a")
    (directory / os.fsdecode(b"\xfe.py")).write_text("")
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    rendered = _messages(info.value)
    assert len(rendered) == 2
    for message in rendered:
        message.encode("utf-8")
    assert any("\\udcff.sql" in message for message in rendered)


def test_control_character_in_a_path_cannot_forge_lines(tmp_path: Path) -> None:
    """A newline in a filename is refused and rendered escaped."""
    _write(tmp_path, "assets/bronze/x\ny.sql")
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    [message] = _messages(info.value)
    assert "\n" not in message
    assert "x\\ny.sql" in message


def test_uppercase_extensions_are_not_skipped(tmp_path: Path) -> None:
    """`.PY` and `.SQL` are assets, whatever the case."""
    _write(tmp_path, "assets/bronze/N.SQL")
    _write(tmp_path, "assets/bronze/m.PY", "x = 1\n")
    assert [(a.table, a.kind) for a in discover(tmp_path).assets] == [
        ("bronze.N", "sql"),
        ("bronze.m", "python"),
    ]


def test_unsafe_symlink_name_is_escaped(tmp_path: Path) -> None:
    """Refusal messages for symlinks escape the name too."""
    (tmp_path / "assets" / "bronze").mkdir(parents=True)
    os.symlink("/etc/passwd", tmp_path / "assets" / "bronze" / "a\nb.sql")
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    assert "\n" not in _messages(info.value)[0]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("assets/gold/Model.sql", "assets/gold/model.py"),
        ("assets/gold/model.sql", "assets/gold/MODEL.py"),
        ("assets/gold/\u00e9.sql", "assets/gold/e\u0301.py"),
    ],
)
def test_sql_and_python_files_differing_by_case_or_unicode_form_collide(
    tmp_path: Path, first: str, second: str
) -> None:
    """A different extension must not hide a case or normalisation collision."""
    _write(tmp_path, first)
    _write(tmp_path, second, "x = 1\n")
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    [message] = _messages(info.value)
    assert "both produce table" in message


@pytest.mark.skipif(os.geteuid() == 0, reason="root can list a mode-000 directory")
def test_an_unreadable_directory_fails_a_python_project(tmp_path: Path) -> None:
    _write(tmp_path, "assets/gold/a.py", "x = 1\n")
    secret = tmp_path / "private"
    secret.mkdir()
    secret.chmod(0)
    try:
        with pytest.raises(ProjectValidationError) as info:
            discover(tmp_path)
    finally:
        secret.chmod(0o700)
    assert any(m.startswith("private: cannot be read") for m in _messages(info.value))


def test_the_symlink_rule_applies_when_no_python_asset_survives(tmp_path: Path) -> None:
    _write(tmp_path, "assets/gold/bad.name.py", "x = 1\n")
    os.symlink("assets", tmp_path / "link")
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    messages = _messages(info.value)
    assert len(messages) == 2
    assert "link: symlinks are not allowed in a repository with Python assets" in messages


def test_a_line_separator_in_a_link_name_is_escaped(tmp_path: Path) -> None:
    _write(tmp_path, "assets/gold/a.py", "x = 1\n")
    os.symlink("assets", tmp_path / "a\u2028b")
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    [message] = _messages(info.value)
    assert "\u2028" not in message
    assert message.startswith("a\\u2028b: symlinks are not allowed")


@pytest.mark.parametrize("suffix", [".py", ".sql"])
def test_a_line_separator_in_an_asset_name_is_refused(tmp_path: Path, suffix: str) -> None:
    _write(tmp_path, f"assets/gold/a\u2029b{suffix}", "x = 1\n")
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    [message] = _messages(info.value)
    assert "\u2029" not in message
    assert "non-UTF-8 or control character" in message


def test_a_namespace_message_does_not_say_sql_only(tmp_path: Path) -> None:
    _write(tmp_path, "assets/orphan.py", "x = 1\n")
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    assert "assets/<namespace>/<name>.sql or .py" in _messages(info.value)[0]


def test_a_directory_named_pyproject_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, "assets/gold/a.py", "x = 1\n")
    (tmp_path / "pyproject.toml").mkdir()
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    assert "pyproject.toml: is not a regular file" in _messages(info.value)


def test_a_root_file_swapped_for_a_symlink_after_the_check_is_not_an_assets_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "assets/gold/a.py", "x = 1\n")
    _write(tmp_path, "elsewhere", "version = 1\n")
    os.symlink("elsewhere", tmp_path / "uv.lock")
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    assert "uv.lock: must not be a symlink" in _messages(info.value)


def test_an_artifact_asset_and_a_table_asset_of_one_name_collide(tmp_path: Path) -> None:
    """The one-name rule holds across kinds: the duplicate check does not look at the kind."""
    _write(
        tmp_path,
        "assets/models/churn.py",
        'from lakana import asset\n\n\n@asset(output="artifact")\ndef churn():\n    return None\n',
    )
    _write(tmp_path, "assets/models/churn.sql")
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    [message] = _messages(info.value)
    assert "assets/models/churn.py and assets/models/churn.sql both produce table" in message


@pytest.mark.parametrize("suffix", ["sql", "py"])
def test_a_fifo_named_like_an_asset_is_refused_without_blocking(
    tmp_path: Path, suffix: str
) -> None:
    """A named pipe is not read: opening it for reading would otherwise wait for a writer."""
    (tmp_path / "assets" / "bronze").mkdir(parents=True)
    os.mkfifo(tmp_path / "assets" / "bronze" / f"pipe.{suffix}")
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    assert _messages(info.value) == [f"assets/bronze/pipe.{suffix}: is not a regular file"]


@pytest.mark.parametrize("name", ["gone.sql", "gone.py", "gone"])
def test_a_dangling_link_under_assets_is_refused_by_name(tmp_path: Path, name: str) -> None:
    """A link to nothing is still a link: refused, not skipped as a missing file."""
    (tmp_path / "assets" / "bronze").mkdir(parents=True)
    os.symlink("does-not-exist", tmp_path / "assets" / "bronze" / name)
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    assert _messages(info.value) == [
        f"assets/bronze/{name}: symlinks are not allowed under assets/"
    ]


def test_a_link_inside_a_dot_directory_fails_a_python_project(tmp_path: Path) -> None:
    """The walk for links does not skip hidden directories."""
    _write(tmp_path, "assets/gold/a.py", "x = 1\n")
    (tmp_path / ".hidden").mkdir()
    os.symlink("does-not-exist", tmp_path / ".hidden" / "link")
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    assert _messages(info.value) == [
        ".hidden/link: symlinks are not allowed in a repository with Python assets"
    ]


def test_a_linked_dot_directory_is_refused_and_not_walked_into(tmp_path: Path) -> None:
    """A link to a directory is one issue, whatever the directory holds."""
    _write(tmp_path, "assets/gold/a.py", "x = 1\n")
    _write(tmp_path, "real/inner.txt", "hi\n")
    os.symlink("real", tmp_path / ".linked")
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    assert _messages(info.value) == [
        ".linked: symlinks are not allowed in a repository with Python assets"
    ]


def test_a_byte_order_mark_is_dropped_from_a_python_asset(tmp_path: Path) -> None:
    """An editor's BOM does not reach the parser, where it would be a syntax error."""
    path = tmp_path / "assets" / "gold" / "a.py"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xef\xbb\xbfx = 1\r\n")
    [asset] = discover(tmp_path).assets
    assert asset.text == "x = 1\n"


def test_discover_returns_the_checks(tmp_path: Path) -> None:
    """`Checkout.checks` holds the check files, sorted by path, beside the assets."""
    _write(tmp_path, "assets/silver/t.sql")
    _write(tmp_path, "checks/silver/t/b.sql", "SELECT 1 FROM silver.t\n")
    _write(tmp_path, "checks/silver/t/a.sql", "SELECT 2 FROM silver.t\n")
    checkout = discover(tmp_path)
    assert [(c.path, c.table, c.check) for c in checkout.checks] == [
        ("checks/silver/t/a.sql", "silver.t", "a"),
        ("checks/silver/t/b.sql", "silver.t", "b"),
    ]


def test_a_project_without_checks_has_an_empty_checks_tuple(tmp_path: Path) -> None:
    _write(tmp_path, "assets/silver/t.sql")
    assert discover(tmp_path).checks == ()


def test_check_issues_are_raised_with_asset_issues(tmp_path: Path) -> None:
    """One bad asset path and one bad check path are reported together."""
    _write(tmp_path, "assets/top.sql")
    _write(tmp_path, "checks/top.sql")
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    assert [issue.path for issue in info.value.issues] == ["assets/top.sql", "checks/top.sql"]


def test_a_project_with_only_checks_has_no_assets(tmp_path: Path) -> None:
    _write(tmp_path, "checks/silver/t/a.sql", "SELECT 1 FROM silver.t\n")
    with pytest.raises(ProjectValidationError, match="no assets found under assets/"):
        discover(tmp_path)


def test_a_link_under_checks_is_reported_once_in_a_python_project(tmp_path: Path) -> None:
    """The check walk names it; the Python-project link sweep does not name it again."""
    _write(tmp_path, "assets/silver/t.py", "x = 1\n")
    _write(tmp_path, "pyproject.toml", "[project]\n")
    _write(tmp_path, "uv.lock", "")
    _write(tmp_path, "checks/silver/t/real.sql")
    (tmp_path / "checks/silver/t/link.sql").symlink_to(tmp_path / "checks/silver/t/real.sql")
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    links = [m for m in _messages(info.value) if "link.sql" in m]
    assert links == ["checks/silver/t/link.sql: symlinks are not allowed under checks/"]


def test_a_check_swapped_for_a_symlink_after_the_listing_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The open that holds is `_read`'s: it refuses a link with the checks/ text."""
    from lakana_core.projects import checks as checks_module

    _write(tmp_path, "assets/silver/t.sql")
    real = _write(tmp_path, "checks/silver/t/a.sql")
    target = _write(tmp_path, "elsewhere.sql")
    original = checks_module.Path.is_symlink

    def swap(self: Path) -> bool:
        """Swap the file for a link just after the walk's own link test.

        Parameters
        ----------
        self
            The path being tested.

        Returns
        -------
        bool
            `False`, as the real test said before the swap.
        """
        answer = original(self)
        if self == real and not answer:
            real.unlink()
            real.symlink_to(target)
        return answer

    monkeypatch.setattr(checks_module.Path, "is_symlink", swap)
    with pytest.raises(ProjectValidationError) as info:
        discover(tmp_path)
    assert _messages(info.value) == [
        "checks/silver/t/a.sql: symlinks are not allowed under checks/"
    ]


def test_discover_reads_translations_beside_the_assets(tmp_path: Path) -> None:
    _write(tmp_path, "assets/gold/t.sql")
    _write(tmp_path, "translations/fr.yaml", "gold.t:\n  label: T\n")
    checkout = discover(tmp_path)
    assert [(t.path, t.language) for t in checkout.translations] == [("translations/fr.yaml", "fr")]
    _write(tmp_path, "translations/fr.yaml", "x" * 2048)
    with pytest.raises(ProjectValidationError) as caught:
        discover(tmp_path, limits=DeployLimits(translation_bytes=1024))
    assert _messages(caught.value) == ["translations/fr.yaml: larger than 1 KiB"]


def test_a_bad_translation_file_name_stops_discovery(tmp_path: Path) -> None:
    _write(tmp_path, "assets/gold/t.sql")
    _write(tmp_path, "translations/fr.yml", "{}\n")
    with pytest.raises(ProjectValidationError) as caught:
        discover(tmp_path)
    assert _messages(caught.value) == [
        "translations/fr.yml: a translation file ends in .yaml; rename it to translations/fr.yaml"
    ]


def test_a_link_under_translations_is_refused_once_in_a_python_project(tmp_path: Path) -> None:
    _write(tmp_path, "assets/gold/a.py", "x = 1\n")
    _write(tmp_path, "pyproject.toml", "[project]\nname = 'p'\n")
    _write(tmp_path, "notes.yaml", "{}\n")
    (tmp_path / "translations").mkdir()
    (tmp_path / "translations" / "fr.yaml").symlink_to(tmp_path / "notes.yaml")
    with pytest.raises(ProjectValidationError) as caught:
        discover(tmp_path)
    assert [m for m in _messages(caught.value) if "symlink" in m] == [
        "translations/fr.yaml: symlinks are not allowed under translations/"
    ]
