"""Validating the restricted expression subset row filters and column masks use."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import Literal, cast

import sqlglot
import sqlglot.errors
import sqlglot.expressions as exp

from lakana_core.errors import ValidationError
from lakana_core.identifiers import shown
from lakana_core.sql_syntax import syntax_problem

_FILTER_NODE_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Column,
    exp.Identifier,
    exp.Literal,
    exp.Null,
    exp.Boolean,
    exp.Neg,
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.LT,
    exp.GTE,
    exp.LTE,
    exp.And,
    exp.Or,
    exp.Not,
    exp.Paren,
    exp.CurrentUser,
)
"""Node types a row-filter predicate may use: comparisons, boolean logic,
literals (including `TRUE`/`FALSE`/negative numbers), column references,
grouping, and `current_user()`. `has_group(...)` is checked separately (see
`_validate_has_group_call`), since a bare `exp.Anonymous` type check cannot
also verify its argument shape."""

_MASK_NODE_TYPES: tuple[type[exp.Expression], ...] = (
    *_FILTER_NODE_TYPES,
    exp.Substring,
    exp.DPipe,
    exp.MD5,
)
"""Additional node types a column mask's replacement-value expression may
use beyond a row filter's: `substr`/`substring`, `||` (concatenation), and
`md5` — restricted to exactly the base grammar, a mask could only ever be a
fixed literal or a bare column reference, never a hash or partial reveal."""

HAS_GROUP = "has_group"
"""The one function name `exp.Anonymous` may take — anything else is refused.

The resolver imports this, so validation and query-time substitution cannot disagree."""


_TIMESTAMP_DTYPES = (exp.DType.TIMESTAMP, exp.DType.TIMESTAMPNTZ)
"""The sqlglot types `TIMESTAMP '...'` parses to, according to the dialect."""

_DATE_FORMAT = "YYYY-MM-DD"
_TIMESTAMP_FORMAT = "YYYY-MM-DD HH:MM:SS"


def _valid_date_text(text: str) -> bool:
    """Say whether text is a date written exactly as `YYYY-MM-DD`.

    Parameters
    ----------
    text
        The text between the quotes of a `DATE '...'` literal.

    Returns
    -------
    bool
        `True` for a real calendar date in that form, with every digit in its place.
    """
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return False
    written = f"{parsed.year:04d}-{parsed.month:02d}-{parsed.day:02d}"
    return len(text) == len(_DATE_FORMAT) and written == text


def _valid_timestamp_text(text: str) -> bool:
    """Say whether text is a timestamp written as `YYYY-MM-DD HH:MM:SS`, with optional fraction.

    Parameters
    ----------
    text
        The text between the quotes of a `TIMESTAMP '...'` literal; a date alone is accepted.

    Returns
    -------
    bool
        `True` for a real moment in that form (a fraction of one to six digits may follow the
        seconds), with every digit in its place.
    """
    if _valid_date_text(text):
        return True
    whole, dot, fraction = text.partition(".")
    if len(whole) != len(_TIMESTAMP_FORMAT):
        return False
    if dot and not (fraction.isascii() and fraction.isdecimal() and len(fraction) <= 6):
        return False
    try:
        parsed = datetime.strptime(whole, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    written = (
        f"{parsed.year:04d}-{parsed.month:02d}-{parsed.day:02d} "
        f"{parsed.hour:02d}:{parsed.minute:02d}:{parsed.second:02d}"
    )
    return written == whole


def _check_typed_literal(node: exp.Cast, sql_expr: str) -> bool:
    """Accept `DATE '...'` and `TIMESTAMP '...'` when the text is a real date or moment.

    A typed literal is the one conversion a policy may contain, because it is a constant that is
    checked here, once: a text that is not a date would fail on every row.

    Parameters
    ----------
    node
        A cast node.
    sql_expr
        The original expression text, for the error message only.

    Returns
    -------
    bool
        `True` if `node` is a well-formed typed literal. `False` if it is any other cast, which
        the caller refuses as outside the subset.

    Raises
    ------
    ValidationError
        If it is a typed literal whose text is not a valid date or timestamp, or a
        `TIMESTAMPTZ` literal.
    """
    target = node.to
    literal = node.this
    extra = {key for key, value in node.args.items() if value and key not in ("this", "to")}
    if (
        type(node) is not exp.Cast
        or extra
        or not isinstance(literal, exp.Literal)
        or not literal.is_string
        or target.expressions
        or target.args.get("nested")
    ):
        return False
    head = f"{_quoted(sql_expr)} is not a valid expression: "
    shown_text = shown(literal.name)
    if target.this is exp.DType.DATE:
        if _valid_date_text(literal.name):
            return True
        raise ValidationError(
            f"{head}'{shown_text}' is not a date. Write DATE '{_DATE_FORMAT}', "
            "for example DATE '2026-01-01'."
        )
    if target.this in _TIMESTAMP_DTYPES:
        if _valid_timestamp_text(literal.name):
            return True
        raise ValidationError(
            f"{head}'{shown_text}' is not a timestamp. Write "
            f"TIMESTAMP '{_TIMESTAMP_FORMAT}', for example TIMESTAMP '2026-01-01 00:00:00'."
        )
    if target.this is exp.DType.TIMESTAMPTZ:
        raise ValidationError(
            f"{head}a timestamp with a time zone is not supported yet. Write "
            f"TIMESTAMP '{_TIMESTAMP_FORMAT}' to compare a timestamp column."
        )
    return False


def _quoted(sql_expr: str) -> str:
    """Quote an expression for an error message, escaped and cut at 200 characters.

    Parameters
    ----------
    sql_expr
        The expression a caller submitted, up to `max_statement_chars` long.

    Returns
    -------
    str
        The expression in single quotes, as `shown` makes it safe.
    """
    return f"'{shown(sql_expr)}'"


def _validate_has_group_call(node: exp.Anonymous, sql_expr: str) -> None:
    """Refuse an `exp.Anonymous` node unless it is a well-formed `has_group(name)` call.

    Parameters
    ----------
    node
        The anonymous function-call node to check.
    sql_expr
        The original expression text, for the error message only.

    Raises
    ------
    ValidationError
        If `node` does not name `has_group`, or does not take exactly one
        string-literal argument.
    """
    if node.name.lower() != HAS_GROUP:
        raise ValidationError(
            f"{_quoted(sql_expr)} is not a valid expression: {shown(node.name)}(...) is not a "
            "recognized context function."
        )
    args = node.expressions
    if len(args) != 1 or not isinstance(args[0], exp.Literal) or not args[0].is_string:
        raise ValidationError(
            f"{_quoted(sql_expr)} is not a valid expression: has_group(...) takes exactly "
            "one string literal argument."
        )


class QualifiedColumnError(ValidationError):
    """A filter or mask expression wrote a column with a dot in its name.

    Raised by `validate_restricted_expression` so that a caller who can read the table's columns
    may ask again with them and get the message that fits: a qualifier, or a struct field.
    """


def _qualified_column_error(
    node: exp.Column,
    sql_expr: str,
    kind: Literal["filter", "mask"],
    columns: Collection[str] | None,
) -> QualifiedColumnError:
    """Build the refusal of a column written with a dot in its name.

    Parameters
    ----------
    node
        The column node with more than one part.
    sql_expr
        The original expression text, for the error message only.
    kind
        `"filter"` or `"mask"`, which words the message.
    columns
        The table's column names, when known. A name whose first part is one of them is a
        field of that column, not a qualifier.

    Returns
    -------
    QualifiedColumnError
        The error to raise.
    """
    dotted = ".".join(part.name for part in node.parts)
    owner = "a row filter" if kind == "filter" else "a column mask"
    head = f"{_quoted(sql_expr)} is not a valid {kind} expression: "
    first = node.parts[0].name
    if columns is not None and first.lower() in {c.lower() for c in columns}:
        return QualifiedColumnError(
            f"{head}'{shown(dotted)}' reads a field of the column '{shown(first)}', and {owner} "
            "does not support struct fields. Use a column of the table as a whole."
        )
    return QualifiedColumnError(
        f"{head}the column '{shown(dotted)}' is written with a qualifier. Write the bare column "
        f"name, '{shown(node.name)}'; {owner} always reads the table it is set on."
    )


def validate_restricted_expression(
    sql_expr: str,
    *,
    kind: Literal["filter", "mask"],
    dialect: str = "duckdb",
    columns: Collection[str] | None = None,
) -> exp.Expression:
    """Validate an expression against the restricted subset row filters/column masks may use.

    Parameters
    ----------
    sql_expr
        The expression text to validate.
    kind
        `"filter"` for a row-filter predicate — comparisons, boolean logic,
        literals, column references, and the two context functions.
        `"mask"` for a column-mask replacement value, which additionally
        allows `substr`/`substring`, `||`, and `md5`.
    dialect
        The SQL dialect to parse `sql_expr` as.
    columns
        The table's column names, when the caller knows them; only the wording of the refusal
        of a dotted column name depends on them.

    Returns
    -------
    sqlglot.expressions.Expression
        The parsed, validated expression tree.

    Raises
    ------
    QualifiedColumnError
        If a column is written with a dot in its name (`t.region`, `read_parquet.region`,
        `address.country`): the relation a policy runs against has no name to qualify by and no
        struct field is supported, so only the bare column name can be honoured.
    ValidationError
        If `sql_expr` does not parse as a single expression (in particular,
        a full query — `SELECT ...` — is refused by the parse itself, via
        `into=exp.Condition`, not just by the node-type walk below); if it
        contains any node type outside the allowed set for `kind`; or if it
        calls `has_group(...)` with anything other than exactly one
        string-literal argument.
    """
    allowed = _MASK_NODE_TYPES if kind == "mask" else _FILTER_NODE_TYPES
    try:
        parsed = sqlglot.parse_one(sql_expr, dialect=dialect, into=exp.Condition)
    except Exception as exc:
        reader_error = isinstance(exc, sqlglot.errors.SqlglotError)
        problem = syntax_problem(exc, sql_expr) if reader_error else None
        if isinstance(exc, RecursionError):
            reason = "it is nested too deeply"
        elif problem is None:
            reason = "it could not be read"
        elif problem.line is None:
            reason = problem.reason
        else:
            reason = f"{problem.reason} (line {problem.line}, column {problem.column})"
        raise ValidationError(
            f"{_quoted(sql_expr)} is not a valid expression: {reason}. "
            "Write it as a single condition over the table's columns."
        ) from exc
    for node in parsed.walk():
        if isinstance(node, exp.Cast) and _check_typed_literal(node, sql_expr):
            continue
        if isinstance(node, exp.DataType) and isinstance(node.parent, exp.Cast):
            continue
        if isinstance(node, exp.Anonymous):
            _validate_has_group_call(node, sql_expr)
            continue
        if isinstance(node, exp.Column) and len(node.parts) > 1:
            error = _qualified_column_error(node, sql_expr, kind, columns)
            raise error
        if not isinstance(node, allowed):
            raise ValidationError(
                f"{_quoted(sql_expr)} is not a valid {kind} expression: "
                f"{type(node).__name__} is outside the restricted subset."
            )
    return cast(exp.Expression, parsed)


def columns_of(sql_expr: str, dialect: str = "duckdb") -> set[str]:
    """Name the columns an expression of the restricted subset reads.

    For a caller that must know whether a stored predicate can be applied to a relation
    before it runs the query, such as a read of a table at an older snapshot.

    Parameters
    ----------
    sql_expr
        A row filter or a column mask that already passed `validate_restricted_expression`
        (also after the resolver put literals in the place of the context functions).
    dialect
        The SQL dialect to parse `sql_expr` as.

    Returns
    -------
    set of str
        The names of the table's columns it references, spelled as in the expression. For a
        dotted reference that is the leftmost part: nothing in the relation a policy runs
        against has a name to qualify a column with, so ``address.country`` is the field
        `country` of the column `address`. A row filter or column mask is no longer accepted
        with such a reference, but one stored before that is still read this way. The database
        matches a column name without regard to case, so a caller compares them that way.
    """
    parsed = sqlglot.parse_one(sql_expr, dialect=dialect, into=exp.Condition)
    return {column.parts[0].name for column in parsed.find_all(exp.Column)}


def normalized_expression(sql_expr: str) -> str:
    """Spell an expression one way, so two spellings of one policy compare equal (#46).

    Parameters
    ----------
    sql_expr
        A row filter or column mask expression, valid or not.

    Returns
    -------
    str
        The expression as sqlglot writes it back in the DuckDB dialect; the text unchanged when
        it does not parse or is nested too deeply to parse (the validation that follows names
        the problem).
    """
    try:
        return sqlglot.parse_one(sql_expr, dialect="duckdb").sql(dialect="duckdb")
    except (sqlglot.errors.SqlglotError, RecursionError):
        return sql_expr
