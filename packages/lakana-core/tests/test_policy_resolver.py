"""Tests for PolicyResolver: composition and context-function substitution."""

import uuid
from datetime import UTC, datetime

import pytest
import sqlglot.errors
from core_test_helpers import ensure_table
from pyiceberg.types import NestedField, StringType
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.catalog.identity import resolve_table_id
from lakana_core.errors import ValidationError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    ColumnMask,
    Group,
    GroupMembership,
    Principal,
    PrincipalType,
    RowFilter,
    Table,
    TableType,
)
from lakana_core.policy import expressions, resolver
from lakana_core.policy.field_binding import (
    FIELD_IDS,
    PENDING,
    UNBOUND,
    UNREADABLE_POLICY_MESSAGE,
    PolicySchema,
    UnreadablePolicyError,
)
from lakana_core.policy.renames import PolicyColumns
from lakana_core.policy.resolver import PolicyResolver


def _field(field_id: int, name: str) -> NestedField:
    """Build an optional string column.

    Parameters
    ----------
    field_id
        The field id.
    name
        The column's name.

    Returns
    -------
    NestedField
        The column.
    """
    return NestedField(field_id, name, StringType(), required=False)


def _add_principal(session: Session, name: str) -> PrincipalRef:
    """Insert a principal and return a PrincipalRef for it.

    Parameters
    ----------
    session
        Database session.
    name
        The principal's name.

    Returns
    -------
    PrincipalRef
        A reference to the persisted principal.
    """
    row = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=name)
    session.add(row)
    session.commit()
    return PrincipalRef(id=row.id, name=row.name, type=row.type)


def _ref(table: str = "customers", *, namespace: tuple[str, ...] = ("gold",)) -> TableRef:
    """Build a TableRef in the dev environment, for the given table and namespace.

    Parameters
    ----------
    table
        The table name.
    namespace
        The namespace's parts, outermost first.

    Returns
    -------
    TableRef
        A reference to `table` in the `dev` environment's `namespace`.
    """
    return TableRef(
        environment="dev", namespace=NamespacePath.from_parts(list(namespace)), name=table
    )


def _add_creator(session: Session) -> uuid.UUID:
    """Insert a throwaway principal to satisfy `created_by`'s foreign key.

    `RowFilter.created_by`/`ColumnMask.created_by` are non-nullable foreign
    keys onto `principals.id`, and this session fixture enforces foreign
    keys for real (see `lakana_core.db.create_engine`) — a random,
    unpersisted UUID here would fail the insert, not merely be a
    meaningless value.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    uuid.UUID
        The id of a freshly persisted principal.
    """
    row = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=f"creator-{uuid.uuid4()}")
    session.add(row)
    session.commit()
    return row.id


def _add_filter(
    session: Session,
    *,
    principal_id: uuid.UUID | None = None,
    group_id: uuid.UUID | None = None,
    expression: str,
    table: str = "customers",
    environment: str = "dev",
    namespace: str = "gold",
    revoked_at: datetime | None = None,
    schema_id: int | None = None,
    binding: str = PENDING,
    field_ids: object = None,
) -> None:
    """Insert a RowFilter row directly.

    Parameters
    ----------
    session
        Database session.
    principal_id
        ID of the principal subject (mutually exclusive with group_id).
    group_id
        ID of the group subject (mutually exclusive with principal_id).
    expression
        The row-filter predicate.
    table
        The table name this filter applies to.
    environment
        The environment this filter applies to.
    namespace
        The namespace this filter applies to, already unit-separator-joined
        if it has more than one part.
    revoked_at
        When set, the filter is inserted already revoked.
    schema_id
        The schema id recorded with the filter.
    binding
        The binding state stored with the filter.
    field_ids
        The stored map of column names to field ids.
    """
    environment_id, table_id = ensure_table(session, environment, namespace, table)
    session.add(
        RowFilter(
            id=uuid.uuid4(),
            principal_id=principal_id,
            group_id=group_id,
            environment=environment,
            namespace=namespace,
            table=table,
            environment_id=environment_id,
            table_id=table_id,
            expression=expression,
            created_by=_add_creator(session),
            created_at=datetime.now(UTC),
            revoked_at=revoked_at,
            schema_id=schema_id,
            binding=binding,
            field_ids=field_ids,
        )
    )
    session.commit()


def _add_mask(
    session: Session,
    *,
    principal_id: uuid.UUID | None = None,
    group_id: uuid.UUID | None = None,
    column: str,
    expression: str,
    table: str = "customers",
    environment: str = "dev",
    namespace: str = "gold",
    revoked_at: datetime | None = None,
    created_at: datetime | None = None,
    schema_id: int | None = None,
    type_checked: bool = False,
    binding: str = PENDING,
    field_ids: object = None,
    column_field_id: int | None = None,
    row_id: uuid.UUID | None = None,
) -> None:
    """Insert a ColumnMask row directly.

    Parameters
    ----------
    session
        Database session.
    principal_id
        ID of the principal subject (mutually exclusive with group_id).
    group_id
        ID of the group subject (mutually exclusive with principal_id).
    column
        The column name being masked.
    expression
        The replacement-value expression.
    table
        The table name this mask applies to.
    environment
        The environment this mask applies to.
    namespace
        The namespace this mask applies to, already unit-separator-joined
        if it has more than one part.
    revoked_at
        When set, the mask is inserted already revoked.
    created_at
        Timestamp to record for this mask; defaults to now.
    schema_id
        The schema id recorded with the mask.
    type_checked
        Whether the mask was checked against its column's type.
    binding
        The binding state stored with the mask.
    field_ids
        The stored map of column names to field ids.
    column_field_id
        The stored field id of the masked column.
    row_id
        The mask's id; random by default.
    """
    environment_id, table_id = ensure_table(session, environment, namespace, table)
    session.add(
        ColumnMask(
            id=row_id or uuid.uuid4(),
            principal_id=principal_id,
            group_id=group_id,
            environment=environment,
            namespace=namespace,
            table=table,
            environment_id=environment_id,
            table_id=table_id,
            column=column,
            expression=expression,
            created_by=_add_creator(session),
            created_at=created_at or datetime.now(UTC),
            revoked_at=revoked_at,
            schema_id=schema_id,
            type_checked=type_checked,
            binding=binding,
            field_ids=field_ids,
            column_field_id=column_field_id,
        )
    )
    session.commit()


def test_no_policy_resolves_to_nothing(session: Session) -> None:
    """A principal with no applicable policy gets an empty ResolvedPolicy."""
    alice = _add_principal(session, "alice")
    resolved = PolicyResolver(session).resolve(alice, _ref())
    assert resolved.row_filters == []
    assert resolved.column_masks == {}


def test_an_explicit_table_id_overrides_the_refs_name(session: Session) -> None:
    """`table_id`, when given, is authoritative over `ref`'s name.

    Closes a TOCTOU race in `lakana_worker.pipeline._resolve_sources`: it
    loads a table row by name, then (before the id binding) called `resolve` a moment
    later, which re-resolved the same name to an id in a second, unrelated
    statement — a rename racing between the two could land that second
    lookup on a *different* table. This simulates exactly that: `ref` now
    names table `b` (as if `a` and `b` had swapped names in between), but
    the caller already holds `a`'s real id from the row it loaded earlier.
    That id must still resolve `a`'s own mask, never `b`'s (which has
    none), proving `ref`'s name is never consulted once `table_id` is
    given.
    """
    alice = _add_principal(session, "alice")
    _add_mask(session, principal_id=alice.id, column="ssn", expression="NULL", table="a")
    ensure_table(session, "dev", "gold", "b")
    a_id = resolve_table_id(session, "dev", "gold", "a")
    assert a_id is not None

    resolved = PolicyResolver(session).resolve(alice, _ref("b"), table_id=a_id)

    assert resolved.column_masks == {"ssn": "NULL"}


def test_every_policy_carries_its_columns_and_its_recorded_schema(session: Session) -> None:
    """The columns a policy uses and the schema it was written against reach the reader."""
    alice = _add_principal(session, "alice")
    _add_filter(session, principal_id=alice.id, expression="region = 'EU' AND TRUE", schema_id=3)
    _add_mask(session, principal_id=alice.id, column="ssn", expression="md5(name)")
    _add_mask(session, principal_id=alice.id, column="email", expression="NULL", schema_id=5)
    _add_filter(session, principal_id=alice.id, expression="has_group('staff')", schema_id=4)

    resolved = PolicyResolver(session).resolve(alice, _ref())

    assert sorted((sorted(policy.columns), policy.schema_id) for policy in resolved.policies) == [
        ([], 4),
        (["email"], 5),
        (["name", "ssn"], None),
        (["region"], 3),
    ]


def test_a_policy_of_another_principal_is_not_carried(session: Session) -> None:
    """Only the policies that apply to the reader can make a read stale."""
    alice = _add_principal(session, "alice")
    bob = _add_principal(session, "bob")
    _add_mask(session, principal_id=bob.id, column="ssn", expression="NULL")

    assert PolicyResolver(session).resolve(alice, _ref()).policies == []


def test_a_direct_row_filter_is_resolved_and_substituted(session: Session) -> None:
    """A direct row filter's current_user() is replaced with the principal's real name."""
    alice = _add_principal(session, "alice")
    _add_filter(session, principal_id=alice.id, expression="region = current_user()")

    resolved = PolicyResolver(session).resolve(alice, _ref())

    assert resolved.row_filters == ["region = 'alice'"]


def test_a_group_row_filter_applies_to_its_members(session: Session) -> None:
    """A row filter granted to a group applies to a principal who is a member of it."""
    alice = _add_principal(session, "alice")
    group = Group(id=uuid.uuid4(), name="analysts")
    session.add(group)
    session.add(GroupMembership(group_id=group.id, principal_id=alice.id))
    session.commit()
    _add_filter(session, group_id=group.id, expression="TRUE")

    resolved = PolicyResolver(session).resolve(alice, _ref())

    assert resolved.row_filters == ["TRUE"]


def test_two_applicable_filters_both_appear_for_the_caller_to_and(session: Session) -> None:
    """Multiple applicable filters are all returned; the caller (query_rewrite) ANDs them."""
    alice = _add_principal(session, "alice")
    group = Group(id=uuid.uuid4(), name="analysts")
    session.add(group)
    session.add(GroupMembership(group_id=group.id, principal_id=alice.id))
    session.commit()
    _add_filter(session, principal_id=alice.id, expression="region = current_user()")
    _add_filter(session, group_id=group.id, expression="TRUE")

    resolved = PolicyResolver(session).resolve(alice, _ref())

    assert set(resolved.row_filters) == {"region = 'alice'", "TRUE"}


def test_a_filter_on_a_different_table_does_not_apply(session: Session) -> None:
    """A row filter is scoped to its own table, not any table with the same name elsewhere.

    `customers` (the table actually asked about) is a real, distinct table
    here too, so the empty result comes from `table_id` mismatch — the rule
    under test — rather than from `customers` simply not existing.
    """
    alice = _add_principal(session, "alice")
    _add_filter(session, principal_id=alice.id, expression="TRUE", table="orders")
    ensure_table(session, "dev", "gold", "customers")

    resolved = PolicyResolver(session).resolve(alice, _ref("customers"))

    assert resolved.row_filters == []


def test_has_group_substitutes_true_for_a_real_membership(session: Session) -> None:
    """has_group('x') becomes TRUE when the principal really is a member of x."""
    alice = _add_principal(session, "alice")
    group = Group(id=uuid.uuid4(), name="managers")
    session.add(group)
    session.add(GroupMembership(group_id=group.id, principal_id=alice.id))
    session.commit()
    _add_filter(session, principal_id=alice.id, expression="has_group('managers')")

    resolved = PolicyResolver(session).resolve(alice, _ref())

    assert resolved.row_filters == ["TRUE"]


def test_has_group_substitutes_false_for_a_membership_the_principal_lacks(session: Session) -> None:
    """has_group('x') becomes FALSE when the principal is not a member of x."""
    alice = _add_principal(session, "alice")
    _add_filter(session, principal_id=alice.id, expression="has_group('managers')")

    resolved = PolicyResolver(session).resolve(alice, _ref())

    assert resolved.row_filters == ["FALSE"]


def test_a_current_user_containing_a_quote_cannot_inject_sql(session: Session) -> None:
    """A malicious-looking principal name is escaped, never spliced in raw."""
    alice = _add_principal(session, "alice'; DROP TABLE x; --")
    _add_filter(session, principal_id=alice.id, expression="region = current_user()")

    resolved = PolicyResolver(session).resolve(alice, _ref())

    assert resolved.row_filters == ["region = 'alice''; DROP TABLE x; --'"]


def test_a_single_applicable_mask_uses_its_own_expression(session: Session) -> None:
    """Exactly one applicable mask uses its own expression unchanged (modulo substitution)."""
    alice = _add_principal(session, "alice")
    _add_mask(session, principal_id=alice.id, column="ssn", expression="NULL")

    resolved = PolicyResolver(session).resolve(alice, _ref())

    assert resolved.column_masks == {"ssn": "NULL"}


def test_two_applicable_masks_on_the_same_column_collapse_to_null(session: Session) -> None:
    """Two masks on the same column, both applicable, are ambiguous; NULL is the safe answer."""
    alice = _add_principal(session, "alice")
    group = Group(id=uuid.uuid4(), name="analysts")
    session.add(group)
    session.add(GroupMembership(group_id=group.id, principal_id=alice.id))
    session.commit()
    _add_mask(session, principal_id=alice.id, column="ssn", expression="md5(ssn)")
    _add_mask(session, group_id=group.id, column="ssn", expression="substr(ssn, 1, 3)")

    resolved = PolicyResolver(session).resolve(alice, _ref())

    assert resolved.column_masks == {"ssn": "NULL"}


def test_masks_on_different_columns_are_independent(session: Session) -> None:
    """Each masked column is resolved independently of the others."""
    alice = _add_principal(session, "alice")
    _add_mask(session, principal_id=alice.id, column="ssn", expression="NULL")
    _add_mask(session, principal_id=alice.id, column="email", expression="'redacted@example.com'")

    resolved = PolicyResolver(session).resolve(alice, _ref())

    assert resolved.column_masks == {"ssn": "NULL", "email": "'redacted@example.com'"}


def test_an_unrecognized_function_call_is_left_untouched(session: Session) -> None:
    """A call other than has_group is never assumed to be one, even naming a real group."""
    alice = _add_principal(session, "alice")
    group = Group(id=uuid.uuid4(), name="managers")
    session.add(group)
    session.add(GroupMembership(group_id=group.id, principal_id=alice.id))
    session.commit()
    _add_filter(session, principal_id=alice.id, expression="foo('managers')")

    resolved = PolicyResolver(session).resolve(alice, _ref())

    assert resolved.row_filters == ["FOO('managers')"]


def test_a_quoted_has_group_function_name_still_substitutes(session: Session) -> None:
    """A quoted has_group call name is recognized via `.name`, not misread via `.this`.

    `.this` on an `exp.Anonymous` node is an `exp.Identifier` object rather
    than a plain string when the function name is quoted — exactly the trap
    the policy validator once fell into. The fix here uses `.name`,
    which normalizes both forms to a plain string.
    """
    alice = _add_principal(session, "alice")
    group = Group(id=uuid.uuid4(), name="managers")
    session.add(group)
    session.add(GroupMembership(group_id=group.id, principal_id=alice.id))
    session.commit()
    _add_filter(session, principal_id=alice.id, expression="\"has_group\"('managers')")

    resolved = PolicyResolver(session).resolve(alice, _ref())

    assert resolved.row_filters == ["TRUE"]


def test_a_has_group_call_with_a_non_string_argument_is_left_untouched(session: Session) -> None:
    """A malformed has_group(...) call (wrong argument shape) is never substituted.

    `validate_restricted_expression` already refuses this shape at write
    time — this pins the resolver's own defense, so a validator bug can't
    turn a malformed call into a silent TRUE/FALSE.
    """
    alice = _add_principal(session, "alice")
    _add_filter(session, principal_id=alice.id, expression="has_group(1)")

    resolved = PolicyResolver(session).resolve(alice, _ref())

    assert resolved.row_filters == ["HAS_GROUP(1)"]


def test_an_unrelated_function_call_does_not_stop_substitution_of_a_later_one(
    session: Session,
) -> None:
    """An unrecognized call earlier in the expression must not abandon the rest of it.

    A loop that `break`s instead of `continue`s once it sees a call other
    than `has_group` would stop walking the tree there, leaving a later
    `current_user()` unsubstituted in the generated SQL.
    """
    alice = _add_principal(session, "alice")
    _add_filter(session, principal_id=alice.id, expression="foo('x') OR current_user() = 'alice'")

    resolved = PolicyResolver(session).resolve(alice, _ref())

    assert resolved.row_filters == ["FOO('x') OR 'alice' = 'alice'"]


def test_a_has_group_call_with_no_arguments_is_left_untouched(session: Session) -> None:
    """`has_group()` (zero arguments) is a malformed call, defensively left unsubstituted.

    Regression guard for an operator-precedence bug: replacing the `or` in
    `len(args) != 1 or not isinstance(...) or not args[0].is_string` with an
    `and` would evaluate `args[0]` even when `args` is empty, raising
    `IndexError` instead of leaving the call untouched.
    """
    alice = _add_principal(session, "alice")
    _add_filter(session, principal_id=alice.id, expression="has_group()")

    resolved = PolicyResolver(session).resolve(alice, _ref())

    assert resolved.row_filters == ["HAS_GROUP()"]


def test_a_malformed_has_group_call_does_not_stop_substitution_of_a_later_one(
    session: Session,
) -> None:
    """A malformed `has_group(...)` call earlier in the expression must not abandon the rest.

    Same reasoning as the unrelated-function-call test above, for the other
    `continue` in this loop: the one after the malformed-arguments check.
    """
    alice = _add_principal(session, "alice")
    _add_filter(
        session, principal_id=alice.id, expression="has_group(1) OR current_user() = 'alice'"
    )

    resolved = PolicyResolver(session).resolve(alice, _ref())

    assert resolved.row_filters == ["HAS_GROUP(1) OR 'alice' = 'alice'"]


def test_rendering_uses_the_duckdb_dialect_not_the_generic_default(session: Session) -> None:
    """DuckDB's 1-indexed bracket semantics survive the round trip.

    Parsing and rendering an expression under two different dialects can
    silently change its meaning even when neither step raises: sqlglot's
    generic default dialect reindexes DuckDB's 1-based `a[1]` to `a[0]`
    when it renders the same AST node back out.
    """
    alice = _add_principal(session, "alice")
    _add_filter(session, principal_id=alice.id, expression="a[1] = b")

    resolved = PolicyResolver(session).resolve(alice, _ref())

    assert resolved.row_filters == ["a[1] = b"]


def test_a_duckdb_specific_operator_is_parsed_and_rendered_correctly(session: Session) -> None:
    """The duckdb dialect, not sqlglot's generic default, parses the expression.

    `**` (duckdb's power operator) fails to parse at all under sqlglot's
    generic default dialect, so dropping the dialect from the parse call
    would raise here instead of resolving normally.
    """
    alice = _add_principal(session, "alice")
    _add_filter(session, principal_id=alice.id, expression="a ** 2 > 0")

    resolved = PolicyResolver(session).resolve(alice, _ref())

    assert resolved.row_filters == ["POWER(a, 2) > 0"]


def test_a_full_statement_is_never_silently_accepted(session: Session) -> None:
    """A stored expression that is a full statement, not a single condition, raises.

    Write-time validation (`validate_restricted_expression`) already
    guarantees a stored expression is a single condition, never a full
    statement — this pins the resolver's own defense, via the
    `into=exp.Condition` restriction on its parse call, so dropping that
    restriction doesn't silently start accepting one.
    """
    alice = _add_principal(session, "alice")
    _add_filter(session, principal_id=alice.id, expression="SELECT 1")

    with pytest.raises(ValidationError, match="stored on this table could not be read") as info:
        PolicyResolver(session).resolve(alice, _ref())
    assert "SELECT" not in str(info.value)
    assert isinstance(info.value.__cause__, sqlglot.errors.ParseError)


def test_an_unreadable_stored_mask_names_no_policy_text(session: Session) -> None:
    """N3: a stored policy that stops parsing never reaches a reader's message, even in part."""
    alice = _add_principal(session, "alice")
    _add_mask(session, principal_id=alice.id, column="email", expression="SECRETWORD ((( +")

    with pytest.raises(ValidationError, match="Ask an administrator") as info:
        PolicyResolver(session).resolve(alice, _ref())
    assert "SECRETWORD" not in str(info.value)


def test_a_mask_current_user_call_is_substituted_with_the_real_principal_name(
    session: Session,
) -> None:
    """A column mask's own `current_user()` is substituted, not left as a placeholder."""
    alice = _add_principal(session, "alice")
    _add_mask(session, principal_id=alice.id, column="owner", expression="current_user()")

    resolved = PolicyResolver(session).resolve(alice, _ref())

    assert resolved.column_masks == {"owner": "'alice'"}


def test_a_mask_has_group_call_is_substituted_using_the_principals_real_memberships(
    session: Session,
) -> None:
    """A column mask's own `has_group(...)` is substituted using real group membership."""
    alice = _add_principal(session, "alice")
    group = Group(id=uuid.uuid4(), name="managers")
    session.add(group)
    session.add(GroupMembership(group_id=group.id, principal_id=alice.id))
    session.commit()
    _add_mask(session, principal_id=alice.id, column="salary", expression="has_group('managers')")

    resolved = PolicyResolver(session).resolve(alice, _ref())

    assert resolved.column_masks == {"salary": "TRUE"}


def test_a_row_filter_leaves_another_principals_filters_alone(
    session: Session,
) -> None:
    """Resolving alice never returns a row filter that belongs to a different principal.

    Without the subject filter (`RowFilter.principal_id == principal.id` or
    via group), any active filter on the table would apply to every
    principal who asks about it, not just the one it was granted to.
    """
    alice = _add_principal(session, "alice")
    bob = _add_principal(session, "bob")
    _add_filter(session, principal_id=bob.id, expression="TRUE")

    resolved = PolicyResolver(session).resolve(alice, _ref())

    assert resolved.row_filters == []


def test_a_column_mask_on_a_different_table_does_not_apply(session: Session) -> None:
    """A column mask is scoped to its own table, not any table with the same name elsewhere.

    `customers` is a real, distinct table here too, so the empty result
    comes from `table_id` mismatch rather than from `customers` simply not
    existing.
    """
    alice = _add_principal(session, "alice")
    _add_mask(session, principal_id=alice.id, column="ssn", expression="NULL", table="orders")
    ensure_table(session, "dev", "gold", "customers")

    resolved = PolicyResolver(session).resolve(alice, _ref("customers"))

    assert resolved.column_masks == {}


def test_a_column_mask_leaves_another_principals_mask_alone(session: Session) -> None:
    """Resolving alice never returns a column mask that belongs to a different principal.

    Without the subject filter, any active mask on the table would apply to
    every principal who asks about it, not just the one it was granted to.
    """
    alice = _add_principal(session, "alice")
    bob = _add_principal(session, "bob")
    _add_mask(session, principal_id=bob.id, column="ssn", expression="NULL")

    resolved = PolicyResolver(session).resolve(alice, _ref())

    assert resolved.column_masks == {}


def test_a_revoked_row_filter_does_not_apply(session: Session) -> None:
    """A revoked row filter is excluded even though it would otherwise apply."""
    alice = _add_principal(session, "alice")
    _add_filter(session, principal_id=alice.id, expression="TRUE", revoked_at=datetime.now(UTC))

    resolved = PolicyResolver(session).resolve(alice, _ref())

    assert resolved.row_filters == []


def test_a_row_filter_in_a_different_environment_does_not_apply(session: Session) -> None:
    """A row filter scoped to a different environment does not leak across environments.

    `dev`'s own `gold.customers` (the table actually asked about) is a
    real, distinct table here too, so the empty result comes from
    `table_id` mismatch rather than from `dev` never having existed.
    """
    alice = _add_principal(session, "alice")
    _add_filter(session, principal_id=alice.id, expression="TRUE", environment="prod")
    ensure_table(session, "dev", "gold", "customers")

    resolved = PolicyResolver(session).resolve(alice, _ref())

    assert resolved.row_filters == []


def test_a_row_filter_in_a_different_namespace_does_not_apply(session: Session) -> None:
    """A row filter scoped to a different namespace does not leak across namespaces.

    `dev.gold.customers` (the table actually asked about) is a real,
    distinct table here too, so the empty result comes from `table_id`
    mismatch rather than from `gold` never having existed.
    """
    alice = _add_principal(session, "alice")
    _add_filter(session, principal_id=alice.id, expression="TRUE", namespace="silver")
    ensure_table(session, "dev", "gold", "customers")

    resolved = PolicyResolver(session).resolve(alice, _ref())

    assert resolved.row_filters == []


def test_a_row_filter_on_a_multi_part_namespace_is_matched(session: Session) -> None:
    """A multi-part namespace is matched via its real unit-separator-joined storage form."""
    alice = _add_principal(session, "alice")
    joined = NamespacePath.from_parts(["gold", "eu"]).joined()
    _add_filter(session, principal_id=alice.id, expression="TRUE", namespace=joined)

    resolved = PolicyResolver(session).resolve(alice, _ref(namespace=("gold", "eu")))

    assert resolved.row_filters == ["TRUE"]


def test_a_row_filter_on_a_sibling_multi_part_namespace_does_not_apply(session: Session) -> None:
    """A filter on gold/eu does not leak to a query against the sibling namespace gold/us.

    `gold.us.customers` (the table actually asked about) is a real,
    distinct table here too, so the empty result comes from `table_id`
    mismatch rather than from `gold.us` never having existed.
    """
    alice = _add_principal(session, "alice")
    joined = NamespacePath.from_parts(["gold", "eu"]).joined()
    _add_filter(session, principal_id=alice.id, expression="TRUE", namespace=joined)
    ensure_table(session, "dev", NamespacePath.from_parts(["gold", "us"]).joined(), "customers")

    resolved = PolicyResolver(session).resolve(alice, _ref(namespace=("gold", "us")))

    assert resolved.row_filters == []


def test_a_revoked_column_mask_does_not_apply(session: Session) -> None:
    """A revoked column mask is excluded even though it would otherwise apply."""
    alice = _add_principal(session, "alice")
    _add_mask(
        session,
        principal_id=alice.id,
        column="ssn",
        expression="NULL",
        revoked_at=datetime.now(UTC),
    )

    resolved = PolicyResolver(session).resolve(alice, _ref())

    assert resolved.column_masks == {}


def test_a_column_mask_in_a_different_environment_does_not_apply(session: Session) -> None:
    """A column mask scoped to a different environment does not leak across environments.

    `dev.gold.customers` (the table actually asked about) is a real,
    distinct table here too, so the empty result comes from `table_id`
    mismatch rather than from `dev` never having existed.
    """
    alice = _add_principal(session, "alice")
    _add_mask(session, principal_id=alice.id, column="ssn", expression="NULL", environment="prod")
    ensure_table(session, "dev", "gold", "customers")

    resolved = PolicyResolver(session).resolve(alice, _ref())

    assert resolved.column_masks == {}


def test_a_column_mask_in_a_different_namespace_does_not_apply(session: Session) -> None:
    """A column mask scoped to a different namespace does not leak across namespaces.

    `dev.gold.customers` (the table actually asked about) is a real,
    distinct table here too, so the empty result comes from `table_id`
    mismatch rather than from `gold` never having existed.
    """
    alice = _add_principal(session, "alice")
    _add_mask(session, principal_id=alice.id, column="ssn", expression="NULL", namespace="silver")
    ensure_table(session, "dev", "gold", "customers")

    resolved = PolicyResolver(session).resolve(alice, _ref())

    assert resolved.column_masks == {}


def test_a_column_mask_on_a_multi_part_namespace_is_matched(session: Session) -> None:
    """A multi-part namespace is matched for column masks too, via the joined storage form."""
    alice = _add_principal(session, "alice")
    joined = NamespacePath.from_parts(["gold", "eu"]).joined()
    _add_mask(session, principal_id=alice.id, column="ssn", expression="NULL", namespace=joined)

    resolved = PolicyResolver(session).resolve(alice, _ref(namespace=("gold", "eu")))

    assert resolved.column_masks == {"ssn": "NULL"}


def test_policy_follows_rename(session: Session) -> None:
    """A row filter still applies after its table is renamed.

    The filter is bound to the table's id (via `table_id`), never its
    display name, so a rename that changes only the `Table.name` column
    does not disturb which filters resolve for it.
    """
    alice = _add_principal(session, "alice")
    _add_filter(session, principal_id=alice.id, expression="TRUE", table="customers")

    row = session.execute(select(Table).where(Table.name == "customers")).scalar_one()
    row.name = "customers2"
    session.commit()

    resolved = PolicyResolver(session).resolve(alice, _ref("customers2"))

    assert resolved.row_filters == ["TRUE"]


def test_policy_on_recreated_table_does_not_apply(session: Session) -> None:
    """Dropping and recreating a table under the same name starts with no policies.

    Before the id binding, a row filter matched by name alone, so a dropped-and-recreated
    `gold.customers` would inherit whatever filters the old table had. Here
    the old table row is deleted directly (`ON DELETE SET NULL` clears the
    filter's now-dangling `table_id`) and a new table is created with the
    same name but a new id; the filter's `table_id` no longer matches it.
    """
    alice = _add_principal(session, "alice")
    _add_filter(session, principal_id=alice.id, expression="TRUE", table="customers")

    old = session.execute(select(Table).where(Table.name == "customers")).scalar_one()
    namespace_id = old.namespace_id
    session.delete(old)
    session.commit()
    session.add(
        Table(
            id=uuid.uuid4(),
            namespace_id=namespace_id,
            name="customers",
            table_type=TableType.ICEBERG,
        )
    )
    session.commit()

    resolved = PolicyResolver(session).resolve(alice, _ref("customers"))

    assert resolved.row_filters == []


def test_resolver_and_validator_share_one_has_group_name() -> None:
    """The validator and the query-time substitution use one `has_group` name, not two copies."""
    assert expressions.HAS_GROUP == "has_group"
    assert resolver.HAS_GROUP is expressions.HAS_GROUP


def test_a_mask_is_typed_only_when_every_mask_of_the_column_was_type_checked(
    session: Session,
) -> None:
    """`typed_masks` names the columns whose masks were all checked; a legacy mask stays out."""
    alice = _add_principal(session, "alice")
    _add_mask(session, principal_id=alice.id, column="ssn", expression="'***'", type_checked=True)
    _add_mask(session, principal_id=alice.id, column="email", expression="'x'")
    _add_mask(session, principal_id=alice.id, column="name", expression="'a'", type_checked=True)
    _add_mask(session, principal_id=alice.id, column="name", expression="'b'", type_checked=True)

    resolved = PolicyResolver(session).resolve(alice, _ref())

    assert resolved.column_masks["name"] == "NULL"
    assert resolved.typed_masks == frozenset({"ssn", "name"})


def test_a_bound_filter_is_rewritten_to_the_names_of_the_schema_read(session: Session) -> None:
    """Filter `dept = 'a'` bound to field 2, now called `department`."""
    alice = _add_principal(session, "alice")
    _add_filter(
        session,
        principal_id=alice.id,
        expression="dept = 'a'",
        binding=FIELD_IDS,
        field_ids={"dept": 2},
    )
    schema = PolicySchema.of((_field(1, "id"), _field(2, "department")))

    policy = PolicyResolver(session).resolve(alice, _ref(), schema=schema)

    assert policy.row_filters == ["\"department\" = 'a'"]
    assert (policy.gone, policy.policies) == (None, [])


def test_a_bound_filter_is_substituted_after_the_rewrite(session: Session) -> None:
    """`current_user()` still becomes the reader's name in the rewritten text."""
    alice = _add_principal(session, "alice")
    _add_filter(
        session,
        principal_id=alice.id,
        expression="owner = current_user()",
        binding=FIELD_IDS,
        field_ids={"owner": 2},
    )
    schema = PolicySchema.of((_field(2, "owned_by"),))

    policy = PolicyResolver(session).resolve(alice, _ref(), schema=schema)

    assert policy.row_filters == ["\"owned_by\" = 'alice'"]


def test_a_bound_mask_is_keyed_by_its_columns_current_name(session: Session) -> None:
    """Mask on `ssn` (field 3), renamed `tax_id`: keyed `tax_id`, expression rewritten."""
    alice = _add_principal(session, "alice")
    _add_mask(
        session,
        principal_id=alice.id,
        column="ssn",
        expression="md5(ssn)",
        type_checked=True,
        binding=FIELD_IDS,
        field_ids={"ssn": 3},
        column_field_id=3,
    )
    schema = PolicySchema.of((_field(1, "id"), _field(3, "tax_id")))

    policy = PolicyResolver(session).resolve(alice, _ref(), schema=schema)

    assert policy.column_masks == {"tax_id": 'MD5("tax_id")'}
    assert policy.masked_field_ids == frozenset({3})
    assert policy.legacy_masked == frozenset()
    assert policy.typed_masks == frozenset({"tax_id"})
    assert (policy.gone, policy.policies) == (None, [])


def test_a_bound_policy_on_a_dropped_column_names_it_as_gone(session: Session) -> None:
    """Field 3 is not in the current schema: `gone` is the name as written; the rest resolves."""
    alice = _add_principal(session, "alice")
    _add_mask(
        session,
        principal_id=alice.id,
        column="ssn",
        expression="NULL",
        binding=FIELD_IDS,
        field_ids={},
        column_field_id=3,
    )
    _add_filter(
        session,
        principal_id=alice.id,
        expression="dept = 'a'",
        binding=FIELD_IDS,
        field_ids={"dept": 2},
    )
    schema = PolicySchema.of((_field(1, "id"), _field(2, "dept")))

    policy = PolicyResolver(session).resolve(alice, _ref(), schema=schema)

    assert policy.gone == "ssn"
    assert policy.row_filters == ["\"dept\" = 'a'"]
    assert policy.column_masks == {}
    assert policy.masked_field_ids == frozenset({3})


def test_the_gone_name_shown_is_the_first_without_regard_to_case(session: Session) -> None:
    """Two rows gone: the refusal names the same one every time."""
    alice = _add_principal(session, "alice")
    for written in ("b", "A"):
        _add_filter(
            session,
            principal_id=alice.id,
            expression=f"{written} = 1",
            binding=FIELD_IDS,
            field_ids={written: 9},
        )

    policy = PolicyResolver(session).resolve(alice, _ref(), schema=PolicySchema.of(()))

    assert policy.gone == "A"


def test_two_masks_on_one_field_written_in_two_spellings_collapse_to_null(
    session: Session,
) -> None:
    """G6 / Review Focus 4: a bound mask on field 3 and a legacy mask on `SSN`: one `NULL`."""
    alice = _add_principal(session, "alice")
    _add_mask(
        session,
        principal_id=alice.id,
        column="ssn",
        expression="'x'",
        type_checked=True,
        binding=FIELD_IDS,
        field_ids={"ssn": 3},
        column_field_id=3,
        row_id=uuid.UUID(int=1),
    )
    _add_mask(
        session,
        principal_id=alice.id,
        column="SSN",
        expression="'y'",
        type_checked=True,
        row_id=uuid.UUID(int=2),
    )
    schema = PolicySchema.of((_field(3, "ssn"),))

    policy = PolicyResolver(session).resolve(alice, _ref(), schema=schema)

    assert policy.column_masks == {"ssn": "NULL"}
    assert policy.typed_masks == frozenset({"ssn"})
    assert (policy.masked_field_ids, policy.legacy_masked) == (frozenset({3}), frozenset({"ssn"}))


def test_a_pending_or_unbound_policy_is_read_as_written_with_the_rename_rule(
    session: Session,
) -> None:
    """Legacy rows keep today's path: text as written and an entry in `policies`."""
    alice = _add_principal(session, "alice")
    _add_filter(session, principal_id=alice.id, expression="dept = 'a'", schema_id=7)
    _add_mask(
        session,
        principal_id=alice.id,
        column="SSN",
        expression="NULL",
        schema_id=7,
        binding=UNBOUND,
    )
    schema = PolicySchema.of((_field(2, "department"),))

    policy = PolicyResolver(session).resolve(alice, _ref(), schema=schema)

    assert policy.row_filters == ["dept = 'a'"]
    assert policy.column_masks == {"SSN": "NULL"}
    assert sorted(policy.policies, key=lambda p: sorted(p.columns)) == [
        PolicyColumns(frozenset({"SSN"}), 7),
        PolicyColumns(frozenset({"dept"}), 7),
    ]
    assert (policy.masked_field_ids, policy.legacy_masked) == (frozenset(), frozenset({"ssn"}))


def test_without_a_schema_a_bound_policy_is_returned_as_written(session: Session) -> None:
    """Presence checks (incremental append, write refusal) pass no schema."""
    alice = _add_principal(session, "alice")
    _add_filter(
        session,
        principal_id=alice.id,
        expression="dept = 'a'",
        binding=FIELD_IDS,
        field_ids={"dept": 2},
    )
    _add_mask(
        session,
        principal_id=alice.id,
        column="ssn",
        expression="NULL",
        binding=FIELD_IDS,
        field_ids={"ssn": 3},
        column_field_id=3,
    )

    policy = PolicyResolver(session).resolve(alice, _ref())

    assert policy.row_filters == ["dept = 'a'"]
    assert policy.column_masks == {"ssn": "NULL"}
    assert (policy.gone, policy.policies, policy.masked_field_ids) == (None, [], frozenset({3}))


@pytest.mark.parametrize("with_schema", [True, False])
def test_a_stored_binding_that_is_not_a_map_refuses(session: Session, with_schema: bool) -> None:
    """`field_ids` of `[]` on a bound row: `UNREADABLE_POLICY_MESSAGE`."""
    alice = _add_principal(session, "alice")
    _add_filter(
        session, principal_id=alice.id, expression="dept = 'a'", binding=FIELD_IDS, field_ids=[]
    )
    schema = PolicySchema.of((_field(2, "dept"),)) if with_schema else None

    with pytest.raises(UnreadablePolicyError) as caught:
        PolicyResolver(session).resolve(alice, _ref(), schema=schema)

    assert str(caught.value) == UNREADABLE_POLICY_MESSAGE


@pytest.mark.parametrize("with_schema", [True, False])
def test_a_bound_mask_without_its_columns_id_refuses(session: Session, with_schema: bool) -> None:
    """F4: `column_field_id` NULL on a bound mask is refused, never skipped."""
    alice = _add_principal(session, "alice")
    _add_mask(
        session,
        principal_id=alice.id,
        column="ssn",
        expression="NULL",
        binding=FIELD_IDS,
        field_ids={},
    )
    schema = PolicySchema.of((_field(3, "ssn"),)) if with_schema else None

    with pytest.raises(UnreadablePolicyError) as caught:
        PolicyResolver(session).resolve(alice, _ref(), schema=schema)

    assert str(caught.value) == UNREADABLE_POLICY_MESSAGE


@pytest.mark.parametrize("with_schema", [True, False])
def test_a_binding_state_nobody_knows_refuses(session: Session, with_schema: bool) -> None:
    """M1: a `binding` outside the four states is unreadable, never read as a legacy row."""
    alice = _add_principal(session, "alice")
    _add_filter(session, principal_id=alice.id, expression="dept = 'a'", binding="Field_Ids")
    schema = PolicySchema.of((_field(2, "dept"),)) if with_schema else None

    with pytest.raises(UnreadablePolicyError) as caught:
        PolicyResolver(session).resolve(alice, _ref(), schema=schema)

    assert str(caught.value) == UNREADABLE_POLICY_MESSAGE


def test_an_unparseable_legacy_policy_refuses_as_unreadable(session: Session) -> None:
    """The text of a row of an earlier release that no longer parses: the same error class."""
    alice = _add_principal(session, "alice")
    _add_filter(session, principal_id=alice.id, expression="dept = (")

    with pytest.raises(UnreadablePolicyError) as caught:
        PolicyResolver(session).resolve(alice, _ref())

    assert str(caught.value) == UNREADABLE_POLICY_MESSAGE
