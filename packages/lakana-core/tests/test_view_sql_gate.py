"""A view's SQL in `table get` follows the content gate of "Defined in" (#587).

`describe_table` shows a view's stored SQL only to a reader who holds READ_ENVIRONMENT, may read
every table the view references (none of them a view), and has no row filter or column mask on
any of them. A shown text is recorded as `SOURCE_VIEWED` first; a refused reader gets
`content_reason` ``restricted`` and the sentence of "Defined in".
"""

from __future__ import annotations

import hashlib

from sqlalchemy import update
from sqlalchemy.orm import Session
from test_projects_definition import _analyst, _person, _policy, _source_viewed, _tables

from lakana_core.auth import PrincipalRef
from lakana_core.catalog.service import CatalogService, TableDescription
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.identifiers import TableRef
from lakana_core.models import Table, TableType
from lakana_core.projects.definition import RESTRICTED_MESSAGE
from lakana_core.storage.resolver import StorageResolver

_SQL = "SELECT a FROM silver.a JOIN gold.f USING (a)"
_VIEW = TableRef.from_dotted("dev", "gold.v")


def _view(session: Session, *references: str, sql: str = _SQL) -> None:
    """Make `gold.v`, a view over `references`, with the tables it names.

    Parameters
    ----------
    session
        Database session; commits.
    *references
        The dotted tables it references (`silver.a` and `gold.f` when none).
    sql
        Its stored SQL.
    """
    names = references or ("silver.a", "gold.f")
    ids = _tables(session, *names, "gold.v")
    session.execute(
        update(Table)
        .where(Table.id == ids["gold.v"])
        .values(
            table_type=TableType.VIEW,
            view_definition={
                "sql": sql,
                "references": [{"id": str(ids[name]), "name": name} for name in names],
                "columns": [{"name": "a", "type": "int", "nullable": True}],
            },
        )
    )
    session.commit()


def _get(session: Session, p: PrincipalRef, *, window_minutes: int | None = 60) -> TableDescription:
    """Describe `gold.v` as `table get` does.

    Parameters
    ----------
    session
        Database session.
    p
        The reader.
    window_minutes
        The audit window the route passes; `None` as an internal caller.

    Returns
    -------
    TableDescription
        The view's description.
    """
    catalog = CatalogService(session, StorageResolver(session), GrantAuthorization(session))
    return catalog.describe_table(p, _VIEW, view_sql_window_minutes=window_minutes)


def _restricted(got: TableDescription) -> bool:
    """Say whether the SQL was withheld by the content gate, the references kept.

    Parameters
    ----------
    got
        The view's description.

    Returns
    -------
    bool
        Whether `sql` is withheld with the reason and sentence of "Defined in".
    """
    return (
        got.sql is None
        and got.content_reason == "restricted"
        and got.content_message == RESTRICTED_MESSAGE
        and got.hidden_references == 0
        and len(got.references) == 2
    )


def test_a_reader_who_passes_the_gate_sees_the_sql_and_the_read_is_recorded_once(
    session: Session,
) -> None:
    _view(session)
    analyst = _analyst(session)

    got = _get(session, analyst)
    again = _get(session, analyst)

    assert (got.sql, got.content_reason, got.content_message) == (_SQL, None, None)
    assert again.sql == _SQL
    [row] = _source_viewed(session)
    assert row.principal_id == analyst.id
    assert (row.securable_namespace, row.securable_table) == ("gold", "v")
    assert row.detail == {"view_sql_sha256": hashlib.sha256(_SQL.encode()).hexdigest()}


def test_a_reader_without_read_environment_gets_no_sql(session: Session) -> None:
    _view(session)
    reader = _person(session, "t", "gold.v", "silver.a", "gold.f")

    assert _restricted(_get(session, reader))
    assert _source_viewed(session) == []


def test_a_row_filter_on_a_referenced_table_hides_the_sql(session: Session) -> None:
    _view(session)
    analyst = _analyst(session)
    _policy(session, "filter", analyst, "silver.a")

    assert _restricted(_get(session, analyst))
    assert _source_viewed(session) == []


def test_a_column_mask_on_a_referenced_table_hides_the_sql(session: Session) -> None:
    _view(session)
    analyst = _analyst(session)
    _policy(session, "mask", analyst, "gold.f")

    assert _restricted(_get(session, analyst))


def test_a_view_among_the_references_hides_the_sql(session: Session) -> None:
    _view(session)
    session.execute(update(Table).where(Table.name == "a").values(table_type=TableType.VIEW))
    session.commit()

    assert _restricted(_get(session, _analyst(session)))


def test_a_file_read_in_the_sql_hides_it(session: Session) -> None:
    _view(session, "gold.f", sql="SELECT a FROM gold.f UNION ALL SELECT a FROM 'x.parquet'")
    got = _get(session, _analyst(session))
    assert got.sql is None and got.content_reason == "restricted"


def test_an_unreadable_reference_still_counts_it_as_before(session: Session) -> None:
    _view(session)
    reader = _person(session, "r", "gold.v", "gold.f", environment=True)

    got = _get(session, reader)

    assert (got.sql, got.references, got.hidden_references, got.content_reason) == (
        None,
        (),
        1,
        None,
    )


def test_an_internal_caller_gets_no_sql_and_records_nothing(session: Session) -> None:
    _view(session)
    got = _get(session, _analyst(session), window_minutes=None)
    assert (got.sql, got.content_reason) == (None, None)
    assert _source_viewed(session) == []
