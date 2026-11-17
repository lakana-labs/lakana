"""Tests for column-level lineage extraction."""

import pytest
import sqlglot.expressions as exp
import sqlglot.lineage

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


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM (SELECT a FROM bronze.x UNION ALL SELECT id FROM bronze.y) AS s",
        "WITH t AS (SELECT a FROM bronze.x UNION ALL SELECT id FROM bronze.y) SELECT id FROM t",
    ],
)
def test_a_union_whose_branches_name_a_column_differently_is_untraceable(sql: str) -> None:
    """The query still runs although sqlglot raises `ValueError` ("Could not find id in ...").

    Found by the query guard's fuzz harness: the error failed the whole query run.

    Parameters
    ----------
    sql
        A query reading a union whose first branch names the column `a`, the second `id`.
    """
    assert extract_lineage(sql) == {"id": []}


@pytest.mark.parametrize("error", [ValueError("something else"), UnicodeError("bad text")])
def test_any_other_value_error_from_sqlglot_s_lineage_still_escapes(
    error: ValueError, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only "Could not find ..." means an untraceable column; another `ValueError` is a bug.

    Parameters
    ----------
    error
        What the stand-in for `sqlglot.lineage.lineage` raises.
    monkeypatch
        Replaces `sqlglot.lineage.lineage`.
    """

    def failing(*_args: object, **_kwargs: object) -> sqlglot.lineage.Node:
        """Raise the error.

        Parameters
        ----------
        *_args
            Ignored.
        **_kwargs
            Ignored.

        Raises
        ------
        ValueError
            Always, `error`.
        """
        raise error

    monkeypatch.setattr(sqlglot.lineage, "lineage", failing)
    with pytest.raises(type(error)):
        extract_lineage("SELECT id FROM bronze.weather")


def test_a_bare_values_list_has_no_traceable_column() -> None:
    """A `VALUES` list reads no table, so its columns trace to nothing."""
    assert extract_lineage("VALUES (1, 'a')") == {}


def test_lineage_traces_the_tree_lakanas_parser_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sqlglot's lineage gets the tree `sql_dialect` parsed, never the text to parse again (#415).

    Parameters
    ----------
    monkeypatch
        Replaces `sqlglot.lineage.lineage` with a recorder that calls the original.
    """
    seen: list[object] = []
    original = sqlglot.lineage.lineage

    def recording(
        column: str, sql: str | exp.Expr, schema: dict[str, object], dialect: str
    ) -> sqlglot.lineage.Node:
        seen.append(sql)
        return original(column, sql, schema=schema, dialect=dialect)

    monkeypatch.setattr(sqlglot.lineage, "lineage", recording)

    result = extract_lineage("SELECT a ~ 'b' AS m FROM bronze.t")

    assert result == {"m": [{"table": "bronze.t", "column": "a"}]}
    assert seen
    assert all(isinstance(sql, exp.Expr) for sql in seen)
    assert all(
        isinstance(sql, exp.Expr) and sql.find(exp.RegexpFullMatch) is not None for sql in seen
    )
