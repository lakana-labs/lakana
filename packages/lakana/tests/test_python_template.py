"""The Python asset `lakana init --template python` writes, run on the sample's rows."""

from __future__ import annotations

import datetime as dt
from types import ModuleType
from typing import Any

import polars as pl

from lakana.assets import ASSET_ATTRIBUTE
from lakana.cli.project_init import PYTHON_TEMPLATE

_PATH = "assets/gold/temperature_anomalies.py"


def _asset() -> Any:  # ruff: ignore[any-type]
    """Execute the template's asset file and return its function.

    Returns
    -------
    Any
        The decorated asset function.
    """
    module = ModuleType("template_asset")
    exec(PYTHON_TEMPLATE[_PATH], module.__dict__)  # nosec B102
    return module.__dict__["temperature_anomalies"]


def _readings() -> pl.DataFrame:
    """Build the rows of the sample's `bronze.readings`, as its SQL computes them.

    Returns
    -------
    polars.DataFrame
        Eighteen readings: three stations, six days each.
    """
    rows = [
        (station, dt.date(2026, 1, 1) + dt.timedelta(days=n), base + (n % 3) - 1)
        for station, base in (("A1", 4.0), ("B2", 1.0), ("C3", 9.0))
        for n in range(6)
    ]
    return pl.DataFrame(rows, schema=["station_id", "day", "temp_c"], orient="row")


def test_the_asset_declares_two_tables_of_the_sample_as_inputs() -> None:
    """It builds from the SQL sample: the readings and the per-station averages."""
    spec = getattr(_asset(), ASSET_ATTRIBUTE)

    assert dict(spec.inputs) == {
        "readings": "bronze.readings",
        "stations": "silver.station_temperatures",
    }
    assert set(spec.columns) == {"station_id", "day", "temp_c", "anomaly_c"}


def test_each_reading_gets_its_distance_from_the_station_average() -> None:
    """The sample's averages are 4, 1 and 9 degrees: every station swings one degree around it."""
    readings = _readings()
    stations = readings.group_by("station_id").agg(
        readings=pl.len(),
        min_temp_c=pl.col("temp_c").min(),
        max_temp_c=pl.col("temp_c").max(),
        avg_temp_c=pl.col("temp_c").mean(),
    )

    result = _asset()(readings=readings, stations=stations)

    assert result.columns == ["station_id", "day", "temp_c", "anomaly_c"]
    assert result.height == 18
    b2 = result.filter(pl.col("station_id") == "B2")
    assert b2["temp_c"].to_list() == [0.0, 1.0, 2.0, 0.0, 1.0, 2.0]
    assert b2["anomaly_c"].to_list() == [-1.0, 0.0, 1.0, -1.0, 0.0, 1.0]
    assert result["station_id"].to_list() == sorted(result["station_id"].to_list())
