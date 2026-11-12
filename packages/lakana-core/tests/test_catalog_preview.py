"""Queueing the preview of a table: who may ask, what is queued, and how the run is bound.

`submit_preview` decides who may ask and writes the query; the rows themselves are read by the
worker, in the worker's tests.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime

import pytest
from core_test_helpers import ensure_table
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from lakana_core.auth import (
    AllowAllAuthorization,
    PrincipalRef,
    RunContext,
    SecurableKind,
    TaskScope,
)
from lakana_core.catalog.preview import (
    PREVIEW_NOT_FOR_A_TASK,
    PREVIEW_ROW_LIMIT,
    preview_needs_submit_query_message,
    preview_of_a_dotted_name_message,
    preview_of_a_dotted_table_name_message,
    preview_of_an_artifact_message,
    preview_parts,
    preview_sql,
    previewed_table,
    submit_preview,
)
from lakana_core.errors import ConflictError, NotFoundError, PermissionDenied
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    Environment,
    Principal,
    PrincipalType,
    QueryRun,
    QueryRunStatus,
    Table,
    TableType,
)
from lakana_core.runs.store import query_completed_detail

_ORDERS = TableRef("dev", NamespacePath.from_parts(["gold"]), "orders")
_MODEL = TableRef("dev", NamespacePath.from_parts(["gold"]), "model")


def _person(session: Session, name: str, role: Role | None = None) -> PrincipalRef:
    """Persist a user and grant it a role on the environment `dev`.

    Parameters
    ----------
    session
        Database session; commits.
    name
        The user's name.
    role
        The role granted on `dev`; `None` grants nothing.

    Returns
    -------
    PrincipalRef
        The user.
    """
    ref = PrincipalRef(id=uuid.uuid4(), name=name, type=PrincipalType.USER)
    session.add(Principal(id=ref.id, type=ref.type, name=ref.name, is_active=True))
    session.commit()
    if role is not None:
        GrantService(session, AllowAllAuthorization()).grant(
            ref, principal=name, role=role, kind=SecurableKind.ENVIRONMENT, environment="dev"
        )
    return ref


def _queued(session: Session) -> int:
    """Count the query runs.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    int
        How many query runs exist.
    """
    return session.scalar(select(func.count()).select_from(QueryRun)) or 0


@pytest.fixture
def orders(session: Session) -> uuid.UUID:
    """Provide the table ``gold.orders`` in `dev` and return its id.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    uuid.UUID
        The table's id.
    """
    return ensure_table(session, "dev", "gold", "orders")[1]


def test_a_viewer_is_refused_with_the_next_step(session: Session, orders: uuid.UUID) -> None:
    """Reading the table is not enough: a preview is a query, and a viewer may run none."""
    viewer = _person(session, "vera", Role.VIEWER)

    with pytest.raises(PermissionDenied) as caught:
        submit_preview(session, GrantAuthorization(session), viewer, _ORDERS)

    assert str(caught.value) == (
        "Preview runs a query as you, which needs the right to run queries in dev. "
        "Ask an administrator for the Analyst role there."
    )
    assert str(caught.value) == preview_needs_submit_query_message("dev")
    assert _queued(session) == 0


def test_an_analyst_may_preview(session: Session, orders: uuid.UUID) -> None:
    """Both rights, through real grants, queue the run."""
    analyst = _person(session, "ana", Role.ANALYST)

    run = submit_preview(session, GrantAuthorization(session), analyst, _ORDERS)

    assert (run.principal_id, run.preview_table_id) == (analyst.id, orders)


def test_read_table_is_checked_before_submit_query(session: Session, orders: uuid.UUID) -> None:
    """A caller who may not read the table is not told about the Analyst role."""
    stranger = _person(session, "sam")

    with pytest.raises(PermissionDenied) as caught:
        submit_preview(session, GrantAuthorization(session), stranger, _ORDERS)

    assert "Analyst role" not in str(caught.value)
    assert _queued(session) == 0


def test_an_unbound_task_token_cannot_preview(
    session: Session, principal: PrincipalRef, orders: uuid.UUID
) -> None:
    """A task-kind token bound to no task has no run context and is still a task."""
    caller = dataclasses.replace(
        principal,
        task_scope=TaskScope(environment="dev", asset_scope=[], token_id=uuid.uuid4()),
        run_context=None,
    )

    with pytest.raises(PermissionDenied) as caught:
        submit_preview(session, AllowAllAuthorization(), caller, _ORDERS)

    assert str(caught.value) == PREVIEW_NOT_FOR_A_TASK
    assert _queued(session) == 0


def test_a_run_context_cannot_preview(
    session: Session, principal: PrincipalRef, orders: uuid.UUID
) -> None:
    """A pipeline task is shown no preview, whatever its principal may do."""
    caller = dataclasses.replace(
        principal, run_context=RunContext(task_run_id=uuid.uuid4(), claim=1)
    )

    with pytest.raises(PermissionDenied) as caught:
        submit_preview(session, AllowAllAuthorization(), caller, _ORDERS)

    assert str(caught.value) == PREVIEW_NOT_FOR_A_TASK
    assert _queued(session) == 0


def test_a_missing_table_is_not_found(session: Session, principal: PrincipalRef) -> None:
    """A reader is told the table does not exist, worded as the catalog words it."""
    ensure_table(session, "dev", "gold", "other")

    with pytest.raises(NotFoundError) as caught:
        submit_preview(session, AllowAllAuthorization(), principal, _ORDERS)

    assert str(caught.value) == "Table 'orders' not found in gold."
    assert _queued(session) == 0


def test_a_table_of_a_deleted_environment_is_not_found(
    session: Session, principal: PrincipalRef, orders: uuid.UUID
) -> None:
    """The table's row outlives its deleted environment; a preview does not reach it."""
    session.execute(update(Environment).values(deleted_at=datetime(2026, 11, 1, tzinfo=UTC)))
    session.commit()

    with pytest.raises(NotFoundError):
        submit_preview(session, AllowAllAuthorization(), principal, _ORDERS)

    assert _queued(session) == 0


def test_an_artifact_has_no_preview(session: Session, principal: PrincipalRef) -> None:
    """An artifact holds files: the refusal names the command that downloads it."""
    _, model = ensure_table(session, "dev", "gold", "model")
    session.execute(update(Table).where(Table.id == model).values(table_type=TableType.ARTIFACT))
    session.commit()

    with pytest.raises(ConflictError) as caught:
        submit_preview(session, AllowAllAuthorization(), principal, _MODEL)

    assert str(caught.value) == (
        "An artifact holds files, not rows; download it with "
        "`lakana artifact download gold.model DIRECTORY --env dev`."
    )
    assert str(caught.value) == preview_of_an_artifact_message("gold.model", "dev")
    assert _queued(session) == 0


def _refusal_of_a_dotted_name(
    session: Session, principal: PrincipalRef, namespace: str, name: str
) -> str:
    """Ask for the preview of a legacy name with a dot, and return the refusal.

    Parameters
    ----------
    session
        Database session.
    principal
        The caller.
    namespace
        The namespace's stored path, one part.
    name
        The table's name.

    Returns
    -------
    str
        The `ConflictError`'s message; no run was queued.
    """
    ensure_table(session, "dev", namespace, name)
    ref = TableRef("dev", NamespacePath.from_parts([namespace], allow_dots=True), name)
    with pytest.raises(ConflictError) as caught:
        submit_preview(session, AllowAllAuthorization(), principal, ref)
    assert _queued(session) == 0
    return str(caught.value)


def test_a_table_in_a_namespace_with_a_dot_is_refused_with_the_cause(
    session: Session, principal: PrincipalRef
) -> None:
    """No query can name such a table yet; the next step is renaming the namespace."""
    refusal = _refusal_of_a_dotted_name(session, principal, "legacy.ns", "t")

    assert refusal == preview_of_a_dotted_name_message("legacy.ns.t")
    assert refusal == (
        "legacy.ns.t is in a namespace whose name contains a dot, which a query cannot name "
        "yet; rename the namespace to preview it."
    )


def test_a_table_whose_own_name_has_a_dot_is_told_to_rename_the_table(
    session: Session, principal: PrincipalRef
) -> None:
    """The dot is in the table's name, so renaming the namespace would not help."""
    refusal = _refusal_of_a_dotted_name(session, principal, "gold", "a.b")

    assert refusal == preview_of_a_dotted_table_name_message("gold.a.b")
    assert refusal == (
        "gold.a.b has a table name that contains a dot, which a query cannot name yet; "
        "rename the table to preview it."
    )


def test_a_preview_run_is_bound_and_carries_the_server_sql(
    session: Session, principal: PrincipalRef, orders: uuid.UUID
) -> None:
    """The server writes the SQL from the catalog row and binds the run to the table's id."""
    run = submit_preview(session, AllowAllAuthorization(), principal, _ORDERS)

    assert run.sql_text == 'SELECT * FROM "gold"."orders" LIMIT 100'
    assert run.preview_table_id == orders
    assert (run.status, run.task_token_id, run.task_run_id, run.check_result_id) == (
        QueryRunStatus.QUEUED,
        None,
        None,
        None,
    )
    assert run.masked_columns is None
    assert previewed_table(run) == "gold.orders"
    assert _queued(session) == 1


def test_an_ordinary_run_previews_no_table() -> None:
    """A run without the binding names no previewed table, whatever its SQL."""
    run = QueryRun(id=uuid.uuid4(), sql_text='SELECT * FROM "gold"."orders" LIMIT 100')

    assert previewed_table(run) is None


@pytest.mark.parametrize(
    ("parts", "name"),
    [(("select",), "from"), (('a"b', "c"), "Mixed"), (("gold", "sales", "eu"), "orders")],
    ids=["keywords", "quote-and-case", "three-levels"],
)
def test_the_preview_sql_quotes_every_part(parts: tuple[str, ...], name: str) -> None:
    """Keywords, quotes, case and deep namespaces name exactly that object.

    Parameters
    ----------
    parts
        The namespace's parts.
    name
        The table's name.
    """
    sql = preview_sql(NamespacePath.from_parts(parts), name)

    assert preview_parts(sql) == (*parts, name)
    assert sql.endswith(f" LIMIT {PREVIEW_ROW_LIMIT}")
    assert PREVIEW_ROW_LIMIT == 100


def test_sql_that_names_no_table_is_refused_when_read_back() -> None:
    """Reading the parts back never answers for SQL that is not a preview's."""
    with pytest.raises(ValueError, match="names no table"):
        preview_parts("SELECT 1")


def test_query_completed_detail_marks_a_preview() -> None:
    """The audit event tells a preview from an ordinary query; an ordinary one is unchanged."""
    preview_run = QueryRun(
        id=uuid.uuid4(),
        sql_text='SELECT * FROM "gold"."orders" LIMIT 100',
        preview_table_id=uuid.uuid4(),
    )
    plain_run = QueryRun(id=uuid.uuid4(), sql_text="SELECT 1")

    assert (
        query_completed_detail(
            preview_run, tables_touched=[], row_count=3, status=QueryRunStatus.SUCCEEDED
        )["preview"]
        is True
    )
    assert "preview" not in query_completed_detail(
        plain_run, tables_touched=[], row_count=3, status=QueryRunStatus.SUCCEEDED
    )
