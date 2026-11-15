"""Run principal binding and undeploy."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from core_test_helpers import ActionAllowList, _prepared_catalog, ensure_table
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from test_projects_dag import make_asset
from test_projects_deploys import _deployed, _repo

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef
from lakana_core.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Environment,
    EnvironmentDeployment,
    JobVersion,
    Principal,
    PrincipalType,
    Repository,
    Run,
    RunKind,
    RunStatus,
)
from lakana_core.projects.deploys import DeployService

_BRONZE = NamespacePath.from_parts(["bronze"])


@pytest.fixture
def env(session: Session, tmp_path: Path) -> None:
    """Create `dev` with `bronze.events`.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    """
    _prepared_catalog(
        session,
        tmp_path,
        AllowAllAuthorization(),
        namespaces=[_BRONZE],
        tables=[TableRef("dev", _BRONZE, "events")],
    )


def _service_account(session: Session, name: str = "etl", *, active: bool = True) -> Principal:
    """Persist a service account.

    Parameters
    ----------
    session
        Database session.
    name
        Its name.
    active
        Whether it is active.

    Returns
    -------
    Principal
        The row.
    """
    row = Principal(
        id=uuid.uuid4(), type=PrincipalType.SERVICE_ACCOUNT, name=name, is_active=active
    )
    session.add(row)
    session.commit()
    return row


def _events(session: Session, event_type: AuditEventType) -> list[AuditEvent]:
    """Return the audit events of one type.

    Parameters
    ----------
    session
        Database session.
    event_type
        The type.

    Returns
    -------
    list of AuditEvent
        Oldest first.
    """
    return list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.event_type == event_type)
            .order_by(AuditEvent.occurred_at)
        )
    )


@pytest.fixture
def deployed(session: Session, env: None, principal: PrincipalRef) -> JobVersion:
    """`analytics` deployed to `dev` with one asset.

    Parameters
    ----------
    session
        Database session.
    env
        The `dev` environment fixture.
    principal
        The deployer.

    Returns
    -------
    Project version
        The current version.
    """
    return _deployed(
        session, principal, _repo(session), "a" * 40, make_asset("silver.clean", ("bronze.events",))
    )


def test_bind_and_clear_are_audited_and_listed(
    session: Session, deployed: JobVersion, principal: PrincipalRef
) -> None:
    """A service account binds, shows in the deployments list, clears; both audited."""
    _service_account(session)
    service = DeployService(session, AllowAllAuthorization())
    view = service.set_run_as(principal, "dev", "analytics", "etl")
    assert view.run_as == "etl"
    assert [d.run_as for d in service.deployments(principal, "dev")] == ["etl"]
    assert service.clear_run_as(principal, "dev", "analytics").run_as is None
    bound = _events(session, AuditEventType.RUN_AS_BOUND)
    cleared = _events(session, AuditEventType.RUN_AS_CLEARED)
    assert bound[0].detail == {"repository": "analytics", "principal": "etl"}
    assert bound[0].securable_environment == "dev"
    assert cleared[0].detail == {"repository": "analytics", "principal": "etl"}


def test_clearing_an_unbound_deployment_writes_no_audit_event(
    session: Session, deployed: JobVersion, principal: PrincipalRef
) -> None:
    """Nothing was cleared, so no `RUN_AS_CLEARED` with a null principal is recorded."""
    service = DeployService(session, AllowAllAuthorization())
    assert service.clear_run_as(principal, "dev", "analytics").run_as is None
    assert not _events(session, AuditEventType.RUN_AS_CLEARED)


def test_binding_survives_a_redeploy_and_keeps_updated_at(
    session: Session, deployed: JobVersion, principal: PrincipalRef
) -> None:
    """The binding belongs to the deployment, not the project version; it is no pointer move."""
    _service_account(session)
    service = DeployService(session, AllowAllAuthorization())
    before = service.deployments(principal, "dev")[0].updated_at
    service.set_run_as(principal, "dev", "analytics", "etl")
    assert service.deployments(principal, "dev")[0].updated_at == before
    repository = session.scalars(select(Repository)).one()
    _deployed(
        session, principal, repository, "b" * 40, make_asset("silver.clean", ("bronze.events",))
    )
    assert service.deployments(principal, "dev")[0].run_as == "etl"


@pytest.mark.parametrize(
    ("setup", "error", "message"),
    [
        ("user", NotFoundError, "^Principal 'etl' not found or not a service account\\.$"),
        ("inactive", ValidationError, "is deactivated"),
        ("missing", NotFoundError, "^Principal 'etl' not found or not a service account\\.$"),
    ],
)
def test_only_an_active_service_account_can_be_bound(
    session: Session,
    deployed: JobVersion,
    principal: PrincipalRef,
    setup: str,
    error: type[Exception],
    message: str,
) -> None:
    """Spec: the principal must be an active service account."""
    if setup == "user":
        session.add(Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="etl"))
        session.commit()
    elif setup == "inactive":
        _service_account(session, active=False)
    with pytest.raises(error, match=message):
        DeployService(session, AllowAllAuthorization()).set_run_as(
            principal, "dev", "analytics", "etl"
        )


def test_binding_needs_manage_environment(
    session: Session, deployed: JobVersion, principal: PrincipalRef
) -> None:
    """DEPLOY alone may not choose who prod writes as."""
    _service_account(session)
    service = DeployService(session, ActionAllowList(Action.DEPLOY, Action.READ_ENVIRONMENT))
    with pytest.raises(PermissionDenied):
        service.set_run_as(principal, "dev", "analytics", "etl")
    with pytest.raises(PermissionDenied):
        service.clear_run_as(principal, "dev", "analytics")


def test_binding_an_undeployed_repository_is_404(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """No deployment, nothing to bind."""
    _repo(session)
    _service_account(session)
    with pytest.raises(NotFoundError, match="not deployed in 'dev'"):
        DeployService(session, AllowAllAuthorization()).set_run_as(
            principal, "dev", "analytics", "etl"
        )


def _active_run(
    session: Session, version: JobVersion, status: RunStatus = RunStatus.RUNNING
) -> Run:
    """Insert a run of `version`.

    Parameters
    ----------
    session
        Database session.
    version
        The project version.
    status
        Its status.

    Returns
    -------
    Run
        The row.
    """
    run = Run(
        id=uuid.uuid4(),
        environment_id=version.environment_id,
        repository_id=version.repository_id,
        job_version_id=version.id,
        kind=RunKind.FULL,
        status=status,
        selection_json={},
    )
    session.add(run)
    session.commit()
    return run


def test_undeploy_removes_pointer_and_binding_and_is_audited(
    session: Session, deployed: JobVersion, principal: PrincipalRef
) -> None:
    """Data and project versions stay; the pointer and the binding go."""
    _service_account(session)
    service = DeployService(session, AllowAllAuthorization())
    service.set_run_as(principal, "dev", "analytics", "etl")
    service.undeploy(principal, "dev", "analytics")
    assert session.scalars(select(EnvironmentDeployment)).all() == []
    assert session.get(JobVersion, deployed.id) is not None
    (event,) = _events(session, AuditEventType.UNDEPLOYED)
    assert event.detail == {"repository": "analytics", "project_version_id": str(deployed.id)}
    with pytest.raises(NotFoundError, match="not deployed"):
        service.undeploy(principal, "dev", "analytics")


@pytest.mark.parametrize("status", [RunStatus.QUEUED, RunStatus.RUNNING])
def test_undeploy_is_refused_while_a_run_is_active(
    session: Session, deployed: JobVersion, principal: PrincipalRef, status: RunStatus
) -> None:
    """409 naming the active run; after it finishes, undeploy succeeds."""
    run = _active_run(session, deployed, status)
    service = DeployService(session, AllowAllAuthorization())
    with pytest.raises(ConflictError, match=str(run.id)):
        service.undeploy(principal, "dev", "analytics")
    assert session.scalars(select(EnvironmentDeployment)).one() is not None
    run.status = RunStatus.SUCCEEDED
    session.commit()
    service.undeploy(principal, "dev", "analytics")


def test_undeploy_needs_deploy(
    session: Session, deployed: JobVersion, principal: PrincipalRef
) -> None:
    """MANAGE_ENVIRONMENT alone does not undeploy."""
    service = DeployService(session, ActionAllowList(Action.MANAGE_ENVIRONMENT))
    with pytest.raises(PermissionDenied):
        service.undeploy(principal, "dev", "analytics")


def test_lock_deployment_detects_a_moved_or_removed_pointer(
    session: Session, deployed: JobVersion, principal: PrincipalRef
) -> None:
    """The compare-and-swap run start relies on."""
    service = DeployService(session, AllowAllAuthorization())
    service.lock_deployment(deployed.environment_id, deployed.repository_id, deployed.id)
    session.rollback()
    with pytest.raises(ConflictError, match="changed"):
        service.lock_deployment(deployed.environment_id, deployed.repository_id, uuid.uuid4())
    service.undeploy(principal, "dev", "analytics")
    with pytest.raises(ConflictError):
        service.lock_deployment(deployed.environment_id, deployed.repository_id, deployed.id)


def test_authorization_precedes_lookup(session: Session, principal: PrincipalRef) -> None:
    """A caller without the action learns nothing about what exists (no 404 before 403)."""
    service = DeployService(session, ActionAllowList())
    for call in (
        lambda: service.set_run_as(principal, "nope", "nope", "nope"),
        lambda: service.clear_run_as(principal, "nope", "nope"),
        lambda: service.undeploy(principal, "nope", "nope"),
    ):
        with pytest.raises(PermissionDenied):
            call()


def test_deployment_reader_authorizes_read_environment(
    session: Session, deployed: JobVersion, principal: PrincipalRef
) -> None:
    """The reader keeps `p` and needs READ_ENVIRONMENT."""
    with pytest.raises(PermissionDenied):
        DeployService(session, ActionAllowList()).deployment(principal, "dev", "analytics")
    view = DeployService(session, ActionAllowList(Action.READ_ENVIRONMENT)).deployment(
        principal, "dev", "analytics"
    )
    assert view.job_version_id == deployed.id and view.run_as is None


def test_a_failed_bind_leaves_the_binding_and_audit_untouched(
    session: Session, deployed: JobVersion, principal: PrincipalRef
) -> None:
    """Refusing a user principal writes no audit event and keeps the old binding."""
    _service_account(session)
    session.add(Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="alice"))
    session.commit()
    service = DeployService(session, AllowAllAuthorization())
    service.set_run_as(principal, "dev", "analytics", "etl")
    with pytest.raises(NotFoundError):
        service.set_run_as(principal, "dev", "analytics", "alice")
    assert service.deployment(principal, "dev", "analytics").run_as == "etl"
    assert len(_events(session, AuditEventType.RUN_AS_BOUND)) == 1


def _remove_pointer_after_lookup(monkeypatch: pytest.MonkeyPatch, session: Session) -> None:
    """Make the deployment vanish right after the service has read it.

    Parameters
    ----------
    monkeypatch
        The pytest monkeypatch fixture.
    session
        Database session.
    """
    original = DeployService._deployment_row

    def racing(self: DeployService, env: Environment, repo: Repository) -> EnvironmentDeployment:
        row = original(self, env, repo)
        session.execute(delete(EnvironmentDeployment))
        session.commit()
        return row

    monkeypatch.setattr(DeployService, "_deployment_row", racing)


@pytest.mark.parametrize("operation", ["set", "clear", "undeploy"])
def test_a_pointer_removed_mid_call_is_404(
    session: Session,
    deployed: JobVersion,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """A concurrent undeploy between lookup and write is a 404, never a silent success."""
    _service_account(session)
    service = DeployService(session, AllowAllAuthorization())
    _remove_pointer_after_lookup(monkeypatch, session)
    with pytest.raises(NotFoundError, match="not deployed"):
        if operation == "set":
            service.set_run_as(principal, "dev", "analytics", "etl")
        elif operation == "clear":
            service.clear_run_as(principal, "dev", "analytics")
        else:
            service.undeploy(principal, "dev", "analytics")
    assert _events(session, AuditEventType.UNDEPLOYED) == []
    assert _events(session, AuditEventType.RUN_AS_BOUND) == []


def test_undeployed_repository_tables_are_orphans(
    session: Session, deployed: JobVersion, principal: PrincipalRef, tmp_path: Path
) -> None:
    """After undeploy the repository's tables show in `orphans` (every version is past)."""
    silver = NamespacePath.from_parts(["silver"])
    _prepared_catalog(session, tmp_path, AllowAllAuthorization(), namespaces=[silver])
    ensure_table(session, "dev", "silver", "clean")
    service = DeployService(session, AllowAllAuthorization())
    assert service.orphans(principal, "dev").orphans == []
    service.undeploy(principal, "dev", "analytics")
    report = service.orphans(principal, "dev")
    assert [(o.repository, o.table) for o in report.orphans] == [("analytics", "silver.clean")]


def test_binding_works_with_manage_environment_alone(
    session: Session, deployed: JobVersion, principal: PrincipalRef
) -> None:
    """A change that committed must not 403 on the returned view for lack of READ_ENVIRONMENT."""
    _service_account(session)
    service = DeployService(session, ActionAllowList(Action.MANAGE_ENVIRONMENT))
    assert service.set_run_as(principal, "dev", "analytics", "etl").run_as == "etl"
    assert service.clear_run_as(principal, "dev", "analytics").run_as is None
