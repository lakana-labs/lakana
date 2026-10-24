"""The test selection of CI: shards of the suite, quarantined tests and the ``slow`` marker."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import pytest_selection as plugin
from scripts.pytest_selection import (
    QUARANTINE_ENV,
    SHARD_ENV,
    assign_files,
    parse_shard,
    pytest_collection_modifyitems,
    read_durations,
    select,
)


class _Marker:
    """Stands in for a pytest marker.

    Parameters
    ----------
    kwargs
        The marker's keyword arguments.
    """

    def __init__(self, kwargs: dict[str, object]) -> None:
        self.kwargs = kwargs
        self.args: tuple[object, ...] = ()


class _Item:
    """Stands in for a collected test.

    Parameters
    ----------
    path
        The test file, relative to the root.
    name
        The test's name.
    markers
        Marker name to keyword arguments.
    """

    def __init__(
        self, path: str, name: str, markers: dict[str, dict[str, object]] | None = None
    ) -> None:
        self.path = Path("/root") / path
        self.nodeid = f"{path}::{name}"
        self.markers = markers or {}
        self.added: list[pytest.MarkDecorator] = []
        self.user_properties: list[tuple[str, object]] = []

    def get_closest_marker(self, name: str) -> _Marker | None:
        """Return the marker of that name.

        Parameters
        ----------
        name
            Marker name.

        Returns
        -------
        _Marker or None
            The marker, or None when the test does not carry it.
        """
        return _Marker(self.markers[name]) if name in self.markers else None

    def add_marker(self, marker: pytest.MarkDecorator) -> None:
        """Record a marker added at collection.

        Parameters
        ----------
        marker
            The marker.
        """
        self.added.append(marker)


class _Hook:
    """Records the tests the plugin deselects."""

    def __init__(self) -> None:
        self.deselected: list[_Item] = []

    def pytest_deselected(self, items: list[_Item]) -> None:
        """Record deselected tests.

        Parameters
        ----------
        items
            The tests.
        """
        self.deselected.extend(items)


class _Config:
    """Stands in for pytest's config: a root path, a stash and the hook relay."""

    def __init__(self) -> None:
        self.rootpath = Path("/root")
        self.stash = pytest.Stash()
        self.hook = _Hook()


def _run(items: list[_Item], durations: Path | None = None) -> tuple[list[_Item], _Config]:
    """Run the selection over fake tests.

    Parameters
    ----------
    items
        The collected tests.
    durations
        The durations file; None reads none.

    Returns
    -------
    tuple
        The tests left selected, and the config that recorded the deselected ones.
    """
    config = _Config()
    selected = list(items)
    select(config, selected, durations=durations)  # ty: ignore[invalid-argument-type]
    return selected, config


@pytest.mark.parametrize(
    ("text", "expected"), [("1/3", (1, 3)), ("3/3", (3, 3)), (" 2/4 ", (2, 4))]
)
def test_a_shard_is_read_as_index_and_count(text: str, expected: tuple[int, int]) -> None:
    """``LAKANA_TEST_SHARD=I/N`` runs shard I of N, counted from one."""
    assert parse_shard(text) == expected


@pytest.mark.parametrize("text", ["", "1", "0/3", "4/3", "a/b", "1/0", "-1/2", "1/2/3"])
def test_a_malformed_shard_names_the_variable_and_the_form(text: str) -> None:
    """A typo must stop the run, never run everything or nothing."""
    with pytest.raises(pytest.UsageError, match=r"LAKANA_TEST_SHARD.*I/N"):
        parse_shard(text)


def test_files_are_spread_by_their_measured_seconds() -> None:
    """The longest file goes first, each to the shard with the least time so far."""
    weights = {"a.py": 10.0, "b.py": 6.0, "c.py": 5.0, "d.py": 1.0}
    shards = assign_files(["d.py", "c.py", "b.py", "a.py"], weights, 2)
    assert shards == {"a.py": 1, "b.py": 2, "c.py": 2, "d.py": 1}


def test_a_file_without_a_measurement_counts_one_second() -> None:
    """A new test file has no entry yet; it still lands in exactly one shard."""
    shards = assign_files(["new.py", "old.py"], {"old.py": 1.5}, 2)
    assert shards == {"old.py": 1, "new.py": 2}


def test_the_spread_does_not_depend_on_the_order_of_collection() -> None:
    """Every shard and every xdist worker must compute the same answer."""
    files = [f"f{i}.py" for i in range(20)]
    weights = {f: float(i % 7) for i, f in enumerate(files)}
    assert assign_files(files, weights, 3) == assign_files(list(reversed(files)), weights, 3)


def test_the_durations_file_is_read_and_a_missing_one_is_empty(tmp_path: Path) -> None:
    """A missing file spreads the files by count; a present one by seconds."""
    assert read_durations(tmp_path / "missing.json") == {}
    path = tmp_path / "d.json"
    path.write_text(json.dumps({"a.py": 2.5}))
    assert read_durations(path) == {"a.py": 2.5}


@pytest.mark.parametrize("text", ["[1]", '{"a.py": "x"}', "not json", '{"a.py": -1}'])
def test_a_broken_durations_file_names_itself_and_the_fix(tmp_path: Path, text: str) -> None:
    """The file is regenerated by a command, so the message gives it."""
    path = tmp_path / "d.json"
    path.write_text(text)
    with pytest.raises(pytest.UsageError, match=r"d\.json.*time_budgets\.py durations"):
        read_durations(path)


def test_without_a_shard_every_test_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Locally nothing is deselected."""
    monkeypatch.delenv(SHARD_ENV, raising=False)
    monkeypatch.delenv(QUARANTINE_ENV, raising=False)
    items = [_Item("a.py", "t1"), _Item("b.py", "t2")]
    selected, config = _run(items)
    assert selected == items
    assert config.hook.deselected == []


def test_the_shards_together_run_every_test_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each test file is in exactly one shard, whole (``--dist loadfile`` keeps it together)."""
    monkeypatch.delenv(QUARANTINE_ENV, raising=False)
    durations = tmp_path / "d.json"
    durations.write_text(json.dumps({"a.py": 9.0, "b.py": 4.0}))
    items = [_Item(f, f"t{i}") for f in ("a.py", "b.py", "c.py") for i in range(3)]
    seen: list[str] = []
    for index in (1, 2, 3):
        monkeypatch.setenv(SHARD_ENV, f"{index}/3")
        selected, config = _run(items, durations)
        assert {i.path for i in selected}.isdisjoint({i.path for i in config.hook.deselected})
        assert len(selected) + len(config.hook.deselected) == len(items)
        seen.extend(i.nodeid for i in selected)
    assert sorted(seen) == sorted(i.nodeid for i in items)


def test_the_selection_runs_once_when_both_conftest_files_load_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``packages/conftest.py`` and ``scripts/conftest.py`` both register the hook."""
    monkeypatch.setenv(SHARD_ENV, "1/2")
    monkeypatch.delenv(QUARANTINE_ENV, raising=False)
    items = [_Item("a.py", "t"), _Item("b.py", "t")]
    config = _Config()
    selected = list(items)
    pytest_collection_modifyitems(config, selected)  # ty: ignore[invalid-argument-type]
    once = list(selected)
    pytest_collection_modifyitems(config, selected)  # ty: ignore[invalid-argument-type]
    assert selected == once
    assert len(config.hook.deselected) == 1


def test_a_quarantined_test_is_skipped_with_its_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Required jobs skip it, and the skip reason names the issue that tracks the fix."""
    monkeypatch.delenv(SHARD_ENV, raising=False)
    monkeypatch.delenv(QUARANTINE_ENV, raising=False)
    flaky = _Item("a.py", "t", {"quarantine": {"issue": 412}})
    steady = _Item("a.py", "u")
    selected, _ = _run([flaky, steady])
    assert selected == [flaky, steady]
    assert steady.added == []
    (skip,) = flaky.added
    assert skip.name == "skip"
    assert "#412" in skip.kwargs["reason"]
    assert "quarantine" in skip.kwargs["reason"]


def test_the_quarantine_job_runs_only_the_quarantined_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``LAKANA_TEST_QUARANTINED=1`` turns the selection around, and nothing is skipped."""
    monkeypatch.delenv(SHARD_ENV, raising=False)
    monkeypatch.setenv(QUARANTINE_ENV, "1")
    flaky = _Item("a.py", "t", {"quarantine": {"issue": 412}})
    steady = _Item("a.py", "u")
    selected, config = _run([flaky, steady])
    assert selected == [flaky]
    assert config.hook.deselected == [steady]
    assert flaky.added == []


@pytest.mark.parametrize("kwargs", [{}, {"issue": "412"}, {"issue": 0}, {"issue": True}])
def test_a_quarantine_without_an_issue_number_stops_the_run(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, object]
) -> None:
    """The same-day rule: a quarantined test always has an issue for its fix."""
    monkeypatch.delenv(SHARD_ENV, raising=False)
    monkeypatch.delenv(QUARANTINE_ENV, raising=False)
    with pytest.raises(pytest.UsageError, match=r"a\.py::t.*quarantine\(issue=N\)"):
        _run([_Item("a.py", "t", {"quarantine": kwargs})])


def test_a_slow_test_says_so_in_the_junit_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """The slow-test ratchet counts the tests that carry the ``slow`` property."""
    monkeypatch.delenv(SHARD_ENV, raising=False)
    monkeypatch.delenv(QUARANTINE_ENV, raising=False)
    slow = _Item("a.py", "t", {"slow": {}})
    fast = _Item("a.py", "u")
    _run([slow, fast])
    assert slow.user_properties == [("slow", "true")]
    assert fast.user_properties == []


def test_the_default_durations_file_sits_next_to_the_plugin() -> None:
    """``scripts/test-durations.json`` is what CI refreshes with ``time_budgets.py durations``."""
    assert Path(plugin.DURATIONS).name == "test-durations.json"
    assert Path(plugin.DURATIONS).parent == Path(plugin.__file__).parent


def test_both_conftest_files_load_the_selection() -> None:
    """Without the hook in both, a run of one tree would ignore the shard."""
    root = Path(__file__).resolve().parents[2]
    for conftest in ("packages/conftest.py", "scripts/conftest.py"):
        text = (root / conftest).read_text()
        assert "from scripts.pytest_selection import pytest_collection_modifyitems" in text


def test_the_markers_are_registered() -> None:
    """An unregistered marker is a warning, and a typo in it would go unnoticed."""
    import tomllib

    root = Path(__file__).resolve().parents[2]
    markers = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"][
        "markers"
    ]
    names = {m.split(":")[0].split("(")[0] for m in markers}
    assert {"slow", "quarantine"} <= names


def test_a_test_file_outside_the_root_is_named_by_its_full_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file pytest collects from elsewhere still lands in exactly one shard."""
    monkeypatch.setenv(SHARD_ENV, "1/1")
    monkeypatch.delenv(QUARANTINE_ENV, raising=False)
    item = _Item("a.py", "t")
    item.path = Path("/elsewhere/a.py")
    selected, _ = _run([item])
    assert selected == [item]
