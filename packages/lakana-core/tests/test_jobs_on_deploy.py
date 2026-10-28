"""Jobs that start after each deploy of their project (`on_deploy: true`)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from core_test_helpers import ActionAllowList, _prepared_catalog
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from test_jobs_service import _events, _grant_admin, _person
from test_projects_deploys import _claimed, _deployed_jobs, _job, _repo

import lakana_core.jobs.on_deploy as on_deploy_module
from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.jobs.on_deploy import start_jobs_after_deploy
from lakana_core.jobs.service import JobService, StartResult
from lakana_core.models import (
    AuditEventType,
    DeployKind,
    DeployRun,
    DeployStatus,
    Environment,
    JobSchedule,
    Run,
    RunStatus,
    RunTrigger,
)
from lakana_core.projects.dag import ProjectGraph, build_graph
from lakana_core.projects.deploy_jobs import (
    OUTCOME_NOT_STARTED,
    OUTCOME_PENDING,
    OUTCOME_STARTED,
    OUTCOME_WAITING,
    REASON_ERROR,
    REASON_LACKS_RUN,
    REASON_LOST,
    REASON_REFUSED,
    DeployJobStart,
    job_starts_view,
    pending_job_starts,
)
from lakana_core.projects.deploys import DeployService
from lakana_core.projects.diff import diff_manifests
from lakana_core.projects.issues import ProjectValidationError
from lakana_core.projects.manifest import build_manifest, job_on_deploy, manifest_jobs
from lakana_core.projects.project_config import (
    JobConfig,
    ResolvedProjectConfig,
    parse_project_config,
)
from lakana_core.projects.run_policy import RunCaps

_BRONZE = NamespacePath.from_parts(["bronze"])
_CAPS = RunCaps(max_retries=5, max_timeout_seconds=600)
_REFRESH = {"refresh": JobConfig(select=["silver.*"], on_deploy=True), "daily": _job()}


@pytest.fixture
def env(session: Session, tmp_path: Path, principal: PrincipalRef) -> None:
    """Create `dev` with `bronze.events`, and make the principal an admin who may `RUN`.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The deployer.
    """
    _prepared_catalog(
        session,
        tmp_path,
        AllowAllAuthorization(),
        namespaces=[_BRONZE],
        tables=[TableRef("dev", _BRONZE, "events")],
    )
    _grant_admin(session, principal.id)


def _start(
    session: Session, run: DeployRun, p: PrincipalRef, authz: object | None = None
) -> Sequence[DeployJobStart]:
    """Start the jobs of a recorded deploy, as the worker does after it.

    Parameters
    ----------
    session
        Database session.
    run
        The deploy run.
    p
        The deployer.
    authz
        Authorization; allow-all when `None`.

    Returns
    -------
    list of DeployJobStart
        What happened to each job.
    """
    return start_jobs_after_deploy(
        session,
        authz or AllowAllAuthorization(),  # ty: ignore[invalid-argument-type]
        run.id,
        p,
        caps=_CAPS,
    )


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


def _view(session: Session, run: DeployRun, p: PrincipalRef) -> tuple[DeployJobStart, ...]:
    """Read a deploy's job starts as the API shows them.

    Parameters
    ----------
    session
        Database session.
    run
        The deploy run.
    p
        The reader.

    Returns
    -------
    tuple of DeployJobStart
        The starts.
    """
    session.expire_all()
    return DeployService(session, AllowAllAuthorization()).get(p, "dev", run.id).job_starts


# -- lakana.yaml and the manifest ---------------------------------------------


def test_on_deploy_is_off_unless_a_job_asks_for_it() -> None:
    """The default changes nothing: a job without the key keeps its manifest entry as it was."""
    config = parse_project_config(
        "jobs:\n  plain:\n    select: ['silver.*']\n"
        "  refresh:\n    select: ['silver.*']\n    on_deploy: true\n"
    )
    assert (config.jobs["plain"].on_deploy, config.jobs["refresh"].on_deploy) == (False, True)
    jobs = manifest_jobs(build_manifest([], _graph(), config.resolved_for("dev")))
    assert "on_deploy" not in jobs["plain"]
    assert jobs["refresh"]["on_deploy"] is True
    assert (job_on_deploy(jobs["plain"]), job_on_deploy(jobs["refresh"])) == (False, True)


def test_on_deploy_must_be_true_or_false() -> None:
    """A quoted value is refused with the file, the line and the key, like any other key."""
    with pytest.raises(ProjectValidationError) as info:
        parse_project_config("jobs:\n  refresh:\n    select: ['silver.*']\n    on_deploy: 'yes'\n")
    (issue,) = info.value.issues
    assert (issue.line, issue.message.split(":")[0]) == (4, "jobs.refresh.on_deploy")


def test_a_damaged_stored_value_does_not_count_as_on() -> None:
    """Only the boolean `true` turns it on: a stored string or number does not."""
    assert not job_on_deploy({"on_deploy": "true"})
    assert not job_on_deploy({"on_deploy": 1})
    assert not job_on_deploy({})


def test_an_environment_turns_on_deploy_on_or_off() -> None:
    """An override changes `on_deploy` for one environment and keeps the rest of the job."""
    config = parse_project_config(
        "jobs:\n  refresh:\n    select: ['silver.*']\n    on_deploy: true\n"
        "environments:\n  prod:\n    jobs:\n      refresh: { on_deploy: false }\n"
        "  test:\n    jobs:\n      refresh: { select: ['gold.*'] }\n"
    )
    assert config.resolved_for("prod").jobs["refresh"].on_deploy is False
    assert config.resolved_for("dev").jobs["refresh"].on_deploy is True
    assert config.resolved_for("test").jobs["refresh"].on_deploy is True


def test_the_diff_names_a_change_of_on_deploy() -> None:
    """A dry run shows that a job now starts after each deploy, or no longer does."""
    off = build_manifest([], _graph(), ResolvedProjectConfig(jobs={"refresh": _job(cron=None)}))
    on = build_manifest(
        [],
        _graph(),
        ResolvedProjectConfig(jobs={"refresh": JobConfig(select=["silver.*"], on_deploy=True)}),
    )
    assert diff_manifests(off, on, existing_unowned=set()).jobs_changed == ("refresh (on deploy)",)
    assert diff_manifests(on, off, existing_unowned=set()).jobs_changed == ("refresh (on deploy)",)
    assert diff_manifests(on, on, existing_unowned=set()).jobs_changed == ()


def test_pending_starts_name_the_on_deploy_jobs_in_name_order() -> None:
    """The list a deploy stores holds only the jobs that asked, and is `None` when none did."""
    manifest = build_manifest(
        [],
        _graph(),
        ResolvedProjectConfig(
            jobs={
                "zeta": JobConfig(select=["a.*"], on_deploy=True),
                "alpha": JobConfig(select=["a.*"], on_deploy=True),
                "plain": JobConfig(select=["a.*"]),
            }
        ),
    )
    assert pending_job_starts(manifest) == [
        {"job": "alpha", "outcome": OUTCOME_PENDING},
        {"job": "zeta", "outcome": OUTCOME_PENDING},
    ]
    assert pending_job_starts({"jobs": {"plain": {"select": ["a.*"]}}}) is None
    assert pending_job_starts({}) is None


def _graph() -> ProjectGraph:
    """Build the DAG of a project without assets.

    Returns
    -------
    ProjectGraph
        An empty graph.
    """
    return build_graph([])


# -- a deploy starts its jobs ----------------------------------------------------


def test_a_deploy_starts_each_on_deploy_job_as_its_deployer(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The run is the job's, with the trigger `deploy`, the deployer as its requester."""
    run = _deployed_jobs(session, principal, _repo(session), "a" * 40, _REFRESH)
    assert run.job_starts_json == [{"job": "refresh", "outcome": OUTCOME_PENDING}]

    (outcome,) = _start(session, run, principal)

    (started,) = _runs(session)
    assert outcome == DeployJobStart(job="refresh", outcome=OUTCOME_STARTED, run_id=started.id)
    assert (started.job, started.trigger, started.triggered_by) == (
        "refresh",
        RunTrigger.DEPLOY,
        principal.id,
    )
    (event,) = _events(session, AuditEventType.RUN_STARTED)
    assert (event.principal_id, event.detail["trigger"], event.detail["job"]) == (
        principal.id,
        "deploy",
        "refresh",
    )
    assert _view(session, run, principal) == (outcome,)


def test_a_busy_project_makes_the_start_wait_like_any_other(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The second deploy meets the first deploy's run: its start waits, audited as waiting."""
    repo = _repo(session)
    first = _deployed_jobs(session, principal, repo, "a" * 40, _REFRESH)
    _start(session, first, principal)
    (active,) = _runs(session)

    second = _deployed_jobs(session, principal, repo, "b" * 40, _REFRESH)
    (outcome,) = _start(session, second, principal)

    assert outcome == DeployJobStart(
        job="refresh", outcome=OUTCOME_WAITING, waits_for_run_id=active.id
    )
    assert len(_runs(session)) == 1
    row = session.scalars(select(JobSchedule).where(JobSchedule.job == "refresh")).one()
    assert row.pending_start is not None
    assert (row.pending_start["trigger"], row.pending_start["requested_by"]) == (
        "deploy",
        str(principal.id),
    )
    (event,) = _events(session, AuditEventType.JOB_START_WAITING)
    assert (event.detail["request_trigger"], event.detail["waits_for_run_id"]) == (
        "deploy",
        str(active.id),
    )
    assert _view(session, second, principal) == (outcome,)


def test_a_waiting_deploy_start_runs_when_the_project_is_free(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The waiting start is checked again as the deployer and runs with the trigger `deploy`."""
    repo = _repo(session)
    _start(session, _deployed_jobs(session, principal, repo, "a" * 40, _REFRESH), principal)
    _start(session, _deployed_jobs(session, principal, repo, "b" * 40, _REFRESH), principal)
    session.execute(update(Run).values(status=RunStatus.SUCCEEDED))
    session.commit()
    row = session.scalars(select(JobSchedule).where(JobSchedule.job == "refresh")).one()

    started = JobService.start_waiting(session, row, caps=_CAPS)

    assert started is not None
    assert (started.trigger, started.triggered_by) == (RunTrigger.DEPLOY, principal.id)


def test_a_waiting_deploy_start_is_dropped_when_the_deployer_lost_run(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A deployer cut off while the start waited never gets the run."""
    repo = _repo(session)
    _start(session, _deployed_jobs(session, principal, repo, "a" * 40, _REFRESH), principal)
    nobody = _person(session, "nobody")
    _start(session, _deployed_jobs(session, nobody, repo, "b" * 40, _REFRESH), nobody)
    session.execute(update(Run).values(status=RunStatus.SUCCEEDED))
    session.commit()
    row = session.scalars(select(JobSchedule).where(JobSchedule.job == "refresh")).one()

    assert JobService.start_waiting(session, row, caps=_CAPS) is None

    (dropped,) = _events(session, AuditEventType.JOB_START_DROPPED)
    assert (dropped.detail["reason"], dropped.detail["trigger"]) == (
        "requester_lacks_run",
        "deploy",
    )
    assert len(_runs(session)) == 1


def test_a_deployer_without_run_deploys_but_starts_nothing(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The deploy stands; the output names the job, the missing right and both next steps."""
    run = _deployed_jobs(session, principal, _repo(session), "a" * 40, _REFRESH)
    authz = AuditingAuthorization(
        ActionAllowList(Action.DEPLOY, Action.READ_ENVIRONMENT), session, DenialAuditLimiter(60)
    )

    (outcome,) = _start(session, run, principal, authz)

    assert (outcome.job, outcome.outcome, outcome.reason) == (
        "refresh",
        OUTCOME_NOT_STARTED,
        REASON_LACKS_RUN,
    )
    assert outcome.message == (
        f"job `refresh` was not started: {principal.name} may not RUN in `dev`. Grant RUN with "
        f"`lakana grant create --principal {principal.name} --action RUN --kind ENVIRONMENT "
        "--env dev`, or start the job yourself with "
        "`lakana job run refresh --repo analytics --env dev`."
    )
    assert _runs(session) == []
    session.expire_all()
    assert session.get(DeployRun, run.id).status is DeployStatus.SUCCEEDED  # ty: ignore
    (denied,) = _events(session, AuditEventType.AUTHORIZATION_DENIED)
    assert (denied.principal_id, denied.detail["action"]) == (principal.id, "RUN")
    assert _view(session, run, principal) == (outcome,)


def test_a_protected_environment_without_a_run_principal_refuses_as_a_start_by_hand(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The rules of a start by the deployer apply: no bound principal, no run."""
    run = _deployed_jobs(session, principal, _repo(session), "a" * 40, _REFRESH)
    session.execute(update(Environment).values(is_protected=True))
    session.commit()

    (outcome,) = _start(session, run, principal)

    assert (outcome.outcome, outcome.reason) == (OUTCOME_NOT_STARTED, REASON_REFUSED)
    assert outcome.message is not None
    assert outcome.message.startswith("job `refresh` was not started: ")
    assert "lakana deployments set-run-as" in outcome.message
    assert _runs(session) == []


def test_a_failed_deploy_starts_nothing(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A deploy that fails stores no start, so the worker has nothing to start."""
    repo = _repo(session)
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(principal, "dev", repository=repo.name, kind=DeployKind.DEPLOY)
    run = _claimed(session, session.get(DeployRun, view.id))
    service.fail(run, "the project has errors", worker_id="w1")

    assert run.job_starts_json is None
    assert _start(session, run, principal) == []
    assert _runs(session) == []
    assert _view(session, run, principal) == ()


def test_a_deploy_of_the_commit_already_current_starts_nothing(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """Only a deploy that makes another project version current starts the jobs."""
    repo = _repo(session)
    _start(session, _deployed_jobs(session, principal, repo, "a" * 40, _REFRESH), principal)
    session.execute(update(Run).values(status=RunStatus.SUCCEEDED))
    session.commit()

    again = _deployed_jobs(session, principal, repo, "a" * 40, _REFRESH)

    assert again.job_starts_json is None
    assert _start(session, again, principal) == []
    assert len(_runs(session)) == 1


def test_a_deploy_back_to_an_earlier_commit_starts_the_jobs(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """Going back to a commit deployed before changes the current version, so it counts."""
    repo = _repo(session)
    _deployed_jobs(session, principal, repo, "a" * 40, _REFRESH)
    _deployed_jobs(session, principal, repo, "b" * 40, _REFRESH)

    back = _deployed_jobs(session, principal, repo, "a" * 40, _REFRESH)

    assert back.job_starts_json == [{"job": "refresh", "outcome": OUTCOME_PENDING}]


def test_a_rollback_starts_nothing(session: Session, env: None, principal: PrincipalRef) -> None:
    """A rollback is not a deploy: it puts an earlier version back and starts no job."""
    repo = _repo(session)
    _deployed_jobs(session, principal, repo, "a" * 40, _REFRESH)
    _deployed_jobs(session, principal, repo, "b" * 40, _REFRESH)
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(
        principal, "dev", repository=repo.name, kind=DeployKind.ROLLBACK, to_previous=True
    )
    run = _claimed(session, session.get(DeployRun, view.id))
    service.execute_rollback(principal, run, worker_id="w1")

    assert run.job_starts_json is None
    assert _start(session, run, principal) == []


def test_starting_twice_starts_each_job_once(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """Only a start that is still pending is made; a second call changes nothing."""
    run = _deployed_jobs(session, principal, _repo(session), "a" * 40, _REFRESH)
    first = _start(session, run, principal)

    assert _start(session, run, principal) == []
    assert len(_runs(session)) == 1
    assert _view(session, run, principal) == tuple(first)


def test_a_start_of_an_unknown_deploy_starts_nothing(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A deploy run that is gone has nothing to start."""
    gone = DeployRun(id=uuid.uuid4())
    assert _start(session, gone, principal) == []


def test_one_job_that_fails_unexpectedly_does_not_stop_the_others(
    session: Session, env: None, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An internal error is recorded for its job, and the next job still starts."""
    jobs = {
        "alpha": JobConfig(select=["silver.*"], on_deploy=True),
        "beta": JobConfig(select=["silver.*"], on_deploy=True),
    }
    run = _deployed_jobs(session, principal, _repo(session), "a" * 40, jobs)
    real = JobService.start

    def flaky(
        self: JobService, p: PrincipalRef, environment: str, repository: str, job: str, **kw: object
    ) -> StartResult:
        if job == "alpha":
            raise RuntimeError("database went away")
        return real(self, p, environment, repository, job, **kw)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(JobService, "start", flaky)

    alpha, beta = _start(session, run, principal)

    assert (alpha.outcome, alpha.reason) == (OUTCOME_NOT_STARTED, REASON_ERROR)
    assert alpha.message == (
        "job `alpha` was not started because of an internal error; the worker's log has the "
        "details. Start it with `lakana job run alpha --repo analytics --env dev`."
    )
    assert beta.outcome == OUTCOME_STARTED
    assert _view(session, run, principal) == (alpha, beta)


def test_an_outcome_that_cannot_be_recorded_is_logged_and_the_rest_go_on(
    session: Session,
    env: None,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure to write one outcome does not stop the starts; the entry stays pending."""
    run = _deployed_jobs(session, principal, _repo(session), "a" * 40, _REFRESH)
    real = on_deploy_module._write_outcome

    def broken(session: Session, run_id: uuid.UUID, outcome: DeployJobStart) -> None:
        session.rollback()
        raise RuntimeError("the database is gone")

    monkeypatch.setattr(on_deploy_module, "_write_outcome", broken)

    (outcome,) = _start(session, run, principal)

    assert outcome.outcome == OUTCOME_STARTED
    assert "could not record" in caplog.text
    monkeypatch.setattr(on_deploy_module, "_write_outcome", real)
    (shown,) = _view(session, run, principal)
    assert shown.outcome == OUTCOME_PENDING


# -- what the deploy shows -------------------------------------------------------


def test_a_start_left_pending_past_the_lease_shows_as_lost(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A worker that stopped between the deploy and the start leaves a start nobody makes."""
    run = _deployed_jobs(session, principal, _repo(session), "a" * 40, _REFRESH)
    assert _view(session, run, principal) == (
        DeployJobStart(job="refresh", outcome=OUTCOME_PENDING),
    )
    session.execute(
        update(DeployRun)
        .where(DeployRun.id == run.id)
        .values(finished_at=datetime.now(UTC) - timedelta(seconds=61))
    )
    session.commit()

    (shown,) = _view(session, run, principal)

    assert (shown.outcome, shown.reason) == (OUTCOME_NOT_STARTED, REASON_LOST)
    assert shown.message == (
        "job `refresh` was not started: the worker stopped before it started the job. Start it "
        "with `lakana job run refresh --repo analytics --env dev`."
    )


def test_damaged_stored_starts_are_left_out() -> None:
    """A stored list from a damaged row shows only the entries that can be read."""
    run_id = uuid.uuid4()
    stored = [
        "not a mapping",
        {"outcome": "started"},
        {"job": "a", "outcome": "started", "run_id": str(run_id)},
        {"job": "b", "outcome": "started", "run_id": "not a uuid"},
        {"job": "c", "outcome": "waiting", "waits_for_run_id": str(run_id), "message": 3},
    ]
    shown = job_starts_view(stored, lost=False, repository="r", environment="dev")
    assert shown == (
        DeployJobStart(job="a", outcome="started", run_id=run_id),
        DeployJobStart(job="b", outcome="started"),
        DeployJobStart(job="c", outcome="waiting", waits_for_run_id=run_id),
    )
    assert job_starts_view({"job": "x"}, lost=False, repository="r", environment="dev") == ()
    assert job_starts_view(None, lost=True, repository="r", environment="dev") == ()


def test_the_job_view_says_the_job_starts_after_each_deploy(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """`lakana job list` reads it from the current project version."""
    _deployed_jobs(session, principal, _repo(session), "a" * 40, _REFRESH)

    views = {
        view.name: view
        for view in JobService(session, AllowAllAuthorization()).list(principal, "dev")
    }

    assert (views["refresh"].on_deploy, views["daily"].on_deploy) == (True, False)
