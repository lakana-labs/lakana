"""A CI trust rule that opts in to starting the `on_deploy` jobs of the deploys it accepts."""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path

import pytest
from ci_trust_test_helpers import (
    SHA,
    CiWorld,
    add_rule,
    authenticate,
    ci_world,
    exchange,
    rules,
)
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from test_projects_dag import make_asset

from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    PrincipalRef,
    Securable,
    SecurableKind,
)
from lakana_core.errors import PermissionDenied
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.service import GrantService
from lakana_core.jobs import on_deploy as on_deploy_module
from lakana_core.jobs.on_deploy import start_jobs_after_deploy
from lakana_core.jobs.service import JobService
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    CiToken,
    CiTrustRule,
    DeployKind,
    DeployRun,
    DeployStatus,
    JobSchedule,
    Repository,
    Run,
    RunStatus,
    RunTrigger,
)
from lakana_core.projects.dag import build_graph
from lakana_core.projects.deploy_jobs import (
    OUTCOME_NOT_STARTED,
    OUTCOME_STARTED,
    OUTCOME_WAITING,
    REASON_LACKS_RUN,
    REASON_REFUSED,
)
from lakana_core.projects.deploys import DeployService
from lakana_core.projects.diff import DeployDiff
from lakana_core.projects.manifest import build_manifest
from lakana_core.projects.project_config import JobConfig, ResolvedProjectConfig
from lakana_core.projects.run_policy import RunCaps
from lakana_core.runs.selection import Selection
from lakana_core.runs.service import RunService

_CAPS = RunCaps(max_retries=5, max_timeout_seconds=600)
_JOBS = {
    "refresh": JobConfig(select=["silver.*"], on_deploy=True),
    "plain": JobConfig(select=["silver.*"]),
}


@pytest.fixture
def world(session: Session, tmp_path: Path) -> CiWorld:
    """Build the CI world: ``prod``, ``analytics``, an administrator and ``deployer``.

    ``deployer`` holds `DEPLOY` on ``prod`` and no `RUN`.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.

    Returns
    -------
    CiWorld
        The world.
    """
    return ci_world(session, tmp_path)


def _authz(session: Session) -> AuditingAuthorization:
    """Build the worker's authorizer: grants, with audited denials.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    AuditingAuthorization
        The authorizer.
    """
    return AuditingAuthorization(GrantAuthorization(session), session, DenialAuditLimiter(1000))


def _ci_deploy(
    session: Session, sha: str = SHA, jobs: dict[str, JobConfig] | None = None
) -> tuple[PrincipalRef, DeployRun]:
    """Exchange an ID token, queue a deploy with the CI token and record it, as the worker does.

    Parameters
    ----------
    session
        Database session.
    sha
        The commit the deploy records.
    jobs
        The jobs of its manifest; `_JOBS` by default.

    Returns
    -------
    tuple of (PrincipalRef, DeployRun)
        The CI-scoped deployer and the finished run.
    """
    ci = authenticate(session, exchange(session))
    service = DeployService(session, _authz(session))
    view = service.submit(ci, "prod", repository="analytics", kind=DeployKind.DEPLOY)
    run = session.get(DeployRun, view.id)
    assert run is not None
    run.status, run.worker_id, run.resolved_sha = DeployStatus.RUNNING, "w1", sha
    session.commit()
    asset = make_asset("silver.clean", ())
    current = service.current_job_version(run.environment_id, run.repository_id)
    plain = dataclasses.replace(ci, ci_scope=None)
    service.record_deploy(
        plain,
        run,
        manifest=build_manifest(
            [asset], build_graph([asset]), ResolvedProjectConfig(jobs=jobs or _JOBS)
        ),
        code_uri=f"file:///prod/_artifacts/code/analytics/{sha}.tar.gz",
        code_sha256="0" * 64,
        diff=DeployDiff(),
        expected_job_version_id=current.id if current is not None else None,
        worker_id="w1",
    )
    return ci, run


def _start(session: Session, ci: PrincipalRef, run: DeployRun) -> list:
    """Start the deploy's jobs as the worker does, with the token's scope.

    Parameters
    ----------
    session
        Database session.
    ci
        The CI-scoped deployer.
    run
        The deploy run.

    Returns
    -------
    list of DeployJobStart
        The outcomes.
    """
    return list(start_jobs_after_deploy(session, _authz(session), run.id, ci, caps=_CAPS))


def _runs(session: Session) -> list[Run]:
    """Read every run, oldest first.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of Run
        The runs.
    """
    session.expire_all()
    return list(session.scalars(select(Run).order_by(Run.created_at)))


def _events(session: Session, kind: AuditEventType) -> list[AuditEvent]:
    """Read the audit events of one type, oldest first.

    Parameters
    ----------
    session
        Database session.
    kind
        The type.

    Returns
    -------
    list of AuditEvent
        The events.
    """
    session.expire_all()
    return list(
        session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == kind).order_by(AuditEvent.occurred_at)
        )
    )


def _grant_run(session: Session, world: CiWorld) -> None:
    """Give ``deployer`` `RUN` on ``prod``, so that only a CI trust rule can refuse its start.

    Parameters
    ----------
    session
        Database session.
    world
        The world.
    """
    GrantService(session, AllowAllAuthorization()).grant(
        world.admin,
        principal="deployer",
        action=Action.RUN,
        kind=SecurableKind.ENVIRONMENT,
        environment="prod",
    )


def test_a_rule_that_opts_in_starts_the_on_deploy_job_of_its_deploy(
    session: Session, world: CiWorld
) -> None:
    """The deployer holds no `RUN`: the rule's setting is the grant, and the audit says so."""
    rule_id = add_rule(session, world, start_on_deploy_jobs=True)
    ci, run = _ci_deploy(session)

    (outcome,) = _start(session, ci, run)

    (started,) = _runs(session)
    assert (outcome.job, outcome.outcome, outcome.run_id) == (
        "refresh",
        OUTCOME_STARTED,
        started.id,
    )
    assert (started.job, started.trigger, started.triggered_by) == (
        "refresh",
        RunTrigger.DEPLOY,
        world.deployer.id,
    )
    (event,) = _events(session, AuditEventType.RUN_STARTED)
    assert (event.principal_id, event.detail["trigger"]) == (world.deployer.id, "deploy")
    assert (event.detail["ci_rule_id"], event.detail["allowed_by"]) == (
        str(rule_id),
        "start_on_deploy_jobs",
    )


def test_a_rule_that_does_not_opt_in_starts_nothing_and_names_the_setting(
    session: Session, world: CiWorld
) -> None:
    rule_id = add_rule(session, world)
    ci, run = _ci_deploy(session)

    (outcome,) = _start(session, ci, run)

    assert (outcome.outcome, outcome.reason) == (OUTCOME_NOT_STARTED, REASON_LACKS_RUN)
    assert outcome.message == (
        "job `refresh` was not started: deployer deployed with a CI token, and its CI trust rule "
        "does not start the deploy's jobs. An instance administrator can turn that on with "
        f"`lakana ci-trust update {rule_id} --start-on-deploy-jobs`; or start the job with "
        "`lakana job run refresh --repo analytics --env prod` and a stored token that holds RUN."
    )
    assert _runs(session) == []


def test_the_setting_allows_no_other_run(session: Session, world: CiWorld) -> None:
    """The token itself still may only deploy; a job without `on_deploy` is never started."""
    add_rule(session, world, start_on_deploy_jobs=True)
    ci, run = _ci_deploy(session)
    jobs = JobService(session, _authz(session), caps=_CAPS)

    with pytest.raises(PermissionDenied):
        jobs.start(ci, "prod", "analytics", "plain")
    with pytest.raises(PermissionDenied):
        jobs.start(ci, "prod", "analytics", "refresh")
    with pytest.raises(PermissionDenied):
        RunService(session, _authz(session)).start(ci, "prod", "analytics", Selection(patterns=()))
    assert [outcome.job for outcome in _start(session, ci, run)] == ["refresh"]
    assert [r.job for r in _runs(session)] == ["refresh"]


def test_the_start_is_allowed_in_the_rules_environment_only(
    session: Session, world: CiWorld
) -> None:
    """Even while starting the deploy's jobs, the token may `RUN` in its own environment only."""
    add_rule(session, world, start_on_deploy_jobs=True)
    ci = authenticate(session, exchange(session))
    assert ci.ci_scope is not None
    starting = dataclasses.replace(
        ci, ci_scope=dataclasses.replace(ci.ci_scope, starting_deploy_jobs=True)
    )
    authz = GrantAuthorization(session)

    assert authz.permits(starting, Action.RUN, Securable.environment_scope("prod"))
    assert not authz.permits(starting, Action.RUN, Securable.environment_scope("dev"))
    assert not authz.permits(starting, Action.DROP_TABLE, Securable.environment_scope("prod"))
    assert not authz.permits(ci, Action.RUN, Securable.environment_scope("prod"))


def test_a_waiting_start_of_a_ci_deploy_runs_while_the_rule_allows_it(
    session: Session, world: CiWorld
) -> None:
    add_rule(session, world, start_on_deploy_jobs=True)
    ci, first = _ci_deploy(session)
    _start(session, ci, first)
    (active,) = _runs(session)
    ci_again, second = _ci_deploy(session, sha="1" * 40)

    (outcome,) = _start(session, ci_again, second)

    assert (outcome.outcome, outcome.waits_for_run_id) == (OUTCOME_WAITING, active.id)
    (waiting,) = _events(session, AuditEventType.JOB_START_WAITING)
    assert waiting.detail["ci_rule_id"] == str(ci_again.ci_scope.rule_id)  # ty: ignore
    session.execute(update(Run).values(status=RunStatus.SUCCEEDED))
    session.commit()
    row = session.scalars(select(JobSchedule).where(JobSchedule.job == "refresh")).one()

    started = JobService.start_waiting(session, row, caps=_CAPS)

    assert started is not None
    assert (started.trigger, started.triggered_by) == (RunTrigger.DEPLOY, world.deployer.id)
    last = _events(session, AuditEventType.RUN_STARTED)[-1]
    assert last.detail["allowed_by"] == "start_on_deploy_jobs"


def test_a_waiting_start_is_dropped_once_the_rule_no_longer_allows_it(
    session: Session, world: CiWorld
) -> None:
    rule_id = add_rule(session, world, start_on_deploy_jobs=True)
    ci, first = _ci_deploy(session)
    _start(session, ci, first)
    ci_again, second = _ci_deploy(session, sha="1" * 40)
    _start(session, ci_again, second)
    # The account may RUN on its own: only the rule's re-check can refuse the start.
    _grant_run(session, world)
    rules(session).update(world.admin, rule_id, start_on_deploy_jobs=False)
    session.execute(update(Run).values(status=RunStatus.SUCCEEDED))
    session.commit()
    row = session.scalars(select(JobSchedule).where(JobSchedule.job == "refresh")).one()

    assert JobService.start_waiting(session, row, caps=_CAPS) is None

    (dropped,) = _events(session, AuditEventType.JOB_START_DROPPED)
    assert dropped.detail["reason"] == "requester_lacks_run"
    assert len(_runs(session)) == 1


@pytest.mark.parametrize("stored", ["missing", "not a uuid"])
def test_a_waiting_start_whose_token_is_gone_is_dropped(
    session: Session, world: CiWorld, stored: str
) -> None:
    add_rule(session, world, start_on_deploy_jobs=True)
    ci, first = _ci_deploy(session)
    _start(session, ci, first)
    ci_again, second = _ci_deploy(session, sha="1" * 40)
    _start(session, ci_again, second)
    row = session.scalars(select(JobSchedule).where(JobSchedule.job == "refresh")).one()
    token_id = str(uuid.uuid4()) if stored == "missing" else stored
    row.pending_start = {**row.pending_start, "ci_token_id": token_id}  # ty: ignore
    session.execute(update(Run).values(status=RunStatus.SUCCEEDED))
    session.commit()

    assert JobService.start_waiting(session, row, caps=_CAPS) is None
    (dropped,) = _events(session, AuditEventType.JOB_START_DROPPED)
    assert dropped.detail["reason"] == "requester_lacks_run"


def test_the_setting_is_an_audited_rule_change_for_administrators_only(
    session: Session, world: CiWorld
) -> None:
    rule_id = add_rule(session, world)
    service = rules(session)
    assert service.get(world.admin, rule_id).start_on_deploy_jobs is False
    with pytest.raises(PermissionDenied):
        service.update(world.deployer, rule_id, start_on_deploy_jobs=True)
    with pytest.raises(PermissionDenied):
        service.create(
            world.deployer,
            issuer="github",
            principal="deployer",
            environment="prod",
            repository="analytics",
            kinds=["deploy"],
            conditions={"repository_id": "1", "ref": "refs/heads/main", "event_name": "push"},
            start_on_deploy_jobs=True,
        )
    revision = session.get(CiTrustRule, rule_id).revision  # ty: ignore

    view = service.update(world.admin, rule_id, start_on_deploy_jobs=True)

    assert view.start_on_deploy_jobs is True
    session.expire_all()
    assert session.get(CiTrustRule, rule_id).revision == revision + 1  # ty: ignore
    created, updated = (
        _events(session, AuditEventType.CI_TRUST_RULE_CREATED)[-1],
        _events(session, AuditEventType.CI_TRUST_RULE_UPDATED)[-1],
    )
    assert (created.detail["start_on_deploy_jobs"], updated.detail["start_on_deploy_jobs"]) == (
        False,
        True,
    )


def _plain_deploy(session: Session, world: CiWorld, sha: str, jobs: dict[str, JobConfig]) -> None:
    """Deploy ``analytics`` to ``prod`` as the administrator, without a CI token.

    Parameters
    ----------
    session
        Database session.
    world
        The world.
    sha
        The commit.
    jobs
        The jobs of the manifest.
    """
    service = DeployService(session, _authz(session))
    view = service.submit(
        world.admin, "prod", repository="analytics", kind=DeployKind.DEPLOY, ref=sha
    )
    run = session.get(DeployRun, view.id)
    assert run is not None
    run.status, run.worker_id, run.resolved_sha = DeployStatus.RUNNING, "w1", sha
    session.commit()
    asset = make_asset("silver.clean", ())
    current = service.current_job_version(run.environment_id, run.repository_id)
    service.record_deploy(
        world.admin,
        run,
        manifest=build_manifest([asset], build_graph([asset]), ResolvedProjectConfig(jobs=jobs)),
        code_uri=f"file:///prod/_artifacts/code/analytics/{sha}.tar.gz",
        code_sha256="0" * 64,
        diff=DeployDiff(),
        expected_job_version_id=current.id if current is not None else None,
        worker_id="w1",
    )


_BY_HAND = {"refresh": JobConfig(select=["silver.*"]), "plain": JobConfig(select=["silver.*"])}


@pytest.mark.parametrize("change", ["opt_out", "disable"])
def test_a_rule_changed_during_the_deploy_starts_nothing(
    session: Session, world: CiWorld, change: str
) -> None:
    """The rule is read again after the deploy committed, not taken from when it was claimed."""
    rule_id = add_rule(session, world, start_on_deploy_jobs=True)
    ci, run = _ci_deploy(session)
    # The account may RUN on its own: only the rule's re-check can refuse the start.
    _grant_run(session, world)
    if change == "opt_out":
        rules(session).update(world.admin, rule_id, start_on_deploy_jobs=False)
    else:
        rules(session).disable(world.admin, rule_id)

    (outcome,) = _start(session, ci, run)

    assert (outcome.outcome, outcome.reason) == (OUTCOME_NOT_STARTED, REASON_LACKS_RUN)
    assert outcome.message is not None
    assert "changed, disabled or deleted since the deploy began" in outcome.message
    assert _runs(session) == []
    (dropped,) = _events(session, AuditEventType.JOB_START_DROPPED)
    assert dropped.principal_id is None
    assert dropped.detail["reason"] == "requester_lacks_run"
    assert (dropped.detail["job"], dropped.detail["trigger"]) == ("refresh", "deploy")
    assert dropped.detail["requested_by"] == str(world.deployer.id)
    assert dropped.detail["ci_rule_id"] == str(rule_id)


def test_a_job_no_longer_on_deploy_in_the_current_version_is_not_started(
    session: Session, world: CiWorld
) -> None:
    """A deploy by hand moved the pointer before the start: its job is not `on_deploy`."""
    add_rule(session, world, start_on_deploy_jobs=True)
    ci, run = _ci_deploy(session)
    _plain_deploy(session, world, "2" * 40, _BY_HAND)

    (outcome,) = _start(session, ci, run)

    assert (outcome.outcome, outcome.reason) == (OUTCOME_NOT_STARTED, REASON_REFUSED)
    assert outcome.message == (
        "job `refresh` was not started: it no longer has `on_deploy: true` in the project version "
        "now current in `prod`, and the CI trust rule allows only those. Start it with "
        "`lakana job run refresh --repo analytics --env prod` and a stored token that holds RUN."
    )
    assert _runs(session) == []
    (dropped,) = _events(session, AuditEventType.JOB_START_DROPPED)
    assert (dropped.detail["reason"], dropped.detail["ci_rule_id"]) == (
        "not_on_deploy",
        str(ci.ci_scope.rule_id),  # ty: ignore
    )


def test_a_waiting_ci_start_whose_job_lost_on_deploy_is_dropped(
    session: Session, world: CiWorld
) -> None:
    add_rule(session, world, start_on_deploy_jobs=True)
    ci, first = _ci_deploy(session)
    _start(session, ci, first)
    ci_again, second = _ci_deploy(session, sha="1" * 40)
    _start(session, ci_again, second)
    _plain_deploy(session, world, "2" * 40, _BY_HAND)
    session.execute(update(Run).values(status=RunStatus.SUCCEEDED))
    session.commit()
    row = session.scalars(select(JobSchedule).where(JobSchedule.job == "refresh")).one()

    assert JobService.start_waiting(session, row, caps=_CAPS) is None

    (dropped,) = _events(session, AuditEventType.JOB_START_DROPPED)
    assert dropped.detail["reason"] == "not_on_deploy"
    assert len(_runs(session)) == 1


def test_a_start_whose_token_names_another_repository_is_not_made(
    session: Session, world: CiWorld
) -> None:
    """The rule's repository binding holds also for the start, not only for the deploy."""
    add_rule(session, world, start_on_deploy_jobs=True)
    ci, run = _ci_deploy(session)
    other = session.scalars(select(Repository.id).where(Repository.name == "other")).one()
    session.execute(update(CiToken).values(repository_id=other))
    session.execute(update(CiTrustRule).values(repository_id=other))
    session.commit()

    (outcome,) = _start(session, ci, run)

    assert (outcome.outcome, outcome.reason) == (OUTCOME_NOT_STARTED, REASON_LACKS_RUN)
    assert _runs(session) == []


def test_a_waiting_start_whose_token_names_another_repository_is_dropped(
    session: Session, world: CiWorld
) -> None:
    add_rule(session, world, start_on_deploy_jobs=True)
    ci, first = _ci_deploy(session)
    _start(session, ci, first)
    ci_again, second = _ci_deploy(session, sha="1" * 40)
    _start(session, ci_again, second)
    other = session.scalars(select(Repository.id).where(Repository.name == "other")).one()
    session.execute(update(CiToken).values(repository_id=other))
    session.execute(update(CiTrustRule).values(repository_id=other))
    session.execute(update(Run).values(status=RunStatus.SUCCEEDED))
    session.commit()
    row = session.scalars(select(JobSchedule).where(JobSchedule.job == "refresh")).one()

    assert JobService.start_waiting(session, row, caps=_CAPS) is None
    (dropped,) = _events(session, AuditEventType.JOB_START_DROPPED)
    assert dropped.detail["reason"] == "requester_lacks_run"


def test_a_ci_start_that_merges_into_a_waiting_scheduled_start_keeps_the_rule_in_the_audit(
    session: Session, world: CiWorld
) -> None:
    rule_id = add_rule(session, world, start_on_deploy_jobs=True)
    ci, first = _ci_deploy(session)
    _start(session, ci, first)
    row = session.scalars(select(JobSchedule).where(JobSchedule.job == "refresh")).one()
    row.pending_start = {
        "trigger": "schedule",
        "requested_at": "2026-10-28T03:00:00+00:00",
        "requested_by": None,
        "scheduled_for": "2026-10-28T03:00:00+00:00",
        "missed": 0,
        "merged": 1,
    }
    session.commit()
    ci_again, second = _ci_deploy(session, sha="1" * 40)

    (outcome,) = _start(session, ci_again, second)

    assert outcome.outcome == OUTCOME_WAITING
    (waiting,) = _events(session, AuditEventType.JOB_START_WAITING)
    assert (waiting.detail["trigger"], waiting.detail["merged"]) == ("schedule", 2)
    assert (waiting.detail["ci_rule_id"], waiting.detail["allowed_by"]) == (
        str(rule_id),
        "start_on_deploy_jobs",
    )


def test_a_rule_changed_while_the_jobs_start_stops_the_jobs_not_started_yet(
    session: Session, world: CiWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule is read again before each start, not once before the first."""
    rule_id = add_rule(session, world, start_on_deploy_jobs=True)
    jobs = {
        "first": JobConfig(select=["silver.*"], on_deploy=True),
        "second": JobConfig(select=["silver.*"], on_deploy=True),
    }
    ci, run = _ci_deploy(session, jobs=jobs)
    _grant_run(session, world)
    real = on_deploy_module._start_one

    def then_disable(*args: object) -> object:
        outcome = real(*args)  # ty: ignore
        rules(session).disable(world.admin, rule_id)
        return outcome

    monkeypatch.setattr(on_deploy_module, "_start_one", then_disable)

    first, second = _start(session, ci, run)

    assert (first.job, first.outcome) == ("first", OUTCOME_STARTED)
    assert (second.job, second.outcome, second.reason) == (
        "second",
        OUTCOME_NOT_STARTED,
        REASON_LACKS_RUN,
    )
    assert len(_runs(session)) == 1
    (dropped,) = _events(session, AuditEventType.JOB_START_DROPPED)
    assert (dropped.detail["job"], dropped.detail["reason"]) == ("second", "requester_lacks_run")
