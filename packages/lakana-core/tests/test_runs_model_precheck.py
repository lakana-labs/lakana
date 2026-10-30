"""The pre-check of a generated check: columns and types, read from the catalog."""

from __future__ import annotations

import uuid

import pytest
from core_test_helpers import ensure_table
from sqlalchemy.orm import Session

from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    AuthorizationService,
    PrincipalRef,
    Securable,
)
from lakana_core.models import TableColumn
from lakana_core.projects.checks import CheckSpec, GeneratedFrom
from lakana_core.runs.model_precheck import is_numeric, model_problem, types_match


def _spec(generated: GeneratedFrom | None) -> CheckSpec:
    """Build a check of `gold.f`, generated unless `generated` is `None`.

    Parameters
    ----------
    generated
        What it was generated from; `None` for a hand-written check.

    Returns
    -------
    CheckSpec
        The check.
    """
    return CheckSpec(
        name="gold.f/x",
        table="gold.f",
        check="x",
        path="assets/gold/f.sql:1",
        on_failure="block",
        description=None,
        sql="SELECT 1",
        generated=generated,
    )


class _Tables:
    """Create `dev` with `gold.f` and `gold.d` and their stored columns.

    Parameters
    ----------
    session
        Database session.
    principal
        The run's principal the tests ask about.
    """

    def __init__(self, session: Session, principal: PrincipalRef) -> None:
        """Keep the session and the principal.

        Parameters
        ----------
        session
            Database session.
        principal
            The run's principal.
        """
        self.session = session
        self.principal = principal

    def __call__(self, *, own: dict[str, str], target: dict[str, str]) -> None:
        """Create both tables with the given columns, in order.

        Parameters
        ----------
        own
            Column name to type of `gold.f`, the checked table.
        target
            Column name to type of `gold.d`, the reference target.
        """
        for table, columns in (("f", own), ("d", target)):
            _, table_id = ensure_table(self.session, "dev", "gold", table)
            for position, (name, type_) in enumerate(columns.items()):
                self.session.add(
                    TableColumn(
                        id=uuid.uuid4(),
                        table_id=table_id,
                        name=name,
                        type=type_,
                        position=position,
                    )
                )
        self.session.commit()


@pytest.fixture
def env_with_tables(session: Session, principal: PrincipalRef) -> _Tables:
    """Provide the callable that creates `gold.f` and `gold.d`.

    Parameters
    ----------
    session
        Database session.
    principal
        The run's principal.

    Returns
    -------
    _Tables
        The callable; `.principal` is the run's principal.
    """
    return _Tables(session, principal)


class _DenyTarget:
    """Permit everything except a read of `gold.d`."""

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Allow every call; the pre-check only asks `permits`.

        Parameters
        ----------
        principal
            The acting principal.
        action
            The action.
        securable
            What it acts on.
        """

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Refuse `gold.d` and permit everything else.

        Parameters
        ----------
        principal
            The acting principal.
        action
            The action.
        securable
            What it acts on.

        Returns
        -------
        bool
            `False` for the table `gold.d`.
        """
        return securable.table != "d"


@pytest.fixture
def deny_target() -> AuthorizationService:
    """Provide an authorization that may not read `gold.d`.

    Returns
    -------
    AuthorizationService
        The authorization.
    """
    return _DenyTarget()


@pytest.mark.parametrize(
    ("columns", "generated", "expected"),
    [
        ({"id": "long"}, GeneratedFrom("key", ("id",)), None),
        ({"id": "long", "day": "date"}, GeneratedFrom("key", ("id", "day")), None),
        (
            {"amount": "double"},
            GeneratedFrom("key", ("amount",)),
            "Key column `amount` is double, a floating-point type, so it cannot identify a row; "
            "cast it in the asset, for example to DECIMAL, BIGINT or VARCHAR, and deploy again.",
        ),
        (
            {"id": "long", "ratio": "float"},
            GeneratedFrom("key", ("id", "ratio")),
            "Key column `ratio` is float, a floating-point type, so it cannot identify a row; "
            "cast it in the asset, for example to DECIMAL, BIGINT or VARCHAR, and deploy again.",
        ),
        (
            {"id": "long"},
            GeneratedFrom("key", ("nope",)),
            "Key column `nope` is not a column of `gold.f`; fix `key:` in assets/gold/f.sql:1 "
            "and deploy again.",
        ),
        (
            {"Id": "long", "id": "long"},
            GeneratedFrom("key", ("ID",)),
            "Key column `ID` is not a column of `gold.f`; fix `key:` in assets/gold/f.sql:1 "
            "and deploy again. Column names match exactly; the table has `Id`.",
        ),
        (
            {"store_ID": "long"},
            GeneratedFrom("references", ("store_id",), "gold.d", "store_id"),
            "Reference column `store_id` is not a column of `gold.f`; fix `references:` in "
            "assets/gold/f.sql:1 and deploy again. Column names match exactly; the table has "
            "`store_ID`.",
        ),
        (
            {"store_id": "int"},
            GeneratedFrom("references", ("store_id",), "gold.d", "store_id"),
            None,
        ),
        (
            {"store_id": "string"},
            GeneratedFrom("references", ("store_id",), "gold.d", "store_id"),
            "The reference `store_id` at `assets/gold/f.sql:1` has a type that differs from its "
            "target key column's; make the types match and deploy again.",
        ),
        (
            {"store_id": "long"},
            GeneratedFrom("references", ("store_id",), "gold.gone", "store_id"),
            "The reference `store_id` at `assets/gold/f.sql:1` cannot be checked: its target is "
            "missing, not readable by the run's principal, or no longer has the key column; fix "
            "the reference or the grant and deploy again.",
        ),
        (
            {"id": "long"},
            GeneratedFrom("references", ("store_id",), "gold.d", "store_id"),
            "Reference column `store_id` is not a column of `gold.f`; fix `references:` in "
            "assets/gold/f.sql:1 and deploy again.",
        ),
        (
            {"store_id": "long"},
            GeneratedFrom("references", ("store_id",), "gold.d", "shop_id"),
            "The reference `store_id` at `assets/gold/f.sql:1` cannot be checked: its target is "
            "missing, not readable by the run's principal, or no longer has the key column; fix "
            "the reference or the grant and deploy again.",
        ),
    ],
)
def test_the_precheck_texts(
    session: Session,
    env_with_tables: _Tables,
    columns: dict[str, str],
    generated: GeneratedFrom,
    expected: str | None,
) -> None:
    env_with_tables(own=columns, target={"store_id": "long"})
    principal = env_with_tables.principal
    text = model_problem(session, AllowAllAuthorization(), principal, "dev", _spec(generated))
    assert text == expected
    assert text is None or generated.target is None or generated.target not in text


def test_a_table_without_column_rows_is_left_to_the_count(
    session: Session, env_with_tables: _Tables
) -> None:
    env_with_tables(own={}, target={"store_id": "long"})
    spec = _spec(GeneratedFrom("key", ("id",)))
    principal = env_with_tables.principal
    assert model_problem(session, AllowAllAuthorization(), principal, "dev", spec) is None


def test_a_target_without_column_rows_is_left_to_the_count(
    session: Session, env_with_tables: _Tables
) -> None:
    env_with_tables(own={"store_id": "long"}, target={})
    spec = _spec(GeneratedFrom("references", ("store_id",), "gold.d", "store_id"))
    principal = env_with_tables.principal
    assert model_problem(session, AllowAllAuthorization(), principal, "dev", spec) is None


def test_a_checked_table_that_is_gone_is_left_to_the_count(
    session: Session, principal: PrincipalRef
) -> None:
    spec = _spec(GeneratedFrom("key", ("id",)))
    assert model_problem(session, AllowAllAuthorization(), principal, "dev", spec) is None


def test_a_hand_written_check_is_not_prechecked(session: Session, env_with_tables: _Tables) -> None:
    env_with_tables(own={"amount": "double"}, target={})
    principal = env_with_tables.principal
    assert model_problem(session, AllowAllAuthorization(), principal, "dev", _spec(None)) is None


def test_the_precheck_does_not_reveal_an_unreadable_target(
    session: Session, env_with_tables: _Tables, deny_target: AuthorizationService
) -> None:
    """Without READ_TABLE on the target, the text is the combined one, with no type."""
    env_with_tables(own={"store_id": "string"}, target={"store_id": "long"})
    text = model_problem(
        session,
        deny_target,
        env_with_tables.principal,
        "dev",
        _spec(GeneratedFrom("references", ("store_id",), "gold.d", "store_id")),
    )
    assert text == (
        "The reference `store_id` at `assets/gold/f.sql:1` cannot be checked: its target is "
        "missing, not readable by the run's principal, or no longer has the key column; fix the "
        "reference or the grant and deploy again."
    )
    assert "gold.d" not in text
    # "longer" holds "long": the target column's type is looked for as a word.
    assert "long" not in text.replace("longer", "")


def test_a_malformed_target_name_reads_as_gone(session: Session, env_with_tables: _Tables) -> None:
    """A stored manifest is untrusted: a target that is not a dotted name gets the combined text."""
    env_with_tables(own={"store_id": "long"}, target={"store_id": "long"})
    text = model_problem(
        session,
        AllowAllAuthorization(),
        env_with_tables.principal,
        "dev",
        _spec(GeneratedFrom("references", ("store_id",), "nodot", "store_id")),
    )
    assert text == (
        "The reference `store_id` at `assets/gold/f.sql:1` cannot be checked: its target is "
        "missing, not readable by the run's principal, or no longer has the key column; fix the "
        "reference or the grant and deploy again."
    )


@pytest.mark.parametrize(
    ("own", "other", "expected"),
    [
        ("long", "long", True),
        ("int", "long", True),
        ("long", "int", True),
        ("string", "long", False),
        ("decimal(10,2)", "decimal(10,2)", True),
        ("decimal(10,2)", "decimal(12,2)", False),
    ],
)
def test_types_match(own: str, other: str, expected: bool) -> None:
    assert types_match(own, other) is expected


def test_the_measures_precheck_refuses_a_string_column_and_a_missing_not_over(
    session: Session, env_with_tables: _Tables
) -> None:
    env_with_tables(
        own={
            "day": "date",
            "amount": "decimal(38,1)",
            "qty": "long",
            "name": "string",
            "flag": "boolean",
            "Stock": "long",
        },
        target={},
    )
    principal = env_with_tables.principal
    authz = AllowAllAuthorization()

    def problem(
        columns: tuple[str, ...], aggregations: tuple[str, ...], not_over: tuple[str, ...] = ()
    ) -> str | None:
        """Pre-check a `measures` check of `gold.f`.

        Parameters
        ----------
        columns
            The measure columns.
        aggregations
            Their aggregations, in the same order.
        not_over
            The `not_over` columns.

        Returns
        -------
        str or None
            The pre-check's text.
        """
        generated = GeneratedFrom("measures", columns, not_over=not_over, aggregations=aggregations)
        return model_problem(session, authz, principal, "dev", _spec(generated))

    assert problem(("amount", "qty"), ("sum", "avg"), ("day",)) is None
    assert problem(("name",), ("sum",)) == (
        "Measure `name` is string, not a number, so `aggregation: sum` cannot apply to it; cast "
        "it in the asset, for example to DECIMAL, BIGINT or DOUBLE, or change `aggregation:` "
        "(`count`, `count_distinct` and `none` take any type), and deploy again."
    )
    assert problem(("flag",), ("max",)) == (
        "Measure `flag` is boolean, so `aggregation: max` cannot apply to it: it needs a number, "
        "a date or a timestamp; cast it in the asset, or change `aggregation:` (`count`, "
        "`count_distinct` and `none` take any type), and deploy again."
    )
    assert problem(("stock",), ("count",)) == (
        "Measure column `stock` is not a column of `gold.f`; fix `measures:` in "
        "assets/gold/f.sql:1 and deploy again. Column names match exactly; the table has `Stock`."
    )
    assert problem(("qty",), ("sum",), ("date",)) == (
        "Column `date` in `not_over` is not a column of `gold.f`; fix `measures:` in "
        "assets/gold/f.sql:1 and deploy again."
    )


@pytest.mark.parametrize(
    ("aggregation", "type_", "allowed"),
    [
        ("sum", "decimal(38,1)", True),
        ("sum", "string", False),
        ("avg", "double", True),
        ("avg", "date", False),
        ("min", "long", True),
        ("min", "date", True),
        ("min", "string", False),
        ("max", "timestamptz", True),
        ("max", "timestamp_ns", True),
        ("max", "boolean", False),
        ("max", "time", False),
        ("count", "string", True),
        ("count_distinct", "string", True),
        ("count_distinct", "list<long>", True),
        ("none", "boolean", True),
    ],
)
def test_the_type_rule_of_each_aggregation(
    session: Session, env_with_tables: _Tables, aggregation: str, type_: str, allowed: bool
) -> None:
    """`sum`/`avg` need a number, `min`/`max` a number, date or timestamp, the rest any type."""
    env_with_tables(own={"m": type_}, target={})
    generated = GeneratedFrom("measures", ("m",), aggregations=(aggregation,))
    text = model_problem(
        session, AllowAllAuthorization(), env_with_tables.principal, "dev", _spec(generated)
    )
    assert (text is None) is allowed, text


@pytest.mark.parametrize(
    ("type_", "expected"),
    [
        ("int", True),
        ("long", True),
        ("float", True),
        ("double", True),
        ("decimal(10,2)", True),
        ("string", False),
        ("boolean", False),
        ("date", False),
        ("timestamptz", False),
        ("list<long>", False),
    ],
)
def test_is_numeric(type_: str, expected: bool) -> None:
    assert is_numeric(type_) is expected
