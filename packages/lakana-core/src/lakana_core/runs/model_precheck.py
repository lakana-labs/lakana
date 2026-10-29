"""The pre-check of a check generated from an asset's header: its columns and their types.

A deploy has no output types, so the rules that need them are checked when the table is written:
the worker calls `model_problem` right before it counts a generated check, and a text it returns
makes that check ``errored`` (so `block` sets the table back) without counting it.

- The pre-check reads names and types from `table_columns` (`stored_columns`), never a value of
  a table.
- A reference's text never names its target, never names a type, and never says whether the
  target exists: a reader of the run may not be able to read the target. A target that is
  missing, one the run's principal may not read and one that no longer has the column get one
  text (`REFERENCE_UNCHECKABLE`); both reference texts point at the check's path, the asset's
  header line, which the reader sees already.
- Each text is a module constant, filled with the header's own column names, the checked table's
  name, the check's path and the checked table's own column type. A column name that matches
  only in case adds `CASE_HINT`, naming the checked table's own column.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.catalog.columns import stored_columns
from lakana_core.errors import ValidationError
from lakana_core.identifiers import TableRef
from lakana_core.projects.checks import CheckSpec, GeneratedFrom

FLOAT_TYPES = frozenset({"float", "double"})
"""The catalog's spellings of the floating-point types, which cannot identify a row."""

INTEGER_TYPES = frozenset({"int", "long"})
"""The catalog's spellings of the integer types; a reference may join one width to the other."""

KEY_FLOAT = (
    "Key column `{column}` is {type}; a floating-point column cannot identify a row. "
    "Use an integer, decimal, string or date column."
)
"""A key column of a floating-point type."""

KEY_MISSING = (
    "Key column `{column}` is not a column of `{table}`; fix `key:` in {path} and deploy again."
)
"""A key column the written table does not have."""

REFERENCE_MISSING = (
    "Reference column `{column}` is not a column of `{table}`; fix `references:` in {path} and "
    "deploy again."
)
"""A referencing column the written table does not have."""

CASE_HINT = " Column names match exactly; the table has `{actual}`."
"""Added to `KEY_MISSING` or `REFERENCE_MISSING` when the checked table has the column in another
case. The table is the asset's own, which the run's principal has just written."""

REFERENCE_UNCHECKABLE = (
    "The reference `{column}` at `{path}` cannot be checked: its target is missing, not readable "
    "by the run's principal, or no longer has the key column; fix the reference or the grant and "
    "deploy again."
)
"""A reference target that is missing, unreadable, or without the referenced column: one text."""

TYPE_MISMATCH = (
    "The reference `{column}` at `{path}` has a type that differs from its target key column's; "
    "make the types match and deploy again."
)
"""A referencing column whose type differs from the target column's; names neither type."""


def types_match(own: str, other: str) -> bool:
    """Say whether a reference column's type matches its target's.

    Parameters
    ----------
    own
        The referencing column's type, as `table_columns` spells it.
    other
        The target column's type.

    Returns
    -------
    bool
        True when equal, or when both are integers of any width.
    """
    return own == other or (own in INTEGER_TYPES and other in INTEGER_TYPES)


def model_problem(
    session: Session,
    authz: AuthorizationService,
    principal: PrincipalRef,
    environment: str,
    spec: CheckSpec,
) -> str | None:
    """Say why a generated check cannot be counted, from the stored columns of its tables.

    Parameters
    ----------
    session
        Database session; only reads.
    authz
        The authorization service that decides whether the run's principal may read a
        reference target.
    principal
        The run's principal: who the check would be counted as.
    environment
        The run's environment.
    spec
        The check.

    Returns
    -------
    str or None
        The fixed text of the first problem found, or `None` when there is none: also for a
        hand-written check, for a checked table that is gone or whose columns are not stored yet
        (the count reports what it finds then).
    """
    generated = spec.generated
    if generated is None:
        return None
    own = stored_columns(session, environment, spec.table)
    if not own:
        return None
    if generated.family == "key":
        return _key_problem(own, generated, spec)
    return _reference_problem(session, authz, principal, environment, own, generated, spec)


def _key_problem(own: dict[str, str], generated: GeneratedFrom, spec: CheckSpec) -> str | None:
    """Check a key's columns: each one there, none of a floating-point type.

    Parameters
    ----------
    own
        The checked table's columns and their types.
    generated
        The key the check comes from.
    spec
        The check.

    Returns
    -------
    str or None
        `KEY_MISSING` or `KEY_FLOAT` for the first key column with a problem, else `None`.
    """
    for column in generated.columns:
        type_ = own.get(column)
        if type_ is None:
            text = KEY_MISSING.format(column=column, table=spec.table, path=spec.path)
            return text + _case_hint(own, column)
        if type_ in FLOAT_TYPES:
            return KEY_FLOAT.format(column=column, type=type_)
    return None


def _reference_problem(
    session: Session,
    authz: AuthorizationService,
    principal: PrincipalRef,
    environment: str,
    own: dict[str, str],
    generated: GeneratedFrom,
    spec: CheckSpec,
) -> str | None:
    """Check a reference: its column, its target, the target's column and the two types.

    Parameters
    ----------
    session
        Database session.
    authz
        Decides whether the run's principal may read the target.
    principal
        The run's principal.
    environment
        The run's environment.
    own
        The checked table's columns and their types.
    generated
        The reference the check comes from.
    spec
        The check.

    Returns
    -------
    str or None
        The text of the first problem, else `None`. The target is read only after the run's
        principal is found to be allowed to read it.
    """
    (column,) = generated.columns
    target, target_column = generated.target or "", generated.target_column or ""
    if column not in own:
        text = REFERENCE_MISSING.format(column=column, table=spec.table, path=spec.path)
        return text + _case_hint(own, column)
    gone = REFERENCE_UNCHECKABLE.format(column=column, path=spec.path)
    try:
        ref = TableRef.from_dotted(environment, target)
    except ValidationError:
        return gone
    if not authz.permits(
        principal, Action.READ_TABLE, Securable.table_scope(environment, ref.namespace, ref.name)
    ):
        return gone
    theirs = stored_columns(session, environment, target)
    if theirs is None:
        return gone
    if target_column not in theirs:
        if not theirs:
            return None
        return gone
    if not types_match(own[column], theirs[target_column]):
        return TYPE_MISMATCH.format(column=column, path=spec.path)
    return None


def _case_hint(own: dict[str, str], column: str) -> str:
    """Name the checked table's column that matches `column` in all but case.

    Parameters
    ----------
    own
        The checked table's columns, in schema order.
    column
        The header's column name, which the table does not have exactly.

    Returns
    -------
    str
        `CASE_HINT` for the first such column, or an empty string when there is none.
    """
    folded = column.casefold()
    for name in own:
        if name.casefold() == folded:
            return CASE_HINT.format(actual=name)
    return ""
