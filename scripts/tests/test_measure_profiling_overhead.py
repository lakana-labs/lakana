"""Tests for scripts/measure_profiling_overhead.py: the arithmetic and the statements."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import duckdb
import pytest

from lakana_worker.query_child import profiling_statements
from scripts import measure_profiling_overhead as overhead


def _clock(durations: list[float]) -> Callable[[], float]:
    """Make a timer whose successive start/end pairs differ by the given durations.

    Parameters
    ----------
    durations
        The duration of each timed run, in call order.

    Returns
    -------
    Callable
        A timer to inject.
    """
    queue = list(durations)
    state = {"now": 0.0, "open": False}

    def timer() -> float:
        if state["open"]:
            state["now"] += queue.pop(0)
        state["open"] = not state["open"]
        return state["now"]

    return timer


def test_the_percent_difference_is_relative_to_the_unprofiled_time() -> None:
    assert overhead.percent_difference(2.0, 2.1) == pytest.approx(5.0)
    assert overhead.percent_difference(2.0, 1.9) == pytest.approx(-5.0)


def test_the_decision_uses_the_median_of_the_differences() -> None:
    assert overhead.decide([0.5, 1.0, 50.0])
    assert overhead.decide([2.0, 2.0, 2.0])
    assert not overhead.decide([2.5, 3.0, 0.0])


def test_the_runs_alternate_and_each_connection_gets_the_durations_scripted_for_it() -> None:
    plain, profiled = duckdb.connect(), duckdb.connect()
    without, with_profiling = overhead.time_runs(
        plain, profiled, "SELECT 1", 2, _clock([1.0, 2.0, 3.0, 4.0])
    )
    assert (without, with_profiling) == ([1.0, 3.0], [2.0, 4.0])


def test_the_profiling_statements_track_only_the_bytes_read(tmp_path: Path) -> None:
    connection = duckdb.connect()
    for statement in profiling_statements(str(tmp_path / "profile.json")):
        connection.execute(statement)
    (tracked,) = connection.execute(
        "SELECT value FROM duckdb_settings() WHERE name = 'tracked_metrics'"
    ).fetchone() or ("",)
    assert tracked == "[io.total_bytes_read]"


@pytest.mark.parametrize(("slowdown", "status"), [(1.01, 0), (1.05, 1)])
def test_main_exits_1_only_when_profiling_costs_more_than_the_limit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], slowdown: float, status: int
) -> None:
    runs = 3
    durations = [1.0, slowdown] * runs * len(overhead.QUERIES)
    assert overhead.main(rows=100, runs=runs, timer=_clock(durations), directory=tmp_path) == status
    out = capsys.readouterr().out
    assert ("TOO SLOW" in out) == bool(status)
    assert all(name in out for name in overhead.QUERIES)


def test_the_parquet_file_is_built_once(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "rows.parquet"
    overhead.build(path, 10)
    stamp = path.stat().st_mtime_ns
    overhead.build(path, 10)
    assert path.stat().st_mtime_ns == stamp
