"""`pyproject.toml` and `uv.lock` of a project with Python assets.

Both files are parsed as TOML. Nothing is resolved, downloaded or installed
at deploy; the venv is built by the first task that needs it.
"""

from __future__ import annotations

import hashlib
import re
import shlex
import sys
import tomllib

from lakana_core.errors import ValidationError
from lakana_core.projects.issues import ProjectIssue
from lakana_core.projects.package_sources import (
    WHEELHOUSE,
    KnownSources,
    RegisteredIndex,
    comparable_index_url,
    normalize_index_url,
    without_userinfo,
)

PYPROJECT_FILE = "pyproject.toml"
LOCK_FILE = "uv.lock"
UV_TOML_FILE = "uv.toml"
UV_TOML_MESSAGE = (
    "is not supported in a project with Python assets: the worker builds with `uv sync "
    "--no-config`, which ignores it. Settings that shape the lock belong in [tool.uv] in "
    "pyproject.toml (they apply when you run `uv lock`); delete uv.toml, run `uv lock`, and "
    "redeploy"
)

_RELEASE = re.compile(r"\A([0-9]{1,9})\.([0-9]{1,9})")
_CLAUSE = re.compile(r"\A(>=|<=|==|!=|~=|>|<)\s{0,8}([0-9]{1,9}(?:\.[0-9]{1,9}){0,7})(\.\*)?\Z")
_SHOWN = 80


def _shown(value: str) -> str:
    """Make a value from a repository file safe to put in a message.

    Parameters
    ----------
    value
        Text from `pyproject.toml` or `uv.lock`.

    Returns
    -------
    str
        The text with control and non-ASCII characters and ``;`` escaped (issues are
        joined with ``; `` for display, so a value must not hold one), cut to 80 characters.
    """
    text = ascii(value)[1:-1].replace(";", "\\x3b")
    return text if len(text) <= _SHOWN else text[:_SHOWN] + "..."


def lock_digest(lock: bytes) -> str:
    """Hash a lock file.

    Parameters
    ----------
    lock
        The file's bytes as `git archive` produced them for the deployed commit.

    Returns
    -------
    str
        SHA-256, hex: the manifest's `lock_sha256`, one input of the venv cache key.
    """
    return hashlib.sha256(lock).hexdigest()


def versions_compatible(lock_version: str, platform_version: str) -> bool:
    """Decide whether the SDK in a lock can talk to this instance.

    Parameters
    ----------
    lock_version
        The `lakana` version in the lock.
    platform_version
        The version of the worker validating it.

    Returns
    -------
    bool
        `True` for equal versions, for the same minor version before 1.0,
        and for the same major version from 1.0. A version that does not
        start with ``<major>.<minor>`` is compatible only with itself.
    """
    if lock_version == platform_version:
        return True
    locked = _RELEASE.match(lock_version)
    platform = _RELEASE.match(platform_version)
    if locked is None or platform is None:
        return False
    if int(platform.group(1)) == 0:
        return (int(locked.group(1)), int(locked.group(2))) == (0, int(platform.group(2)))
    return int(locked.group(1)) == int(platform.group(1))


def _clause_holds(
    operator: str, wanted: tuple[int, ...], prefix: bool, python: tuple[int, int, int]
) -> bool:
    """Evaluate one version clause.

    Parameters
    ----------
    operator
        ``>=``, ``>``, ``<=``, ``<``, ``==``, ``!=`` or ``~=``.
    wanted
        The clause's release numbers.
    prefix
        Whether the clause ends in ``.*``.
    python
        The interpreter's version.

    Returns
    -------
    bool
        Whether `python` satisfies the clause.
    """
    width = max(len(wanted), 3)
    have = python + (0,) * (width - 3)
    want = wanted + (0,) * (width - len(wanted))
    if operator in ("==", "!="):
        equal = have[: len(wanted)] == wanted if prefix else have == want
        return equal if operator == "==" else not equal
    if operator == "~=":
        return have >= want and have[: len(wanted) - 1] == wanted[:-1]
    if operator == ">=":
        return have >= want
    if operator == ">":
        return have > want
    if operator == "<=":
        return have <= want
    return have < want


def task_interpreter(python: str | None) -> tuple[int, int, int]:
    """Name the interpreter a project's tasks run on, for the `requires-python` check.

    Parameters
    ----------
    python
        `lakana.yaml`'s `python:`, or `None`.

    Returns
    -------
    tuple of int
        This process's ``(major, minor, micro)`` when `python` is `None` or its own version;
        otherwise ``(3, minor, 0)``: the task image's micro release is not known here, so a
        project that names a version must admit its first release.
    """
    own = sys.version_info
    if python is None or python == f"{own.major}.{own.minor}":
        return (own.major, own.minor, own.micro)
    major, minor = (int(part) for part in python.split("."))
    return (major, minor, 0)


def python_admitted(specifier: str, python: tuple[int, int, int]) -> bool | None:
    """Evaluate a `requires-python` specifier for one interpreter.

    Parameters
    ----------
    specifier
        Comma-separated clauses, e.g. ``>=3.11,<3.14``.
    python
        The worker's ``(major, minor, micro)``.

    Returns
    -------
    bool or None
        Whether every clause holds; `None` when a clause cannot be read
        (an empty clause as in ``>=3.11,``, an arbitrary-equality ``===``, a
        marker, ``.*`` with an ordering operator, ``~=`` with a single number).
    """
    admitted = True
    if not specifier.strip():
        return admitted
    for raw in specifier.split(","):
        clause = raw.strip()
        match = _CLAUSE.match(clause)
        if match is None:
            return None
        operator, version, star = match.groups()
        wanted = tuple(int(part) for part in version.split("."))
        prefix = star is not None
        if prefix and operator not in ("==", "!="):
            return None
        if operator == "~=" and len(wanted) < 2:
            return None
        admitted = admitted and _clause_holds(operator, wanted, prefix, python)
    return admitted


def _parse(text: str, name: str, issues: list[ProjectIssue]) -> dict[str, object] | None:
    """Parse a TOML document, recording an issue if it is malformed.

    Parameters
    ----------
    text
        The document.
    name
        The file's name, for the issue.
    issues
        Collects the issue.

    Returns
    -------
    dict or None
        The document, or `None` after an issue.
    """
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        issues.append(ProjectIssue(name, None, f"is not valid TOML: {_shown(str(exc))}"))
    except RecursionError:
        issues.append(ProjectIssue(name, None, "is not valid TOML: nested too deeply"))
    except ValueError as exc:
        issues.append(ProjectIssue(name, None, f"is not valid TOML: {_shown(str(exc))}"))
    return None


def _locked_versions(lock: dict[str, object]) -> dict[str, list[str | None]]:
    """Index the lock's packages by name.

    Parameters
    ----------
    lock
        The parsed lock.

    Returns
    -------
    dict
        Package name → one version per ``[[package]]`` entry with that name
        (forked resolutions lock a name more than once); `None` for an entry
        without a string version (an editable or workspace source).
    """
    packages = lock.get("package")
    versions: dict[str, list[str | None]] = {}
    for package in packages if isinstance(packages, list) else []:
        if not isinstance(package, dict):
            continue
        name, version = package.get("name"), package.get("version")
        if isinstance(name, str):
            versions.setdefault(name, []).append(version if isinstance(version, str) else None)
    return versions


_LOCAL_SOURCES = ("path", "directory", "editable", "virtual")


def _local_packages(lock: dict[str, object]) -> list[str]:
    """List the packages built from a local path other than the root project.

    Nothing a venv installs may come from the repository's files (the cache is
    shared by projects). The root project's source is ``"."``.

    Parameters
    ----------
    lock
        The parsed lock.

    Returns
    -------
    list of str
        Names of packages whose source is a ``path``, ``directory``,
        ``editable`` or ``virtual`` location other than ``"."``, a second
        package at ``"."``, or a ``source`` that is not a table.
    """
    packages = lock.get("package")
    names: list[str] = []
    roots = 0
    for package in packages if isinstance(packages, list) else []:
        if not isinstance(package, dict):
            continue
        source = package.get("source")
        if source is None:
            continue
        local = not isinstance(source, dict)
        if isinstance(source, dict):
            locations = [source[kind] for kind in _LOCAL_SOURCES if kind in source]
            if "." in locations:
                roots += 1
            local = any(location != "." for location in locations) or roots > 1
        if local:
            name = package.get("name")
            names.append(_shown(name) if isinstance(name, str) else "?")
    return names


_AUTHORITY_END = re.compile(r"[/?#]")
_WELL_FORMED_AUTHORITY = re.compile(r"(\[[0-9A-Fa-f:.]*\]|[A-Za-z0-9._-]*)(:[0-9]*)?")
_QUERY_OR_FRAGMENT = re.compile(r"[?#]")
_NAMES_SHOWN = 3


def _listed(names: list[str]) -> str:
    """Join package names for a message, at most three.

    Parameters
    ----------
    names
        Names already made safe with `_shown`.

    Returns
    -------
    str
        The first three, comma-separated, then ``, ...`` if there are more.
    """
    return ", ".join(names[:_NAMES_SHOWN]) + (", ..." if len(names) > _NAMES_SHOWN else "")


def _mask_userinfo(value: str) -> str:
    """Replace the userinfo of a URL with ``***``.

    A path may hold an ``@`` (``feed@view``), so the userinfo is looked for in
    the authority only: between ``//`` and the next ``/``, ``?`` or ``#``. A
    raw lock string whose authority is not well formed (``https://user:p/w@host/``,
    where the password holds a ``/``), or that has a port-like ``:`` and an ``@``
    later, is masked up to its last ``@`` instead: hiding a host in an error
    message costs less than showing a password.

    Parameters
    ----------
    value
        The URL as written in the file.

    Returns
    -------
    str
        The URL with ``//***@`` in place of ``//`` and the userinfo.
    """
    start = value.find("//")
    if start < 0:
        return value
    rest = value[start + 2 :]
    end = _AUTHORITY_END.search(rest)
    authority = rest[: end.start()] if end else rest
    if "@" in authority:
        return f"{value[:start]}//***@{rest[authority.rindex('@') + 1 :]}"
    bare = authority.split("]")[-1]
    if "@" in rest and (not _WELL_FORMED_AUTHORITY.fullmatch(authority) or ":" in bare):
        return f"{value[:start]}//***@{rest[rest.rindex('@') + 1 :]}"
    return value


def _shown_url(value: str) -> str:
    r"""Make a URL from a repository file safe to put in a message.

    A URL in a lock may carry a credential in its userinfo, in its query or in
    its fragment, so none of the three is shown.

    Parameters
    ----------
    value
        The URL as written in the file.

    Returns
    -------
    str
        `_shown` of the URL with its userinfo masked (`_mask_userinfo`) and
        everything from the first ``?`` or ``#`` removed; a space and a backtick are written
        ``\x20`` and ``\x60``, so that the URL cannot end a command quoted in backticks or
        run into the text around it.
    """
    masked = _mask_userinfo(value)
    shown = _shown(_QUERY_OR_FRAGMENT.split(masked, maxsplit=1)[0])
    return shown.replace(" ", "\\x20").replace("`", "\\x60")


def _shown_folder(value: str) -> str:
    """Make a ``registry`` value that is not a URL safe to put in a message.

    Such a value is shown as a local folder, but it may be a malformed URL with a
    credential (``https:/user:password@host/``).

    Parameters
    ----------
    value
        The value as written in the lock.

    Returns
    -------
    str
        `_shown` of the value with everything up to its last ``@`` replaced by
        ``***@`` and everything from the first ``?`` or ``#`` removed.
    """
    masked = f"***@{value.rpartition('@')[2]}" if "@" in value else value
    return _shown(_QUERY_OR_FRAGMENT.split(masked, maxsplit=1)[0])


def _refusal(url: str) -> str | None:
    """Say why a URL cannot be registered as written.

    Parameters
    ----------
    url
        A lock's ``registry`` value.

    Returns
    -------
    str or None
        The rule's message without its final period (it never contains the URL), or `None`
        when the URL can be registered.
    """
    try:
        normalize_index_url(url, credential=False)
    except ValidationError as exc:
        return str(exc).rstrip(".")
    return None


def _outside_wheelhouse(package: dict[str, object]) -> bool:
    """Say whether a package of the folder `wheelhouse` names a file outside it.

    Parameters
    ----------
    package
        One ``[[package]]`` table whose source is the folder `wheelhouse`.

    Returns
    -------
    bool
        `True` if a wheel's or the sdist's ``path`` is not a string, is empty, starts with
        ``/``, holds a backslash or has a ``..`` segment.
    """
    wheels, sdist = package.get("wheels"), package.get("sdist")
    entries = [*(wheels if isinstance(wheels, list) else []), sdist]
    for entry in entries:
        if not isinstance(entry, dict) or "path" not in entry:
            continue
        path = entry["path"]
        if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
            return True
        if ".." in path.split("/"):
            return True
    return False


def _package_tables(lock: dict[str, object]) -> list[dict[str, object]]:
    """List the lock's ``[[package]]`` tables.

    Parameters
    ----------
    lock
        The parsed lock.

    Returns
    -------
    list of dict
        Every entry of ``package`` that is a table.
    """
    packages = lock.get("package")
    return [p for p in packages if isinstance(p, dict)] if isinstance(packages, list) else []


def _source_issues(lock: dict[str, object], sources: KnownSources) -> list[ProjectIssue]:
    """Refuse every package whose source is not a known one.

    Parameters
    ----------
    lock
        The parsed lock.
    sources
        The sources this instance knows.

    Returns
    -------
    list of ProjectIssue
        One issue per unknown index URL, one per local folder other than
        `wheelhouse`, one for git sources and one for direct URLs.
    """
    unknown: dict[str, list[str]] = {}
    refused: dict[tuple[str, str], list[str]] = {}
    folders: dict[str, list[str]] = {}
    outside: list[str] = []
    git: list[str] = []
    direct: list[str] = []
    for package in _package_tables(lock):
        source = package.get("source")
        if not isinstance(source, dict):
            continue
        raw = package.get("name")
        name = _shown(raw) if isinstance(raw, str) else "?"
        if "git" in source:
            git.append(name)
        elif "url" in source:
            direct.append(name)
        if "registry" not in source:
            continue
        registry = source["registry"]
        if not isinstance(registry, str):
            refused.setdefault(("?", "it is not a URL"), []).append(name)
        elif "://" not in registry:
            if registry.removeprefix("./").rstrip("/") != WHEELHOUSE:
                folders.setdefault(_shown_folder(registry), []).append(name)
            elif _outside_wheelhouse(package):
                outside.append(name)
        elif not sources.is_known(registry):
            reason = _refusal(registry)
            if reason is None:
                unknown.setdefault(normalize_index_url(registry, credential=False), []).append(name)
            else:
                refused.setdefault((_shown_url(registry), reason), []).append(name)
    issues = [
        ProjectIssue(
            LOCK_FILE,
            None,
            f"takes {_listed(names)} from the package index {url}, which is not registered on "
            "this instance. Ask an administrator to register it "
            f"(`lakana index add NAME --url {shlex.quote(url)}`), or take the package from a "
            "registered index (`lakana index list`), then run `uv lock`",
        )
        for url, names in unknown.items()
    ]
    issues += [
        ProjectIssue(
            LOCK_FILE,
            None,
            f"takes {_listed(names)} from the package index {url}, which is not a registered "
            f"package index, and this URL cannot be registered as written: {reason}. Take the "
            "package from a registered package index (`lakana index list`), then run `uv lock`",
        )
        for (url, reason), names in refused.items()
    ]
    issues += [
        ProjectIssue(
            LOCK_FILE,
            None,
            f"takes {_listed(names)} from the local folder {folder}; the only folder a lock may "
            f"name is `{WHEELHOUSE}` at the root of the project. Rename the folder or take the "
            "package from a package index, then run `uv lock`",
        )
        for folder, names in folders.items()
    ]
    if outside:
        issues.append(
            ProjectIssue(
                LOCK_FILE,
                None,
                f"takes {_listed(outside)} from a file outside the folder `{WHEELHOUSE}`; "
                f"packages in `{WHEELHOUSE}` must be files inside that folder. Put the file into "
                f"`{WHEELHOUSE}` (not a path with `..`, a backslash or a leading slash), then "
                "run `uv lock`",
            )
        )
    for names, what in ((git, "a git repository"), (direct, "a direct URL")):
        if names:
            issues.append(
                ProjectIssue(
                    LOCK_FILE,
                    None,
                    f"takes {_listed(names)} from {what}, which is not supported. Publish the "
                    "package to a package index, ask an administrator to register that index "
                    "(`lakana index add`), then run `uv lock`",
                )
            )
    return issues


def _declaration_issues(
    project: dict[str, object], sources: KnownSources, *, deploying: bool
) -> list[ProjectIssue]:
    """Check how `pyproject.toml` declares the registered indexes.

    The name rule keeps an author from believing that `team` is the
    registered `team` when it points elsewhere. Nothing at build time depends
    on an index's name, so the rule is applied at a deploy only: a deployed
    project must not stop because an administrator later registered a name it
    uses. It never applies to a local folder (a flat index such as
    `wheelhouse`), which cannot be mistaken for a registered URL.

    Parameters
    ----------
    project
        The parsed `pyproject.toml`.
    sources
        The sources this instance knows.
    deploying
        Whether this is the check of a deploy, which applies the name rule too.

    Returns
    -------
    list of ProjectIssue
        A registered index declared without ``explicit = true``, a registered
        index named by ``index-url`` or ``extra-index-url`` and, at a deploy,
        an index that has a registered index's name and another URL.
    """
    tool = project.get("tool")
    uv = tool.get("uv") if isinstance(tool, dict) else None
    if not isinstance(uv, dict):
        return []
    by_url = sources.by_url()
    by_name = {index.name.casefold(): index for index in sources.registered}
    issues: list[ProjectIssue] = []
    declared = uv.get("index")
    for entry in declared if isinstance(declared, list) else []:
        if not isinstance(entry, dict):
            continue
        url, name = entry.get("url"), entry.get("name")
        comparable = comparable_index_url(without_userinfo(url)) if isinstance(url, str) else None
        shown_name = _shown(name) if isinstance(name, str) else "(no name)"
        shown_url = _shown_url(url) if isinstance(url, str) else "(no URL)"
        if comparable in by_url and entry.get("explicit") is not True:
            issues.append(
                ProjectIssue(
                    PYPROJECT_FILE,
                    None,
                    f"the index {shown_name} ({shown_url}) is a registered package index and "
                    "needs `explicit = true`, so that only the packages you name come from it. "
                    "Add `explicit = true` to its `[[tool.uv.index]]` table and one line per "
                    "package under `[tool.uv.sources]` "
                    f'(`PACKAGE = {{ index = "{shown_name}" }}`), then run `uv lock`',
                )
            )
        named = by_name.get(name.casefold()) if isinstance(name, str) else None
        is_folder = isinstance(url, str) and ":" not in url
        if deploying and named is not None and comparable != named.url and not is_folder:
            issues.append(
                ProjectIssue(
                    PYPROJECT_FILE,
                    None,
                    f"the index named {shown_name} points at {shown_url}, but the package index "
                    f"registered as {_shown(named.name)} is {named.url}. Use the registered URL, "
                    "or give your index another name, then run `uv lock`",
                )
            )
    for key in ("index-url", "extra-index-url"):
        value = uv.get(key)
        for url in [value] if isinstance(value, str) else value if isinstance(value, list) else []:
            registered = (
                by_url.get(comparable_index_url(without_userinfo(url)) or "")
                if isinstance(url, str)
                else None
            )
            if registered is not None:
                issues.append(
                    ProjectIssue(
                        PYPROJECT_FILE,
                        None,
                        f"`{key}` names the registered package index {_shown(registered.name)}. "
                        "Declare it as a `[[tool.uv.index]]` table with `explicit = true` "
                        "instead, and name its packages under `[tool.uv.sources]`, then run "
                        "`uv lock`",
                    )
                )
    return issues


def _loads(text: str) -> dict[str, object] | None:
    """Parse TOML without reporting why it failed.

    Parameters
    ----------
    text
        The document.

    Returns
    -------
    dict or None
        The document, or `None` if it is not valid TOML.
    """
    try:
        return tomllib.loads(text)
    except (ValueError, RecursionError):
        return None


def source_issues(
    pyproject: str | None, lock: bytes | None, sources: KnownSources
) -> list[ProjectIssue]:
    """List only the issues that depend on the registered indexes.

    `lockfile_issues` contains these too. The worker asks for them alone
    before an execution, because their remedy differs: an index was removed
    or changed after the deploy, and the lock itself may be fine. The rule
    about an index's name is left out: it is a rule for a deploy.

    Parameters
    ----------
    pyproject
        `pyproject.toml`'s text, or `None`.
    lock
        `uv.lock`'s raw bytes, or `None`.
    sources
        The sources this instance knows.

    Returns
    -------
    list of ProjectIssue
        The declaration issues, then the source issues; empty for a file
        that is absent or cannot be parsed (`lockfile_issues` reports that).
    """
    issues: list[ProjectIssue] = []
    project = _loads(pyproject) if pyproject is not None else None
    if project is not None:
        issues += _declaration_issues(project, sources, deploying=False)
    try:
        locked = _loads(lock.decode("utf-8")) if lock is not None else None
    except UnicodeDecodeError:
        locked = None
    if locked is not None:
        issues += _source_issues(locked, sources)
    return issues


def used_indexes(lock: bytes | None, sources: KnownSources) -> tuple[RegisteredIndex, ...]:
    """List the registered indexes a lock takes packages from.

    Parameters
    ----------
    lock
        `uv.lock`'s raw bytes, or `None`.
    sources
        The sources this instance knows.

    Returns
    -------
    tuple of RegisteredIndex
        Sorted by name, each once; empty for a lock that is absent or cannot
        be parsed.
    """
    try:
        locked = _loads(lock.decode("utf-8")) if lock is not None else None
    except UnicodeDecodeError:
        locked = None
    if locked is None:
        return ()
    by_url = sources.by_url()
    found: dict[str, RegisteredIndex] = {}
    for package in _package_tables(locked):
        source = package.get("source")
        registry = source.get("registry") if isinstance(source, dict) else None
        index = (
            by_url.get(comparable_index_url(registry) or "") if isinstance(registry, str) else None
        )
        if index is not None:
            found[index.name] = index
    return tuple(found[name] for name in sorted(found))


def _installed_names(lock: dict[str, object]) -> set[str] | None:
    """List the packages a task's environment can contain, as far as the lock shows.

    The environment is the root package's own ``dependencies`` and what they
    need (``uv sync --no-dev --no-default-groups``, no extras). The walk
    ignores markers and takes every extra of a package it reaches, so the
    result can only be too large: a name missing from it is certainly not
    installed.

    Parameters
    ----------
    lock
        The parsed lock.

    Returns
    -------
    set of str or None
        Names reachable from the root package; `None` unless exactly one
        package has the project directory (``"."``) as its source.
    """
    packages = lock.get("package")
    entries = [p for p in packages if isinstance(p, dict)] if isinstance(packages, list) else []
    roots = [
        p
        for p in entries
        if isinstance(p.get("source"), dict)
        and any(p["source"].get(kind) == "." for kind in _LOCAL_SOURCES)
    ]
    if len(roots) != 1:
        return None
    by_name: dict[str, list[dict[str, object]]] = {}
    for entry in entries:
        if isinstance(entry.get("name"), str):
            by_name.setdefault(str(entry["name"]), []).append(entry)

    def names(items: object) -> list[str]:
        found = items if isinstance(items, list) else []
        return [d["name"] for d in found if isinstance(d, dict) and isinstance(d.get("name"), str)]

    pending = names(roots[0].get("dependencies"))
    reached: set[str] = set()
    while pending:
        name = pending.pop()
        if name in reached:
            continue
        reached.add(name)
        for entry in by_name.get(name, []):
            pending += names(entry.get("dependencies"))
            extras = entry.get("optional-dependencies")
            for group in extras.values() if isinstance(extras, dict) else []:
                pending += names(group)
    return reached


def _requires_python(lock: dict[str, object], pyproject: dict[str, object] | None) -> str | None:
    """Find the project's Python constraint.

    Parameters
    ----------
    lock
        The parsed lock.
    pyproject
        The parsed `pyproject.toml`, if it parsed.

    Returns
    -------
    str or None
        The lock's top-level `requires-python`, else `project.requires-python`,
        else `None` (no constraint).
    """
    value = lock.get("requires-python")
    if isinstance(value, str):
        return value
    project = pyproject.get("project") if pyproject is not None else None
    value = project.get("requires-python") if isinstance(project, dict) else None
    return value if isinstance(value, str) else None


def lockfile_issues(
    pyproject: str | None,
    lock: bytes | None,
    *,
    platform_version: str,
    python: tuple[int, int, int],
    sources: KnownSources,
    deploying: bool = True,
    named_python: str | None = None,
) -> list[ProjectIssue]:
    """Validate the dependency files of a project that has a Python asset.

    Parameters
    ----------
    pyproject
        `pyproject.toml`'s text, or `None` if the file is absent. The caller
        decodes it and turns a `UnicodeDecodeError` into a `ProjectIssue`.
    lock
        `uv.lock`'s raw bytes, or `None` if the file is absent.
    platform_version
        The validating worker's version.
    python
        The ``(major, minor, micro)`` the project's tasks run on (`task_interpreter`): the
        validating worker's own, or the one `named_python` names.
    sources
        The sources a lock may name on this instance: the registered package
        indexes and the operator's index (PyPI and the folder `wheelhouse`
        are always known).
    deploying
        Whether this is the check of a deploy. The check before an execution
        passes `False`, which leaves out the one rule that is for a deploy
        only: an index with a registered index's name and another URL.
    named_python
        `lakana.yaml`'s `python:`, when the project names one: an issue with `requires-python`
        then points at that setting instead of at the worker's Python.

    Returns
    -------
    list of ProjectIssue
        Missing or malformed files, a lock without `lakana` or `polars`, an
        incompatible `lakana`, a `requires-python` that excludes the worker, a
        package from a source that is not known, a registered index declared
        without ``explicit = true`` or, at a deploy, an index with a
        registered index's name and another URL.
        Values taken from the files are rendered with `repr`-style escapes.
    """
    issues: list[ProjectIssue] = []
    if pyproject is None:
        issues.append(
            ProjectIssue(
                PYPROJECT_FILE,
                None,
                "a project with Python assets needs a pyproject.toml at the repository root",
            )
        )
    if lock is None:
        issues.append(
            ProjectIssue(
                LOCK_FILE,
                None,
                "a project with Python assets needs a committed uv.lock; run `uv lock`",
            )
        )
    project = _parse(pyproject, PYPROJECT_FILE, issues) if pyproject is not None else None
    if project is not None:
        issues += _declaration_issues(project, sources, deploying=deploying)
    if lock is None:
        return issues
    try:
        lock_text = lock.decode("utf-8")
    except UnicodeDecodeError:
        return [*issues, ProjectIssue(LOCK_FILE, None, "is not valid UTF-8")]
    locked = _parse(lock_text, LOCK_FILE, issues)
    if locked is None:
        return issues
    versions = _locked_versions(locked)
    sdk = versions.get("lakana")
    if sdk is None:
        issues.append(
            ProjectIssue(
                LOCK_FILE,
                None,
                "does not contain lakana; add lakana[assets] to the project's dependencies "
                "and run `uv lock`",
            )
        )
    elif None in sdk:
        issues.append(
            ProjectIssue(
                LOCK_FILE,
                None,
                "lakana has no locked version (editable or workspace source); "
                "depend on the released package and run `uv lock`",
            )
        )
    else:
        refused = next(
            (v for v in sdk if v is not None and not versions_compatible(v, platform_version)),
            None,
        )
        if refused is not None:
            rule = (
                "before 1.0 the minor versions must match"
                if platform_version.startswith("0.")
                else "the major versions must match"
            )
            issues.append(
                ProjectIssue(
                    LOCK_FILE,
                    None,
                    f"has lakana {_shown(refused)}, but this instance runs {platform_version} "
                    f"({rule}); update the dependency and run `uv lock`",
                )
            )
    installed = _installed_names(locked)
    if sdk and None not in sdk and installed is not None and "lakana" not in installed:
        issues.append(
            ProjectIssue(
                LOCK_FILE,
                None,
                "lakana is locked, but no task installs it: it is only in a dependency group or "
                "an extra, not in the project's own dependencies; add `lakana[assets]` to "
                "`[project] dependencies` in pyproject.toml, run `uv lock`, commit and deploy "
                "again",
            )
        )
    local = _local_packages(locked)
    if local:
        issues.append(
            ProjectIssue(
                LOCK_FILE,
                None,
                "local path and workspace dependencies are not supported "
                f"({', '.join(local[:3])}{', ...' if len(local) > 3 else ''}); put helper code "
                "in the repository (it is importable from the repository root and `src/`) "
                "or depend on a released package",
            )
        )
    issues += _source_issues(locked, sources)
    if "polars" not in versions:
        issues.append(
            ProjectIssue(
                LOCK_FILE,
                None,
                "does not contain polars; depend on lakana[assets] and run `uv lock`",
            )
        )
    specifier = _requires_python(locked, project)
    if specifier is not None:
        admitted = python_admitted(specifier, python)
        shown = ".".join(str(part) for part in python)
        if admitted is None:
            issues.append(
                ProjectIssue(
                    LOCK_FILE, None, f"requires-python '{_shown(specifier)}' cannot be evaluated"
                )
            )
        elif not admitted:
            interpreter = (
                f"the worker's Python {shown}"
                if named_python is None
                else f"Python {_shown(named_python)}, which python: in lakana.yaml names"
            )
            issues.append(
                ProjectIssue(
                    LOCK_FILE,
                    None,
                    f"requires-python '{_shown(specifier)}' does not admit {interpreter}",
                )
            )
    return issues
