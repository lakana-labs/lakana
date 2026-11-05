"""Tests for binding a policy's column names to Iceberg field ids."""

import pytest
from pyiceberg.types import NestedField, StringType, StructType

from lakana_core.errors import ValidationError
from lakana_core.policy.field_binding import (
    FIELD_IDS,
    AmbiguousPolicyColumnError,
    PolicySchema,
    UnreadablePolicyError,
    bind_policy,
    bound_field_ids,
    match_column,
    policy_column_gone_message,
    rename_columns,
    resolve_expression,
    resolve_names,
    stored_field_ids,
)
from lakana_core.policy.resolver import UNREADABLE_POLICY_MESSAGE


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


def test_a_name_binds_to_the_field_equal_under_lower() -> None:
    """`Region` binds to `region`, as the database compares it."""
    assert match_column((_field(1, "id"), _field(2, "region")), "Region") == 2


def test_a_missing_name_binds_to_nothing() -> None:
    """No field of that name: `None`, for the caller's own "no column" message."""
    assert match_column((_field(1, "id"),), "region") is None


def test_a_name_equal_to_two_columns_under_casefold_is_refused() -> None:
    """`Maß` and `MASS` are one name under casefold but not under lower: refused, never guessed."""
    fields = (_field(1, "Maß"), _field(2, "MASS"))

    with pytest.raises(AmbiguousPolicyColumnError) as caught:
        match_column(fields, "Maß")

    assert str(caught.value) == (
        "The policy names column 'Maß', and the table has the columns 'MASS' and 'Maß', "
        "which Lakana cannot tell apart for a policy. Ask the owner of the table to rename "
        "one of them, then create the policy again."
    )


def test_a_dotted_name_never_binds_to_a_nested_field() -> None:
    """Only top-level fields bind: `address.country` is not a column."""
    address = NestedField(1, "address", StructType(_field(2, "country")), required=False)

    assert match_column((address,), "address.country") is None
    assert match_column((address,), "country") is None


def test_bind_policy_records_every_name_as_written_and_the_masked_column() -> None:
    """Keys are the names as the policy spells them; the mask's own column has its own id."""
    fields = (_field(1, "id"), _field(2, "ssn"), _field(3, "dept"))

    binding = bind_policy(fields, {"Dept", "ssn"}, masked_column="ssn")

    assert binding.binding == FIELD_IDS
    assert binding.field_ids == {"Dept": 3, "ssn": 2}
    assert binding.column_field_id == 2


def test_bind_policy_of_a_filter_that_reads_no_column_is_bound_and_empty() -> None:
    """`has_group('staff')` has nothing to follow and is bound."""
    binding = bind_policy((_field(1, "id"),), set())

    assert (binding.binding, binding.field_ids, binding.column_field_id) == (FIELD_IDS, {}, None)


def test_bind_policy_refuses_a_name_the_fields_lack() -> None:
    """A belt behind the callers' own refusal: never stored half-bound."""
    with pytest.raises(ValidationError, match="'region'"):
        bind_policy((_field(1, "id"),), {"region"})


def test_a_mask_on_a_column_with_a_case_twin_is_refused_as_ambiguous() -> None:
    """`ssn` next to `SSN`: the mask's own column is checked for the twin too."""
    with pytest.raises(AmbiguousPolicyColumnError):
        bind_policy((_field(1, "ssn"), _field(2, "SSN")), set(), masked_column="ssn")


@pytest.mark.parametrize("value", [None, [], {"a": "1"}, {"a": True}, {1: 2}, "{}"])
def test_a_stored_map_that_is_not_names_to_ids_fails_closed(value: object) -> None:
    """Anything but `dict[str, int]` refuses as an unreadable policy."""
    with pytest.raises(UnreadablePolicyError) as caught:
        stored_field_ids(value)

    assert str(caught.value) == UNREADABLE_POLICY_MESSAGE


def test_a_stored_map_of_names_to_ids_is_returned() -> None:
    """The happy path returns a copy."""
    assert stored_field_ids({"a": 1}) == {"a": 1}


def test_a_renamed_column_resolves_to_its_new_name() -> None:
    """Field 2 was `dept`, is `department` now."""
    schema = PolicySchema.of((_field(1, "id"), _field(2, "department")))

    resolved = resolve_names({"dept": 2}, schema)

    assert (resolved.names, resolved.gone) == ({"dept": "department"}, ())


def test_a_new_column_under_the_old_name_is_not_the_policys() -> None:
    """`dept` (2) renamed away and a new `dept` (5) added: the policy keeps field 2."""
    schema = PolicySchema.of((_field(2, "old_dept"), _field(5, "dept")))

    assert resolve_names({"dept": 2}, schema).names == {"dept": "old_dept"}


def test_a_dropped_field_is_gone() -> None:
    """Field 2 is in no schema now."""
    resolved = resolve_names({"dept": 2, "id": 1}, PolicySchema.of((_field(1, "id"),)))

    assert (resolved.names, resolved.gone) == ({"id": "id"}, ("dept",))


def test_gone_names_are_sorted_without_regard_to_case() -> None:
    """Two gone names: sorted by casefold, so the refusal names the same one every time."""
    resolved = resolve_names({"b": 2, "A": 3}, PolicySchema.of((_field(1, "id"),)))

    assert resolved.gone == ("A", "b")


def test_a_field_added_after_the_snapshot_is_named_by_its_current_name() -> None:
    """G2: present now, absent from the check's snapshot: current name, not gone."""
    schema = PolicySchema(read=(_field(1, "id"),), current=(_field(1, "id"), _field(4, "tier")))

    resolved = resolve_names({"Tier": 4}, schema)

    assert (resolved.names, resolved.gone) == ({"Tier": "tier"}, ())


def test_a_field_dropped_after_the_snapshot_is_gone_even_in_the_snapshot() -> None:
    """G2: absent now refuses, whatever the snapshot has."""
    schema = PolicySchema(read=(_field(1, "id"), _field(2, "dept")), current=(_field(1, "id"),))

    assert resolve_names({"dept": 2}, schema).gone == ("dept",)


def test_a_field_is_named_as_the_read_names_it() -> None:
    """The snapshot read calls field 2 `dept`, the table calls it `team` now: `dept`."""
    schema = PolicySchema(read=(_field(2, "dept"),), current=(_field(2, "team"),))

    assert resolve_names({"dept": 2}, schema).names == {"dept": "dept"}


def test_a_nested_field_with_the_id_is_not_a_column() -> None:
    """Only top-level fields name a policy's column (Review Focus 3)."""
    address = NestedField(1, "address", StructType(_field(2, "dept")), required=False)

    assert resolve_names({"dept": 2}, PolicySchema.of((address,))).gone == ("dept",)


def test_rename_columns_rewrites_only_column_references() -> None:
    """A string literal equal to the old name stays; the column is quoted with its new name."""
    out = rename_columns("dept = 'dept' AND has_group('dept')", {"dept": "Department"})

    assert out == "\"Department\" = 'dept' AND HAS_GROUP('dept')"


def test_rename_columns_of_a_bare_column_mask() -> None:
    """The root node itself is a column."""
    assert rename_columns("ssn", {"ssn": "tax_id"}) == '"tax_id"'


@pytest.mark.parametrize("expression", ["t.dept = 'a'", "other = 1", "dept = ("])
def test_rename_columns_fails_closed(expression: str) -> None:
    """Qualified, unmapped, unparseable: an unreadable policy, never the text as written."""
    with pytest.raises(UnreadablePolicyError) as caught:
        rename_columns(expression, {"dept": "dept"})

    assert str(caught.value) == UNREADABLE_POLICY_MESSAGE


def test_the_refusal_text_is_the_specs() -> None:
    """Spec §3, verbatim, with the names shown."""
    assert policy_column_gone_message("ssn", "gold.staff") == (
        "`gold.staff` has a row filter or column mask on a column that was dropped or replaced "
        "(created for `ssn`); an administrator creates it again for the column it is meant "
        "for, or revokes it"
    )


def test_a_masks_own_column_joins_its_bound_ids() -> None:
    """The masked column's id is part of what the mask follows."""
    assert bound_field_ids({"dept": 2}, column="ssn", column_field_id=3) == {"dept": 2, "ssn": 3}


def test_a_filters_bound_ids_are_the_stored_map() -> None:
    """A row filter has no column of its own."""
    assert bound_field_ids({"dept": 2}, column=None, column_field_id=None) == {"dept": 2}


@pytest.mark.parametrize(
    ("field_ids", "column_field_id"),
    [({"dept": 2}, None), ({"ssn": 4}, 3), (None, 3), ({"ssn": 3}, True)],
)
def test_a_masks_binding_that_does_not_add_up_fails_closed(
    field_ids: object, column_field_id: object
) -> None:
    """F4: no id of its own, an expression key with another id, no map, or a non-int id."""
    with pytest.raises(UnreadablePolicyError) as caught:
        bound_field_ids(field_ids, column="ssn", column_field_id=column_field_id)

    assert str(caught.value) == UNREADABLE_POLICY_MESSAGE


def test_a_bound_expression_is_rewritten_with_its_columns_names() -> None:
    """Mask on `ssn` (3) built from `dept` (2); both renamed."""
    schema = PolicySchema.of((_field(2, "team"), _field(3, "tax_id")))

    resolved = resolve_expression(
        "md5(dept)", {"dept": 2}, column="ssn", column_field_id=3, schema=schema
    )

    assert resolved.expression == 'MD5("team")'
    assert (resolved.names, resolved.gone) == ({"dept": "team", "ssn": "tax_id"}, ())


def test_a_bound_expression_whose_field_is_gone_is_returned_as_written() -> None:
    """Nothing is rewritten; the caller refuses the read by the gone names."""
    resolved = resolve_expression(
        "dept = 'a'", {"dept": 2}, column=None, column_field_id=None, schema=PolicySchema.of(())
    )

    assert (resolved.expression, resolved.names, resolved.gone) == ("dept = 'a'", {}, ("dept",))
