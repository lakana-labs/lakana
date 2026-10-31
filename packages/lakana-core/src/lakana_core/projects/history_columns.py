"""The columns `write: history` adds to a table, and the texts that name them.

A leaf module: the deploy's checks (`projects`) and the write (`catalog.history`) both need these
names, and `projects` may not import `catalog.history` (its package imports `catalog.service`,
which imports `projects`).
"""

from __future__ import annotations

VALID_FROM = "valid_from"
"""The column holding when a version became valid (UTC)."""

VALID_TO = "valid_to"
"""The column holding when a version stopped being valid (UTC); NULL for the open version."""

IS_CURRENT = "is_current"
"""The column saying whether a version is its key's current one."""


def surrogate_column(table: str) -> str:
    """Name a history table's surrogate key column.

    Parameters
    ----------
    table
        The table's dotted name.

    Returns
    -------
    str
        The last part of the name followed by ``_key``: `gold.dim_customer` → `dim_customer_key`.
    """
    return f"{table.rsplit('.', 1)[-1]}_key"


def added_columns(table: str) -> tuple[str, str, str, str]:
    """Name the four columns a history write adds, in table order.

    Parameters
    ----------
    table
        The table's dotted name.

    Returns
    -------
    tuple of str
        The surrogate key, `VALID_FROM`, `VALID_TO` and `IS_CURRENT`.
    """
    return (surrogate_column(table), VALID_FROM, VALID_TO, IS_CURRENT)


def reserved_name_message(column: str, table: str) -> str:
    """Refuse a result column whose name a history write adds itself.

    Parameters
    ----------
    column
        The column, as the result spells it.
    table
        The table's dotted name.

    Returns
    -------
    str
        The message, naming the four names and a rename to write.
    """
    listed = ", ".join((VALID_FROM, VALID_TO, IS_CURRENT, surrogate_column(table)))
    return (
        f"Column `{column}` of `{table}`'s SELECT has a name `write: history` adds itself "
        f"({listed}); rename it in the SELECT, for example `AS source_{column}`."
    )


def valid_from_column_message(column: str, table: str) -> str:
    """Refuse a `valid_from:` column the result does not return.

    Parameters
    ----------
    column
        The column `valid_from:` names.
    table
        The table's dotted name.

    Returns
    -------
    str
        The message.
    """
    return (
        f"Column `{column}` in `valid_from:` is not a column of `{table}`'s SELECT; add it to the "
        "SELECT, or change `valid_from:`."
    )


def valid_from_key_message(column: str) -> str:
    """Refuse a `valid_from:` that names a key column.

    Parameters
    ----------
    column
        The column.

    Returns
    -------
    str
        The message.
    """
    return (
        f"`valid_from: {column}` names a key column; name the column that says when the row's "
        "values took effect, or leave `valid_from:` out to use the run's start time"
    )
