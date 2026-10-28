"""How far each reading lies from its station's average: a Python asset built from the sample."""

import polars as pl

from lakana import asset


@asset(
    inputs={"readings": "bronze.readings", "stations": "silver.station_temperatures"},
    columns={
        "station_id": "The station that took the reading.",
        "day": "The day of the reading.",
        "temp_c": "The temperature in degrees Celsius.",
        "anomaly_c": "How many degrees the reading lies above (positive) or below (negative) "
        "the station's average.",
    },
)
def temperature_anomalies(readings: pl.DataFrame, stations: pl.DataFrame) -> pl.DataFrame:
    """Each reading, and how much warmer or colder than its station's average it was."""
    averages = stations.select("station_id", "avg_temp_c")
    return (
        readings.join(averages, on="station_id")
        .with_columns(anomaly_c=pl.col("temp_c") - pl.col("avg_temp_c"))
        .select("station_id", "day", "temp_c", "anomaly_c")
        .sort("station_id", "day")
    )
