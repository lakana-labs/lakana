"""The reference table of log codes, the code list in the source and the call sites cannot drift.

`docs/site/reference/log-events.md` lists every `event=<code>` with the process that writes it.
This test fails when a code is in the table but not in `lakana_core.log_events.LogEvent` (or the
other way round), when the table's process differs from `WHERE`, when no source file of that
process logs the code, and when no test of the packages asserts the code: that a code is emitted
where it is documented is proven there, by the test of each call site.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from lakana_core.log_events import WHERE, LogEvent

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs/site/reference/log-events.md"

_ROW = re.compile(r"^\| `([a-z_]+)` \| ([^|]+?) \|", re.MULTILINE)
"""A row of the table: the code in backticks, then the process that writes it."""

_SOURCES = {
    "server": ("lakana-server", "lakana-core"),
    "worker": ("lakana-worker", "lakana-core"),
}
"""The packages whose source runs in each process; `lakana-core` runs in both."""


def _documented() -> list[tuple[str, str]]:
    """Read the table of the reference page.

    Returns
    -------
    list of tuple of str
        Each code with its process as the table names it, without a remark in parentheses.
    """
    text = PAGE.read_text()
    table = text[text.index("## The codes") :]
    return [(code, re.sub(r"\s*\(.*\)", "", where).strip()) for code, where in _ROW.findall(table)]


def _call_sites(name: str) -> set[str]:
    """Name the packages whose source logs a code.

    Parameters
    ----------
    name
        The member's name, as in `LogEvent.MEMORY_STOP`.

    Returns
    -------
    set of str
        The package directories (`lakana-worker`, ...) with a file that uses it.
    """
    used = re.compile(rf"\bLogEvent\.{name}\b")
    return {
        path.relative_to(ROOT / "packages").parts[0]
        for path in (ROOT / "packages").glob("*/src/**/*.py")
        if path.name != "log_events.py" and used.search(path.read_text())
    }


def test_the_table_lists_exactly_the_codes_of_the_source_in_its_order() -> None:
    assert [code for code, _ in _documented()] == [code.value for code in LogEvent]


def test_the_table_names_the_process_the_source_names() -> None:
    assert dict(_documented()) == {code.value: where for code, where in WHERE.items()}


@pytest.mark.slow
def test_every_code_is_logged_by_the_process_the_table_names() -> None:
    for code in LogEvent:
        processes = WHERE[code].split(", ")
        sites = _call_sites(code.name)
        assert sites, f"no source file logs {code.value}"
        allowed = {package for process in processes for package in _SOURCES[process]}
        assert sites <= allowed, f"{code.value} is logged in {sites}, documented as {WHERE[code]}"
        for process in processes:
            assert sites & set(_SOURCES[process]), f"{code.value}: nothing of the {process} logs it"


def test_every_code_has_a_test_that_asserts_it() -> None:
    tests = "\n".join(
        path.read_text()
        for path in (ROOT / "packages").glob("*/tests/**/*.py")
        if path.name != "test_log_events.py"
    )
    for code in LogEvent:
        assert f'"{code.value}"' in tests, f"no test asserts the code {code.value}"


def test_the_health_how_to_and_the_operator_guide_link_the_table() -> None:
    for page in ("how-to/instance-health.md", "how-to/operate-for-others.md"):
        assert "reference/log-events.md" in (ROOT / "docs/site" / page).read_text(), page
