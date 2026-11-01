"""The stamp that names the task attempt which made a snapshot (#49)."""

from __future__ import annotations

import uuid

import pytest
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
from lakana_core.catalog import attempt_stamp
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
    (stripped,) = stamp_snapshots([forged], None)
    assert _summary(stripped) == {"operation": "append", "keep": "yes"}
    (marked,) = stamp_snapshots(
        [forged], RunContext(task_run_id=_OTHER_RUN, query_run_id=_TASK_RUN)
    )
    assert _summary(marked) == {
        "operation": "append",
        "keep": "yes",
        TASK_RUN_PROPERTY: str(_OTHER_RUN),
    }
    (restamped,) = stamp_snapshots([forged], RunContext(task_run_id=_OTHER_RUN, claim=7))
    assert _summary(restamped) == {"operation": "append", "keep": "yes", **_stamp(_OTHER_RUN, 7)}


def test_a_sql_tasks_write_run_marks_its_task_run_but_proves_no_attempt() -> None:
    """The query-run form (a SQL task's write run) names its task run and no claim.

    The mark is what `written_by_run` reads to refuse a second history write of one run; without
    a claim it never passes for a Python attempt's write (`written_by_attempt`).
    """
    staged = AddSnapshotUpdate(snapshot=_snapshot(5, None, **{"added-records": "3"}))

    (marked,) = stamp_snapshots(
        [staged], RunContext(task_run_id=_TASK_RUN, query_run_id=uuid.uuid4())
    )

    assert _summary(marked) == {
        "operation": "append",
        "added-records": "3",
        TASK_RUN_PROPERTY: str(_TASK_RUN),
    }
    assert isinstance(marked, AddSnapshotUpdate)
    metadata = _table(marked.snapshot)
    for claim in (0, 1, 2):
        assert not written_by_attempt(metadata, since=None, task_run_id=_TASK_RUN, claim=claim)


def test_a_context_with_both_forms_stamps_nothing() -> None:
    forged = AddSnapshotUpdate(snapshot=_snapshot(5, None, **_stamp()))
    both = RunContext(task_run_id=_TASK_RUN, query_run_id=uuid.uuid4(), claim=2)
    (stripped,) = stamp_snapshots([forged], both)
    assert _summary(stripped) == {"operation": "append"}


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


def test_a_snapshot_without_a_summary_is_left_as_it_is() -> None:
    """A client's snapshot may omit the summary: there is nothing to strip, and none is invented."""
    bare = Snapshot(
        snapshot_id=5,
        parent_snapshot_id=None,
        sequence_number=5,
        timestamp_ms=1_700_000_000_005,
        manifest_list="file:///t/metadata/snap-5.avro",
        summary=None,
        schema_id=0,
    )
    update = AddSnapshotUpdate(snapshot=bare)
    for context in (None, RunContext(task_run_id=_TASK_RUN, claim=2)):
        (same,) = stamp_snapshots([update], context)
        assert same is update


def test_a_stamp_under_an_expired_parent_is_not_found() -> None:
    """Review N1: the walk ends at a parent that is no longer in the metadata."""
    metadata = _table(_snapshot(1, None, **_stamp()), _snapshot(3, 2))
    assert metadata.snapshot_by_id(2) is None
    assert not written_by_attempt(metadata, since=None, task_run_id=_TASK_RUN, claim=2)


def test_the_walk_gives_up_after_its_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """More foreign snapshots on top than the walk reads: the write is not proven."""
    monkeypatch.setattr(attempt_stamp, "MAX_ANCESTORS", 2)
    metadata = _table(
        _snapshot(1, None), _snapshot(2, 1, **_stamp()), _snapshot(3, 2), _snapshot(4, 3)
    )
    assert not written_by_attempt(metadata, since=1, task_run_id=_TASK_RUN, claim=2)
    monkeypatch.setattr(attempt_stamp, "MAX_ANCESTORS", 3)
    assert written_by_attempt(metadata, since=1, task_run_id=_TASK_RUN, claim=2)
