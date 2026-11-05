"""Resolving the row filters and column masks that apply to a (principal, table) pair."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import sqlglot
import sqlglot.expressions as exp
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.errors import ValidationError
from lakana_core.identifiers import TableRef
from lakana_core.models import ColumnMask, Group, GroupMembership, RowFilter
from lakana_core.policy.expressions import HAS_GROUP, columns_of
from lakana_core.policy.field_binding import (
    FIELD_IDS,
    UNREADABLE_POLICY_MESSAGE,
    PolicySchema,
    bound_field_ids,
    resolve_expression,
)
from lakana_core.policy.renames import PolicyColumns
from lakana_core.sql_syntax import SQL_SYNTAX_ERRORS


@dataclass(frozen=True)
class ResolvedPolicy:
    """The row filters and column masks that apply to one (principal, table) pair.

    Parameters
    ----------
    row_filters
        Every applicable filter's predicate, already substituted for the
        requesting principal — the caller (`query_rewrite.rewrite_query`)
        ANDs them together.
    column_masks
        Column name to its applicable, already-substituted mask expression.
        A column with no entry has no mask.
    policies
        One entry per applicable filter and mask that is not bound to field ids (`pending`,
        `unbound`, or a raw table's `names`), with the columns it uses and the schema it was
        written against: these keep the rename rule of `policy.renames`. Unlike
        `column_masks`, two masks on a column that collapse to `NULL` are still two entries,
        so the check for a rename sees both.
    typed_masks
        The columns whose applicable masks were all checked against the column's type when they
        were created (`ColumnMask.type_checked`). Only these are cast to the column's type on a
        read, the `NULL` of two masks on one column included; a mask of an earlier release is
        used as written.
    gone
        With a schema: the first (without regard to case) name, as written, of a column a
        bound policy uses whose field the table no longer has; the read is refused. `None`
        when every bound policy resolved, and always without a schema.
    masked_field_ids
        The field id of the column of every applicable mask bound to field ids.
    legacy_masked
        The casefolded column of every other applicable mask.
    """

    row_filters: list[str] = field(default_factory=list)
    column_masks: dict[str, str] = field(default_factory=dict)
    policies: list[PolicyColumns] = field(default_factory=list)
    typed_masks: frozenset[str] = frozenset()
    gone: str | None = None
    masked_field_ids: frozenset[int] = frozenset()
    legacy_masked: frozenset[str] = frozenset()


def _by_case(name: str) -> tuple[str, str]:
    """Order names without regard to case, ties by the name itself.

    Parameters
    ----------
    name
        A column name.

    Returns
    -------
    tuple of str
        The sort key.
    """
    return name.casefold(), name


def _resolved_text(
    row: RowFilter | ColumnMask, schema: PolicySchema | None
) -> tuple[str, str | None, tuple[str, ...]]:
    """Give a row's expression, and a mask's column, in the names of the schema read.

    Parameters
    ----------
    row
        The stored policy.
    schema
        The schema read; `None` for a caller that only asks whether a policy applies.

    Returns
    -------
    tuple
        The expression, a mask's column (`None` for a row filter), and the written names whose
        field is gone. Without a schema, or for a row not bound to field ids, the text and
        column as written.

    Raises
    ------
    ValidationError
        If the stored binding or expression of a bound row cannot be read (fail closed), with
        or without a schema.
    """
    column = row.column if isinstance(row, ColumnMask) else None
    if row.binding != FIELD_IDS:
        return row.expression, column, ()
    column_field_id = row.column_field_id if isinstance(row, ColumnMask) else None
    if schema is None:
        bound_field_ids(row.field_ids, column=column, column_field_id=column_field_id)
        return row.expression, column, ()
    resolved = resolve_expression(
        row.expression,
        row.field_ids,
        column=column,
        column_field_id=column_field_id,
        schema=schema,
    )
    return (
        resolved.expression,
        resolved.names.get(column, column) if column is not None else None,
        resolved.gone,
    )


def _substitute_context_functions(
    expression: str, *, principal_name: str, member_group_names: set[str], dialect: str = "duckdb"
) -> str:
    """Replace context-function calls in a stored expression with literal values.

    Parameters
    ----------
    expression
        A row-filter/column-mask expression, already validated once at
        write time by `lakana_core.policy.expressions.validate_restricted_expression`
        — trusted here, not re-validated.
    principal_name
        The requesting principal's name, substituted for `current_user()`.
    member_group_names
        Every group the requesting principal directly belongs to,
        substituted for `has_group(name)`.
    dialect
        The SQL dialect to parse and render `expression` as.

    Returns
    -------
    str
        `expression` rendered as SQL text, with every context function
        replaced by a literal — safe to splice directly into generated SQL,
        since `exp.Literal.string`/`exp.true`/`exp.false` render through
        sqlglot's own escaping (a literal embedding a single quote is
        doubled, the SQL-standard escape — never through raw string
        interpolation, which is exactly how an attacker-influenced
        principal name would become a SQL injection).

    Raises
    ------
    ValidationError
        If the stored expression no longer parses; the message names none of
        its text (`UNREADABLE_POLICY_MESSAGE`).
    """
    try:
        parsed = sqlglot.parse_one(expression, dialect=dialect, into=exp.Condition)
    except SQL_SYNTAX_ERRORS as exc:
        raise ValidationError(UNREADABLE_POLICY_MESSAGE) from exc
    for node in list(parsed.walk()):
        if isinstance(node, exp.CurrentUser):
            replacement: exp.Expression = exp.Literal.string(principal_name)
        elif isinstance(node, exp.Anonymous):
            # `.name`, not `.this` — for a quoted function name `.this` can
            # be an `exp.Identifier` object rather than a plain string
            # (the policy validator hit the same trap). Any call other than
            # `has_group` is left untouched rather than assumed to be one:
            # this function is what turns a stored policy into the literal
            # text spliced into generated SQL, so it does not trust that an
            # `exp.Anonymous` node it sees was already validated — even
            # though today it always has been, by
            # `validate_restricted_expression` at write time.
            if node.name.lower() != HAS_GROUP:
                continue
            args = node.expressions
            if len(args) != 1 or not isinstance(args[0], exp.Literal) or not args[0].is_string:
                continue
            group_name = args[0].this
            replacement = exp.true() if group_name in member_group_names else exp.false()
        else:
            continue
        # `node.replace(...)` mutates `node`'s parent to point at the
        # replacement — a no-op when `node` IS the tree's own root (a bare
        # `current_user()` or `has_group(...)` expression with nothing
        # around it), since a root has no parent to update. Verified
        # directly against sqlglot 30.18.0: replacing a root-level
        # `exp.Anonymous`/`exp.CurrentUser` left `parsed.sql()` rendering
        # the ORIGINAL, un-substituted text. Reassigning `parsed` itself in
        # that case is what actually fixes it.
        if node is parsed:
            parsed = replacement
        else:
            node.replace(replacement)
    return parsed.sql(dialect=dialect)


class PolicyResolver:
    """Resolve applicable row filters and column masks for a principal and table.

    Parameters
    ----------
    session
        Database session to query policies and group memberships from.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def resolve(
        self,
        principal: PrincipalRef,
        ref: TableRef,
        *,
        table_id: uuid.UUID | None = None,
        schema: PolicySchema | None = None,
    ) -> ResolvedPolicy:
        """Resolve every active policy covering `ref` for `principal`.

        Parameters
        ----------
        principal
            The requesting principal.
        ref
            The table being read.
        table_id
            The id of the table actually being read, if the caller already
            resolved it. When given, this is used directly and `ref` is
            never re-resolved by name. A caller that loaded its table row
            once (for example `lakana_worker.pipeline._resolve_sources`,
            which needs the row before it can even ask for the table's
            type) and then called this a moment later used to re-resolve
            `ref`'s name to an id independently, in a second, unrelated
            statement — a table rename racing between the two reads could
            make that second lookup land on a *different* table than the
            one actually being queried, applying that other table's
            policies (or none) to this read. Passing the id the
            caller already has closes that window; `None` (the default)
            preserves the by-name resolution for callers with no row of
            their own to draw an id from.
        schema
            The fields the read projects and the table's current ones. Given, every policy
            bound to field ids is rewritten to the names of the read, and one whose field is
            gone is named in `ResolvedPolicy.gone`. `None` for a caller that only asks whether
            a policy applies: the text is returned as written.

        Returns
        -------
        ResolvedPolicy
            Row filters (one predicate per applicable filter, direct or via
            any group) and column masks (exactly one applicable mask per
            column; two or more applicable masks on the same column
            collapse to `NULL` — nesting them is neither simple nor
            obviously safer). Empty when
            `table_id` was not given and `ref` does not resolve to an
            existing table — an unresolvable table has no id
            for a policy to be bound to, so it can never have one apply.

        Raises
        ------
        ValidationError
            `UNREADABLE_POLICY_MESSAGE` if a stored policy, or the binding of one, cannot be
            read.
        """
        if table_id is None:
            # Imported here because `lakana_core.catalog` imports this module at load time, so a
            # module-level import made this module fail as the first import of a process.
            from lakana_core.catalog.identity import resolve_table_id

            table_id = resolve_table_id(
                self._session, ref.environment, ref.namespace.joined(), ref.name
            )
        if table_id is None:
            return ResolvedPolicy()
        group_ids = list(
            self._session.execute(
                select(GroupMembership.group_id).where(GroupMembership.principal_id == principal.id)
            )
            .scalars()
            .all()
        )
        member_group_names = (
            set(
                self._session.execute(select(Group.name).where(Group.id.in_(group_ids)))
                .scalars()
                .all()
            )
            if group_ids
            else set()
        )

        row_filter_filter = RowFilter.principal_id == principal.id
        column_mask_filter = ColumnMask.principal_id == principal.id
        if group_ids:
            row_filter_filter = or_(row_filter_filter, RowFilter.group_id.in_(group_ids))
            column_mask_filter = or_(column_mask_filter, ColumnMask.group_id.in_(group_ids))

        filter_rows = (
            self._session.execute(
                select(RowFilter).where(
                    RowFilter.table_id == table_id,
                    RowFilter.revoked_at.is_(None),
                    row_filter_filter,
                )
            )
            .scalars()
            .all()
        )
        mask_rows = (
            self._session.execute(
                select(ColumnMask).where(
                    ColumnMask.table_id == table_id,
                    ColumnMask.revoked_at.is_(None),
                    column_mask_filter,
                )
            )
            .scalars()
            .all()
        )
        gone: list[str] = []
        row_filters: list[str] = []
        for row in filter_rows:
            text, _, row_gone = _resolved_text(row, schema)
            gone.extend(row_gone)
            if not row_gone:
                row_filters.append(
                    _substitute_context_functions(
                        text, principal_name=principal.name, member_group_names=member_group_names
                    )
                )
        # Masks are grouped by their column without regard to case, as the database matches
        # it: a bound mask and a legacy one on the same column, spelled two ways, are two masks
        # on it. The first row's spelling (by id, so it never changes) is the key.
        by_column: dict[str, tuple[str, list[tuple[ColumnMask, str]]]] = {}
        for row in sorted(mask_rows, key=lambda each: str(each.id)):
            text, key, row_gone = _resolved_text(row, schema)
            gone.extend(row_gone)
            if not row_gone and key is not None:
                by_column.setdefault(key.lower(), (key, []))[1].append((row, text))
        column_masks = {
            key: (
                _substitute_context_functions(
                    rows[0][1],
                    principal_name=principal.name,
                    member_group_names=member_group_names,
                )
                if len(rows) == 1
                else "NULL"
            )
            for key, rows in by_column.values()
        }
        policies = [
            PolicyColumns(frozenset(columns_of(row.expression)), row.schema_id)
            for row in filter_rows
            if row.binding != FIELD_IDS
        ] + [
            PolicyColumns(frozenset({row.column, *columns_of(row.expression)}), row.schema_id)
            for row in mask_rows
            if row.binding != FIELD_IDS
        ]
        typed_masks = frozenset(
            key for key, rows in by_column.values() if all(row.type_checked for row, _ in rows)
        )
        masked_field_ids = frozenset(
            row.column_field_id
            for row in mask_rows
            if row.binding == FIELD_IDS and row.column_field_id is not None
        )
        legacy_masked = frozenset(
            row.column.casefold() for row in mask_rows if row.binding != FIELD_IDS
        )
        return ResolvedPolicy(
            row_filters=row_filters,
            column_masks=column_masks,
            policies=policies,
            typed_masks=typed_masks,
            gone=min(gone, key=_by_case) if gone else None,
            masked_field_ids=masked_field_ids,
            legacy_masked=legacy_masked,
        )
