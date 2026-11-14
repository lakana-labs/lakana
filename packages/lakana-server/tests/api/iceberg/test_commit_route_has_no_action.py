"""No Iceberg REST request can reach the maintenance form of `commit_table`.

`CatalogService.commit_table` lets `action=MAINTAIN_TABLE` through the protected-environment
guard and accepts only snapshot expiry and the removal of a failed check's tag. Only
table maintenance may ask for that; the REST catalog, which any Iceberg client reaches, never
passes `action`, so its commits take the guarded path.
"""

from __future__ import annotations

import ast
from pathlib import Path

import lakana_server.api.iceberg as iceberg_package


def commit_calls_with_an_action(source: str) -> list[int]:
    """Find the calls of `commit_table` that pass `action` or an unpacked mapping.

    Parameters
    ----------
    source
        Python source.

    Returns
    -------
    list of int
        The line numbers of the calls that pass `action=` or `**something`, which could hide it.
    """
    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute | ast.Name)
            and (node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id)
            == "commit_table"
            and any(keyword.arg in (None, "action") for keyword in node.keywords)
        ):
            found.append(node.lineno)
    return found


def test_the_iceberg_rest_commit_never_passes_an_action() -> None:
    """Every `commit_table` call below `lakana_server/api/iceberg/` leaves `action` alone."""
    root = Path(iceberg_package.__file__).parent
    calls = 0
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        source = path.read_text()
        calls += source.count("commit_table(")
        offenders += [f"{path.name}:{line}" for line in commit_calls_with_an_action(source)]

    assert calls, "the REST catalog's commit route moved; point this test at it"
    assert offenders == []


def test_the_check_finds_an_action_however_it_is_passed() -> None:
    """The scan has teeth: `action=`, a positional-looking call with `**`, and a clean call."""
    assert commit_calls_with_an_action("catalog.commit_table(p, ref, action=Action.MAINTAIN_TABLE)")
    assert commit_calls_with_an_action("catalog.commit_table(p, ref, **options)")
    assert commit_calls_with_an_action("commit_table(p, ref, action=a)")
    assert commit_calls_with_an_action("catalog.commit_table(p, ref, updates=[])") == []
    assert commit_calls_with_an_action("catalog.other(p, action=a)") == []


def test_the_iceberg_rest_commit_asks_for_the_session_release() -> None:
    """Only the REST route releases the database session during the field-id check."""
    source = (Path(iceberg_package.__file__).parent / "router.py").read_text()
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "commit_table"
    ]

    assert len(calls) == 1
    assert [
        k.value.value  # ty: ignore[missing-attribute]
        for k in calls[0].keywords
        if k.arg == "release_session"
    ] == [True]
