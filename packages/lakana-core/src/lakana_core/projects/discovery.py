"""Asset discovery by path convention: `assets/<ns…>/<name>.sql|.py` → `<ns…>.<name>`.

Runs over an extracted checkout the worker owns. Symlinks are refused by
name and never followed or read, so a repository cannot make the worker read
a file outside the checkout.
"""

from __future__ import annotations

import errno
import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from lakana_core.errors import ValidationError
from lakana_core.identifiers import validate_path_component
from lakana_core.projects.issues import ProjectIssue, ProjectValidationError
from lakana_core.projects.limits import DeployLimits
from lakana_core.projects.lockfile import (
    LOCK_FILE,
    PYPROJECT_FILE,
    UV_TOML_FILE,
    UV_TOML_MESSAGE,
)
from lakana_core.projects.project_config import normalize_source, unsafe_identifier

if TYPE_CHECKING:
    from lakana_core.projects.checks import DiscoveredCheck
    from lakana_core.projects.translations import DiscoveredTranslation

ASSETS_DIR = "assets"
CHECKS_DIR = "checks"
CONFIG_FILE = "lakana.yaml"
TRANSLATIONS_DIR = "translations"
"""The folder at the repository root that holds one translation file per language."""
_SUFFIXES = {".sql": "sql", ".py": "python"}
_INIT_MESSAGE = (
    "every .py file under assets/ is an asset; put helper code outside assets/ "
    "(the repository root and src/ are importable)"
)
_LINK_MESSAGE = "symlinks are not allowed in a repository with Python assets"


@dataclass(frozen=True)
class DiscoveredAsset:
    """One SQL or Python asset file.

    Parameters
    ----------
    path
        Repository-relative POSIX path.
    table
        The dotted table name the path decides.
    text
        The decoded content, BOM removed and line endings normalized.
    kind
        ``sql`` or ``python``.
    """

    path: str
    table: str
    text: str
    kind: str = "sql"


@dataclass(frozen=True)
class Checkout:
    """What discovery found in a checkout.

    Parameters
    ----------
    config_text
        `lakana.yaml`'s content, or `None` if the file is absent.
    assets
        The SQL and Python assets, sorted by path.
    pyproject_text
        `pyproject.toml`'s content; read only when a Python asset exists,
        `None` if absent.
    lock
        `uv.lock`'s raw bytes; read only when a Python asset exists, `None`
        if absent.
    checks
        The checks under `checks/`, sorted by path.
    translations
        The translation files under `translations/`, sorted by path.
    """

    config_text: str | None
    assets: tuple[DiscoveredAsset, ...]
    pyproject_text: str | None = None
    lock: bytes | None = None
    checks: tuple[DiscoveredCheck, ...] = ()
    translations: tuple[DiscoveredTranslation, ...] = ()


def _read(path: Path, relative: str, max_bytes: int, issues: list[ProjectIssue]) -> bytes | None:
    """Read a regular file without following a symlink or blocking on a FIFO.

    The earlier `is_symlink` checks are advisory; this open is the one that
    holds if the entry is swapped in between, and it never reads a device or
    pipe nor more than `max_bytes`.

    Parameters
    ----------
    path
        The file to read.
    relative
        Its repository-relative path, for issues.
    max_bytes
        Largest size accepted.
    issues
        Collects the issue.

    Returns
    -------
    bytes or None
        The content, or `None` after recording an issue.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno != errno.ELOOP:
            message = f"cannot be read: {exc.strerror}"
        elif relative.startswith(ASSETS_DIR + "/"):
            message = "symlinks are not allowed under assets/"
        elif relative.startswith(CHECKS_DIR + "/"):
            message = "symlinks are not allowed under checks/"
        elif relative.startswith(TRANSLATIONS_DIR + "/"):
            message = "symlinks are not allowed under translations/"
        else:
            message = "must not be a symlink"
        issues.append(ProjectIssue(relative, None, message))
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            issues.append(ProjectIssue(relative, None, "is not a regular file"))
            return None
        if info.st_size > max_bytes:
            issues.append(ProjectIssue(relative, None, f"larger than {max_bytes // 1024} KiB"))
            return None
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read(max_bytes + 1)[:max_bytes]
    finally:
        os.close(descriptor)


def _is_unsafe(relative: str) -> bool:
    """Say whether a path cannot be shown or stored faithfully.

    Parameters
    ----------
    relative
        A repository-relative path.

    Returns
    -------
    bool
        True if it is not encodable as UTF-8 (a filename that was not valid
        UTF-8 decodes to lone surrogates) or has a control, format, line or
        paragraph separator character, which could forge lines in rendered issues.
    """
    try:
        relative.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return unsafe_identifier(relative)


def _display(relative: str) -> str:
    """Escape a path for use in an issue when it is unsafe.

    Parameters
    ----------
    relative
        A repository-relative path.

    Returns
    -------
    str
        `relative`, or its ASCII-escaped form without quotes if unsafe.
    """
    return ascii(relative)[1:-1] if _is_unsafe(relative) else relative


def _decode(data: bytes, relative: str, issues: list[ProjectIssue]) -> str | None:
    """Decode a file as UTF-8, recording an issue if it is not.

    Parameters
    ----------
    data
        The raw bytes.
    relative
        The file's repository-relative path.
    issues
        Collects the issue.

    Returns
    -------
    str or None
        The normalized text, or `None` after recording an issue.
    """
    try:
        return normalize_source(data.decode("utf-8"))
    except UnicodeDecodeError:
        issues.append(ProjectIssue(relative, None, f"{relative} is not valid UTF-8"))
        return None


def _fold(table: str) -> str:
    """Fold a table name the way a case- and normalisation-insensitive filesystem does.

    Mirrors `lakana_core.catalog.external_location._normalize_component`
    (NFD, casefold, NFD) rather than importing that private helper.

    Parameters
    ----------
    table
        A dotted table name.

    Returns
    -------
    str
        The folded name; two names a filesystem may treat as one fold equal.
    """
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", table).casefold())


def _table_name(parts: list[str], relative: str, issues: list[ProjectIssue]) -> str | None:
    """Validate a path's components and join them into a table name.

    Parameters
    ----------
    parts
        Components below `assets/`, the last one with its `.sql` or `.py` suffix removed.
    relative
        The file's repository-relative path.
    issues
        Collects issues.

    Returns
    -------
    str or None
        The dotted name, or `None` after recording an issue.
    """
    if len(parts) < 2:
        issues.append(
            ProjectIssue(
                relative,
                None,
                "an asset must live in a namespace directory: assets/<namespace>/<name>.sql or .py",
            )
        )
        return None
    try:
        for component in parts[:-1]:
            validate_path_component(component, what="Namespace component")
        validate_path_component(parts[-1], what="Table name")
    except ValidationError as exc:
        issues.append(ProjectIssue(relative, None, str(exc)))
        return None
    return ".".join(parts)


def _read_root_file(
    root: Path, name: str, max_bytes: int, issues: list[ProjectIssue]
) -> bytes | None:
    """Read a file at the repository root, refusing a symlink.

    Parameters
    ----------
    root
        The extracted checkout.
    name
        The file's name.
    max_bytes
        Largest size accepted.
    issues
        Collects issues.

    Returns
    -------
    bytes or None
        The content; `None` if the file is absent (no issue) or was refused.
    """
    path = root / name
    if path.is_symlink():
        issues.append(ProjectIssue(name, None, "must not be a symlink"))
        return None
    if not os.path.lexists(path):
        return None
    return _read(path, name, max_bytes, issues)


def _links_outside_assets(root: Path) -> list[ProjectIssue]:
    """List every symlink of the checkout that is not under `assets/`.

    A Python task unpacks the whole repository and refuses links; a project
    with a Python asset is therefore refused at deploy for any of them.
    Links under `assets/` are reported by the asset walk, links under `checks/` by the check
    walk, links under `translations/` by the translation listing, and a linked
    `pyproject.toml`, `uv.lock` or `uv.toml` by its own check. A directory that cannot be
    listed is an issue too, so no link hides behind it.

    Parameters
    ----------
    root
        The extracted checkout.

    Returns
    -------
    list of ProjectIssue
        The unreadable directories, then one issue per link sorted by path.
    """
    found: list[str] = []
    unreadable: list[ProjectIssue] = []

    def _walk_error(exc: OSError) -> None:
        """Record a directory that could not be listed.

        Parameters
        ----------
        exc
            The error `os.walk` hit.
        """
        name = _display(Path(str(exc.filename)).relative_to(root).as_posix())
        unreadable.append(ProjectIssue(name, None, f"cannot be read: {exc.strerror}"))

    for directory, dirnames, filenames in os.walk(root, onerror=_walk_error, followlinks=False):
        here = Path(directory)
        for name in (*dirnames, *filenames):
            path = here / name
            relative = path.relative_to(root).as_posix()
            under_assets = relative == ASSETS_DIR or relative.startswith(ASSETS_DIR + "/")
            under_checks = relative == CHECKS_DIR or relative.startswith(CHECKS_DIR + "/")
            under_translations = relative == TRANSLATIONS_DIR or relative.startswith(
                TRANSLATIONS_DIR + "/"
            )
            reported = relative in (PYPROJECT_FILE, LOCK_FILE, UV_TOML_FILE)
            walked = under_assets or under_checks or under_translations
            if path.is_symlink() and not walked and not reported:
                found.append(relative)
    links = [ProjectIssue(_display(relative), None, _LINK_MESSAGE) for relative in sorted(found)]
    return [*unreadable, *links]


def discover(root: Path, *, limits: DeployLimits | None = None) -> Checkout:
    """Find `lakana.yaml` and every SQL and Python asset under `root`.

    Parameters
    ----------
    root
        The extracted checkout.
    limits
        Size and count limits; the defaults when `None`.

    Returns
    -------
    Checkout
        The configuration text, the assets, the checks, the translation files and, for a
        project with Python assets, its `pyproject.toml` and `uv.lock`.

    Raises
    ------
    ProjectValidationError
        With every problem found: symlinks, a `.sql` and a `.py` file for one
        table, `__init__.py` under `assets/`, a symlink anywhere in a project
        with Python assets, bad or dotted components, oversized or non-UTF-8
        files, too many assets, or no assets at all; and every problem
        `discover_checks` finds under `checks/` and `discover_translations` under
        `translations/`.
    """
    # Imported here: discovery -> checks -> dag -> parse -> discovery is a cycle (checks needs
    # the readers below, and parse needs `DiscoveredAsset`).
    from lakana_core.projects.checks import discover_checks
    from lakana_core.projects.translations import discover_translations

    limits = limits or DeployLimits()
    issues: list[ProjectIssue] = []
    config_text: str | None = None
    config_path = root / CONFIG_FILE
    if config_path.is_symlink():
        issues.append(ProjectIssue(CONFIG_FILE, None, "lakana.yaml must not be a symlink"))
    elif os.path.lexists(config_path):
        data = _read(config_path, CONFIG_FILE, limits.yaml_bytes, issues)
        if data is not None:
            config_text = _decode(data, CONFIG_FILE, issues)

    assets_root = root / ASSETS_DIR
    found: list[DiscoveredAsset] = []
    saw_python = False
    if assets_root.is_symlink():
        issues.append(ProjectIssue(ASSETS_DIR, None, "symlinks are not allowed under assets/"))
    elif assets_root.is_dir():
        asset_files = 0
        produced: dict[str, str] = {}

        def _walk_error(exc: OSError) -> None:
            """Record a directory that could not be listed.

            Parameters
            ----------
            exc
                The error `os.walk` hit.
            """
            name = _display(Path(str(exc.filename)).relative_to(root).as_posix())
            issues.append(ProjectIssue(name, None, f"cannot be read: {exc.strerror}"))

        for directory, dirnames, filenames in os.walk(
            assets_root, onerror=_walk_error, followlinks=False
        ):
            dirnames.sort()
            here = Path(directory)
            for name in list(dirnames):
                if (here / name).is_symlink():
                    relative = (here / name).relative_to(root).as_posix()
                    issues.append(
                        ProjectIssue(
                            _display(relative), None, "symlinks are not allowed under assets/"
                        )
                    )
                    dirnames.remove(name)
            for name in sorted(filenames):
                path = here / name
                relative = path.relative_to(root).as_posix()
                if path.is_symlink():
                    issues.append(
                        ProjectIssue(
                            _display(relative), None, "symlinks are not allowed under assets/"
                        )
                    )
                    continue
                lowered = name.lower()
                suffix = next((ext for ext in _SUFFIXES if lowered.endswith(ext)), None)
                if suffix is None:
                    continue
                kind = _SUFFIXES[suffix]
                saw_python = saw_python or kind == "python"
                if _is_unsafe(relative):
                    issues.append(
                        ProjectIssue(
                            _display(relative),
                            None,
                            "the path has a non-UTF-8 or control character",
                        )
                    )
                    continue
                if lowered == "__init__.py":
                    issues.append(ProjectIssue(relative, None, _INIT_MESSAGE))
                    continue
                asset_files += 1
                if asset_files > limits.max_assets:
                    continue
                parts = list(path.relative_to(assets_root).parts)
                parts[-1] = parts[-1][: -len(suffix)]
                table = _table_name(parts, relative, issues)
                data = _read(path, relative, limits.sql_file_bytes, issues)
                text = _decode(data, relative, issues) if data is not None else None
                if table is None or text is None:
                    continue
                earlier = produced.get(_fold(table))
                if earlier is not None:
                    issues.append(
                        ProjectIssue(
                            relative,
                            None,
                            f"{earlier} and {relative} both produce table {table!r}",
                        )
                    )
                    continue
                produced[_fold(table)] = relative
                found.append(DiscoveredAsset(path=relative, table=table, text=text, kind=kind))
        if asset_files > limits.max_assets:
            issues.append(ProjectIssue(ASSETS_DIR, None, f"more than {limits.max_assets} assets"))
    pyproject_text: str | None = None
    lock: bytes | None = None
    if any(asset.kind == "python" for asset in found):
        raw = _read_root_file(root, PYPROJECT_FILE, limits.pyproject_bytes, issues)
        if raw is not None:
            pyproject_text = _decode(raw, PYPROJECT_FILE, issues)
        lock = _read_root_file(root, LOCK_FILE, limits.lock_bytes, issues)
        if os.path.lexists(root / UV_TOML_FILE):
            issues.append(ProjectIssue(UV_TOML_FILE, None, UV_TOML_MESSAGE))
    if saw_python:
        issues.extend(_links_outside_assets(root))
    checks, check_problems = discover_checks(root, limits=limits)
    issues.extend(check_problems)
    found_translations, translation_problems = discover_translations(root, limits=limits)
    issues.extend(translation_problems)
    if not found and not issues:
        issues.append(ProjectIssue(ASSETS_DIR, None, "no assets found under assets/"))
    if issues:
        raise ProjectValidationError(issues)
    return Checkout(
        config_text=config_text,
        assets=tuple(sorted(found, key=lambda a: a.path)),
        pyproject_text=pyproject_text,
        lock=lock,
        checks=tuple(checks),
        translations=tuple(found_translations),
    )
