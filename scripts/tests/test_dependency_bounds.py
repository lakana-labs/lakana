"""The dependencies whose API Lakana uses deeply must not float onto a release nobody tested."""

import re
import tomllib
from pathlib import Path

import pytest
import yaml

PACKAGES = Path(__file__).resolve().parents[2] / "packages"
DEPENDABOT = Path(__file__).resolve().parents[2] / ".github" / "dependabot.yml"

DEEPLY_USED = [
    ("lakana-core", "sqlglot"),
    ("lakana-core", "pyiceberg"),
    ("lakana-worker", "duckdb"),
]


def _requirements(package: str) -> list[str]:
    """Collect every runtime requirement a package declares, including its build hook's.

    Parameters
    ----------
    package
        Directory name under ``packages/``.

    Returns
    -------
    list of str
        The requirement strings.
    """
    data = tomllib.loads((PACKAGES / package / "pyproject.toml").read_text())
    hook = data.get("tool", {}).get("hatch", {}).get("metadata", {}).get("hooks", {})
    return [
        *data["project"].get("dependencies", []),
        *hook.get("custom", {}).get("dependencies", []),
    ]


@pytest.mark.parametrize(("package", "dependency"), DEEPLY_USED)
def test_a_deeply_used_dependency_has_an_upper_bound(package: str, dependency: str) -> None:
    """`sqlglot`, `pyiceberg` and `duckdb` carry an upper bound, raised on purpose.

    A fresh ``pip install`` ignores the lock, so an unbounded requirement takes the newest
    release on the day. The bound is raised in its own pull request, after reading the upstream
    changelog and running the suite.
    """
    (requirement,) = (r for r in _requirements(package) if re.match(rf"{dependency}\b", r))
    assert re.search(r"<=?\s*\d", requirement), (
        f"{package} must give {dependency} an upper bound (found {requirement!r}): a fresh install "
        "ignores the lock and would take a release Lakana was not tested on. Raise the bound in "
        "its own pull request, after reading the upstream changelog and running the suite."
    )


def _dependabot_uv_ignores(dependency: str) -> list[str]:
    """Collect the versions Dependabot's `uv` updates ignore for one dependency.

    Parameters
    ----------
    dependency
        The package name as Dependabot's `dependency-name` gives it.

    Returns
    -------
    list of str
        The `versions` of every matching ignore entry, in file order.
    """
    config = yaml.safe_load(DEPENDABOT.read_text())
    (uv,) = (u for u in config["updates"] if u["package-ecosystem"] == "uv")
    return [
        version
        for entry in uv.get("ignore", [])
        if entry["dependency-name"] == dependency
        for version in entry.get("versions", [])
    ]


def test_a_sqlglot_hold_below_a_minor_release_is_also_a_dependabot_ignore() -> None:
    """A hold such as `sqlglot<30.25` stays put until the rewrite is adapted (issue #415).

    Without an ignore Dependabot's weekly group raises the bound itself (pull request #528 made
    it `>=30.29.0,<30.30`), which moves the security-relevant query guard onto a sqlglot nobody
    reviewed. A bound at a major release (`<31`) is the ordinary one and needs no ignore; a
    leftover ignore would then keep the lock from moving inside it.
    """
    (requirement,) = (r for r in _requirements("lakana-core") if re.match(r"sqlglot\b", r))
    match = re.search(r"<\s*(\d+(?:\.\d+)?)\s*$", requirement)
    assert match, f"sqlglot needs a plain `<X` or `<X.Y` upper bound (found {requirement!r})"
    bound = match.group(1)
    expected = [f">={bound}"] if "." in bound else []
    assert _dependabot_uv_ignores("sqlglot") == expected, (
        f"lakana-core requires {requirement!r}: .github/dependabot.yml must ignore sqlglot "
        f"{expected or 'nothing'} so that Dependabot keeps the hold, and drop the ignore when "
        "the bound goes back to the next major release."
    )


def test_the_worker_requires_the_duckdb_release_its_query_process_is_written_for() -> None:
    """The query process uses DuckDB 2's settings, which 1.x does not have (issue #96).

    `extension_directories`, `allow_extension_repositories` and `async_threads` fail on 1.x, so
    a lower bound below 2.0 would let a fresh install pick a DuckDB the worker cannot configure.
    """
    (requirement,) = (r for r in _requirements("lakana-worker") if re.match(r"duckdb\b", r))
    assert requirement == "duckdb>=2.0,<3"
