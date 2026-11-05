"""The messages a fired health signal sends to the jobs that build its table (#340)."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from health_test_helpers import (
    _MANIFEST,
    _ORDERS,
    _deployed,
    _environment_id,
    _rows,
    _seed,
    _table_id,
    health_catalog,
)
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import lakana_core.catalog.service as service_module
from lakana_core.auth import PrincipalRef, RunContext
from lakana_core.catalog.health_jobs import (
    building_jobs,
    deployed_manifests,
    judged_by_run,
    queue_health_notifications,
    rows_to_tell,
)
from lakana_core.catalog.service import CatalogService
from lakana_core.models import (
    ColumnMask,
    EnvironmentDeployment,
    JobVersion,
    NotificationDelivery,
    Principal,
    PrincipalType,
    RowFilter,
    Run,
    RunKind,
    RunStatus,
    Table,
    TableHealthSignal,
    TaskRun,
    TaskStatus,
)
from lakana_core.notifications.payload import facts_for_health
from lakana_core.projects.manifest import JobNotify

_FIRED_AT = datetime(2026, 11, 3, 6, 0, tzinfo=UTC)


@pytest.fixture
def catalog(session: Session, tmp_path: Path, principal: PrincipalRef) -> CatalogService:
    """Provide an allow-all catalog with the empty table `silver.orders`.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The creator.

    Returns
    -------
    CatalogService
        The catalog.
    """
    return health_catalog(session, tmp_path, principal)


def _deliveries(session: Session) -> list[NotificationDelivery]:
    """Read the queued deliveries.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of NotificationDelivery
        Every delivery.
    """
    return list(session.scalars(select(NotificationDelivery)))


def test_the_building_jobs_are_those_whose_selection_holds_the_table(
    catalog: CatalogService, session: Session
) -> None:
    repository_id = _deployed(session, _MANIFEST)
    found = building_jobs([(repository_id, _MANIFEST)], "silver.orders")
    assert [(j.job, j.notify, j.schedule) for j in found] == [
        ("nightly", JobNotify("team-hook", "slack"), ("0 6 * * *", "UTC")),
        ("quiet", None, None),
    ]
    assert building_jobs([(repository_id, {"assets": "damaged"})], "silver.orders") == []
    assert building_jobs([(repository_id, "not a manifest")], "silver.orders") == []
    assert deployed_manifests(session, _environment_id(session)) == [(repository_id, _MANIFEST)]


def test_a_fired_warn_signal_is_queued_once_per_notifying_job(
    catalog: CatalogService, session: Session, principal: PrincipalRef
) -> None:
    _deployed(session, _MANIFEST)
    _seed(session, "volume", {"added": 1_000, "total": 1_000})
    catalog.append(principal, _ORDERS, _rows([1, 2, 3]))
    (delivery,) = _deliveries(session)
    fired = session.scalars(
        select(TableHealthSignal).where(TableHealthSignal.state == "fired")
    ).one()
    assert (
        delivery.event,
        delivery.run_id,
        delivery.health_signal_id,
        delivery.job,
        delivery.format,
        delivery.secret_name,
    ) == ("health", None, fired.id, "nightly", "slack", "team-hook")
    assert delivery.next_attempt_at == fired.observed_at
    again = queue_health_notifications(
        session,
        environment_id=fired.environment_id,
        table="silver.orders",
        rows=[fired],
        now=fired.observed_at,
    )
    assert again == []


def test_a_block_signal_outside_a_run_is_queued(
    catalog: CatalogService, session: Session, principal: PrincipalRef
) -> None:
    _deployed(session, _MANIFEST)
    table = session.get(Table, _table_id(session))
    assert table is not None
    table.health_effects = {"volume": "block"}
    session.commit()
    _seed(session, "volume", {"added": 1_000, "total": 1_000})
    catalog.append(principal, _ORDERS, _rows([1, 2, 3]))  # no run context: nothing can block it
    assert len(_deliveries(session)) == 1


def test_which_rows_are_told() -> None:
    def row(state: str, effect: str, column: str | None = None) -> TableHealthSignal:
        """Build an unsaved signal row.

        Parameters
        ----------
        state
            Its state.
        effect
            Its effect.
        column
            Its column.

        Returns
        -------
        TableHealthSignal
            The row.
        """
        signal = "volume" if column is None else "range"
        return TableHealthSignal(signal=signal, column_name=column, state=state, effect=effect)

    rows = [
        row("fired", "warn"),
        row("fired", "block"),
        row("ok", "warn"),
        row("off", "off"),
        row("fired", "block", "price"),
        row("fired", "block", "qty"),
    ]

    def told(judged: frozenset[tuple[str, str | None]]) -> list[tuple]:
        """Say which rows are told when the run's health checks judge `judged`.

        Parameters
        ----------
        judged
            What the run's checks judge.

        Returns
        -------
        list of tuple
            Signal, column, state and effect of each row told.
        """
        return [
            (r.signal, r.column_name, r.state, r.effect) for r in rows_to_tell(rows, judged=judged)
        ]

    # Outside a run, or in a run whose checks do not judge it (an older manifest), a block too.
    assert told(frozenset()) == [
        ("volume", None, "fired", "warn"),
        ("volume", None, "fired", "block"),
        ("range", "price", "fired", "block"),
        ("range", "qty", "fired", "block"),
    ]
    # A run's check blocks it, and the run's `failed` message tells of it.
    assert told(frozenset({("volume", None), ("range", "price")})) == [
        ("volume", None, "fired", "warn"),
        ("range", "qty", "fired", "block"),
    ]
    assert told(frozenset({("range", None)})) == [
        ("volume", None, "fired", "warn"),
        ("volume", None, "fired", "block"),
    ]


def _task_run(
    session: Session, manifest: dict, *, has_checks: bool, asset: str, repository: str
) -> uuid.UUID:
    """Store a run of the deployed project with one task run, as a run's start leaves it.

    Parameters
    ----------
    session
        Database session; commits.
    manifest
        The manifest of the run's project version.
    has_checks
        Whether the task has checks.
    asset
        The task's asset.
    repository
        The run's repository: one running run per repository.

    Returns
    -------
    uuid.UUID
        The task run's id.
    """
    repository_id = _deployed(session, manifest, repository=repository)
    version = session.scalars(
        select(JobVersion)
        .join(EnvironmentDeployment, EnvironmentDeployment.current_job_version_id == JobVersion.id)
        .where(EnvironmentDeployment.repository_id == repository_id)
    ).one()
    run = Run(
        id=uuid.uuid4(),
        environment_id=_environment_id(session),
        repository_id=repository_id,
        job_version_id=version.id,
        kind=RunKind.FULL,
        selection_json={},
        status=RunStatus.RUNNING,
        created_at=_FIRED_AT,
    )
    session.add(run)
    session.flush()
    task = TaskRun(
        id=uuid.uuid4(),
        run_id=run.id,
        asset=asset,
        status=TaskStatus.RUNNING,
        max_attempts=1,
        timeout_seconds=60,
        retry_delay_seconds=0,
        has_checks=has_checks,
    )
    session.add(task)
    session.commit()
    return task.id


def test_what_a_run_judges_comes_from_its_own_manifest(
    catalog: CatalogService, session: Session
) -> None:
    blocking = {
        "version": 14,
        "assets": [
            {
                "table": "silver.orders",
                "path": "assets/silver/orders.sql",
                "health": {"volume": "block", "range.price": "block", "schema": "warn"},
            }
        ],
        "edges": [],
        "jobs": {},
    }
    task = _task_run(session, blocking, has_checks=True, asset="silver.orders", repository="a")
    assert judged_by_run(session, task, "silver.orders") == {("volume", None), ("range", "price")}
    assert judged_by_run(session, None, "silver.orders") == frozenset()
    # Another table than the task's own, or a task without checks: nothing is judged.
    assert judged_by_run(session, task, "silver.other") == frozenset()
    older = _task_run(session, blocking, has_checks=False, asset="silver.orders", repository="b")
    assert judged_by_run(session, older, "silver.orders") == frozenset()


def test_a_judged_block_signal_of_a_task_run_is_not_also_sent(
    catalog: CatalogService, session: Session
) -> None:
    """The run context reaches the filter: the run's own check reports the block, not a message."""
    manifest = {
        **_MANIFEST,
        "assets": [
            {
                "table": "silver.orders",
                "path": "assets/silver/orders.sql",
                "health": {"volume": "block", "null_rate": "warn"},
            },
            {"table": "gold.report", "path": "assets/gold/report.sql"},
        ],
    }
    task = _task_run(session, manifest, has_checks=True, asset="silver.orders", repository="a")
    blocked, warned = (
        TableHealthSignal(
            id=uuid.uuid4(),
            environment_id=_environment_id(session),
            table_id=_table_id(session),
            signal=signal,
            column_name=column,
            snapshot_id=7,
            sequence_number=7,
            observed={},
            band={},
            state="fired",
            effect=effect,
            observed_at=_FIRED_AT,
        )
        for signal, column, effect in (("volume", None, "block"), ("null_rate", "status", "warn"))
    )
    rows = [blocked, warned]

    def told(run: RunContext | None) -> list[uuid.UUID]:
        queued = catalog._queue_health(_ORDERS, _environment_id(session), rows, run)
        return sorted((d.health_signal_id for d in queued if d.health_signal_id), key=str)

    run = RunContext(task_run_id=task, query_run_id=uuid.uuid4())
    assert told(run) == [warned.id]  # the judged block is left to the run's `failed` message
    assert sorted(told(None), key=str) == sorted([blocked.id, warned.id], key=str)  # no run: both


def test_a_table_no_job_builds_is_only_shown(
    catalog: CatalogService, session: Session, principal: PrincipalRef
) -> None:
    _seed(session, "volume", {"added": 1_000, "total": 1_000})
    catalog.append(principal, _ORDERS, _rows([1, 2, 3]))
    assert _deliveries(session) == []


def test_a_pipeline_that_stays_broken_sends_one_message_until_it_recovers(
    catalog: CatalogService, session: Session, principal: PrincipalRef
) -> None:
    _deployed(session, _MANIFEST)
    # Sequence numbers below the table's own (1, 2, ...): the history came first.
    _seed(session, "volume", {"added": 1_000, "total": 1_000}, first_day=-10)
    catalog.append(principal, _ORDERS, _rows([1, 2, 3]))
    catalog.append(principal, _ORDERS, _rows([4, 5, 6]))  # fired again: no second message
    assert len(_deliveries(session)) == 1
    catalog.append(principal, _ORDERS, _rows(list(range(10, 1_010))))  # back in the band
    catalog.append(principal, _ORDERS, _rows([7, 8, 9]))  # fired after an ok: told again
    fired = session.scalars(
        select(TableHealthSignal.id)
        .where(TableHealthSignal.signal == "volume", TableHealthSignal.state == "fired")
        .order_by(TableHealthSignal.sequence_number)
    ).all()
    assert len(fired) == 3
    assert [d.health_signal_id for d in _deliveries(session)] == [fired[0], fired[2]]


def test_a_fired_row_that_was_set_back_is_not_the_previous_observation(
    catalog: CatalogService, session: Session, principal: PrincipalRef
) -> None:
    _deployed(session, _MANIFEST)
    # Sequence numbers below the table's own (1, 2, ...): the history came first.
    _seed(session, "volume", {"added": 1_000, "total": 1_000}, first_day=-10)
    _seed(session, "volume", {"added": 3, "total": 3}, count=1, first_day=-2, state="fired")
    marked = session.scalars(select(TableHealthSignal).where(TableHealthSignal.state == "fired"))
    for row in marked:
        row.set_back_at = _FIRED_AT
    session.commit()
    catalog.append(principal, _ORDERS, _rows([1, 2, 3]))
    assert len(_deliveries(session)) == 1


def test_a_failure_to_queue_never_fails_the_write(
    catalog: CatalogService,
    session: Session,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _deployed(session, _MANIFEST)
    _seed(session, "volume", {"added": 1_000, "total": 1_000})

    def explode(*args: object, **kwargs: object) -> list[NotificationDelivery]:
        raise RuntimeError("a damaged manifest")

    monkeypatch.setattr(service_module, "queue_health_notifications", explode)
    with caplog.at_level(logging.WARNING, logger="lakana_core.catalog.service"):
        catalog.append(principal, _ORDERS, _rows([1, 2, 3]))
    assert _deliveries(session) == []
    assert session.scalars(
        select(TableHealthSignal).where(TableHealthSignal.state == "fired")
    ).one()
    assert any(
        "health notifications of silver.orders could not be queued (RuntimeError)" in r.message
        for r in caplog.records
    )


def _fired(
    session: Session,
    signal: str,
    column: str | None,
    observed: dict,
    band: dict,
) -> NotificationDelivery:
    """Record a fired signal of `silver.orders` and a delivery queued for it.

    Parameters
    ----------
    session
        Database session; commits.
    signal
        The signal.
    column
        Its column.
    observed
        What was observed.
    band
        The usual band.

    Returns
    -------
    NotificationDelivery
        The delivery.
    """
    repository_id = _deployed(session, _MANIFEST)
    row = TableHealthSignal(
        id=uuid.uuid4(),
        environment_id=_environment_id(session),
        table_id=_table_id(session),
        signal=signal,
        column_name=column,
        snapshot_id=7,
        sequence_number=7,
        observed=observed,
        band=band,
        state="fired",
        effect="warn",
        observed_at=_FIRED_AT,
    )
    session.add(row)
    session.flush()
    (delivery,) = queue_health_notifications(
        session,
        environment_id=row.environment_id,
        table="silver.orders",
        rows=[row],
        now=_FIRED_AT,
        manifests=[(repository_id, _MANIFEST)],
    )
    session.add(delivery)
    session.commit()
    return delivery


_VOLUME = ({"added": 3, "total": 3}, {"low": 900, "high": 1_100, "median": 1_000})
_NULLS = ({"nulls": 9, "values": 10, "rate": 0.9}, {"low": 0.0, "high": 0.1, "median": 0.05})


def _creator(session: Session) -> uuid.UUID:
    """Store a principal that creates policies.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    uuid.UUID
        Its id.
    """
    who = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="steward", is_active=True)
    session.add(who)
    session.flush()
    return who.id


def _policy(session: Session, kind: type[ColumnMask] | type[RowFilter], **extra: str) -> None:
    """Store a policy on `silver.orders` for someone else than any reader here.

    Parameters
    ----------
    session
        Database session; commits.
    kind
        `ColumnMask` or `RowFilter`.
    **extra
        The policy's own columns.
    """
    creator = _creator(session)
    session.add(
        kind(
            id=uuid.uuid4(),
            principal_id=creator,
            environment="dev",
            namespace="silver",
            table="orders",
            environment_id=_environment_id(session),
            table_id=_table_id(session),
            created_by=creator,
            created_at=_FIRED_AT,
            **extra,
        )
    )
    session.commit()


def test_a_health_message_of_a_table_without_policies_says_the_values(
    catalog: CatalogService, session: Session
) -> None:
    delivery = _fired(session, "volume", None, *_VOLUME)
    facts = facts_for_health(session, delivery, "dev", delivery.id)
    assert (facts.table, facts.job, facts.repository, facts.environment) == (
        "silver.orders",
        "nightly",
        "shop",
        "dev",
    )
    assert (facts.run_id, facts.status, facts.commit, facts.finished_at) == ("", "", "", _FIRED_AT)
    assert facts.alert == (
        "silver.orders: volume 3 rows, usually 900\N{EN DASH}1,100 (snapshot 7, 03 Nov 06:00 UTC); "
        "check the source extract, or set health: {volume: off} if this table now loads less"
    )
    assert facts.command == "lakana table health silver.orders --env dev"


def test_a_message_of_a_set_back_signal_is_still_delivered(
    catalog: CatalogService, session: Session
) -> None:
    delivery = _fired(session, "volume", None, *_VOLUME)
    row = session.get(TableHealthSignal, delivery.health_signal_id)
    assert row is not None
    row.set_back_at = _FIRED_AT
    session.commit()
    assert facts_for_health(session, delivery, "dev", delivery.id).alert.startswith(
        "silver.orders: volume 3 rows"
    )


_QUIET_NULLS = (
    "silver.orders: health signal null_rate of status fired at snapshot 7 (03 Nov 06:00 UTC); "
    "`lakana table health silver.orders --env dev --signals` shows its usual band and new value "
    "to readers who may see them; check what fills status upstream, or set health: "
    "{null_rate: {status: off}} if this is expected"
)


def test_a_masked_column_sends_no_values(catalog: CatalogService, session: Session) -> None:
    _policy(session, ColumnMask, column="email", expression="'***'")
    delivery = _fired(session, "null_rate", "status", *_NULLS)
    facts = facts_for_health(session, delivery, "dev", delivery.id)
    assert facts.alert == _QUIET_NULLS
    assert "90" not in facts.alert and "0.9" not in facts.alert
    volume = _fired(session, "volume", None, *_VOLUME)  # a row count is no column's value
    assert "3 rows" in facts_for_health(session, volume, "dev", volume.id).alert


def test_a_row_filter_sends_no_values_of_any_signal(
    catalog: CatalogService, session: Session
) -> None:
    _policy(session, RowFilter, expression="status = 's'")
    delivery = _fired(session, "volume", None, *_VOLUME)
    alert = facts_for_health(session, delivery, "dev", delivery.id).alert
    assert alert == (
        "silver.orders: health signal volume fired at snapshot 7 (03 Nov 06:00 UTC); "
        "`lakana table health silver.orders --env dev --signals` shows its usual band and new "
        "value to readers who may see them; check the source extract, or set health: "
        "{volume: off} if this is expected"
    )
    assert "1,100" not in alert and "3 rows" not in alert


def test_a_damaged_row_sends_the_text_without_values(
    catalog: CatalogService, session: Session
) -> None:
    delivery = _fired(session, "null_rate", "status", {"nulls": 1}, {"low": 0.0})
    assert facts_for_health(session, delivery, "dev", delivery.id).alert == _QUIET_NULLS


def test_a_database_error_while_queuing_fails_the_write(
    catalog: CatalogService,
    session: Session,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _deployed(session, _MANIFEST)
    _seed(session, "volume", {"added": 1_000, "total": 1_000})

    def gone(*args: object, **kwargs: object) -> list[NotificationDelivery]:
        raise OperationalError("SELECT", {}, Exception("gone"))

    monkeypatch.setattr(service_module, "queue_health_notifications", gone)
    with pytest.raises(OperationalError):
        catalog.append(principal, _ORDERS, _rows([1, 2, 3]))


def test_rows_that_did_not_fire_queue_nothing() -> None:
    quiet = TableHealthSignal(id=uuid.uuid4(), signal="volume", state="ok", effect="warn")
    assert (
        queue_health_notifications(
            None,  # ty: ignore[invalid-argument-type]
            environment_id=uuid.uuid4(),
            table="silver.orders",
            rows=[quiet],
            now=_FIRED_AT,
        )
        == []
    )
