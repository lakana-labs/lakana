"""The stamp that names the task attempt which made a snapshot (#49)."""

from __future__ import annotations

import uuid

from pyiceberg.schema import Schema
from pyiceberg.table.metadata import TableMetadata
from pyiceberg.table.refs import SnapshotRefType
from pyiceberg.table.snapshots import Operation, Snapshot, Summary
from pyiceberg.table.update import (
    AddSnapshotUpdate,
    SetPropertiesUpdate,
    SetSnapshotRefUpdate,
    TableUpdate,
)
from pyiceberg.types import LongType, NestedField

from lakana_core.auth import RunContext
from lakana_core.catalog.attempt_stamp import (
    CLAIM_PROPERTY,
    TASK_RUN_PROPERTY,
    stamp_snapshots,
    written_by_attempt,
)
from lakana_core.catalog.metadata import apply_updates, build_table_metadata

_TASK_RUN = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
_OTHER_RUN = uuid.UUID("00000000-0000-0000-0000-0000000000bb")


def _snapshot(snapshot_id: int, parent: int | None, **summary: str) -> Snapshot:
    """Build a snapshot with a summary.

    Parameters
    ----------
    snapshot_id
        Its id.
    parent
        Its parent's id.
    **summary
        Summary properties besides the operation.

    Returns
    -------
    Snapshot
        The snapshot.
    """
    return Snapshot(
        snapshot_id=snapshot_id,
        parent_snapshot_id=parent,
        sequence_number=snapshot_id,
        timestamp_ms=1_700_000_000_000 + snapshot_id,
        manifest_list=f"file:///t/metadata/snap-{snapshot_id}.avro",
        summary=Summary(Operation.APPEND, **summary),
        schema_id=0,
    )


def _summary(update: TableUpdate) -> dict[str, str]:
    """Read the summary of an `AddSnapshotUpdate`.

    Parameters
    ----------
    update
        The update.

    Returns
    -------
    dict
        Its summary, the operation included.
    """
    assert isinstance(update, AddSnapshotUpdate)
    assert update.snapshot.summary is not None
    return update.snapshot.summary.model_dump()


def _table(*snapshots: Snapshot) -> TableMetadata:
    """Build metadata whose `main` is the last of `snapshots`.

    Parameters
    ----------
    *snapshots
        The snapshots, oldest first.

    Returns
    -------
    TableMetadata
        The metadata.
    """
    base = build_table_metadata(
        schema=Schema(NestedField(1, "id", LongType(), required=False)),
        location="file:///t",
        partition_spec=None,
        sort_order=None,
        properties={},
    )
    updates: list[TableUpdate] = [AddSnapshotUpdate(snapshot=s) for s in snapshots]
    if snapshots:
        updates.append(
            SetSnapshotRefUpdate(
                ref_name="main",
                type=SnapshotRefType.BRANCH,
                snapshot_id=snapshots[-1].snapshot_id,
            )
        )
    return apply_updates(base, updates)


def _stamp(task_run: uuid.UUID = _TASK_RUN, claim: int = 2) -> dict[str, str]:
    """Write the stamp properties of an attempt.

    Parameters
    ----------
    task_run
        The task run.
    claim
        The claim.

    Returns
    -------
    dict
        The two summary properties, by their (dotted) names.
    """
    return {TASK_RUN_PROPERTY: str(task_run), CLAIM_PROPERTY: str(claim)}


def test_a_write_of_a_task_attempt_is_stamped_with_its_task_run_and_claim() -> None:
    other = SetPropertiesUpdate(updates={"owner": "x"})
    staged = AddSnapshotUpdate(snapshot=_snapshot(5, None, **{"added-records": "3"}))

    stamped = stamp_snapshots([other, staged], RunContext(task_run_id=_TASK_RUN, claim=2))

    assert stamped[0] is other
    assert _summary(stamped[1]) == {"operation": "append", "added-records": "3", **_stamp()}
    assert isinstance(stamped[1], AddSnapshotUpdate)
    assert stamped[1].snapshot.snapshot_id == 5
    assert _summary(staged) == {"operation": "append", "added-records": "3"}


def test_a_stamp_a_caller_sent_is_never_kept() -> None:
    """Only the platform stamps: a summary that claims an attempt is stripped of the claim."""
    forged = AddSnapshotUpdate(snapshot=_snapshot(5, None, **_stamp(), keep="yes"))
    for context in (None, RunContext(task_run_id=_TASK_RUN, query_run_id=uuid.uuid4())):
        (stripped,) = stamp_snapshots([forged], context)
        assert _summary(stripped) == {"operation": "append", "keep": "yes"}
    (restamped,) = stamp_snapshots([forged], RunContext(task_run_id=_OTHER_RUN, claim=7))
    assert _summary(restamped) == {"operation": "append", "keep": "yes", **_stamp(_OTHER_RUN, 7)}


def test_updates_without_a_snapshot_are_returned_as_they_are() -> None:
    plain = AddSnapshotUpdate(snapshot=_snapshot(5, None))
    updates: list[TableUpdate] = [SetPropertiesUpdate(updates={"a": "b"}), plain]
    assert stamp_snapshots(updates, None) == updates


def test_the_current_snapshot_stamped_by_the_attempt_proves_its_write() -> None:
    metadata = _table(_snapshot(1, None), _snapshot(2, 1, **_stamp()))
    assert written_by_attempt(metadata, since=1, task_run_id=_TASK_RUN, claim=2)
    assert written_by_attempt(metadata, since=None, task_run_id=_TASK_RUN, claim=2)


def test_another_attempt_or_task_run_proves_nothing() -> None:
    metadata = _table(_snapshot(1, None), _snapshot(2, 1, **_stamp(claim=1)))
    assert not written_by_attempt(metadata, since=1, task_run_id=_TASK_RUN, claim=2)
    assert not written_by_attempt(metadata, since=1, task_run_id=_OTHER_RUN, claim=1)
    assert not written_by_attempt(
        _table(_snapshot(1, None)), since=None, task_run_id=_TASK_RUN, claim=2
    )
    assert not written_by_attempt(_table(), since=None, task_run_id=_TASK_RUN, claim=2)


def test_a_write_another_writer_committed_on_top_of_still_counts() -> None:
    """The attempt's snapshot is an ancestor of the current one, newer than where it started."""
    metadata = _table(_snapshot(1, None), _snapshot(2, 1, **_stamp()), _snapshot(3, 2))
    assert written_by_attempt(metadata, since=1, task_run_id=_TASK_RUN, claim=2)


def test_a_stamp_at_or_before_where_the_attempt_started_is_not_its_write() -> None:
    metadata = _table(_snapshot(1, None, **_stamp()), _snapshot(2, 1))
    assert not written_by_attempt(metadata, since=1, task_run_id=_TASK_RUN, claim=2)
    assert not written_by_attempt(metadata, since=2, task_run_id=_TASK_RUN, claim=2)


def test_a_snapshot_off_the_current_branch_is_not_the_write() -> None:
    """A stamped snapshot that `main` no longer descends from (rolled back) does not count."""
    metadata = _table(_snapshot(1, None), _snapshot(2, 1, **_stamp()), _snapshot(3, 1))
    assert not written_by_attempt(metadata, since=1, task_run_id=_TASK_RUN, claim=2)
