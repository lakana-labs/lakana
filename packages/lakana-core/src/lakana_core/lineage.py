"""Column-level lineage extraction from submitted SQL.

Lineage for one query only — this is not a persisted, cross-query lineage
graph. That is M5's problem, once assets and project versions exist to own it.
"""

from __future__ import annotations

from typing import cast

import sqlglot
import sqlglot.errors
import sqlglot.lineage

from lakana_core.query_rewrite import _dotted_table_name


def extract_lineage(sql: str, dialect: str = "duckdb") -> dict[str, list[dict[str, str]]]:
    """Trace each of a query's output columns back to its source table and column.

    Parameters
    ----------
    sql
        The query to analyse.
    dialect
        The SQL dialect to parse `sql` as.

    Returns
    -------
    dict
        Maps each output column name to a list of `{"table": ..., "column": ...}`
        entries — more than one when the column derives from more than one
        source column (for example a `COALESCE`). A computed column with no
        traceable source (a literal, an aggregate with no single source
        column) maps to an empty list.
    """
    # parse_one's declared return type is the base Expression; .selects is
    # only defined on the Query subtype, which a SELECT statement always
    # parses to. The cast tells the type checker what sqlglot guarantees at
    # runtime but does not encode in parse_one's own signature.
    parsed = cast("sqlglot.expressions.Query", sqlglot.parse_one(sql, dialect=dialect))
    result: dict[str, list[dict[str, str]]] = {}
    for projection in parsed.selects:
        column_name = projection.alias_or_name
        try:
            # No schema is known here. sqlglot 30.22 made the argument required; an empty
            # mapping is what its earlier default (None) meant.
            node = sqlglot.lineage.lineage(column_name, sql, schema={}, dialect=dialect)
        except sqlglot.errors.SqlglotError:
            # An unaliased computed projection has no name to trace:
            # `SELECT COUNT(*) FROM t` names the column `*` and
            # `SELECT a * 2 FROM t` names it `''`, and sqlglot's optimizer
            # raises `Cannot find column ... in query` for both. That is the
            # "no traceable source" case this function documents, not a bug,
            # so the column maps to an empty list and the remaining columns
            # are still traced. Verified against sqlglot 30.18.0.
            result[column_name] = []
            continue
        sources = []
        for leaf in node.walk():
            if leaf.downstream == [] and isinstance(leaf.source, sqlglot.expressions.Table):
                sources.append(
                    {
                        "table": _dotted_table_name(leaf.source),
                        "column": leaf.name.rsplit(".", 1)[-1],
                    }
                )
        result[column_name] = sources
    return result
