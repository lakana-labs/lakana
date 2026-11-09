"""Warm the slow-test re-run up, so each candidate pays only what it costs in a shard.

``scripts/time_budgets.py slow-tests --confirm`` runs every unmarked test that took 2 s or more
in its shard again, in a fresh pytest process per package, and loads this plugin there with
``-p scripts.remeasure_warm_up``. In a fresh process the first test that needs a session- or
module-scoped fixture pays its setup (``catalog_template`` builds a schema in about 0.7 s); in
a shard that cost falls on whichever test of the module runs first. So, per candidate, this
plugin keeps the cheapest other test of the same module whose fixtures include all of the
candidate's session-, package- and module-scoped ones, runs it right before the candidate, and
deselects everything else. A candidate without such a test runs alone. The candidate's own
function-scoped setup, call and teardown are still measured in full; only its time is counted.

``LAKANA_REMEASURE_WARM_UP`` names a JSON file ``{"candidates": [node id, ...], "seconds":
{node id: seconds}}``: the candidates, and the shard times of the tests that may warm them up.
A test missing from ``seconds`` (skipped in its shard, or not run) is never a warm-up, nor is
one marked ``skip``. Without the variable the plugin changes nothing.

See docs/agent/testing.md, "Time budgets".
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path

import pytest

WARM_UP_ENV = "LAKANA_REMEASURE_WARM_UP"
_SHARED_SCOPES = frozenset({"session", "package", "module"})


def read_plan(path: Path) -> tuple[list[str], dict[str, float]]:
    """Read the candidates and the shard times that `time_budgets.py` wrote.

    Parameters
    ----------
    path
        The JSON file ``LAKANA_REMEASURE_WARM_UP`` names.

    Returns
    -------
    tuple
        The candidates' node ids, and the seconds of the possible warm-ups by node id.

    Raises
    ------
    pytest.UsageError
        If the file cannot be read or does not hold a plan.
    """
    try:
        data = json.loads(path.read_text())
    except OSError as error:
        raise pytest.UsageError(
            f"{WARM_UP_ENV} names {path}, but the plugin cannot read it ({error}). Unset the "
            "variable, or let scripts/time_budgets.py slow-tests --confirm write the file."
        ) from None
    except ValueError:
        data = None
    candidates = data.get("candidates") if isinstance(data, dict) else None
    seconds = data.get("seconds") if isinstance(data, dict) else None
    if not isinstance(candidates, list) or not isinstance(seconds, dict):
        raise pytest.UsageError(
            f"{path} is not a warm-up plan: it must be a JSON object with a list "
            '"candidates" and an object "seconds". scripts/time_budgets.py slow-tests '
            "--confirm writes it; unset the variable to run without it."
        )
    return [str(c) for c in candidates], {str(k): float(v) for k, v in seconds.items()}


def _shared(item: pytest.Item) -> frozenset[object]:
    """Name the fixtures whose setup outlives the test: session, package and module scope.

    Parameters
    ----------
    item
        A collected test.

    Returns
    -------
    frozenset
        The fixture definitions the test uses at those scopes; empty for a non-function item.
    """
    if not isinstance(item, pytest.Function):
        return frozenset()
    # pytest has no public API for an item's resolved fixture definitions; the last one of
    # each name is the one this item uses (the closest override).
    return frozenset(
        defs[-1]
        for defs in item._fixtureinfo.name2fixturedefs.values()
        if defs[-1].scope in _SHARED_SCOPES
    )


def choose(
    items: Sequence[pytest.Item], candidates: Sequence[str], seconds: dict[str, float]
) -> list[pytest.Item]:
    """Order the candidates, each after the cheapest test that pays its one-off setup.

    Parameters
    ----------
    items
        The collected tests, in collection order.
    candidates
        The candidates' node ids.
    seconds
        The shard times of the tests that may warm up; any other test never does.

    Returns
    -------
    list of pytest.Item
        The tests to run, in order: each candidate, preceded by its warm-up the first time
        its module and fixture set come up.
    """
    wanted = set(candidates)
    keep: list[pytest.Item] = []
    warmed: set[tuple[Path, frozenset[object]]] = set()
    for candidate in (item for item in items if item.nodeid in wanted):
        needs = _shared(candidate)
        key = (candidate.path, needs)
        if key not in warmed:
            warmed.add(key)
            sharing = [
                item
                for item in items
                if item.path == key[0]
                and item.nodeid not in wanted
                and item.nodeid in seconds
                and item.get_closest_marker("skip") is None
                and needs <= _shared(item)
            ]
            if sharing:
                warm_up = min(sharing, key=lambda item: seconds[item.nodeid])
                if warm_up not in keep:
                    keep.append(warm_up)
        keep.append(candidate)
    return keep


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Keep the candidates and their warm-ups, after every other selection.

    Parameters
    ----------
    config
        pytest's config.
    items
        The collected tests, changed in place.
    """
    path = os.environ.get(WARM_UP_ENV)
    if not path:
        return
    candidates, seconds = read_plan(Path(path))
    keep = choose(items, candidates, seconds)
    kept = set(map(id, keep))
    config.hook.pytest_deselected(items=[item for item in items if id(item) not in kept])
    items[:] = keep
