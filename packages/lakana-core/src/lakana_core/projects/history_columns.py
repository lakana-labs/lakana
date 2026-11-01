"""The columns `write: history` adds to a table, and the texts that name them.

A leaf module: the deploy's checks (`projects`) and the write (`catalog.history`) both need these
names, and `projects` may not import `catalog.history` (its package imports `catalog.service`,
which imports `projects`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

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


def reserved_name_message(column: str, table: str, *, valid_from: str | None = None) -> str:
    """Refuse a result column whose name a history write adds itself.

    Parameters
    ----------
    column
        The column, as the result spells it.
    table
        The table's dotted name.
    valid_from
        The `valid_from:` column; when it is `column`, the message says to rename it there too.

    Returns
    -------
    str
        The message, naming the four names and a rename to write.
    """
    listed = ", ".join((VALID_FROM, VALID_TO, IS_CURRENT, surrogate_column(table)))
    also = (
        f", and name the new column in `valid_from:` too (`valid_from: source_{column}`)"
        if valid_from == column
        else ""
    )
    return (
        f"Column `{column}` of `{table}`'s SELECT has a name `write: history` adds itself "
        f"({listed}); rename it in the SELECT, for example `AS source_{column}`{also}."
    )


def valid_from_column_message(column: str, table: str, *, spelled: str | None = None) -> str:
    """Refuse a `valid_from:` column the result does not return.

    Parameters
    ----------
    column
        The column `valid_from:` names.
    table
        The table's dotted name.
    spelled
        The result's column of the same name in another letter case, if there is one: the run
        reads the result's names exactly, so the message says how to make them match.

    Returns
    -------
    str
        The message.
    """
    if spelled is not None:
        return (
            f"Column `{column}` in `valid_from:` is not a column of `{table}`'s SELECT, which "
            f"spells it `{spelled}`; write `valid_from: {spelled}`, or rename the column in the "
            f"SELECT to `{column}`."
        )
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


@dataclass(frozen=True)
class HistorySpec:
    """How a history asset's run writes its table.

    Parameters
    ----------
    key
        The natural key's columns.
    valid_from
        The column a version's start is read from; `None` for the run's start time.
    close_missing
        Whether the open version of a key the result lacks is closed.
    """

    key: tuple[str, ...]
    valid_from: str | None
    close_missing: bool

    def to_json(self) -> dict[str, object]:
        """Write the spec as `QueryRun.history` stores it.

        Returns
        -------
        dict
            ``key``, ``valid_from`` and ``close_missing``.
        """
        return {
            "key": list(self.key),
            "valid_from": self.valid_from,
            "close_missing": self.close_missing,
        }

    @staticmethod
    def from_json(raw: object) -> HistorySpec | None:
        """Read a stored spec; anything a deploy or the worker did not write reads as none.

        Parameters
        ----------
        raw
            The stored value; untrusted.

        Returns
        -------
        HistorySpec or None
            The spec, or `None` unless ``key`` is a non-empty list of non-empty strings,
            ``valid_from`` a string or null, and ``close_missing`` a boolean.
        """
        if not isinstance(raw, Mapping):
            return None
        key, valid_from, close = raw.get("key"), raw.get("valid_from"), raw.get("close_missing")
        if not isinstance(key, list) or not key:
            return None
        if not all(isinstance(column, str) and column for column in key):
            return None
        if valid_from is not None and not isinstance(valid_from, str):
            return None
        if not isinstance(close, bool):
            return None
        return HistorySpec(tuple(key), valid_from, close)


def added_column_descriptions(table: str, key: Sequence[str]) -> dict[str, str]:
    """Write the fixed English descriptions of the columns a history table adds.

    Parameters
    ----------
    table
        The table's dotted name.
    key
        The natural key's columns, named in the surrogate key's description.

    Returns
    -------
    dict
        Column → description, in table order.
    """
    named = ", ".join(f"`{column}`" for column in key)
    return {
        surrogate_column(table): (
            f"Identifies this version: a hash of the key ({named}) and `valid_from`. A fact can "
            "store it to point at exactly this version."
        ),
        VALID_FROM: "When this version became valid (UTC).",
        VALID_TO: (
            "When this version stopped being valid (UTC); empty while it is the current version."
        ),
        IS_CURRENT: "Whether this is the current version of its key.",
    }
