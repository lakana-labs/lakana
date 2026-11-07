"""Measure what DuckDB's profiling costs the query child (manual; not run in CI).

The query child switches profiling on to count the bytes a statement read
(`lakana_worker.query_child.configure`). This script times three queries over a Parquet file with
and without those statements, alternating the two so that drift hits both, and exits 1 when the
median of the three differences is above `LIMIT_PERCENT`. Run it after a DuckDB upgrade::

    uv run --frozen python scripts/measure_profiling_overhead.py
"""

from __future__ import annotations

import statistics
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import duckdb

from lakana_worker.query_child import profiling_statements

LIMIT_PERCENT = 2.0
"""The largest median difference, in percent, that is accepted."""
ROWS = 30_000_000
"""Rows of the Parquet file the real measurement reads."""
RUNS = 15
"""Timed runs per query and configuration."""
CACHE = Path.home() / ".cache" / "m282"
"""Where the Parquet file is kept between runs."""
QUERIES: dict[str, str] = {
    "group-by": "SELECT k, sum(v), count(*) FROM read_parquet('{path}') GROUP BY k",
    "filter-aggregate": "SELECT sum(v) FROM read_parquet('{path}') WHERE v % 7 = 0",
    "limit": "SELECT * FROM read_parquet('{path}') LIMIT 1000",
}
"""The benchmark queries; `{path}` is the Parquet file."""


def percent_difference(without: float, with_profiling: float) -> float:
    """Say how much slower the profiled runs are, in percent.

    Parameters
    ----------
    without
        The median time without profiling.
    with_profiling
        The median time with it.

    Returns
    -------
    float
        ``(with - without) / without * 100``; negative when profiling was faster.
    """
    return (with_profiling - without) / without * 100.0


def time_runs(
    plain: duckdb.DuckDBPyConnection,
    profiled: duckdb.DuckDBPyConnection,
    sql: str,
    runs: int,
    timer: Callable[[], float],
) -> tuple[list[float], list[float]]:
    """Time a query alternately on both connections, after one untimed run on each.

    Parameters
    ----------
    plain
        The connection without profiling.
    profiled
        The connection with the child's profiling statements.
    sql
        The query.
    runs
        Timed runs per connection.
    timer
        Gives the time in seconds; called twice per run, before and after.

    Returns
    -------
    tuple of (list of float, list of float)
        The durations in seconds without and with profiling.
    """
    for connection in (plain, profiled):
        connection.execute(sql).fetchall()
    without: list[float] = []
    with_profiling: list[float] = []
    for _ in range(runs):
        for connection, durations in ((plain, without), (profiled, with_profiling)):
            start = timer()
            connection.execute(sql).fetchall()
            durations.append(timer() - start)
    return without, with_profiling


def decide(differences: Sequence[float]) -> bool:
    """Say whether the median difference is within the limit.

    Parameters
    ----------
    differences
        The percent difference of each query.

    Returns
    -------
    bool
        `True` if the median is at most `LIMIT_PERCENT`.
    """
    return statistics.median(differences) <= LIMIT_PERCENT


def build(path: Path, rows: int) -> None:
    """Write the Parquet file the queries read, unless it exists.

    Parameters
    ----------
    path
        The file.
    rows
        Its number of rows.
    """
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    duckdb.sql(
        f"COPY (SELECT i % 1000 AS k, i AS v, repeat('x', 8) AS s FROM range({rows}) AS t(i)) "
        f"TO '{path}' (FORMAT parquet)"
    )


def main(
    rows: int = ROWS,
    runs: int = RUNS,
    timer: Callable[[], float] = time.perf_counter,
    directory: Path = CACHE,
) -> int:
    """Measure, print one line per query and the verdict.

    Parameters
    ----------
    rows
        Rows of the Parquet file.
    runs
        Timed runs per query and configuration.
    timer
        Gives the time in seconds.
    directory
        Where the Parquet file and the profiler's output go.

    Returns
    -------
    int
        0 if the median difference is within `LIMIT_PERCENT`, else 1.
    """
    path = directory / f"rows-{rows}.parquet"
    build(path, rows)
    differences: list[float] = []
    print(f"{'query':<18}{'without ms':>12}{'with ms':>12}{'difference':>12}")
    for name, template in QUERIES.items():
        plain, profiled = duckdb.connect(), duckdb.connect()
        for statement in profiling_statements(str(directory / "profile.json")):
            profiled.execute(statement)
        without, with_profiling = time_runs(
            plain, profiled, template.format(path=path), runs, timer
        )
        plain.close()
        profiled.close()
        base, profiling = statistics.median(without), statistics.median(with_profiling)
        differences.append(percent_difference(base, profiling))
        print(f"{name:<18}{base * 1000:>12.1f}{profiling * 1000:>12.1f}{differences[-1]:>11.2f}%")
    within = decide(differences)
    median = statistics.median(differences)
    verdict = "ok" if within else "TOO SLOW"
    print(f"median difference {median:.2f}% (limit {LIMIT_PERCENT}%): {verdict}")
    return 0 if within else 1


if __name__ == "__main__":  # pragma: no cover -- the real measurement
    sys.exit(main())
