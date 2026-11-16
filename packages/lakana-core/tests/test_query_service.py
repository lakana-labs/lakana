"""Tests for QueryService."""

import dataclasses
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from core_test_helpers import ActionAllowList, _prepared_catalog, python_task_run
from sqlalchemy import Table, event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    BoundTask,
    PrincipalRef,
    Securable,
    SecurableKind,
    TaskScope,
)
from lakana_core.catalog.identity import resolve_environment_id
from lakana_core.catalog.query_service import (
    QUERY_LIST_ENDED_SECONDS,
    QueryListItem,
    QueryService,
)
from lakana_core.catalog.service import CatalogService
from lakana_core.config import StorageConfig
from lakana_core.db import db_now
from lakana_core.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    DeployKind,
    DeployRun,
    DeployStatus,
    Environment,
    Grant,
    Group,
    GroupMembership,
    Principal,
    PrincipalType,
    QueryOrigin,
    QueryRun,
    QueryRunStatus,
    Repository,
    RepositoryKind,
    Worker,
    WriteMode,
)
from lakana_core.runs import pools
from lakana_core.storage.resolver import StorageResolver


@pytest.fixture
def principal(session: Session) -> PrincipalRef:
    """Provide the acting principal, persisted so `QueryRun.principal_id`'s FK resolves.

    `QueryRun.principal_id` is a real foreign key to `principals.id`, enforced
    even under SQLite (`lakana_core.db` turns the pragma on deliberately). A
    bare `PrincipalRef` is only the auth-layer identity, not a row in the
    table `CatalogService.create_environment` never inserts into either, so
    the row is created here to match the pattern `test_query_runs.py` already
    uses.

    Parameters
    ----------
    session
        Database session to persist the backing `Principal` row into.

    Returns
    -------
    PrincipalRef
        The acting principal, whose id matches a persisted `Principal` row.
    """
    principal_id = uuid.uuid4()
    session.add(Principal(id=principal_id, type=PrincipalType.USER, name="admin"))
    session.commit()
    return PrincipalRef(id=principal_id, name="admin", type=PrincipalType.USER)


def _catalog(session: Session) -> CatalogService:
    return CatalogService(session, StorageResolver(session), AllowAllAuthorization())


def test_submit_inserts_a_queued_row(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Submitting a query authorizes SUBMIT_QUERY and returns a QUEUED row."""
    _catalog(session).create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    service = QueryService(session, AllowAllAuthorization())

    run = service.submit_query(principal, "dev", "SELECT 1")

    assert run.status == QueryRunStatus.QUEUED
    assert run.sql_text == "SELECT 1"
    assert run.principal_id == principal.id


def test_submit_authorizes_submit_query_on_the_environment(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The environment-scoped SUBMIT_QUERY action is checked before the row is inserted."""
    _catalog(session).create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )

    class _Deny:
        def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
            assert action == Action.SUBMIT_QUERY
            assert securable == Securable.environment_scope("dev")
            raise PermissionDenied("denied")

        def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
            """Answer as `authorize` would, as a boolean.

            Parameters
            ----------
            principal
                The actor requesting the action.
            action
                The action requested.
            securable
                The object acted upon.

            Returns
            -------
            bool
                `False` if `authorize` raises `PermissionDenied`, else `True`.
            """
            try:
                self.authorize(principal, action, securable)
            except PermissionDenied:
                return False
            return True

    service = QueryService(session, _Deny())

    with pytest.raises(PermissionDenied):
        service.submit_query(principal, "dev", "SELECT 1")


def test_submit_raises_not_found_for_a_missing_environment(
    session: Session, principal: PrincipalRef
) -> None:
    """Submitting against an environment that does not exist is a 404, not a 500."""
    service = QueryService(session, AllowAllAuthorization())

    with pytest.raises(NotFoundError):
        service.submit_query(principal, "nope", "SELECT 1")


def test_get_query_run_returns_the_submitted_row(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A submitted run can be read back by id."""
    _catalog(session).create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    service = QueryService(session, AllowAllAuthorization())
    submitted = service.submit_query(principal, "dev", "SELECT 1")

    fetched = service.get_query_run(principal, "dev", submitted.id)

    assert fetched.id == submitted.id
    assert fetched.status == QueryRunStatus.QUEUED


def test_get_query_run_raises_not_found_for_a_missing_environment(
    session: Session, principal: PrincipalRef
) -> None:
    """Fetching a run against an environment that does not exist is a 404, not a 500."""
    service = QueryService(session, AllowAllAuthorization())

    with pytest.raises(NotFoundError):
        service.get_query_run(principal, "nope", uuid.uuid4())


def test_get_query_run_raises_not_found_for_an_unknown_id(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A query id from another environment, or that never existed, is a 404."""
    _catalog(session).create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    service = QueryService(session, AllowAllAuthorization())

    with pytest.raises(NotFoundError):
        service.get_query_run(principal, "dev", uuid.uuid4())


def _persisted(session: Session, name: str) -> PrincipalRef:
    """Persist a principal row and return its auth-layer reference.

    Parameters
    ----------
    session
        Database session.
    name
        Principal name.

    Returns
    -------
    PrincipalRef
        Reference whose id matches the persisted row.
    """
    principal_id = uuid.uuid4()
    session.add(Principal(id=principal_id, type=PrincipalType.USER, name=name))
    session.commit()
    return PrincipalRef(id=principal_id, name=name, type=PrincipalType.USER)


def test_another_principals_run_is_not_found(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Bob cannot read alice's run; he gets the same error as for a missing id."""
    _catalog(session).create_environment(
        principal, name="dev", storage=StorageConfig(scheme="file", root=f"file://{tmp_path}")
    )
    service = QueryService(session, AllowAllAuthorization())
    bob = _persisted(session, "bob")
    run = service.submit_query(principal, "dev", "SELECT 1")

    with pytest.raises(NotFoundError) as foreign:
        service.get_query_run(bob, "dev", run.id)
    with pytest.raises(NotFoundError) as missing:
        service.get_query_run(bob, "dev", uuid.uuid4())

    assert type(foreign.value) is type(missing.value)
    assert "admin" not in str(foreign.value)
    assert service.get_query_run(principal, "dev", run.id).id == run.id


def test_task_token_caller_sees_only_its_own_runs(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A task-token caller only sees runs submitted with that same token.

    A run the same principal submitted via a PAT is invisible to it — a
    task token's scope must not implicitly widen to every run its
    principal ever submitted.
    """
    _catalog(session).create_environment(
        principal, name="dev", storage=StorageConfig(scheme="file", root=f"file://{tmp_path}")
    )
    service = QueryService(session, AllowAllAuthorization())
    pat_run = service.submit_query(principal, "dev", "SELECT 1")

    task_principal = PrincipalRef(
        id=principal.id,
        name=principal.name,
        type=principal.type,
        task_scope=TaskScope(environment="dev", asset_scope=["gold.*"], token_id=uuid.uuid4()),
    )
    task_run = service.submit_query(task_principal, "dev", "SELECT 1")

    with pytest.raises(NotFoundError):
        service.get_query_run(task_principal, "dev", pat_run.id)
    assert service.get_query_run(task_principal, "dev", task_run.id).id == task_run.id


def test_a_submitted_read_is_written_and_a_task_tokens_is_task(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The origin comes from the caller's token, never from the request."""
    _catalog(session).create_environment(
        principal, name="dev", storage=StorageConfig(scheme="file", root=f"file://{tmp_path}")
    )
    service = QueryService(session, AllowAllAuthorization())
    task_principal = PrincipalRef(
        id=principal.id,
        name=principal.name,
        type=principal.type,
        task_scope=TaskScope(environment="dev", asset_scope=["gold.*"], token_id=uuid.uuid4()),
    )

    assert service.submit_query(principal, "dev", "SELECT 1").origin is QueryOrigin.WRITTEN
    assert service.submit_query(task_principal, "dev", "SELECT 1").origin is QueryOrigin.TASK


def test_pat_caller_sees_runs_its_tasks_submitted(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A PAT caller sees every run its principal submitted, including via a task token."""
    _catalog(session).create_environment(
        principal, name="dev", storage=StorageConfig(scheme="file", root=f"file://{tmp_path}")
    )
    service = QueryService(session, AllowAllAuthorization())
    task_principal = PrincipalRef(
        id=principal.id,
        name=principal.name,
        type=principal.type,
        task_scope=TaskScope(environment="dev", asset_scope=["gold.*"], token_id=uuid.uuid4()),
    )
    task_run = service.submit_query(task_principal, "dev", "SELECT 1")

    assert service.get_query_run(principal, "dev", task_run.id).id == task_run.id


def test_a_run_with_no_submitter_is_not_found_for_anyone(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A run whose submitter was deleted (principal_id NULL) is readable by nobody."""
    _catalog(session).create_environment(
        principal, name="dev", storage=StorageConfig(scheme="file", root=f"file://{tmp_path}")
    )
    service = QueryService(session, AllowAllAuthorization())
    run = service.submit_query(principal, "dev", "SELECT 1")
    run.principal_id = None
    session.commit()

    with pytest.raises(NotFoundError):
        service.get_query_run(principal, "dev", run.id)


def test_a_query_submitted_with_a_bound_token_is_stamped_with_its_task_run(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    _prepared_catalog(
        session,
        tmp_path,
        AllowAllAuthorization(),
        namespaces=[NamespacePath.from_parts(["bronze"])],
        tables=[TableRef("dev", NamespacePath.from_parts(["bronze"]), "events")],
    )
    task = python_task_run(session, principal.id)
    scope = TaskScope(
        environment="dev",
        asset_scope=[],
        token_id=uuid.uuid4(),
        bound=BoundTask(task.id, 1, "gold.scores", ("bronze.events",), ("bronze.events",)),
    )
    service = QueryService(session, AllowAllAuthorization())
    bound = dataclasses.replace(principal, task_scope=scope)
    stamped = service.submit_query(bound, "dev", "SELECT * FROM bronze.events")
    assert stamped.task_run_id == task.id
    assert stamped.task_token_id == scope.token_id
    unbound = dataclasses.replace(principal, task_scope=dataclasses.replace(scope, bound=None))
    assert service.submit_query(unbound, "dev", "SELECT 1").task_run_id is None
    assert service.submit_query(principal, "dev", "SELECT 1").task_run_id is None


def _queued(session: Session, principal: PrincipalRef, tmp_path: Path) -> QueryRun:
    """Create the environment `dev` and submit one query as `principal`.

    Parameters
    ----------
    session
        Database session.
    principal
        The submitter.
    tmp_path
        The environment's storage root.

    Returns
    -------
    QueryRun
        The queued run.
    """
    _catalog(session).create_environment(
        principal, name="dev", storage=StorageConfig(scheme="file", root=f"file://{tmp_path}")
    )
    return QueryService(session, AllowAllAuthorization()).submit_query(principal, "dev", "SELECT 1")


def _set_status(session: Session, run: QueryRun, status: QueryRunStatus) -> None:
    """Put a run into a status, as the worker would.

    Parameters
    ----------
    session
        Database session.
    run
        The run.
    status
        The status to set.
    """
    run.status = status
    session.commit()


def _cancel_events(session: Session) -> list[AuditEvent]:
    """List the `QUERY_CANCELLED` audit events.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of AuditEvent
        The events, oldest first.
    """
    return list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.event_type == AuditEventType.QUERY_CANCELLED)
            .order_by(AuditEvent.occurred_at)
        )
    )


SUBMITTER = ActionAllowList(Action.SUBMIT_QUERY)
MANAGER = ActionAllowList(Action.MANAGE_ENVIRONMENT)


def test_cancelling_a_queued_run_ends_it_at_once_and_is_audited(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    cancelled = QueryService(session, SUBMITTER).cancel_query(principal, "dev", run.id)
    assert cancelled.status is QueryRunStatus.CANCELLED
    assert cancelled.cancel_requested is True
    assert (cancelled.error_cause, cancelled.error_message) == (
        "cancelled",
        "The query was cancelled.",
    )
    assert cancelled.finished_at is not None
    (event,) = _cancel_events(session)
    assert event.principal_id == principal.id
    assert event.detail == {
        "query_id": str(run.id),
        "status": "QUEUED",
        "submitted_by": str(principal.id),
    }


def test_cancelling_a_running_run_only_sets_the_flag(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    _set_status(session, run, QueryRunStatus.RUNNING)
    asked = QueryService(session, SUBMITTER).cancel_query(principal, "dev", run.id)
    assert (asked.status, asked.cancel_requested) == (QueryRunStatus.RUNNING, True)
    assert (asked.error_cause, asked.error_message, asked.finished_at) == (None, None, None)
    assert [event.detail["status"] for event in _cancel_events(session)] == ["RUNNING"]


@pytest.mark.parametrize(
    "status", [QueryRunStatus.SUCCEEDED, QueryRunStatus.FAILED, QueryRunStatus.CANCELLED]
)
def test_cancelling_a_run_that_has_ended_changes_nothing_and_is_no_error(
    session: Session, principal: PrincipalRef, tmp_path: Path, status: QueryRunStatus
) -> None:
    run = _queued(session, principal, tmp_path)
    _set_status(session, run, status)
    same = QueryService(session, SUBMITTER).cancel_query(principal, "dev", run.id)
    assert (same.status, same.cancel_requested, same.error_cause) == (status, False, None)
    assert _cancel_events(session) == []


def test_another_principal_gets_the_answer_of_a_status_read_and_changes_nothing(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    bob = _persisted(session, "bob")
    service = QueryService(session, SUBMITTER)
    with pytest.raises(NotFoundError) as foreign:
        service.cancel_query(bob, "dev", run.id)
    with pytest.raises(NotFoundError) as read:
        service.get_query_run(bob, "dev", run.id)
    assert str(foreign.value) == str(read.value) == f"No query run {run.id} in 'dev'."
    session.refresh(run)
    assert (run.status, run.cancel_requested) == (QueryRunStatus.QUEUED, False)
    assert _cancel_events(session) == []


def test_a_caller_who_may_not_read_queries_is_refused_as_for_a_status_read(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    with pytest.raises(PermissionDenied):
        QueryService(session, ActionAllowList()).cancel_query(principal, "dev", run.id)
    session.refresh(run)
    assert run.status is QueryRunStatus.QUEUED


def test_a_holder_of_manage_environment_cancels_anyones_run(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    operator = _persisted(session, "operator")
    authz = ActionAllowList(Action.MANAGE_ENVIRONMENT)
    cancelled = QueryService(session, authz).cancel_query(operator, "dev", run.id)
    assert cancelled.status is QueryRunStatus.CANCELLED
    assert [action for action, _securable in authz.seen] == [Action.MANAGE_ENVIRONMENT]
    (event,) = _cancel_events(session)
    assert event.principal_id == operator.id
    assert event.detail["submitted_by"] == str(principal.id)


def test_a_manager_cancels_a_run_whose_submitter_was_deleted(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    operator = _persisted(session, "operator")
    run.principal_id = None
    session.commit()
    cancelled = QueryService(session, MANAGER).cancel_query(operator, "dev", run.id)
    assert cancelled.status is QueryRunStatus.CANCELLED
    assert _cancel_events(session)[0].detail["submitted_by"] is None


def test_a_manager_gets_not_found_for_an_unknown_run_and_an_unknown_environment(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    _queued(session, principal, tmp_path)
    service = QueryService(session, MANAGER)
    missing = uuid.uuid4()
    with pytest.raises(NotFoundError, match=f"No query run {missing} in 'dev'"):
        service.cancel_query(principal, "dev", missing)
    with pytest.raises(NotFoundError, match="No environment named 'nowhere'"):
        service.cancel_query(principal, "nowhere", missing)


def test_a_task_token_cancels_only_what_it_submitted_whatever_its_principal_holds(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A task token never takes the manager's path."""
    pat_run = _queued(session, principal, tmp_path)
    everything = ActionAllowList(Action.SUBMIT_QUERY, Action.MANAGE_ENVIRONMENT)
    service = QueryService(session, everything)
    token = PrincipalRef(
        id=principal.id,
        name=principal.name,
        type=principal.type,
        task_scope=TaskScope(environment="dev", asset_scope=["gold.*"], token_id=uuid.uuid4()),
    )
    own = service.submit_query(token, "dev", "SELECT 1")
    with pytest.raises(NotFoundError):
        service.cancel_query(token, "dev", pat_run.id)
    session.refresh(pat_run)
    assert pat_run.status is QueryRunStatus.QUEUED
    assert service.cancel_query(token, "dev", own.id).status is QueryRunStatus.CANCELLED
    assert Action.MANAGE_ENVIRONMENT not in [action for action, _securable in everything.seen]


def test_the_write_of_a_pipeline_task_is_cancelled_through_its_run(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    task = python_task_run(session, principal.id, kind="sql")
    run.task_run_id = task.id
    run.write_mode = WriteMode.REPLACE
    session.flush()
    task.query_run_id = run.id
    session.commit()
    with pytest.raises(ValidationError) as refused:
        QueryService(session, MANAGER).cancel_query(principal, "dev", run.id)
    assert str(refused.value) == (
        f"Query run {run.id} is the write of a task in pipeline run {task.run_id}, not a query "
        f"of its own. Cancel that run instead: lakana runs cancel {task.run_id} --env dev"
    )
    session.refresh(run)
    assert (run.status, run.cancel_requested) == (QueryRunStatus.QUEUED, False)


@pytest.mark.parametrize("shape", ["write-mode-only", "bound-only"])
def test_a_run_that_has_a_write_mode_or_is_the_tasks_bound_run_is_still_called_its_write(
    session: Session, principal: PrincipalRef, tmp_path: Path, shape: str
) -> None:
    """Either mark of a write is enough: only a run with neither is the count of a check."""
    run = _queued(session, principal, tmp_path)
    task = python_task_run(session, principal.id, kind="sql")
    run.task_run_id = task.id
    if shape == "write-mode-only":
        run.write_mode = WriteMode.APPEND
    else:
        session.flush()
        task.query_run_id = run.id
    session.commit()
    with pytest.raises(ValidationError, match="is the write of a task in pipeline run"):
        QueryService(session, MANAGER).cancel_query(principal, "dev", run.id)


def test_the_count_of_a_check_is_cancelled_through_its_run_and_is_not_called_a_write(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A count has no write mode and is not the task's `query_run_id`."""
    run = _queued(session, principal, tmp_path)
    task = python_task_run(session, principal.id, kind="sql")
    run.task_run_id = task.id
    session.commit()
    with pytest.raises(ValidationError) as refused:
        QueryService(session, MANAGER).cancel_query(principal, "dev", run.id)
    assert str(refused.value) == (
        f"Query run {run.id} counts a check of a task in pipeline run {task.run_id}; it is not "
        f"a query of its own. Cancel that run instead: lakana runs cancel {task.run_id} --env dev"
    )
    session.refresh(run)
    assert (run.status, run.cancel_requested) == (QueryRunStatus.QUEUED, False)


def test_a_query_a_python_task_submitted_with_its_token_can_be_cancelled(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    task = python_task_run(session, principal.id)
    run.task_run_id = task.id
    run.task_token_id = uuid.uuid4()
    session.commit()
    cancelled = QueryService(session, MANAGER).cancel_query(principal, "dev", run.id)
    assert cancelled.status is QueryRunStatus.CANCELLED


def test_a_second_request_while_running_is_audited_again(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    _set_status(session, run, QueryRunStatus.RUNNING)
    operator = _persisted(session, "operator")
    QueryService(session, SUBMITTER).cancel_query(principal, "dev", run.id)
    QueryService(session, MANAGER).cancel_query(operator, "dev", run.id)
    assert [event.principal_id for event in _cancel_events(session)] == [principal.id, operator.id]


def test_a_maintenance_run_is_cancelled_while_queued_and_refused_once_it_runs(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Maintenance runs in the worker's own process; nothing can stop it once it has started."""
    run = _queued(session, principal, tmp_path)
    run.write_mode = WriteMode.MAINTAIN
    run.status = QueryRunStatus.RUNNING
    session.commit()
    service = QueryService(session, SUBMITTER)
    with pytest.raises(ValidationError) as refused:
        service.cancel_query(principal, "dev", run.id)
    assert str(refused.value) == (
        f"Query run {run.id} is a maintenance run that has already started. It cannot be "
        f"stopped and ends by itself; follow it with: lakana query status {run.id} --env dev"
    )
    session.refresh(run)
    assert run.cancel_requested is False
    assert _cancel_events(session) == []
    _set_status(session, run, QueryRunStatus.QUEUED)
    assert service.cancel_query(principal, "dev", run.id).status is QueryRunStatus.CANCELLED


def _denials(session: Session) -> list[AuditEvent]:
    """List the `AUTHORIZATION_DENIED` audit events.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of AuditEvent
        The events.
    """
    return list(
        session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.AUTHORIZATION_DENIED)
        )
    )


def test_a_manager_of_one_environment_cannot_cancel_a_run_of_another(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    _catalog(session).create_environment(
        principal, name="other", storage=StorageConfig(scheme="file", root=f"file://{tmp_path}/o")
    )
    operator = _persisted(session, "operator")
    with pytest.raises(NotFoundError) as refused:
        QueryService(session, MANAGER).cancel_query(operator, "other", run.id)
    assert str(refused.value) == f"No query run {run.id} in 'other'."
    session.refresh(run)
    assert (run.status, run.cancel_requested) == (QueryRunStatus.QUEUED, False)
    assert _cancel_events(session) == []


def test_a_refused_cancel_is_audited_alike_for_a_foreign_and_an_unknown_run(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The caller sees one answer; the audit row is the same for both cases (no oracle)."""
    run = _queued(session, principal, tmp_path)
    bob = _persisted(session, "bob")
    authz = AuditingAuthorization(SUBMITTER, session, DenialAuditLimiter(10_000))
    service = QueryService(session, authz)
    answers = []
    for query_id in (run.id, uuid.uuid4()):
        with pytest.raises(NotFoundError) as refused:
            service.cancel_query(bob, "dev", query_id)
        answers.append(str(refused.value).replace(str(query_id), "<id>"))
    assert answers[0] == answers[1] == "No query run <id> in 'dev'."
    first, second = _denials(session)
    assert (
        first.detail == second.detail == {"action": "MANAGE_ENVIRONMENT", "reason": "not_submitter"}
    )
    assert first.principal_id == second.principal_id == bob.id
    assert (first.securable_kind, first.securable_environment) == (
        second.securable_kind,
        second.securable_environment,
    )
    session.refresh(run)
    assert run.status is QueryRunStatus.QUEUED
    assert _cancel_events(session) == []


def test_refused_cancels_go_through_the_rate_limiter(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    bob = _persisted(session, "bob")
    service = QueryService(
        session, AuditingAuthorization(SUBMITTER, session, DenialAuditLimiter(1))
    )
    for _ in range(5):
        with pytest.raises(NotFoundError):
            service.cancel_query(bob, "dev", run.id)
    assert len(_denials(session)) == 1


def test_an_unknown_environment_and_a_plain_submitter_cancel_are_not_denials(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    service = QueryService(
        session, AuditingAuthorization(SUBMITTER, session, DenialAuditLimiter(100))
    )
    with pytest.raises(NotFoundError, match="No environment named 'nowhere'"):
        service.cancel_query(principal, "nowhere", run.id)
    assert service.cancel_query(principal, "dev", run.id).status is QueryRunStatus.CANCELLED
    assert _denials(session) == []


def _held(
    session: Session,
    run: QueryRun,
    *,
    heartbeat_age: int | None,
    **fields: object,
) -> None:
    """Make a queued run `RUNNING` under the worker `w`, whose heartbeat has that age.

    Parameters
    ----------
    session
        Database session.
    run
        The run.
    heartbeat_age
        Seconds since the worker's heartbeat by the database's clock; `None` inserts no worker
        row.
    **fields
        More column values to set on the run (`write_mode`, `rows_written`).
    """
    if heartbeat_age is not None:
        beat = db_now(session) - timedelta(seconds=heartbeat_age)
        session.add(Worker(id="w", host="h", version="1", started_at=beat, heartbeat_at=beat))
    run.status = QueryRunStatus.RUNNING
    run.worker_id = "w"
    for name, value in fields.items():
        setattr(run, name, value)
    session.commit()


def _completions(session: Session) -> list[AuditEvent]:
    """List the `QUERY_COMPLETED` audit events.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of AuditEvent
        The events, oldest first.
    """
    return list(
        session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.QUERY_COMPLETED)
        )
    )


def test_cancelling_a_running_run_whose_worker_is_gone_ends_it_at_once(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    _held(session, run, heartbeat_age=61)
    cancelled = QueryService(session, SUBMITTER).cancel_query(principal, "dev", run.id)
    assert (cancelled.status, cancelled.cancel_requested) == (QueryRunStatus.CANCELLED, True)
    assert (cancelled.error_cause, cancelled.error_message) == (
        "cancelled",
        "The query was cancelled.",
    )
    assert cancelled.finished_at is not None
    (event,) = _cancel_events(session)
    assert event.detail["status"] == "RUNNING"
    # Exactly one completion event, as for a run its own worker cancels.
    (done,) = _completions(session)
    assert done.detail["status"] == "CANCELLED"


def test_cancelling_a_running_run_of_an_alive_worker_only_asks(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    _held(session, run, heartbeat_age=20)
    asked = QueryService(session, SUBMITTER).cancel_query(principal, "dev", run.id)
    assert (asked.status, asked.cancel_requested) == (QueryRunStatus.RUNNING, True)
    assert asked.finished_at is None
    assert _completions(session) == []


def test_a_started_maintenance_run_whose_worker_is_gone_can_be_cancelled(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    _held(session, run, heartbeat_age=61, write_mode=WriteMode.MAINTAIN)
    cancelled = QueryService(session, SUBMITTER).cancel_query(principal, "dev", run.id)
    assert cancelled.status is QueryRunStatus.CANCELLED


def test_a_started_maintenance_run_of_an_alive_worker_is_still_refused(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    _held(session, run, heartbeat_age=5, write_mode=WriteMode.MAINTAIN)
    with pytest.raises(ValidationError, match="maintenance run that has already started"):
        QueryService(session, SUBMITTER).cancel_query(principal, "dev", run.id)


@pytest.mark.parametrize("rows", [3, 0])
def test_cancelling_a_lost_write_that_committed_ends_it_succeeded_not_cancelled(
    session: Session, principal: PrincipalRef, tmp_path: Path, rows: int
) -> None:
    """R4.7.4: CANCELLED over a committed write would invite a second append."""
    run = _queued(session, principal, tmp_path)
    _held(
        session,
        run,
        heartbeat_age=61,
        write_mode=WriteMode.APPEND,
        target_name="s.o",
        rows_written=rows,
    )
    answer = QueryService(session, SUBMITTER).cancel_query(principal, "dev", run.id)
    assert (answer.status, answer.cancel_requested) == (QueryRunStatus.SUCCEEDED, True)
    assert (answer.error_cause, answer.error_message) == (None, None)
    assert answer.finished_at is not None and answer.rows_written == rows
    (event,) = _cancel_events(session)
    assert event.detail["status"] == "RUNNING"
    (done,) = _completions(session)
    assert done.detail["status"] == "SUCCEEDED" and done.detail["row_count"] == rows


def test_a_committed_write_of_an_alive_worker_only_asks(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    _held(session, run, heartbeat_age=5, write_mode=WriteMode.APPEND, rows_written=2)
    answer = QueryService(session, SUBMITTER).cancel_query(principal, "dev", run.id)
    assert (answer.status, answer.cancel_requested) == (QueryRunStatus.RUNNING, True)


def test_the_cancel_judges_alive_by_the_services_lease(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    _held(session, run, heartbeat_age=6)
    cancelled = QueryService(session, SUBMITTER, lease_seconds=5).cancel_query(
        principal, "dev", run.id
    )
    assert cancelled.status is QueryRunStatus.CANCELLED


# -- wait reasons ----------------------------------------------------


def _statements(session: Session, call: Callable[[], object]) -> list[str]:
    """Run `call` and return the SQL statements it issued.

    Parameters
    ----------
    session
        Database session.
    call
        What to run.

    Returns
    -------
    list of str
        The statements, in order.
    """
    seen: list[str] = []

    def record(conn: object, cursor: object, statement: str, *args: object) -> None:
        seen.append(statement)

    bind = session.get_bind()
    event.listen(bind, "before_cursor_execute", record)
    try:
        call()
    finally:
        event.remove(bind, "before_cursor_execute", record)
    return seen


def _alive_worker(session: Session, worker_id: str, age: int = 5, takes: str = "all") -> None:
    """Insert a worker whose heartbeat is `age` seconds old by the database's clock.

    Parameters
    ----------
    session
        Database session.
    worker_id
        Its id.
    age
        Seconds since the heartbeat.
    takes
        What it takes (`all`, `interactive` or `pipeline`).
    """
    beat = db_now(session) - timedelta(seconds=age)
    session.add(
        Worker(id=worker_id, host="h", version="1", started_at=beat, heartbeat_at=beat, takes=takes)
    )
    session.commit()


def _other_work(
    session: Session, run: QueryRun, *, query_ahead: int, deploy_ahead: int, later: int
) -> None:
    """Queue queries and deploys in another environment, older or younger than `run`.

    Parameters
    ----------
    session
        Database session.
    run
        The query asked about.
    query_ahead
        Queued queries of another environment older than `run`.
    deploy_ahead
        Queued deploys older than `run`.
    later
        Queued queries younger than `run`.
    """
    other = Environment(
        id=uuid.uuid4(),
        name="prod",
        storage_root="file:///tmp/lakana-prod",
        storage_options={},
        is_protected=False,
        properties={},
    )
    repo = Repository(id=uuid.uuid4(), name="r", kind=RepositoryKind.HTTPS, url="https://h/r.git")
    session.add_all([other, repo])
    session.flush()
    for n in range(query_ahead):
        session.add(
            QueryRun(
                origin=QueryOrigin.WRITTEN,
                id=uuid.uuid4(),
                environment_id=other.id,
                sql_text="SELECT 2",
                status=QueryRunStatus.QUEUED,
                created_at=run.created_at - timedelta(seconds=n + 1),
            )
        )
    for n in range(deploy_ahead):
        session.add(
            DeployRun(
                id=uuid.uuid4(),
                environment_id=other.id,
                repository_id=repo.id,
                kind=DeployKind.DEPLOY,
                status=DeployStatus.QUEUED,
                created_at=run.created_at - timedelta(seconds=n + 1),
            )
        )
    for n in range(later):
        session.add(
            QueryRun(
                origin=QueryOrigin.WRITTEN,
                id=uuid.uuid4(),
                environment_id=other.id,
                sql_text="SELECT 3",
                status=QueryRunStatus.QUEUED,
                created_at=run.created_at + timedelta(seconds=n + 1),
            )
        )
    session.commit()


def _busy_worker(session: Session, worker_id: str = "busy", age: int = 5) -> None:
    """Insert an alive worker that carries a `RUNNING` query run.

    Parameters
    ----------
    session
        Database session.
    worker_id
        The worker's id.
    age
        Seconds since its heartbeat.
    """
    _alive_worker(session, worker_id, age)
    env = session.scalars(select(Environment).where(Environment.name == "dev")).one()
    session.add(
        QueryRun(
            origin=QueryOrigin.WRITTEN,
            id=uuid.uuid4(),
            environment_id=env.id,
            sql_text="SELECT 9",
            status=QueryRunStatus.RUNNING,
            worker_id=worker_id,
        )
    )
    session.commit()


def test_a_queued_run_has_a_wait_reason_and_any_other_status_has_none(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    service = QueryService(session, AllowAllAuthorization())
    reason = service.waiting_for(run)
    assert reason is not None and reason.code == "no_worker"
    for status in (
        QueryRunStatus.RUNNING,
        QueryRunStatus.SUCCEEDED,
        QueryRunStatus.FAILED,
        QueryRunStatus.CANCELLED,
    ):
        _set_status(session, run, status)
        session.refresh(run)
        issued = _statements(session, lambda: service.waiting_for(run))
        assert service.waiting_for(run) is None
        assert issued == []


def test_ahead_counts_queued_queries_and_deploys_of_every_environment(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    _busy_worker(session)
    _other_work(session, run, query_ahead=1, deploy_ahead=1, later=1)
    reason = QueryService(session, AllowAllAuthorization()).waiting_for(run)
    assert reason is not None
    assert reason.to_json() == {
        "code": "workers_busy",
        "message": "The only worker is busy, and about 2 queries or deploys are ahead of this one.",
        "workers": 1,
        "ahead": 2,
    }


def test_a_free_worker_and_queries_ahead_is_starting_with_the_number_ahead(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    _alive_worker(session, "free")
    _other_work(session, run, query_ahead=3, deploy_ahead=0, later=0)
    reason = QueryService(session, AllowAllAuthorization()).waiting_for(run)
    assert reason is not None
    assert (reason.code, reason.workers, reason.ahead) == ("starting", 1, 3)


def test_a_dead_worker_counts_as_busy_until_its_lease_is_over(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    _busy_worker(session, age=30)
    service = QueryService(session, AllowAllAuthorization())
    reason = service.waiting_for(run)
    assert reason is not None
    assert reason.message == "The only worker is busy, and nothing is ahead of this one."
    worker = session.get(Worker, "busy")
    assert worker is not None
    worker.heartbeat_at = db_now(session) - timedelta(seconds=61)
    session.commit()
    reason = service.waiting_for(run)
    assert reason is not None and reason.code == "no_worker"


def test_the_reason_judges_alive_by_the_services_lease(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    _alive_worker(session, "w", age=10)
    reason = QueryService(session, AllowAllAuthorization(), lease_seconds=5).waiting_for(run)
    assert reason is not None and reason.code == "no_worker"
    reason = QueryService(session, AllowAllAuthorization(), lease_seconds=20).waiting_for(run)
    assert reason is not None and reason.code == "starting"


def test_a_worker_that_supervises_a_task_is_free_for_a_query(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    _alive_worker(session, "w")
    task = python_task_run(session, principal.id)
    task.lease_owner = "w"
    session.commit()
    reason = QueryService(session, AllowAllAuthorization()).waiting_for(run)
    assert reason is not None
    assert reason.code == "starting"


def test_the_reason_of_a_queued_run_costs_a_bounded_number_of_statements(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    service = QueryService(session, AllowAllAuthorization())
    _other_work(session, run, query_ahead=5, deploy_ahead=5, later=5)
    session.refresh(run)
    # now + alive workers: nothing else is read when no worker is alive
    assert len(_statements(session, lambda: service.waiting_for(run))) == 2
    _busy_worker(session)
    session.refresh(run)
    # now + workers + (running queries, running deploys, task leases) + ahead; every worker is
    # busy, so the principal's running count is not read
    assert len(_statements(session, lambda: service.waiting_for(run))) == 6
    _alive_worker(session, "free")
    session.refresh(run)
    # a worker is free: also the principal's running count, which does not reach the limit
    assert len(_statements(session, lambda: service.waiting_for(run))) == 7


def test_the_statements_of_each_case_of_a_queued_run(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    service = QueryService(session, AllowAllAuthorization(), running_per_principal=1)
    _alive_worker(session, "pipeline-only", takes="pipeline")
    session.refresh(run)
    # no worker of the kind: the clock and the workers
    assert len(_statements(session, lambda: service.waiting_for(run))) == 2
    _alive_worker(session, "w")
    _running_query(session, run, principal, "w")
    session.refresh(run)
    # at the limit with the only interactive worker busy: the clock, the workers, what they
    # carry and what is ahead; no count of the principal's queries
    reason = service.waiting_for(run)
    assert reason is not None and reason.code == "workers_busy"
    assert len(_statements(session, lambda: service.waiting_for(run))) == 6
    session.query(QueryRun).filter(QueryRun.worker_id == "w").update({"worker_id": "gone"})
    session.commit()
    session.refresh(run)
    # the same limit held by the run of a lost worker, a worker free: the clock, the workers,
    # what they carry and the count; the limit decides, nothing is counted ahead
    reason = service.waiting_for(run)
    assert reason is not None and reason.code == "user_limit"
    assert len(_statements(session, lambda: service.waiting_for(run))) == 6
    maintenance = QueryService(session, AllowAllAuthorization()).submit_query(
        principal, "dev", "VACUUM"
    )
    maintenance.write_mode = WriteMode.MAINTAIN
    session.commit()
    session.refresh(maintenance)
    # a pipeline run has no count: the clock, the workers, three of the work, the count ahead
    assert len(_statements(session, lambda: service.waiting_for(maintenance))) == 6


def _running_query(
    session: Session,
    queued: QueryRun,
    principal: PrincipalRef,
    worker_id: str | None = None,
    environment_id: uuid.UUID | None = None,
) -> QueryRun:
    """Insert an ad-hoc query run of a principal that is `RUNNING`, in the queued run's environment.

    Parameters
    ----------
    session
        Database session.
    queued
        A run whose environment is used.
    principal
        The submitter.
    worker_id
        The worker that carries it, if any.
    environment_id
        The environment it runs in; the queued run's when omitted.

    Returns
    -------
    QueryRun
        The committed run.
    """
    run = QueryRun(
        origin=QueryOrigin.WRITTEN,
        id=uuid.uuid4(),
        environment_id=environment_id or queued.environment_id,
        principal_id=principal.id,
        sql_text="SELECT 9",
        status=QueryRunStatus.RUNNING,
        worker_id=worker_id,
    )
    session.add(run)
    session.commit()
    return run


def test_a_query_on_an_instance_of_pipeline_workers_says_no_worker_of_kind(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    _alive_worker(session, "p1", takes="pipeline")
    _alive_worker(session, "p2", takes="pipeline")
    reason = QueryService(session, AllowAllAuthorization()).waiting_for(run)
    assert reason is not None
    assert reason.to_json() == {
        "code": "no_worker_of_kind",
        "message": (
            "No worker that takes interactive work is running (2 workers take pipeline work "
            "only), so this query cannot start. Ask the operator to start a worker with "
            "LAKANA_WORKER__TAKES=interactive or all."
        ),
        "workers": 0,
        "other_workers": 2,
    }


def test_a_maintenance_run_on_interactive_workers_says_this_maintenance_run(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    run.write_mode = WriteMode.MAINTAIN
    session.commit()
    _alive_worker(session, "i1", takes="interactive")
    reason = QueryService(session, AllowAllAuthorization()).waiting_for(run)
    assert reason is not None
    assert (reason.code, reason.other_workers) == ("no_worker_of_kind", 1)
    assert reason.message == (
        "No worker that takes pipeline work is running (1 worker takes interactive work only), "
        "so this maintenance run cannot start. Ask the operator to start a worker with "
        "LAKANA_WORKER__TAKES=pipeline or all."
    )


def test_a_query_run_of_a_task_is_pipeline_work(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    task = python_task_run(session, principal.id)
    run.task_run_id = task.id
    session.commit()
    _alive_worker(session, "i1", takes="interactive")
    reason = QueryService(session, AllowAllAuthorization()).waiting_for(run)
    assert reason is not None
    assert reason.code == "no_worker_of_kind"
    assert "takes pipeline work" in reason.message


def test_a_capped_caller_sees_user_limit_with_their_numbers(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    _alive_worker(session, "w1")
    _alive_worker(session, "w2")
    _alive_worker(session, "w3")
    _running_query(session, run, principal, "w1")
    _running_query(session, run, principal, "w2")
    reason = QueryService(session, AllowAllAuthorization(), running_per_principal=2).waiting_for(
        run
    )
    assert reason is not None
    assert reason.to_json() == {
        "code": "user_limit",
        "message": (
            "You have 2 queries running, which is the limit per user here; this one can start "
            "when one of them ends. 'lakana query list' shows them."
        ),
        "running": 2,
        "limit": 2,
    }


def test_a_principal_passed_by_after_a_lock_timeout_sees_the_lock_not_starting(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """#95: a worker is free, the claim lock timed out and the worker wrote the pass-by time."""
    run = _queued(session, principal, tmp_path)
    _alive_worker(session, "w1")
    service = QueryService(session, AllowAllAuthorization())
    first = service.waiting_for(run)
    assert first is not None and first.code == "starting"

    pools.pass_by(session, principal.id, 30)
    reason = service.waiting_for(run)

    assert reason is not None
    body = reason.to_json()
    assert (body["code"], body["workers"]) == ("lock_timeout", 1)
    assert "lock that serialises the start of this user's queries" in str(body["message"])
    assert reason.next_attempt_at is not None
    assert reason.next_attempt_at > db_now(session)
    stored = session.get(Principal, principal.id)
    assert stored is not None
    stored.query_passed_by_until = db_now(session) - timedelta(seconds=1)
    session.commit()
    ended = service.waiting_for(run)
    assert ended is not None and ended.code == "starting"


def test_the_user_limit_comes_before_the_pass_by(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A reached limit is the truer reason while the pass-by time lasts."""
    run = _queued(session, principal, tmp_path)
    _alive_worker(session, "w1")
    _alive_worker(session, "w2")
    _running_query(session, run, principal, "w1")
    pools.pass_by(session, principal.id, 30)
    reason = QueryService(session, AllowAllAuthorization(), running_per_principal=1).waiting_for(
        run
    )
    assert reason is not None and reason.code == "user_limit"


def test_a_listing_shows_the_lock_for_a_principal_passed_by_and_starting_for_others(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """One extra statement for the listing, whatever the number of rows."""
    env = _env(session, principal, tmp_path, "dev")
    _alive_worker(session, "w")
    mine = _add_run(session, env, principal, age=100)
    pools.pass_by(session, principal.id, 30)
    rows = QueryService(session, SUBMITTER).list_queries(principal, "dev")
    reason = next(row.waiting_for for row in rows if row.id == mine.id)
    assert reason is not None
    assert (reason.code, reason.next_attempt_at is not None) == ("lock_timeout", True)


def test_the_limit_under_auto_follows_the_alive_interactive_workers(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    service = QueryService(session, AllowAllAuthorization())
    _alive_worker(session, "w1")
    _alive_worker(session, "w2")
    _running_query(session, run, principal, "w1")
    reason = service.waiting_for(run)
    assert reason is not None
    assert reason.code == "starting"  # one running, a limit of 2: not capped
    _running_query(session, run, principal, "w2")
    reason = service.waiting_for(run)
    assert reason is not None
    # two workers, both busy with this user's queries: no reserve, the busy sentence as before
    assert (reason.code, reason.limit) == ("workers_busy", None)
    # a third worker: auto reserves one, the limit is 2, and a worker is free
    _alive_worker(session, "w3")
    reason = service.waiting_for(run)
    assert reason is not None
    assert (reason.code, reason.limit, reason.running) == ("user_limit", 2, 2)
    # a pipeline-only worker is not one of the N
    _alive_worker(session, "p", takes="pipeline")
    reason = service.waiting_for(run)
    assert reason is not None
    assert (reason.code, reason.limit) == ("user_limit", 2)
    # a fourth interactive worker: three of four
    _alive_worker(session, "w4", takes="interactive")
    reason = service.waiting_for(run)
    assert reason is not None
    assert reason.code == "starting"


def test_a_default_install_keeps_the_busy_sentence_for_a_users_second_query(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    _alive_worker(session, "only")
    _running_query(session, run, principal, "only")
    reason = QueryService(session, AllowAllAuthorization()).waiting_for(run)
    assert reason is not None
    assert reason.to_json() == {
        "code": "workers_busy",
        "message": "The only worker is busy, and nothing is ahead of this one.",
        "workers": 1,
        "ahead": 0,
    }


def test_at_the_limit_and_busy_the_reason_counts_what_other_people_have_ahead(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run, _maintenance = _older_work(session, principal, tmp_path)
    _alive_worker(session, "only")
    _running_query(session, run, principal, "only")
    reason = QueryService(session, AllowAllAuthorization()).waiting_for(run)
    assert reason is not None
    assert (reason.code, reason.ahead) == ("workers_busy", 5)


def test_a_fixed_limit_of_one_on_two_workers_says_user_limit_while_the_other_is_free(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    _alive_worker(session, "w1")
    _alive_worker(session, "w2")
    _running_query(session, run, principal, "w1")
    reason = QueryService(session, AllowAllAuthorization(), running_per_principal=1).waiting_for(
        run
    )
    assert reason is not None
    assert (reason.code, reason.running, reason.limit) == ("user_limit", 1, 1)


def test_a_lost_workers_run_holds_the_limit_while_a_worker_is_free(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    _alive_worker(session, "alive")
    _running_query(session, run, principal, "dead")  # no such worker is alive any more
    reason = QueryService(session, AllowAllAuthorization()).waiting_for(run)
    assert reason is not None
    assert (reason.code, reason.running, reason.limit) == ("user_limit", 1, 1)


def test_the_count_spans_every_environment(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    prod = _env(session, principal, tmp_path, "prod")
    _alive_worker(session, "w")
    _running_query(session, run, principal, "gone", environment_id=prod.id)
    reason = QueryService(session, AllowAllAuthorization()).waiting_for(run)
    assert reason is not None
    assert (reason.code, reason.running, reason.limit) == ("user_limit", 1, 1)
    listed = QueryService(session, SUBMITTER).list_queries(principal, "dev")
    entry = listed[0].waiting_for
    assert entry is not None
    assert (entry.code, entry.running, entry.limit) == ("user_limit", 1, 1)


def test_a_listings_auto_counts_only_workers_that_take_interactive_work(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """One `all` and one pipeline-only worker: `auto` is 1, not 2 (the mutant: over all alive)."""
    run = _queued(session, principal, tmp_path)
    _alive_worker(session, "interactive-capable")
    _alive_worker(session, "pipeline-only", takes="pipeline")
    _running_query(session, run, principal, "gone")
    status = QueryService(session, AllowAllAuthorization()).waiting_for(run)
    listed = QueryService(session, SUBMITTER).list_queries(principal, "dev")
    entry = next(row.waiting_for for row in listed if row.id == run.id)
    assert status is not None and entry is not None
    assert (entry.code, entry.running, entry.limit) == ("user_limit", 1, 1)
    assert entry == status


def test_a_worker_with_an_unknown_takes_is_not_named_as_taking_the_other_kind(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    _alive_worker(session, "p", takes="pipeline")
    _alive_worker(session, "x", takes="something-newer")
    reason = QueryService(session, AllowAllAuthorization()).waiting_for(run)
    assert reason is not None
    assert reason.code == "no_worker_of_kind"
    assert "(2 workers take other work only)" in reason.message


def test_the_cap_now_is_the_one_function_the_worker_uses(
    session: Session, principal: PrincipalRef, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason resolves the limit with `pools.cap_for`, never a second implementation."""
    run = _queued(session, principal, tmp_path)
    _alive_worker(session, "w1")
    seen: list[tuple[object, int]] = []

    def cap_for(setting: object, interactive_workers: int) -> int:
        seen.append((setting, interactive_workers))
        return 7

    monkeypatch.setattr("lakana_core.catalog.query_service.pools.cap_for", cap_for)
    reason = QueryService(session, AllowAllAuthorization(), running_per_principal=4).waiting_for(
        run
    )
    assert seen == [(4, 1)]
    assert reason is not None and reason.code == "starting"


def test_a_run_without_a_principal_has_no_limit(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    _alive_worker(session, "w1")
    _running_query(session, run, principal, "w1")
    run.principal_id = None
    session.commit()
    reason = QueryService(session, AllowAllAuthorization(), running_per_principal=1).waiting_for(
        run
    )
    assert reason is not None
    assert reason.code == "workers_busy"
    # no principal: no count, so six statements and not seven
    service = QueryService(session, AllowAllAuthorization(), running_per_principal=1)
    session.refresh(run)
    assert len(_statements(session, lambda: service.waiting_for(run))) == 6


def test_a_pipeline_run_of_a_capped_principal_is_not_capped(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    _alive_worker(session, "w1")
    _running_query(session, run, principal, "w1")
    run.write_mode = WriteMode.MAINTAIN
    session.commit()
    reason = QueryService(session, AllowAllAuthorization(), running_per_principal=1).waiting_for(
        run
    )
    assert reason is not None
    assert reason.code == "workers_busy"
    assert reason.ahead == 0


def test_a_running_pipeline_run_is_not_counted_against_the_limit(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _queued(session, principal, tmp_path)
    _alive_worker(session, "w1")
    held = _running_query(session, run, principal, "w1")
    held.write_mode = WriteMode.MAINTAIN
    session.commit()
    reason = QueryService(session, AllowAllAuthorization(), running_per_principal=1).waiting_for(
        run
    )
    assert reason is not None
    assert reason.code == "workers_busy"


def _older_work(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> tuple[QueryRun, QueryRun]:
    """Queue, older than the run asked about, three ad-hoc queries, a maintenance run and a deploy.

    Parameters
    ----------
    session
        Database session.
    principal
        The submitter of everything.
    tmp_path
        The environment's storage root.

    Returns
    -------
    tuple[QueryRun, QueryRun]
        The ad-hoc query asked about, and a younger maintenance run asked about.
    """
    run = _queued(session, principal, tmp_path)
    run.created_at = datetime.now(UTC) - timedelta(seconds=50)
    session.commit()
    env = session.scalars(select(Environment)).one()
    for n in range(3):
        _add_run(session, env, principal, age=100 - n)
    _add_run(session, env, principal, age=90, write_mode=WriteMode.MAINTAIN)
    _other_work(session, run, query_ahead=0, deploy_ahead=1, later=0)
    maintenance = _add_run(session, env, principal, write_mode=WriteMode.MAINTAIN)
    return run, maintenance


def test_ahead_counts_every_kind_while_a_worker_takes_all(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run, maintenance = _older_work(session, principal, tmp_path)
    _alive_worker(session, "a")
    _running_query(session, run, principal, "a")
    service = QueryService(session, AllowAllAuthorization(), running_per_principal=9)
    reason = service.waiting_for(run)
    assert reason is not None
    # three ad-hoc queries, the maintenance run and the deploy
    assert (reason.code, reason.ahead) == ("workers_busy", 5)
    session.refresh(maintenance)
    reason = service.waiting_for(maintenance)
    assert reason is not None
    assert reason.ahead == 6  # and the ad-hoc query asked about before


def test_ahead_counts_only_the_kind_while_no_worker_takes_all(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run, maintenance = _older_work(session, principal, tmp_path)
    _alive_worker(session, "i", takes="interactive")
    _alive_worker(session, "p", takes="pipeline")
    _running_query(session, run, principal, "i")
    service = QueryService(session, AllowAllAuthorization(), running_per_principal=9)
    reason = service.waiting_for(run)
    assert reason is not None
    assert (reason.code, reason.ahead, reason.workers) == ("workers_busy", 3, 1)
    assert reason.message == (
        "The only worker that takes interactive work is busy, and about 3 queries or deploys "
        "are ahead of this one."
    )
    session.refresh(maintenance)
    reason = service.waiting_for(maintenance)
    assert reason is not None
    # the older maintenance run and the deploy; the pipeline worker is free
    assert (reason.code, reason.ahead, reason.workers) == ("starting", 2, 1)


@pytest.mark.parametrize("status", [QueryRunStatus.SUCCEEDED, QueryRunStatus.FAILED])
def test_cancelling_an_ended_run_whose_worker_is_gone_changes_nothing(
    session: Session, principal: PrincipalRef, tmp_path: Path, status: QueryRunStatus
) -> None:
    """R4.7.1: the cancel's lost-run UPDATE carries `status = RUNNING` itself."""
    run = _queued(session, principal, tmp_path)
    _held(session, run, heartbeat_age=61)
    _set_status(session, run, status)
    answer = QueryService(session, SUBMITTER).cancel_query(principal, "dev", run.id)
    assert (answer.status, answer.error_cause, answer.cancel_requested) == (status, None, False)
    assert _cancel_events(session) == []
    assert _completions(session) == []


# -- the query list -------------------------------------------


def _env(session: Session, principal: PrincipalRef, tmp_path: Path, name: str) -> Environment:
    """Create an environment.

    Parameters
    ----------
    session
        Database session.
    principal
        The creator.
    tmp_path
        Parent of the storage root.
    name
        The environment's name.

    Returns
    -------
    Environment
        The row.
    """
    return _catalog(session).create_environment(
        principal, name=name, storage=StorageConfig(scheme="file", root=f"file://{tmp_path / name}")
    )


def _add_run(
    session: Session,
    env: Environment,
    submitter: PrincipalRef | None,
    status: QueryRunStatus = QueryRunStatus.QUEUED,
    *,
    age: int = 0,
    **fields: object,
) -> QueryRun:
    """Insert a query run that was created `age` seconds ago.

    Parameters
    ----------
    session
        Database session.
    env
        The run's environment.
    submitter
        Who submitted it, or `None`.
    status
        Its status.
    age
        Seconds since it was created.
    **fields
        More column values (`started_at`, `finished_at`, `write_mode`, ...).

    Returns
    -------
    QueryRun
        The committed row.
    """
    run = QueryRun(
        origin=QueryOrigin.WRITTEN,
        id=uuid.uuid4(),
        environment_id=env.id,
        principal_id=None if submitter is None else submitter.id,
        sql_text="SELECT secret_column FROM hidden_table",
        status=status,
        created_at=datetime.now(UTC) - timedelta(seconds=age),
    )
    for name, value in fields.items():
        setattr(run, name, value)
    session.add(run)
    session.commit()
    return run


def _ago(seconds: int) -> datetime:
    """Return the time `seconds` ago.

    Parameters
    ----------
    seconds
        How long ago.

    Returns
    -------
    datetime
        An aware UTC time.
    """
    return datetime.now(UTC) - timedelta(seconds=seconds)


def _ids(rows: list[QueryListItem]) -> list[uuid.UUID]:
    """Return the ids of rows, in order.

    Parameters
    ----------
    rows
        The listed rows.

    Returns
    -------
    list of uuid.UUID
        Their ids.
    """
    return [row.id for row in rows]


def test_a_submitter_sees_only_the_runs_their_principal_submitted(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    bob = _persisted(session, "bob")
    mine = _add_run(session, env, principal)
    theirs = _add_run(session, env, bob)
    orphan = _add_run(session, env, None)  # its submitter was deleted: nobody's but a manager's
    service = QueryService(session, SUBMITTER)
    assert _ids(service.list_queries(principal, "dev")) == [mine.id]
    assert _ids(service.list_queries(bob, "dev")) == [theirs.id]
    assert orphan.id in _ids(QueryService(session, MANAGER).list_queries(principal, "dev"))
    assert [row.submitter for row in service.list_queries(bob, "dev")] == ["bob"]


def test_a_task_token_sees_only_the_runs_of_that_token(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    token_id = uuid.uuid4()
    token = PrincipalRef(
        id=principal.id,
        name=principal.name,
        type=principal.type,
        task_scope=TaskScope(environment="dev", asset_scope=["gold.*"], token_id=token_id),
    )
    own = _add_run(session, env, principal, task_token_id=token_id)
    _add_run(session, env, principal)
    _add_run(session, env, principal, task_token_id=uuid.uuid4())
    service = QueryService(session, SUBMITTER)
    assert _ids(service.list_queries(token, "dev")) == [own.id]
    assert len(service.list_queries(principal, "dev")) == 3


def test_a_manager_with_a_personal_token_sees_every_row(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    other = _env(session, principal, tmp_path, "other")
    operator = _persisted(session, "operator")
    bob = _persisted(session, "bob")
    first = _add_run(session, env, bob, age=20)
    second = _add_run(session, env, principal, age=10)
    third = _add_run(session, env, None, age=5)
    _add_run(session, other, bob)
    rows = QueryService(session, MANAGER).list_queries(operator, "dev")
    assert _ids(rows) == [first.id, second.id, third.id]
    assert [row.submitter for row in rows] == ["bob", "admin", None]


def test_a_task_token_of_a_manager_is_not_a_manager(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    token_id = uuid.uuid4()
    token = PrincipalRef(
        id=principal.id,
        name=principal.name,
        type=principal.type,
        task_scope=TaskScope(environment="dev", asset_scope=["gold.*"], token_id=token_id),
    )
    own = _add_run(session, env, principal, task_token_id=token_id)
    _add_run(session, env, _persisted(session, "bob"))
    everything = ActionAllowList(Action.SUBMIT_QUERY, Action.MANAGE_ENVIRONMENT)
    assert _ids(QueryService(session, everything).list_queries(token, "dev")) == [own.id]
    assert Action.MANAGE_ENVIRONMENT not in [action for action, _securable in everything.seen]


def test_a_caller_without_submit_query_gets_the_status_reads_denial(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    run = _add_run(session, _env(session, principal, tmp_path, "dev"), principal)
    service = QueryService(session, ActionAllowList())
    with pytest.raises(PermissionDenied) as read:
        service.get_query_run(principal, "dev", run.id)
    with pytest.raises(PermissionDenied) as listed:
        service.list_queries(principal, "dev")
    assert str(listed.value) == str(read.value)


def test_a_missing_environment_is_not_found(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    _env(session, principal, tmp_path, "dev")
    for authz in (SUBMITTER, MANAGER):
        with pytest.raises(NotFoundError, match="No environment named 'nowhere'"):
            QueryService(session, authz).list_queries(principal, "nowhere")


def test_no_row_carries_sql_an_error_message_or_lineage(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    assert [f.name for f in dataclasses.fields(QueryListItem)] == [
        "id",
        "submitter",
        "kind",
        "target",
        "status",
        "created_at",
        "started_at",
        "finished_at",
        "error_cause",
        "waiting_for",
    ]
    env = _env(session, principal, tmp_path, "dev")
    failed = _add_run(
        session,
        env,
        principal,
        QueryRunStatus.FAILED,
        error_message="secret detail",
        error_cause="invalid_query",
        finished_at=_ago(5),
        lineage_json={"c": [{"table": "t", "column": "c"}]},
        result_location="results/x.arrow",
        worker_id="host-1:42",
    )
    (row,) = QueryService(session, SUBMITTER).list_queries(principal, "dev", include_ended=True)
    assert row.id == failed.id
    assert row.error_cause == "invalid_query"
    assert "secret" not in repr(row)
    assert "host-1" not in repr(row)


@pytest.mark.parametrize(
    ("mode", "target", "kind"),
    [
        (None, None, "read"),
        (WriteMode.CREATE, "gold.daily", "create"),
        (WriteMode.REPLACE, "gold.daily", "replace"),
        (WriteMode.APPEND, "gold.daily", "append"),
        (WriteMode.VIEW, "gold.daily", "view"),
        (WriteMode.MAINTAIN, "gold.daily", "maintain"),
    ],
)
def test_kind_and_target(
    session: Session,
    principal: PrincipalRef,
    tmp_path: Path,
    mode: WriteMode | None,
    target: str | None,
    kind: str,
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    _add_run(session, env, principal, write_mode=mode, target_name=target)
    (row,) = QueryService(session, SUBMITTER).list_queries(principal, "dev")
    assert (row.kind, row.target) == (kind, target)


def test_a_submitter_whose_principal_was_deleted_is_null(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    _add_run(session, env, None)
    (row,) = QueryService(session, MANAGER).list_queries(principal, "dev")
    assert row.submitter is None


def test_the_order_is_running_then_queued_then_ended(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    queued_old = _add_run(session, env, principal, age=500)
    queued_new = _add_run(session, env, principal, age=100)
    run_late = _add_run(session, env, principal, QueryRunStatus.RUNNING, started_at=_ago(10))
    run_early = _add_run(session, env, principal, QueryRunStatus.RUNNING, started_at=_ago(300))
    done_old = _add_run(
        session, env, principal, QueryRunStatus.SUCCEEDED, age=7200, finished_at=_ago(3600)
    )
    done_new = _add_run(
        session, env, principal, QueryRunStatus.FAILED, age=7200, finished_at=_ago(60)
    )
    _add_run(session, env, principal, QueryRunStatus.CANCELLED, age=9000, finished_at=_ago(90_000))
    service = QueryService(session, SUBMITTER)
    live = [run_early.id, run_late.id, queued_old.id, queued_new.id]
    assert _ids(service.list_queries(principal, "dev")) == live
    assert _ids(service.list_queries(principal, "dev", include_ended=True)) == [
        *live,
        done_new.id,
        done_old.id,
    ]
    assert _ids(service.list_queries(principal, "dev", include_ended=True, limit=3)) == live[:3]
    assert _ids(service.list_queries(principal, "dev", include_ended=True, limit=5)) == [
        *live,
        done_new.id,
    ]


def test_a_run_that_ended_just_inside_and_just_outside_24_hours(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    inside = _add_run(
        session,
        env,
        principal,
        QueryRunStatus.SUCCEEDED,
        finished_at=_ago(QUERY_LIST_ENDED_SECONDS - 60),
    )
    _add_run(
        session,
        env,
        principal,
        QueryRunStatus.SUCCEEDED,
        finished_at=_ago(QUERY_LIST_ENDED_SECONDS + 60),
    )
    rows = QueryService(session, SUBMITTER).list_queries(principal, "dev", include_ended=True)
    assert _ids(rows) == [inside.id]


def test_queued_rows_carry_their_reason_and_others_none(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    _alive_worker(session, "w")
    _alive_worker(session, "w2")
    first = _add_run(session, env, principal, age=30)
    second = _add_run(session, env, principal, age=20)
    running = _add_run(
        session, env, principal, QueryRunStatus.RUNNING, started_at=_ago(5), worker_id="w"
    )
    done = _add_run(session, env, principal, QueryRunStatus.SUCCEEDED, finished_at=_ago(5))
    service = QueryService(session, SUBMITTER)
    rows = service.list_queries(principal, "dev", include_ended=True)
    by_id = {row.id: row.waiting_for for row in rows}
    assert by_id[running.id] is None
    assert by_id[done.id] is None
    one, two = by_id[first.id], by_id[second.id]
    assert one is not None
    assert two is not None
    assert (one.code, one.ahead, one.workers) == ("starting", 0, 2)
    assert (two.code, two.ahead) == ("starting", 1)
    # what the status read says for the same row
    session.refresh(second)
    assert service.waiting_for(second) == two


def test_the_reason_of_a_row_the_listing_cut_beyond_the_ahead_cap_says_more_than(
    session: Session, principal: PrincipalRef, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("lakana_core.catalog.query_service.MAX_AHEAD_COUNTED", 3)
    env = _env(session, principal, tmp_path, "dev")
    _alive_worker(session, "w")
    runs = [_add_run(session, env, principal, age=100 - n) for n in range(6)]
    rows = QueryService(session, SUBMITTER).list_queries(principal, "dev")
    reasons = [row.waiting_for for row in rows]
    assert [r.ahead for r in reasons if r is not None] == [0, 1, 2, 3, 3, 3]
    assert reasons[3] is not None
    assert reasons[4] is not None
    assert "more than" not in reasons[3].message
    assert "more than 3 queries or deploys are ahead" in reasons[4].message
    assert _ids(rows) == [run.id for run in runs]


def test_the_snapshot_is_read_once_per_list(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    _busy_worker(session)
    service = QueryService(session, SUBMITTER)
    for n in range(2):
        _add_run(session, env, principal, age=100 - n)

    def listing() -> object:
        return service.list_queries(principal, "dev", include_ended=True, limit=200)

    two = _statements(session, listing)
    for n in range(2, 100):
        _add_run(session, env, principal, age=100 - n)
    many = _statements(session, listing)
    assert len(many) == len(two)
    # environment, clock, three reads of runs, alive workers, three of what they carry, two of
    # times; the only worker is busy, so the limit is not asked and no running count is read
    assert len(many) == 11
    assert sum("FROM workers" in s and "count" not in s for s in many) == 1
    assert not any("sql_text" in s for s in many)


def _capped_listing(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> tuple[PrincipalRef, QueryRun, QueryRun]:
    """Give two principals two running ad-hoc queries each and one queued query each, limit 2.

    Parameters
    ----------
    session
        Database session.
    principal
        The first principal, who is also the manager in the tests.
    tmp_path
        The environment's storage root.

    Returns
    -------
    tuple[PrincipalRef, QueryRun, QueryRun]
        The other principal, and the queued run of the first and of the other one.
    """
    env = _env(session, principal, tmp_path, "dev")
    bob = _persisted(session, "bob")
    _alive_worker(session, "w1")
    _alive_worker(session, "w2")
    _alive_worker(session, "w3")  # free: the limit, not the workers, holds the queued rows
    for who in (principal, bob):
        for worker in ("w1", "w2"):
            _add_run(
                session, env, who, QueryRunStatus.RUNNING, worker_id=worker, started_at=_ago(5)
            )
    own = _add_run(session, env, principal, age=20)
    theirs = _add_run(session, env, bob, age=10)
    return bob, own, theirs


def test_a_managers_row_of_a_capped_principal_carries_the_limit_and_no_number_running(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    _bob, own, theirs = _capped_listing(session, principal, tmp_path)
    rows = QueryService(session, MANAGER, running_per_principal=2).list_queries(principal, "dev")
    by_id = {row.id: row.waiting_for for row in rows}
    other = by_id[theirs.id]
    mine = by_id[own.id]
    assert other is not None and mine is not None
    assert other.to_json() == {
        "code": "user_limit",
        "message": (
            "Its submitter has reached the limit of 2 running queries per user; it can start "
            "when fewer than 2 of theirs are running."
        ),
        "limit": 2,
    }
    assert other.running is None
    assert mine.to_json() == {
        "code": "user_limit",
        "message": (
            "You have 2 queries running, which is the limit per user here; this one can start "
            "when one of them ends. 'lakana query list' shows them."
        ),
        "running": 2,
        "limit": 2,
    }


def test_a_managers_row_of_a_principal_above_the_limit_carries_the_same_body(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    bob, _own, theirs = _capped_listing(session, principal, tmp_path)
    env = session.scalars(select(Environment)).one()
    _add_run(session, env, bob, QueryRunStatus.RUNNING, worker_id="w1", started_at=_ago(5))
    manager = QueryService(session, MANAGER, running_per_principal=2)
    other = {row.id: row.waiting_for for row in manager.list_queries(principal, "dev")}[theirs.id]
    assert other is not None
    assert other.to_json() == {
        "code": "user_limit",
        "message": (
            "Its submitter has reached the limit of 2 running queries per user; it can start "
            "when fewer than 2 of theirs are running."
        ),
        "limit": 2,
    }
    mine = {row.id: row.waiting_for for row in manager.list_queries(bob, "dev")}[theirs.id]
    assert mine is not None
    assert mine.running == 3


def test_a_task_token_sees_its_own_row_as_own(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    _bob, own, _theirs = _capped_listing(session, principal, tmp_path)
    token_id = uuid.uuid4()
    token = dataclasses.replace(
        principal,
        task_scope=TaskScope(environment="dev", asset_scope=["gold.*"], token_id=token_id),
    )
    own.task_token_id = token_id
    session.commit()
    rows = QueryService(
        session,
        ActionAllowList(Action.SUBMIT_QUERY, Action.MANAGE_ENVIRONMENT),
        running_per_principal=2,
    ).list_queries(token, "dev")
    assert _ids(rows) == [own.id]
    reason = rows[0].waiting_for
    assert reason is not None
    assert (reason.code, reason.running, reason.limit) == ("user_limit", 2, 2)


def test_a_listing_reads_the_running_counts_once(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    _alive_worker(session, "free")
    service = QueryService(session, SUBMITTER, running_per_principal=5)
    for n in range(2):
        _add_run(session, env, principal, age=100 - n)

    def listing() -> object:
        return service.list_queries(principal, "dev")

    two = _statements(session, listing)
    for n in range(2, 20):
        _add_run(session, env, principal, age=100 - n)
    twenty = _statements(session, listing)
    assert len(twenty) == len(two)
    assert sum("count(" in s.lower() and "GROUP BY" in s for s in twenty) == 1


def test_a_listing_of_maintenance_runs_only_reads_no_running_counts_and_no_pass_by_times(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    _alive_worker(session, "free")
    service = QueryService(session, SUBMITTER, running_per_principal=5)
    _add_run(session, env, principal, age=10)

    def listing() -> object:
        return service.list_queries(principal, "dev")

    adhoc = len(_statements(session, listing))
    for run in session.scalars(select(QueryRun).where(QueryRun.status == QueryRunStatus.QUEUED)):
        run.write_mode = WriteMode.MAINTAIN
    session.commit()
    # the grouped running count and the read of the principals passed by
    assert len(_statements(session, listing)) == adhoc - 2


def test_a_listing_names_no_worker_of_the_kind_for_each_kind_of_row(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    _alive_worker(session, "i", takes="interactive")
    adhoc = _add_run(session, env, principal, age=20)
    maintenance = _add_run(session, env, principal, age=10, write_mode=WriteMode.MAINTAIN)
    rows = QueryService(session, SUBMITTER).list_queries(principal, "dev")
    by_id = {row.id: row.waiting_for for row in rows}
    one, two = by_id[adhoc.id], by_id[maintenance.id]
    assert one is not None and two is not None
    assert (one.code, two.code) == ("starting", "no_worker_of_kind")
    assert "this maintenance run cannot start" in two.message


@pytest.mark.parametrize("limit", [0, 201, -1])
def test_a_limit_outside_1_to_200_is_refused(
    session: Session, principal: PrincipalRef, tmp_path: Path, limit: int
) -> None:
    _env(session, principal, tmp_path, "dev")
    with pytest.raises(ValidationError, match="between 1 and 200"):
        QueryService(session, SUBMITTER).list_queries(principal, "dev", limit=limit)


def _grant(
    session: Session,
    who: PrincipalRef,
    action: Action,
    scope: str | None,
    *,
    group: Group | None = None,
) -> None:
    """Grant one action on an environment (or the instance, for `None`) to a principal or group.

    Parameters
    ----------
    session
        Database session.
    who
        The grantee (when `group` is `None`) and the grantor.
    action
        The action.
    scope
        The environment's name, or `None` for the whole instance.
    group
        Grant to this group instead of to `who`.
    """
    env_id = None if scope is None else resolve_environment_id(session, scope)
    session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=None if group is not None else who.id,
            group_id=None if group is None else group.id,
            action=action,
            kind=SecurableKind.INSTANCE if scope is None else SecurableKind.ENVIRONMENT,
            environment=scope,
            environment_id=env_id,
            granted_by=who.id,
            granted_at=datetime.now(UTC),
        )
    )
    session.commit()


def test_with_real_grants_a_manager_sees_only_the_environment_they_manage(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    dev = _env(session, principal, tmp_path, "dev")
    prod = _env(session, principal, tmp_path, "prod")
    bob = _persisted(session, "bob")
    carol = _persisted(session, "carol")
    in_dev = _add_run(session, dev, bob)
    in_prod = _add_run(session, prod, bob)
    for who in (carol,):
        _grant(session, who, Action.SUBMIT_QUERY, "dev")
        _grant(session, who, Action.SUBMIT_QUERY, "prod")
    _grant(session, carol, Action.MANAGE_ENVIRONMENT, "prod")
    service = QueryService(session, GrantAuthorization(session))
    assert _ids(service.list_queries(carol, "prod")) == [in_prod.id]
    # a manager of prod is only a submitter in dev: bob's row there is absent
    assert service.list_queries(carol, "dev") == []
    assert in_dev.id not in _ids(service.list_queries(carol, "prod"))


def test_with_real_grants_a_manager_of_another_environment_gets_the_status_denial(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    dev = _env(session, principal, tmp_path, "dev")
    _env(session, principal, tmp_path, "prod")
    bob = _persisted(session, "bob")
    carol = _persisted(session, "carol")
    run = _add_run(session, dev, bob)
    _grant(session, carol, Action.MANAGE_ENVIRONMENT, "prod")
    service = QueryService(session, GrantAuthorization(session))
    with pytest.raises(PermissionDenied) as read:
        service.get_query_run(carol, "dev", run.id)
    with pytest.raises(PermissionDenied) as listed:
        service.list_queries(carol, "dev")
    assert str(listed.value) == str(read.value)


def test_with_real_grants_a_group_held_manage_environment_sees_every_row(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    dev = _env(session, principal, tmp_path, "dev")
    bob = _persisted(session, "bob")
    carol = _persisted(session, "carol")
    run = _add_run(session, dev, bob)
    group = Group(id=uuid.uuid4(), name="ops")
    session.add(group)
    session.add(GroupMembership(group_id=group.id, principal_id=carol.id))
    session.commit()
    _grant(session, principal, Action.MANAGE_ENVIRONMENT, "dev", group=group)
    assert _ids(QueryService(session, GrantAuthorization(session)).list_queries(carol, "dev")) == [
        run.id
    ]


def test_with_real_grants_the_instance_administrator_sees_every_environment(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    dev = _env(session, principal, tmp_path, "dev")
    prod = _env(session, principal, tmp_path, "prod")
    bob = _persisted(session, "bob")
    admin = _persisted(session, "root")
    a, b = _add_run(session, dev, bob, age=9), _add_run(session, prod, bob, age=8)
    session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=admin.id,
            role=Role.ADMIN,
            kind=SecurableKind.INSTANCE,
            granted_by=admin.id,
            granted_at=datetime.now(UTC),
        )
    )
    session.commit()
    service = QueryService(session, GrantAuthorization(session))
    assert _ids(service.list_queries(admin, "dev")) == [a.id]
    assert _ids(service.list_queries(admin, "prod")) == [b.id]


def test_a_non_manager_never_learns_that_another_principals_row_exists(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    dev = _env(session, principal, tmp_path, "dev")
    bob = _persisted(session, "bob")
    carol = _persisted(session, "carol")
    for n in range(5):
        _add_run(session, dev, bob, QueryRunStatus.RUNNING, started_at=_ago(n + 1))
        _add_run(session, dev, bob, age=n + 1)
    mine = _add_run(session, dev, carol, age=1)
    _alive_worker(session, "w")
    rows = QueryService(session, SUBMITTER).list_queries(carol, "dev", include_ended=True, limit=1)
    assert _ids(rows) == [mine.id]
    reason = rows[0].waiting_for
    assert reason is not None
    assert reason.ahead == 5  # a count of the shared queue, as the status read gives it


def test_the_model_has_the_index_the_ended_read_walks() -> None:
    names = {
        str(index.name): [c.name for c in index.columns]
        for index in cast(Table, QueryRun.__table__).indexes
    }
    assert names["ix_query_runs_environment_id_finished_at"] == ["environment_id", "finished_at"]


def test_a_final_run_without_a_finish_time_is_not_listed_under_all(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Every writer of a final status sets `finished_at`; a row without one is not listed."""
    env = _env(session, principal, tmp_path, "dev")
    _add_run(session, env, principal, QueryRunStatus.SUCCEEDED)
    listed = _add_run(session, env, principal, QueryRunStatus.SUCCEEDED, finished_at=_ago(5))
    rows = QueryService(session, SUBMITTER).list_queries(principal, "dev", include_ended=True)
    assert _ids(rows) == [listed.id]


class _FakePostgresCancel:
    """Pose a SQLite session as PostgreSQL for a cancel, and make a lock wait time out.

    ``SET LOCAL`` statements are replaced by ``SELECT 1`` so SQLite accepts them; once the
    timeout was set, the next ``UPDATE`` raises the given error, as PostgreSQL does when the run's
    row is locked by a worker's commit.
    """

    def __init__(
        self, session: Session, monkeypatch: pytest.MonkeyPatch, failure: Exception | None
    ) -> None:
        """Install the stand-in.

        Parameters
        ----------
        session
            The SQLite session.
        monkeypatch
            Used to claim the dialect is PostgreSQL.
        failure
            What the first ``UPDATE`` after ``SET LOCAL`` raises; `None` for none.
        """
        self.statements: list[str] = []
        self._armed = False
        monkeypatch.setattr(pools, "on_postgres", lambda _session: True)

        @event.listens_for(session.get_bind(), "before_cursor_execute", retval=True)
        def _intercept(
            conn: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            executemany: bool,
        ) -> tuple[str, object]:
            if statement.startswith("SET LOCAL"):
                self.statements.append(statement)
                self._armed = True
                return "SELECT 1", ()
            if self._armed and statement.startswith("UPDATE") and failure is not None:
                raise failure
            return statement, parameters


_BUSY = (
    "Query {id} is being started or is finishing right now, so it cannot be cancelled at this "
    "moment. Check it with: lakana query status {id} --env dev. If it is still running, cancel "
    "it with: lakana query cancel {id} --env dev"
)


def _lock_timeout() -> OperationalError:
    """Build the error PostgreSQL's ``lock_timeout`` produces.

    Returns
    -------
    OperationalError
        With SQLSTATE 55P03 in its text, as pg8000 reports it.
    """
    return OperationalError(
        "UPDATE", {}, Exception("{'C': '55P03', 'M': 'canceling statement due to lock timeout'}")
    )


def test_a_cancel_that_waits_for_the_runs_row_lock_is_a_conflict_not_a_hang(
    session: Session, principal: PrincipalRef, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _queued(session, principal, tmp_path)
    _held(session, run, heartbeat_age=61)
    fake = _FakePostgresCancel(session, monkeypatch, _lock_timeout())
    with pytest.raises(ConflictError) as refused:
        QueryService(session, SUBMITTER).cancel_query(principal, "dev", run.id)
    assert fake.statements == ["SET LOCAL lock_timeout = '2000ms'"]
    assert str(refused.value) == _BUSY.format(id=run.id)
    assert _cancel_events(session) == []
    session.refresh(run)
    assert (run.status, run.cancel_requested) == (QueryRunStatus.RUNNING, False)


def test_a_cancel_on_postgres_resets_the_timeout_for_the_statements_after_it(
    session: Session, principal: PrincipalRef, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _queued(session, principal, tmp_path)
    _held(session, run, heartbeat_age=20)
    fake = _FakePostgresCancel(session, monkeypatch, None)
    asked = QueryService(session, SUBMITTER).cancel_query(principal, "dev", run.id)
    assert (asked.status, asked.cancel_requested) == (QueryRunStatus.RUNNING, True)
    assert fake.statements == [
        "SET LOCAL lock_timeout = '2000ms'",
        "SET LOCAL lock_timeout = DEFAULT",
    ]


def test_another_database_error_during_a_cancel_is_not_taken_for_a_lock_timeout(
    session: Session, principal: PrincipalRef, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _queued(session, principal, tmp_path)
    _held(session, run, heartbeat_age=61)
    _FakePostgresCancel(session, monkeypatch, OperationalError("UPDATE", {}, Exception("reset")))
    with pytest.raises(OperationalError):
        QueryService(session, SUBMITTER).cancel_query(principal, "dev", run.id)


def test_a_cancel_of_a_queued_run_whose_row_is_locked_is_bounded_too(
    session: Session, principal: PrincipalRef, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker that dequeued the run and froze before its start holds the row of a QUEUED run."""
    run = _queued(session, principal, tmp_path)
    fake = _FakePostgresCancel(session, monkeypatch, _lock_timeout())
    with pytest.raises(ConflictError) as refused:
        QueryService(session, SUBMITTER).cancel_query(principal, "dev", run.id)
    assert fake.statements == ["SET LOCAL lock_timeout = '2000ms'"]
    assert str(refused.value) == _BUSY.format(id=run.id)
    session.refresh(run)
    assert run.status is QueryRunStatus.QUEUED
    assert session.execute(select(1)).scalar_one() == 1, "the session is usable after the refusal"
