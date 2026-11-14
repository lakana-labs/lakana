"""Tests for column-level lineage extraction."""

import pytest

from lakana_core.lineage import extract_lineage


def test_extracts_direct_column_lineage() -> None:
    """A simple projection traces each output column to its one source."""
    lineage = extract_lineage("SELECT id, temp_c FROM bronze.weather")

    assert lineage == {
        "id": [{"table": "bronze.weather", "column": "id"}],
        "temp_c": [{"table": "bronze.weather", "column": "temp_c"}],
    }


def test_extracts_lineage_across_a_join() -> None:
    """A joined output column traces back through both source tables."""
    lineage = extract_lineage(
        "SELECT a.id, b.name FROM bronze.a AS a JOIN bronze.b AS b ON a.id = b.id"
    )

    assert lineage["id"] == [{"table": "bronze.a", "column": "id"}]
    assert lineage["name"] == [{"table": "bronze.b", "column": "name"}]


@pytest.mark.parametrize(
    "source",
    ['"bronze"."raw"."eu"."orders"', "bronze.raw.eu.orders AS o", "bronze.raw.eu.orders"],
)
def test_a_table_in_a_three_level_namespace_keeps_every_part(source: str) -> None:
    """`bronze.raw.eu.orders` is the table `orders` of `bronze.raw.eu`, never `bronze.raw.orders`.

    Parameters
    ----------
    source
        The table as the query names it.
    """
    lineage = extract_lineage(f"SELECT id FROM {source}")

    assert lineage == {"id": [{"table": "bronze.raw.eu.orders", "column": "id"}]}


@pytest.mark.parametrize(
    ("sql", "column"),
    [
        ("SELECT COUNT(*) FROM bronze.weather", "*"),
        ("SELECT SUM(temp_c) FROM bronze.weather", ""),
        ("SELECT temp_c * 2 FROM bronze.weather", ""),
        ("SELECT UPPER(name) FROM bronze.weather", ""),
        ("SELECT CASE WHEN temp_c > 0 THEN 1 ELSE 0 END FROM bronze.weather", ""),
        ("SELECT 1 FROM bronze.weather", "1"),
    ],
)
def test_an_untraceable_column_maps_to_an_empty_list(sql: str, column: str) -> None:
    """A computed projection sqlglot cannot trace yields [], never an exception.

    Parameters
    ----------
    sql
        A query whose only output column has no traceable source column.
    column
        The name sqlglot gives that unaliased projection.
    """
    assert extract_lineage(sql) == {column: []}


def test_a_traceable_column_survives_an_untraceable_sibling() -> None:
    """One uncomputable column does not cost the other columns their lineage."""
    lineage = extract_lineage("SELECT category, COUNT(*) FROM bronze.weather GROUP BY category")

    assert lineage == {
        "category": [{"table": "bronze.weather", "column": "category"}],
        "*": [],
    }


def test_a_bare_values_list_has_no_traceable_column() -> None:
    """A `VALUES` list reads no table, so its columns trace to nothing."""
    assert extract_lineage("VALUES (1, 'a')") == {}
