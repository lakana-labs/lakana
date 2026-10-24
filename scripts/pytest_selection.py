"""Select the tests of a CI job: one shard of the suite, the quarantine, the ``slow`` marker.

Loaded by ``packages/conftest.py`` and ``scripts/conftest.py``, like the memory limit. Its one
hook runs after pytest's own ``-m`` selection, and only once when both files load it.

- **Shards.** ``LAKANA_TEST_SHARD=I/N`` keeps shard I of N (counted from one). Whole test files
  are spread over the shards, the longest first, each to the shard with the least time so far;
  the seconds per file come from ``scripts/test-durations.json`` (written by
  ``scripts/time_budgets.py durations``), and a file without an entry counts one second. Every
  shard and every xdist worker computes the same answer from the same collection.
- **Quarantine.** A test marked ``@pytest.mark.quarantine(issue=N)`` is skipped, with a reason
  naming issue #N. ``LAKANA_TEST_QUARANTINED=1`` turns the selection around: only quarantined
  tests run (the non-blocking ``quarantined tests`` job of ``ci.yml``).
- **Slow tests.** A test marked ``@pytest.mark.slow`` gets the user property ``slow=true``,
  which pytest's junit report carries; the slow-test ratchet counts those.

See docs/agent/testing.md, "Time budgets" and "Flaky tests: the same-day rule".
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterable
from pathlib import Path

import pytest

SHARD_ENV = "LAKANA_TEST_SHARD"
QUARANTINE_ENV = "LAKANA_TEST_QUARANTINED"
DURATIONS = Path(__file__).with_name("test-durations.json")
UNKNOWN_FILE_SECONDS = 1.0
_DONE = pytest.StashKey[bool]()


def parse_shard(text: str) -> tuple[int, int]:
    """Read ``I/N`` as shard I of N.

    Parameters
    ----------
    text
        The value of ``LAKANA_TEST_SHARD``.

    Returns
    -------
    tuple of int
        The shard's number, from one, and the number of shards.

    Raises
    ------
    pytest.UsageError
        If the value is not two whole numbers with ``1 <= I <= N``.
    """
    parts = text.strip().split("/")
    try:
        index, count = (int(part) for part in parts)
    except ValueError:
        index, count = 0, 0
    if not 1 <= index <= count:
        raise pytest.UsageError(
            f"{SHARD_ENV}={text!r} is not a shard: write it as I/N with 1 <= I <= N, "
            f"for example {SHARD_ENV}=1/3, or unset it to run every test."
        )
    return index, count


def read_durations(path: Path | None) -> dict[str, float]:
    """Read the measured seconds per test file.

    Parameters
    ----------
    path
        The durations file; None or a missing file reads as empty.

    Returns
    -------
    dict
        Test file, relative to the root, to seconds.

    Raises
    ------
    pytest.UsageError
        If the file is not a JSON object of non-negative numbers.
    """
    if path is None or not path.exists():
        return {}
    fix = (
        f"{path} is not a JSON object of seconds per test file. Restore it from git, or "
        "regenerate it from junit reports: uv run python scripts/time_budgets.py durations "
        f"--output {path} REPORT.xml ..."
    )
    try:
        data = json.loads(path.read_text())
    except ValueError:
        raise pytest.UsageError(fix) from None
    if not isinstance(data, dict) or not all(
        isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v) and v >= 0
        for v in data.values()
    ):
        raise pytest.UsageError(fix)
    return {str(k): float(v) for k, v in data.items()}


def assign_files(files: Iterable[str], weights: dict[str, float], count: int) -> dict[str, int]:
    """Spread test files over shards by their seconds, the longest first.

    Parameters
    ----------
    files
        The test files collected, relative to the root; the order does not matter.
    weights
        Measured seconds per file; a file without one counts `UNKNOWN_FILE_SECONDS`.
    count
        The number of shards.

    Returns
    -------
    dict
        Test file to its shard, from one.
    """
    totals = [0.0] * count
    shards: dict[str, int] = {}
    for file in sorted(set(files), key=lambda f: (-weights.get(f, UNKNOWN_FILE_SECONDS), f)):
        lightest = min(range(count), key=lambda i: (totals[i], i))
        totals[lightest] += weights.get(file, UNKNOWN_FILE_SECONDS)
        shards[file] = lightest + 1
    return shards


def _issue(item: pytest.Item) -> int:
    """Read the issue of a quarantined test.

    Parameters
    ----------
    item
        A test marked ``quarantine``.

    Returns
    -------
    int
        The issue number.

    Raises
    ------
    pytest.UsageError
        If the marker does not name a positive issue number.
    """
    marker = item.get_closest_marker("quarantine")
    issue = marker.kwargs.get("issue") if marker is not None else None
    if isinstance(issue, bool) or not isinstance(issue, int) or issue < 1:
        raise pytest.UsageError(
            f"{item.nodeid} is quarantined without an issue: write "
            "@pytest.mark.quarantine(issue=N) with the number of the issue labelled `flaky` "
            "that tracks its fix (docs/agent/testing.md, 'Flaky tests: the same-day rule')."
        )
    return issue


def _relative(item: pytest.Item, root: Path) -> str:
    """Name a test's file relative to the root.

    Parameters
    ----------
    item
        A collected test.
    root
        pytest's root directory.

    Returns
    -------
    str
        The POSIX path of the file, relative to `root` when it lies below it.
    """
    path = Path(item.path)
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def select(config: pytest.Config, items: list[pytest.Item], durations: Path | None) -> None:
    """Apply the quarantine, the shard and the ``slow`` property to the collected tests.

    Parameters
    ----------
    config
        pytest's config.
    items
        The collected tests, changed in place.
    durations
        The file of seconds per test file.
    """
    if config.stash.get(_DONE, False):
        return
    config.stash[_DONE] = True
    keep: list[pytest.Item] = []
    dropped: list[pytest.Item] = []
    only_quarantined = os.environ.get(QUARANTINE_ENV) == "1"
    for item in items:
        quarantined = item.get_closest_marker("quarantine") is not None
        if quarantined:
            issue = _issue(item)
            if not only_quarantined:
                item.add_marker(
                    pytest.mark.skip(reason=f"in quarantine until issue #{issue} is fixed")
                )
        (keep if quarantined or not only_quarantined else dropped).append(item)
        if item.get_closest_marker("slow") is not None:
            item.user_properties.append(("slow", "true"))
    shard = os.environ.get(SHARD_ENV)
    if shard is not None:
        index, count = parse_shard(shard)
        files = {id(item): _relative(item, config.rootpath) for item in keep}
        spread = assign_files(files.values(), read_durations(durations), count)
        dropped.extend(item for item in keep if spread[files[id(item)]] != index)
        keep = [item for item in keep if spread[files[id(item)]] == index]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = keep


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Select this job's tests, after pytest's own ``-m`` selection.

    Parameters
    ----------
    config
        pytest's config.
    items
        The collected tests, changed in place.
    """
    select(config, items, DURATIONS)
