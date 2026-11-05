"""Tests for the restricted expression validator row filters and column masks share."""

import uuid
from typing import Literal

import pytest
import sqlglot
import sqlglot.errors

from lakana_core.auth import PrincipalRef
from lakana_core.errors import ValidationError
from lakana_core.models import PrincipalType
from lakana_core.policy import columns
from lakana_core.policy.expressions import (
    QualifiedColumnError,
    columns_of,
    normalized_expression,
    validate_restricted_expression,
)


@pytest.mark.parametrize(
    "expression",
    [
        "region = current_user()",
        "current_user = 'alice'",
        "has_group('managers')",
        "a > 1 AND b < 2",
        "a = 1 OR b = 2",
        "NOT (a = 1)",
        "TRUE",
        "FALSE",
        "a = -1",
        "a <= 1 AND (b >= 2 OR has_group('x'))",
    ],
)
def test_valid_filter_expressions_are_accepted(expression: str) -> None:
    """Every documented row-filter shape parses without raising."""
    validate_restricted_expression(expression, kind="filter")


@pytest.mark.parametrize(
    "expression",
    [
        "(SELECT 1)",
        "a = (SELECT 1)",
        "foo(bar)",
        "a IN (1, 2, 3)",
        "a LIKE '%x%'",
        "CAST(a AS INT)",
        "substr(a, 1, 2)",
        "has_group()",
        "has_group(a, b)",
        "has_group(col)",
        "count(*) OVER ()",
    ],
)
def test_invalid_filter_expressions_are_rejected(expression: str) -> None:
    """Every documented out-of-grammar shape raises for a row filter."""
    with pytest.raises(ValidationError):
        validate_restricted_expression(expression, kind="filter")


@pytest.mark.parametrize(
    "expression",
    [
        "NULL",
        "'REDACTED'",
        "substr(ssn, 1, 3) || '***'",
        "substring(ssn, 1, 3)",
        "md5(ssn)",
        "region = current_user()",
    ],
)
def test_valid_mask_expressions_are_accepted(expression: str) -> None:
    """Mask expressions additionally allow substr/substring, ||, and md5."""
    validate_restricted_expression(expression, kind="mask")


@pytest.mark.parametrize("expression", ["(SELECT 1)", "foo(bar)", "sha256(ssn)"])
def test_invalid_mask_expressions_are_rejected(expression: str) -> None:
    """A mask still refuses a subquery, unrecognized function, or function outside allowlist."""
    with pytest.raises(ValidationError):
        validate_restricted_expression(expression, kind="mask")


def test_a_mask_only_function_is_rejected_for_a_filter() -> None:
    """Substr is valid for a mask but not for a filter — the grammars differ."""
    validate_restricted_expression("substr(ssn, 1, 3)", kind="mask")
    with pytest.raises(ValidationError):
        validate_restricted_expression("substr(ssn, 1, 3) = 'abc'", kind="filter")


def test_invalid_sql_syntax_raises_validation_error() -> None:
    """Malformed SQL syntax is rejected as a validation error."""
    with pytest.raises(ValidationError):
        validate_restricted_expression(")", kind="filter")
    with pytest.raises(ValidationError):
        validate_restricted_expression("(", kind="filter")
    with pytest.raises(ValidationError):
        validate_restricted_expression("a = ", kind="filter")


def test_unrecognized_function_call_message_names_the_function() -> None:
    """The rejection names the actual function called, not a placeholder message."""
    with pytest.raises(
        ValidationError, match=r"foo\(\.\.\.\) is not a recognized context function"
    ):
        validate_restricted_expression("foo('x')", kind="filter")


def test_has_group_wrong_argument_shape_message_is_specific() -> None:
    """The rejection names the offending expression, not a placeholder."""
    with pytest.raises(ValidationError) as exc_info:
        validate_restricted_expression("has_group(a, b)", kind="filter")
    assert str(exc_info.value) == (
        "'has_group(a, b)' is not a valid expression: has_group(...) takes exactly "
        "one string literal argument."
    )


def test_has_group_rejects_a_non_string_literal_argument() -> None:
    """A single argument that is a Literal but not a string is still refused.

    Regression guard for an operator-precedence bug: `len(args) != 1 or not
    isinstance(...) or not args[0].is_string` short-circuits correctly, but
    a stray `and` in place of the middle `or` would make this case (a
    numeric literal — `isinstance` true, so the `and` branch controls the
    outcome) silently pass since it never reaches the `is_string` check.
    """
    with pytest.raises(ValidationError):
        validate_restricted_expression("has_group(123)", kind="filter")


def test_expressions_are_parsed_using_the_duckdb_dialect() -> None:
    """The duckdb dialect, not sqlglot's generic default, parses the expression.

    `a ** 2` (duckdb's power operator) fails to parse at all under sqlglot's
    generic default dialect, so if the duckdb dialect were ever dropped from
    the parse call, this would raise a different message (a parse error)
    instead of the grammar-restriction message pinned here — and if the
    node type named in that message were ever computed from the wrong
    value, it would say something other than `Pow`.
    """
    with pytest.raises(ValidationError) as exc_info:
        validate_restricted_expression("a ** 2", kind="filter")
    assert str(exc_info.value) == (
        "'a ** 2' is not a valid filter expression: Pow is outside the restricted subset."
    )


def test_a_bare_select_statement_is_rejected_at_parse_time() -> None:
    """A full statement is refused by the parse itself, not just the node walk.

    Distinct from `"(SELECT 1)"` in `test_invalid_filter_expressions_are_rejected`:
    that shape parses as a `Condition` either way (a parenthesized subquery)
    and is caught by the node-type walk instead. A bare `SELECT` is not
    itself a condition — it only reaches the walk (with a different, less
    specific message) if the `into=exp.Condition` restriction on the parse
    call is ever dropped.
    """
    with pytest.raises(
        ValidationError,
        match=r"not a valid expression: Invalid expression / Unexpected token \(line 1, column 6\)",
    ):
        validate_restricted_expression("SELECT 1", kind="filter")


def test_a_disallowed_node_after_a_valid_has_group_call_is_still_caught() -> None:
    """Validation doesn't stop early just because an earlier node passed.

    `md5(...)` is valid for a mask but not for a filter. Placing it after a
    valid `has_group(...)` call must still be caught — a loop that `break`s
    instead of `continue`s after handling the `has_group` node would abandon
    validation of everything that follows it.
    """
    with pytest.raises(ValidationError):
        validate_restricted_expression("has_group('eng') AND md5(name) = 'x'", kind="filter")


def test_a_valid_expression_returns_the_parsed_tree() -> None:
    """The validated expression tree is returned, not swallowed."""
    result = validate_restricted_expression("a = 1", kind="filter")
    assert result.sql(dialect="duckdb") == "a = 1"


def test_quoted_function_name_is_handled_safely() -> None:
    """Quoted identifiers as function names are handled without AttributeError.

    This is a regression test for a security issue where quoted function names
    like "has_group"('x') would cause an uncaught AttributeError instead of
    ValidationError, breaking the documented contract of validate_restricted_expression.
    Both quoted and unquoted forms should behave identically: either both
    accepted (for valid functions) or both rejected (for invalid functions).
    """
    # Both forms should be accepted
    validate_restricted_expression("has_group('x')", kind="filter")
    validate_restricted_expression("\"has_group\"('x')", kind="filter")

    # Both forms should be rejected for invalid function names
    with pytest.raises(ValidationError):
        validate_restricted_expression("foo('x')", kind="filter")
    with pytest.raises(ValidationError):
        validate_restricted_expression("\"foo\"('x')", kind="filter")


@pytest.mark.parametrize("bad", ["a = 1 AND " + "(" * 50_000, "has_group(" + "a, " * 30_000 + "b)"])
def test_a_refused_expression_is_echoed_bounded(bad: str) -> None:
    """An invalid expression of 100,000 characters does not become a message of that size."""
    with pytest.raises(ValidationError) as caught:
        validate_restricted_expression(bad, kind="filter")
    assert len(str(caught.value)) < 1000


def test_a_refused_expression_is_echoed_escaped() -> None:
    """A control character in the expression does not reach the message raw."""
    with pytest.raises(ValidationError) as caught:
        validate_restricted_expression("has_group(a)\nFORGED(b)", kind="filter")
    assert "\n" not in str(caught.value)


def test_a_parse_failure_is_cleaned_and_bounded() -> None:
    """The description reaches the message cleaned: no escape code, token dump or class path."""
    with pytest.raises(ValidationError) as exc_info:
        validate_restricted_expression("a >", kind="filter")
    message = str(exc_info.value)
    assert message.startswith("'a >' is not a valid expression: ")
    assert "\x1b" not in message
    assert "<class" not in message
    assert "line 1, column 3" in message
    assert message.endswith("Write it as a single condition over the table's columns.")


def test_a_long_description_is_cut() -> None:
    """A parse failure that echoes a long expression stays short."""
    with pytest.raises(ValidationError) as exc_info:
        validate_restricted_expression("a = " + "x" * 5000 + " )", kind="filter")
    assert len(str(exc_info.value).partition("is not a valid expression: ")[2]) < 400


def test_an_unclosed_quote_is_placed_by_line_and_column() -> None:
    """An unclosed quote names where it opens."""
    with pytest.raises(ValidationError) as exc_info:
        validate_restricted_expression("a = 'x\n and b", kind="filter")
    assert "never closed (line 1, column 5)" in str(exc_info.value)


def test_nesting_too_deep_is_named() -> None:
    """The parser's recursion limit reads as nesting, not as a Python error."""
    with pytest.raises(ValidationError, match="it is nested too deeply"):
        validate_restricted_expression("(" * 5000 + "a" + ")" * 5000, kind="filter")


def test_a_failure_without_a_position_omits_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """A parse error with no line and column still reads cleanly."""

    def fail(*_args: object, **_kwargs: object) -> None:
        raise sqlglot.errors.ParseError("x", errors=[{"description": "Bad thing"}])

    monkeypatch.setattr(sqlglot, "parse_one", fail)
    with pytest.raises(ValidationError) as exc_info:
        validate_restricted_expression("a", kind="filter")
    assert str(exc_info.value).startswith("'a' is not a valid expression: Bad thing. ")


def test_an_unterminated_comment_says_it_could_not_be_read() -> None:
    """A failure sqlglot does not describe in structured form gets a plain reason."""
    with pytest.raises(ValidationError, match="it could not be read"):
        validate_restricted_expression("a = 1 /* never ends", kind="filter")


@pytest.mark.parametrize(
    ("expression", "columns"),
    [
        ("region = 'eu'", {"region"}),
        ("region = 'eu' AND (Tier = 'gold' OR NOT \"Is Vip\")", {"region", "Tier", "Is Vip"}),
        ("TRUE", set()),
        ("'region' = 'eu'", set()),
        ("address.country = 'DE' AND a.b.c = 1", {"address", "a"}),
        ("substr(region, 1, 1) || '***'", {"region"}),
    ],
    ids=[
        "one",
        "several-as-written",
        "none",
        "a-literal-is-no-column",
        "a-field-of-a-struct-is-its-column",
        "a-mask",
    ],
)
def test_the_columns_an_expression_reads_are_named_as_written(
    expression: str, columns: set[str]
) -> None:
    """What a stored predicate needs of a table, to tell whether a read can apply it.

    Parameters
    ----------
    expression
        A row filter as the resolver hands it on.
    columns
        The column names it reads, spelled as in the expression.
    """
    assert columns_of(expression) == columns


@pytest.mark.parametrize(
    ("expression", "qualified", "bare"),
    [
        ("read_parquet.region = 'EU'", "read_parquet.region", "region"),
        ("t.region = 'EU'", "t.region", "region"),
        ('"t"."region" = 1', "t.region", "region"),
        ("a = 1 AND NOT (t.region = 'EU')", "t.region", "region"),
        ("gold.customers.region = 'EU'", "gold.customers.region", "region"),
    ],
)
def test_a_filter_refuses_a_qualified_column(expression: str, qualified: str, bare: str) -> None:
    """A row filter names its columns bare: the relation it runs on has no name to qualify by."""
    with pytest.raises(ValidationError) as exc_info:
        validate_restricted_expression(expression, kind="filter")
    assert str(exc_info.value) == (
        f"'{expression}' is not a valid filter expression: the column '{qualified}' is written "
        f"with a qualifier. Write the bare column name, '{bare}'; a row filter always reads the "
        "table it is set on."
    )


def test_a_filter_with_a_bare_column_is_still_accepted() -> None:
    """The refusal is only for a qualifier; a bare column and a quoted one pass."""
    validate_restricted_expression("region = 'EU' AND \"Id\" > 1", kind="filter")


def test_a_mask_refuses_a_qualified_column_too() -> None:
    """A mask expression reads the table it is set on, as a filter does."""
    with pytest.raises(ValidationError) as exc_info:
        validate_restricted_expression("read_parquet.ssn || 'x'", kind="mask")
    assert str(exc_info.value) == (
        "'read_parquet.ssn || 'x'' is not a valid mask expression: the column "
        "'read_parquet.ssn' is written with a qualifier. Write the bare column name, 'ssn'; "
        "a column mask always reads the table it is set on."
    )


@pytest.mark.parametrize(
    ("kind", "what"),
    [("filter", "a row filter does"), ("mask", "a column mask does")],
)
def test_a_dotted_name_that_starts_with_a_column_is_a_struct_field(
    kind: Literal["filter", "mask"], what: str
) -> None:
    """When the first part is a column of the table, no advice to write the field alone."""
    with pytest.raises(ValidationError) as exc_info:
        validate_restricted_expression("Address.country = 'DE'", kind=kind, columns=["address"])
    assert str(exc_info.value) == (
        f"'Address.country = 'DE'' is not a valid {kind} expression: 'Address.country' reads a "
        f"field of the column 'Address', and {what} not support struct fields. Use a column "
        "of the table as a whole."
    )


def test_a_dotted_name_that_does_not_start_with_a_column_keeps_the_qualifier_advice() -> None:
    """Known columns that do not include the first part: it is a qualifier."""
    with pytest.raises(ValidationError, match="Write the bare column name, 'country'"):
        validate_restricted_expression("t.country = 'DE'", kind="filter", columns=["country"])


def test_a_qualified_column_stays_refused_even_if_the_worded_check_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading the columns only rewords the refusal; the first refusal stands as a safety net."""
    real = columns.validate_restricted_expression
    calls: list[object] = []

    def worded_check_passes(
        expression: str, *, kind: Literal["filter", "mask"], columns: object = None
    ) -> None:
        """Refuse the first time, as the grammar does, and let the worded second check pass.

        Parameters
        ----------
        expression
            The policy expression.
        kind
            The kind of policy.
        columns
            The table's columns, given only to the second check.
        """
        calls.append(columns)
        if len(calls) == 1:
            real(expression, kind=kind)

    monkeypatch.setattr(columns, "validate_restricted_expression", worded_check_passes)
    monkeypatch.setattr(columns, "read_column_names", lambda *_args, **_kwargs: ["region"])
    principal = PrincipalRef(id=uuid.uuid4(), name="manager", type=PrincipalType.USER)

    with pytest.raises(QualifiedColumnError):
        columns.validate_for_table(
            object(),  # ty: ignore[invalid-argument-type]
            principal,
            environment="dev",
            namespace="gold",
            table="orders",
            expression="t.region = 'EU'",
            kind="filter",
        )
    assert calls == [None, ["region"]]


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("region='EU'", "region = 'EU'"),
        ("region  =  'É''s'", "region = 'É''s'"),
        ("has_group('pii')", "HAS_GROUP('pii')"),
        ("a b c (", "a b c ("),
        ("", ""),
    ],
)
def test_normalized_expression_spells_one_policy_one_way(expression: str, expected: str) -> None:
    """Two spellings of one expression compare equal; text that does not parse stays as it is."""
    assert normalized_expression(expression) == expected


def test_normalized_expression_leaves_a_too_deeply_nested_expression_alone() -> None:
    """The parser's recursion limit is no reason to fail; validation reports it later (#46)."""
    deep = "(" * 400 + "1" + ")" * 400
    assert normalized_expression(deep) == deep
