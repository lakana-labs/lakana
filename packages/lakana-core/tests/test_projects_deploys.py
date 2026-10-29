"""`DeployService`: submission, records, rollback and the read side."""

from __future__ import annotations

import base64
import copy
import dataclasses
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from core_test_helpers import _prepared_catalog, ensure_table
from sqlalchemy import delete, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from sqlalchemy.sql import ClauseElement
from test_projects_dag import make_asset

import lakana_core.projects.deploys as deploys_module
from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.catalog.identity import resolve_table_id
from lakana_core.catalog.service import CatalogService
from lakana_core.connections.kinds import ConnectionKind
from lakana_core.connections.service import ConnectionService
from lakana_core.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.jobs.cron import next_fire_after
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Connection,
    DeployKind,
    DeployRun,
    DeployStatus,
    Environment,
    EnvironmentDeployment,
    JobFire,
    JobSchedule,
    JobVersion,
    Principal,
    PrincipalType,
    Repository,
    RepositoryKind,
    Secret,
    SecretUseKind,
    Table,
    TableType,
)
from lakana_core.projects.asset_model import ResolvedReference
from lakana_core.projects.checks import ParsedCheck, checks_listing
from lakana_core.projects.dag import build_graph
from lakana_core.projects.deploys import (
    CONFLICT_MESSAGE,
    DEPLOY_ENDED_MESSAGE,
    DeployService,
    OrphanReport,
)
from lakana_core.projects.diff import DeployDiff, diff_manifests
from lakana_core.projects.frontmatter import AssetMetadata
from lakana_core.projects.issues import ProjectValidationError
from lakana_core.projects.manifest import (
    build_manifest,
    entry_input_kinds,
    manifest_assets,
    manifest_tables,
)
from lakana_core.projects.parse import ParsedAsset
from lakana_core.projects.project_config import (
    JobConfig,
    NotifyConfig,
    ProjectConfig,
    ResolvedProjectConfig,
    ScheduleConfig,
)
from lakana_core.runs.store import DEPLOY_LOST_MESSAGE
from lakana_core.secrets.keys import EnvKeyProvider
from lakana_core.storage.resolver import StorageResolver

_BRONZE = NamespacePath.from_parts(["bronze"])
_SILVER = NamespacePath.from_parts(["silver"])


def _principal(session: Session, name: str) -> PrincipalRef:
    """Persist a principal.

    Parameters
    ----------
    session
        Database session.
    name
        Its name.

    Returns
    -------
    PrincipalRef
        A reference to the row.
    """
    ref = PrincipalRef(id=uuid.uuid4(), name=name, type=PrincipalType.USER)
    session.add(Principal(id=ref.id, type=ref.type, name=name, is_active=True))
    session.commit()
    return ref


def _repo(session: Session, name: str = "analytics") -> Repository:
    """Persist an https repository row directly.

    Parameters
    ----------
    session
        Database session.
    name
        Its name.

    Returns
    -------
    Repository
        The row.
    """
    repo = Repository(
        id=uuid.uuid4(), name=name, kind=RepositoryKind.HTTPS, url=f"https://h/{name}"
    )
    session.add(repo)
    session.commit()
    return repo


def _manifest(*assets: ParsedAsset) -> dict:
    """Build a manifest.

    Parameters
    ----------
    *assets
        The assets.

    Returns
    -------
    dict
        The manifest.
    """
    return build_manifest(list(assets), build_graph(list(assets)), ResolvedProjectConfig())


def _claimed(session: Session, run: DeployRun | None, worker: str = "w1") -> DeployRun:
    """Mark a queued run as claimed by a worker, the way `run_deploy` does.

    Parameters
    ----------
    session
        Database session; committed here.
    run
        The queued run (the result of `session.get`).
    worker
        The id of the claiming worker.

    Returns
    -------
    DeployRun
        The same run, `running` under `worker`.
    """
    assert run is not None
    run.status = DeployStatus.RUNNING
    run.worker_id = worker
    run.started_at = datetime.now(UTC)
    session.commit()
    return run


@pytest.fixture
def env(session: Session, tmp_path: Path) -> None:
    """Create `dev` with `bronze.events` and an empty `silver` namespace.

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
        namespaces=[_BRONZE, _SILVER],
        tables=[TableRef("dev", _BRONZE, "events")],
    )


def _deployed(
    session: Session, p: PrincipalRef, repo: Repository, sha: str, *assets: ParsedAsset
) -> JobVersion:
    """Submit and record a deploy of `assets` at `sha`.

    Parameters
    ----------
    session
        Database session.
    p
        The deployer.
    repo
        The repository.
    sha
        The commit.
    *assets
        The manifest's assets.

    Returns
    -------
    Project version
        The recorded version.
    """
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(p, "dev", repository=repo.name, kind=DeployKind.DEPLOY, ref="main")
    run = _claimed(session, session.get(DeployRun, view.id))
    assert run is not None
    run.resolved_sha = sha
    current = service.current_job_version(run.environment_id, repo.id)
    return service.record_deploy(
        p,
        run,
        manifest=_manifest(*assets),
        code_uri=f"file:///dev/_artifacts/code/{repo.name}/{sha}.tar.gz",
        code_sha256="0" * 64,
        diff=DeployDiff(added=tuple(a.table for a in assets)),
        expected_job_version_id=current.id if current else None,
        worker_id="w1",
    )


def test_record_deploy_keeps_the_size_of_the_code_archive(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A deploy records its archive's size; one that sends none leaves it unknown."""
    repo = _repo(session)
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(principal, "dev", repository=repo.name, kind=DeployKind.DEPLOY)
    run = _claimed(session, session.get(DeployRun, view.id))
    assert run is not None
    run.resolved_sha = "c" * 40
    version = service.record_deploy(
        principal,
        run,
        manifest=_manifest(),
        code_uri="u",
        code_sha256="0" * 64,
        code_bytes=4321,
        diff=DeployDiff(),
        expected_job_version_id=None,
        worker_id="w1",
    )
    assert version.code_bytes == 4321
    assert _deployed(session, principal, repo, "d" * 40).code_bytes is None


def test_submit_queues_a_run(session: Session, env: None, principal: PrincipalRef) -> None:
    """A deploy is queued with its ref; the view carries names, not ids."""
    _repo(session)
    view = DeployService(session, AllowAllAuthorization()).submit(
        principal, "dev", repository="analytics", kind=DeployKind.DEPLOY, ref="main"
    )
    assert (view.environment, view.repository, view.status, view.requested_ref) == (
        "dev",
        "analytics",
        DeployStatus.QUEUED,
        "main",
    )


def test_submit_needs_deploy(session: Session, env: None) -> None:
    """Without DEPLOY on the environment the submission is refused."""
    _repo(session)
    stranger = _principal(session, "stranger")
    with pytest.raises(PermissionDenied):
        DeployService(session, GrantAuthorization(session)).submit(
            stranger, "dev", repository="analytics", kind=DeployKind.DRY_RUN
        )


@pytest.mark.parametrize(
    ("arguments", "error", "message"),
    [
        (
            {"kind": DeployKind.DEPLOY, "ref": "--upload-pack=x"},
            ValidationError,
            "not a valid git ref",
        ),
        ({"kind": DeployKind.DEPLOY, "to_sha": "a" * 40}, ValidationError, "only for a rollback"),
        ({"kind": DeployKind.ROLLBACK}, ValidationError, "exactly one of"),
        (
            {"kind": DeployKind.ROLLBACK, "to_sha": "a" * 40, "to_previous": True},
            ValidationError,
            "exactly one of",
        ),
        ({"kind": DeployKind.ROLLBACK, "to_sha": "main"}, ValidationError, "full 40-character"),
        ({"kind": DeployKind.ROLLBACK, "to_sha": "a" * 40}, ValidationError, "deploy it instead"),
        ({"kind": DeployKind.ROLLBACK, "to_previous": True}, ValidationError, "no previous"),
        (
            {"kind": DeployKind.ROLLBACK, "to_previous": True, "ref": "main"},
            ValidationError,
            "takes no ref",
        ),
    ],
)
def test_submit_validates_the_request_shape(
    session: Session,
    env: None,
    principal: PrincipalRef,
    arguments: dict,
    error: type[Exception],
    message: str,
) -> None:
    """Refs are validated; rollbacks name exactly one existing target."""
    _repo(session)
    with pytest.raises(error, match=message):
        DeployService(session, AllowAllAuthorization()).submit(
            principal, "dev", repository="analytics", **arguments
        )


def test_submit_unknown_repository_or_environment(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """Unknown names are 404s."""
    service = DeployService(session, AllowAllAuthorization())
    with pytest.raises(NotFoundError, match="repository"):
        service.submit(principal, "dev", repository="nope", kind=DeployKind.DEPLOY)
    _repo(session)
    with pytest.raises(NotFoundError, match="environment"):
        service.submit(principal, "prod", repository="analytics", kind=DeployKind.DEPLOY)


def test_record_deploy_writes_version_pointer_run_and_audit(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """One transaction: Project version, pointer, succeeded run, DEPLOYED audit with counts."""
    repo = _repo(session)
    version = _deployed(
        session, principal, repo, "a" * 40, make_asset("silver.clean", ("bronze.events",))
    )
    pointer = session.execute(select(EnvironmentDeployment)).scalar_one()
    assert pointer.current_job_version_id == version.id
    run = session.execute(select(DeployRun)).scalar_one()
    assert (run.status, run.job_version_id) == (DeployStatus.SUCCEEDED, version.id)
    assert run.diff_json is not None and run.diff_json["added"] == ["silver.clean"]
    event = session.execute(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.DEPLOYED)
    ).scalar_one()
    assert event.detail["repository"] == "analytics"
    assert event.detail["sha"] == "a" * 40
    assert event.detail["project_version_id"] == str(version.id)
    assert event.detail["diff"]["added"] == 1


def test_a_deploy_and_a_rollback_publish_the_assets_descriptions(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The header's text lands on the existing table; a rollback brings the earlier text back."""
    repo = _repo(session)

    def asset(text: str) -> ParsedAsset:
        return make_asset(
            "bronze.events",
            metadata=AssetMetadata(
                materialize="table", description=text, columns={"id": f"id: {text}"}
            ),
        )

    _deployed(session, principal, repo, "a" * 40, asset("first"))
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    events = TableRef("dev", _BRONZE, "events")
    shown = catalog.describe_table(principal, events)
    assert (shown.description, shown.description_source) == ("first", "project")
    assert shown.column_descriptions["id"].text == "id: first"

    catalog.set_table_description(principal, events, "by hand")
    _deployed(session, principal, repo, "b" * 40, asset("second"))
    shown = catalog.describe_table(principal, events)
    assert (shown.description, shown.description_source) == ("by hand", "edited")
    catalog.set_table_description(principal, events, None)
    assert catalog.describe_table(principal, events).description == "second"

    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(
        principal, "dev", repository=repo.name, kind=DeployKind.ROLLBACK, to_previous=True
    )
    run = _claimed(session, session.get(DeployRun, view.id))
    assert run is not None
    service.execute_rollback(principal, run, worker_id="w1")
    assert catalog.describe_table(principal, events).description == "first"


def test_redeploying_a_sha_reuses_the_job_version(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """Same SHA, same environment → the row is reused, still audited."""
    repo = _repo(session)
    asset = make_asset("silver.clean", ("bronze.events",))
    first = _deployed(session, principal, repo, "a" * 40, asset)
    second = _deployed(session, principal, repo, "a" * 40, asset)
    assert first.id == second.id
    assert session.query(JobVersion).count() == 1
    assert (
        session.query(AuditEvent).filter(AuditEvent.event_type == AuditEventType.DEPLOYED).count()
        == 2
    )


def test_compare_and_swap_refuses_a_stale_expectation(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The loser of a race, with a stale expected pointer, gets ConflictError; nothing moves."""
    _repo(session)
    service = DeployService(session, AllowAllAuthorization())
    first = service.submit(principal, "dev", repository="analytics", kind=DeployKind.DEPLOY)
    second = service.submit(principal, "dev", repository="analytics", kind=DeployKind.DEPLOY)
    run_one = _claimed(session, session.get(DeployRun, first.id))
    run_two = _claimed(session, session.get(DeployRun, second.id))
    assert run_one is not None and run_two is not None
    run_one.resolved_sha, run_two.resolved_sha = "a" * 40, "b" * 40
    winner = service.record_deploy(
        principal,
        run_one,
        manifest=_manifest(),
        code_uri="u1",
        code_sha256="0" * 64,
        diff=DeployDiff(),
        expected_job_version_id=None,
        worker_id="w1",
    )
    with pytest.raises(ConflictError, match="moved this environment's deployment first"):
        service.record_deploy(
            principal,
            run_two,
            manifest=_manifest(),
            code_uri="u2",
            code_sha256="0" * 64,
            diff=DeployDiff(),
            expected_job_version_id=None,
            worker_id="w1",
        )
    pointer = session.execute(select(EnvironmentDeployment)).scalar_one()
    assert pointer.current_job_version_id == winner.id
    assert session.query(JobVersion).count() == 1
    assert CONFLICT_MESSAGE.startswith("Another deploy")


def test_dry_run_records_only_the_run(session: Session, env: None, principal: PrincipalRef) -> None:
    """A dry run stores its diff and audit event; no project version, no pointer."""
    _repo(session)
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(
        principal, "dev", repository="analytics", kind=DeployKind.DRY_RUN, ref="main"
    )
    run = _claimed(session, session.get(DeployRun, view.id))
    assert run is not None
    run.resolved_sha = "c" * 40
    service.record_dry_run(principal, run, diff=DeployDiff(added=("silver.x",)), worker_id="w1")
    assert run.status is DeployStatus.SUCCEEDED
    assert session.query(JobVersion).count() == 0
    assert session.query(EnvironmentDeployment).count() == 0
    assert (
        session.query(AuditEvent)
        .filter(AuditEvent.event_type == AuditEventType.DEPLOY_DRY_RUN)
        .count()
        == 1
    )


def test_rollback_to_sha_and_to_previous(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """`to_sha` and `to_previous` move the pointer without new project versions."""
    repo = _repo(session)
    first = _deployed(
        session, principal, repo, "a" * 40, make_asset("silver.one", ("bronze.events",))
    )
    second = _deployed(
        session, principal, repo, "b" * 40, make_asset("silver.two", ("bronze.events",))
    )
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(
        principal, "dev", repository="analytics", kind=DeployKind.ROLLBACK, to_previous=True
    )
    assert view.rollback_to_job_version_id == first.id
    run = _claimed(session, session.get(DeployRun, view.id))
    assert run is not None
    assert service.execute_rollback(principal, run, worker_id="w1").id == first.id
    assert (
        session.execute(select(EnvironmentDeployment)).scalar_one().current_job_version_id
        == first.id
    )
    assert run.diff_json is not None
    assert (run.diff_json["added"], run.diff_json["removed"]) == (["silver.one"], ["silver.two"])
    view = service.submit(
        principal, "dev", repository="analytics", kind=DeployKind.ROLLBACK, to_sha="B" * 40
    )
    assert view.rollback_to_job_version_id == second.id
    assert session.query(JobVersion).count() == 2
    assert (
        session.query(AuditEvent)
        .filter(AuditEvent.event_type == AuditEventType.ROLLED_BACK)
        .count()
        == 1
    )


def test_rollback_refuses_a_version_from_another_repository(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The target must belong to this environment and repository."""
    repo = _repo(session)
    other = _repo(session, "other")
    _deployed(session, principal, repo, "a" * 40)
    foreign = _deployed(session, principal, other, "b" * 40)
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(principal, "dev", repository="analytics", kind=DeployKind.DEPLOY)
    run = _claimed(session, session.get(DeployRun, view.id))
    assert run is not None
    run.kind = DeployKind.ROLLBACK
    run.rollback_to_job_version_id = foreign.id
    with pytest.raises(ValidationError, match="does not belong"):
        service.execute_rollback(principal, run, worker_id="w1")


def test_external_inputs_unreadable_equals_missing(session: Session, env: None) -> None:
    """A missing table and an unreadable one both report "not found"."""
    deployer = _principal(session, "ci")
    GrantService(session, AllowAllAuthorization()).grant(
        deployer,
        principal="ci",
        action=Action.DEPLOY,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    repo = _repo(session)
    assets = [make_asset("silver.a", ("bronze.events",)), make_asset("silver.b", ("bronze.ghost",))]
    issues = DeployService(session, GrantAuthorization(session)).validate_against_environment(
        deployer, "dev", repo.id, assets, build_graph(assets)
    )
    assert [i.message for i in issues] == [
        "table 'bronze.events' not found",
        "table 'bronze.ghost' not found",
    ]
    GrantService(session, AllowAllAuthorization()).grant(
        deployer,
        principal="ci",
        action=Action.READ_TABLE,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    issues = DeployService(session, GrantAuthorization(session)).validate_against_environment(
        deployer, "dev", repo.id, assets, build_graph(assets)
    )
    assert [(i.path, i.message) for i in issues] == [
        ("assets/silver/b.sql", "table 'bronze.ghost' not found")
    ]


def test_cross_repository_inputs_and_ownership(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """B may read A's current outputs; B may not produce a table A produces."""
    repo_a = _repo(session, "a")
    repo_b = _repo(session, "b")
    _deployed(session, principal, repo_a, "a" * 40, make_asset("silver.shared", ("bronze.events",)))
    service = DeployService(session, AllowAllAuthorization())
    reader = [make_asset("silver.mine", ("silver.shared",))]
    assert (
        service.validate_against_environment(
            principal, "dev", repo_b.id, reader, build_graph(reader)
        )
        == []
    )
    thief = [make_asset("silver.shared", ("bronze.events",))]
    issues = service.validate_against_environment(
        principal, "dev", repo_b.id, thief, build_graph(thief)
    )
    assert [i.message for i in issues] == [
        "table 'silver.shared' is produced by repository 'a' in dev"
    ]


def test_existing_unowned(session: Session, env: None, principal: PrincipalRef) -> None:
    """A readable table no current project version produces is 'unowned'; a produced one is not."""
    repo = _repo(session)
    service = DeployService(session, AllowAllAuthorization())
    assert service.existing_unowned(principal, "dev", ["bronze.events", "silver.new"]) == {
        "bronze.events"
    }
    _deployed(session, principal, repo, "a" * 40, make_asset("bronze.events"))
    assert service.existing_unowned(principal, "dev", ["bronze.events"]) == set()


def test_read_side_needs_read_environment(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """History, deployments, project versions and orphans all need READ_ENVIRONMENT."""
    repo = _repo(session)
    version = _deployed(session, principal, repo, "a" * 40)
    stranger = _principal(session, "stranger")
    service = DeployService(session, GrantAuthorization(session))
    run_id = session.execute(select(DeployRun.id)).scalar_one()
    for call in (
        lambda: service.history(stranger, "dev"),
        lambda: service.get(stranger, "dev", run_id),
        lambda: service.deployments(stranger, "dev"),
        lambda: service.job_version(stranger, "dev", version.id),
        lambda: service.orphans(stranger, "dev"),
    ):
        with pytest.raises(PermissionDenied):
            call()


def test_reads_are_scoped_to_their_environment(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A `dev` run or project version id is a 404 under `prod`, which lists none of dev's rows."""
    repo = _repo(session)
    version = _deployed(session, principal, repo, "a" * 40)
    ensure_table(session, "prod", "bronze", "events")
    run_id = session.execute(select(DeployRun.id)).scalar_one()
    service = DeployService(session, AllowAllAuthorization())
    assert service.get(principal, "dev", run_id).id == run_id
    assert service.job_version(principal, "dev", version.id).id == version.id
    with pytest.raises(NotFoundError):
        service.get(principal, "dev", uuid.uuid4())
    with pytest.raises(NotFoundError):
        service.get(principal, "prod", run_id)
    with pytest.raises(NotFoundError):
        service.job_version(principal, "prod", version.id)
    assert [r.id for r in service.history(principal, "dev")] == [run_id]
    assert service.history(principal, "prod") == []
    assert service.deployments(principal, "prod") == []
    assert service.orphans(principal, "prod") == OrphanReport(orphans=[], hidden=0)
    with pytest.raises(NotFoundError, match="environment"):
        service.history(principal, "nope")


def test_deployments_list_current_versions(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """One row per repository with its current SHA."""
    repo = _repo(session)
    version = _deployed(session, principal, repo, "a" * 40)
    [row] = DeployService(session, AllowAllAuthorization()).deployments(principal, "dev")
    assert (row.repository, row.job_version_id, row.commit_sha) == (
        "analytics",
        version.id,
        "a" * 40,
    )


def test_manifest_sql_is_hidden_from_a_reader_without_access(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """An asset's SQL, references and lineage are withheld unless every reference is readable."""
    repo = _repo(session)
    version = _deployed(
        session,
        principal,
        repo,
        "a" * 40,
        make_asset(
            "silver.clean",
            ("bronze.events",),
            metadata=AssetMetadata(materialize="table", owner="o"),
        ),
    )
    version.manifest_json = {
        **version.manifest_json,
        "assets": [
            {
                **version.manifest_json["assets"][0],
                "inputs": {"x": "bronze.events"},
                "input_kinds": {"x": "artifact"},
            }
        ],
    }
    session.commit()
    reader = _principal(session, "reader")
    GrantService(session, AllowAllAuthorization()).grant(
        principal,
        principal="reader",
        action=Action.READ_ENVIRONMENT,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    view = DeployService(session, GrantAuthorization(session)).job_version(
        reader, "dev", version.id
    )
    [asset] = view.manifest["assets"]
    assert "sql" not in asset and "references" not in asset and "lineage" not in asset
    assert "inputs" not in asset and "input_kinds" not in asset
    assert asset["hidden_references"] == 1
    assert asset["metadata"]["owner"] == "o"
    assert view.manifest["external_inputs"] == []
    assert view.manifest["hidden_external_inputs"] == 1
    full = DeployService(session, AllowAllAuthorization()).job_version(principal, "dev", version.id)
    assert full.manifest["assets"][0]["sql"].startswith("SELECT")
    assert full.manifest["assets"][0]["input_kinds"] == {"x": "artifact"}


def test_orphans_after_removing_an_asset(
    session: Session, env: None, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A table an earlier project version produced and the current one does not, still exists."""
    repo = _repo(session)
    _prepared_catalog(
        session, tmp_path, AllowAllAuthorization(), tables=[TableRef("dev", _SILVER, "old")]
    )
    _deployed(
        session,
        principal,
        repo,
        "a" * 40,
        make_asset("silver.old", ("bronze.events",)),
        make_asset("silver.kept"),
    )
    _deployed(session, principal, repo, "b" * 40, make_asset("silver.kept"))
    report = DeployService(session, AllowAllAuthorization()).orphans(principal, "dev")
    assert [(o.repository, o.table) for o in report.orphans] == [("analytics", "silver.old")]
    assert report.hidden == 0


def test_a_table_moved_to_another_repository_is_not_an_orphan(
    session: Session, env: None, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A table repo A dropped but repo B's current deployment now produces is live, not orphaned."""
    repo_a = _repo(session, "repo_a")
    repo_b = _repo(session, "repo_b")
    _prepared_catalog(
        session,
        tmp_path,
        AllowAllAuthorization(),
        tables=[TableRef("dev", _SILVER, "moved"), TableRef("dev", _SILVER, "dropped")],
    )
    _deployed(
        session,
        principal,
        repo_a,
        "a" * 40,
        make_asset("silver.moved"),
        make_asset("silver.dropped"),
        make_asset("silver.kept"),
    )
    _deployed(session, principal, repo_a, "b" * 40, make_asset("silver.kept"))
    _deployed(session, principal, repo_b, "c" * 40, make_asset("silver.moved"))
    report = DeployService(session, AllowAllAuthorization()).orphans(principal, "dev")
    assert [(o.repository, o.table) for o in report.orphans] == [("repo_a", "silver.dropped")]
    assert report.hidden == 0


def test_fail_marks_the_run(session: Session, env: None, principal: PrincipalRef) -> None:
    """`fail` stores the message and a finish time."""
    _repo(session)
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(principal, "dev", repository="analytics", kind=DeployKind.DEPLOY)
    run = session.get(DeployRun, view.id)
    assert run is not None
    service.fail(run, "boom", worker_id=None)
    assert (run.status, run.error_message) == (DeployStatus.FAILED, "boom")
    assert run.finished_at is not None


def _file_asset(table: str, sql: str, reference: str) -> ParsedAsset:
    """Build an asset whose SQL reads a file, as the parser reports it.

    Parameters
    ----------
    table
        Dotted table name it produces.
    sql
        Its SQL.
    reference
        The reference the parser yields for that SQL.

    Returns
    -------
    ParsedAsset
        The asset.
    """
    return dataclasses.replace(make_asset(table, (reference,)), sql=sql, normalized_sql=sql)


@pytest.mark.parametrize(
    ("sql", "reference"),
    [
        ("SELECT * FROM 'bronze.csv'", "bronze.csv"),
        ('SELECT * FROM "bronze.csv"', "bronze.csv"),
        ("SELECT * FROM read_csv('bronze.csv')", "bronze.csv"),
    ],
)
def test_a_file_reference_is_never_a_catalog_table(
    session: Session, env: None, tmp_path: Path, principal: PrincipalRef, sql: str, reference: str
) -> None:
    """`FROM 'bronze.csv'` reads a file; it must not pass as the readable table `bronze.csv`."""
    _prepared_catalog(
        session,
        tmp_path,
        AllowAllAuthorization(),
        tables=[TableRef("dev", _BRONZE, "csv")],
    )
    repo = _repo(session)
    service = DeployService(session, AllowAllAuthorization())
    asset = _file_asset("silver.f", sql, reference)
    issues = service.validate_against_environment(
        principal, "dev", repo.id, [asset], build_graph([asset])
    )
    assert [i.message for i in issues] == ["table 'bronze.csv' not found"]
    # Not even another repository's output of that name makes it a table.
    other = _repo(session, "other")
    _deployed(session, principal, other, "a" * 40, make_asset("bronze.csv"))
    issues = service.validate_against_environment(
        principal, "dev", repo.id, [asset], build_graph([asset])
    )
    assert [i.message for i in issues] == ["table 'bronze.csv' not found"]


def test_an_unquoted_dotted_table_still_resolves(
    session: Session, env: None, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Control: the same text without quotes is the catalog table `bronze.csv`."""
    _prepared_catalog(
        session,
        tmp_path,
        AllowAllAuthorization(),
        tables=[TableRef("dev", _BRONZE, "csv")],
    )
    repo = _repo(session)
    asset = make_asset("silver.f", ("bronze.csv",))
    service = DeployService(session, AllowAllAuthorization())
    assert (
        service.validate_against_environment(
            principal, "dev", repo.id, [asset], build_graph([asset])
        )
        == []
    )


@pytest.mark.parametrize("name", ["nodots", "a/b.csv", "s3://b/x.parquet", ".", "a..b"])
def test_malformed_external_inputs_are_not_found(
    session: Session, env: None, principal: PrincipalRef, name: str
) -> None:
    """A name that cannot be a table is reported like a missing one, never raised."""
    repo = _repo(session)
    asset = make_asset("silver.m", (name,))
    issues = DeployService(session, AllowAllAuthorization()).validate_against_environment(
        principal, "dev", repo.id, [asset], build_graph([asset])
    )
    assert [i.message for i in issues] == [f"table {name!r} not found"]


def test_a_stale_pointer_expectation_is_refused_after_the_first_deploy(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """Once a pointer exists, an expectation other than its value loses; nothing moves."""
    repo = _repo(session)
    first = _deployed(session, principal, repo, "a" * 40)
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(principal, "dev", repository="analytics", kind=DeployKind.DEPLOY)
    run = _claimed(session, session.get(DeployRun, view.id))
    assert run is not None
    run.resolved_sha = "b" * 40
    with pytest.raises(ConflictError, match="moved this environment's deployment first"):
        service.record_deploy(
            principal,
            run,
            manifest=_manifest(),
            code_uri="u",
            code_sha256="0" * 64,
            diff=DeployDiff(),
            expected_job_version_id=uuid.uuid4(),
            worker_id="w1",
        )
    assert (
        session.execute(select(EnvironmentDeployment)).scalar_one().current_job_version_id
        == first.id
    )
    assert session.query(JobVersion).count() == 1


def test_a_losing_first_deploy_rolls_back_its_job_version_in_the_same_transaction(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The loser's project version is not committed behind the winner's back."""
    _repo(session)
    service = DeployService(session, AllowAllAuthorization())
    runs = []
    for sha in ("a" * 40, "b" * 40):
        view = service.submit(principal, "dev", repository="analytics", kind=DeployKind.DEPLOY)
        run = _claimed(session, session.get(DeployRun, view.id))
        assert run is not None
        run.resolved_sha = sha
        runs.append(run)
    service.record_deploy(
        principal,
        runs[0],
        manifest=_manifest(),
        code_uri="u1",
        code_sha256="0" * 64,
        diff=DeployDiff(),
        expected_job_version_id=None,
        worker_id="w1",
    )
    with pytest.raises(ConflictError):
        service.record_deploy(
            principal,
            runs[1],
            manifest=_manifest(),
            code_uri="u2",
            code_sha256="0" * 64,
            diff=DeployDiff(),
            expected_job_version_id=None,
            worker_id="w1",
        )
    session.expire_all()
    assert [v.commit_sha for v in session.query(JobVersion)] == ["a" * 40]
    assert (
        session.query(AuditEvent).filter(AuditEvent.event_type == AuditEventType.DEPLOYED).count()
        == 1
    )
    reloaded = session.get(DeployRun, runs[1].id)
    assert reloaded is not None
    assert reloaded.status is DeployStatus.RUNNING


def test_a_racing_insert_of_the_same_sha_reuses_the_winning_job_version(
    session: Session, env: None, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If another worker inserted the SHA first, the unique violation reuses its row."""
    repo = _repo(session)
    winner = _deployed(session, principal, repo, "a" * 40)
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(principal, "dev", repository="analytics", kind=DeployKind.DEPLOY)
    run = _claimed(session, session.get(DeployRun, view.id))
    assert run is not None
    run.resolved_sha = "a" * 40
    real = service._existing_job_version
    calls: list[str] = []

    def blind_first(
        environment_id: uuid.UUID, repository_id: uuid.UUID, sha: str
    ) -> JobVersion | None:
        calls.append(sha)
        return None if len(calls) == 1 else real(environment_id, repository_id, sha)

    monkeypatch.setattr(service, "_existing_job_version", blind_first)
    version = service.record_deploy(
        principal,
        run,
        manifest=_manifest(),
        code_uri="u",
        code_sha256="0" * 64,
        diff=DeployDiff(),
        expected_job_version_id=winner.id,
        worker_id="w1",
    )
    assert version.id == winner.id
    assert run.resolved_sha == "a" * 40
    assert run.status is DeployStatus.SUCCEEDED
    assert session.query(JobVersion).count() == 1


def test_rollback_target_gone_or_unset(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A rollback whose target is unset (deleted versions are nulled) is refused."""
    repo = _repo(session)
    _deployed(session, principal, repo, "a" * 40)
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(principal, "dev", repository="analytics", kind=DeployKind.DEPLOY)
    run = _claimed(session, session.get(DeployRun, view.id))
    assert run is not None
    run.kind = DeployKind.ROLLBACK
    with pytest.raises(ValidationError, match="no longer exists"):
        service.execute_rollback(principal, run, worker_id="w1")


def test_rollback_loses_the_race_when_the_pointer_moved(
    session: Session, env: None, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deploy that moves the pointer between the rollback's read and swap wins."""
    repo = _repo(session)
    first = _deployed(session, principal, repo, "a" * 40, make_asset("silver.one"))
    second = _deployed(session, principal, repo, "b" * 40, make_asset("silver.two"))
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(
        principal, "dev", repository="analytics", kind=DeployKind.ROLLBACK, to_previous=True
    )
    run = _claimed(session, session.get(DeployRun, view.id))
    assert run is not None
    real = service.current_job_version

    def stale(environment_id: uuid.UUID, repository_id: uuid.UUID) -> JobVersion | None:
        observed = real(environment_id, repository_id)
        third = _deployed(session, principal, repo, "c" * 40, make_asset("silver.three"))
        assert observed is not None
        assert third.id != observed.id
        return observed

    monkeypatch.setattr(service, "current_job_version", stale)
    with pytest.raises(ConflictError, match="moved this environment's deployment first"):
        service.execute_rollback(principal, run, worker_id="w1")
    monkeypatch.undo()
    pointer = session.execute(select(EnvironmentDeployment)).scalar_one()
    assert pointer.current_job_version_id not in (first.id, second.id)
    assert (
        session.query(AuditEvent)
        .filter(AuditEvent.event_type == AuditEventType.ROLLED_BACK)
        .count()
        == 0
    )


def test_orphans_hide_unreadable_ones_and_skip_missing_tables(
    session: Session, env: None, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Unreadable orphans are counted, not named; vanished tables are not orphans."""
    repo = _repo(session)
    _prepared_catalog(
        session,
        tmp_path,
        AllowAllAuthorization(),
        tables=[TableRef("dev", _SILVER, "old"), TableRef("dev", _SILVER, "seen")],
    )
    _deployed(
        session,
        principal,
        repo,
        "a" * 40,
        make_asset("silver.old"),
        make_asset("silver.seen"),
        make_asset("silver.gone"),
        make_asset("silver.kept"),
    )
    _deployed(session, principal, repo, "b" * 40, make_asset("silver.kept"))
    reader = _principal(session, "reader")
    grants = GrantService(session, AllowAllAuthorization())
    grants.grant(
        principal,
        principal="reader",
        action=Action.READ_ENVIRONMENT,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    grants.grant(
        principal,
        principal="reader",
        action=Action.READ_TABLE,
        kind=SecurableKind.TABLE,
        environment="dev",
        namespace="silver",
        table="seen",
    )
    report = DeployService(session, GrantAuthorization(session)).orphans(reader, "dev")
    assert [(o.repository, o.table) for o in report.orphans] == [("analytics", "silver.seen")]
    assert report.hidden == 1


def test_orphans_ignore_a_malformed_table_name_in_an_old_manifest(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A table name that cannot be parsed is skipped rather than raised."""
    repo = _repo(session)
    old = _deployed(session, principal, repo, "a" * 40, make_asset("silver.old"))
    old.manifest_json = {**old.manifest_json, "assets": [{"table": "nodots"}]}
    _deployed(session, principal, repo, "b" * 40, make_asset("silver.kept"))
    report = DeployService(session, AllowAllAuthorization()).orphans(principal, "dev")
    assert report == OrphanReport(orphans=[], hidden=0)


def test_manifest_hides_only_what_the_reader_cannot_read(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """Readable references keep their SQL; a malformed reference counts as hidden."""
    repo = _repo(session)
    version = _deployed(
        session, principal, repo, "a" * 40, make_asset("silver.clean", ("bronze.events",))
    )
    version.manifest_json = {
        **version.manifest_json,
        "assets": [
            {**version.manifest_json["assets"][0], "references": ["bronze.events", "a/b.csv"]}
        ],
        "external_inputs": ["bronze.events", "a/b.csv"],
    }
    session.commit()
    view = DeployService(session, AllowAllAuthorization()).job_version(principal, "dev", version.id)
    [asset] = view.manifest["assets"]
    assert "sql" not in asset
    assert asset["hidden_references"] == 1
    assert view.manifest["external_inputs"] == ["bronze.events"]
    assert view.manifest["hidden_external_inputs"] == 1


def test_manifest_with_readable_references_keeps_its_sql(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """When every reference is readable nothing is withheld."""
    repo = _repo(session)
    version = _deployed(
        session, principal, repo, "a" * 40, make_asset("silver.clean", ("bronze.events",))
    )
    view = DeployService(session, AllowAllAuthorization()).job_version(principal, "dev", version.id)
    [asset] = view.manifest["assets"]
    assert asset["hidden_references"] == 0
    assert asset["sql"].startswith("SELECT")
    assert view.manifest["hidden_external_inputs"] == 0


def test_job_version_does_not_mutate_the_stored_manifest(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """Hiding works on a copy; the stored manifest keeps its SQL."""
    repo = _repo(session)
    version = _deployed(
        session, principal, repo, "a" * 40, make_asset("silver.clean", ("bronze.events",))
    )
    reader = _principal(session, "reader")
    GrantService(session, AllowAllAuthorization()).grant(
        principal,
        principal="reader",
        action=Action.READ_ENVIRONMENT,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    DeployService(session, GrantAuthorization(session)).job_version(reader, "dev", version.id)
    assert "sql" in version.manifest_json["assets"][0]


def test_unknown_job_version_is_not_found(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A random id is a 404."""
    with pytest.raises(NotFoundError):
        DeployService(session, AllowAllAuthorization()).job_version(principal, "dev", uuid.uuid4())


def test_authorize_deploy(session: Session, env: None, principal: PrincipalRef) -> None:
    """The worker re-checks DEPLOY at claim time."""
    stranger = _principal(session, "stranger")
    DeployService(session, AllowAllAuthorization()).authorize_deploy(principal, "dev")
    with pytest.raises(PermissionDenied):
        DeployService(session, GrantAuthorization(session)).authorize_deploy(stranger, "dev")


def _vanish(session: Session, repo: Repository, *, commit: bool = True) -> None:
    """Delete a repository the way a concurrent `RepositoryService.delete` would.

    Parameters
    ----------
    session
        Database session.
    repo
        The repository to delete; its runs, versions and pointers cascade.
    commit
        Whether to commit (which expires every loaded row) or only flush.
    """
    session.execute(delete(Repository).where(Repository.id == repo.id))
    if commit:
        session.commit()
    else:
        session.flush()


def _queued(
    session: Session, principal: PrincipalRef, kind: DeployKind = DeployKind.DEPLOY
) -> DeployRun:
    """Queue a run and return its row with a resolved SHA.

    Parameters
    ----------
    session
        Database session.
    principal
        The submitter.
    kind
        The run's kind.

    Returns
    -------
    DeployRun
        The row.
    """
    view = DeployService(session, AllowAllAuthorization()).submit(
        principal, "dev", repository="analytics", kind=DeployKind.DEPLOY, ref="main"
    )
    run = _claimed(session, session.get(DeployRun, view.id))
    assert run is not None
    run.kind = kind
    run.resolved_sha = "a" * 40
    return run


def test_recording_after_the_repository_vanished_fails_cleanly(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A repository deleted mid-deploy yields NotFoundError, never a crash."""
    repo = _repo(session)
    service = DeployService(session, AllowAllAuthorization())
    run = _queued(session, principal)
    dry = _queued(session, principal, DeployKind.DRY_RUN)
    _vanish(session, repo)
    with pytest.raises(NotFoundError, match="repository"):
        service.record_deploy(
            principal,
            run,
            manifest=_manifest(),
            code_uri="u",
            code_sha256="0" * 64,
            diff=DeployDiff(),
            expected_job_version_id=None,
            worker_id="w1",
        )
    with pytest.raises(NotFoundError, match="repository"):
        service.record_dry_run(principal, dry, diff=DeployDiff(), worker_id="w1")
    with pytest.raises(NotFoundError, match="repository"):
        service.execute_rollback(principal, run, worker_id="w1")
    with pytest.raises(NotFoundError, match="repository"):
        service.fail(run, "boom", worker_id="w1")
    assert session.query(JobVersion).count() == 0
    assert session.query(EnvironmentDeployment).count() == 0


def _transient_run(session: Session, *, repository_id: uuid.UUID | None = None) -> DeployRun:
    """Build an unsaved run row in `dev`, optionally pointing at a repository that does not exist.

    Parameters
    ----------
    session
        Database session.
    repository_id
        The repository id; a random one if omitted.

    Returns
    -------
    DeployRun
        A detached row, as a worker would hold after the database lost it.
    """
    environment_id = session.execute(select(Environment.id)).scalar_one()
    return DeployRun(
        id=uuid.uuid4(),
        environment_id=environment_id,
        repository_id=repository_id or uuid.uuid4(),
        kind=DeployKind.DEPLOY,
        status=DeployStatus.RUNNING,
        resolved_sha="a" * 40,
    )


def test_reading_a_run_whose_repository_vanished_is_not_found(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """`get`, `history` and the view of a held run never crash on a missing repository."""
    repo = _repo(session)
    service = DeployService(session, AllowAllAuthorization())
    run_id = _queued(session, principal).id
    session.commit()
    _vanish(session, repo)
    with pytest.raises(NotFoundError):
        service.get(principal, "dev", run_id)
    assert service.history(principal, "dev") == []
    with pytest.raises(NotFoundError, match="repository"):
        service._view(_transient_run(session))


def test_the_environment_vanishing_is_not_found(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A run whose environment is gone is a 404 view, not an AttributeError."""
    run = _transient_run(session)
    run.environment_id = uuid.uuid4()
    with pytest.raises(NotFoundError, match="environment"):
        DeployService(session, AllowAllAuthorization())._view(run)


def test_a_run_pointing_at_a_missing_repository_is_refused_before_any_write(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The liveness check also catches a row whose repository id no longer resolves."""
    run = _transient_run(session)
    with pytest.raises(NotFoundError, match="repository"):
        DeployService(session, AllowAllAuthorization()).record_dry_run(
            principal, run, diff=DeployDiff(), worker_id="w1"
        )
    assert (
        session.query(AuditEvent)
        .filter(AuditEvent.event_type == AuditEventType.DEPLOY_DRY_RUN)
        .count()
        == 0
    )


@pytest.mark.parametrize("operation", ["record_deploy", "record_dry_run", "fail"])
def test_a_repository_deleted_after_the_check_is_still_a_clean_error(
    session: Session,
    env: None,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """A delete landing between the liveness check and the write is translated."""
    repo = _repo(session)
    service = DeployService(session, AllowAllAuthorization())
    _deployed(session, principal, repo, "b" * 40)
    run = _queued(
        session,
        principal,
        DeployKind.DRY_RUN if operation == "record_dry_run" else DeployKind.DEPLOY,
    )
    _vanish(session, repo, commit=False)
    monkeypatch.setattr(service, "_require_alive", lambda run: ("dev", "analytics"))
    monkeypatch.setattr(service, "_hold", lambda *args, **kwargs: None)
    with pytest.raises((ConflictError, NotFoundError)):
        if operation == "record_deploy":
            service.record_deploy(
                principal,
                run,
                manifest=_manifest(),
                code_uri="u",
                code_sha256="0" * 64,
                diff=DeployDiff(),
                expected_job_version_id=None,
                worker_id="w1",
            )
        elif operation == "record_dry_run":
            service.record_dry_run(principal, run, diff=DeployDiff(), worker_id="w1")
        else:
            service.fail(run, "boom", worker_id="w1")
    assert [v.commit_sha for v in session.query(JobVersion)] == ["b" * 40]


def test_to_previous_rollback_conflicts_if_a_deploy_landed_after_submit(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A deploy landing after a to_previous submit must not be silently overwritten."""
    repo = _repo(session)
    _deployed(session, principal, repo, "a" * 40, make_asset("silver.one"))
    _deployed(session, principal, repo, "b" * 40, make_asset("silver.two"))
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(
        principal, "dev", repository="analytics", kind=DeployKind.ROLLBACK, to_previous=True
    )
    third = _deployed(session, principal, repo, "c" * 40, make_asset("silver.three"))
    run = _claimed(session, session.get(DeployRun, view.id))
    assert run is not None
    with pytest.raises(ConflictError, match="moved this environment's deployment first"):
        service.execute_rollback(principal, run, worker_id="w1")
    pointer = session.execute(select(EnvironmentDeployment)).scalar_one()
    assert pointer.current_job_version_id == third.id


def test_to_sha_rollback_overrides_later_deploys(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The user named the target, so a to_sha rollback proceeds whatever deployed since."""
    repo = _repo(session)
    first = _deployed(session, principal, repo, "a" * 40, make_asset("silver.one"))
    _deployed(session, principal, repo, "b" * 40, make_asset("silver.two"))
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(
        principal, "dev", repository="analytics", kind=DeployKind.ROLLBACK, to_sha="a" * 40
    )
    _deployed(session, principal, repo, "c" * 40, make_asset("silver.three"))
    run = _claimed(session, session.get(DeployRun, view.id))
    assert run is not None
    assert service.execute_rollback(principal, run, worker_id="w1").id == first.id


def test_rollback_cannot_take_a_table_another_repository_now_produces(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A drops T, B deploys T, A rolls back to the version producing T: refused, nothing moves."""
    repo_a = _repo(session, "a")
    repo_b = _repo(session, "b")
    old = _deployed(session, principal, repo_a, "a" * 40, make_asset("silver.t"))
    _deployed(session, principal, repo_a, "b" * 40, make_asset("silver.other"))
    _deployed(session, principal, repo_b, "c" * 40, make_asset("silver.t"))
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(
        principal, "dev", repository="a", kind=DeployKind.ROLLBACK, to_sha="a" * 40
    )
    run = _claimed(session, session.get(DeployRun, view.id))
    assert run is not None
    with pytest.raises(
        ValidationError, match=r"table .silver\.t. is produced by repository .b. in dev"
    ):
        service.execute_rollback(principal, run, worker_id="w1")
    pointer = session.get(EnvironmentDeployment, (run.environment_id, repo_a.id))
    assert pointer is not None
    assert pointer.current_job_version_id != old.id
    assert (
        session.query(AuditEvent)
        .filter(AuditEvent.event_type == AuditEventType.ROLLED_BACK)
        .count()
        == 0
    )


def test_fail_refuses_a_run_that_already_finished(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A late `fail` must not overwrite a succeeded run."""
    repo = _repo(session)
    _deployed(session, principal, repo, "a" * 40)
    run = session.execute(select(DeployRun)).scalar_one()
    with pytest.raises(ConflictError, match="no longer this worker"):
        DeployService(session, AllowAllAuthorization()).fail(run, "boom", worker_id="w1")
    session.refresh(run)
    assert (run.status, run.error_message) == (DeployStatus.SUCCEEDED, None)


@pytest.mark.parametrize("operation", ["record_deploy", "record_dry_run", "execute_rollback"])
def test_recording_refuses_a_run_of_another_kind(
    session: Session, env: None, principal: PrincipalRef, operation: str
) -> None:
    """Each recording method accepts only its own kind of run."""
    _repo(session)
    service = DeployService(session, AllowAllAuthorization())
    wrong = {
        "record_deploy": DeployKind.DRY_RUN,
        "record_dry_run": DeployKind.DEPLOY,
        "execute_rollback": DeployKind.DEPLOY,
    }[operation]
    run = _queued(session, principal, wrong)
    with pytest.raises(ValidationError, match="not a"):
        if operation == "record_deploy":
            service.record_deploy(
                principal,
                run,
                manifest=_manifest(),
                code_uri="u",
                code_sha256="0" * 64,
                diff=DeployDiff(),
                expected_job_version_id=None,
                worker_id="w1",
            )
        elif operation == "record_dry_run":
            service.record_dry_run(principal, run, diff=DeployDiff(), worker_id="w1")
        else:
            service.execute_rollback(principal, run, worker_id="w1")
    assert session.query(JobVersion).count() == 0
    assert session.query(EnvironmentDeployment).count() == 0


def test_a_deploy_only_principal_can_poll_its_run_but_not_read_history(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The CI recipe (raw DEPLOY grant): `get` works, every other read stays READ_ENVIRONMENT."""
    repo = _repo(session)
    version = _deployed(session, principal, repo, "a" * 40)
    ci = _principal(session, "ci")
    GrantService(session, AllowAllAuthorization()).grant(
        ci, principal="ci", action=Action.DEPLOY, kind=SecurableKind.ENVIRONMENT, environment="dev"
    )
    service = DeployService(session, GrantAuthorization(session))
    run_id = session.execute(select(DeployRun.id)).scalar_one()
    assert service.get(ci, "dev", run_id).id == run_id
    for call in (
        lambda: service.history(ci, "dev"),
        lambda: service.deployments(ci, "dev"),
        lambda: service.job_version(ci, "dev", version.id),
        lambda: service.orphans(ci, "dev"),
    ):
        with pytest.raises(PermissionDenied):
            call()


def _read_environment_only(session: Session, admin: PrincipalRef, *tables: str) -> PrincipalRef:
    """Create a reader with READ_ENVIRONMENT on `dev` and READ_TABLE on some tables.

    Parameters
    ----------
    session
        Database session.
    admin
        Grants the access.
    *tables
        Dotted table names the reader may read.

    Returns
    -------
    PrincipalRef
        The reader.
    """
    reader = _principal(session, "reader")
    grants = GrantService(session, AllowAllAuthorization())
    grants.grant(
        admin,
        principal="reader",
        action=Action.READ_ENVIRONMENT,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    for dotted in tables:
        namespace, table = dotted.split(".")
        grants.grant(
            admin,
            principal="reader",
            action=Action.READ_TABLE,
            kind=SecurableKind.TABLE,
            environment="dev",
            namespace=namespace,
            table=table,
        )
    return reader


def test_adopted_tables_a_reader_cannot_read_are_counted_not_named(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """`get` and `history` drop unreadable `adopted` names and report how many."""
    repo = _repo(session)
    _deployed(session, principal, repo, "a" * 40, make_asset("silver.clean"))
    run = session.execute(select(DeployRun)).scalar_one()
    run.diff_json = {"added": ["silver.clean"], "adopted": ["bronze.events", "bronze.secret"]}
    session.commit()
    ensure_table(session, "dev", "bronze", "secret")
    reader = _read_environment_only(session, principal, "bronze.events")
    service = DeployService(session, GrantAuthorization(session))
    for view in (service.get(reader, "dev", run.id), service.history(reader, "dev")[0]):
        assert view.diff is not None
        assert view.diff["adopted"] == ["bronze.events"]
        assert view.diff["added"] == ["silver.clean"]
        assert view.hidden_adopted == 1
    full = DeployService(session, AllowAllAuthorization()).get(principal, "dev", run.id)
    assert full.diff is not None
    assert full.diff["adopted"] == ["bronze.events", "bronze.secret"]
    assert full.hidden_adopted == 0


def _kept_removed(session: Session, principal: PrincipalRef, *current: str) -> DeployRun:
    """Record a deploy of `current` whose diff says it removed three tables, two of them kept.

    Parameters
    ----------
    session
        Database session; commits.
    principal
        The deployer.
    *current
        The dotted names of the assets of the repository's current deployment.

    Returns
    -------
    DeployRun
        The run; ``silver.clean`` and ``silver.secret`` carry the note of a kept table.
    """
    repo = _repo(session)
    _deployed(session, principal, repo, "a" * 40, *(make_asset(name) for name in current))
    run = session.execute(select(DeployRun)).scalar_one()
    run.diff_json = {"removed": ["silver.clean", "silver.other", "silver.secret"]}
    session.commit()
    for name in ("clean", "secret", "other"):
        _, table_id = ensure_table(session, "dev", "silver", name)
        if name != "other":
            session.execute(
                update(Table).where(Table.id == table_id).values(kept_run_id=uuid.uuid4())
            )
    session.commit()
    return run


def test_a_deploy_that_removes_the_asset_of_a_kept_table_warns(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The warning names the kept table, both next steps, and only what the reader may read."""
    run = _kept_removed(session, principal, "gold.report")
    reader = _read_environment_only(session, principal, "silver.clean", "silver.other")

    full = DeployService(session, AllowAllAuthorization()).get(principal, "dev", run.id)
    limited = DeployService(session, GrantAuthorization(session)).get(reader, "dev", run.id)

    assert [w.split("`")[1] for w in full.warnings] == ["silver.clean", "silver.secret"]
    assert "add it back and build it, or drop the table" in full.warnings[0]
    assert "kept after its checks failed or could not run" in full.warnings[0]
    assert "or set back to data whose checks are not known" in full.warnings[0]
    assert [w.split("`")[1] for w in limited.warnings] == ["silver.clean"]


def test_the_warning_stops_once_the_asset_is_back_in_the_current_deployment(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """An older deploy must not keep saying "add it back" for an asset that is back."""
    run = _kept_removed(session, principal, "gold.report", "silver.clean")

    got = DeployService(session, AllowAllAuthorization()).get(principal, "dev", run.id)
    listed = DeployService(session, AllowAllAuthorization()).history(principal, "dev")[0]

    assert [w.split("`")[1] for w in got.warnings] == ["silver.secret"]
    assert listed.warnings == got.warnings


def test_a_listing_reads_the_kept_tables_and_the_current_deployment_once(
    session: Session, env: None, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two runs of one repository that both remove a kept table share the two reads."""
    repo = _repo(session)
    _deployed(session, principal, repo, "a" * 40, make_asset("gold.report"))
    _deployed(session, principal, repo, "b" * 40, make_asset("gold.report"))
    runs = session.execute(select(DeployRun)).scalars().all()
    assert len(runs) == 2
    for run in runs:
        run.diff_json = {"removed": ["silver.clean"]}
    _, table_id = ensure_table(session, "dev", "silver", "clean")
    session.execute(update(Table).where(Table.id == table_id).values(kept_run_id=uuid.uuid4()))
    session.commit()
    kept_reads: list[object] = []
    current_reads: list[object] = []
    real_kept = deploys_module.kept_tables
    real_current = DeployService.current_job_version

    def counting_kept(session: Session, environment_id: uuid.UUID) -> dict[str, uuid.UUID]:
        kept_reads.append(environment_id)
        return real_kept(session, environment_id)

    def counting_current(
        self: DeployService, environment_id: uuid.UUID, repository_id: uuid.UUID
    ) -> JobVersion | None:
        current_reads.append(repository_id)
        return real_current(self, environment_id, repository_id)

    monkeypatch.setattr(deploys_module, "kept_tables", counting_kept)
    monkeypatch.setattr(DeployService, "current_job_version", counting_current)

    listed = DeployService(session, AllowAllAuthorization()).history(principal, "dev")

    assert [len(view.warnings) for view in listed] == [1, 1]
    assert (len(kept_reads), len(current_reads)) == (1, 1)


def test_a_dry_run_that_removes_the_asset_of_a_kept_table_warns_though_it_is_still_deployed(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A dry run moves no pointer: the current deployment still builds the table it removes."""
    repo = _repo(session)
    _deployed(session, principal, repo, "a" * 40, make_asset("silver.clean"))
    _, table_id = ensure_table(session, "dev", "silver", "clean")
    session.execute(update(Table).where(Table.id == table_id).values(kept_run_id=uuid.uuid4()))
    session.commit()
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(
        principal, "dev", repository="analytics", kind=DeployKind.DRY_RUN, ref="main"
    )
    run = _claimed(session, session.get(DeployRun, view.id))
    assert run is not None
    run.resolved_sha = "b" * 40
    service.record_dry_run(
        principal, run, diff=DeployDiff(removed=("silver.clean",)), worker_id="w1"
    )

    got = service.get(principal, "dev", run.id)

    assert [w.split("`")[1] for w in got.warnings] == ["silver.clean"]


def test_a_deploy_without_kept_removed_tables_has_no_warning(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    repo = _repo(session)
    _deployed(session, principal, repo, "a" * 40, make_asset("gold.report"))
    run = session.execute(select(DeployRun)).scalar_one()
    run.diff_json = {"removed": ["silver.clean"]}
    session.commit()
    ensure_table(session, "dev", "silver", "clean")

    got = DeployService(session, AllowAllAuthorization()).get(principal, "dev", run.id)
    assert got.warnings == []


def test_a_run_without_a_diff_has_nothing_hidden(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A queued run has no diff; filtering leaves it `None`."""
    _repo(session)
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(principal, "dev", repository="analytics", kind=DeployKind.DEPLOY)
    got = service.get(principal, "dev", view.id)
    assert got.diff is None
    assert got.hidden_adopted == 0


def test_manifest_edges_into_a_hidden_asset_are_dropped(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """An edge into an asset whose SQL is withheld would reveal what that SQL reads."""
    repo = _repo(session)
    version = _deployed(
        session,
        principal,
        repo,
        "a" * 40,
        make_asset("silver.a"),
        make_asset("gold.b", ("silver.a",)),
        make_asset("gold.c", ("silver.a", "bronze.events")),
    )
    ensure_table(session, "dev", "silver", "a")
    reader = _read_environment_only(session, principal, "silver.a")
    view = DeployService(session, GrantAuthorization(session)).job_version(
        reader, "dev", version.id
    )
    assert view.manifest["edges"] == [["silver.a", "gold.b"]]
    assert view.manifest["hidden_edges"] == 1
    full = DeployService(session, AllowAllAuthorization()).job_version(principal, "dev", version.id)
    assert full.manifest["edges"] == [["silver.a", "gold.b"], ["silver.a", "gold.c"]]
    assert full.manifest["hidden_edges"] == 0


def test_record_deploy_rechecks_ownership_at_commit_time(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A table another repository came to produce after validation is refused at record time.

    Validation reads ownership at fetch time; a second repository may have recorded the same
    new table since, so the check is repeated inside the recording transaction.
    """
    first, second = _repo(session, "first"), _repo(session, "second")
    _deployed(session, principal, first, "a" * 40, make_asset("silver.shared"))
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(principal, "dev", repository="second", kind=DeployKind.DEPLOY)
    run = _claimed(session, session.get(DeployRun, view.id))
    assert run is not None
    run.resolved_sha = "b" * 40
    with pytest.raises(
        ValidationError, match=r"table 'silver\.shared' is produced by repository 'first' in dev"
    ):
        service.record_deploy(
            principal,
            run,
            manifest=_manifest(make_asset("silver.shared")),
            code_uri="u",
            code_sha256="0" * 64,
            diff=DeployDiff(),
            expected_job_version_id=None,
            worker_id="w1",
        )
    assert session.query(JobVersion).filter(JobVersion.repository_id == second.id).count() == 0
    assert (
        session.query(EnvironmentDeployment)
        .filter(EnvironmentDeployment.repository_id == second.id)
        .count()
        == 0
    )


def test_the_ownership_lock_is_a_postgres_advisory_lock_keyed_on_the_environment() -> None:
    """On PostgreSQL the lock is `pg_advisory_xact_lock(<signed 64-bit key of the environment>)`.

    The behaviour under real contention is pinned by the integration tier; here only the
    statement and its key are checked, on a stub session.
    """
    from types import SimpleNamespace

    statements: list[str] = []

    class StubSession:
        """Reports a PostgreSQL bind and records what it is asked to run."""

        def get_bind(self) -> SimpleNamespace:
            """Return a bind that looks like PostgreSQL.

            Returns
            -------
            SimpleNamespace
                An object with ``dialect.name``.
            """
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def execute(self, statement: ClauseElement) -> None:
            """Record the compiled statement.

            Parameters
            ----------
            statement
                The statement.
            """
            statements.append(str(statement.compile(compile_kwargs={"literal_binds": True})))

    environment_id = uuid.UUID(int=(1 << 127) | 5)
    DeployService(StubSession(), AllowAllAuthorization())._lock_ownership(environment_id)  # ty: ignore[invalid-argument-type]
    expected = int.from_bytes(environment_id.bytes[:8], "big", signed=True)
    assert expected < 0
    assert statements == [f"SELECT pg_advisory_xact_lock({expected}) AS pg_advisory_xact_lock_1"]


def test_to_sha_naming_a_commit_deployed_only_to_another_environment_is_refused(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A SHA recorded in `prod` is not a rollback target in `dev`."""
    repo = _repo(session)
    _deployed(session, principal, repo, "a" * 40, make_asset("silver.one"))
    ensure_table(session, "prod", "bronze", "events")
    prod = session.execute(select(Environment).where(Environment.name == "prod")).scalar_one()
    session.add(
        JobVersion(
            id=uuid.uuid4(),
            repository_id=repo.id,
            environment_id=prod.id,
            commit_sha="f" * 40,
            manifest_json=_manifest(make_asset("silver.one")),
            code_uri="u",
            code_sha256="0" * 64,
        )
    )
    session.commit()
    with pytest.raises(ValidationError, match="never been deployed to dev"):
        DeployService(session, AllowAllAuthorization()).submit(
            principal, "dev", repository="analytics", kind=DeployKind.ROLLBACK, to_sha="f" * 40
        )


def test_the_ownership_lock_is_retaken_after_a_unique_violation_rolls_it_back(
    session: Session, env: None, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A unique violation rolls the lock back; the re-check takes it again."""
    repo = _repo(session)
    winner = _deployed(session, principal, repo, "a" * 40)
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(principal, "dev", repository="analytics", kind=DeployKind.DEPLOY)
    run = _claimed(session, session.get(DeployRun, view.id))
    assert run is not None
    run.resolved_sha = "a" * 40
    real = service._existing_job_version
    seen: list[str] = []
    locks: list[uuid.UUID] = []

    def blind_first(
        environment_id: uuid.UUID, repository_id: uuid.UUID, sha: str
    ) -> JobVersion | None:
        seen.append(sha)
        return None if len(seen) == 1 else real(environment_id, repository_id, sha)

    monkeypatch.setattr(service, "_existing_job_version", blind_first)
    monkeypatch.setattr(service, "_lock_ownership", locks.append)
    service.record_deploy(
        principal,
        run,
        manifest=_manifest(),
        code_uri="u",
        code_sha256="0" * 64,
        diff=DeployDiff(),
        expected_job_version_id=winner.id,
        worker_id="w1",
    )
    assert len(locks) == 2


def test_a_python_asset_has_no_file_references() -> None:
    asset = ParsedAsset(
        table="gold.scores",
        path="assets/gold/scores.py",
        block_line=1,
        body_line=2,
        metadata=AssetMetadata(materialize="table"),
        sql="",
        normalized_sql="python:sha256:" + "a" * 64,
        references=("silver.clean",),
        columns=None,
        lineage={},
        kind="python",
        inputs=(("obs", "silver.clean"),),
    )
    assert deploys_module._file_references(asset) == set()


def _as_artifact(session: Session, namespace: str, name: str) -> None:
    """Make `namespace.name` an artifact row in `dev`.

    Parameters
    ----------
    session
        Database session.
    namespace
        The namespace.
    name
        The name.
    """
    _as_type(session, namespace, name, TableType.ARTIFACT)


def _as_type(session: Session, namespace: str, name: str, table_type: TableType) -> None:
    """Make `namespace.name` a row of the given type in `dev`.

    Parameters
    ----------
    session
        Database session.
    namespace
        The namespace.
    name
        The name.
    table_type
        The type to give the row.
    """
    _, table_id = ensure_table(session, "dev", namespace, name)
    row = session.get(Table, table_id)
    assert row is not None
    row.table_type = table_type
    session.commit()


def _python(table: str, inputs: dict[str, str], *, output: str = "table") -> ParsedAsset:
    """Build a parsed Python asset.

    Parameters
    ----------
    table
        Its name.
    inputs
        Parameter to input name.
    output
        ``table`` or ``artifact``.

    Returns
    -------
    ParsedAsset
        The asset.
    """
    return dataclasses.replace(
        make_asset(table, tuple(sorted(set(inputs.values()))), columns=None),
        path=f"assets/{table.replace('.', '/')}.py",
        sql="",
        kind="python",
        inputs=tuple(inputs.items()),
        output=output,
    )


def _issues(
    session: Session,
    deployer: PrincipalRef,
    assets: list[ParsedAsset],
    *,
    repo: Repository | None = None,
) -> list[str]:
    """Run the environment validation as `deployer`, under real grants, and list the messages.

    Parameters
    ----------
    session
        Database session.
    deployer
        The deploying principal, holding no read grant.
    assets
        The project's assets.
    repo
        The repository; a new one when `None`.

    Returns
    -------
    list of str
        Issue messages.
    """
    service = DeployService(session, GrantAuthorization(session))
    repo = repo or _repo(session)
    return [
        issue.message
        for issue in service.validate_against_environment(
            deployer, "dev", repo.id, assets, build_graph(assets)
        )
    ]


def test_input_kinds_come_from_the_project_first_then_the_catalog(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """Kinds are known at deploy."""
    _as_artifact(session, "models", "base")
    ensure_table(session, "dev", "gold", "rows")
    service = DeployService(session, AllowAllAuthorization())
    assets = [
        _python("models.churn", {"rows": "gold.rows", "base": "models.base"}, output="artifact"),
        _python("gold.predictions", {"model": "models.churn", "rows": "gold.rows"}),
    ]
    graph = build_graph(assets)
    assert service.artifact_inputs(principal, "dev", graph) == frozenset({"models.base"})
    assert (
        service.validate_against_environment(principal, "dev", _repo(session).id, assets, graph)
        == []
    )


def test_two_parameters_naming_one_object_get_one_kind(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A kind belongs to the object, never to the parameter."""
    _as_artifact(session, "models", "base")
    service = DeployService(session, AllowAllAuthorization())
    assets = [
        _python("models.churn", {"a": "models.base", "b": "models.base"}, output="artifact"),
        _python("gold.p", {"x": "models.churn", "y": "models.churn", "z": "bronze.events"}),
    ]
    graph = build_graph(assets)
    manifest = build_manifest(
        assets,
        graph,
        ResolvedProjectConfig(),
        artifacts=service.artifact_inputs(principal, "dev", graph),
    )
    entries = manifest_assets(manifest)
    assert entry_input_kinds(entries["models.churn"]) == {"a": "artifact", "b": "artifact"}
    assert entry_input_kinds(entries["gold.p"]) == {"x": "artifact", "y": "artifact", "z": "table"}


def test_an_input_that_differs_only_in_case_is_another_object(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The catalog is case-sensitive, so a differently written own name is not the artifact."""
    assets = [
        _python("models.churn", {}, output="artifact"),
        _python("gold.p", {"m": "Models.Churn"}),
    ]
    service = DeployService(session, AllowAllAuthorization())
    graph = build_graph(assets)
    assert service.artifact_inputs(principal, "dev", graph) == frozenset()
    issues = service.validate_against_environment(
        principal, "dev", _repo(session).id, assets, graph
    )
    assert [issue.message for issue in issues] == ["table 'Models.Churn' not found"]


def test_an_artifact_asset_reading_itself_is_refused_even_if_it_gets_past_the_parser(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """Parsing refuses it at parse time; deploy keeps refusing a hand-built asset."""
    assets = [_python("models.churn", {"me": "models.churn"}, output="artifact")]
    service = DeployService(session, AllowAllAuthorization())
    issues = service.validate_against_environment(
        principal, "dev", _repo(session).id, assets, build_graph(assets)
    )
    assert [issue.message for issue in issues] == [
        "an artifact asset cannot read its own artifact (models.churn); remove it from inputs"
    ]


def test_a_table_asset_may_read_itself(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """Only an artifact asset is barred from reading its own output."""
    assets = [_python("gold.p", {"me": "gold.p"})]
    service = DeployService(session, AllowAllAuthorization())
    assert (
        service.validate_against_environment(
            principal, "dev", _repo(session).id, assets, build_graph(assets)
        )
        == []
    )


@pytest.mark.parametrize("exists", [False, True], ids=["missing", "exists-unreadable"])
def test_an_unreadable_artifact_input_is_not_found_like_a_missing_one(
    session: Session, env: None, exists: bool
) -> None:
    """The deploy output does not change with the existence of what it may not read."""
    if exists:
        _as_artifact(session, "models", "base")
    deployer = _principal(session, "ci")
    assets = [_python("gold.predictions", {"model": "models.base"})]
    graph = build_graph(assets)
    service = DeployService(session, GrantAuthorization(session))
    assert service.artifact_inputs(deployer, "dev", graph) == frozenset()
    assert _issues(session, deployer, assets) == ["table 'models.base' not found"]


@pytest.mark.parametrize("exists", [False, True], ids=["missing", "exists-unreadable"])
def test_a_sql_asset_reading_an_unreadable_artifact_gets_the_not_found_of_a_missing_one(
    session: Session, env: None, exists: bool
) -> None:
    if exists:
        _as_artifact(session, "models", "base")
    deployer = _principal(session, "ci")
    assert _issues(session, deployer, [make_asset("gold.report", ("models.base",))]) == [
        "table 'models.base' not found"
    ]


def test_a_sql_asset_cannot_read_an_artifact(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A SQL asset reads tables; the issue says who can read an artifact."""
    _as_artifact(session, "models", "base")
    service = DeployService(session, AllowAllAuthorization())
    assets = [
        _python("models.churn", {}, output="artifact"),
        make_asset("gold.report", ("models.base", "models.churn")),
    ]
    issues = service.validate_against_environment(
        principal, "dev", _repo(session).id, assets, build_graph(assets)
    )
    assert [(issue.path, issue.message) for issue in issues] == [
        (
            "assets/gold/report.sql",
            "`models.base` is an artifact; only a Python asset can read it.",
        ),
        (
            "assets/gold/report.sql",
            "`models.churn` is an artifact; only a Python asset can read it.",
        ),
    ]


@pytest.mark.parametrize(
    ("existing", "asset_output", "message"),
    [
        (
            TableType.ICEBERG,
            "artifact",
            "`models.churn` already exists in dev as a table; drop it first, or choose another "
            "name.",
        ),
        (
            TableType.VIEW,
            "artifact",
            "`models.churn` already exists in dev as a view; drop it first, or choose another "
            "name.",
        ),
        (
            TableType.EXTERNAL,
            "artifact",
            "`models.churn` already exists in dev as an external table; drop it first, or "
            "choose another name.",
        ),
        (
            TableType.ARTIFACT,
            "table",
            "`models.churn` already exists in dev as an artifact; drop it first "
            "(`lakana artifact drop models.churn`), or choose another name.",
        ),
    ],
)
def test_a_name_that_is_the_other_kind_in_the_catalog_is_a_deploy_issue(
    session: Session,
    env: None,
    principal: PrincipalRef,
    existing: TableType,
    asset_output: str,
    message: str,
) -> None:
    """A clash is an issue with a next step, never an exception."""
    _as_type(session, "models", "churn", existing)
    service = DeployService(session, AllowAllAuthorization())
    repo_id = _repo(session).id
    assets = [_python("models.churn", {}, output=asset_output)]
    issues = service.validate_against_environment(
        principal, "dev", repo_id, assets, build_graph(assets)
    )
    assert [issue.message for issue in issues] == [message]
    same_kind = "artifact" if existing is TableType.ARTIFACT else "table"
    matching = [_python("models.churn", {}, output=same_kind)]
    assert (
        service.validate_against_environment(
            principal, "dev", repo_id, matching, build_graph(matching)
        )
        == []
    )


def test_a_sql_asset_on_an_artifact_name_is_a_clash(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A SQL asset is a table asset: it may not take an artifact's name either."""
    _as_artifact(session, "models", "churn")
    service = DeployService(session, AllowAllAuthorization())
    assets = [make_asset("models.churn", ("bronze.events",))]
    issues = service.validate_against_environment(
        principal, "dev", _repo(session).id, assets, build_graph(assets)
    )
    assert [issue.message for issue in issues] == [
        "`models.churn` already exists in dev as an artifact; drop it first "
        "(`lakana artifact drop models.churn`), or choose another name."
    ]


@pytest.mark.parametrize("exists", [False, True], ids=["missing", "exists-unreadable"])
@pytest.mark.parametrize(
    ("existing", "asset_output"),
    [
        (TableType.ICEBERG, "artifact"),
        (TableType.VIEW, "artifact"),
        (TableType.EXTERNAL, "artifact"),
        (TableType.ARTIFACT, "table"),
    ],
)
def test_a_clash_with_an_unreadable_object_is_left_to_the_run(
    session: Session, env: None, exists: bool, existing: TableType, asset_output: str
) -> None:
    """No new text confirms an object the deployer may not read."""
    if exists:
        _as_type(session, "models", "churn", existing)
    deployer = _principal(session, "ci")
    assert _issues(session, deployer, [_python("models.churn", {}, output=asset_output)]) == []


def test_an_artifact_this_project_deployed_before_is_not_a_clash(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    repo = _repo(session)
    asset = _python("models.churn", {}, output="artifact")
    _deployed(session, principal, repo, "a" * 40, asset)
    _as_artifact(session, "models", "churn")
    service = DeployService(session, AllowAllAuthorization())
    assert (
        service.validate_against_environment(
            principal, "dev", repo.id, [asset], build_graph([asset])
        )
        == []
    )
    table = [make_asset("models.churn", ("bronze.events",))]
    issues = service.validate_against_environment(
        principal, "dev", repo.id, table, build_graph(table)
    )
    assert [issue.message for issue in issues] == [
        "`models.churn` already exists in dev as an artifact; drop it first "
        "(`lakana artifact drop models.churn`), or choose another name."
    ]


@pytest.mark.parametrize(
    ("mine", "theirs"), [("artifact", "table"), ("table", "artifact"), ("artifact", "artifact")]
)
def test_a_name_another_repository_produces_stays_refused_for_either_kind(
    session: Session, env: None, principal: PrincipalRef, mine: str, theirs: str
) -> None:
    """The ownership check goes by name, so it holds for an artifact name too."""
    other = _repo(session, "other")
    _deployed(session, principal, other, "a" * 40, _python("models.churn", {}, output=theirs))
    if theirs == "artifact":
        _as_artifact(session, "models", "churn")
    service = DeployService(session, AllowAllAuthorization())
    assets = [_python("models.churn", {}, output=mine)]
    issues = service.validate_against_environment(
        principal, "dev", _repo(session, "mine").id, assets, build_graph(assets)
    )
    assert "table 'models.churn' is produced by repository 'other' in dev" in [
        issue.message for issue in issues
    ]


def test_an_artifact_no_deploy_produces_any_more_is_an_orphan(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A removed artifact asset is not dropped; it shows up under `lakana orphans`."""
    repo = _repo(session)
    _deployed(session, principal, repo, "a" * 40, _python("models.churn", {}, output="artifact"))
    _as_artifact(session, "models", "churn")
    _deployed(session, principal, repo, "b" * 40, make_asset("silver.other", ()))
    report = DeployService(session, AllowAllAuthorization()).orphans(principal, "dev")
    assert [(orphan.repository, orphan.table) for orphan in report.orphans] == [
        (repo.name, "models.churn")
    ]
    assert resolve_table_id(session, "dev", "models", "churn") is not None


def _reader_of(session: Session, admin: PrincipalRef, *tables: str) -> PrincipalRef:
    """Create a deployer that may read only the named tables.

    Parameters
    ----------
    session
        Database session.
    admin
        Grants the access.
    *tables
        Dotted table names the deployer may read.

    Returns
    -------
    PrincipalRef
        The deployer.
    """
    deployer = _principal(session, "ci")
    grants = GrantService(session, AllowAllAuthorization())
    for dotted in tables:
        namespace, table = dotted.split(".")
        grants.grant(
            admin,
            principal="ci",
            action=Action.READ_TABLE,
            kind=SecurableKind.TABLE,
            environment="dev",
            namespace=namespace,
            table=table,
        )
    return deployer


def _row_state(
    session: Session, admin: PrincipalRef, state: str, table_type: TableType
) -> PrincipalRef:
    """Put `models.x` in the catalog as `state` says and return a deployer for it.

    Parameters
    ----------
    session
        Database session.
    admin
        Grants the access.
    state
        ``no-row``, ``unreadable-row`` or ``readable-row``.
    table_type
        The type of the row, when there is one.

    Returns
    -------
    PrincipalRef
        A deployer who may read `models.x` only in ``readable-row``.
    """
    if state != "no-row":
        _as_type(session, "models", "x", table_type)
    return _reader_of(session, admin, *(["models.x"] if state == "readable-row" else []))


def _consumer(session: Session) -> Repository:
    """Return the repository ``consumer``, creating it on first use.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    Repository
        The repository whose deploy the tests check.
    """
    found = session.execute(
        select(Repository).where(Repository.name == "consumer")
    ).scalar_one_or_none()
    return found if found is not None else _repo(session, "consumer")


def _complete_output(
    session: Session, deployer: PrincipalRef, assets: list[ParsedAsset]
) -> dict[str, object]:
    """Run every deploy check as `deployer` and return everything a deploy would say or record.

    Parameters
    ----------
    session
        Database session.
    deployer
        The deploying principal, under real grants.
    assets
        The project's assets.

    Returns
    -------
    dict
        ``issues`` (path, line, message, in order), ``artifacts`` (sorted artifact
        inputs), ``assets`` and ``external_inputs`` (the whole manifest) and ``diff``
        (the diff against no previous deploy, as JSON).
    """
    service = DeployService(session, GrantAuthorization(session))
    graph = build_graph(assets)
    repo = _consumer(session)
    issues = [
        (issue.path, issue.line, issue.message)
        for issue in service.validate_against_environment(deployer, "dev", repo.id, assets, graph)
    ]
    found = service.artifact_inputs(deployer, "dev", graph, repository_id=repo.id)
    manifest = build_manifest(assets, graph, ResolvedProjectConfig(), artifacts=found)
    diff = diff_manifests(
        None,
        manifest,
        existing_unowned=service.existing_unowned(deployer, "dev", manifest_tables(manifest)),
    )
    return {
        "issues": issues,
        "artifacts": sorted(found),
        "assets": manifest["assets"],
        "external_inputs": manifest["external_inputs"],
        "diff": diff.to_json(),
    }


def _whole_output(
    session: Session, deployer: PrincipalRef, assets: list[ParsedAsset]
) -> tuple[list[tuple[str, int | None, str]], list[str], list[dict[str, str]]]:
    """Run every deploy check as `deployer` and return what a deploy would say about kinds.

    Parameters
    ----------
    session
        Database session.
    deployer
        The deploying principal, under real grants.
    assets
        The project's assets.

    Returns
    -------
    tuple
        The issues (path, line, message) in order, the artifact inputs, and the
        `input_kinds` of every Python entry of the manifest.
    """
    complete = _complete_output(session, deployer, assets)
    entries = cast("list[dict[str, object]]", complete["assets"])
    kinds = [entry_input_kinds(entry) for entry in entries if entry.get("kind") == "python"]
    return (
        cast("list[tuple[str, int | None, str]]", complete["issues"]),
        cast("list[str]", complete["artifacts"]),
        kinds,
    )


_ROW_STATES = ["no-row", "unreadable-row", "readable-row"]


@pytest.mark.parametrize("state", _ROW_STATES)
@pytest.mark.parametrize(
    ("theirs", "kind"), [("artifact", "artifact"), ("table", "table")], ids=["artifact", "table"]
)
def test_the_kind_of_an_input_another_repository_produces_comes_from_its_deploy(
    session: Session, env: None, principal: PrincipalRef, state: str, theirs: str, kind: str
) -> None:
    """No row, an unreadable row and a consistent readable row all say the same."""
    other = _repo(session, "other")
    _deployed(session, principal, other, "a" * 40, _python("models.x", {}, output=theirs))
    deployer = _row_state(
        session, principal, state, TableType.ARTIFACT if theirs == "artifact" else TableType.ICEBERG
    )
    assets = [_python("gold.p", {"m": "models.x"})]
    issues, found, kinds = _whole_output(session, deployer, assets)
    assert issues == []
    assert found == (["models.x"] if kind == "artifact" else [])
    assert kinds == [{"m": kind}]


@pytest.mark.parametrize("state", _ROW_STATES)
def test_a_sql_asset_reading_what_another_repository_produces_as_an_artifact_is_refused_alike(
    session: Session, env: None, principal: PrincipalRef, state: str
) -> None:
    other = _repo(session, "other")
    _deployed(session, principal, other, "a" * 40, _python("models.x", {}, output="artifact"))
    deployer = _row_state(session, principal, state, TableType.ARTIFACT)
    issues, found, kinds = _whole_output(
        session, deployer, [make_asset("gold.report", ("models.x",))]
    )
    assert issues == [
        (
            "assets/gold/report.sql",
            1,
            "`models.x` is an artifact; only a Python asset can read it.",
        )
    ]
    assert (found, kinds) == (["models.x"], [])


@pytest.mark.parametrize("state", ["no-row", "unreadable-row"])
@pytest.mark.parametrize("reads", ["python", "sql"])
def test_a_name_no_repository_produces_is_a_missing_name_even_if_another_name_is_produced(
    session: Session, env: None, principal: PrincipalRef, state: str, reads: str
) -> None:
    """The one bit is only for a produced name: the rest is a missing name, to the letter."""
    other = _repo(session, "other")
    _deployed(session, principal, other, "a" * 40, _python("models.y", {}, output="artifact"))
    deployer = _row_state(session, principal, state, TableType.ARTIFACT)
    consumer = (
        _python("gold.p", {"m": "models.x"})
        if reads == "python"
        else make_asset("gold.p", ("models.x",))
    )
    issues, found, kinds = _whole_output(session, deployer, [consumer])
    assert issues == [(consumer.path, consumer.body_line, "table 'models.x' not found")]
    assert found == []
    assert kinds in ([{"m": "table"}], [])


@pytest.mark.parametrize(
    ("row", "theirs", "kind"),
    [
        (TableType.ICEBERG, "artifact", "table"),
        (TableType.ARTIFACT, "table", "artifact"),
    ],
    ids=["catalog-table-manifest-artifact", "catalog-artifact-manifest-table"],
)
def test_a_readable_row_wins_over_the_manifest_of_the_producer(
    session: Session, env: None, principal: PrincipalRef, row: TableType, theirs: str, kind: str
) -> None:
    """The catalog describes the object that exists; the run re-checks it."""
    other = _repo(session, "other")
    _deployed(session, principal, other, "a" * 40, _python("models.x", {}, output=theirs))
    deployer = _row_state(session, principal, "readable-row", row)
    _, found, kinds = _whole_output(session, deployer, [_python("gold.p", {"m": "models.x"})])
    assert (found, kinds) == (["models.x"] if kind == "artifact" else [], [{"m": kind}])


@pytest.mark.parametrize(
    "damaged",
    [
        {"assets": [3]},
        {"assets": 3},
        {"assets": [{"kind": "python"}]},
        {"assets": ["x", None]},
        [1],
    ],
    ids=["int-entry", "int-assets", "entry-without-table", "junk-entries", "list-manifest"],
)
def test_a_damaged_manifest_of_another_repository_does_not_break_this_deploy(
    session: Session, env: None, principal: PrincipalRef, damaged: dict[str, object] | list[int]
) -> None:
    good = _repo(session, "good")
    _deployed(session, principal, good, "a" * 40, _python("models.x", {}, output="artifact"))
    bad = _repo(session, "bad")
    version = _deployed(session, principal, bad, "b" * 40, make_asset("gold.q", ("bronze.events",)))
    version.manifest_json = cast("dict", damaged)  # a stored manifest can be anything
    session.commit()
    deployer = _reader_of(session, principal, "bronze.events")
    issues, found, kinds = _whole_output(
        session, deployer, [_python("gold.p", {"m": "models.x", "e": "bronze.events"})]
    )
    assert (issues, found, kinds) == ([], ["models.x"], [{"e": "table", "m": "artifact"}])


def test_the_deployers_own_repository_is_not_elsewhere_for_the_kind_of_an_input(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The bit is for "another repository": the output ignores the deployer's own deploy."""
    deployer = _reader_of(session, principal)
    repo = _consumer(session)
    assets = [make_asset("gold.report", ("models.x",)), _python("gold.p", {"m": "models.x"})]
    before = _whole_output(session, deployer, assets)
    _deployed(session, principal, repo, "a" * 40, _python("models.x", {}, output="artifact"))
    assert _whole_output(session, deployer, assets) == before
    assert before[0] == [("assets/gold/p.py", 1, "table 'models.x' not found")]


_ALL_TYPES = [TableType.ICEBERG, TableType.VIEW, TableType.ARTIFACT, TableType.EXTERNAL]

_ROLES = {
    "python-input": lambda: [_python("gold.p", {"m": "models.x"})],
    "sql-reference": lambda: [make_asset("gold.p", ("models.x",))],
    "python-and-sql-readers": lambda: [
        _python("gold.p", {"m": "models.x"}),
        make_asset("gold.q", ("models.x",)),
    ],
    "own-name-as-sql-asset": lambda: [make_asset("models.x", ("bronze.events",))],
    "own-name-as-python-table": lambda: [_python("models.x", {"e": "bronze.events"})],
    "own-name-as-python-artifact": lambda: [
        _python("models.x", {"e": "bronze.events"}, output="artifact")
    ],
}


@pytest.mark.parametrize("role", list(_ROLES))
@pytest.mark.parametrize(
    "producer",
    ["no-producer", "producer-of-artifact", "producer-of-table", "producer-of-another-name"],
)
def test_an_unreadable_row_of_any_type_says_nothing_a_missing_row_does_not(
    session: Session, env: None, principal: PrincipalRef, role: str, producer: str
) -> None:
    """A deploy must not reveal the existence or the type of a row its principal cannot read.

    Whatever another repository produces, an unreadable row of any of the four types
    gives the whole output (issues in order, artifact inputs, every manifest entry,
    the diff) of no row at all.
    """
    other = _repo(session, "other")
    if producer in ("producer-of-artifact", "producer-of-table"):
        output = "artifact" if producer == "producer-of-artifact" else "table"
        _deployed(session, principal, other, "a" * 40, _python("models.x", {}, output=output))
    elif producer == "producer-of-another-name":
        _deployed(session, principal, other, "a" * 40, _python("models.y", {}, output="artifact"))
    deployer = _reader_of(session, principal)
    assets = _ROLES[role]()
    without_row = _complete_output(session, deployer, assets)
    for table_type in _ALL_TYPES:
        _as_type(session, "models", "x", table_type)
        assert _complete_output(session, deployer, assets) == without_row, table_type.name


@pytest.mark.parametrize(
    ("parameters", "messages"),
    [
        pytest.param(("model",), [], id="an-ascii-identifier"),
        pytest.param(("_m1", "M_2"), [], id="underscores-digits-and-capitals"),
        pytest.param(("m" * 128,), [], id="as-long-as-allowed"),
        pytest.param(
            ("modèle",),
            [
                "parameter 'modèle' reads the artifact `models.base`, which the task stores in a "
                "directory of that name: rename the parameter, in the function and in inputs, to "
                "an ASCII identifier of at most 128 characters (letters, digits, underscores)"
            ],
            id="non-ascii",
        ),
        pytest.param(
            ("m" * 129,),
            [
                f"parameter '{'m' * 129}' reads the artifact `models.base`, which the task "
                "stores in a directory of that name: rename the parameter, in the function and "
                "in inputs, to an ASCII identifier of at most 128 characters (letters, digits, "
                "underscores)"
            ],
            id="too-long",
        ),
        pytest.param(
            ("Model", "model"),
            [
                "parameters 'Model' and 'model' read artifacts and differ only in case, so a "
                "file system may store both in one directory: rename one, in the function and "
                "in inputs"
            ],
            id="case-only",
        ),
    ],
)
def test_an_artifact_parameter_the_task_cannot_store_is_a_deploy_issue(
    session: Session,
    env: None,
    principal: PrincipalRef,
    parameters: tuple[str, ...],
    messages: list[str],
) -> None:
    """The harness exits with status 2 for these; deploy says so first, naming the parameter."""
    _as_artifact(session, "models", "base")
    _as_artifact(session, "models", "other")
    service = DeployService(session, AllowAllAuthorization())
    tables = ["models.base", "models.other"]
    inputs = {name: tables[min(i, 1)] for i, name in enumerate(parameters)}
    assets = [_python("gold.p", inputs)]
    issues = service.validate_against_environment(
        principal, "dev", _repo(session).id, assets, build_graph(assets)
    )
    assert [issue.message for issue in issues] == messages


def test_a_table_parameter_may_be_any_python_name(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The rule is about directories, which only an artifact copy gets."""
    ensure_table(session, "dev", "gold", "rows")
    ensure_table(session, "dev", "gold", "more")
    service = DeployService(session, AllowAllAuthorization())
    assets = [_python("gold.p", {"données": "gold.rows", "Données": "gold.more"})]
    assert (
        service.validate_against_environment(
            principal, "dev", _repo(session).id, assets, build_graph(assets)
        )
        == []
    )


_FINISHING = ["record_deploy", "record_dry_run", "execute_rollback"]
_KIND_OF = {
    "record_deploy": DeployKind.DEPLOY,
    "record_dry_run": DeployKind.DRY_RUN,
    "execute_rollback": DeployKind.ROLLBACK,
}


def _finish_call(
    service: DeployService,
    principal: PrincipalRef,
    run: DeployRun,
    operation: str,
    *,
    worker_id: str | None,
) -> None:
    """Call one of the three recording methods with empty arguments.

    Parameters
    ----------
    service
        The deploy service.
    principal
        The run's principal.
    run
        The run to finish.
    operation
        `record_deploy`, `record_dry_run` or `execute_rollback`.
    worker_id
        The id the caller claims to run under.
    """
    if operation == "record_deploy":
        service.record_deploy(
            principal,
            run,
            manifest=_manifest(),
            code_uri="u",
            code_sha256="0" * 64,
            diff=DeployDiff(),
            expected_job_version_id=None,
            worker_id=worker_id,
        )
    elif operation == "record_dry_run":
        service.record_dry_run(principal, run, diff=DeployDiff(), worker_id=worker_id)
    else:
        service.execute_rollback(principal, run, worker_id=worker_id)


def _ended_by_the_sweep(session: Session, run: DeployRun) -> None:
    """Set a run failed with the sweep's message, the way `end_lost_runs` does.

    Parameters
    ----------
    session
        Database session; committed here.
    run
        The run.
    """
    session.execute(
        update(DeployRun)
        .where(DeployRun.id == run.id)
        .values(status=DeployStatus.FAILED, error_message=DEPLOY_LOST_MESSAGE)
        .execution_options(synchronize_session=False)
    )
    session.commit()


def _nothing_was_recorded(session: Session) -> None:
    """Assert that no project version, pointer or deploy audit event exists.

    Parameters
    ----------
    session
        Database session.
    """
    assert session.query(JobVersion).count() == 0
    assert session.query(EnvironmentDeployment).count() == 0
    assert (
        session.query(AuditEvent)
        .filter(
            AuditEvent.event_type.in_(
                [
                    AuditEventType.DEPLOYED,
                    AuditEventType.DEPLOY_DRY_RUN,
                    AuditEventType.ROLLED_BACK,
                ]
            )
        )
        .count()
        == 0
    )


@pytest.mark.parametrize("operation", _FINISHING)
def test_a_finishing_call_on_a_run_the_sweep_ended_records_nothing(
    session: Session, env: None, principal: PrincipalRef, operation: str
) -> None:
    """A late worker meets `failed`: its call raises and nothing it would have written lands."""
    _repo(session)
    run = _queued(session, principal, _KIND_OF[operation])
    _ended_by_the_sweep(session, run)
    service = DeployService(session, AllowAllAuthorization())
    with pytest.raises(ConflictError) as raised:
        _finish_call(service, principal, run, operation, worker_id="w1")
    assert str(raised.value) == DEPLOY_ENDED_MESSAGE.format(id=run.id)
    session.expire_all()
    assert (run.status, run.error_message) == (DeployStatus.FAILED, DEPLOY_LOST_MESSAGE)
    _nothing_was_recorded(session)


@pytest.mark.parametrize("operation", _FINISHING)
def test_a_finishing_call_under_another_workers_id_records_nothing(
    session: Session, env: None, principal: PrincipalRef, operation: str
) -> None:
    """A run still `running`, but claimed by another worker, is not this caller's to finish."""
    _repo(session)
    run = _queued(session, principal, _KIND_OF[operation])
    service = DeployService(session, AllowAllAuthorization())
    with pytest.raises(ConflictError, match="is no longer this worker's running run"):
        _finish_call(service, principal, run, operation, worker_id="w2")
    session.expire_all()
    assert (run.status, run.worker_id) == (DeployStatus.RUNNING, "w1")
    _nothing_was_recorded(session)


def test_the_deploy_ended_message_is_true_for_every_case_it_is_raised_in() -> None:
    """It says the run is not this worker's running run and that nothing was changed."""
    text = DEPLOY_ENDED_MESSAGE.format(id="X")
    assert text == ("Deploy run X is no longer this worker's running run, so nothing was changed.")


def test_fail_does_not_overwrite_a_run_the_sweep_ended(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The sweep's message stays; `fail` raises instead of writing a second outcome."""
    _repo(session)
    run = _queued(session, principal)
    _ended_by_the_sweep(session, run)
    with pytest.raises(ConflictError, match="is no longer this worker's running run"):
        DeployService(session, AllowAllAuthorization()).fail(run, "boom", worker_id="w1")
    session.expire_all()
    assert (run.status, run.error_message) == (DeployStatus.FAILED, DEPLOY_LOST_MESSAGE)


def test_fail_marks_a_queued_run_nobody_claimed(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A run still `queued` has no worker; failing it (inactive principal) is allowed (R5.2)."""
    _repo(session)
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(principal, "dev", repository="analytics", kind=DeployKind.DEPLOY)
    run = session.get(DeployRun, view.id)
    assert run is not None
    service.fail(run, "inactive", worker_id=None)
    session.expire_all()
    assert (run.status, run.error_message) == (DeployStatus.FAILED, "inactive")


def test_fail_under_another_workers_id_is_refused(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """Only the worker that claimed a running deploy may fail it."""
    _repo(session)
    run = _queued(session, principal)
    with pytest.raises(ConflictError, match="is no longer this worker's running run"):
        DeployService(session, AllowAllAuthorization()).fail(run, "boom", worker_id="w2")
    session.expire_all()
    assert (run.status, run.error_message) == (DeployStatus.RUNNING, None)


def test_the_run_is_locked_again_after_the_job_version_race(
    session: Session, env: None, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rollback after a unique violation releases the run's lock: it is taken again.

    The run is ended in the gap (the rollback is where the lock is lost). Without the second
    hold the call would go on to move the pointer and turn the failed run into a succeeded one.
    """
    repo = _repo(session)
    winner = _deployed(session, principal, repo, "a" * 40)
    service = DeployService(session, AllowAllAuthorization())
    run = _claimed(
        session,
        session.get(
            DeployRun,
            service.submit(principal, "dev", repository="analytics", kind=DeployKind.DEPLOY).id,
        ),
    )
    run.resolved_sha = "a" * 40
    real = service._existing_job_version
    seen: list[str] = []

    def blind_first(
        environment_id: uuid.UUID, repository_id: uuid.UUID, sha: str
    ) -> JobVersion | None:
        seen.append(sha)
        return None if len(seen) == 1 else real(environment_id, repository_id, sha)

    real_rollback = session.rollback
    fired: list[bool] = []

    def end_the_run_after_the_rollback() -> None:
        real_rollback()
        if not fired:
            fired.append(True)
            _ended_by_the_sweep(session, run)

    monkeypatch.setattr(service, "_existing_job_version", blind_first)
    monkeypatch.setattr(session, "rollback", end_the_run_after_the_rollback)
    with pytest.raises(ConflictError, match="is no longer this worker's running run"):
        service.record_deploy(
            principal,
            run,
            manifest=_manifest(),
            code_uri="u",
            code_sha256="0" * 64,
            diff=DeployDiff(),
            expected_job_version_id=winner.id,
            worker_id="w1",
        )
    monkeypatch.undo()
    assert fired == [True]
    session.expire_all()
    assert run.status is DeployStatus.FAILED
    pointer = session.execute(select(EnvironmentDeployment)).scalar_one()
    assert pointer.current_job_version_id == winner.id


def test_the_hold_reads_the_run_for_update() -> None:
    """On PostgreSQL the hold is `SELECT … FOR UPDATE` on the run's row, nothing weaker."""
    from types import SimpleNamespace

    statements: list[str] = []

    class StubResult:
        """The one row the stub database answers with."""

        def one_or_none(self) -> tuple[DeployStatus, str]:
            """Return a running run held by `w1`.

            Returns
            -------
            tuple
                Status and worker id.
            """
            return (DeployStatus.RUNNING, "w1")

    class StubSession:
        """Reports a PostgreSQL bind and records what it is asked to run."""

        def get_bind(self) -> SimpleNamespace:
            """Return a bind that looks like PostgreSQL.

            Returns
            -------
            SimpleNamespace
                An object with ``dialect.name``.
            """
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def execute(self, statement: ClauseElement) -> StubResult:
            """Record the compiled statement.

            Parameters
            ----------
            statement
                The statement.

            Returns
            -------
            StubResult
                The row.
            """
            statements.append(str(statement.compile(dialect=postgresql.dialect())))
            return StubResult()

    run = DeployRun(id=uuid.uuid4())
    DeployService(StubSession(), AllowAllAuthorization())._hold(run, "w1")  # ty: ignore[invalid-argument-type]
    assert len(statements) == 1
    assert statements[0].rstrip().endswith("FOR UPDATE")


def test_a_run_deleted_after_the_liveness_check_is_not_found_at_the_hold(
    session: Session, env: None, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hold finds no row to lock when the repository (and its runs) went in between."""
    repo = _repo(session)
    service = DeployService(session, AllowAllAuthorization())
    run = _queued(session, principal)
    _vanish(session, repo, commit=False)
    monkeypatch.setattr(service, "_require_alive", lambda run: ("dev", "analytics"))
    with pytest.raises(NotFoundError, match="no longer exists"):
        service.fail(run, "boom", worker_id="w1")


def test_a_late_rollback_does_not_move_the_pointer_back(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """Two versions deployed, the rollback run ended before it finishes: the pointer stays."""
    repo = _repo(session)
    _deployed(session, principal, repo, "a" * 40, make_asset("silver.one", ("bronze.events",)))
    second = _deployed(
        session, principal, repo, "b" * 40, make_asset("silver.two", ("bronze.events",))
    )
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(
        principal, "dev", repository="analytics", kind=DeployKind.ROLLBACK, to_previous=True
    )
    run = _claimed(session, session.get(DeployRun, view.id))
    assert run is not None
    _ended_by_the_sweep(session, run)
    with pytest.raises(ConflictError) as raised:
        service.execute_rollback(principal, run, worker_id="w1")
    assert str(raised.value) == DEPLOY_ENDED_MESSAGE.format(id=run.id)
    session.expire_all()
    pointer = session.execute(select(EnvironmentDeployment)).scalar_one()
    assert pointer.current_job_version_id == second.id  # a late rollback would have moved it
    assert (run.status, run.error_message) == (DeployStatus.FAILED, DEPLOY_LOST_MESSAGE)
    assert (
        session.query(AuditEvent)
        .filter(AuditEvent.event_type == AuditEventType.ROLLED_BACK)
        .count()
        == 0
    )


_CHECKED = "silver.clean"


def _check_of(sql: str, *, table: str = _CHECKED, name: str = "x") -> ParsedCheck:
    """Build a parsed check of `table` with the given SQL, reading what the SQL names.

    Parameters
    ----------
    sql
        The check's SQL.
    table
        The checked table.
    name
        The check's name.

    Returns
    -------
    ParsedCheck
        The check, its SQL on line 1.
    """
    from lakana_core.query_rewrite import extract_table_refs

    namespace, short = table.rsplit(".", 1)
    return ParsedCheck(
        name=f"{table}/{name}",
        table=table,
        check=name,
        path=f"checks/{namespace}/{short}/{name}.sql",
        on_failure="block",
        description=None,
        sql=sql,
        references=tuple(sorted(extract_table_refs(sql))),
        body_line=1,
    )


def test_a_check_that_reads_a_table_nobody_can_find_is_refused(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A table a check reads must exist readably or be produced, as for an asset."""
    repo = _repo(session)
    assets = [make_asset(_CHECKED, ("bronze.events",))]
    check = _check_of(f"SELECT 1 FROM {_CHECKED} JOIN dim.missing USING (id)")
    issues = DeployService(session, AllowAllAuthorization()).validate_checks_against_environment(
        principal, "dev", repo.id, [check], assets
    )
    assert [i.render() for i in issues] == [
        "checks/silver/clean/x.sql:1: table 'dim.missing' not found"
    ]


def test_a_check_may_read_a_table_another_repository_produces(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A table another repository's current deploy produces counts as found."""
    other = _repo(session, "other")
    _deployed(session, principal, other, "a" * 40, make_asset("dim.keys", ("bronze.events",)))
    repo = _repo(session)
    assets = [make_asset(_CHECKED, ("bronze.events",))]
    check = _check_of(f"SELECT 1 FROM {_CHECKED} JOIN dim.keys USING (id)")
    service = DeployService(session, AllowAllAuthorization())
    assert (
        service.validate_checks_against_environment(principal, "dev", repo.id, [check], assets)
        == []
    )


def test_a_check_may_read_a_table_the_deployer_can_read(session: Session, env: None) -> None:
    """A table of the catalog the deployer may read passes; one they may not is not found."""
    deployer = _principal(session, "ci")
    grants = GrantService(session, AllowAllAuthorization())
    grants.grant(
        deployer,
        principal="ci",
        action=Action.DEPLOY,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    repo = _repo(session)
    assets = [make_asset(_CHECKED, ("bronze.events",))]
    check = _check_of(f"SELECT 1 FROM {_CHECKED} JOIN bronze.events USING (id)")
    service = DeployService(session, GrantAuthorization(session))
    assert [
        i.message
        for i in service.validate_checks_against_environment(
            deployer, "dev", repo.id, [check], assets
        )
    ] == ["table 'bronze.events' not found"]
    grants.grant(
        deployer,
        principal="ci",
        action=Action.READ_TABLE,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    assert (
        service.validate_checks_against_environment(deployer, "dev", repo.id, [check], assets) == []
    )


def test_a_check_that_reads_a_file_is_not_found(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """``FROM 'x.csv'`` is a file, never a table."""
    repo = _repo(session)
    assets = [make_asset(_CHECKED, ("bronze.events",))]
    check = _check_of(f"SELECT 1 FROM {_CHECKED} JOIN 'x.csv' USING (id)")
    issues = DeployService(session, AllowAllAuthorization()).validate_checks_against_environment(
        principal, "dev", repo.id, [check], assets
    )
    assert [i.message for i in issues] == ["table 'x.csv' not found"]


def test_a_check_that_reads_an_artifact_of_the_project_is_refused(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """SQL cannot read an artifact, so a check that reads one is refused."""
    repo = _repo(session)
    artifact = dataclasses.replace(make_asset("ml.model"), output="artifact", kind="python")
    assets = [make_asset(_CHECKED, ("bronze.events",)), artifact]
    check = _check_of(f"SELECT 1 FROM {_CHECKED} JOIN ml.model USING (id)")
    issues = DeployService(session, AllowAllAuthorization()).validate_checks_against_environment(
        principal, "dev", repo.id, [check], assets
    )
    assert [i.render() for i in issues] == [
        "checks/silver/clean/x.sql:1: reads the artifact 'ml.model'; SQL cannot read an artifact"
    ]


def test_a_check_that_reads_a_catalog_artifact_is_refused(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """An artifact of the catalog the deployer may read is refused the same way."""
    session.execute(
        update(Table).where(Table.name == "events").values(table_type=TableType.ARTIFACT)
    )
    session.commit()
    repo = _repo(session)
    assets = [make_asset(_CHECKED, ("silver.other",))]
    check = _check_of(f"SELECT 1 FROM {_CHECKED} JOIN bronze.events USING (id)")
    issues = DeployService(session, AllowAllAuthorization()).validate_checks_against_environment(
        principal, "dev", repo.id, [check], assets
    )
    assert [i.message for i in issues] == [
        "reads the artifact 'bronze.events'; SQL cannot read an artifact"
    ]


def test_a_dry_run_and_a_deploy_record_their_checks(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The listing is stored on the run and shown by the view; a run without checks has none."""
    repo = _repo(session)
    service = DeployService(session, AllowAllAuthorization())
    listing = checks_listing(
        [dataclasses.replace(_check_of("SELECT 1 FROM silver.clean"), on_failure="warn")]
    )
    dry = service.submit(
        principal, "dev", repository=repo.name, kind=DeployKind.DRY_RUN, ref="main"
    )
    run = _claimed(session, session.get(DeployRun, dry.id))
    run.resolved_sha = "c" * 40
    service.record_dry_run(
        principal, run, diff=DeployDiff(added=("silver.clean",)), worker_id="w1", checks=listing
    )
    expected = {"silver.clean": [{"check": "x", "on_failure": "warn"}]}
    assert service.get(principal, "dev", dry.id).checks == expected

    plain = service.submit(
        principal, "dev", repository=repo.name, kind=DeployKind.DRY_RUN, ref="main"
    )
    run = _claimed(session, session.get(DeployRun, plain.id), "w2")
    run.resolved_sha = "d" * 40
    service.record_dry_run(principal, run, diff=DeployDiff(), worker_id="w2")
    assert service.get(principal, "dev", plain.id).checks is None

    deploy = service.submit(
        principal, "dev", repository=repo.name, kind=DeployKind.DEPLOY, ref="main"
    )
    run = _claimed(session, session.get(DeployRun, deploy.id), "w3")
    run.resolved_sha = "e" * 40
    service.record_deploy(
        principal,
        run,
        manifest=_manifest(make_asset("silver.clean", ("bronze.events",))),
        code_uri="file:///dev/_artifacts/code/analytics/e.tar.gz",
        code_sha256="0" * 64,
        diff=DeployDiff(added=("silver.clean",)),
        expected_job_version_id=None,
        worker_id="w3",
        checks=listing,
    )
    assert service.get(principal, "dev", deploy.id).checks == expected


def _with_check(session: Session, version: JobVersion, sql: str, references: list[str]) -> None:
    """Store one check of `silver.clean` in a project version's manifest, and create its tables.

    Parameters
    ----------
    session
        Database session; commits.
    version
        The project version.
    sql
        The check's SQL.
    references
        What the manifest claims the SQL reads; untrusted, never what decides.
    """
    version.manifest_json = {
        **version.manifest_json,
        "checks": [
            {
                "name": "silver.clean/no_salary",
                "table": "silver.clean",
                "check": "no_salary",
                "path": "checks/silver/clean/no_salary.sql",
                "on_failure": "block",
                "description": "d",
                "sql": sql,
                "references": references,
            }
        ],
    }
    session.commit()
    ensure_table(session, "dev", "silver", "clean")
    ensure_table(session, "dev", "hr", "salaries")


_SECRET_CHECK = "SELECT c.* FROM silver.clean c JOIN hr.salaries s ON c.id = s.id"


def test_a_checks_sql_is_hidden_from_a_reader_without_access_to_one_of_its_tables(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The SQL is read again; the stored references, which leave out `hr.salaries`, are ignored."""
    repo = _repo(session)
    version = _deployed(session, principal, repo, "a" * 40, make_asset("silver.clean"))
    _with_check(session, version, _SECRET_CHECK, ["silver.clean"])
    reader = _read_environment_only(session, principal, "silver.clean")

    view = DeployService(session, GrantAuthorization(session)).job_version(
        reader, "dev", version.id
    )

    [check] = view.manifest["checks"]
    assert check == {
        "name": "silver.clean/no_salary",
        "table": "silver.clean",
        "check": "no_salary",
        "path": "checks/silver/clean/no_salary.sql",
        "on_failure": "block",
        "description": "d",
        "hidden_references": 1,
    }
    assert "sql" in version.manifest_json["checks"][0]


def test_a_checks_sql_is_kept_for_a_reader_of_every_table_it_reads(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    repo = _repo(session)
    version = _deployed(session, principal, repo, "a" * 40, make_asset("silver.clean"))
    _with_check(session, version, _SECRET_CHECK, ["silver.clean"])
    reader = _read_environment_only(session, principal, "silver.clean", "hr.salaries")

    view = DeployService(session, GrantAuthorization(session)).job_version(
        reader, "dev", version.id
    )

    [check] = view.manifest["checks"]
    assert check["sql"] == _SECRET_CHECK
    assert check["references"] == ["hr.salaries", "silver.clean"]
    assert check["hidden_references"] == 0


def test_a_checks_sql_that_cannot_be_read_is_hidden(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A stored manifest is untrusted: SQL whose tables cannot be listed is never shown."""
    repo = _repo(session)
    version = _deployed(session, principal, repo, "a" * 40, make_asset("silver.clean"))
    _with_check(session, version, "SELECT FROM WHERE (", [])
    reader = _read_environment_only(session, principal, "silver.clean")

    view = DeployService(session, GrantAuthorization(session)).job_version(
        reader, "dev", version.id
    )

    [check] = view.manifest["checks"]
    assert "sql" not in check and "references" not in check
    assert check["hidden_references"] == 1


def test_a_check_entry_without_sql_text_is_hidden(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A tampered entry whose `sql` is not a string shows no SQL and counts as hidden."""
    repo = _repo(session)
    version = _deployed(session, principal, repo, "a" * 40, make_asset("silver.clean"))
    _with_check(session, version, _SECRET_CHECK, ["silver.clean"])
    entry = {**version.manifest_json["checks"][0], "sql": ["SELECT 1"]}
    version.manifest_json = {**version.manifest_json, "checks": [entry]}
    session.commit()

    view = DeployService(session, AllowAllAuthorization()).job_version(principal, "dev", version.id)

    [check] = view.manifest["checks"]
    assert "sql" not in check and "references" not in check
    assert check["hidden_references"] == 1


@pytest.mark.parametrize(
    ("checks", "shown"),
    [({"name": "x"}, []), (["not an entry", 7], [])],
    ids=["not-a-list", "entries-not-mappings"],
)
def test_damaged_checks_of_a_stored_manifest_show_as_nothing(
    session: Session, env: None, principal: PrincipalRef, checks: object, shown: list[object]
) -> None:
    """A stored manifest is untrusted: a damaged `checks` value passes nothing through.

    Parameters
    ----------
    session
        Database session.
    env
        The environment.
    principal
        The reader.
    checks
        The damaged value.
    shown
        What the view shows.
    """
    repo = _repo(session)
    version = _deployed(session, principal, repo, "a" * 40, make_asset("silver.clean"))
    version.manifest_json = {**version.manifest_json, "checks": checks}
    session.commit()

    view = DeployService(session, AllowAllAuthorization()).job_version(principal, "dev", version.id)

    assert view.manifest["checks"] == shown


def _job(
    cron: str | None = "0 3 * * *",
    tz: str = "UTC",
    select: str = "silver.*",
    notify: str | None = None,
) -> JobConfig:
    """Build a job, with a schedule unless `cron` is `None`.

    Parameters
    ----------
    cron
        The cron expression, or `None` for a job that only starts by hand.
    tz
        The time zone of the schedule.
    select
        The glob the job selects.
    notify
        The secret the job notifies through, or `None` for no notification.

    Returns
    -------
    JobConfig
        The job.
    """
    schedule = ScheduleConfig(cron=cron, timezone=tz) if cron is not None else None
    hook = NotifyConfig(webhook=notify) if notify is not None else None
    return JobConfig(select=[select], schedule=schedule, notify=hook)


def _deployed_jobs(
    session: Session,
    p: PrincipalRef,
    repo: Repository,
    sha: str,
    jobs: dict[str, JobConfig],
    table: str = "silver.clean",
) -> DeployRun:
    """Submit and record a deploy whose manifest declares `jobs`.

    Parameters
    ----------
    session
        Database session.
    p
        The deployer.
    repo
        The repository.
    sha
        The commit.
    jobs
        The jobs of the manifest, by name.
    table
        The one asset of the manifest; a table has one producing repository.

    Returns
    -------
    DeployRun
        The finished run.
    """
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(p, "dev", repository=repo.name, kind=DeployKind.DEPLOY, ref="main")
    run = _claimed(session, session.get(DeployRun, view.id))
    run.resolved_sha = sha
    current = service.current_job_version(run.environment_id, repo.id)
    asset = make_asset(table, ("bronze.events",))
    manifest = build_manifest([asset], build_graph([asset]), ResolvedProjectConfig(jobs=jobs))
    service.record_deploy(
        p,
        run,
        manifest=manifest,
        code_uri=f"file:///dev/_artifacts/code/{repo.name}/{sha}.tar.gz",
        code_sha256="0" * 64,
        diff=DeployDiff(),
        expected_job_version_id=current.id if current else None,
        worker_id="w1",
    )
    return run


def _schedules(session: Session) -> dict[str, JobSchedule]:
    """Read the job schedule rows, by job name.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    dict
        Job name to row.
    """
    session.expire_all()
    return {row.job: row for row in session.scalars(select(JobSchedule))}


def _bind(session: Session, principal: PrincipalRef) -> None:
    """Bind a run principal to every deployment, as ``set-run-as`` would.

    Parameters
    ----------
    session
        Database session; commits.
    principal
        The principal to bind.
    """
    session.execute(update(EnvironmentDeployment).values(run_as_principal_id=principal.id))
    session.commit()


def test_a_first_deploy_creates_the_schedule_rows(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A scheduled job fires next after the deploy time; a job without a schedule has none."""
    repo = _repo(session)
    run = _deployed_jobs(
        session, principal, repo, "a" * 40, {"daily": _job(), "manual": _job(cron=None)}
    )
    rows = _schedules(session)
    assert set(rows) == {"daily", "manual"}
    assert run.finished_at is not None
    assert rows["daily"].next_fire_at == next_fire_after("0 3 * * *", "UTC", run.finished_at)
    assert rows["manual"].next_fire_at is None
    assert not rows["daily"].paused and rows["daily"].pending_start is None


def test_a_changed_cron_or_zone_recomputes_and_an_unchanged_job_keeps_its_state(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """Only a changed schedule moves `next_fire_at`; pause and the waiting start survive."""
    repo = _repo(session)
    _deployed_jobs(
        session, principal, repo, "a" * 40, {"keep": _job(), "cron": _job(), "zone": _job()}
    )
    rows = _schedules(session)
    marker = datetime(2031, 1, 1, tzinfo=UTC)
    waiting = {"trigger": "manual", "merged": 1}
    keep = rows["keep"]
    keep.next_fire_at, keep.paused, keep.pending_start = marker, True, waiting
    session.commit()

    run = _deployed_jobs(
        session,
        principal,
        repo,
        "b" * 40,
        {"keep": _job(), "cron": _job("0 4 * * *"), "zone": _job(tz="Europe/Berlin")},
    )
    rows = _schedules(session)
    assert (rows["keep"].next_fire_at, rows["keep"].paused) == (marker, True)
    assert rows["keep"].pending_start == waiting
    assert run.finished_at is not None
    assert rows["cron"].next_fire_at == next_fire_after("0 4 * * *", "UTC", run.finished_at)
    expected = next_fire_after("0 3 * * *", "Europe/Berlin", run.finished_at)
    assert rows["zone"].next_fire_at == expected


def test_a_schedule_added_or_removed_sets_or_clears_the_fire_time(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A job that gains a schedule gets a time; one that loses it gets null."""
    repo = _repo(session)
    _deployed_jobs(session, principal, repo, "a" * 40, {"gain": _job(cron=None), "lose": _job()})
    _deployed_jobs(session, principal, repo, "b" * 40, {"gain": _job(), "lose": _job(cron=None)})
    rows = _schedules(session)
    assert rows["gain"].next_fire_at is not None
    assert rows["lose"].next_fire_at is None


def test_a_removed_job_deletes_its_row_and_warns_when_a_start_waited(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The waiting start goes with the row, and the deploy says so."""
    repo = _repo(session)
    jobs = {"gone": _job(), "idle": _job(), "kept": _job()}
    _deployed_jobs(session, principal, repo, "a" * 40, jobs)
    _schedules(session)["gone"].pending_start = {"trigger": "manual", "merged": 1}
    session.commit()

    run = _deployed_jobs(session, principal, repo, "b" * 40, {"kept": _job()})

    assert set(_schedules(session)) == {"kept"}
    view = DeployService(session, AllowAllAuthorization()).get(principal, "dev", run.id)
    dropped = [w for w in view.warnings if "waiting" in w]
    assert len(dropped) == 1 and "`gone`" in dropped[0] and "`idle`" not in dropped[0]


def test_a_scheduled_job_without_a_run_principal_warns(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The warning names the job, the environment and the command that fixes it."""
    repo = _repo(session)
    jobs = {"daily": _job(), "manual": _job(cron=None)}
    run = _deployed_jobs(session, principal, repo, "a" * 40, jobs)
    service = DeployService(session, AllowAllAuthorization())
    assert service.get(principal, "dev", run.id).warnings == [
        "job `daily` has a schedule but no run principal is bound in `dev`; it does not run "
        "until an admin binds one with `lakana deployments set-run-as`"
    ]
    _bind(session, principal)
    run = _deployed_jobs(session, principal, repo, "b" * 40, {"daily": _job()})
    assert service.get(principal, "dev", run.id).warnings == []


def _create_command(name: str) -> str:
    """Give the command a warning shows for creating a webhook secret of `dev`.

    Parameters
    ----------
    name
        The secret's name.

    Returns
    -------
    str
        The command, with the JSON object the secret holds.
    """
    return (
        f'printf \'{{"url": "https://hooks.example.com/..."}}\' | lakana secret create {name} '
        '--env dev --values-stdin (add "signing_key" to the object to sign the json format)'
    )


def _secret(
    session: Session,
    principal: PrincipalRef,
    name: str,
    *,
    revoked: bool = False,
    use: tuple[SecretUseKind, uuid.UUID | None] | None = None,
) -> None:
    """Store a secret row of the `dev` environment with placeholder ciphertext.

    The deploy only looks the name up, so the bytes are never decrypted.

    Parameters
    ----------
    session
        Database session; commits.
    principal
        The creator.
    name
        The secret's name.
    revoked
        Mark the secret revoked.
    use
        The secret's use and target; `None` for unbound.
    """
    environment = session.execute(select(Environment).where(Environment.name == "dev")).scalar_one()
    now = datetime.now(UTC)
    session.add(
        Secret(
            id=uuid.uuid4(),
            environment_id=environment.id,
            name=name,
            ciphertext=b"x",
            nonce=b"x" * 12,
            wrapped_data_key=b"x",
            format_version=2,
            key_id="env",
            created_by=principal.id,
            created_at=now,
            revoked_at=now if revoked else None,
            use_kind=use[0] if use else None,
            use_target_id=use[1] if use else None,
        )
    )
    session.commit()


def test_a_job_that_notifies_through_a_secret_bound_to_an_index_warns_at_deploy(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A secret that serves an index or a repository never serves a webhook; the deploy says so."""
    repo = _repo(session)
    _secret(session, principal, "idx", use=(SecretUseKind.PACKAGE_INDEX, uuid.uuid4()))
    _secret(session, principal, "own", use=(SecretUseKind.ENVIRONMENT, None))
    jobs = {
        "a": _job(cron=None, notify="idx"),
        "b": _job(cron=None, notify="own"),
    }
    run = _deployed_jobs(session, principal, repo, "a" * 40, jobs)
    warnings = (
        DeployService(session, AllowAllAuthorization()).get(principal, "dev", run.id).warnings
    )
    (warning,) = warnings
    assert warning.startswith(
        "job `a` notifies through secret `idx`, which is bound to a package index that was "
        "removed, not to its environment, so Lakana does not send it to a webhook; it does not "
        "notify until you give the job a secret of its own"
    )
    assert warning.endswith("lakana secret bind idx --env dev --own-env")


def test_a_job_that_notifies_through_a_missing_secret_warns_at_deploy(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The deploy goes through; the warning names the job, secret, environment and fix."""
    repo = _repo(session)
    _secret(session, principal, "present")
    _secret(session, principal, "revoked-one", revoked=True)
    jobs = {
        "ok": _job(cron=None, notify="present"),
        "missing": _job(cron=None, notify="team-alerts"),
        "gone": _job(cron=None, notify="revoked-one"),
        "silent": _job(cron=None),
    }
    run = _deployed_jobs(session, principal, repo, "a" * 40, jobs)
    warnings = (
        DeployService(session, AllowAllAuthorization()).get(principal, "dev", run.id).warnings
    )
    assert warnings == [
        "job `gone` notifies through secret `revoked-one`, which is revoked in `dev` (a revoked "
        "name cannot be created or rotated again); it does not notify until you create a secret "
        "under another name and put that name in its `notify.webhook`, then deploy: "
        + _create_command("NEW-NAME"),
        "job `missing` notifies through secret `team-alerts`, which does not exist in `dev`; "
        "it does not notify until you create it: " + _create_command("team-alerts"),
    ]


def test_a_dry_run_says_a_job_would_not_notify(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    repo = _repo(session)
    _deployed_jobs(session, principal, repo, "a" * 40, {"daily": _job(cron=None)})
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(
        principal, "dev", repository="analytics", kind=DeployKind.DRY_RUN, ref="main"
    )
    run = _claimed(session, session.get(DeployRun, view.id))
    run.resolved_sha = "c" * 40
    asset = make_asset("silver.clean", ("bronze.events",))
    manifest = build_manifest(
        [asset],
        build_graph([asset]),
        ResolvedProjectConfig(jobs={"daily": _job(cron=None, notify="team-alerts")}),
    )
    service.record_dry_run(principal, run, diff=DeployDiff(), worker_id="w1", manifest=manifest)
    (warning,) = service.get(principal, "dev", run.id).warnings
    assert "`daily`" in warning and "`team-alerts`" in warning
    assert "would not notify" in warning
    _secret(session, principal, "team-alerts")
    run = _claimed(
        session,
        session.get(
            DeployRun,
            service.submit(
                principal, "dev", repository="analytics", kind=DeployKind.DRY_RUN, ref="main"
            ).id,
        ),
    )
    run.resolved_sha = "d" * 40
    service.record_dry_run(principal, run, diff=DeployDiff(), worker_id="w1", manifest=manifest)
    assert service.get(principal, "dev", run.id).warnings == []


def test_an_environment_override_without_a_schedule_gives_no_fire_time_there(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The manifest is resolved per environment, so ``schedule: null`` leaves `dev` unscheduled."""
    repo = _repo(session)
    config = ProjectConfig.model_validate(
        {
            "jobs": {"daily": {"select": ["silver.*"], "schedule": {"cron": "0 3 * * *"}}},
            "environments": {"dev": {"jobs": {"daily": {"schedule": None}}}},
        }
    ).resolved_for("dev")
    asset = make_asset("silver.clean", ("bronze.events",))
    manifest = build_manifest([asset], build_graph([asset]), config)
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(principal, "dev", repository=repo.name, kind=DeployKind.DEPLOY, ref="m")
    run = _claimed(session, session.get(DeployRun, view.id))
    run.resolved_sha = "a" * 40
    service.record_deploy(
        principal,
        run,
        manifest=manifest,
        code_uri="file:///x",
        code_sha256="0" * 64,
        diff=DeployDiff(),
        expected_job_version_id=None,
        worker_id="w1",
    )
    assert _schedules(session)["daily"].next_fire_at is None
    assert service.get(principal, "dev", run.id).warnings == []


def test_a_dry_run_changes_no_schedule_rows(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A dry run moves no pointer, so it touches no `job_schedules` row."""
    repo = _repo(session)
    _deployed_jobs(session, principal, repo, "a" * 40, {"daily": _job()})
    before = {k: v.next_fire_at for k, v in _schedules(session).items()}
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(
        principal, "dev", repository="analytics", kind=DeployKind.DRY_RUN, ref="main"
    )
    run = _claimed(session, session.get(DeployRun, view.id))
    run.resolved_sha = "c" * 40
    service.record_dry_run(principal, run, diff=DeployDiff(jobs_removed=("daily",)), worker_id="w1")
    assert {k: v.next_fire_at for k, v in _schedules(session).items()} == before


def test_a_rollback_syncs_the_schedules_like_a_deploy_of_the_earlier_version(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """Rolling back restores the earlier jobs and computes their times from the rollback."""
    repo = _repo(session)
    _deployed_jobs(session, principal, repo, "a" * 40, {"old": _job("0 5 * * *")})
    _deployed_jobs(session, principal, repo, "b" * 40, {"new": _job()})
    assert set(_schedules(session)) == {"new"}
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(
        principal, "dev", repository="analytics", kind=DeployKind.ROLLBACK, to_previous=True
    )
    run = _claimed(session, session.get(DeployRun, view.id))
    service.execute_rollback(principal, run, worker_id="w1")
    rows = _schedules(session)
    assert set(rows) == {"old"}
    assert run.finished_at is not None
    assert rows["old"].next_fire_at == next_fire_after("0 5 * * *", "UTC", run.finished_at)
    assert any("`old`" in w for w in service.get(principal, "dev", run.id).warnings)


def test_undeploy_deletes_the_schedules_and_their_fires(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The pointer row has no foreign key to the schedules, so undeploy removes them itself."""
    repo = _repo(session)
    other = _repo(session, "other")
    _deployed_jobs(session, principal, repo, "a" * 40, {"daily": _job()})
    _deployed_jobs(session, principal, other, "a" * 40, {"daily": _job()}, "silver.other")
    row = next(r for r in session.scalars(select(JobSchedule)) if r.repository_id == repo.id)
    session.add(JobFire(id=uuid.uuid4(), job_schedule_id=row.id, scheduled_for=datetime.now(UTC)))
    session.commit()

    DeployService(session, AllowAllAuthorization()).undeploy(principal, "dev", "analytics")

    remaining = session.scalars(select(JobSchedule)).all()
    assert [r.repository_id for r in remaining] == [other.id]
    assert session.query(JobFire).count() == 0


def test_a_dry_run_says_what_the_deploy_would_drop_and_would_not_run(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The dry run shows both warnings in the conditional, and still writes no schedule row."""
    repo = _repo(session)
    _deployed_jobs(session, principal, repo, "a" * 40, {"gone": _job(), "daily": _job()})
    _schedules(session)["gone"].pending_start = {"trigger": "manual", "merged": 1}
    session.commit()
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(
        principal, "dev", repository="analytics", kind=DeployKind.DRY_RUN, ref="main"
    )
    run = _claimed(session, session.get(DeployRun, view.id))
    run.resolved_sha = "c" * 40
    asset = make_asset("silver.clean", ("bronze.events",))
    manifest = build_manifest(
        [asset], build_graph([asset]), ResolvedProjectConfig(jobs={"daily": _job()})
    )
    service.record_dry_run(principal, run, diff=DeployDiff(), worker_id="w1", manifest=manifest)
    warnings = service.get(principal, "dev", run.id).warnings
    assert len(warnings) == 2
    assert "`gone`" in warnings[0] and "would be dropped" in warnings[0]
    assert "`daily`" in warnings[1] and "would not run" in warnings[1]
    assert set(_schedules(session)) == {"gone", "daily"}


def test_a_rollback_that_removes_a_job_with_a_waiting_start_warns(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The rollback drops the job's row; the waiting start is named in the rollback's warnings."""
    repo = _repo(session)
    _deployed_jobs(session, principal, repo, "a" * 40, {"old": _job()})
    _deployed_jobs(session, principal, repo, "b" * 40, {"new": _job()})
    _schedules(session)["new"].pending_start = {"trigger": "manual", "merged": 1}
    session.commit()
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(
        principal, "dev", repository="analytics", kind=DeployKind.ROLLBACK, to_previous=True
    )
    run = _claimed(session, session.get(DeployRun, view.id))
    service.execute_rollback(principal, run, worker_id="w1")
    warnings = service.get(principal, "dev", run.id).warnings
    assert any("`new`" in w and "dropped" in w and "deploy again" in w for w in warnings)


def test_the_sync_reads_the_schedule_rows_for_update(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """On PostgreSQL the rows are locked, so a scheduler tick's waiting start cannot be lost."""
    from sqlalchemy import event

    repo = _repo(session)
    statements: list[str] = []

    def record(state: object) -> None:
        statement = getattr(state, "statement", None)
        text = str(statement.compile(dialect=postgresql.dialect())) if statement is not None else ""
        if "FROM job_schedules" in text:
            statements.append(text)

    event.listen(session, "do_orm_execute", record)
    _deployed_jobs(session, principal, repo, "a" * 40, {"daily": _job()})
    event.remove(session, "do_orm_execute", record)
    assert statements
    assert statements[0].rstrip().endswith("FOR UPDATE")


_DROP_FIELDS = {
    "repository",
    "job",
    "trigger",
    "requested_by",
    "scheduled_for",
    "missed",
    "merged",
    "reason",
}


def _dropped_events(session: Session) -> list[AuditEvent]:
    """Read the `JOB_START_DROPPED` events, oldest first.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of AuditEvent
        The events.
    """
    session.expire_all()
    return list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.event_type == AuditEventType.JOB_START_DROPPED)
            .order_by(AuditEvent.occurred_at)
        )
    )


def test_a_deploy_that_removes_a_job_with_a_waiting_start_audits_the_drop(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The waiting event gets its outcome: dropped, `job_removed`, no actor, same fields."""
    repo = _repo(session)
    _deployed_jobs(
        session, principal, repo, "a" * 40, {"gone": _job(), "idle": _job(), "k": _job()}
    )
    _schedules(session)["gone"].pending_start = {
        "trigger": "schedule",
        "requested_at": "2026-10-15T03:00:01+00:00",
        "requested_by": None,
        "scheduled_for": "2026-10-15T03:00:00+00:00",
        "missed": 2,
        "merged": 3,
    }
    session.commit()
    _deployed_jobs(session, principal, repo, "b" * 40, {"k": _job()})
    (event,) = _dropped_events(session)
    assert event.principal_id is None
    assert set(event.detail) >= _DROP_FIELDS
    assert (event.detail["job"], event.detail["reason"]) == ("gone", "job_removed")
    assert (event.detail["scheduled_for"], event.detail["missed"], event.detail["merged"]) == (
        "2026-10-15T03:00:00+00:00",
        2,
        3,
    )


def test_an_undeploy_that_drops_a_waiting_start_audits_it(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """Undeploy removes the start without a trace today; now it says `undeployed`."""
    repo = _repo(session)
    _deployed_jobs(session, principal, repo, "a" * 40, {"daily": _job(), "idle": _job()})
    _schedules(session)["daily"].pending_start = {"trigger": "manual", "merged": 1}
    session.commit()
    DeployService(session, AllowAllAuthorization()).undeploy(principal, "dev", "analytics")
    (event,) = _dropped_events(session)
    assert event.principal_id is None
    assert set(event.detail) >= _DROP_FIELDS
    assert (event.detail["job"], event.detail["reason"]) == ("daily", "undeployed")
    assert (event.detail["scheduled_for"], event.detail["missed"]) == (None, 0)


def test_a_missing_time_zone_in_a_manifest_means_utc_not_the_text_none(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A stored `timezone: null` reads as UTC, as the scheduler reads it."""
    repo = _repo(session)
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(
        principal, "dev", repository="analytics", kind=DeployKind.DEPLOY, ref="main"
    )
    run = _claimed(session, session.get(DeployRun, view.id))
    run.resolved_sha = "a" * 40
    asset = make_asset("silver.clean", ("bronze.events",))
    manifest = build_manifest(
        [asset], build_graph([asset]), ResolvedProjectConfig(jobs={"daily": _job()})
    )
    manifest["jobs"]["daily"]["schedule"]["timezone"] = None
    service.record_deploy(
        principal,
        run,
        manifest=manifest,
        code_uri=f"file:///dev/_artifacts/code/{repo.name}/{'a' * 40}.tar.gz",
        code_sha256="0" * 64,
        diff=DeployDiff(),
        expected_job_version_id=None,
        worker_id="w1",
    )
    next_at = _schedules(session)["daily"].next_fire_at
    assert next_at is not None and (next_at.hour, next_at.minute) == (3, 0)


_CONNECTION_KEYS = EnvKeyProvider(base64.b64encode(b"5" * 32).decode())


def _connection(session: Session, p: PrincipalRef, name: str) -> None:
    """Create a `directory` connection in `dev`.

    Parameters
    ----------
    session
        Database session.
    p
        The administrator.
    name
        The connection's name.
    """
    ConnectionService(session, AllowAllAuthorization(), _CONNECTION_KEYS).create(
        p,
        "dev",
        name=name,
        kind=ConnectionKind.DIRECTORY,
        address={"root": "shared", "path": name},
        settings={},
    )


def _customers(*connections: str) -> ParsedAsset:
    """Build the Python asset `bronze.customers` declaring `connections`.

    Parameters
    ----------
    *connections
        The connection names it declares.

    Returns
    -------
    ParsedAsset
        The asset.
    """
    return dataclasses.replace(_python("bronze.customers", {}), connections=connections)


def _unknown(asset: str, name: str) -> str:
    """Spell the deploy's refusal of a connection the environment does not have.

    Parameters
    ----------
    asset
        The asset's name.
    name
        The connection's name.

    Returns
    -------
    str
        The fixed text, for `dev`.
    """
    return (
        f"Asset `{asset}` declares connection `{name}`, which environment `dev` does not "
        f"have. Create it: `lakana connection create {name} --env dev --kind <kind> …` "
        "(see `lakana connection create --help`)."
    )


def test_record_deploy_refuses_a_connection_the_environment_does_not_have(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A connection deleted after validation still stops the record; nothing is written."""
    repo = _repo(session)
    _connection(session, principal, "files")
    with pytest.raises(ProjectValidationError) as info:
        _deployed(session, principal, repo, "a" * 40, _customers("crm-db", "files", "erp"))
    assert [issue.render() for issue in info.value.issues] == [
        f"assets/bronze/customers.py: {_unknown('bronze.customers', 'crm-db')}",
        f"assets/bronze/customers.py: {_unknown('bronze.customers', 'erp')}",
    ]
    assert session.query(JobVersion).count() == 0
    assert session.execute(select(EnvironmentDeployment)).first() is None
    run = session.execute(select(DeployRun)).scalar_one()
    assert run.status is DeployStatus.RUNNING  # the caller fails the run


def test_record_deploy_accepts_the_connections_the_environment_has(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    repo = _repo(session)
    _connection(session, principal, "crm-db")
    version = _deployed(session, principal, repo, "a" * 40, _customers("crm-db"))
    assert manifest_assets(version.manifest_json)["bronze.customers"]["connections"] == ["crm-db"]


def test_record_deploy_reads_the_connections_under_a_share_lock(
    session: Session, env: None, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check runs in the recording transaction, locking the rows it found."""
    repo = _repo(session)
    _connection(session, principal, "crm-db")
    calls: list[tuple[list[str], bool]] = []
    real = deploys_module.missing_connections

    def spy(
        session: Session, environment_id: uuid.UUID, names: list[str], *, lock: bool
    ) -> list[str]:
        calls.append((list(names), lock))
        return real(session, environment_id, names, lock=lock)

    monkeypatch.setattr(deploys_module, "missing_connections", spy)
    _deployed(session, principal, repo, "a" * 40, _customers("crm-db"))
    assert calls == [(["crm-db"], True)]


def test_a_rollback_to_a_version_whose_connection_is_gone_is_refused(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """Any change of the deployed version runs the check, a rollback too."""
    repo = _repo(session)
    _connection(session, principal, "crm-db")
    first = _deployed(session, principal, repo, "a" * 40, _customers("crm-db"))
    second = _deployed(session, principal, repo, "b" * 40, _customers())
    ConnectionService(session, AllowAllAuthorization(), _CONNECTION_KEYS).delete(
        principal, "dev", "crm-db"
    )
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(
        principal, "dev", repository="analytics", kind=DeployKind.ROLLBACK, to_previous=True
    )
    assert view.rollback_to_job_version_id == first.id
    run = _claimed(session, session.get(DeployRun, view.id))
    with pytest.raises(ProjectValidationError) as info:
        service.execute_rollback(principal, run, worker_id="w1")
    assert [issue.message for issue in info.value.issues] == [
        _unknown("bronze.customers", "crm-db")
    ]
    pointer = session.execute(select(EnvironmentDeployment)).scalar_one()
    assert pointer.current_job_version_id == second.id


def test_a_rollback_to_a_version_whose_connections_exist_moves_the_pointer(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    repo = _repo(session)
    _connection(session, principal, "crm-db")
    first = _deployed(session, principal, repo, "a" * 40, _customers("crm-db"))
    _deployed(session, principal, repo, "b" * 40, _customers())
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(
        principal, "dev", repository="analytics", kind=DeployKind.ROLLBACK, to_previous=True
    )
    run = _claimed(session, session.get(DeployRun, view.id))
    assert service.execute_rollback(principal, run, worker_id="w1").id == first.id


@pytest.mark.parametrize("gone", [False, True], ids=["still-there", "deleted-meanwhile"])
def test_the_connection_check_is_taken_again_after_a_unique_violation_rolls_it_back(
    session: Session,
    env: None,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
    gone: bool,
) -> None:
    """The rollback after a lost insert releases the share locks; the retry checks again.

    With `gone`, the connection disappears between the lost insert and the retry, and the
    retry's answer is the one that counts: the record is refused.
    """
    repo = _repo(session)
    _connection(session, principal, "crm-db")
    winner = _deployed(session, principal, repo, "a" * 40, _customers("crm-db"))
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(principal, "dev", repository="analytics", kind=DeployKind.DEPLOY)
    run = _claimed(session, session.get(DeployRun, view.id))
    run.resolved_sha = "a" * 40
    real = service._existing_job_version
    seen: list[str] = []
    checks: list[bool] = []
    real_missing = deploys_module.missing_connections

    def blind_first(
        environment_id: uuid.UUID, repository_id: uuid.UUID, sha: str
    ) -> JobVersion | None:
        seen.append(sha)
        return None if len(seen) == 1 else real(environment_id, repository_id, sha)

    def spy(
        session: Session, environment_id: uuid.UUID, names: list[str], *, lock: bool
    ) -> list[str]:
        checks.append(lock)
        if gone and len(checks) == 2:
            session.execute(delete(Connection).where(Connection.name == "crm-db"))
        return real_missing(session, environment_id, names, lock=lock)

    monkeypatch.setattr(service, "_existing_job_version", blind_first)
    monkeypatch.setattr(deploys_module, "missing_connections", spy)

    def record() -> JobVersion:
        return service.record_deploy(
            principal,
            run,
            manifest=_manifest(_customers("crm-db")),
            code_uri="u",
            code_sha256="0" * 64,
            diff=DeployDiff(),
            expected_job_version_id=winner.id,
            worker_id="w1",
        )

    if not gone:
        assert record().id == winner.id
        assert checks == [True, True]
        return
    with pytest.raises(ProjectValidationError) as info:
        record()
    assert checks == [True, True]
    assert [issue.message for issue in info.value.issues] == [
        _unknown("bronze.customers", "crm-db")
    ]
    refused = session.get(DeployRun, view.id)
    assert refused is not None
    assert refused.status is DeployStatus.RUNNING


_UNREADABLE = (
    "Asset `bronze.customers`: the connections this project version declares cannot be read, "
    "so they cannot be checked against environment `dev`. Deploy the repository again to "
    "record a new version."
)


def _malformed_manifest() -> dict:
    """Build a manifest whose `bronze.customers` names its connection as a bare string.

    Returns
    -------
    dict
        The manifest.
    """
    manifest = _manifest(_customers("crm-db"))
    manifest["assets"][0]["connections"] = "crm-db"
    return manifest


def test_record_deploy_refuses_connections_it_cannot_read(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A malformed entry never skips the check, even when the named connection exists."""
    repo = _repo(session)
    _connection(session, principal, "crm-db")
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(principal, "dev", repository=repo.name, kind=DeployKind.DEPLOY)
    run = _claimed(session, session.get(DeployRun, view.id))
    run.resolved_sha = "a" * 40
    with pytest.raises(ProjectValidationError) as info:
        service.record_deploy(
            principal,
            run,
            manifest=_malformed_manifest(),
            code_uri="u",
            code_sha256="0" * 64,
            diff=DeployDiff(),
            expected_job_version_id=None,
            worker_id="w1",
        )
    assert [issue.render() for issue in info.value.issues] == [
        f"assets/bronze/customers.py: {_UNREADABLE}"
    ]
    assert session.query(JobVersion).count() == 0


def test_a_rollback_to_a_version_with_unreadable_connections_is_refused(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    repo = _repo(session)
    _connection(session, principal, "crm-db")
    first = _deployed(session, principal, repo, "a" * 40, _customers("crm-db"))
    second = _deployed(session, principal, repo, "b" * 40, _customers())
    first.manifest_json = _malformed_manifest()
    session.commit()
    service = DeployService(session, AllowAllAuthorization())
    view = service.submit(
        principal, "dev", repository="analytics", kind=DeployKind.ROLLBACK, to_previous=True
    )
    run = _claimed(session, session.get(DeployRun, view.id))
    with pytest.raises(ProjectValidationError) as info:
        service.execute_rollback(principal, run, worker_id="w1")
    assert [issue.message for issue in info.value.issues] == [_UNREADABLE]
    pointer = session.execute(select(EnvironmentDeployment)).scalar_one()
    assert pointer.current_job_version_id == second.id


def test_a_reference_may_point_at_a_table_another_repository_builds(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The key comes from the other repository's current manifest, once the table exists."""
    other = _repo(session, "other")
    dim = make_asset(
        "dim.store",
        ("bronze.events",),
        columns=("store_id",),
        metadata=AssetMetadata(materialize="table", kind="dimension", grain="g", key=("store_id",)),
    )
    _deployed(session, principal, other, "a" * 40, dim)
    ensure_table(session, "dev", "dim", "store")
    repo = _repo(session)
    fact = make_asset(
        "silver.f",
        ("bronze.events",),
        columns=("store_id",),
        metadata=AssetMetadata(materialize="table", references={"store_id": "dim.store"}),
    )
    resolved, issues = DeployService(session, AllowAllAuthorization()).resolve_model_references(
        principal, "dev", repo.id, [fact]
    )
    assert issues == []
    assert resolved["silver.f"] == (ResolvedReference("store_id", "dim.store", "store_id"),)


def test_a_catalog_table_no_project_builds_needs_the_long_form(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """`bronze.events` exists in `dev` (the `env` fixture) and declares no key."""
    repo = _repo(session)
    fact = make_asset(
        "silver.f",
        ("bronze.events",),
        columns=("event_id",),
        metadata=AssetMetadata(materialize="table", references={"event_id": "bronze.events"}),
    )
    _, issues = DeployService(session, AllowAllAuthorization()).resolve_model_references(
        principal, "dev", repo.id, [fact]
    )
    assert "`bronze.events` declares no key" in issues[0].message


def test_a_target_the_deployer_may_not_read_reveals_nothing(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """Built by another repository and present, but unreadable: the combined text, nothing else."""
    other = _repo(session, "other")
    dim = make_asset(
        "dim.store",
        ("bronze.events",),
        columns=("store_id", "region"),
        metadata=AssetMetadata(
            materialize="table", kind="dimension", grain="g", key=("store_id", "region")
        ),
    )
    _deployed(session, principal, other, "a" * 40, dim)
    ensure_table(session, "dev", "dim", "store")
    reader = _read_environment_only(session, principal, "bronze.events")
    repo = _repo(session)
    fact = make_asset(
        "silver.f",
        ("bronze.events",),
        columns=("store_id",),
        metadata=AssetMetadata(materialize="table", references={"store_id": "dim.store"}),
    )
    _, issues = DeployService(session, GrantAuthorization(session)).resolve_model_references(
        reader, "dev", repo.id, [fact]
    )
    assert [i.message for i in issues] == [
        "Reference target `dim.store` does not exist in environment `dev`, or you may not read "
        "it; build it first, or ask for READ_TABLE on it."
    ]


def _bind_run_as(session: Session, admin: PrincipalRef, repo: Repository, *tables: str) -> str:
    """Bind a run principal to `repo` in `dev` that may read only `tables`.

    Parameters
    ----------
    session
        Database session.
    admin
        Grants the access and deploys the repository once, which makes its deployment row.
    repo
        The repository.
    *tables
        Dotted table names the run principal may read.

    Returns
    -------
    str
        The run principal's name.
    """
    svc = _principal(session, "svc-etl")
    grants = GrantService(session, AllowAllAuthorization())
    for dotted in tables:
        namespace, table = dotted.split(".")
        grants.grant(
            admin,
            principal="svc-etl",
            action=Action.READ_TABLE,
            kind=SecurableKind.TABLE,
            environment="dev",
            namespace=namespace,
            table=table,
        )
    _deployed(session, admin, repo, "b" * 40, make_asset("silver.seed"))
    session.execute(
        update(EnvironmentDeployment)
        .where(EnvironmentDeployment.repository_id == repo.id)
        .values(run_as_principal_id=svc.id)
    )
    session.commit()
    return svc.name


def test_a_target_the_bound_run_principal_may_not_read_is_refused_by_name(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The deployer may read the target, so naming it reveals nothing; the runs could not."""
    other = _repo(session, "other")
    dim = make_asset(
        "dim.store",
        ("bronze.events",),
        columns=("store_id",),
        metadata=AssetMetadata(materialize="table", kind="dimension", grain="g", key=("store_id",)),
    )
    _deployed(session, principal, other, "a" * 40, dim)
    ensure_table(session, "dev", "dim", "store")
    repo = _repo(session)
    run_as = _bind_run_as(session, principal, repo, "bronze.events")
    reader = _read_environment_only(session, principal, "bronze.events", "dim.store")
    fact = make_asset(
        "silver.f",
        ("bronze.events",),
        columns=("store_id",),
        metadata=AssetMetadata(materialize="table", references={"store_id": "dim.store"}),
    )
    resolved, issues = DeployService(session, GrantAuthorization(session)).resolve_model_references(
        reader, "dev", repo.id, [fact]
    )
    assert [i.message for i in issues] == [
        f"Reference target `dim.store` cannot be read by `{run_as}`, the run principal bound to "
        "this repository in `dev`, so its runs could not check it; grant it READ_TABLE on "
        "`dim.store`, or bind another run principal with `lakana deployments set-run-as`."
    ]
    assert resolved == {}


def test_a_run_principal_that_may_read_the_target_lets_the_reference_through(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    repo = _repo(session)
    _bind_run_as(session, principal, repo, "bronze.events")
    reader = _read_environment_only(session, principal, "bronze.events")
    fact = make_asset(
        "silver.f",
        ("bronze.events",),
        columns=("event_id",),
        metadata=AssetMetadata(materialize="table", references={"event_id": "bronze.events.id"}),
    )
    resolved, issues = DeployService(session, GrantAuthorization(session)).resolve_model_references(
        reader, "dev", repo.id, [fact]
    )
    assert issues == []
    assert resolved["silver.f"] == (ResolvedReference("event_id", "bronze.events", "id"),)


def test_a_project_target_or_a_deployer_who_is_the_run_principal_needs_no_second_check(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The runs build a project's own target; a deployer bound as run principal was checked."""
    repo = _repo(session)
    _bind_run_as(session, principal, repo)
    dim = make_asset(
        "silver.dim",
        ("bronze.events",),
        columns=("id",),
        metadata=AssetMetadata(materialize="table", kind="dimension", grain="g", key=("id",)),
    )
    fact = make_asset(
        "silver.f",
        ("bronze.events",),
        columns=("id",),
        metadata=AssetMetadata(materialize="table", references={"id": "silver.dim"}),
    )
    service = DeployService(session, GrantAuthorization(session))
    reader = _read_environment_only(session, principal, "bronze.events")
    resolved, issues = service.resolve_model_references(reader, "dev", repo.id, [dim, fact])
    assert issues == []
    assert resolved["silver.f"] == (ResolvedReference("id", "silver.dim", "id"),)
    svc = session.execute(select(Principal).where(Principal.name == "svc-etl")).scalar_one()
    as_run_principal = PrincipalRef(id=svc.id, name=svc.name, type=svc.type)
    fact_on_events = make_asset(
        "silver.g",
        ("bronze.events",),
        columns=("id",),
        metadata=AssetMetadata(materialize="table", references={"id": "bronze.events.id"}),
    )
    _, issues = service.resolve_model_references(as_run_principal, "dev", repo.id, [fact_on_events])
    assert [i.message for i in issues] == [
        "Reference target `bronze.events.id` does not exist in environment `dev`, or you may not "
        "read it; build it first, or ask for READ_TABLE on it."
    ]


def test_a_project_version_hides_model_references_the_reader_may_not_read(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A reference target names a table, as the SQL's references do; unreadable ones are counted."""
    repo = _repo(session)
    version = _deployed(
        session,
        principal,
        repo,
        "a" * 40,
        make_asset(
            "silver.f",
            ("bronze.events",),
            metadata=AssetMetadata(materialize="table", key=("a",)),
        ),
    )
    manifest = copy.deepcopy(version.manifest_json)
    (entry,) = manifest["assets"]
    entry["model"]["references"] = [
        {"column": "a", "target": "bronze.events", "target_column": "id"},
        {"column": "b", "target": "restricted.dim_customer", "target_column": "customer_sk"},
        "damaged",
    ]
    version.manifest_json = {**manifest, "assets": [entry]}
    session.commit()
    reader = _read_environment_only(session, principal, "bronze.events")
    view = DeployService(session, GrantAuthorization(session)).job_version(
        reader, "dev", version.id
    )
    (shown_entry,) = view.manifest["assets"]
    assert shown_entry["model"]["references"] == [
        {"column": "a", "target": "bronze.events", "target_column": "id"}
    ]
    assert shown_entry["hidden_model_references"] == 2
    assert "restricted" not in str(view.manifest)
    full = DeployService(session, AllowAllAuthorization()).job_version(principal, "dev", version.id)
    assert len(full.manifest["assets"][0]["model"]["references"]) == 2
    assert full.manifest["assets"][0]["hidden_model_references"] == 1


def test_a_merge_target_deployed_before_models_reads_its_merge_key(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A version 8 entry has no `model`; its merge key still identifies a row."""
    other = _repo(session, "other")
    dim = make_asset(
        "dim.store",
        ("bronze.events",),
        columns=("store_id",),
        metadata=AssetMetadata(materialize="table", write="merge", key=("store_id",)),
    )
    version = _deployed(session, principal, other, "a" * 40, dim)
    manifest = copy.deepcopy(version.manifest_json)
    del manifest["assets"][0]["model"]
    version.manifest_json = manifest
    session.commit()
    ensure_table(session, "dev", "dim", "store")
    repo = _repo(session)
    fact = make_asset(
        "silver.f",
        ("bronze.events",),
        columns=("store_id",),
        metadata=AssetMetadata(materialize="table", references={"store_id": "dim.store"}),
    )
    resolved, issues = DeployService(session, AllowAllAuthorization()).resolve_model_references(
        principal, "dev", repo.id, [fact]
    )
    assert issues == []
    assert resolved["silver.f"] == (ResolvedReference("store_id", "dim.store", "store_id"),)
