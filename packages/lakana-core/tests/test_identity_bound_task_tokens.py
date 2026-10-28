"""Bound task tokens: minted per task attempt, valid only while it runs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from core_test_helpers import _prepared_catalog, ensure_table, python_task_run
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import lakana_core.identity.task_tokens as task_tokens_module
from lakana_core.auth import AllowAllAuthorization, BoundTask, PrincipalRef, RunContext
from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.errors import AuthenticationError, ConflictError, NotFoundError, ValidationError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.identity.task_tokens import (
    TaskTokenService,
    _resolve_bound,
    resolve_task_token_scope,
)
from lakana_core.identity.tokens import parse_token
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    JobVersion,
    Principal,
    Repository,
    Run,
    RunStatus,
    Table,
    TaskRun,
    TaskStatus,
    TaskToken,
)


@pytest.fixture
def dev(session: Session, tmp_path: Path, principal: PrincipalRef) -> None:
    """Create the environment `dev`.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The admin, who is also the run principal in these tests.
    """
    _prepared_catalog(
        session,
        tmp_path,
        AllowAllAuthorization(),
        namespaces=[NamespacePath.from_parts(["bronze"])],
        tables=[],
    )


def _service(session: Session, max_ttl: int = 86_400) -> TaskTokenService:
    """Build the service the worker builds.

    Parameters
    ----------
    session
        Database session.
    max_ttl
        The operator's cap.

    Returns
    -------
    TaskTokenService
        The service.
    """
    return TaskTokenService(session, AllowAllAuthorization(), max_ttl_seconds=max_ttl)


def _verify(session: Session, token: str) -> PrincipalRef:
    """Authenticate with a full token value.

    Parameters
    ----------
    session
        Database session.
    token
        ``lakana_task_<key_id>_<secret>``.

    Returns
    -------
    PrincipalRef
        The authenticated principal.
    """
    parsed = parse_token(token, "lakana_task")
    assert parsed is not None
    session.expire_all()
    return _service(session).verify(*parsed).principal


def _row(session: Session, key_id: str) -> TaskToken:
    """Read a token row.

    Parameters
    ----------
    session
        Database session.
    key_id
        Its key id.

    Returns
    -------
    TaskToken
        The row.
    """
    return session.execute(select(TaskToken).where(TaskToken.key_id == key_id)).scalar_one()


def test_mint_for_task_binds_the_token_to_the_attempt(
    session: Session, dev: None, principal: PrincipalRef
) -> None:
    task = python_task_run(session, principal.id, claim=3)
    before = datetime.now(UTC)
    minted = _service(session).mint_for_task(task.id, 3, ttl_seconds=130)
    after = datetime.now(UTC)
    row = _row(session, minted.key_id)
    assert (row.task_run_id, row.claim) == (task.id, 3)
    assert row.principal_id == principal.id
    assert row.environment == "dev"
    assert row.asset_scope == ["bronze.stations", "gold.scores", "silver.weather"]
    assert before + timedelta(seconds=130) <= row.expires_at <= after + timedelta(seconds=130)
    event = session.execute(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.TASK_TOKEN_MINTED)
    ).scalar_one()
    assert event.principal_id == principal.id
    assert event.detail == {
        "issuer": "worker",
        "subject": principal.name,
        "key_id": minted.key_id,
        "environment": "dev",
        "asset_scope": ["bronze.stations", "gold.scores", "silver.weather"],
        "task_run_id": str(task.id),
        "claim": 3,
    }


def test_the_lifetime_is_capped_by_the_operator_maximum(
    session: Session, dev: None, principal: PrincipalRef
) -> None:
    task = python_task_run(session, principal.id)
    before = datetime.now(UTC)
    minted = _service(session, max_ttl=60).mint_for_task(task.id, 1, ttl_seconds=3600)
    after = datetime.now(UTC)
    assert before + timedelta(seconds=60) <= minted.expires_at <= after + timedelta(seconds=60)


def test_verify_resolves_the_bound_task_from_the_manifest(
    session: Session, dev: None, principal: PrincipalRef
) -> None:
    task = python_task_run(session, principal.id)
    minted = _service(session).mint_for_task(task.id, 1, ttl_seconds=60)
    row = _row(session, minted.key_id)
    row.asset_scope = ["anything.at_all"]
    session.commit()
    scope = _verify(session, minted.token).task_scope
    assert scope is not None
    assert scope.environment == "dev"
    assert scope.bound == BoundTask(
        task_run_id=task.id,
        claim=1,
        asset="gold.scores",
        inputs=("bronze.stations", "silver.weather"),
        readable=("bronze.stations", "silver.weather"),
        repository=_repository_name(session, task),
    )


def test_an_unbound_token_has_no_bound_task(session: Session, principal: PrincipalRef) -> None:
    minted = _service(session).mint(
        principal, principal.name, environment="dev", asset_scope=["gold.x"], ttl_seconds=60
    )
    scope = _verify(session, minted.token).task_scope
    assert scope is not None and scope.bound is None


def test_a_view_input_makes_the_tables_behind_it_readable(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A view is read with the reader's grants on its base tables."""
    bronze, gold = NamespacePath.from_parts(["bronze"]), NamespacePath.from_parts(["gold"])
    catalog = _prepared_catalog(
        session,
        tmp_path,
        AllowAllAuthorization(),
        namespaces=[bronze, gold],
        tables=[TableRef("dev", bronze, "orders")],
    )
    columns = [ColumnSpec(name="id", type="long", nullable=True)]
    for view, source in (("gold.v", "bronze.orders"), ("gold.vv", "gold.v")):
        catalog.create_or_replace_view(
            principal,
            TableRef.from_dotted("dev", view),
            sql=f"SELECT id FROM {source}",
            references=catalog.resolve_lineage_sources(principal, "dev", [source]),
            columns=columns,
        )

    def readable(inputs: dict[str, str]) -> tuple[str, ...]:
        task = python_task_run(
            session, principal.id, asset=f"gold.t{uuid.uuid4().hex[:6]}", inputs=inputs
        )
        minted = _service(session).mint_for_task(task.id, 1, ttl_seconds=60)
        scope = _verify(session, minted.token).task_scope
        assert scope is not None and scope.bound is not None
        assert scope.bound.inputs == tuple(sorted(set(inputs.values())))
        return scope.bound.readable

    assert readable({"a": "gold.vv"}) == ("bronze.orders", "gold.v", "gold.vv")
    assert readable({"a": "bronze.orders"}) == ("bronze.orders",)
    assert readable({"a": "gold.absent", "b": "not a name"}) == ("gold.absent", "not a name")
    monkeypatch.setattr(task_tokens_module, "_MAX_VIEW_DEPTH", 1)
    assert readable({"a": "gold.vv"}) == ("gold.v", "gold.vv")
    monkeypatch.setattr(task_tokens_module, "_MAX_VIEW_DEPTH", 10)
    monkeypatch.setattr(task_tokens_module, "_MAX_READABLE", 1)
    assert readable({"a": "gold.vv"}) == ("gold.vv",)


def test_view_expansion_costs_a_query_per_level_not_one_per_name(
    session: Session, dev: None
) -> None:
    names = tuple(f"bronze.t{i:02d}" for i in range(30))
    statements: list[str] = []

    def count(*event: object) -> None:
        statements.append(str(event[2]))

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", count)
    try:
        readable = task_tokens_module._readable_tables(session, "dev", names)
    finally:
        event.remove(engine, "before_cursor_execute", count)

    assert readable == names
    assert len(statements) <= 2, statements


@pytest.mark.parametrize(
    "status",
    [
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.PENDING,
        TaskStatus.READY,
        TaskStatus.UPSTREAM_FAILED,
    ],
)
def test_the_token_dies_when_the_attempt_ends(
    session: Session, dev: None, principal: PrincipalRef, status: TaskStatus
) -> None:
    task = python_task_run(session, principal.id)
    minted = _service(session).mint_for_task(task.id, 1, ttl_seconds=60)
    assert _verify(session, minted.token).task_scope is not None
    task.status = status
    session.commit()
    with pytest.raises(AuthenticationError, match="no longer valid"):
        _verify(session, minted.token)


def test_a_superseded_attempts_token_is_invalid_at_once(
    session: Session, dev: None, principal: PrincipalRef
) -> None:
    """Reclaim: the task runs again under a new claim; the old token must not work."""
    task = python_task_run(session, principal.id)
    old = _service(session).mint_for_task(task.id, 1, ttl_seconds=60)
    task.claim_count = 2
    session.commit()
    with pytest.raises(AuthenticationError, match="no longer valid"):
        _verify(session, old.token)
    new = _service(session).mint_for_task(task.id, 2, ttl_seconds=60)
    scope = _verify(session, new.token).task_scope
    assert scope is not None and scope.bound is not None and scope.bound.claim == 2


def test_other_ways_a_bound_token_stops_working(
    session: Session, dev: None, principal: PrincipalRef
) -> None:
    service = _service(session)

    def fresh() -> tuple[TaskRun, str, TaskToken]:
        task = python_task_run(session, principal.id, asset=f"gold.t{uuid.uuid4().hex[:6]}")
        minted = service.mint_for_task(task.id, 1, ttl_seconds=60)
        return task, minted.token, _row(session, minted.key_id)

    _, token, row = fresh()
    row.environment = "prod"
    session.commit()
    with pytest.raises(AuthenticationError, match="no longer valid"):
        _verify(session, token)

    task, token, _ = fresh()
    run = session.get(Run, task.run_id)
    assert run is not None
    version = session.get(JobVersion, run.job_version_id)
    assert version is not None
    version.manifest_json = {"version": 2, "assets": [{"table": task.asset, "kind": "sql"}]}
    session.commit()
    with pytest.raises(AuthenticationError, match="no longer valid"):
        _verify(session, token)

    task, token, _ = fresh()
    run = session.get(Run, task.run_id)
    assert run is not None
    version = session.get(JobVersion, run.job_version_id)
    assert version is not None
    for manifest in (
        {},
        {"assets": "garbage"},
        {"assets": ["x", {"table": "other"}]},
        {"assets": [{"table": task.asset, "kind": "python", "inputs": ["a.b"]}]},
        {"assets": [{"table": task.asset, "kind": "python", "inputs": {"x": 1}}]},
    ):
        version.manifest_json = manifest
        session.commit()
        with pytest.raises(AuthenticationError, match="no longer valid"):
            _verify(session, token)

    _, token, row = fresh()
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()
    with pytest.raises(AuthenticationError, match="no longer valid"):
        _verify(session, token)

    _, token, _ = fresh()
    account = session.get(Principal, principal.id)
    assert account is not None
    account.is_active = False
    session.commit()
    with pytest.raises(AuthenticationError, match="no longer valid"):
        _verify(session, token)


def test_deleting_the_task_run_deletes_its_tokens(
    session: Session, dev: None, principal: PrincipalRef
) -> None:
    task = python_task_run(session, principal.id)
    minted = _service(session).mint_for_task(task.id, 1, ttl_seconds=60)
    session.delete(session.get(Run, task.run_id))
    session.commit()
    assert session.execute(select(TaskToken)).scalars().all() == []
    with pytest.raises(AuthenticationError, match="Invalid credentials"):
        _verify(session, minted.token)


@pytest.mark.parametrize(
    ("options", "claim"),
    [
        ({"status": TaskStatus.READY}, 1),
        ({"status": TaskStatus.SUCCEEDED}, 1),
        ({"claim": 2}, 1),
        ({"kind": "sql"}, 1),
    ],
)
def test_mint_for_task_refuses_anything_but_the_running_python_attempt(
    session: Session, dev: None, principal: PrincipalRef, options: dict[str, Any], claim: int
) -> None:
    task = python_task_run(session, principal.id, **options)
    with pytest.raises(ConflictError, match="not the running attempt of a Python asset"):
        _service(session).mint_for_task(task.id, claim, ttl_seconds=60)
    assert session.execute(select(TaskToken)).scalars().all() == []


def test_mint_for_task_refuses_an_unknown_task_and_a_dead_principal(
    session: Session, dev: None, principal: PrincipalRef
) -> None:
    with pytest.raises(ConflictError):
        _service(session).mint_for_task(uuid.uuid4(), 1, ttl_seconds=60)
    task = python_task_run(session, principal.id)
    account = session.get(Principal, principal.id)
    assert account is not None
    account.is_active = False
    session.commit()
    with pytest.raises(NotFoundError, match="run principal"):
        _service(session).mint_for_task(task.id, 1, ttl_seconds=60)
    run = session.get(Run, task.run_id)
    assert run is not None
    run.run_as_principal_id = None
    session.commit()
    with pytest.raises(NotFoundError, match="run principal"):
        _service(session).mint_for_task(task.id, 1, ttl_seconds=60)


def test_revoke_for_task_is_idempotent_and_audited(
    session: Session, dev: None, principal: PrincipalRef
) -> None:
    task = python_task_run(session, principal.id)
    service = _service(session)
    minted = service.mint_for_task(task.id, 1, ttl_seconds=60)
    service.revoke_for_task(minted.key_id)
    first = _row(session, minted.key_id).revoked_at
    assert first is not None
    service.revoke_for_task(minted.key_id)
    assert _row(session, minted.key_id).revoked_at == first
    service.revoke_for_task("no-such-key")
    with pytest.raises(AuthenticationError, match="no longer valid"):
        _verify(session, minted.token)
    events = (
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.TASK_TOKEN_REVOKED)
        )
        .scalars()
        .all()
    )
    assert [event.detail for event in events] == [
        {
            "issuer": "worker",
            "subject": principal.name,
            "key_id": minted.key_id,
            "environment": "dev",
            "task_run_id": str(task.id),
            "claim": 1,
            "reason": "task_attempt_ended",
        }
    ]


def test_revoke_for_task_refuses_an_unbound_token(
    session: Session, principal: PrincipalRef
) -> None:
    service = _service(session)
    minted = service.mint(
        principal, principal.name, environment="dev", asset_scope=["gold.x"], ttl_seconds=60
    )
    with pytest.raises(ValidationError, match="not bound to a task"):
        service.revoke_for_task(minted.key_id)


def test_a_half_bound_row_cannot_be_stored(
    session: Session, dev: None, principal: PrincipalRef
) -> None:
    task = python_task_run(session, principal.id)
    for task_run_id, claim in ((task.id, None), (None, 1)):
        session.add(
            TaskToken(
                id=uuid.uuid4(),
                principal_id=principal.id,
                environment="dev",
                asset_scope=[],
                key_id=uuid.uuid4().hex,
                secret_hash="0" * 64,
                expires_at=datetime.now(UTC) + timedelta(seconds=60),
                task_run_id=task_run_id,
                claim=claim,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_a_half_bound_row_is_invalid_even_if_the_database_let_it_in(
    session: Session, dev: None, principal: PrincipalRef
) -> None:
    """Fail closed without relying on the CHECK constraint."""
    task = python_task_run(session, principal.id)
    session.execute(sa.text("PRAGMA ignore_check_constraints = ON"))
    ids = []
    for task_run_id, claim in ((task.id, None), (None, 1)):
        token = TaskToken(
            id=uuid.uuid4(),
            principal_id=principal.id,
            environment="dev",
            asset_scope=["gold.scores"],
            key_id=uuid.uuid4().hex,
            secret_hash="0" * 64,
            expires_at=datetime.now(UTC) + timedelta(seconds=60),
            task_run_id=task_run_id,
            claim=claim,
        )
        session.add(token)
        session.commit()
        ids.append(token.id)
    session.execute(sa.text("PRAGMA ignore_check_constraints = OFF"))
    for token_id in ids:
        assert resolve_task_token_scope(session, token_id) is None


@pytest.mark.parametrize("inputs", [["a.b"], {"x": 1}, "garbage"])
def test_mint_for_task_tells_a_tampered_manifest_from_a_lost_claim(
    session: Session, dev: None, principal: PrincipalRef, inputs: object
) -> None:
    """A python entry with malformed `inputs` is a ValidationError, not a lost claim."""
    task = python_task_run(session, principal.id)
    run = session.get(Run, task.run_id)
    assert run is not None
    version = session.get(JobVersion, run.job_version_id)
    assert version is not None
    version.manifest_json = {
        "version": 2,
        "assets": [{"table": task.asset, "kind": "python", "inputs": inputs}],
    }
    session.commit()
    with pytest.raises(ValidationError, match="inputs"):
        _service(session).mint_for_task(task.id, 1, ttl_seconds=60)
    assert session.execute(select(TaskToken)).scalars().all() == []


def _view_setup(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> tuple[Table, Table, Table]:
    """Create `gold.v` over `bronze.orders`, plus an unrelated `bronze.other`.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The acting principal.

    Returns
    -------
    tuple of Table
        The view row, the `bronze.orders` row and the `bronze.other` row.
    """
    bronze, gold = NamespacePath.from_parts(["bronze"]), NamespacePath.from_parts(["gold"])
    catalog = _prepared_catalog(
        session,
        tmp_path,
        AllowAllAuthorization(),
        namespaces=[bronze, gold],
        tables=[TableRef("dev", bronze, "orders"), TableRef("dev", bronze, "other")],
    )
    catalog.create_or_replace_view(
        principal,
        TableRef.from_dotted("dev", "gold.v"),
        sql="SELECT id FROM bronze.orders",
        references=catalog.resolve_lineage_sources(principal, "dev", ["bronze.orders"]),
        columns=[ColumnSpec(name="id", type="long", nullable=True)],
    )
    rows = {row.name: row for row in session.scalars(select(Table))}
    return rows["v"], rows["orders"], rows["other"]


def _readable_of(session: Session, principal: PrincipalRef, view: str) -> tuple[str, ...]:
    """Mint for a task reading `view` and return its readable tables.

    Parameters
    ----------
    session
        Database session.
    principal
        The run principal.
    view
        The declared input.

    Returns
    -------
    tuple of str
        `BoundTask.readable`.
    """
    task = python_task_run(
        session, principal.id, asset=f"gold.t{uuid.uuid4().hex[:6]}", inputs={"a": view}
    )
    minted = _service(session).mint_for_task(task.id, 1, ttl_seconds=60)
    scope = _verify(session, minted.token).task_scope
    assert scope is not None and scope.bound is not None
    return scope.bound.readable


@pytest.mark.parametrize(
    "definition",
    [
        {"references": None},
        {"references": 7},
        {"references": "x"},
        {"references": [None, 3, "x", {"name": 5}, {"id": "nope", "name": "bronze.orders"}]},
        {"references": [{"name": "bronze.orders"}]},
        ["a list"],
        "a string",
    ],
)
def test_a_malformed_view_definition_never_raises(
    session: Session, tmp_path: Path, principal: PrincipalRef, definition: object
) -> None:
    view, _, _ = _view_setup(session, tmp_path, principal)
    session.execute(sa.update(Table).where(Table.id == view.id).values(view_definition=definition))
    session.commit()
    assert _readable_of(session, principal, "gold.v") == ("gold.v",)


def test_view_expansion_follows_the_stored_id_not_the_stored_name(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    view, orders, other = _view_setup(session, tmp_path, principal)
    stored = dict(view.view_definition or {})
    assert _readable_of(session, principal, "gold.v") == ("bronze.orders", "gold.v")

    def point_at(table_id: uuid.UUID) -> None:
        view.view_definition = {
            **stored,
            "references": [{"id": str(table_id), "name": "bronze.orders"}],
        }
        session.commit()

    point_at(uuid.uuid4())  # dropped, or dropped and re-created under the same name
    assert _readable_of(session, principal, "gold.v") == ("gold.v",)
    point_at(other.id)  # the id now carries another name: renamed
    assert _readable_of(session, principal, "gold.v") == ("gold.v",)
    point_at(orders.id)
    assert _readable_of(session, principal, "gold.v") == ("bronze.orders", "gold.v")


@pytest.mark.parametrize(
    ("options", "cancel"),
    [
        ({"status": RunStatus.SUCCEEDED}, False),
        ({"status": RunStatus.FAILED}, False),
        ({"status": RunStatus.CANCELLED}, False),
        ({"status": RunStatus.RUNNING}, True),
    ],
)
def test_the_run_must_be_running_and_not_cancelling(
    session: Session,
    dev: None,
    principal: PrincipalRef,
    options: dict[str, Any],
    cancel: bool,
) -> None:
    task = python_task_run(session, principal.id)
    minted = _service(session).mint_for_task(task.id, 1, ttl_seconds=60)
    assert _verify(session, minted.token).task_scope is not None
    run = session.get(Run, task.run_id)
    assert run is not None
    run.status = options["status"]
    run.cancel_requested = cancel
    session.commit()
    with pytest.raises(AuthenticationError, match="no longer valid"):
        _verify(session, minted.token)
    with pytest.raises(ConflictError, match="not the running attempt"):
        _service(session).mint_for_task(task.id, 1, ttl_seconds=60)


def test_repr_of_a_minted_token_hides_the_secret(
    session: Session, dev: None, principal: PrincipalRef
) -> None:
    task = python_task_run(session, principal.id)
    minted = _service(session).mint_for_task(task.id, 1, ttl_seconds=60)
    assert minted.token not in repr(minted)
    # The secret may itself contain "_": cut the known prefix, never split on it.
    prefix = f"lakana_task_{minted.key_id}_"
    assert minted.token.startswith(prefix)
    secret = minted.token.removeprefix(prefix)
    assert len(secret) >= 32
    assert secret not in repr(minted)


def test_verify_sets_the_claim_form_run_context(
    session: Session, dev: None, principal: PrincipalRef
) -> None:
    task = python_task_run(session, principal.id, claim=5)
    minted = _service(session).mint_for_task(task.id, 5, ttl_seconds=60)
    assert _verify(session, minted.token).run_context == RunContext(task_run_id=task.id, claim=5)
    plain = _service(session).mint(
        principal, principal.name, environment="dev", asset_scope=["gold.x"], ttl_seconds=60
    )
    assert _verify(session, plain.token).run_context is None


@pytest.mark.parametrize(
    ("extra", "output", "keep"),
    [
        ({}, "table", None),
        ({"output": "table", "keep_versions": 2}, "table", None),
        ({"output": "artifact"}, "artifact", None),
        ({"output": "artifact", "keep_versions": 2}, "artifact", 2),
        ({"output": "artifact", "keep_versions": 0}, "artifact", None),
        ({"output": "artifact", "keep_versions": True}, "artifact", None),
        ({"output": "artifact", "keep_versions": "2"}, "artifact", None),
        ({"output": "ARTIFACT"}, "table", None),
        ({"output": ["artifact"]}, "table", None),
        ({"output": " artifact"}, "table", None),
        ({"output": None, "keep_versions": 2}, "table", None),
        ({"output": "artifact", "keep_versions": -3}, "artifact", None),
        ({"output": "artifact", "keep_versions": 2.0}, "artifact", None),
        ({"output": "artifact", "keep_versions": 10**30}, "artifact", None),
        ({"output": "artifact", "keep_versions": [2]}, "artifact", None),
        ({"output": "artifact", "keep_versions": None}, "artifact", None),
    ],
)
def test_a_bound_scope_carries_output_and_keep_versions(
    session: Session,
    dev: None,
    principal: PrincipalRef,
    extra: dict[str, object],
    output: str,
    keep: int | None,
) -> None:
    """Retention is read from the project version manifest, as untrusted input."""
    ensure_table(session, "dev", "silver", "rows")
    task = python_task_run(
        session, principal.id, asset="models.churn", inputs={"rows": "silver.rows"}, extra=extra
    )
    bound = _resolve_bound(session, task.id, 1, "dev")
    assert bound is not None
    assert (bound.asset, bound.inputs, bound.output, bound.keep_versions) == (
        "models.churn",
        ("silver.rows",),
        output,
        keep,
    )


def test_a_sql_entry_never_resolves_to_an_artifact_scope(
    session: Session, dev: None, principal: PrincipalRef
) -> None:
    """An entry that is not a Python asset gives no bound scope, whatever `output` says."""
    task = python_task_run(
        session, principal.id, asset="models.churn", kind="sql", extra={"output": "artifact"}
    )
    assert _resolve_bound(session, task.id, 1, "dev") is None


def _repository_name(session: Session, task: TaskRun) -> str:
    """Read the name of the repository a task's run was deployed from.

    Parameters
    ----------
    session
        Database session.
    task
        The task run.

    Returns
    -------
    str
        The repository's name.
    """
    run = session.get(Run, task.run_id)
    assert run is not None
    repository = session.get(Repository, run.repository_id)
    assert repository is not None
    return repository.name


@pytest.mark.parametrize(
    ("extra", "connections", "readable"),
    [
        ({"connections": ["crm-db"]}, ("crm-db",), True),
        ({"connections": ["crm-db", "files"]}, ("crm-db", "files"), True),
        ({}, (), True),
        ({"connections": []}, (), True),
        ({"connections": "crm-db"}, (), False),
        ({"connections": ["crm-db", 3]}, (), False),
        ({"connections": ["Not A Name"]}, (), False),
        ({"connections": {"crm-db": 1}}, (), False),
    ],
)
def test_a_bound_scope_carries_the_declared_connections_and_the_repository(
    session: Session,
    dev: None,
    principal: PrincipalRef,
    extra: dict[str, object],
    connections: tuple[str, ...],
    readable: bool,
) -> None:
    """Connections come from the deployed manifest, read fail-safe; never from the token."""
    ensure_table(session, "dev", "silver", "rows")
    task = python_task_run(
        session, principal.id, asset="bronze.customers", inputs={"rows": "silver.rows"}, extra=extra
    )
    bound = _resolve_bound(session, task.id, 1, "dev")
    assert bound is not None
    assert bound.connections == connections
    assert bound.connections_readable is readable
    assert bound.repository == _repository_name(session, task)


def test_a_bound_scope_defaults_to_no_connections_and_no_repository() -> None:
    """A `BoundTask` built without them (older callers, tests) declares nothing."""
    bound = BoundTask(uuid.uuid4(), 1, "gold.t", (), ())
    assert (bound.connections, bound.connections_readable, bound.repository) == ((), True, "")
