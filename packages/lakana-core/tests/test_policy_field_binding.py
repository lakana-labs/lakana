"""Tests for binding a policy's column names to Iceberg field ids."""

import pytest
from pyiceberg.types import NestedField, StringType, StructType

from lakana_core.errors import ValidationError
from lakana_core.policy.field_binding import (
    FIELD_IDS,
    AmbiguousPolicyColumnError,
    bind_policy,
    match_column,
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
    with pytest.raises(ValidationError) as caught:
        stored_field_ids(value)

    assert str(caught.value) == UNREADABLE_POLICY_MESSAGE


def test_a_stored_map_of_names_to_ids_is_returned() -> None:
    """The happy path returns a copy."""
    assert stored_field_ids({"a": 1}) == {"a": 1}
