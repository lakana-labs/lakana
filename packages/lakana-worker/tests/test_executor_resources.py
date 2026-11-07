"""A query child's figures reach the usage collector that is current when it ends."""

from __future__ import annotations

import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from lakana_core.runs.resources import AttemptResources
from lakana_worker.executor import execute_query
from lakana_worker.resources import current_collector, usage_collector


def _read(tmp_path: Path) -> pa.Table:
    """Read a small Parquet file through `execute_query`.

    Parameters
    ----------
    tmp_path
        Where the file is written.

    Returns
    -------
    pa.Table
        The result.
    """
    probe = tmp_path / "probe.parquet"
    pq.write_table(pa.table({"a": list(range(1000))}), probe)
    return execute_query(
        f"SELECT * FROM read_parquet('{probe}')", read_prefixes=(f"{tmp_path.resolve()}/",)
    )


def test_a_read_leaves_its_figures_in_the_collector(tmp_path: Path) -> None:
    with usage_collector() as collector:
        _read(tmp_path)
    figures = collector.resources
    assert figures.cpu_seconds is not None and figures.cpu_seconds > 0
    if sys.platform.startswith("linux"):
        assert figures.peak_memory_bytes is not None and figures.peak_memory_bytes > 0
    assert figures.bytes_read is not None and figures.bytes_read > 0


def test_outside_a_collector_nothing_is_stored_and_nothing_raises(tmp_path: Path) -> None:
    assert current_collector() is None
    assert _read(tmp_path).num_rows == 1000
    assert current_collector() is None


def test_a_query_that_fails_still_leaves_its_figures() -> None:
    with usage_collector() as collector, pytest.raises(Exception, match="nope"):
        execute_query("SELECT error('nope')")
    assert collector.resources.cpu_seconds is not None


def test_two_children_merge_in_one_collector() -> None:
    with usage_collector() as collector:
        collector.add(AttemptResources(100, 1.0, 10))
        collector.add(AttemptResources(300, 0.5, None))
    assert collector.resources == AttemptResources(300, 1.5, 10)
