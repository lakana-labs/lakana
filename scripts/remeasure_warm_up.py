"""Warm the slow-test re-run up, so each candidate pays only what it costs in a shard.

``scripts/time_budgets.py slow-tests --confirm`` runs every unmarked test that took 2 s or more
in its shard again (first in a pytest process per package, then each test still over the line
in a fresh process of its own), and loads this plugin there with
``-p scripts.remeasure_warm_up``. In a fresh process the first test that needs a session- or
module-scoped fixture pays its setup (``catalog_template`` builds a schema in about 0.7 s, and
``_openapi_text`` builds the server's OpenAPI document in about 2 s); in a shard that cost falls
on whichever test runs first. One-off setup is never charged to a candidate: per candidate, this
plugin keeps the cheapest other test of the same module whose fixtures include all of the
candidate's session-, package-, module- and class-scoped ones with the same instances (the
same class for a class fixture, the same parameter for a parametrized one), and runs it right
before the candidate. When the module has no such test, it adds a no-op item beside the
candidate (named ``test_x[warm-up][params]`` for ``test_x[params]``) that requests only those
fixtures, with the candidate's parameters and its markers except skip and xfail, and so sets
them up; a candidate with no such fixture runs alone. Each distinct setup gets its own warm-up,
so two parameter sets or two classes in one process are each warmed. Everything else is
deselected. The candidate's own function-scoped setup, call and teardown are still measured in
full. This moves setup only: the teardown of the shared fixtures runs after the last item of
the process, which is a candidate, and stays on its clock (xunit1 times include teardown).

``LAKANA_REMEASURE_WARM_UP`` names a JSON file ``{"candidates": [node id, ...], "seconds":
{node id: seconds}}``: the candidates, and the shard times of the tests that may warm them up.
A test missing from ``seconds`` (skipped in its shard, or not run) is never a warm-up, nor is
one marked ``skip``. Without the variable the plugin changes nothing.

See docs/agent/testing.md, "Time budgets".
"""

from __future__ import annotations

import inspect
import json
import os
from collections.abc import Sequence
from pathlib import Path

import pytest

WARM_UP_ENV = "LAKANA_REMEASURE_WARM_UP"
_SHARED_SCOPES = frozenset({"session", "package", "module"})
# What a no-op item beside the candidate sets up: its parent is the candidate's, so a class
# fixture of the candidate's class lives on too.
_WARMED_SCOPES = _SHARED_SCOPES | {"class"}
# Markers that decide an item's outcome rather than its setup; a warm-up item takes none.
_OUTCOME_MARKERS = frozenset({"skip", "skipif", "xfail"})


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
    """Name the fixtures whose setup outlives the test: session, package, module and class.

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
        if defs[-1].scope in _WARMED_SCOPES
    )


def _setup_key(item: pytest.Item, needs: frozenset[object]) -> tuple[object, ...]:
    """Say which setup of `needs` an item gets: the same key means the same instances.

    Parameters
    ----------
    item
        A collected test.
    needs
        The fixture definitions in question, from `_shared`.

    Returns
    -------
    tuple
        The item's module, its class when one of `needs` is class-scoped (a class fixture is
        set up per class), and the parameter index of each parametrized one of `needs`.
    """
    if not isinstance(item, pytest.Function):
        return (item.path, None, ())
    names = {name for name, defs in item._fixtureinfo.name2fixturedefs.items() if defs[-1] in needs}
    callspec = getattr(item, "callspec", None)
    indices = tuple(
        sorted((n, i) for n, i in callspec.indices.items() if n in names) if callspec else ()
    )
    by_class = any(getattr(d, "scope", None) == "class" for d in needs)
    return (item.path, item.cls if by_class else None, indices)


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
        The tests to run, in order: each candidate, preceded by its warm-up (a test of its
        module, or else `fixture_warm_up`'s item) the first time its setup (module, class
        when it matters, parameters and fixture set) comes up.
    """
    wanted = set(candidates)
    keep: list[pytest.Item] = []
    warmed: set[tuple[object, ...]] = set()
    for candidate in (item for item in items if item.nodeid in wanted):
        needs = _shared(candidate)
        setup = _setup_key(candidate, needs)
        key = (*setup, needs)
        if key not in warmed:
            warmed.add(key)
            sharing = [
                item
                for item in items
                if item.path == candidate.path
                and item.nodeid not in wanted
                and item.nodeid in seconds
                and item.get_closest_marker("skip") is None
                and needs <= _shared(item)
                and _setup_key(item, needs) == setup
            ]
            if sharing:
                warm_up = min(sharing, key=lambda item: seconds[item.nodeid])
            else:
                warm_up = fixture_warm_up(candidate)
            if warm_up is not None and warm_up not in keep:
                keep.append(warm_up)
        keep.append(candidate)
    return keep


def fixture_warm_up(candidate: pytest.Item) -> pytest.Function | None:
    """Make a no-op item that sets up the candidate's shared fixtures, and nothing else.

    It sits beside the candidate (same module, same class), requests by name each fixture of
    the candidate's at session, package, module or class scope (their own dependencies come
    with them, autouse fixtures too), and carries the candidate's parameters and markers, so it
    sets up what the candidate would set up first. Its call does nothing.

    Parameters
    ----------
    candidate
        The test to warm up.

    Returns
    -------
    pytest.Function or None
        The item, named ``<test>[warm-up]`` and the candidate's parameters; None for an
        item that is not a test function or that uses no such fixture.
    """
    parent = candidate.parent
    if not isinstance(candidate, pytest.Function) or parent is None:
        return None
    # pytest has no public API for an item's resolved fixtures or the fixture manager. A name
    # the manager cannot find beside the candidate is a parameter of `@pytest.mark.parametrize`
    # with a scope, not a fixture: the candidate's parameters below set it.
    manager = candidate.session._fixturemanager
    names = [
        name
        for name, defs in candidate._fixtureinfo.name2fixturedefs.items()
        if defs[-1].scope in _WARMED_SCOPES
        and name.isidentifier()
        and manager.getfixturedefs(name, parent)
    ]
    if not names:
        return None

    def warm_up(**fixtures: object) -> None:
        """Do nothing: the fixtures it requests are its whole point."""

    # pytest reads the fixture names from the signature; beside a method it drops the first
    # parameter as `self`, so a method's warm-up gets one to drop.
    leading = [inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    warm_up.__signature__ = inspect.Signature(  # ty: ignore[missing-attribute]
        [
            *(leading if candidate.cls is not None else []),
            *(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY) for name in names),
        ]
    )
    item = pytest.Function.from_parent(
        parent,
        name=f"{candidate.originalname}[warm-up]",
        callobj=warm_up,
        callspec=getattr(candidate, "callspec", None),
    )
    # A fixture may read a marker of the candidate's; skip and xfail would only make the
    # no-op item report an outcome that never happened (a strict xfail an XPASS failure).
    item.own_markers.extend(
        mark for mark in candidate.own_markers if mark.name not in _OUTCOME_MARKERS
    )
    return item


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
