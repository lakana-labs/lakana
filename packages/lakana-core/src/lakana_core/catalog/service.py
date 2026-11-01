"""The catalog service.

Phase A implements environment management. Phase B adds namespaces and tables
to this same class.
"""

from __future__ import annotations

import contextlib
import json
import logging
import uuid
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

import pyarrow as pa
from pyiceberg.io import FileIO
from pyiceberg.partitioning import PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.table.metadata import TableMetadata
from pyiceberg.table.snapshots import Snapshot
from pyiceberg.table.sorting import SortOrder
from pyiceberg.table.update import (
    AddPartitionSpecUpdate,
    AddSchemaUpdate,
    AddSnapshotUpdate,
    AddSortOrderUpdate,
    AssertCurrentSchemaId,
    AssertRefSnapshotId,
    AssertTableUUID,
    AssignUUIDUpdate,
    RemovePartitionSpecsUpdate,
    RemovePropertiesUpdate,
    RemoveSchemasUpdate,
    RemoveSnapshotRefUpdate,
    RemoveSnapshotsUpdate,
    SetCurrentSchemaUpdate,
    SetDefaultSortOrderUpdate,
    SetDefaultSpecUpdate,
    SetLocationUpdate,
    SetSnapshotRefUpdate,
    TableRequirement,
    TableUpdate,
)
from pyiceberg.types import NestedField
from sqlalchemy import delete, event, null, or_, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import ORMExecuteState, Session, SessionTransaction

from lakana_core.artifacts.store import current_version, version_row
from lakana_core.audit.service import AuditService
from lakana_core.audit.views import View, ViewDetail, record_view, sessions_like
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.catalog.append import (
    AppendResult,
    build_transaction,
    cleanup_written_paths,
    new_data_file_paths,
    new_manifest_paths,
    strip_field_metadata,
    validate_arrow_schema,
)
from lakana_core.catalog.attempt_stamp import stamp_snapshots
from lakana_core.catalog.columns import sync_table_columns
from lakana_core.catalog.commit import commit_metadata_pointer
from lakana_core.catalog.connection_lineage import (
    ConnectionSource,
    connection_edges,
    connection_sources,
)
from lakana_core.catalog.datafiles import list_data_files
from lakana_core.catalog.descriptions import (
    KEEP,
    Keep,
    ShownDescription,
    check_label,
    check_text,
    edit_column,
    edit_table,
    edit_table_label,
    sync_created_table,
)
from lakana_core.catalog.environment_deletion import (
    DELETED_HINT_MESSAGE,
    ENVIRONMENT_GONE_MESSAGE,
    NAME_HELD_MESSAGE,
    NO_DELETED_MESSAGE,
    NOT_DELETED_MESSAGE,
    delete_rows,
    lock_live_environment,
    purge_early,
    restore_rows,
)
from lakana_core.catalog.external_location import check_claim_free, parse_external_location
from lakana_core.catalog.field_ids import check_added_files_have_field_ids
from lakana_core.catalog.health import (
    TableHealth,
    last_maintenance,
    latest_checks,
    readers,
    snapshot_stats,
)
from lakana_core.catalog.history import (
    EARLIEST_START,
    HISTORY_KEY_PROPERTY,
    RUN_WROTE_MESSAGE,
    UTC_MICROS,
    HistoryResult,
    check_first_versions_order,
    check_history_batch,
    check_history_table,
    check_starts,
    conform_batch,
    count_open_versions,
    first_versions,
    newest_run_start,
    plan_history,
    read_last_ends,
    read_open_versions,
    run_clock,
    stage_history,
    too_many_open_versions_message,
    version_starts,
    written_by_run,
)
from lakana_core.catalog.identity import artifact_refusal, resolve_table_id
from lakana_core.catalog.json_schema import ColumnSpec, from_iceberg_schema
from lakana_core.catalog.loads import require_bronze
from lakana_core.catalog.locations import (
    CLAIM_RELEASED_MESSAGE,
    claim_holder,
    claim_location,
    forget_location,
    lock_location,
    other_table_locations,
    publish_location,
    refuse_root_clash,
    release_claim,
)
from lakana_core.catalog.maintenance import (
    DEFAULT_KEEP_DAYS,
    DEFAULT_KEEP_LAST,
    DEFAULT_ORPHAN_AGE_HOURS,
    FAILED_SNAPSHOT_KEEP_DAYS,
    FAILED_SNAPSHOT_TAG_PREFIX,
    MaintenanceReport,
    MaintenanceStopped,
    check_orphan_age,
    has_foreign_reference,
    old_failed_tags,
    referenced_paths,
    select_expired_snapshots,
    select_orphans,
)
from lakana_core.catalog.merge import (
    SDK_ADVICE,
    MergeAdvice,
    MergeResult,
    check_merge_batch,
    stage_upsert,
)
from lakana_core.catalog.metadata import (
    apply_updates,
    build_table_metadata,
    check_requirements,
    next_metadata_location,
    read_metadata,
    write_metadata,
)
from lakana_core.catalog.model_metadata import (
    TableModelView,
    TableReferences,
    model_view,
    references_view,
)
from lakana_core.catalog.orphaned_locations import (
    AUDITED_LOCATIONS,
    DEFAULT_OLDER_THAN_DAYS,
    OrphanedLocations,
    delete_orphaned_locations,
    find_orphaned_locations,
    target,
)
from lakana_core.catalog.retention import (
    DEFAULT_EXPIRY_WARNING_DAYS,
    NOT_MANAGED_MESSAGE,
    PROTECTED_ENVIRONMENT_MESSAGE,
    PROTECTED_TABLE_MESSAGE,
    TableRetention,
    check_days,
    check_rule,
    describe_retention,
    describe_retentions,
    expiry_time,
    last_reads,
    lock_row,
    planned_expiry,
    retention_candidates,
)
from lakana_core.catalog.schema_compat import SchemaDiff, arrow_to_iceberg_schema, plan_schema
from lakana_core.catalog.schema_inference import infer_schema
from lakana_core.catalog.texts import LanguageCoverage, TextChooser, column_texts
from lakana_core.catalog.tombstones import (
    DEFAULT_UNDO_DAYS,
    FILES_GONE_MESSAGE,
    NAME_TAKEN_MESSAGE,
    NAMESPACE_GONE_MESSAGE,
    NO_TOMBSTONE_MESSAGE,
    PERIOD_PASSED_MESSAGE,
    PURGE_STARTED_MESSAGE,
    DroppedTable,
    drop_row,
    find_tombstone,
    list_tombstones,
    name_holder,
    refresh_restored,
    restore_policies,
    restore_table,
    revive,
)
from lakana_core.catalog.translations import edit_translation, lock_table, translation_rows
from lakana_core.catalog.usage import EnvironmentUsage, environment_usage
from lakana_core.catalog.watermark import Watermark, check_incremental_key, compute_watermark
from lakana_core.catalog.writes import (
    DataFileWriter,
    ExpectedSnapshot,
    LineageSource,
    SnapshotPin,
    WriteResult,
    check_sources,
    cleanup_location,
    expected_snapshot_value,
    max_rows_message,
    stage_snapshot,
    target_changed_message,
)
from lakana_core.config import DEFAULT_MAX_HISTORY_OPEN_VERSIONS, StorageConfig, ViewAuditConfig
from lakana_core.db import db_now, rowcount
from lakana_core.errors import (
    AlreadyExistsError,
    CheckedSnapshotGoneError,
    CheckResultGoneError,
    ConflictError,
    KeptAfterCheckError,
    LakanaError,
    NoCheckedSnapshotError,
    NotFoundError,
    NotPublishedError,
    PermissionDenied,
    StorageRefusedError,
    StorageUnavailableError,
    UnsupportedTableError,
    ValidationError,
    WriteOutcomeUnknownError,
)
from lakana_core.grants.admin import may_see_grants
from lakana_core.grants.revocation import revoke_attached
from lakana_core.identifiers import (
    NAMESPACE_SEPARATOR,
    NamespacePath,
    TableRef,
    refuse_percent_in_new_name,
    shown,
    validate_path_component,
)
from lakana_core.languages import DEFAULT_MAIN_LANGUAGE, canonical_language, language_problem
from lakana_core.log_events import LogEvent, log_event
from lakana_core.models import (
    LIVE_ENVIRONMENT,
    AuditEvent,
    AuditEventType,
    CheckOutcome,
    CheckResult,
    ColumnMask,
    Environment,
    EnvironmentDeployment,
    Grant,
    LineageEdge,
    Namespace,
    QueryRun,
    QueryRunStatus,
    Repository,
    RollbackOutcome,
    RowFilter,
    Table,
    TableLocationState,
    TableType,
    TaskRun,
    WriteMode,
)
from lakana_core.models.run import TERMINAL_TASK_STATUSES
from lakana_core.pagination import escape_like
from lakana_core.policy.resolver import PolicyResolver
from lakana_core.policy.schema_change import BrokenPolicy, broken_policies, changed_columns
from lakana_core.projects.layers import LAYERS
from lakana_core.projects.translations import MAX_LANGUAGES
from lakana_core.query_rewrite import ViewDefinition
from lakana_core.runs.checks import (
    CHECK_ROWS_NOT_FOR_A_TASK,
    check_result_gone_message,
    checked_snapshot_gone_message,
    no_checked_snapshot_message,
    not_published_message,
)
from lakana_core.runs.hidden_checks import ReferenceCheckFilter, result_hider
from lakana_core.runs.kept import (
    kept_message,
    refused_message,
    restore_kept,
    shown_kept_check,
    task_may_read,
)
from lakana_core.runs.lockdown import (
    OTHER_ASSET_MESSAGE,
    ProtectedWriteGuard,
    WriteOperation,
    table_target,
)
from lakana_core.runs.tasks import Written
from lakana_core.secrets.uses import (
    SecretBindingRefused,
    approved_storage_options,
    record_refusal,
    require_secret_management_for_storage_change,
)
from lakana_core.storage.base import StoredObject
from lakana_core.storage.resolver import (
    ResolvedStorage,
    StorageResolver,
    check_root_scheme,
    check_storage_root,
    is_id_suffixed_location,
    is_within_location,
    nested_locations_within,
    table_root_from_metadata_location,
)
from lakana_core.storage.unavailable import (
    ORPHANED_STORAGE_REFUSED,
    ORPHANED_STORAGE_UNREACHABLE,
    TABLE_STORAGE_UNREACHABLE,
    is_storage_unavailable,
    storage_failure,
)

logger = logging.getLogger(__name__)

_TRACKED_KEY = "lakana.catalog.tracks_writes"
"""`Session.info` key: the session carries the write tracker of `CatalogService._track_writes`."""

_WROTE_KEY = "lakana.catalog.transaction_wrote"
"""`Session.info` key: the session's transaction flushed or executed a write."""

WRITE_OUTCOME_UNKNOWN_MESSAGE = (
    "The write to {table} may or may not have been committed: the database did not confirm it, "
    "and asking it afterwards failed too. The files of the write were kept. Do not run the write "
    "again before you know: if it was committed, an append would add the rows twice. Read the "
    "table to see whether the rows are there ('lakana sql \"SELECT count(*) FROM {table}\"'), "
    "or look at the query ('lakana query status ID'). {leftovers}"
)
"""Raised as `WriteOutcomeUnknownError` (`{table}` is the dotted name, `{leftovers}` says what
stays behind)."""

LOCK_TIMEOUT_SECONDS = 5
"""How long the check after a failed commit waits for a transaction that is still moving the
table's pointer (Postgres); a longer wait makes the outcome unknown, never "not landed"."""


_MEDALLION_TOP_LEVEL_NAMESPACES = frozenset(LAYERS)
"""The only top-level namespaces an ICEBERG (managed-zone) table may live under.

Enforced only for ICEBERG tables, in `create_table` — `create_external_table` (raw
zone) does not call this. `create_namespace` stays unrestricted: this rule tracks a
table's declared zone (`table_type`), not the namespace's own name, so an arbitrary
namespace can still exist — it simply cannot hold an `ICEBERG` table unless its
top-level component is one of these three. """


def _not_exposed_artifact_message(dotted: str) -> str:
    """Word the refusal of the Iceberg REST surface to show an artifact.

    Parameters
    ----------
    dotted
        The artifact's dotted name.

    Returns
    -------
    str
        The message.
    """
    return (
        f"{dotted} is an artifact; artifacts have no Iceberg metadata and are not exposed over "
        "the Iceberg REST catalog."
    )


def _name_taken_message(existing: Table, name: str, namespace: NamespacePath) -> str:
    """Word the refusal to create or rename onto a name that is taken.

    Parameters
    ----------
    existing
        The row that holds the name.
    name
        The table name.
    namespace
        The namespace.

    Returns
    -------
    str
        For an artifact, the message says so and names the next step; for anything
        else the generic text.
    """
    if existing.table_type is TableType.ARTIFACT:
        return (
            f"{name!r} already exists in {namespace} as an artifact. Choose another name, or "
            f"drop the artifact first (`lakana artifact drop {namespace}.{name}`)."
        )
    return f"Table {name!r} already exists in {namespace}."


def _stale_reference_message(view: TableRef, reference: str) -> str:
    """Word a stale view's error when the reader may not learn what happened.

    One wording for a dropped reference and for one renamed to a name the
    reader can't read, so the message can't tell the two apart.

    Parameters
    ----------
    view
        The stale view.
    reference
        The name the view was created against.

    Returns
    -------
    str
        The message.
    """
    return (
        f"View {view.dotted()!r} references {reference!r}, which was dropped or replaced; "
        "re-create the view."
    )


@dataclass(frozen=True)
class PropertyUpdateSummary:
    """What an update to a namespace's properties actually changed.

    Parameters
    ----------
    updated
        Keys that were set.
    removed
        Keys that were present and have been removed.
    missing
        Keys requested for removal that were not present. The Iceberg REST
        specification reports these rather than failing the call.
    """

    updated: list[str]
    removed: list[str]
    missing: list[str]


def _present_removals(update: TableUpdate, properties: Mapping[str, str]) -> TableUpdate:
    """Leave out of a property removal the keys the table does not have.

    Parameters
    ----------
    update
        One update of a commit.
    properties
        The table's current properties.

    Returns
    -------
    TableUpdate
        `update` itself, unless it is a `RemovePropertiesUpdate`: then one that removes only
        the keys `properties` has.
    """
    if not isinstance(update, RemovePropertiesUpdate):
        return update
    return RemovePropertiesUpdate(removals=[key for key in update.removals if key in properties])


def _added_records(snapshot: Snapshot | None) -> int:
    """Read how many rows a snapshot added, from its summary.

    Parameters
    ----------
    snapshot
        The snapshot, as the committing client wrote it; untrusted.

    Returns
    -------
    int
        Its summary's ``added-records``; 0 when there is no summary, no such entry, or the
        entry is not a whole number of at least zero.
    """
    summary = None if snapshot is None else snapshot.summary
    value = None if summary is None else summary["added-records"]
    try:
        return max(int(value or 0), 0)
    except ValueError:
        return 0


MAX_EDIT_LANGUAGES = MAX_LANGUAGES
"""An edit may not add a language once a table has texts in this many besides its main one.

A deploy is bounded separately, by its 50 translation files (`MAX_LANGUAGES`); it does not
count the languages of edits."""


@dataclass(frozen=True)
class LoadedTable:
    """A table and the Iceberg metadata it currently points at.

    Parameters
    ----------
    ref
        How the table is addressed.
    uuid
        The Iceberg ``table-uuid``, which is also the catalog row's id.
    metadata_location
        Absolute URI of the current metadata file.
    metadata
        The parsed metadata.
    text_language
        The language of the table's project texts (its project's `main_language:`), when the
        row says; `None` for the instance's main language, or where the loader does not read it.
    """

    ref: TableRef
    uuid: uuid.UUID
    metadata_location: str
    metadata: TableMetadata
    text_language: str | None = None


@dataclass(frozen=True)
class LoadedAt:
    """A table and the one snapshot of it that a check result recorded, to be read.

    What `CatalogService.load_table_at_check` returns. It is deliberately **not** a
    `LoadedTable`: code that takes a loaded table reads its current snapshot and its current
    schema, and for this value both would be wrong (the rows a check found are in `snapshot_id`,
    and a table that is not published has no current snapshot at all). List the files with
    `data_files` and take the columns from `columns`.

    Parameters
    ----------
    ref
        How the table is addressed.
    metadata
        The table's parsed current metadata, which holds the snapshot. For the table's
        location and for `data_files`; its `current_snapshot_id` and `schema()` describe the
        table as it is now, not what is to be read.
    snapshot_id
        The snapshot to read; it is in `metadata.snapshots`.
    columns
        The fields of the schema the snapshot was written with, in order. A rollback sets the
        table's schema back, so the current schema can lack a column the snapshot's files
        have, and a reader under a row filter or a column mask is given exactly these.
    """

    ref: TableRef
    metadata: TableMetadata
    snapshot_id: int
    columns: tuple[NestedField, ...]

    def data_files(self, file_io: FileIO, *, is_valid_path: Callable[[str], bool]) -> list[str]:
        """List the data files of the snapshot to read, and of no other.

        Parameters
        ----------
        file_io
            Used to read the snapshot's manifest list and manifests.
        is_valid_path
            The check of where the manifest list and each manifest lie, as for
            `lakana_core.catalog.datafiles.list_data_files`. Required: this value is read for
            a caller's query, and a manifest outside the table's location must never be.

        Returns
        -------
        list of str
            Absolute URIs of the live data files of `snapshot_id`.
        """
        return list_data_files(
            self.metadata, file_io, is_valid_path=is_valid_path, snapshot_id=self.snapshot_id
        )


def _schema_of(metadata: TableMetadata, snapshot_id: int) -> Schema | None:
    """Find the schema a snapshot was written with.

    Parameters
    ----------
    metadata
        The table's metadata, which holds every schema the table still has.
    snapshot_id
        The snapshot.

    Returns
    -------
    Schema or None
        The schema the snapshot names. `None` when the metadata does not hold the snapshot,
        or the snapshot names no schema, or one the metadata does not hold: its columns are
        then not known, and the current schema's would be a guess.
    """
    snapshot = metadata.snapshot_by_id(snapshot_id)
    if snapshot is None:
        return None
    return next((s for s in metadata.schemas if s.schema_id == snapshot.schema_id), None)


@dataclass(frozen=True)
class TableDescription:
    """What the platform API shows for any kind of table.

    Parameters
    ----------
    ref
        How the table is addressed.
    id
        Its id.
    kind
        ``"TABLE"``, ``"EXTERNAL"``, ``"VIEW"`` or ``"ARTIFACT"``.
    metadata_location
        The current metadata file, for a managed table only.
    columns
        Its columns: the Iceberg schema, an external table's inferred
        schema, or a view's planned output.
    sql
        A view's SELECT. `None` if the caller may not read every reference,
        even if the view itself stores one.
    references
        A view's references, as ``(id, dotted name)`` pairs. Empty if the
        caller may not read every reference.
    hidden_references
        The number of the view's references the caller may not READ, and
        so cannot see named in `sql` or `references`. `0` for a table, an
        external table, or a view whose every reference the caller may
        read.
    snapshot_id
        The current snapshot of a managed table; `None` without one, and for
        a view or an external table.
    state
        ``"not_published"`` for a managed table whose first build did not finish, or
        failed a blocking check, and that no later run has published; ``"published"``
        for everything else, a view, an external table and an artifact included. ``"kept"``
        for a published managed table that keeps the data that failed a ``keep`` check
        (`Table.kept_run_id`); a table that is not published is never ``"kept"``.
    state_message
        Why it is not published (the text a read of it is refused with), or why it is kept;
        `None` when it is published.
    schema_id
        The id of the current schema of a managed table; `None` for a view, an external table
        and an artifact, which have no field ids.
    file_format
        An external table's stored file format (`"parquet"`, `"csv"`, `"json"`); `None` for
        every other kind.
    description
        The text the table shows, in the reader's language where it has one, else the main
        language's: its edit when it has one, else the project's; `None` for none.
    description_source
        ``"edited"`` or ``"project"``; `None` when there is no description.
    column_descriptions
        Column name → text, source and language, for the columns of `columns` that have a
        description.
    last_write_at
        When the current snapshot of a managed table was committed; `None` without one, and for
        every other kind.
    row_count
        The rows in the current snapshot's summary (``total-records``); `None` when missing.
    size_bytes
        The size of its data files (``total-files-size``); `None` when missing.
    file_count
        Its data files (``total-data-files``); `None` when missing.
    expires_at
        When a managed table's retention makes it expire; `None` when it never does, and for
        every other kind.
    expires_soon
        Whether that is within ``LAKANA_TABLES__EXPIRY_WARNING_DAYS``; never when that is 0.
    model
        The model the asset header declares (`kind:`, `grain:`, `key:`, `references:`), with
        only the references whose target the caller may read; `None` without one.
    label
        The label the table shows, chosen like `description`; `None` for none.
    label_source
        ``"edited"`` or ``"project"``; `None` when there is no label.
    label_language
        The language of `label`; `None` exactly when it is.
    description_language
        The language of `description`; `None` exactly when it is.
    column_labels
        Column name → label, source and language, for the columns that have a label.
    language
        The first language the reader asked for; the table's main language when none.
    main_language
        The table's main language: its own `text_language`, else the instance's.
    translations
        Per language with texts, how many of the table's main-language texts it has.
    """

    ref: TableRef
    id: uuid.UUID
    kind: str
    metadata_location: str | None
    columns: list[ColumnSpec]
    sql: str | None = None
    references: tuple[tuple[str, str], ...] = ()
    hidden_references: int = 0
    snapshot_id: int | None = None
    state: Literal["published", "not_published", "kept"] = "published"
    state_message: str | None = None
    schema_id: int | None = None
    file_format: str | None = None
    description: str | None = None
    description_source: Literal["project", "edited"] | None = None
    column_descriptions: dict[str, ShownDescription] = dataclass_field(default_factory=dict)
    last_write_at: datetime | None = None
    row_count: int | None = None
    size_bytes: int | None = None
    file_count: int | None = None
    expires_at: datetime | None = None
    expires_soon: bool = False
    model: TableModelView | None = None
    label: str | None = None
    label_source: Literal["project", "edited"] | None = None
    label_language: str | None = None
    description_language: str | None = None
    column_labels: dict[str, ShownDescription] = dataclass_field(default_factory=dict)
    language: str = DEFAULT_MAIN_LANGUAGE
    main_language: str = DEFAULT_MAIN_LANGUAGE
    translations: tuple[LanguageCoverage, ...] = ()


_NO_UNPUBLISHING = (
    "A commit cannot remove a table's current snapshot: readers would see an empty table. "
    "Drop the table, or write the rows it should hold."
)
"""Why a commit may not leave a table with no current snapshot."""

_NO_SIDE_DOOR = (
    "{table} is not published: its first build did not finish, or failed a check. A commit can "
    "publish it only with a new snapshot, not by pointing at an existing one. Run its pipeline "
    "again."
)
"""Why a commit may not publish a not-published table by pointing `main` at a snapshot."""

_ANSWER_WITHOUT_HISTORY = frozenset(
    {
        "format-version",
        "table-uuid",
        "location",
        "last-column-id",
        "last-sequence-number",
        "schemas",
        "current-schema-id",
        "partition-specs",
        "default-spec-id",
        "last-partition-id",
        "sort-orders",
        "default-sort-order-id",
        # The single-schema and single-spec fields of format version 1.
        "schema",
        "partition-spec",
    }
)
"""The metadata fields a commit's or a create's answer keeps for a caller who may not read.

What clients need to parse the answer as table metadata (pyiceberg and other Iceberg clients
require the schemas, specs and sort orders with their ids) and nothing of the table's
history. Left out:
`snapshots`, `refs`, `current-snapshot-id`, `snapshot-log`, `statistics`,
`partition-statistics`, `properties`, `metadata-log`, and `last-updated-ms`, the time of the
table's last change (pyiceberg 0.12 fills in the current time when it is missing). Leaving out
`metadata-log` deletes nothing on the client: pyiceberg 0.12 (`Catalog._delete_old_metadata`,
called from `Table._do_commit`) reads `write.metadata.delete-after-commit.enabled` from the
properties of the answer, not of the metadata it held before, and this answer has no properties,
so the setting reads as off.
"""

_CHANGED_SINCE_YOUR_METADATA = (
    "The table changed while this write was in progress, so it was not applied. Run the write "
    "again; an Iceberg client has to load the table again first, which needs READ on the table."
)
"""The text of a failed requirement for a caller who may not see the table's state.

pyiceberg's own text names the ref and the snapshot id it found, or that a ref is missing, so
it would answer a probe of the head or of a failed check's tag that the answer withholds. The
same text reaches an SDK append or replace that lost a race, so it says first that the write
can simply be run again.
"""

_UPDATE_DOES_NOT_FIT = (
    "The updates do not fit the table's current metadata, so nothing was applied. The details "
    "need READ on the table."
)
"""The text of a refused update for a caller who may not see the table's state.

pyiceberg's own texts print what they compared against: every snapshot for an unknown snapshot
id (with its manifest list and summary), a stored partition spec or sort order, a location; and
Lakana's own refusals say whether the table is published or has a current snapshot.
"""

_NO_SNAPSHOT_REMOVAL = (
    "An Iceberg commit cannot remove snapshots or the tag of a failed check: running tasks and "
    "kept tags still need them. Expire snapshots with `lakana table maintain`, which keeps "
    "what they need."
)
"""Why a commit that is not maintenance may not remove snapshots; the same for everyone.

It depends on nothing in the table, so it is also what a caller without READ is told.
"""

_EARLIER_SCHEMA_NEEDS_READ = (
    "Making an earlier schema, partition spec or sort order current needs READ on the table. "
    "Add a new one and make that one current, or ask for READ."
)
"""The refusal of a switch to a layout the commit did not add, without READ.

The updates are `set-current-schema`, `set-default-spec` and `set-default-sort-order`. The
answer to the commit then carries that earlier layout, with the names of columns the table has
since dropped (a partition field is named like its column). The text is the same whether or not
the id exists.
"""

_NO_FAILED_TAG_CHANGE = (
    "An Iceberg commit cannot set a ref named `lakana-check-failed-...`: Lakana keeps the "
    "snapshot of a failed check under such a name. Use another name for your own ref."
)
"""Why a commit may not set a ref named like the tag of a failed check; the same for everyone."""

_NO_SCHEMA_OR_SPEC_REMOVAL = (
    "Lakana keeps every schema, partition spec and sort order a table has had, because its "
    "snapshots and a set-back may need them. Leave them in place; an unused one costs nothing."
)
"""Why a commit may not remove a schema or a partition spec; the same for everyone.

A schema or spec nothing refers to yet is refused too: the rule is not about what refers to it.
"""

_KEPT_ADVICE_FOR_A_PERSON = (
    "If you are a person and not a pipeline task, read it with your own token, not a task token."
)
"""What a task token that is bound to no task is told in the refusal of a kept table."""

_UUID_CANNOT_CHANGE = "A table's UUID cannot change."
"""Why an `assign-uuid` that differs from the table's UUID is refused: its writes require it."""

_SCHEMA_ID_TAKEN = (
    "This schema change adds a schema under an id the table already uses, so nothing was "
    "applied. Load the table again and make the change on what the load gives; loading needs "
    "READ on the table."
)
"""The refusal of an `AddSchemaUpdate` whose schema id the table already has, for every caller.

pyiceberg's builder accepts it, and the `SetCurrentSchemaUpdate(-1)` that follows then finds
the older schema of that id first, so a dropped column would come back. A client that sees only
the current schema (a writer without READ) picks such an id after the table evolved back to an
earlier schema.
"""

_SPEC_ID_TAKEN = (
    "This partition spec change adds a partition spec under an id the table already uses, so "
    "nothing was applied. Load the table again and make the change on what the load gives; "
    "loading needs READ on the table."
)
"""The refusal of an `AddPartitionSpecUpdate` whose spec id the table already has, for every caller.

pyiceberg accepts one whose content is identical to the spec of that id. For a writer without
READ, success then tells whether its guess of an earlier spec (field names included) is right,
and a hit lets it make that spec the default again. A different content is refused as well, so
that the answer does not depend on the content.
"""

_SORT_ORDER_ID_TAKEN = (
    "This sort order change adds a sort order under an id the table already uses, so nothing "
    "was applied. Load the table again and make the change on what the load gives; loading "
    "needs READ on the table."
)
"""The same for an `AddSortOrderUpdate`."""

_KEPT_CHANGING = "the table's metadata kept changing"
"""`Rollback.reason` when the metadata pointer moved under both attempts."""

_BEFORE_IS_GONE = (
    "the snapshot the table had before this run no longer exists (table maintenance expired it)"
)
"""`Rollback.reason` when the snapshot to go back to was expired."""

ROLLBACK_REFUSED_REASON = "the database refused or interrupted the rollback"
"""`Rollback.reason` when the database failed and the table was found NOT to have been set back."""

ROLLBACK_NOT_KNOWN_REASON = (
    "the rollback was interrupted and its outcome could not be read back; whether it took "
    "effect is not known: look at the table with `lakana table get`"
)
"""`Rollback.reason` when the rollback was interrupted and what became of it cannot be read.

Nothing is recorded for this outcome: the table may or may not hold the run's data.
"""

_STORAGE_FAILED = "the table's metadata could not be read or written: {error}"
"""`Rollback.reason` when object storage failed during a rollback (never the database's wording)."""

CHANGED_BY_ANOTHER_REASON = "another writer changed it during this run"
"""`Rollback.reason` of `changed_since` when the task run has no write of its own to set back."""

_DROPPED = "the table was dropped while the rollback was prepared"
"""`Rollback.reason` of `changed_since` when the table is gone."""

_LANDED_OUTCOMES = frozenset(
    o.value for o in (RollbackOutcome.ROLLED_BACK, RollbackOutcome.UNPUBLISHED)
)
"""The outcomes whose audit event says that the rollback's commit landed."""

_COMMITTED_BY_THE_ROLLBACK = frozenset({RollbackOutcome.ROLLED_BACK, RollbackOutcome.UNPUBLISHED})
"""The outcomes whose audit event, results and stamp land in the rollback's own commit."""


class _RollbackReason(LakanaError):
    """A failure of a rollback whose text this module worded itself.

    Only such a text becomes `Rollback.reason`, which a task's message quotes to whoever may
    read the run. Any other error's text stays in the worker's log: it may name a storage
    location.
    """


@contextlib.contextmanager
def _storage_errors_as_lakana(active: bool) -> Generator[None]:
    """Turn an object-storage `OSError` into a failure of the rollback with a worded reason.

    The rollback treats a raw `OSError` as a database failure (a driver raises one after a
    server-side cut); storage errors are `OSError`s too, and must not be mistaken for it.

    Parameters
    ----------
    active
        Convert only for a rollback; any other commit lets the error through unchanged.

    Yields
    ------
    None
        While the storage call runs.

    Raises
    ------
    _RollbackReason
        With `_STORAGE_FAILED` and the error's class name (never its text: it may name a path).
    OSError
        Unchanged, when `active` is false.
    """
    try:
        yield
    except OSError as exc:
        if not active:
            raise
        raise _RollbackReason(_STORAGE_FAILED.format(error=type(exc).__name__)) from exc


@contextlib.contextmanager
def _arrow_memory_returned() -> Generator[None]:
    """Hand the Arrow pool's freed pages back to the system once a write is over.

    pyarrow's default pool keeps the pages a large write freed; in a worker process, which
    its memory watchdog cannot end on its own, they would count against every later query.

    Yields
    ------
    None
        While the write runs; the pages are returned whether or not it succeeds.
    """
    try:
        yield
    finally:
        pa.default_memory_pool().release_unused()


def failed_snapshot_tag(run_id: uuid.UUID) -> str:
    """Name the tag that keeps the snapshot a run's failed check saw.

    Parameters
    ----------
    run_id
        The run whose check failed.

    Returns
    -------
    str
        ``lakana-check-failed-<run id>``.
    """
    return f"{FAILED_SNAPSHOT_TAG_PREFIX}{run_id}"


def _is_maintenance_update(update: TableUpdate) -> bool:
    """Tell whether a maintenance commit may carry an update.

    Parameters
    ----------
    update
        The update.

    Returns
    -------
    bool
        `True` for snapshot expiry and for removing a ref named like the tag of a failed check;
        no other ref, `main` included, is removed by maintenance.
    """
    return isinstance(update, RemoveSnapshotsUpdate) or (
        isinstance(update, RemoveSnapshotRefUpdate)
        and update.ref_name.startswith(FAILED_SNAPSHOT_TAG_PREFIX)
    )


def _refusal_before_lookup(update: TableUpdate) -> str | None:
    """Name the refusal of an update that no Iceberg commit may carry, if there is one.

    These updates change what Lakana relies on: the snapshots and tags that running
    tasks and failed checks still need, and the schemas and partition specs those snapshots
    refer to. The text depends on nothing in the table.

    Parameters
    ----------
    update
        The update.

    Returns
    -------
    str or None
        The refusal's text, or `None` for an update this check lets through.
    """
    if _is_maintenance_update(update):
        return _NO_SNAPSHOT_REMOVAL
    if isinstance(update, SetSnapshotRefUpdate) and update.ref_name.startswith(
        FAILED_SNAPSHOT_TAG_PREFIX
    ):
        return _NO_FAILED_TAG_CHANGE
    if isinstance(update, (RemoveSchemasUpdate, RemovePartitionSpecsUpdate)):
        return _NO_SCHEMA_OR_SPEC_REMOVAL
    return None


def _layouts_added(update: TableUpdate) -> set[tuple[str, int]]:
    """Name the schema, partition spec or sort order that an update adds.

    Parameters
    ----------
    update
        The update.

    Returns
    -------
    set
        One ``(kind, id)`` pair, the kind being ``schema``, ``spec`` or ``order``; empty for
        any other update.
    """
    if isinstance(update, AddSchemaUpdate):
        return {("schema", update.schema_.schema_id)}
    if isinstance(update, AddPartitionSpecUpdate):
        return {("spec", update.spec.spec_id)}
    if isinstance(update, AddSortOrderUpdate):
        return {("order", update.sort_order.order_id)}
    return set()


def _layout_switch(update: TableUpdate) -> tuple[str, int] | None:
    """Name the schema, partition spec or sort order that an update makes current.

    Parameters
    ----------
    update
        The update.

    Returns
    -------
    tuple or None
        The ``(kind, id)`` pair, in the form `_layouts_added` gives; an id of ``-1`` means the
        last one added. `None` for any other update.
    """
    if isinstance(update, SetCurrentSchemaUpdate):
        return ("schema", update.schema_id)
    if isinstance(update, SetDefaultSpecUpdate):
        return ("spec", update.spec_id)
    if isinstance(update, SetDefaultSortOrderUpdate):
        return ("order", update.sort_order_id)
    return None


def _bound_to_own_asset(p: PrincipalRef, ref: TableRef) -> bool:
    """Tell whether `p` is a task attempt's bound token and `ref` is that task's own asset.

    The names are compared as dotted strings, which a legacy name with a dot inside one of its
    parts could make equal for two tables; `WRITE_TABLE`, authorized first, compares the parts
    and refuses the other table.

    Parameters
    ----------
    p
        The acting principal.
    ref
        The table.

    Returns
    -------
    bool
        `True` only for a bound token on its own asset.
    """
    scope = p.task_scope
    bound = None if scope is None else scope.bound
    return bound is not None and bound.asset == ref.dotted()


@dataclass(frozen=True)
class Rollback:
    """What `CatalogService.roll_back` did.

    Parameters
    ----------
    outcome
        What happened to the table.
    reason
        For `RollbackOutcome.FAILED`, why the table could not be set back; otherwise `None`.
    """

    outcome: RollbackOutcome
    reason: str | None = None


_COMMIT_ACTIONS = frozenset({Action.WRITE_TABLE, Action.MAINTAIN_TABLE})
"""The actions `commit_table` may authorize a commit under."""


@dataclass(frozen=True)
class _PreparedTable:
    """A new managed table, authorized and validated, with nothing written yet.

    Parameters
    ----------
    ref
        Where the table will live.
    table_id
        Its id, which is also its Iceberg ``table-uuid``.
    namespace_id
        The owning namespace's id.
    environment_id
        The owning environment's id.
    location
        Its id-suffixed location.
    resolved
        The environment's resolved storage.
    metadata
        Its initial metadata.
    """

    ref: TableRef
    table_id: uuid.UUID
    namespace_id: uuid.UUID
    environment_id: uuid.UUID
    location: str
    resolved: ResolvedStorage
    metadata: TableMetadata


def _not_an_artifact_source(name: str) -> str:
    """Phrase the refusal of a version named for something that is not an artifact source.

    Parameters
    ----------
    name
        The dotted name the caller attached a version to.

    Returns
    -------
    str
        The message.
    """
    return f"source_version names {name}, which is not an artifact among the sources."


class CatalogService:
    """Domain operations over the catalog.

    Parameters
    ----------
    session
        Database session. Each method commits internally on success, so the
        caller does not own the transaction: a `CatalogService` call is a
        complete unit of work by itself, not one step of a larger one the
        caller assembles.
    storage
        Resolver used to reach an environment's storage.
    authz
        Authorization service consulted before every operation.
    undo_days
        `LAKANA_TABLES__UNDO_DAYS`: how many days a dropped managed table keeps its files and
        can be restored (`undrop_table`). 0 makes every drop due at the next sweep.
    expiry_warning_days
        `LAKANA_TABLES__EXPIRY_WARNING_DAYS`: a table that expires within this many days is
        described as expiring soon; 0 never describes one so.
    view_audit
        `LAKANA_VIEW_AUDIT__*`: whether and how often seeing the readers of a table is recorded
        as `ACTIVITY_VIEWED`. `None` (the default) is the settings' default: on, with a window
        of 60 minutes.
    main_language
        `LAKANA_MAIN_LANGUAGE`: the language of the texts of a table whose project declares none.
    """

    def __init__(
        self,
        session: Session,
        storage: StorageResolver,
        authz: AuthorizationService,
        *,
        undo_days: int = DEFAULT_UNDO_DAYS,
        expiry_warning_days: int = DEFAULT_EXPIRY_WARNING_DAYS,
        view_audit: ViewAuditConfig | None = None,
        main_language: str = DEFAULT_MAIN_LANGUAGE,
    ) -> None:
        self._session = session
        self._main_language = main_language
        self._storage = storage
        self._authz = authz
        self._undo_days = undo_days
        self._expiry_warning_days = expiry_warning_days
        self._view_audit = view_audit or ViewAuditConfig()
        self._guard = ProtectedWriteGuard(session, authz)
        self._commit_sent = False
        self._track_writes()

    def create_environment(
        self,
        p: PrincipalRef,
        *,
        name: str,
        storage: StorageConfig,
        is_protected: bool = False,
        properties: Mapping[str, str] | None = None,
    ) -> Environment:
        """Create an environment.

        Parameters
        ----------
        p
            The acting principal.
        name
            Unique environment name, used as the Iceberg REST path prefix.
        storage
            Where this environment's data lives.
        is_protected
            Whether deleting this environment and changing its storage are
            refused, as they are in production, until cleared via
            `set_environment_protection`.
        properties
            Free-form metadata.

        Returns
        -------
        Environment
            The created environment. The three top-level namespaces
            `bronze`, `silver`, and `gold` are created in the same
            transaction — see `create_table`'s naming enforcement, which
            this makes usable with zero setup.

        Raises
        ------
        AlreadyExistsError
            If an environment with that name already exists, also a deleted one whose files
            are kept (`NAME_HELD_MESSAGE` names the restore and the early purge).
        ValidationError
            If the storage root is in another scheme than `storage.scheme`, aliases such as
            ``s3a`` mapped (`check_root_scheme`; the message names both schemes and a root to
            use), not written canonically (`check_storage_root`; the message
            names the spelling to use), or it lies at or inside another environment's
            ``managed/``, ``raw/``, ``artifacts/`` or ``_artifacts/``, deleted environments
            included, or one of its own such directories holds another environment's root
            (`refuse_root_clash`; the message names that environment and a root to use). A
            shared root and one nested elsewhere in another root are accepted.
        PermissionDenied
            If the principal may not manage environments.

        Notes
        -----
        Authorized at instance scope, not against the not-yet-existing
        environment's own name: an environment-scoped
        `MANAGE_ENVIRONMENT` grant for a name that doesn't exist yet used to
        let its holder pre-emptively create — and so control the storage
        root of — an environment nobody meant to delegate to them.
        """
        self._authz.authorize(p, Action.MANAGE_ENVIRONMENT, Securable.instance_scope())
        check_root_scheme(storage.root, storage.scheme)
        check_storage_root(storage.root)
        existing = self._session.execute(
            select(Environment).where(Environment.name == name)
        ).scalar_one_or_none()
        if existing is not None and existing.deleted_at is not None:
            assert existing.purge_after is not None  # nosec B101 -- set with deleted_at
            raise AlreadyExistsError(
                NAME_HELD_MESSAGE.format(
                    name=name, purge_after=existing.purge_after.isoformat(timespec="seconds")
                )
            )
        if existing is not None:
            raise AlreadyExistsError(f"Environment {name!r} already exists.")
        refuse_root_clash(self._session, storage.root, name)
        env = Environment(
            id=uuid.uuid4(),
            name=name,
            storage_root=storage.root,
            storage_options=dict(storage.options),
            is_protected=is_protected,
            properties=dict(properties or {}),
        )
        self._session.add(env)
        # The scaffold commits with the environment: an environment
        # exists with all three layers or not at all. `MANAGE_ENVIRONMENT` at
        # instance scope, checked above, covers it; there is no separate
        # `CREATE_NAMESPACE` check that could fail after the environment row.
        for layer in ("bronze", "silver", "gold"):
            self._session.add(
                Namespace(
                    id=uuid.uuid4(),
                    environment_id=env.id,
                    path=NamespacePath.from_parts([layer]).joined(),
                    properties={},
                )
            )
        AuditService(self._session).record(
            p.id,
            AuditEventType.ENVIRONMENT_CREATED,
            securable=Securable.environment_scope(name),
            detail={"storage_root": storage.root, "is_protected": is_protected},
        )
        self._session.commit()
        return env

    def get_environment(self, p: PrincipalRef, name: str) -> Environment:
        """Fetch an environment by name.

        Parameters
        ----------
        p
            The acting principal.
        name
            Environment name.

        Returns
        -------
        Environment
            The environment.

        Raises
        ------
        NotFoundError
            If no environment has that name, also when it is deleted; the message names the
            restore when `p` may make it (`DELETED_HINT_MESSAGE`).
        PermissionDenied
            If the principal may not read environments.
        """
        self._authz.authorize(p, Action.READ_ENVIRONMENT, Securable.environment_scope(name))
        return self._find_environment(name, p)

    def list_environments(self, p: PrincipalRef) -> list[Environment]:
        """List every environment `p` may read.

        Parameters
        ----------
        p
            The acting principal.

        Returns
        -------
        list of Environment
            Every environment `p` may read, in creation order: all of them
            for an instance-scoped grant, only its own for a task token,
            none for a principal with no grants.

        Notes
        -----
        Filtered through `permits`, not `authorize`. A hidden
        environment is not a denied action and writes no audit event.
        """
        rows = self._session.execute(
            select(Environment).where(LIVE_ENVIRONMENT).order_by(Environment.created_at)
        )
        return [
            env
            for env in rows.scalars()
            if self._authz.permits(
                p, Action.READ_ENVIRONMENT, Securable.environment_scope(env.name)
            )
        ]

    def delete_environment(self, p: PrincipalRef, name: str, *, purge: bool = False) -> Environment:
        """Delete an environment: its tables become tombstones for the undo period.

        In one transaction under the environment's row lock
        (`lakana_core.catalog.environment_deletion.delete_rows`): every managed table is dropped
        through the drop path (``reason: environment_deleted``), so its files stay until
        ``purge_after`` (now plus `undo_days`) and the grants, row filters and column masks
        bound to it are revoked onto its tombstone; views and external tables are dropped
        likewise and leave no tombstone (external files are never deleted); what is still bound
        to the environment is revoked and listed on the row, and task tokens scoped to it are
        revoked. The row is then marked deleted: hidden from every lookup by name, its name
        still taken, and kept with the storage settings the location sweep needs to delete the
        files once ``purge_after`` has passed. `restore_environment` undoes it until then; the
        sweep deletes the row after the last file.

        Deletion is authorized on its own behalf, against ``MANAGE_ENVIRONMENT``: it must not be
        satisfiable merely by borrowing the weaker ``READ_ENVIRONMENT`` check that
        :meth:`get_environment` performs.

        A protected environment (`Environment.is_protected`) refuses deletion outright.

        An environment that still has a deployed repository refuses deletion: a run start locks
        the deployment pointer and then the environment row, and a deletion locks the row
        first, so the two could deadlock. The environment row is locked first (and re-read, so
        protection is checked on the locked row, never a stale copy), then the pointers are
        looked at without locking, which removes the cycle; undeploy first.

        Parameters
        ----------
        p
            The acting principal.
        name
            Environment name.
        purge
            Make everything due now: the next sweep deletes the files and then the row, and the
            environment cannot be restored. On an environment that is already deleted, this is
            all the call does, to it and to every tombstone it has.

        Returns
        -------
        Environment
            The deleted environment, with its ``purge_after``.

        Raises
        ------
        NotFoundError
            If no environment has that name (also when a concurrent delete wins the row lock),
            or it is already deleted and `purge` is false.
        ValidationError
            If the environment is protected.
        ConflictError
            If a repository is still deployed to the environment; the message names them.
        PermissionDenied
            If the principal may not manage environments.
        """
        self._authz.authorize(p, Action.MANAGE_ENVIRONMENT, Securable.environment_scope(name))
        env = self._lock_environment_row(name)
        if env is None:
            self._session.rollback()
            raise NotFoundError(f"No environment named {name!r}.")
        now = db_now(self._session)
        if env.deleted_at is not None:
            if not purge:
                message = self._missing_environment_message(p, env)
                self._session.rollback()
                raise NotFoundError(message)
            detail = purge_early(self._session, env, now=now)
        else:
            if env.is_protected:
                self._session.rollback()
                raise ValidationError(f"Environment {name!r} is protected.")
            deployed = self._session.scalars(
                select(Repository.name)
                .join(EnvironmentDeployment, EnvironmentDeployment.repository_id == Repository.id)
                .where(EnvironmentDeployment.environment_id == env.id)
                .order_by(Repository.name)
            ).all()
            if deployed:
                self._session.rollback()
                raise ConflictError(
                    f"Environment {name!r} still has deployed repositories: "
                    f"{', '.join(deployed)}. Undeploy each first (`lakana undeploy REPO --env "
                    f"{name}`)."
                )
            purge_after = now if purge else now + timedelta(days=self._undo_days)
            detail = delete_rows(
                self._session, p, env, now=now, purge_after=purge_after, purge=purge
            )
        AuditService(self._session).record(
            p.id,
            AuditEventType.ENVIRONMENT_DELETED,
            securable=Securable.environment_scope(name),
            detail=detail,
        )
        self._session.commit()
        return env

    def restore_environment(self, p: PrincipalRef, name: str) -> Environment:
        """Undo the deletion of an environment within its undo period.

        Under the environment's row lock and the lock of every tombstone the deletion made
        (`lakana_core.catalog.environment_deletion.restore_rows`): the tables it dropped come
        back with the same ids and metadata, with exactly the grants, row filters and column
        masks it revoked, and the row is no longer deleted. A table dropped before the deletion
        stays a tombstone, which `undrop_table` restores afterwards. One `TABLE_UNDROPPED` per
        table and one `ENVIRONMENT_RESTORED` record it.

        Parameters
        ----------
        p
            The acting principal; needs ``MANAGE_ENVIRONMENT``, the right of the deletion.
        name
            Environment name.

        Returns
        -------
        Environment
            The restored environment.

        Raises
        ------
        NotFoundError
            If no environment of that name exists, deleted or not (`NO_DELETED_MESSAGE`).
        ConflictError
            If it is not deleted (`NOT_DELETED_MESSAGE`), its undo period has ended, the sweep
            has started deleting one of its tables' files, or a table's metadata file is gone.
        PermissionDenied
            If the principal may not manage environments.
        """
        self._authz.authorize(p, Action.MANAGE_ENVIRONMENT, Securable.environment_scope(name))
        env = self._lock_environment_row(name)
        if env is None:
            self._session.rollback()
            raise NotFoundError(NO_DELETED_MESSAGE.format(name=name))
        if env.deleted_at is None:
            self._session.rollback()
            raise ConflictError(NOT_DELETED_MESSAGE.format(name=name))
        resolved = self._storage.for_environment(name, include_deleted=True)

        def file_exists(location: str) -> bool:
            """Say whether a metadata file is still in the environment's storage.

            Parameters
            ----------
            location
                The metadata location.

            Returns
            -------
            bool
                Whether it exists.
            """
            return bool(resolved.file_io.new_input(location).exists())

        try:
            detail = restore_rows(
                self._session, p, env, now=db_now(self._session), file_exists=file_exists
            )
        except ConflictError:
            self._session.rollback()
            raise
        AuditService(self._session).record(
            p.id,
            AuditEventType.ENVIRONMENT_RESTORED,
            securable=Securable.environment_scope(name),
            detail=detail,
        )
        self._session.commit()
        return env

    def list_deleted_environments(self, p: PrincipalRef) -> list[Environment]:
        """List the deleted environments `p` may read, whose files are not deleted yet.

        Parameters
        ----------
        p
            The acting principal.

        Returns
        -------
        list of Environment
            Oldest deletion first, each with its ``deleted_at`` and ``purge_after``. Filtered
            through `permits` as `list_environments` is.
        """
        rows = self._session.execute(
            select(Environment)
            .where(Environment.deleted_at.is_not(None))
            .order_by(Environment.deleted_at, Environment.name)
        )
        return [
            env
            for env in rows.scalars()
            if self._authz.permits(
                p, Action.READ_ENVIRONMENT, Securable.environment_scope(env.name)
            )
        ]

    def environment_usage(self, p: PrincipalRef, name: str) -> EnvironmentUsage:
        """Report how much storage an environment uses, from the catalog database alone.

        Reads nothing from object storage (`lakana_core.catalog.usage`): the sizes of managed
        tables are those the worker's housekeeping measured.

        Parameters
        ----------
        p
            The acting principal; needs ``MANAGE_ENVIRONMENT`` on the environment, because the
            figures cover tables and dropped tables the caller may not be allowed to see.
        name
            Environment name.

        Returns
        -------
        EnvironmentUsage
            Its tables, dropped tables, artifacts and code archives.

        Raises
        ------
        PermissionDenied
            If the principal may not manage the environment.
        NotFoundError
            If no environment has that name, or it is deleted.
        """
        self._authz.authorize(p, Action.MANAGE_ENVIRONMENT, Securable.environment_scope(name))
        return environment_usage(self._session, self._find_environment(name, p))

    def find_orphaned_locations(
        self, p: PrincipalRef, name: str, *, older_than_days: int = DEFAULT_OLDER_THAN_DAYS
    ) -> OrphanedLocations:
        """List the table directories under an environment's ``managed/`` that no record names.

        Directories older releases left behind (`lakana_core.catalog.orphaned_locations`).
        Deletes nothing and records no audit event. A deleted environment is accepted: its
        leftovers are what this is for.

        Parameters
        ----------
        p
            The acting principal; needs ``MANAGE_ENVIRONMENT`` on the instance, because the walk
            sees the files of every environment that shares the storage root.
        name
            Environment name, deleted or not.
        older_than_days
            A directory whose newest file is younger is listed as recent.

        Returns
        -------
        OrphanedLocations
            The directories, oldest found first; ``complete`` is false when the time limit
            stopped the walk.

        Raises
        ------
        PermissionDenied
            If the principal may not manage environments on the instance.
        ValidationError
            If `older_than_days` is not 1 to 3650.
        NotFoundError
            If no environment has that name.
        StorageRefusedError
            If the storage refused the environment's credentials (`_orphaned`).
        StorageUnavailableError
            If the storage did not answer.
        """
        self._authz.authorize(p, Action.MANAGE_ENVIRONMENT, Securable.instance_scope())
        return self._orphaned(name, older_than_days, find_orphaned_locations)

    def delete_orphaned_locations(
        self, p: PrincipalRef, name: str, *, older_than_days: int = DEFAULT_OLDER_THAN_DAYS
    ) -> OrphanedLocations:
        """Delete the table directories without a record whose newest file is old enough.

        Each one is checked again, reserved with a record, and purged under that record's lock
        as the location sweep purges (`lakana_core.catalog.orphaned_locations`). One
        `ORPHANED_LOCATIONS_DELETED` event records the call, also when nothing was deleted.

        Parameters
        ----------
        p
            The acting principal; needs ``MANAGE_ENVIRONMENT`` on the instance.
        name
            Environment name, deleted or not.
        older_than_days
            Only directories whose newest file is older are deleted.

        Returns
        -------
        OrphanedLocations
            Each directory, deleted or with the reason it was kept; ``complete`` is false when
            the time limit stopped the call, and the next call continues.

        Raises
        ------
        PermissionDenied
            If the principal may not manage environments on the instance.
        ValidationError
            If `older_than_days` is not 1 to 3650.
        NotFoundError
            If no environment has that name.
        StorageRefusedError, StorageUnavailableError
            If the storage refused the credentials or did not answer during the walk, which
            comes before any deletion; nothing was deleted.
        """
        self._authz.authorize(p, Action.MANAGE_ENVIRONMENT, Securable.instance_scope())
        report = self._orphaned(name, older_than_days, delete_orphaned_locations)
        deleted = [entry for entry in report.locations if entry.deleted]
        AuditService(self._session).record(
            p.id,
            AuditEventType.ORPHANED_LOCATIONS_DELETED,
            securable=Securable.environment_scope(name),
            detail={
                "environment": name,
                "older_than_days": older_than_days,
                "count": len(deleted),
                "files": sum(entry.files_deleted for entry in report.locations),
                "bytes": sum(entry.bytes_deleted for entry in report.locations),
                "locations": [entry.location for entry in deleted[:AUDITED_LOCATIONS]],
                "more_locations": max(0, len(deleted) - AUDITED_LOCATIONS),
                "kept": len(report.locations) - len(deleted),
                "complete": report.complete,
            },
        )
        self._session.commit()
        return report

    def _orphaned(
        self,
        name: str,
        older_than_days: int,
        work: Callable[..., OrphanedLocations],
    ) -> OrphanedLocations:
        """Check the age, find the environment, and run a listing or a deletion.

        The caller has authorized the principal first.

        Parameters
        ----------
        name
            Environment name, deleted or not.
        older_than_days
            The age asked for.
        work
            `find_orphaned_locations` or `delete_orphaned_locations`.

        Returns
        -------
        OrphanedLocations
            What `work` returned.

        Raises
        ------
        StorageRefusedError
            If the storage refused the environment's credentials while the directories were
            walked (`ORPHANED_STORAGE_REFUSED`, log code `storage_refused`).
        StorageUnavailableError
            If the storage did not answer while the directories were walked
            (`ORPHANED_STORAGE_UNREACHABLE`, log code `storage_unreachable`). The walk comes
            before any deletion, so nothing was deleted either way, and the log line has the
            error's class, never its text, which can name a host or a path.
        Exception
            Any other error (a missing file, for one), as it was raised (`storage_failure`).
        """
        check_days(older_than_days, "older_than_days")
        environment = target(self._session, name)
        resolved = self._storage.for_environment(name, include_deleted=True)
        now = db_now(self._session)
        self._session.rollback()
        try:
            return work(
                self._session,
                resolved.storage,
                environment=environment,
                now=now,
                older_than_days=older_than_days,
            )
        except Exception as exc:
            failure = storage_failure(exc)
            if failure is None:
                raise
            self._session.rollback()
            refused = failure == "refused"
            code = LogEvent.STORAGE_REFUSED if refused else LogEvent.STORAGE_UNREACHABLE
            logger.warning(
                "The storage of environment %s %s while its orphaned locations were walked (%s).",
                shown(name),
                "refused access" if refused else "could not be reached",
                type(exc).__name__,
                extra=log_event(code),
            )
            if refused:
                raise StorageRefusedError(
                    ORPHANED_STORAGE_REFUSED.format(environment=shown(name))
                ) from None
            raise StorageUnavailableError(
                ORPHANED_STORAGE_UNREACHABLE.format(environment=shown(name))
            ) from None

    def _lock_environment_row(self, name: str) -> Environment | None:
        """Lock an environment's row by name, deleted or not, and read it again.

        Parameters
        ----------
        name
            The environment's name.

        Returns
        -------
        Environment or None
            The locked row; `None` when no row has the name.
        """
        return self._session.execute(
            select(Environment)
            .where(Environment.name == name)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()

    def _missing_environment_message(self, p: PrincipalRef | None, env: Environment) -> str:
        """Word the `NotFoundError` of a deleted environment.

        Parameters
        ----------
        p
            The caller, when known.
        env
            The deleted environment.

        Returns
        -------
        str
            `DELETED_HINT_MESSAGE` for a deleted environment the caller may restore; otherwise
            the message of a missing one, so the deletion is not disclosed.
        """
        if (
            env.purge_after is None
            or p is None
            or not self._authz.permits(
                p, Action.MANAGE_ENVIRONMENT, Securable.environment_scope(env.name)
            )
        ):
            return f"No environment named {env.name!r}."
        return DELETED_HINT_MESSAGE.format(
            name=env.name, purge_after=env.purge_after.isoformat(timespec="seconds")
        )

    def update_environment_storage(
        self,
        p: PrincipalRef,
        name: str,
        *,
        options: Mapping[str, str] | None = None,
        secret_ref: str | None = None,
    ) -> Environment:
        """Update an environment's storage options and/or secret reference.

        Resolves the create-environment/create-secret ordering problem: a
        secret must reference an already-existing environment, so an
        environment needing secret-backed storage is created first (with
        `storage_secret_ref` left unset), then its secret is created, then
        this method attaches the reference.

        `options`/`secret_ref` are each "leave unchanged" when `None`, not
        "clear this field" — there is no way to un-set `storage_secret_ref`
        back to `None` through this method.

        Parameters
        ----------
        p
            The acting principal.
        name
            Environment name.
        options
            Replaces `storage_options` entirely if given.
        secret_ref
            Replaces `storage_secret_ref` if given.

        Returns
        -------
        Environment
            The updated environment.

        Raises
        ------
        NotFoundError
            If no environment has that name.
        ValidationError
            If the environment is protected.
        PermissionDenied
            If the principal may not manage environments, or may not manage
            secrets in this one when `secret_ref` is given.
        SecretBindingRefused
            (A `PermissionDenied`.) If `options` would change the options of
            an environment that has a storage secret and the principal may not
            manage secrets in it: the options decide where the secret is
            presented. Unchanged options need no extra right. The refusal is
            audited as `AUTHORIZATION_DENIED`.

        Notes
        -----
        `MANAGE_ENVIRONMENT` is checked at instance scope, not against this
        environment's own name: an environment-scoped
        grant used to be enough to repoint an environment's storage root at
        an attacker-controlled location, which is exactly the privilege a
        merely-environment-scoped principal must not have. `MANAGE_SECRET`
        stays environment-scoped, unchanged.
        """
        self._authz.authorize(p, Action.MANAGE_ENVIRONMENT, Securable.instance_scope())
        if secret_ref is not None:
            self._authz.authorize(p, Action.MANAGE_SECRET, Securable.environment_scope(name))
        env = self._lock_unprotected_environment(name)
        if options is not None:
            if dict(options) != env.storage_options:
                try:
                    require_secret_management_for_storage_change(self._authz, p, env)
                except SecretBindingRefused as refusal:
                    self._session.rollback()
                    record_refusal(self._authz, p, refusal)
                    raise
            env.storage_options = dict(options)
        if secret_ref is not None:
            env.storage_secret_ref = secret_ref
        AuditService(self._session).record(
            p.id,
            AuditEventType.ENVIRONMENT_STORAGE_UPDATED,
            securable=Securable.environment_scope(name),
            detail={
                "options_keys": sorted(options.keys()) if options is not None else None,
                "secret_ref_set": secret_ref is not None,
                **(
                    {"approved_options": approved_storage_options(env.storage_options)}
                    if secret_ref is not None
                    else {}
                ),
            },
        )
        self._session.commit()
        return env

    def set_environment_protection(
        self, p: PrincipalRef, name: str, protected: bool
    ) -> Environment:
        """Set or clear an environment's protected flag.

        A protected environment refuses `delete_environment` and
        `update_environment_storage`; this is the only
        way to change that flag once the environment already exists.

        Parameters
        ----------
        p
            The acting principal.
        name
            Environment name.
        protected
            The new value of `is_protected`.

        Returns
        -------
        Environment
            The updated environment.

        Raises
        ------
        NotFoundError
            If no environment has that name.
        PermissionDenied
            If the principal may not manage environments.
        """
        self._authz.authorize(p, Action.MANAGE_ENVIRONMENT, Securable.instance_scope())
        env = self._find_environment(name)
        env.is_protected = protected
        AuditService(self._session).record(
            p.id,
            AuditEventType.ENVIRONMENT_PROTECTION_CHANGED,
            securable=Securable.environment_scope(name),
            detail={"is_protected": protected},
        )
        self._session.commit()
        return env

    def _find_environment(self, name: str, p: PrincipalRef | None = None) -> Environment:
        """Fetch an environment row by name, without authorizing; a deleted one is not found.

        Parameters
        ----------
        name
            Environment name.
        p
            The caller, when the error may name the restore of a deleted environment.

        Returns
        -------
        Environment
            The environment.

        Raises
        ------
        NotFoundError
            If no environment has that name, or it is deleted
            (`_missing_environment_message`).
        """
        env = self._session.execute(
            select(Environment).where(Environment.name == name)
        ).scalar_one_or_none()
        if env is None or env.deleted_at is not None:
            if env is None:
                raise NotFoundError(f"No environment named {name!r}.")
            raise NotFoundError(self._missing_environment_message(p, env))
        return env

    def create_namespace(
        self,
        p: PrincipalRef,
        environment: str,
        path: NamespacePath,
        properties: Mapping[str, str] | None = None,
    ) -> Namespace:
        """Create a namespace in an environment.

        Parameters
        ----------
        p
            The acting principal.
        environment
            Environment name.
        path
            The namespace path to create.
        properties
            Free-form metadata.

        Returns
        -------
        Namespace
            The created namespace.

        Raises
        ------
        NotFoundError
            If the environment does not exist.
        AlreadyExistsError
            If the namespace already exists in this environment.
        ConflictError
            If the insert fails for a reason other than a path collision —
            for instance, the environment was dropped between the checks
            above and the insert.
        PermissionDenied
            If the principal may not create namespaces here.
        ValidationError
            If the new (last) component is invalid, including containing a
            ``.``. A component is exempt only when the namespace up to and
            including it already exists (a child under a legacy dotted
            parent); every newly named component is checked, at any depth.
        """
        validate_path_component(path.parts[-1], what="Namespace component")
        refuse_percent_in_new_name(path.parts[-1], what="Namespace component")
        self._authz.authorize(
            p, Action.CREATE_NAMESPACE, Securable.namespace_scope(environment, path)
        )
        self._guard.check(p, environment, WriteOperation.CREATE_NAMESPACE, path.parts)
        env = self._find_environment(environment)
        environment_id = env.id
        joined = path.joined()
        # Missing parents are not required to exist, so any component whose
        # prefix has no row is being newly named and must pass the strict
        # rule; only a prefix that already exists may keep a legacy dot.
        for depth, part in enumerate(path.parts, start=1):
            if self._lookup_namespace(env, NamespacePath(path.parts[:depth])) is None:
                validate_path_component(part, what="Namespace component")
                refuse_percent_in_new_name(part, what="Namespace component")
        if self._lookup_namespace(env, path) is not None:
            raise AlreadyExistsError(f"Namespace {path} already exists in {environment!r}.")
        ns = Namespace(
            id=uuid.uuid4(),
            environment_id=environment_id,
            path=joined,
            properties=dict(properties or {}),
        )
        self._session.add(ns)
        try:
            self._guard.record_pending()
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            # The same idiom `create_table` uses, for the same reason. The
            # check above only produces a good message in the common case;
            # on two servers against one Postgres the loser of a genuine
            # create/create race reaches here instead, and without this
            # would surface a raw SQLAlchemy `IntegrityError`. Not every
            # `IntegrityError` is that race, though -- the environment
            # could have been dropped between `_find_environment` and this
            # insert, which fails as a foreign-key violation -- so the row
            # is re-selected to tell the two apart. `environment_id` was
            # captured before the rollback so this re-check never touches
            # the possibly-expired `env` ORM object.
            conflicting = self._session.execute(
                select(Namespace).where(
                    Namespace.environment_id == environment_id, Namespace.path == joined
                )
            ).scalar_one_or_none()
            if conflicting is not None:
                raise AlreadyExistsError(
                    f"Namespace {path} already exists in {environment!r}."
                ) from exc
            # A fixed message, not `str(exc)`: SQLAlchemy's `IntegrityError`
            # string embeds the failed statement and its bound parameters,
            # and this exception is expected to reach a REST client once
            # Phase C maps `ConflictError` onto a response body.
            raise ConflictError(
                f"Could not create namespace {path} in {environment!r}: "
                "the write failed for a reason other than a path collision."
            ) from exc
        return ns

    def _lookup_namespace(self, env: Environment, path: NamespacePath) -> Namespace | None:
        """Look up a namespace row within an environment, without authorizing.

        Parameters
        ----------
        env
            The owning environment.
        path
            The namespace path.

        Returns
        -------
        Namespace or None
            The row, or `None` when it does not exist.
        """
        return self._session.execute(
            select(Namespace).where(
                Namespace.environment_id == env.id, Namespace.path == path.joined()
            )
        ).scalar_one_or_none()

    def _find_namespace(self, env: Environment, path: NamespacePath) -> Namespace:
        """Fetch a namespace row within an environment, without authorizing.

        Parameters
        ----------
        env
            The owning environment.
        path
            The namespace path.

        Returns
        -------
        Namespace
            The namespace row.

        Raises
        ------
        NotFoundError
            If the namespace does not exist in this environment.
        """
        ns = self._lookup_namespace(env, path)
        if ns is None:
            raise NotFoundError(f"Namespace {shown(str(path))} not found in {env.name!r}.")
        return ns

    def load_namespace(self, p: PrincipalRef, environment: str, path: NamespacePath) -> Namespace:
        """Fetch a namespace by path.

        Parameters
        ----------
        p
            The acting principal.
        environment
            Environment name.
        path
            The namespace path.

        Returns
        -------
        Namespace
            The namespace.

        Raises
        ------
        NotFoundError
            If the environment or the namespace does not exist.
        PermissionDenied
            If the principal may not read this namespace.
        """
        self._authz.authorize(
            p, Action.READ_NAMESPACE, Securable.namespace_scope(environment, path)
        )
        return self._find_namespace(self._find_environment(environment), path)

    def list_namespaces(
        self, p: PrincipalRef, environment: str, parent: NamespacePath | None = None
    ) -> list[NamespacePath]:
        r"""List namespaces, optionally only those below a parent.

        Parameters
        ----------
        p
            The acting principal.
        environment
            Environment name.
        parent
            When given, only strict descendants of this path are returned;
            the parent itself is excluded.

        Returns
        -------
        list of NamespacePath
            Matching paths, ordered by `parts` (tuple ordering, the natural
            hierarchical order). This is *not* the same as ordering by the
            stored separator-joined string in SQL: the unit separator
            (U+001F) is collation-ignorable under common Postgres
            collations such as `en_US.utf8`, so a database-side
            `ORDER BY path` can interleave a namespace with its own
            children (`gold`, `golda`, `gold\x1fsales` instead of `gold`,
            `gold\x1fsales`, `golda`). Sorting in Python on the parsed
            tuple sidesteps collation entirely and is identical across
            SQLite and Postgres.

        Raises
        ------
        NotFoundError
            If the environment does not exist.
        PermissionDenied
            If the principal may not read namespaces in this environment.
        """
        self._authz.authorize(p, Action.READ_NAMESPACE, Securable.environment_scope(environment))
        env = self._find_environment(environment, p)
        query = select(Namespace).where(Namespace.environment_id == env.id)
        if parent is not None:
            prefix = escape_like(parent.joined() + NAMESPACE_SEPARATOR)
            query = query.where(Namespace.path.like(prefix + "%", escape="\\"))
        rows = self._session.execute(query).scalars().all()
        paths = [NamespacePath.parse(row.path) for row in rows]
        return sorted(paths, key=lambda ns: ns.parts)

    def drop_namespace(self, p: PrincipalRef, environment: str, path: NamespacePath) -> None:
        """Remove a namespace.

        Every active NAMESPACE-kind grant bound to exactly this namespace is
        soft-revoked first, in the same transaction. A row filter
        or column mask is always table-scoped, so none applies here directly;
        the `ConflictError` checks below already guarantee no table remains
        underneath to worry about.

        Parameters
        ----------
        p
            The acting principal.
        environment
            Environment name.
        path
            The namespace path to remove.

        Raises
        ------
        NotFoundError
            If the environment or the namespace does not exist (also when a
            concurrent drop wins the row lock).
        ConflictError
            If the namespace still contains a table, an artifact or a child
            namespace.
            `Table.namespace_id` cascades on delete at the database level,
            so this check is the only thing standing between a drop and
            silently destroying every table underneath it. A namespace
            with a nested child is refused for the same reason a non-empty
            directory refuses `rmdir`: deleting `gold` while `gold.sales`
            still exists would orphan it under a namespace that no longer
            exists, and there is no namespace-to-namespace foreign key to
            catch that at the database level either.
        PermissionDenied
            If the principal may not drop this namespace.
        """
        self._authz.authorize(
            p, Action.DROP_NAMESPACE, Securable.namespace_scope(environment, path)
        )
        self._guard.check(p, environment, WriteOperation.DROP_NAMESPACE, path.parts)
        env = self._find_environment(environment)
        ns = self._find_namespace(env, path)
        # Locked before the emptiness checks: a table or an artifact created
        # between a check and the delete would be removed by the database's
        # cascade, with an artifact's bytes left in storage. A creator of an
        # artifact takes this row `FOR SHARE` first (`ArtifactService._create`).
        locked = self._session.execute(
            select(Namespace.id).where(Namespace.id == ns.id).with_for_update()
        ).scalar_one_or_none()
        if locked is None:
            self._session.rollback()
            raise NotFoundError(f"Namespace {path} not found in {env.name!r}.")
        has_table = self._session.execute(
            select(Table.id).where(
                Table.namespace_id == ns.id, Table.table_type != TableType.ARTIFACT
            )
        ).first()
        if has_table is not None:
            raise ConflictError(f"Namespace {path} still contains tables.")
        has_artifact = self._session.execute(
            select(Table.id).where(
                Table.namespace_id == ns.id, Table.table_type == TableType.ARTIFACT
            )
        ).first()
        if has_artifact is not None:
            raise ConflictError(
                f"Namespace {path} still contains artifacts; drop each first "
                "(`lakana artifact drop`)."
            )
        prefix = escape_like(path.joined() + NAMESPACE_SEPARATOR)
        has_child = self._session.execute(
            select(Namespace.id).where(
                Namespace.environment_id == env.id,
                Namespace.path.like(prefix + "%", escape="\\"),
            )
        ).first()
        if has_child is not None:
            raise ConflictError(f"Namespace {path} still contains child namespaces.")
        revoke_attached(self._session, p, namespace=(env.id, path.joined()))
        self._session.delete(ns)
        self._guard.record_pending()
        self._session.commit()

    def update_namespace_properties(
        self,
        p: PrincipalRef,
        environment: str,
        path: NamespacePath,
        updates: Mapping[str, str],
        removals: Sequence[str],
    ) -> PropertyUpdateSummary:
        """Set and unset properties on a namespace.

        Parameters
        ----------
        p
            The acting principal.
        environment
            Environment name.
        path
            The namespace path.
        updates
            Properties to set.
        removals
            Property keys to remove. Keys that are absent are reported in the
            summary rather than raising, and a key repeated in this sequence
            is removed, or reported, exactly once.

        Returns
        -------
        PropertyUpdateSummary
            What changed.

        Raises
        ------
        ValidationError
            If a key appears in both `updates` and `removals`.
        NotFoundError
            If the environment or the namespace does not exist.
        PermissionDenied
            If the principal may not manage this namespace.
        """
        self._authz.authorize(
            p, Action.MANAGE_NAMESPACE, Securable.namespace_scope(environment, path)
        )
        self._guard.check(p, environment, WriteOperation.UPDATE_NAMESPACE, path.parts)
        overlap = sorted(set(updates) & set(removals))
        if overlap:
            raise ValidationError(
                f"Keys appear in both updates and removals: {', '.join(overlap)}."
            )
        env = self._find_environment(environment)
        ns = self._find_namespace(env, path)

        properties = dict(ns.properties)
        # De-duplicated first: `removals` is a plain JSON list, so a client
        # can send the same key twice. Both comprehensions run against the
        # pre-mutation dictionary, so a duplicated present key would appear
        # twice in `removed` and the second `del` would raise a builtin
        # `KeyError` -- which, being nothing this method documents, would
        # fall through to the transport as a 500. Order is preserved, so the
        # summary still reports keys in the order the caller sent them.
        unique_removals = list(dict.fromkeys(removals))
        removed = [key for key in unique_removals if key in properties]
        missing = [key for key in unique_removals if key not in properties]
        for key in removed:
            del properties[key]
        properties.update(updates)

        # Reassigning rather than mutating in place: the column is a JSON type,
        # and SQLAlchemy does not track in-place mutation of a plain dict.
        ns.properties = properties
        self._guard.record_pending()
        self._session.commit()
        return PropertyUpdateSummary(updated=list(updates), removed=removed, missing=missing)

    def create_table(
        self,
        p: PrincipalRef,
        ref: TableRef,
        *,
        schema: Schema,
        partition_spec: PartitionSpec | None = None,
        sort_order: SortOrder | None = None,
        properties: Mapping[str, str] | None = None,
        location: str | None = None,
    ) -> LoadedTable:
        """Create an Iceberg table.

        The location's claim is committed first (`claim_location`), then the metadata file is
        written to object storage, then the catalog row is inserted (architecture Constraint 3)
        behind the publish fence (`publish_location`). If the insert then loses a race on the
        ``(namespace_id, name)`` unique constraint, or fails before its commit was sent, the
        file and the claim are deleted (`_release_location`); the opposite order would leave a
        row pointing at a file that does not exist, which is not recoverable. A commit that
        raised is settled as a write's is (`_settle_failed_write`): landed, the table is
        returned; in doubt, the claim and the file stay for the sweep. The pre-existence check
        exists only to produce a good error message in the common case — the unique constraint
        is what actually arbitrates a concurrent create.

        Parameters
        ----------
        p
            The acting principal.
        ref
            Where to create the table.
        schema
            The table's schema.
        partition_spec
            Partitioning, or `None` for unpartitioned.
        sort_order
            Sort order, or `None` for unsorted.
        properties
            Table properties.
        location
            Must be `None`. Lakana assigns every managed table's location
            itself (:meth:`lakana_core.storage.resolver.ResolvedStorage.table_location`,
            id-suffixed); a non-`None` value — including
            the Iceberg REST `CreateTableRequest.location` a client can
            send — is refused rather than honoured, since it would
            otherwise be handed straight to `file_io`, bypassing that
            choke point entirely.

        Returns
        -------
        LoadedTable
            The created table.

        Raises
        ------
        NotFoundError
            If the environment or namespace does not exist.
        AlreadyExistsError
            If a table, view or artifact of that name already exists in the
            namespace (an artifact gets its own message naming how to drop it).
        ValidationError
            If `ref.name` is not a valid table name; if `properties` is
            refused by
            :meth:`lakana_core.storage.resolver.ResolvedStorage.validate_table_properties`;
            if `location` is not `None`; if the metadata location computed
            from the assigned table location does not resolve to somewhere
            inside the environment's storage root; or if `ref.namespace`'s
            top-level component is not `bronze`, `silver`, or `gold`.
        ConflictError
            If the insert fails for a reason other than a name collision —
            for instance, the namespace was dropped between the checks above
            and the insert — or the sweep released the claim before the
            insert (`CLAIM_RELEASED_MESSAGE`).
        PermissionDenied
            If the principal may not create tables here.
        WriteOutcomeUnknownError
            If the commit failed and the table could not be shown to exist; the
            file and the claim are kept.
        """
        self._authz.authorize(
            p,
            Action.CREATE_TABLE,
            Securable.table_scope(ref.environment, ref.namespace, ref.name),
        )
        self._guard.check(p, ref.environment, WriteOperation.CREATE_TABLE, table_target(ref))
        prepared = self._prepare_table(
            ref,
            schema=schema,
            partition_spec=partition_spec,
            sort_order=sort_order,
            properties=properties,
            location=location,
            holder_run_id=claim_holder(p, None),
        )
        self._commit_sent = False
        try:
            return self._publish_table(prepared, prepared.metadata, owner_id=p.id)
        except Exception as exc:
            if not self._settle_failed_write(
                exc,
                ref,
                table_id=prepared.table_id,
                snapshot_id=None,
                new_table=True,
                commit_sent=self._commit_sent,
                clean_up=lambda: self._release_location(prepared),
            ):
                raise
        _, row = self._find_table(ref)
        return self._to_loaded_table(ref, row)

    def _prepare_table(
        self,
        ref: TableRef,
        *,
        schema: Schema,
        partition_spec: PartitionSpec | None,
        sort_order: SortOrder | None,
        properties: Mapping[str, str] | None,
        location: str | None,
        holder_run_id: uuid.UUID | None,
    ) -> _PreparedTable:
        """Validate a new managed table, build its metadata and claim its location.

        The first half of `create_table`: a create-mode write
        needs the table's id and location to write its data files before
        the row exists, so the two halves are separable. The caller must
        already have authorized `Action.CREATE_TABLE` on `ref`. No file is
        written here; the last step inserts and commits the location's
        `CLAIMED` record (`claim_location`), so the claim exists before the
        first file does. A create that is refused claims nothing.

        Parameters
        ----------
        ref
            Where to create the table.
        schema
            The table's schema.
        partition_spec
            Partitioning, or `None` for unpartitioned.
        sort_order
            Sort order, or `None` for unsorted.
        properties
            Table properties.
        location
            Must be `None`; see `create_table`.
        holder_run_id
            The run that makes the create, recorded on the claim (`claim_holder`).

        Returns
        -------
        _PreparedTable
            The table, ready for `_publish_table`.

        Raises
        ------
        NotFoundError
            If the environment or namespace does not exist.
        AlreadyExistsError
            If a table, view or artifact of that name already exists.
        ValidationError
            As `create_table` documents.
        """
        if location is not None:
            raise ValidationError("Lakana assigns table locations; do not supply one.")
        resolved = self._storage.for_environment(ref.environment)
        resolved.validate_table_properties(properties or {})
        env = self._find_environment(ref.environment)
        ns = self._find_namespace(env, ref.namespace)
        top_level = ref.namespace.parts[0]
        if top_level not in _MEDALLION_TOP_LEVEL_NAMESPACES:
            raise ValidationError(
                f"Iceberg tables may only be created under a top-level namespace of "
                f"bronze, silver, or gold; got {top_level!r}."
            )
        taken = self._lookup_table(ns, ref.name)
        if taken is not None:
            raise AlreadyExistsError(_name_taken_message(taken, ref.name, ref.namespace))

        # `table_location` validates `ref.name` itself; it is now the only
        # path a location can be derived from, `location=` above having
        # already been refused unconditionally, so no separate validation
        # call is needed here the way one was while `location=` could
        # bypass it.
        table_id = uuid.uuid4()
        table_location = resolved.table_location(ref.namespace, ref.name, table_id)
        metadata = build_table_metadata(
            schema=schema,
            location=table_location,
            partition_spec=partition_spec,
            sort_order=sort_order,
            properties=dict(properties or {}),
            table_uuid=table_id,
        )
        claim_location(
            self._session,
            environment_id=env.id,
            location=table_location,
            holder_run_id=holder_run_id,
        )
        return _PreparedTable(
            ref=ref,
            table_id=table_id,
            namespace_id=ns.id,
            environment_id=env.id,
            location=table_location,
            resolved=resolved,
            metadata=metadata,
        )

    def _publish_table(
        self, prepared: _PreparedTable, metadata: TableMetadata, *, owner_id: uuid.UUID
    ) -> LoadedTable:
        """Write a prepared table's metadata file and insert its catalog row.

        The second half of `create_table`. `metadata` is the prepared
        metadata, or — for a create-mode write — that metadata with the
        first snapshot already applied, so the table appears with its data
        in one step. Anything already added to the session (an audit event,
        lineage edges) commits with the row or not at all, and so does the
        move of the location's claim to `LIVE` (`publish_location`): when the
        claim is no longer `CLAIMED` (the sweep abandoned it), nothing commits.

        Parameters
        ----------
        prepared
            From `_prepare_table`.
        metadata
            The metadata to publish.
        owner_id
            The principal who creates the table, who owns it.

        Returns
        -------
        LoadedTable
            The created table.

        Raises
        ------
        AlreadyExistsError
            If a concurrent create won the ``(namespace_id, name)`` race.
        ConflictError
            If the insert failed for another reason, or the location's claim was
            released before the insert (`CLAIM_RELEASED_MESSAGE`).
        NotFoundError
            If the environment was deleted before the commit (`ENVIRONMENT_GONE_MESSAGE`); the
            commit takes the environment's row lock first (`lock_live_environment`).
        ValidationError
            If the computed metadata location escapes the storage root.
        """
        ref = prepared.ref
        target = next_metadata_location(
            table_location=prepared.location,
            current_location=None,
            properties=metadata.properties,
        )
        # Belt and braces. `validate_table_properties` has already confined
        # every property pyiceberg is known to read as a location, but the
        # location provider is free to derive a path any way it likes and
        # may gain new inputs in a future release. Confining what it
        # actually produced catches those without this method having to
        # learn about them first.
        target = prepared.resolved.confine_to_root(target)
        written = write_metadata(prepared.resolved.file_io, metadata, target)
        row = Table(
            id=prepared.table_id,
            namespace_id=prepared.namespace_id,
            name=ref.name,
            table_type=TableType.ICEBERG,
            metadata_location=written,
            previous_metadata_location=None,
            owner_id=owner_id,
        )
        self._session.add(row)
        sync_created_table(self._session, prepared.environment_id, row, ref.dotted())
        # Read now: after the commit the row is expired, and reading it would cost a statement.
        text_language = row.text_language
        sync_table_columns(self._session, row.id, from_iceberg_schema(metadata.schema()))
        try:
            self._session.flush()  # a name collision raises here, and SQLite's write lock is held
            if not lock_live_environment(self._session, prepared.environment_id):
                self._session.rollback()
                raise NotFoundError(
                    ENVIRONMENT_GONE_MESSAGE.format(table=ref.dotted(), environment=ref.environment)
                )
            # The fence's UPDATE flushes the row first, so a name collision raises here.
            # The location as the sweep and the backfill derive it from the metadata file, so
            # the record that goes LIVE is the one they would find for this table.
            if not publish_location(
                self._session,
                location=table_root_from_metadata_location(written) or prepared.location,
                table_id=prepared.table_id,
            ):
                self._session.rollback()
                raise ConflictError(CLAIM_RELEASED_MESSAGE.format(table=ref.dotted()))
            self._guard.record_pending()
            self._commit_sent = True
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            # Not every IntegrityError here is the name collision this
            # method exists to report: the namespace could have been
            # dropped by another writer between `_prepare_table`'s lookup
            # and this insert, which fails the same way but for an
            # unrelated reason (a foreign-key violation, not the
            # unique-constraint violation Constraint 3 relies on to
            # arbitrate a genuine create/create race). `prepared.namespace_id`
            # is a plain value captured in `_prepare_table`, so this
            # re-check never touches a namespace ORM object that a real
            # drop would have turned into an `ObjectDeletedError` on
            # attribute access.
            conflicting = self._session.execute(
                select(Table).where(
                    Table.namespace_id == prepared.namespace_id, Table.name == ref.name
                )
            ).scalar_one_or_none()
            if conflicting is not None:
                raise AlreadyExistsError(
                    _name_taken_message(conflicting, ref.name, ref.namespace)
                ) from exc
            # A fixed message, not `str(exc)`: SQLAlchemy's `IntegrityError`
            # string includes the failed statement and its bound parameters
            # (`[SQL: INSERT INTO tables ...] [parameters: ...]`), and this
            # exception is expected to reach a REST client once Phase C maps
            # `ConflictError` onto a response body. `exc` is still chained
            # via `from exc`, so it remains visible in logs and tracebacks.
            raise ConflictError(
                f"Could not create table {ref.name!r} in {ref.namespace}: "
                "the write failed for a reason other than a name collision."
            ) from exc
        return LoadedTable(
            ref=ref,
            uuid=metadata.table_uuid,
            metadata_location=written,
            metadata=metadata,
            text_language=text_language,
        )

    def create_external_table(
        self,
        p: PrincipalRef,
        ref: TableRef,
        *,
        location_pattern: str,
        format: str,
        properties: Mapping[str, str] | None = None,
    ) -> Table:
        """Register a raw-zone table backed by files already on storage.

        Unlike `create_table`, this never writes Iceberg metadata: the table's
        location, format, and inferred schema are the whole of its catalog
        entry. Not subject to the medallion naming rule `create_table`
        enforces — raw is exempt from that convention (architecture design,
        Decision 2).

        Parameters
        ----------
        p
            The acting principal.
        ref
            Where to register the table.
        location_pattern
            A pattern shaped `"raw/<dir...>/<basename-glob>"`, confined to
            the raw zone and exclusive to this table: the
            directory part is this table's claim, and no other active
            external table in this environment may claim an overlapping
            directory. The final component is a basename-only glob, matched
            against direct children of the claim only — never a
            subdirectory (for example `"raw/events/*.parquet"`). See
            `lakana_core.catalog.external_location`.
        format
            `"parquet"`, `"csv"`, or `"json"`.
        properties
            Free-form metadata. Reserved for parity with `create_table`; not
            yet interpreted.

        Returns
        -------
        Table
            The created catalog row, with `table_type` `EXTERNAL`.

        Raises
        ------
        NotFoundError
            If the environment or namespace does not exist, or no file
            matches `location_pattern`.
        AlreadyExistsError
            If a table, view or artifact of that name already exists in the
            namespace (an artifact gets its own message naming how to drop it).
        ValidationError
            If `ref.name` contains a ``.``, if `location_pattern` is not
            shaped `"raw/<dir...>/<basename-glob>"`, if its claim overlaps another
            active external table's claim in this environment, if it
            escapes the environment's storage root, or if `format` is
            unsupported.
        ConflictError
            If the insert fails for a reason other than a name collision —
            for instance, the namespace was dropped between the checks above
            and the insert.
        PermissionDenied
            If the principal may not create tables here.
        """
        validate_path_component(ref.name, what="Table name")
        refuse_percent_in_new_name(ref.name, what="Table name")
        self._authz.authorize(
            p, Action.CREATE_TABLE, Securable.table_scope(ref.environment, ref.namespace, ref.name)
        )
        self._guard.check(
            p, ref.environment, WriteOperation.CREATE_EXTERNAL_TABLE, table_target(ref)
        )
        env = self._find_environment(ref.environment)
        ns = self._find_namespace(env, ref.namespace)
        namespace_id = ns.id
        taken = self._lookup_table(ns, ref.name)
        if taken is not None:
            raise AlreadyExistsError(_name_taken_message(taken, ref.name, ref.namespace))
        loc = parse_external_location(location_pattern)
        # Checked unlocked, before any storage I/O: listing and reading the claimed directory first
        # — as
        # this method used to — is both an existence oracle (a 404 "no file
        # matches" versus a 422 "claim taken" tells an unprivileged caller
        # whether a directory exists at all) and a cross-table read (the
        # sample file read to infer schema would be *another* table's own
        # data, before the caller is ever confirmed to have a claim of
        # their own on it). This unlocked check can still race a concurrent
        # create; the locked re-check below, immediately before the
        # insert, is what actually serialises that race.
        check_claim_free(self._session, env.id, loc.claim)
        resolved = self._storage.for_environment(ref.environment)
        resolved.confine_to_root(f"{resolved.config.root.rstrip('/')}/{loc.claim}")
        matches = sorted(path for path in resolved.storage.list(loc.claim) if loc.matches(path))
        if not matches:
            raise NotFoundError(f"No file matches {location_pattern!r}.")
        sample_bytes = resolved.storage.read(matches[0])
        schema = infer_schema(sample_bytes, format)
        # The lock is taken as late as possible, around only the claim
        # re-check and the insert: everything above only reads storage and
        # never touches the environment row, so holding the lock around a
        # slow listing/read would serialise every other create in this
        # environment behind it for no benefit. Serialises concurrent
        # creates so two callers racing on overlapping claims cannot both
        # pass `check_claim_free`. On Postgres,
        # `SELECT ... FOR UPDATE` is what actually serialises this; on
        # SQLite it is a no-op (no row-level locking), so the
        # SELECT-then-INSERT here is not serialised there and two
        # concurrent creates in the same environment can still both pass
        # this check and race on the insert — a real gap on SQLite, not one
        # this comment used to claim SQLite closed on its own.
        locked = self._session.execute(
            select(Environment)
            .where(Environment.id == env.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one()
        if locked.deleted_at is not None:
            self._session.rollback()
            raise NotFoundError(f"No environment named {ref.environment!r}.")
        check_claim_free(self._session, env.id, loc.claim)
        row = Table(
            id=uuid.uuid4(),
            namespace_id=namespace_id,
            name=ref.name,
            table_type=TableType.EXTERNAL,
            metadata_location=None,
            previous_metadata_location=None,
            external_config={
                "location_pattern": location_pattern,
                "format": format,
                "schema": schema,
            },
            external_claim=loc.claim,
        )
        self._session.add(row)
        sync_table_columns(
            self._session,
            row.id,
            [ColumnSpec(name=n, type=t, nullable=True) for n, t in schema.items()],
        )
        try:
            self._guard.record_pending()
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            # Not every IntegrityError here is the name collision this
            # method exists to report: the namespace could have been
            # dropped by another writer between `_find_namespace` above and
            # this insert, which fails the same way but for an unrelated
            # reason (a foreign-key violation, not the unique-constraint
            # violation that arbitrates a genuine create/create race).
            # `namespace_id` was captured before the rollback specifically
            # so this re-check never touches the now-possibly-expired `ns`
            # ORM object, which a real dropped namespace would turn into an
            # `ObjectDeletedError` on any attribute access.
            conflicting = self._session.execute(
                select(Table).where(Table.namespace_id == namespace_id, Table.name == ref.name)
            ).scalar_one_or_none()
            if conflicting is not None:
                raise AlreadyExistsError(
                    _name_taken_message(conflicting, ref.name, ref.namespace)
                ) from exc
            # A fixed message, not `str(exc)`: SQLAlchemy's `IntegrityError`
            # string includes the failed statement and its bound parameters,
            # and this exception is expected to reach a REST client once
            # an endpoint is wired on top of this method.
            raise ConflictError(
                f"Could not create table {ref.name!r} in {ref.namespace}: "
                "the write failed for a reason other than a name collision."
            ) from exc
        return row

    def _lookup_table(self, ns: Namespace, name: str) -> Table | None:
        """Look up a table row within a namespace, without authorizing.

        Parameters
        ----------
        ns
            The owning namespace.
        name
            Table name.

        Returns
        -------
        Table or None
            The row, or `None` when it does not exist.
        """
        return self._session.execute(
            select(Table).where(Table.namespace_id == ns.id, Table.name == name)
        ).scalar_one_or_none()

    def _lock_unprotected_environment(self, name: str) -> Environment:
        """Lock an environment's row for a change and refuse a protected one.

        Parameters
        ----------
        name
            The environment's name.

        Returns
        -------
        Environment
            The locked row, refreshed from the database.

        Raises
        ------
        NotFoundError
            If no environment has that name (also when a concurrent delete wins the row lock).
        ValidationError
            If the environment is protected.
        """
        env = self._session.execute(
            select(Environment)
            .where(Environment.name == name)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if env is None or env.deleted_at is not None:
            self._session.rollback()
            raise NotFoundError(f"No environment named {name!r}.")
        if env.is_protected:
            self._session.rollback()
            raise ValidationError(f"Environment {name!r} is protected.")
        return env

    def _release_session(self) -> None:
        """Give the session's database connection back to the pool before slow storage reads.

        The transaction must have held only reads: a rollback would drop a write silently.
        The session's write tracker (`_track_writes`) says whether anything was flushed or
        executed as a statement that writes since the transaction began, and any change still
        pending counts too.

        Raises
        ------
        RuntimeError
            If the transaction wrote. That is a bug in the caller, which must commit its
            writes before it asks for the release; nothing is rolled back.
        """
        session = self._session
        if session.info.get(_WROTE_KEY) or session.new or session.dirty or session.deleted:
            raise RuntimeError(
                "The database session was released for a slow storage read, but something was "
                "written in the transaction and a rollback would lose it. Commit the write "
                "before the call, or do not ask for the release."
            )
        session.rollback()

    def _track_writes(self) -> None:
        """Note on the session whether its transaction has written, once per session.

        Flushes and statements that write (Core `insert`, `update`, `delete`) set the note;
        the end of the session's outermost transaction clears it.
        """
        info = self._session.info
        if _TRACKED_KEY in info:
            return
        info[_TRACKED_KEY] = True

        def flushed(_session: Session, _context: object) -> None:
            info[_WROTE_KEY] = True

        def executed(state: ORMExecuteState) -> None:
            if state.statement.is_dml:
                info[_WROTE_KEY] = True

        def ended(_session: Session, transaction: SessionTransaction) -> None:
            if transaction.parent is None:
                info[_WROTE_KEY] = False

        event.listen(self._session, "after_flush", flushed)
        event.listen(self._session, "do_orm_execute", executed)
        event.listen(self._session, "after_transaction_end", ended)

    def _find_table(self, ref: TableRef) -> tuple[Namespace, Table]:
        """Resolve a table row from a reference, without authorizing.

        Parameters
        ----------
        ref
            The table reference.

        Returns
        -------
        tuple of (Namespace, Table)
            The owning namespace and the table row.

        Raises
        ------
        NotFoundError
            If the environment, namespace or table does not exist.
        """
        env = self._find_environment(ref.environment)
        ns = self._find_namespace(env, ref.namespace)
        row = self._lookup_table(ns, ref.name)
        if row is None:
            raise NotFoundError(
                f"Table '{shown(ref.name)}' not found in {shown(str(ref.namespace))}."
            )
        return ns, row

    def load_table(self, p: PrincipalRef, ref: TableRef) -> LoadedTable:
        """Load a table and its current Iceberg metadata.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table reference.

        Returns
        -------
        LoadedTable
            The table.

        Raises
        ------
        NotFoundError
            If the table does not exist, has no metadata pointer, or is a
            view or an artifact.
        NotPublishedError
            If the table's first build did not finish, or failed a blocking check, and no
            later run has published it. Raised only after the read grant passed, so it tells nobody
            who may not read the table that it exists.
        KeptAfterCheckError
            If the table was kept after a failed check and has not passed its checks since, and
            the reader is a task whose run is not one of the project that builds the table, or
            that is neither the table's own task, nor downstream of it in its run, nor the
            count of a check. Raised after the read grant passed and after the refusal above;
            never for a reader without a run context.
        PermissionDenied
            If the principal may not read this table.

        Notes
        -----
        The two entry points that read a table for a reader, the query path (through the
        worker's source resolution) and the Iceberg REST load, go through this method, so
        the refusal sits here. Code that reads a table's metadata without it does so for
        a reason: `describe_table` shows the state instead of refusing; writers and
        maintenance (`append`, `write_batches`, `commit_table`, `maintain`,
        `read_watermark`, `rename_table`) and the worker's own bookkeeping
        (`resolve_lineage_sources`, `python_tasks` reading its target) need the table to
        write or account for it, and return metadata, never rows or file locations.
        `load_table_at_check` is the one other reader: it reads the single snapshot that a
        stored check result recorded and states there why it does not refuse a table that is
        not published.
        """
        self._authz.authorize(
            p, Action.READ_TABLE, Securable.table_scope(ref.environment, ref.namespace, ref.name)
        )
        ns, row = self._find_table(ref)
        if row.table_type is TableType.VIEW:
            raise NotFoundError(
                f"{ref.dotted()} is a view; views have no Iceberg metadata and are not "
                "exposed over the Iceberg REST catalog."
            )
        if row.table_type is TableType.ARTIFACT:
            raise NotFoundError(_not_exposed_artifact_message(ref.dotted()))
        self._require_published(p, ref, row)
        self._require_not_kept(p, ref, ns, row)
        return self._to_loaded_table(ref, row)

    def load_table_at_check(
        self, p: PrincipalRef, ref: TableRef, *, check_result_id: uuid.UUID
    ) -> LoadedAt:
        """Load a table to read it at the snapshot a stored check result recorded.

        For showing the rows a check found, to a person. The table and the snapshot are read
        from the check result itself, never taken from the caller, so nothing but a snapshot a
        check read can be reached this way.

        Parameters
        ----------
        p
            The acting principal: the reader, never the run that checked.
        ref
            The table as the reader addresses it. The grant is decided for this name, and the
            name must hold the table the result is of.
        check_result_id
            The check result whose snapshot is to be read.

        Returns
        -------
        LoadedAt
            The table's current metadata with the snapshot to read and the columns of that
            snapshot.

        Raises
        ------
        PermissionDenied
            If the principal may not read the table `ref` names; decided before anything is
            looked up, so nothing below tells a caller without the grant whether the table,
            the result or the snapshot exists. Or, with the grant, if the principal has a run
            context or authenticated with a task token, bound to a task or not: the text is
            `CHECK_ROWS_NOT_FOR_A_TASK`.
        NotFoundError
            If the environment does not exist.
        CheckResultGoneError
            If the result is not bound to the named table: the environment of `ref` has no
            check result with this id, or the result names no table, or the name does not hold
            the table the result is of (it was dropped, or dropped and built again; or the
            result is of another table), or the name holds an artifact. One text for all of
            them, which names only the table as addressed.
        NoCheckedSnapshotError
            If the table's own result names no snapshot: nothing was checked that can be read.
        CheckedSnapshotGoneError
            If the table's own result names a snapshot that cannot be read: the row is not a
            managed table with metadata, or the metadata no longer holds the snapshot or the
            schema it names. The text names the table as addressed, the result's run and the
            environment.
        KeptAfterCheckError
            As `load_table` raises it. No caller can meet it today, because a principal with a
            run context is refused before; it stays so that the kept refusal does not depend
            on that.

        Notes
        -----
        The one reader of a table that does not go through `load_table`, and the one that does
        not refuse a table that is not published. Row filters and column masks are not applied
        here, as they are not in `load_table`: the query path resolves them for the reader, by
        the table's id, for this read as for any other.
        """
        self._authz.authorize(
            p, Action.READ_TABLE, Securable.table_scope(ref.environment, ref.namespace, ref.name)
        )
        # A blocking check keeps failing rows from everything downstream. A task that holds
        # the read grant on the table would get them here after all, so this read is for a
        # person's own query only. Refused before the result is looked up: a task learns
        # nothing about it.
        if p.run_context is not None or p.task_scope is not None:
            raise PermissionDenied(CHECK_ROWS_NOT_FOR_A_TASK)
        env = self._find_environment(ref.environment)
        ns = self._lookup_namespace(env, ref.namespace)
        row = None if ns is None else self._lookup_table(ns, ref.name)
        # Looked up in the environment of the name: a result of another environment is no
        # result here, whatever table id it carries.
        result = self._session.execute(
            select(CheckResult).where(
                CheckResult.id == check_result_id, CheckResult.environment_id == env.id
            )
        ).scalar_one_or_none()
        # Bound first, before anything of the result is told. The id says which table a check
        # read, and the grant above was decided for the name: both must mean one table. The
        # result's own table name is not compared, it goes stale with a rename. A table
        # dropped and created again under the name is another table, and so is any table the
        # result is not of. Every way of not being bound gets one answer that names only what
        # the caller named: whoever may read this table and holds the id of another table's
        # result learns neither its run, nor whether it has a snapshot, nor that it exists.
        # An artifact is never bound, whatever a result says: a check reads a table or a
        # view, so no result is an artifact's, and the answer for a name that holds one is
        # the answer for a name that holds no table.
        if (
            result is None
            or result.table_id is None
            or ns is None
            or row is None
            or row.id != result.table_id
            or row.table_type is TableType.ARTIFACT
        ):
            raise CheckResultGoneError(check_result_gone_message(ref.dotted()))
        # From here on the result is this table's own, and a text may name its run.
        if result.checked_snapshot_id is None:
            raise NoCheckedSnapshotError(no_checked_snapshot_message(result.run_id))
        gone = CheckedSnapshotGoneError(
            checked_snapshot_gone_message(ref.dotted(), result.run_id, ref.environment)
        )
        # A row without Iceberg metadata (a view, an external table, a table whose creation
        # has not finished) has no snapshot at all.
        if row.table_type is not TableType.ICEBERG or row.metadata_location is None:
            raise gone
        # No `_require_published` here, and only here. That refusal exists because a table
        # without a current snapshot would read as empty, which a consumer cannot tell from
        # "no data". This read never shows the table's current state: it shows the one
        # snapshot that a stored check result of this very table recorded, so it cannot be
        # mistaken for the table as published, and a snapshot no check read (a build that
        # failed before its checks, say) cannot be reached. Nothing is published by it
        # either: the row and the metadata are only read.
        self._require_not_kept(p, ref, ns, row)
        metadata = self._to_loaded_table(ref, row).metadata
        schema = _schema_of(metadata, result.checked_snapshot_id)
        if schema is None:
            raise gone
        return LoadedAt(
            ref=ref,
            metadata=metadata,
            snapshot_id=result.checked_snapshot_id,
            columns=schema.fields,
        )

    def _require_published(self, p: PrincipalRef, ref: TableRef, row: Table) -> None:
        """Refuse a read of a table that is not published (its first build failed).

        Parameters
        ----------
        p
            The reader: the text names only the checks it may see (`result_hider`).
        ref
            How the caller addressed the table.
        row
            The resolved catalog row; the caller already authorized `READ_TABLE` on it.

        Raises
        ------
        NotPublishedError
            If `row.unpublished_run_id` is set; the text names the failed checks and the run.
        """
        if row.unpublished_run_id is not None:
            raise NotPublishedError(
                not_published_message(
                    self._session,
                    ref.dotted(),
                    row.id,
                    row.unpublished_run_id,
                    ref.environment,
                    result_hider(self._session, self._authz, p, ref.environment),
                )
            )

    def _require_not_kept(self, p: PrincipalRef, ref: TableRef, ns: Namespace, row: Table) -> None:
        """Refuse a task a table that was kept after a failed check and has not passed since.

        A reader without a run context is never refused, unless it authenticated with a task
        token that is bound to no task: that is a task with no run, refused outright. The table
        keeps the failing data so that people can look at it. A task is one whose principal has a
        run context, in either form; both name the reading task run, and the note is the only
        thing that makes this method read it, its run's manifest and, for the worker's form, its
        query run. A
        context is not checked for being live here: it only ever narrows what its principal
        may read, so a stale one gains nothing.

        Parameters
        ----------
        p
            The acting principal; the caller already authorized `READ_TABLE` for it.
        ref
            How the caller addressed the table.
        ns
            The table's namespace.
        row
            The resolved catalog row.

        Raises
        ------
        KeptAfterCheckError
            If the row carries the note and the reader is a task without standing
            (`lakana_core.runs.kept.task_may_read`): its run is not one of the project that
            builds the table, or it is neither the table's own task, nor downstream of it in
            its run, nor the count of a check. The text names the check, the run and the way
            out.
        """
        if row.kept_run_id is None:
            return
        context = p.run_context
        if context is None and p.task_scope is None:
            return
        if context is not None and task_may_read(
            self._session,
            context.task_run_id,
            context.query_run_id,
            ns.environment_id,
            table_target(ref),
        ):
            return
        # Only tasks are refused, and a task's error reaches every reader of its run: the text
        # names no generated reference check (`result_hider`).
        check = shown_kept_check(
            self._session, row, result_hider(self._session, self._authz, p, ref.environment)
        )
        refused = refused_message(row, ref.dotted(), ref.environment, check)
        if context is None:
            # A task token bound to no task has no run to stand on: refused like a task that
            # has no standing, and told what a person who holds one does instead.
            raise KeptAfterCheckError(f"{refused} {_KEPT_ADVICE_FOR_A_PERSON}")
        raise KeptAfterCheckError(refused)

    def load_table_row(self, p: PrincipalRef, ref: TableRef) -> Table:
        """Fetch a table's raw catalog row: ICEBERG, EXTERNAL, VIEW or ARTIFACT.

        Unlike `load_table`, this never reads Iceberg metadata, so it works
        for `EXTERNAL` tables too, which have none. Exists for a caller that
        needs to know `table_type` before deciding how to read a table at
        all — `load_table` remains the right call once a caller already
        knows a table is `ICEBERG` and wants its parsed metadata.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table reference.

        Returns
        -------
        Table
            The raw catalog row.

        Raises
        ------
        NotFoundError
            If the table does not exist.
        PermissionDenied
            If the principal may not read this table.
        """
        self._authz.authorize(
            p, Action.READ_TABLE, Securable.table_scope(ref.environment, ref.namespace, ref.name)
        )
        _, row = self._find_table(ref)
        return row

    def describe_table(
        self, p: PrincipalRef, ref: TableRef, *, languages: Sequence[str] = ()
    ) -> TableDescription:
        """Describe a table, an external table or a view for the platform API.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table reference.
        languages
            The reader's languages, most wanted first; empty: the main language.

        Returns
        -------
        TableDescription
            Its kind, columns, and — per kind — its metadata location or its
            SQL and references.

        Raises
        ------
        NotFoundError
            If it does not exist.
        PermissionDenied
            If `p` may not read it.

        Notes
        -----
        A view's `sql` and `references` name every table it reads, so
        READ_TABLE on the view alone is not enough to show them: a
        principal can hold READ on a view without READ on what it
        references (grants are per-securable; only the *creator* is
        required to hold READ on every reference, and only at creation
        time). Showing the SQL text to such a reader would disclose a
        table's existence and exact name — the same class of leak the
        lineage API guards against for the lineage API. `sql`/`references` are
        included only if `p` holds READ_TABLE on every reference right
        now; otherwise both are empty and `hidden_references` reports how
        many were withheld.
        """
        self._authz.authorize(
            p, Action.READ_TABLE, Securable.table_scope(ref.environment, ref.namespace, ref.name)
        )
        _, row = self._find_table(ref)
        described = self._describe_row(p, ref, row)
        chooser = TextChooser(
            row.text_language or self._main_language,
            languages,
            translation_rows(self._session, row),
        )
        description = chooser.choose(
            "table", "", "description", row.description_project, row.description_edited
        )
        label = chooser.choose("table", "", "label", row.label_project, row.label_edited)
        columns = column_texts(self._session, row.id, (c.name for c in described.columns), chooser)
        model = model_view(
            self._session,
            row,
            ref,
            lambda target: self.can_read_table(p, target),
            ReferenceCheckFilter(self._session, self._authz, p, ref.environment).hides,
            texts=chooser,
        )
        keys = [
            ("table", "", field)
            for field, project, edited in (
                ("label", row.label_project, row.label_edited),
                ("description", row.description_project, row.description_edited),
                ("grain", row.model_grain, None),
            )
            if project is not None or edited is not None
        ]
        keys += columns.keys
        keys += list(model.measure_keys) if model is not None else []
        expires_at = None
        expires_soon = False
        if row.table_type is TableType.ICEBERG:
            env = self._find_environment(ref.environment)
            expires_at = describe_retention(self._session, env, row, ref).expires_at
            warning = db_now(self._session) + timedelta(days=self._expiry_warning_days)
            # 0 turns the warning off, also for a table already due.
            expires_soon = (
                self._expiry_warning_days > 0 and expires_at is not None and expires_at <= warning
            )
        return replace(
            described,
            description=None if description is None else description.text,
            description_source=None if description is None else description.source,
            description_language=None if description is None else description.language,
            label=None if label is None else label.text,
            label_source=None if label is None else label.source,
            label_language=None if label is None else label.language,
            column_descriptions=columns.descriptions,
            column_labels=columns.labels,
            language=chooser.requested,
            main_language=chooser.main,
            translations=chooser.coverage(keys),
            expires_at=expires_at,
            expires_soon=expires_soon,
            model=model,
        )

    def table_references(self, p: PrincipalRef, ref: TableRef) -> TableReferences:
        """Show what a table's header says it references, and which tables reference it.

        These are the references the model declares, not lineage. A reference whose target, or
        whose referencing table, `p` may not read is counted, never named; a missing target is
        counted the same way for such a reader, so the answer never says whether it exists.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table reference.

        Returns
        -------
        TableReferences
            Both directions, as `p` may see them.

        Raises
        ------
        NotFoundError
            If the table does not exist.
        PermissionDenied
            If `p` may not read it.
        """
        self._authz.authorize(
            p, Action.READ_TABLE, Securable.table_scope(ref.environment, ref.namespace, ref.name)
        )
        _, row = self._find_table(ref)
        return references_view(
            self._session, row, ref, lambda target: self.can_read_table(p, target)
        )

    def table_retention(self, p: PrincipalRef, ref: TableRef) -> TableRetention:
        """Show a managed table's retention, where it comes from and when the table expires.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table reference.

        Returns
        -------
        TableRetention
            The retention.

        Raises
        ------
        NotFoundError
            If the table does not exist.
        PermissionDenied
            If `p` may not read it.
        ValidationError
            If it is not a managed table.
        """
        self._authz.authorize(
            p, Action.READ_TABLE, Securable.table_scope(ref.environment, ref.namespace, ref.name)
        )
        _, row = self._find_table(ref)
        if row.table_type is not TableType.ICEBERG:
            raise ValidationError(NOT_MANAGED_MESSAGE.format(table=shown(ref.dotted())))
        return describe_retention(self._session, self._find_environment(ref.environment), row, ref)

    def set_table_retention(
        self,
        p: PrincipalRef,
        ref: TableRef,
        *,
        days: int | None = None,
        on: date | None = None,
        never: bool = False,
    ) -> TableRetention:
        """Set when a managed table expires: `days` after its last activity, `on` a day, or `never`.

        Who may: the table's owner always. Anyone else needs `DROP_TABLE` to set a retention
        that makes the table expire earlier than it would now (or at all, when it would not),
        and `WRITE_TABLE` to make it expire later or never. The change is the table's activity,
        so a retention by days counts from now. One `TABLE_RETENTION_SET` (or, for `never`,
        `TABLE_RETENTION_CLEARED`) audit event records it.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table reference.
        days
            Expire this many days (1 to 3,650) after the table's last activity.
        on
            Expire from midnight UTC of this day on; it must be after today.
        never
            Never expire, whatever the environment's retention says.

        Returns
        -------
        TableRetention
            The retention now.

        Raises
        ------
        NotFoundError
            If the table does not exist and `p` may write where it would be.
        PermissionDenied
            Without the right the change needs.
        ValidationError
            Unless exactly one valid rule is given; for anything but a managed table; and for
            `days` or `on` in a protected environment, whose tables never expire.
        """
        now = db_now(self._session)
        check_rule(days=days, on=on, never=never, today=now.date())
        scope = Securable.table_scope(ref.environment, ref.namespace, ref.name)
        try:
            _, row = self._find_table(ref)
        except NotFoundError:
            self._authz.authorize(p, Action.WRITE_TABLE, scope)
            raise
        env = self._find_environment(ref.environment)
        before = planned_expiry(row, env, last_reads(self._session, [row.id]).get(row.id))
        after = None if never else expiry_time(days=days, on=on, activity=now, floor=None)
        shortened = after is not None and (before is None or after < before)
        if row.owner_id != p.id:
            self._authz.authorize(p, Action.DROP_TABLE if shortened else Action.WRITE_TABLE, scope)
        if row.table_type is not TableType.ICEBERG:
            raise ValidationError(NOT_MANAGED_MESSAGE.format(table=shown(ref.dotted())))
        if env.is_protected and not never:
            raise ValidationError(PROTECTED_TABLE_MESSAGE.format(environment=shown(env.name)))
        row = self._lock_table_row(ref, row.id)
        previous = {
            "days": row.retention_days,
            "on": None if row.retention_on is None else row.retention_on.isoformat(),
            "never": row.retention_never,
        }
        row.retention_days, row.retention_on, row.retention_never = days, on, never
        row.retention_set_by, row.retention_set_at = p.id, now
        AuditService(self._session).record(
            p.id,
            AuditEventType.TABLE_RETENTION_CLEARED if never else AuditEventType.TABLE_RETENTION_SET,
            securable=scope,
            detail={
                "table_id": str(row.id),
                "days": days,
                "on": None if on is None else on.isoformat(),
                "shortened": shortened,
                "previous": previous,
                "expires_at": None if after is None else after.isoformat(),
            },
        )
        self._session.commit()
        return describe_retention(self._session, env, row, ref)

    def set_environment_retention(
        self, p: PrincipalRef, name: str, days: int | None
    ) -> Environment:
        """Set the retention of an environment's tables that set none of their own, or turn it off.

        Needs `MANAGE_ENVIRONMENT` on the environment. A table then expires `days` after its last
        activity, and never earlier than `days` after this change. One
        `ENVIRONMENT_RETENTION_SET` audit event records it, with `days: null` for off.

        Parameters
        ----------
        p
            The acting principal.
        name
            Environment name.
        days
            1 to 3,650 days, or `None` to turn it off.

        Returns
        -------
        Environment
            The environment.

        Raises
        ------
        NotFoundError
            If no environment has that name.
        PermissionDenied
            Without `MANAGE_ENVIRONMENT` on it.
        ValidationError
            If `days` is out of range, or the environment is protected (turning it off is
            allowed there).
        """
        self._authz.authorize(p, Action.MANAGE_ENVIRONMENT, Securable.environment_scope(name))
        env = self._find_environment(name)
        if days is not None:
            check_days(days, "An environment's retention")
            if env.is_protected:
                raise ValidationError(PROTECTED_ENVIRONMENT_MESSAGE.format(environment=shown(name)))
        previous = env.retention_days
        env.retention_days = days
        env.retention_set_by, env.retention_set_at = p.id, db_now(self._session)
        AuditService(self._session).record(
            p.id,
            AuditEventType.ENVIRONMENT_RETENTION_SET,
            securable=Securable.environment_scope(name),
            detail={"days": days, "previous_days": previous},
        )
        self._session.commit()
        return env

    def list_expiring_tables(
        self, p: PrincipalRef, environment: str, *, within_days: int
    ) -> list[TableRetention]:
        """List the managed tables of an environment that expire within some days, soonest first.

        Parameters
        ----------
        p
            The acting principal.
        environment
            Environment name.
        within_days
            The window, 1 to 3,650 days from now; a table already due is listed too.

        Returns
        -------
        list of TableRetention
            The tables `p` may read whose expiry falls within the window; a table that never
            expires (a protected environment, a deployed project's asset) is not listed.

        Raises
        ------
        NotFoundError
            If no environment has that name.
        PermissionDenied
            Without `READ_ENVIRONMENT` on it.
        ValidationError
            If `within_days` is out of range.
        """
        check_days(within_days, "The window")
        self._authz.authorize(p, Action.READ_ENVIRONMENT, Securable.environment_scope(environment))
        env = self._find_environment(environment)
        until = db_now(self._session) + timedelta(days=within_days)
        found = describe_retentions(self._session, env, retention_candidates(self._session, env))
        due = [
            entry
            for entry in found
            if entry.expires_at is not None
            and entry.expires_at <= until
            and self.can_read_table(p, entry.ref)
        ]
        return sorted(due, key=lambda entry: (entry.expires_at, entry.ref.dotted()))

    def table_health(
        self,
        p: PrincipalRef,
        ref: TableRef,
        *,
        days: int,
        now: datetime,
    ) -> TableHealth:
        """Show a table's last maintenance, the newest result of each of its checks and its readers.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table reference.
        days
            The window of the readers in days, already checked against the kept period.
        now
            The present time.

        Returns
        -------
        TableHealth
            The health. `readers` is `None` unless `p` may see grants (`MANAGE_GRANT` and the
            `ADMIN` role, as for the grants listing).

        Raises
        ------
        NotFoundError
            If the table does not exist.
        PermissionDenied
            If `p` may not read it.
        AuditUnavailableError
            If the readers name someone other than `p` and the `ACTIVITY_VIEWED` row could not
            be written; nothing is returned.
        """
        scope = Securable.table_scope(ref.environment, ref.namespace, ref.name)
        self._authz.authorize(p, Action.READ_TABLE, scope)
        _, row = self._find_table(ref)
        shown = None
        if may_see_grants(self._session, self._authz, p):
            shown = readers(self._session, row.id, days=days, now=now)
            others = sum(1 for reader in shown if reader.principal != p.name)
            record_view(
                sessions_like(self._session),
                p,
                View.TABLE_READERS,
                scope,
                None,
                ViewDetail(
                    filters={"days": days},
                    rows_shown=len(shown),
                    others_shown=others,
                    distinct_others=others,
                ),
                config=self._view_audit,
            )
        withheld = ReferenceCheckFilter(self._session, self._authz, p, ref.environment)
        every = latest_checks(self._session, row.id)
        checks = [c for c in every if not withheld.hides(c.run_id, c.name, c.generated)]
        return TableHealth(
            days=days,
            last_maintenance=last_maintenance(self._session, row.id),
            checks=checks,
            readers=shown,
            hidden_checks=len(every) - len(checks),
        )

    def _describe_row(self, p: PrincipalRef, ref: TableRef, row: Table) -> TableDescription:
        """Describe a table row by kind, without its descriptions.

        Parameters
        ----------
        p
            The acting principal, who has been authorized to read the table.
        ref
            The table reference.
        row
            Its row.

        Returns
        -------
        TableDescription
            Its kind, columns, and per kind its metadata location or SQL and references.
        """
        if row.table_type is TableType.VIEW:
            definition = row.view_definition or {}
            references = definition.get("references", [])
            columns = [ColumnSpec(**column) for column in definition.get("columns", [])]
            hidden = sum(
                0 if self._can_read_reference(p, ref.environment, reference) else 1
                for reference in references
            )
            if hidden:
                return TableDescription(
                    ref=ref,
                    id=row.id,
                    kind=row.kind,
                    metadata_location=None,
                    columns=columns,
                    hidden_references=hidden,
                )
            return TableDescription(
                ref=ref,
                id=row.id,
                kind=row.kind,
                metadata_location=None,
                columns=columns,
                sql=definition.get("sql"),
                references=tuple((r["id"], r["name"]) for r in references),
            )
        if row.table_type is TableType.ARTIFACT:
            return TableDescription(
                ref=ref, id=row.id, kind=row.kind, metadata_location=None, columns=[]
            )
        if row.table_type is TableType.EXTERNAL:
            schema = (row.external_config or {}).get("schema", {})
            return TableDescription(
                ref=ref,
                id=row.id,
                kind=row.kind,
                metadata_location=None,
                columns=[ColumnSpec(name=n, type=t, nullable=True) for n, t in schema.items()],
                file_format=(row.external_config or {}).get("format"),
            )
        loaded = self._to_loaded_table(ref, row)
        unpublished = row.unpublished_run_id
        state: Literal["published", "not_published", "kept"] = "published"
        message = None
        hides = result_hider(self._session, self._authz, p, ref.environment)
        if unpublished is not None:
            state = "not_published"
            message = not_published_message(
                self._session, ref.dotted(), row.id, unpublished, ref.environment, hides
            )
        elif row.kept_run_id is not None:
            state, message = "kept", kept_message(row, shown_kept_check(self._session, row, hides))
        stats = snapshot_stats(loaded.metadata)
        return TableDescription(
            ref=ref,
            id=row.id,
            kind=row.kind,
            metadata_location=loaded.metadata_location,
            columns=from_iceberg_schema(loaded.metadata.schema()),
            snapshot_id=loaded.metadata.current_snapshot_id,
            state=state,
            state_message=message,
            schema_id=loaded.metadata.current_schema_id,
            last_write_at=stats.last_write_at,
            row_count=stats.row_count,
            size_bytes=stats.size_bytes,
            file_count=stats.file_count,
        )

    def _authorize_describe(self, p: PrincipalRef, ref: TableRef, row: Table) -> None:
        """Allow the table's owner, or a principal who may manage it, to change a description.

        Managing a table is the right to drop it (`DROP_TABLE`), which the data engineer role
        and the admin role hold; there is no separate action for it.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table reference.
        row
            Its row.

        Raises
        ------
        PermissionDenied
            If `p` neither owns the table nor holds `DROP_TABLE` on it.
        ValidationError
            If the row is an artifact, which has no description.
        """
        if row.owner_id != p.id:
            self._authz.authorize(
                p,
                Action.DROP_TABLE,
                Securable.table_scope(ref.environment, ref.namespace, ref.name),
            )
        if row.table_type is TableType.ARTIFACT:
            raise ValidationError(artifact_refusal(ref.dotted(), "a description"))

    def _find_table_to_describe(self, p: PrincipalRef, ref: TableRef) -> Table:
        """Find the table whose description `p` wants to change, and check `p` may.

        A name that holds no table is answered as a refusal when `p` could not have changed
        the description of a table of that name, so the answer does not tell what exists.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table reference.

        Returns
        -------
        Table
            The row.

        Raises
        ------
        NotFoundError
            If the table does not exist and `p` holds `DROP_TABLE` where it would be.
        PermissionDenied
            If `p` neither owns the table nor holds `DROP_TABLE` on it.
        ValidationError
            If the row is an artifact, which has no description.
        """
        try:
            _, row = self._find_table(ref)
        except NotFoundError:
            self._authz.authorize(
                p,
                Action.DROP_TABLE,
                Securable.table_scope(ref.environment, ref.namespace, ref.name),
            )
            raise
        self._authorize_describe(p, ref, row)
        return row

    def set_table_description(
        self,
        p: PrincipalRef,
        ref: TableRef,
        description: str | Keep | None = KEEP,
        *,
        label: str | Keep | None = KEEP,
        language: str | None = None,
    ) -> None:
        """Set or reset the edits of a table's description and label, in one language.

        An edit is shown instead of the project's text in its language. `None` removes the
        edit, so the project's text shows again; `KEEP` leaves a field as it is. One
        `TABLE_DESCRIBED` audit event records the change with the language and the lengths of
        the edits before and after, never the text.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table reference.
        description
            The new description, `None` to reset its edit, or `KEEP`.
        label
            The new label, `None` to reset its edit, or `KEEP`.
        language
            The canonical or any-case tag of the language edited; `None` for the table's main
            language.

        Raises
        ------
        NotFoundError
            If the table does not exist.
        PermissionDenied
            If `p` neither owns the table nor holds `DROP_TABLE` on it, or may not read it.
        ValidationError
            If both fields are `KEEP`, a text is blank or too long, a label has more than one
            line, `language` is not a tag Lakana takes, the edit would add a language past
            `MAX_EDIT_LANGUAGES`, or the row is an artifact.
        """
        row = self._find_table_to_describe(p, ref)
        self._authz.authorize(
            p, Action.READ_TABLE, Securable.table_scope(ref.environment, ref.namespace, ref.name)
        )
        detail = self._edit_texts(p, row, ref.dotted(), None, description, label, language)
        AuditService(self._session).record(
            p.id,
            AuditEventType.TABLE_DESCRIBED,
            securable=Securable.table_scope(ref.environment, ref.namespace, ref.name),
            detail={"table": ref.dotted(), **detail},
        )
        self._session.commit()

    def set_column_description(
        self,
        p: PrincipalRef,
        ref: TableRef,
        column: str,
        description: str | Keep | None = KEEP,
        *,
        label: str | Keep | None = KEEP,
        language: str | None = None,
    ) -> None:
        """Set or reset the edits of a column's description and label, in one language.

        Like `set_table_description`, with a `COLUMN_DESCRIBED` event that also names the column.
        The column must be in the table's schema now, and `p` must be able to read the table.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table reference.
        column
            The column's name.
        description
            The new description, `None` to reset its edit, or `KEEP`.
        label
            The new label, `None` to reset its edit, or `KEEP`.
        language
            The canonical or any-case tag of the language edited; `None` for the table's main
            language.

        Raises
        ------
        NotFoundError
            If the table or the column does not exist.
        PermissionDenied
            If `p` neither owns the table nor holds `DROP_TABLE` on it, or may not read it.
        ValidationError
            If both fields are `KEEP`, a text is blank or too long, a label has more than one
            line, `language` is not a tag Lakana takes, or the edit would add a language past
            `MAX_EDIT_LANGUAGES`.
        """
        row = self._find_table_to_describe(p, ref)
        names = [c.name for c in self.describe_table(p, ref).columns]
        if column not in names:
            have = ", ".join(names) if names else "none"
            raise NotFoundError(
                f"Table {ref.dotted()} has no column {shown(column)!r}. Its columns: {have}."
            )
        detail = self._edit_texts(p, row, ref.dotted(), column, description, label, language)
        AuditService(self._session).record(
            p.id,
            AuditEventType.COLUMN_DESCRIBED,
            securable=Securable.table_scope(ref.environment, ref.namespace, ref.name),
            detail={"table": ref.dotted(), "column": column, **detail},
        )
        self._session.commit()

    def _edit_texts(
        self,
        p: PrincipalRef,
        row: Table,
        dotted: str,
        column: str | None,
        description: str | Keep | None,
        label: str | Keep | None,
        language: str | None,
    ) -> dict[str, object]:
        """Check and store the edits of a table's or column's texts in one language.

        Everything is checked before anything is written. An edit in the table's main language
        goes onto the table or column row; one in another language is a translation edit.

        Parameters
        ----------
        p
            The acting principal, recorded as the editor.
        row
            The table, already authorized.
        dotted
            The table's dotted name, for the messages.
        column
            The column's name, already checked; `None` for the table itself.
        description
            The new description, `None` to reset its edit, or `KEEP`.
        label
            The new label, `None` to reset its edit, or `KEEP`.
        language
            The tag of the language edited, any case; `None` for the table's main language.

        Returns
        -------
        dict
            The audit detail: `language`, the lengths of each field sent, and `reset`.

        Raises
        ------
        ValidationError
            If both fields are `KEEP`, a text fails its rules, `language` is not a tag, or the
            edit would give the table a language past `MAX_EDIT_LANGUAGES`.
        """
        if isinstance(description, Keep) and isinstance(label, Keep):
            raise ValidationError("send `description`, `label` or both; null removes an edit")
        what = f"table {dotted}" if column is None else f"column {column} of table {dotted}"
        asked = None if language is None else self._edit_language(language)
        if not isinstance(description, Keep):
            description = check_text(description, what)
        if not isinstance(label, Keep):
            label = check_label(label, what)
        # Read again under the lock: the main language, the list and the table's edits as they
        # are now, not as the row was loaded.
        lock_table(self._session, row)
        main = row.text_language or self._main_language
        lang = asked or main
        listed = row.text_languages or []
        if (
            lang != main
            and lang not in listed
            and len(listed) >= MAX_EDIT_LANGUAGES
            and any(isinstance(sent, str) for sent in (description, label))
        ):
            raise ValidationError(
                f"Table {shown(dotted)} has texts in {MAX_EDIT_LANGUAGES} languages besides its "
                "main language, the most a table may have. To add "
                f"`{lang}`, first remove another language's texts: reset its edits "
                f"(`lakana table describe {shown(dotted)} --lang TAG --reset`, also with "
                "`--column` for each column that has one) and remove its file from translations/."
            )
        detail: dict[str, object] = {"language": lang}
        given: list[str | None] = []
        fields: tuple[tuple[Literal["description", "label"], str | Keep | None, str], ...] = (
            ("description", description, ""),
            ("label", label, "label_"),
        )
        for field, value, prefix in fields:
            if isinstance(value, Keep):
                continue
            if lang != main:
                old, new = edit_translation(
                    self._session,
                    row,
                    subject="table" if column is None else "column",
                    name=column or "",
                    language=lang,
                    field=field,
                    text=value,
                    by=p.id,
                )
            elif column is not None:
                old, new = edit_column(self._session, row.id, column, value, p.id, field=field)
            elif field == "description":
                old, new = edit_table(row, value)
            else:
                old, new = edit_table_label(row, value)
            detail[f"{prefix}old_length"], detail[f"{prefix}new_length"] = old, new
            given.append(value)
        detail["reset"] = all(sent is None for sent in given)
        return detail

    def _edit_language(self, language: str) -> str:
        """Read the language an edit names.

        Parameters
        ----------
        language
            The tag, any case.

        Returns
        -------
        str
            The canonical tag.

        Raises
        ------
        ValidationError
            With the tag text, for a tag outside the subset.
        """
        canonical = canonical_language(language.strip())
        if canonical is None:
            raise ValidationError(f"lang: {language_problem(language.strip())}")
        return canonical

    def can_read_table(self, p: PrincipalRef, ref: TableRef) -> bool:
        """Whether `p` holds READ_TABLE on a table, view or artifact, right now.

        The question a listing asks before it names an object: the same check
        `describe_table` and `ArtifactService.list_artifacts` make, asked as a
        yes or no instead of a refusal, so a listing leaves out what the caller
        may not read and never fails on it.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The object.

        Returns
        -------
        bool
            `True` if `p` may read it; nothing is looked up, so `False` says
            nothing about whether it exists.
        """
        return self._authz.permits(
            p, Action.READ_TABLE, Securable.table_scope(ref.environment, ref.namespace, ref.name)
        )

    def commit_answer(self, p: PrincipalRef, loaded: LoadedTable) -> dict[str, Any]:
        """Return the table metadata that the answer to a commit or a create may carry for `p`.

        The whole metadata names every snapshot with its manifest list and summary, every ref
        (a failed check's tag among them) and the table's properties. It goes only to a caller
        who could load the table now: one who holds READ_TABLE and whom neither
        `_require_published` nor `_require_not_kept` would refuse. A bound task token is no
        exception: it may write its own asset and not read it, so it gets what every writer
        without READ gets. That is the fields in `_ANSWER_WITHOUT_HISTORY`, with only the
        current schema, the default partition spec and the default sort order, which still
        parse as table metadata with no snapshot. The commit itself is decided elsewhere;
        this decides only what its answer shows.

        Parameters
        ----------
        p
            The caller who committed or created the table.
        loaded
            The table as the commit or the create left it.

        Returns
        -------
        dict
            The metadata in the JSON form of the Iceberg REST catalog: whole, or without the
            table's history.
        """
        metadata = json.loads(loaded.metadata.model_dump_json(by_alias=True, exclude_none=True))
        if self._may_see_state(p, loaded.ref):
            return metadata
        reduced = {key: value for key, value in metadata.items() if key in _ANSWER_WITHOUT_HISTORY}
        # Earlier schemas name columns the table no longer has, and earlier specs and orders
        # tell how it was laid out; a client parses the answer with the current ones alone.
        for listed, field, current in (
            ("schemas", "schema-id", "current-schema-id"),
            ("partition-specs", "spec-id", "default-spec-id"),
            ("sort-orders", "order-id", "default-sort-order-id"),
        ):
            reduced[listed] = [
                entry
                for entry in reduced.get(listed, [])
                if entry.get(field) == reduced.get(current)
            ]
        return reduced

    def _may_see_state(
        self, p: PrincipalRef, ref: TableRef, *, publication_only: bool = False
    ) -> bool:
        """Say whether `p` may see a table's state in what a commit tells it.

        The one rule for the answer's errors: the refused requirement and the refused update
        show their detail only to a caller who could load the table now. Whether the table is
        published, or has a current snapshot, is what the load itself tells every holder of
        READ_TABLE (its refusal of a table that is not published says so), so for that alone
        the grant is enough.

        Parameters
        ----------
        p
            The caller.
        ref
            The table.
        publication_only
            The detail is only whether the table is published or has a current snapshot.

        Returns
        -------
        bool
            `True` for a caller who holds READ_TABLE and whom the load would not refuse, or,
            with `publication_only`, who holds READ_TABLE.
        """
        readable = self._authz.permits(
            p, Action.READ_TABLE, Securable.table_scope(ref.environment, ref.namespace, ref.name)
        )
        if publication_only:
            return readable
        return self._may_see_history(p, ref, readable=readable)

    @contextlib.contextmanager
    def _state_withheld(
        self,
        p: PrincipalRef,
        ref: TableRef,
        kind: type[LakanaError],
        text: str,
        *,
        publication_only: bool = False,
    ) -> Generator[None, None, None]:
        """Replace the text of a refusal by a fixed one for a caller who may not see the state.

        Parameters
        ----------
        p
            The caller.
        ref
            The table.
        kind
            The refusal to catch; the fixed one is of the same kind, so the status stays.
        text
            The fixed text.
        publication_only
            Passed to `_may_see_state`.

        Yields
        ------
        None
            Control, for the checks whose texts may print the table's state.

        Raises
        ------
        LakanaError
            The refusal of `kind`: as it was for a caller who may see the state, else with
            `text`.
        """
        try:
            yield
        except kind:
            if not self._may_see_state(p, ref, publication_only=publication_only):
                raise kind(text) from None
            raise

    def _may_see_history(self, p: PrincipalRef, ref: TableRef, *, readable: bool) -> bool:
        """Say whether `p` may see a table's history in the answer to its commit or create.

        Parameters
        ----------
        p
            The caller.
        ref
            The table.
        readable
            Whether `p` holds READ_TABLE on it.

        Returns
        -------
        bool
            `True` for a caller who holds READ_TABLE and could load the table now; `False`
            otherwise, also when the table is gone.
        """
        if not readable:
            return False
        try:
            ns, row = self._find_table(ref)
            self._require_published(p, ref, row)
            self._require_not_kept(p, ref, ns, row)
        except (NotFoundError, NotPublishedError, KeptAfterCheckError):
            return False
        return True

    def _can_read_reference(
        self, p: PrincipalRef, environment: str, reference: Mapping[str, str]
    ) -> bool:
        """Whether `p` holds READ_TABLE on a view's stored reference, right now.

        Checked by the reference's *current* location (the
        approach of lineage pruning), not its recorded name: a stale
        reference — dropped or renamed since the view was created — is
        treated as unreadable rather than raising, since `describe_table`
        only describes, it does not expand or execute the view.

        Parameters
        ----------
        p
            The acting principal.
        environment
            The view's environment; every reference lives in it too.
        reference
            One `{"id", "name"}` entry from a view's stored `references`.

        Returns
        -------
        bool
            `True` if the referenced table or view still exists and `p`
            currently holds `READ_TABLE` on it; `False` if it was dropped
            or `p` may not read it.
        """
        current = self._session.get(Table, uuid.UUID(reference["id"]))
        if current is None:
            return False
        path = self._session.execute(
            select(Namespace.path).where(Namespace.id == current.namespace_id)
        ).scalar_one()
        securable = Securable.table_scope(environment, NamespacePath.parse(path), current.name)
        return self._authz.permits(p, Action.READ_TABLE, securable)

    def _to_loaded_table(self, ref: TableRef, row: Table) -> LoadedTable:
        """Build a `LoadedTable` for an already-resolved row, without authorizing.

        Factored out of `load_table` so that `rename_table` can return the
        table at its new reference without a second, redundant
        authorization: it already authorized `DROP_TABLE` on the source and
        `CREATE_TABLE` on the destination, and reading the row it just
        renamed does not need `READ_TABLE` on top of those.

        Parameters
        ----------
        ref
            How the table is addressed.
        row
            The resolved catalog row.

        Returns
        -------
        LoadedTable
            The table and its current metadata.

        Raises
        ------
        NotFoundError
            If the row has no metadata pointer.
        StorageUnavailableError
            If the environment's storage did not answer while the metadata was read
            (`TABLE_STORAGE_UNREACHABLE`, which names the environment and the next step). The
            log line has the error's class, never its text, which can name a host or a path.
        """
        if row.metadata_location is None:
            raise NotFoundError(f"Table {ref.name!r} has no metadata yet.")
        resolved = self._storage.for_environment(ref.environment)
        try:
            metadata = read_metadata(resolved.file_io, row.metadata_location)
        except Exception as exc:
            if not is_storage_unavailable(exc):
                raise
            logger.warning(
                "The storage of environment %s could not be reached while table %s was read (%s).",
                shown(ref.environment),
                shown(ref.name),
                type(exc).__name__,
                extra=log_event(LogEvent.STORAGE_UNREACHABLE),
            )
            raise StorageUnavailableError(
                TABLE_STORAGE_UNREACHABLE.format(environment=shown(ref.environment))
            ) from None
        return LoadedTable(
            ref=ref,
            uuid=row.id,
            metadata_location=row.metadata_location,
            metadata=metadata,
            text_language=row.text_language,
        )

    def commit_table(
        self,
        p: PrincipalRef,
        ref: TableRef,
        *,
        requirements: Sequence[TableRequirement],
        updates: Sequence[TableUpdate],
        action: Action = Action.WRITE_TABLE,
        expected_metadata_location: str | None = None,
        internal_rollback: bool = False,
        after_swap: Callable[[], None] | None = None,
        own_data_files: bool = False,
        release_session: bool = False,
    ) -> LoadedTable:
        """Apply Iceberg updates to a table and publish them atomically.

        The order is mandatory (architecture Constraint 3): the new metadata
        file is written to object storage *before* the pointer is moved. A
        writer that loses the compare-and-swap has written a file nobody
        references, which is recoverable garbage; a writer that moved the
        pointer first would have published a reference to a file that may
        not exist, which is corruption.

        `updates` can include a `SetPropertiesUpdate` that sets
        `write.metadata.path`, `write.data.path`, `write.object-storage.path`,
        or `write.py-location-provider.impl`, exactly as a `properties=`
        argument to `create_table` can, so `updated.properties` is validated
        with the same
        :meth:`lakana_core.storage.resolver.ResolvedStorage.validate_table_properties`
        guard `create_table` uses — all four are refused outright (the first three used to be merely
        confined to the storage root, which was not enough to stop one
        redirecting a table's metadata or data to a location colliding with
        another table's own). A `SetLocationUpdate` is likewise refused
        outright: a table's location is immutable once created,
        so there is no legitimate value for it to move to. An
        `AddSnapshotUpdate` whose `snapshot.manifest_list` does not lie
        under the table's own (immutable) location is refused for the same
        reason — a committed snapshot is itself Iceberg metadata pointing at
        further metadata, so it is exactly as capable of naming a location
        outside the table's own as any of the properties above.

        Two rules hold. No commit, maintenance included, may leave a table that has a
        current snapshot without one (readers would see an empty table); `roll_back` is the
        one caller exempt. And a commit that is not maintenance may give a not-published
        table (`Table.unpublished_run_id`) a current snapshot only by adding that snapshot,
        never by pointing `main` at one that exists (the snapshot a failed check kept under
        its tag). A commit that does leave the table with a current snapshot clears the
        not-published marker in the same statement that moves the pointer.

        A writer that loses the compare-and-swap surfaces as `ConflictError`
        on SQLite as elsewhere in the common case, but is not guaranteed to:
        a winner that holds its transaction open past pysqlite's five-second
        busy timeout instead surfaces `OperationalError: database is
        locked` to the loser. `ConflictError` is not the only possible
        outcome of a lost race.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table reference.
        requirements
            Conditions the caller asserts hold, checked before anything is
            written.
        updates
            Iceberg updates to apply.
        action
            `Action.WRITE_TABLE` (the default) or `Action.MAINTAIN_TABLE`
            for maintenance.

        expected_metadata_location
            When given, the commit only proceeds if the table's current
            metadata file is exactly this one, so updates derived from a
            particular snapshot of the metadata (snapshot expiry) can't
            apply to anything newer. `None` accepts whatever is current.
        internal_rollback
            Only `CatalogService.roll_back` passes `True`: the commit may then remove the
            table's current snapshot. No request, route or token reaches this parameter.
        after_swap
            Only `CatalogService.roll_back` passes one: called right after the pointer swap
            and before the guard's last check, so the rollback's own statements run in the
            swap's transaction, after the metadata file was uploaded and in the lock order of a
            write (`tables`, then `task_runs`). No request, route or token reaches it.
        own_data_files
            Only `CatalogService.append`, `merge` and `write_batches` pass `True`: this service
            wrote the commit's data files through pyiceberg, which always writes field ids, so
            their footers are not read again. No request, route or token reaches this parameter.
        release_session
            Only the Iceberg REST commit route passes `True`: the database transaction is
            ended before the field-id check reads manifests and footers, so that no pooled
            connection sits idle for up to a minute. The session must not have written anything
            in its transaction (see `_release_session`).

        Returns
        -------
        LoadedTable
            The table at its new metadata location.

        Raises
        ------
        NotFoundError
            If the table does not exist or has no metadata yet.
        ValidationError
            If `updates` includes a `SetLocationUpdate` or an `AssignUUIDUpdate` with another uuid;
            if a caller who may not read the table makes an earlier schema, partition spec or
            sort order current; if `updates` add a schema, partition spec or sort order
            under an id the table already has; if `updates` set a
            table property that `ResolvedStorage.validate_table_properties`
            refuses; if `updates` includes an `AddSnapshotUpdate` whose
            `snapshot.manifest_list` does not lie under the table's own
            location; if a snapshot the commit adds names a data file that is not Parquet,
            cannot be read, or has no Iceberg field ids
            (`field_ids.check_added_files_have_field_ids`; also if there are more than 2,000 such
            files or the check takes over a minute), unless `own_data_files`; if the
            metadata location computed from the table's
            (unchanged) location falls outside the environment's storage
            root; if applying `updates` produces metadata pyiceberg
            rejects as invalid; if the commit would remove the table's current snapshot
            (`internal_rollback` off); if the table is not published and the commit leaves
            it a current snapshot that the commit does not add; or if `action` is
            `MAINTAIN_TABLE` and
            `updates` holds anything but `RemoveSnapshotsUpdate` or the removal of a tag
            named like `failed_snapshot_tag`'s (the protected-environment lockdown's
            maintenance exemption is structural).
        StorageUnavailableError
            If storage failed while the field-id check read a manifest or a footer.
        RuntimeError
            If `release_session` is set and the session wrote in its transaction: a programming
            error, nothing is released and nothing applied.
        ConflictError
            If a requirement fails, or another writer committed first.
        PermissionDenied
            If the principal may not write to this table, or the environment
            is protected and this write has neither a covering run context
            nor an authorized break-glass; or, outside maintenance and the internal
            rollback, if `updates` removes snapshots, removes or sets a ref named like the tag
            of a failed check, or removes schemas or partition specs.
        ValueError
            If `action` is not one a commit may be authorized under.
        """
        maintenance = action is Action.MAINTAIN_TABLE
        if maintenance and not all(_is_maintenance_update(u) for u in updates):
            raise ValidationError(
                "A maintenance commit may only expire snapshots or remove the tag of a failed "
                "check; it cannot carry a data or schema change."
            )
        if action not in _COMMIT_ACTIONS:
            raise ValueError(f"commit_table authorizes a write, not {action.value}.")
        self._authz.authorize(
            p, action, Securable.table_scope(ref.environment, ref.namespace, ref.name)
        )
        if not maintenance and not internal_rollback:
            for refused in map(_refusal_before_lookup, updates):
                if refused is not None:
                    raise PermissionDenied(refused)
        if not maintenance:
            self._guard.check(
                p,
                ref.environment,
                WriteOperation.COMMIT,
                table_target(ref),
                rollback=internal_rollback,
            )
        _, row = self._find_table(ref)
        if row.table_type is TableType.ARTIFACT:
            raise NotFoundError(_not_exposed_artifact_message(ref.dotted()))
        if row.metadata_location is None:
            raise NotFoundError(f"Table {ref.name!r} has no metadata yet.")
        # Captured before the CAS and used for everything after it,
        # including the final `LoadedTable`. This is what `_publish_table`
        # does with `namespace_id` around its own commit, for the same
        # reason: `expire_on_commit` means any attribute read on `row`
        # after `self._session.commit()` issues a refresh `SELECT`, which
        # raises a raw `ObjectDeletedError` if the row was dropped between
        # the CAS and the return -- for a commit that already succeeded.
        # It also means `row.metadata_location` is never assigned before
        # `commit_metadata_pointer` runs, which is what keeps this clear of
        # the autoflush trap: nothing here can flush a pending write ahead
        # of the CAS's own `UPDATE` for it to compare against.
        table_id = row.id
        unpublished = row.unpublished_run_id is not None
        expected = row.metadata_location
        if expected_metadata_location is not None and expected != expected_metadata_location:
            raise ConflictError(f"{ref.dotted()} changed since the update was prepared.")

        resolved = self._storage.for_environment(ref.environment)
        with _storage_errors_as_lakana(internal_rollback):
            current = read_metadata(resolved.file_io, expected)
        with self._state_withheld(p, ref, ConflictError, _CHANGED_SINCE_YOUR_METADATA):
            check_requirements(current, requirements)
        # A table's location is assigned once, at `create_table`, and never
        # moves again: the id suffix `table_location` gives it
        # is what lets the worker trust that every data file under it
        # belongs to this table, and a `SetLocationUpdate` would invalidate
        # that trust for every file already committed under the old
        # location. Refused outright, before `updates` is even applied, so
        # this never depends on where the new location would have pointed.
        #
        # `AddSnapshotUpdate` is checked in the same pass: its `snapshot`
        # carries a `manifest_list` pointing at Iceberg's own metadata, not
        # the catalog's, and nothing about the location guard above inspects
        # it. A `manifest_list` outside the table's own (immutable) location
        # would let a committed snapshot vouch for manifests -- and, through
        # them, data files -- that live anywhere the credential can reach.
        # This is a shallow check on the pointer itself, not a walk of what
        # it points to: the worker (`lakana_worker.pipeline`) re-checks it at
        # query time, along with every manifest and data file the manifest
        # list leads to, which is the authoritative, defence-in-depth check
        # .
        known_schemas = {schema.schema_id for schema in current.schemas}
        known_specs = {spec.spec_id for spec in current.partition_specs}
        known_orders = {order.order_id for order in current.sort_orders}
        added_layouts: set[tuple[str, int]] = set()
        for table_update in updates:
            if isinstance(table_update, SetLocationUpdate):
                raise ValidationError("A table's location cannot change.")
            if (
                isinstance(table_update, AssignUUIDUpdate)
                and table_update.uuid != current.table_uuid
            ):
                raise ValidationError(_UUID_CANNOT_CHANGE)
            if isinstance(table_update, AddSchemaUpdate):
                if table_update.schema_.schema_id in known_schemas:
                    raise ValidationError(_SCHEMA_ID_TAKEN)
                known_schemas.add(table_update.schema_.schema_id)
            if isinstance(table_update, AddPartitionSpecUpdate):
                if table_update.spec.spec_id in known_specs:
                    raise ValidationError(_SPEC_ID_TAKEN)
                known_specs.add(table_update.spec.spec_id)
            if isinstance(table_update, AddSortOrderUpdate):
                if table_update.sort_order.order_id in known_orders:
                    raise ValidationError(_SORT_ORDER_ID_TAKEN)
                known_orders.add(table_update.sort_order.order_id)
            added_layouts.update(_layouts_added(table_update))
            switch = _layout_switch(table_update)
            if (
                switch is not None
                and not internal_rollback
                and switch not in added_layouts
                and switch[1] != -1
                and not self._may_see_state(p, ref)
            ):
                raise ValidationError(_EARLIER_SCHEMA_NEEDS_READ)
            if isinstance(table_update, AddSnapshotUpdate) and not is_within_location(
                current.location, table_update.snapshot.manifest_list
            ):
                raise ValidationError(
                    "A committed snapshot's manifest list must lie under the table's own location."
                )
        if not maintenance and not internal_rollback and not own_data_files:
            # A file without field ids would read as NULL in every column: refused here, at
            # the one commit path other clients reach, after the manifest list is known to lie
            # under the table's location.
            added_snapshots = [u.snapshot for u in updates if isinstance(u, AddSnapshotUpdate)]
            if added_snapshots:
                # The reads can take a minute: no pooled connection sits idle in a transaction
                # meanwhile. The pointer swap below compares against `expected`, so a table
                # that changed meanwhile fails it like any concurrent commit. (It does not
                # compare `unpublished_run_id`, which the set-back of a first build without
                # rows clears without moving the pointer; that window existed before, and
                # such a table has no earlier snapshot for a ref to be pointed at.)
                if release_session:
                    self._release_session()
                check_added_files_have_field_ids(
                    added_snapshots, current, resolved.file_io, ref.dotted()
                )
        if any(isinstance(u, RemovePropertiesUpdate) for u in updates) and not (
            self._may_see_state(p, ref)
        ):
            # pyiceberg refuses to remove a key the table does not have, which would tell this
            # caller which keys it has; for it, such a removal changes nothing, as in Iceberg.
            updates = [_present_removals(u, current.properties) for u in updates]
        # Only the catalog says which task attempt made a snapshot (#49): a caller's own claim
        # is removed, and a bound task token's write is stamped.
        updates = stamp_snapshots(updates, p.run_context)
        added = {u.snapshot.snapshot_id for u in updates if isinstance(u, AddSnapshotUpdate)}
        with self._state_withheld(p, ref, ValidationError, _UPDATE_DOES_NOT_FIT):
            updated = apply_updates(current, updates)
        # A write of this service found and recorded its own column change (`write_batches`);
        # every other commit (an Iceberg client's) is looked at here.
        schema_change = (
            None
            if maintenance or internal_rollback or own_data_files
            else self._commit_schema_change(ref, table_id, current, updated)
        )
        had, has = current.current_snapshot_id, updated.current_snapshot_id
        with self._state_withheld(
            p, ref, ValidationError, _UPDATE_DOES_NOT_FIT, publication_only=True
        ):
            if had is not None and has is None and not internal_rollback:
                raise ValidationError(_NO_UNPUBLISHING)
            if not maintenance and unpublished and has is not None and has not in added:
                raise ValidationError(_NO_SIDE_DOOR.format(table=ref.dotted()))

        # Same guard `create_table` applies to `properties=`: a commit
        # update can set the same properties, reaching the same
        # `next_metadata_location` call with the same power to escape the
        # storage root or import an arbitrary class.
        resolved.validate_table_properties(updated.properties)
        # Belt and braces: `updated.location` can now only ever equal
        # `current.location` (a `SetLocationUpdate` is refused above), which
        # was itself confined when the table was created. Confining it again
        # here costs nothing and catches a future update type that changes
        # `location` by some path this method does not yet know about.
        with self._state_withheld(p, ref, ValidationError, _UPDATE_DOES_NOT_FIT):
            location = resolved.confine_to_root(updated.location)
            target = next_metadata_location(
                table_location=location,
                current_location=expected,
                properties=updated.properties,
            )
            # Belt and braces, as in `create_table`: confine what the location
            # provider actually produced, not just the property that fed it.
            target = resolved.confine_to_root(target)
        with _storage_errors_as_lakana(internal_rollback):
            written = write_metadata(resolved.file_io, updated, target)

        # Noted after every refusal above, so a commit that is refused leaves no note behind on
        # the guard for a later commit of the same target to take as its own.
        if (
            not maintenance
            and not internal_rollback
            and has is not None
            and has in added
            and has != had
        ):
            self._guard.note_own_commit(
                p,
                ref.environment,
                table_target(ref),
                rows=_added_records(updated.snapshot_by_id(has)),
                snapshot_id=has,
                table_id=table_id,
                snapshot_before=had,
                publishes=unpublished,
            )
        if schema_change is not None:
            AuditService(self._session).record(
                p.id,
                AuditEventType.TABLE_SCHEMA_CHANGED,
                securable=Securable.table_scope(ref.environment, ref.namespace, ref.name),
                detail={
                    "target": ref.dotted(),
                    "principal": p.name,
                    "broken_policies": list(schema_change),
                },
            )
            self._guard.note_schema_change(
                ref.environment, table_target(ref), table=ref.dotted(), policies=schema_change
            )
        commit_metadata_pointer(
            self._session,
            table_id,
            expected_location=expected,
            new_location=written,
            publish=not maintenance and updated.current_snapshot_id is not None,
        )
        sync_table_columns(self._session, table_id, from_iceberg_schema(updated.schema()))
        if after_swap is not None:
            after_swap()
        # A maintenance commit never passed the guard's `check`, so whatever
        # is pending belongs to an earlier, unfinished write and must not land.
        if not maintenance:
            self._guard.record_pending()
        self._commit_sent = True
        self._session.commit()
        return LoadedTable(ref=ref, uuid=table_id, metadata_location=written, metadata=updated)

    def _commit_schema_change(
        self,
        ref: TableRef,
        table_id: uuid.UUID,
        current: TableMetadata,
        updated: TableMetadata,
    ) -> tuple[dict[str, str], ...] | None:
        """Find the column change of an Iceberg commit and the policies it leaves out of date.

        Parameters
        ----------
        ref
            The table.
        table_id
            The table's id.
        current
            The metadata before the commit.
        updated
            The metadata the commit makes current.

        Returns
        -------
        tuple of dict or None
            The policies, as stored (possibly none), when the commit removes or replaces a column;
            `None` when it does not.
        """
        if updated.current_schema_id == current.current_schema_id:
            return None
        changed = changed_columns(current.schema(), updated.schema())
        if not changed:
            return None
        broken = broken_policies(self._session, table_id, ref.dotted(), changed)
        return tuple(warning.to_json() for warning in broken)

    def roll_back(
        self,
        p: PrincipalRef,
        ref: TableRef,
        *,
        run_id: uuid.UUID,
        task_run_id: uuid.UUID,
        written: Written,
        failed_checks: Sequence[str],
        foreign_change: bool = False,
    ) -> Rollback:
        """Set a table back to what it was before a run's write, if it still is what the run wrote.

        The one operation that undoes a write. It decides by what it
        finds: the table must still be the table the run wrote (same id) and its current
        snapshot must still be the snapshot the run made. Then, in one metadata commit that
        requires exactly that, it sets `main` back to the snapshot before (and the schema the
        earlier snapshot was written with), tags the run's snapshot, records the audit event
        and the outcome on the failed blocking results and clears the task run's *written*
        stamp. A first build (`written.publishes`) instead loses its current snapshot and is
        marked not published. Anyone else's commit since the run's write moves the snapshot or
        the metadata pointer, so the requirement fails and nothing is discarded; a pointer that
        moved without a new snapshot (a property change) is tried once more.

        Authorized as a write of the table by the acting principal and held by the write guard
        like any commit; no route offers it.

        Parameters
        ----------
        p
            The acting principal: the run's principal.
        ref
            The checked table.
        run_id
            The run whose check failed.
        task_run_id
            The task run whose write is set back.
        written
            What the task run's write did.
        failed_checks
            The names of the failed blocking checks, for the audit event.
        foreign_change
            The caller found the table changed although the task run has no write of its own
            (a Python task run without a tip, `TaskRun.own_snapshot_id`): what differs from
            before is somebody else's. Nothing is looked at and nothing is touched; the outcome
            is ``changed_since`` with `CHANGED_BY_ANOTHER_REASON`, authorized, guarded and
            recorded like any other. Without it a `Written` that names no snapshot would read
            as "nothing written", or as a first build to mark not published.

        Returns
        -------
        Rollback
            The outcome, and for `FAILED` the reason.

        Raises
        ------
        PermissionDenied
            If `p` may not write the table, or the write guard refuses (a superseded claim,
            another asset, a protected environment without a covering run context).
        """
        securable = Securable.table_scope(ref.environment, ref.namespace, ref.name)
        self._authz.authorize(p, Action.WRITE_TABLE, securable)
        # What an earlier write of this service noted and then failed to commit is not this
        # rollback's evidence.
        self._guard.forget_result()
        self._guard.check(
            p, ref.environment, WriteOperation.COMMIT, table_target(ref), rollback=True
        )
        self._require_own_task(p, ref, task_run_id)
        claim = self._session.execute(
            select(TaskRun.claim_count).where(TaskRun.id == task_run_id)
        ).scalar_one_or_none()
        detail: dict[str, object] = {
            "task_run_id": str(task_run_id),
            "claim": claim,
            "run_id": str(run_id),
            "table": ref.dotted(),
            "before": written.snapshot_before,
            "after": written.snapshot_after,
            "checks": list(failed_checks),
        }
        result = Rollback(RollbackOutcome.FAILED, _KEPT_CHANGING)
        if foreign_change:
            result = Rollback(RollbackOutcome.CHANGED_SINCE, CHANGED_BY_ANOTHER_REASON)
        for _ in range(0 if foreign_change else 2):
            try:
                result = self._roll_back_once(
                    p, ref, securable, run_id, task_run_id, written, detail
                )
            except ConflictError:
                self._rollback_quietly()
                continue
            except PermissionDenied:
                raise
            except NotFoundError:
                self._rollback_quietly()
                result = Rollback(RollbackOutcome.CHANGED_SINCE, _DROPPED)
            except _RollbackReason as exc:
                self._rollback_quietly()
                result = Rollback(RollbackOutcome.FAILED, str(exc))
            except LakanaError as exc:
                # Not a text this module worded: it can name a storage location (unreadable
                # metadata does), and the reason is shown to whoever may read the run. The
                # full text is the operator's.
                logger.warning(
                    "The rollback of %s failed: %s: %s",
                    shown(ref.dotted()),
                    type(exc).__name__,
                    exc,
                )
                self._rollback_quietly()
                result = Rollback(
                    RollbackOutcome.FAILED, _STORAGE_FAILED.format(error=type(exc).__name__)
                )
            except (DBAPIError, OSError) as exc:
                result = self._after_database_error(exc, ref, task_run_id, claim, written)
            break
        if (
            result.outcome not in _COMMITTED_BY_THE_ROLLBACK
            and result.reason != ROLLBACK_NOT_KNOWN_REASON
        ):
            try:
                # The claim is checked again, and then once more under lock by `record_pending`:
                # a commit that failed has consumed the guard's earlier check, and the task may
                # have been claimed again (and rolled back by the new claim) since. A stale
                # attempt records nothing; it is refused as any stale write is.
                self._guard.check(
                    p, ref.environment, WriteOperation.COMMIT, table_target(ref), rollback=True
                )
                self._record_rollback(p, securable, task_run_id, result.outcome, detail)
                self._guard.record_pending()
                self._session.commit()
            except (DBAPIError, OSError) as exc:
                logger.warning(
                    "The database failed while recording a rollback of %s: %s",
                    shown(ref.dotted()),
                    type(exc).__name__,
                )
                self._rollback_quietly()
        return result

    def _after_database_error(
        self,
        exc: Exception,
        ref: TableRef,
        task_run_id: uuid.UUID,
        claim: int | None,
        written: Written,
    ) -> Rollback:
        """Say what became of a rollback during which the database failed.

        The failure may have come after the server applied the commit (a lost acknowledgement),
        so the answer is read back before anything is recorded: the commit landed if and only
        if this rollback's own audit event is there (see `_rollback_landed`). A rollback that
        landed already wrote its event and the results' outcome in its own transaction.

        The catch is wide on purpose (`DBAPIError`, and the raw `OSError` some drivers raise
        after a server-side cut). `DBAPIError` also covers integrity and programming errors,
        which would be a bug here, so every catch is logged with the error's class and the
        table, never SQL or parameters.

        Parameters
        ----------
        exc
            The error.
        ref
            The checked table.
        task_run_id
            The task run whose write is set back.
        claim
            The claim this attempt runs under.
        written
            What the task run's write did.

        Returns
        -------
        Rollback
            The committed outcome if it landed, `FAILED` with `ROLLBACK_REFUSED_REASON` if it
            did not, `FAILED` with `ROLLBACK_NOT_KNOWN_REASON` if that cannot be found out.
        """
        logger.warning(
            "The database failed during the rollback of %s: %s",
            shown(ref.dotted()),
            type(exc).__name__,
        )
        self._rollback_quietly()
        try:
            landed = self._rollback_landed(ref, task_run_id, claim, written.table_id)
        except Exception as reading:
            logger.warning(
                "The outcome of the rollback of %s could not be read back: %s",
                shown(ref.dotted()),
                type(reading).__name__,
            )
            self._rollback_quietly()
            return Rollback(RollbackOutcome.FAILED, ROLLBACK_NOT_KNOWN_REASON)
        if landed is not None:
            return Rollback(landed)
        return Rollback(RollbackOutcome.FAILED, ROLLBACK_REFUSED_REASON)

    def _rollback_landed(
        self, ref: TableRef, task_run_id: uuid.UUID, claim: int | None, table_id: uuid.UUID
    ) -> RollbackOutcome | None:
        """Say whether a rollback's commit reached the database, from its own audit event.

        The event of outcome `rolled_back` or `unpublished` for this task run and this claim is
        inserted in the rollback's transaction and nowhere else, and no later commit can hide
        it; the table's present state is not asked (someone else may have written since, and a
        marker an earlier attempt left would be taken for this attempt's).

        Read fresh on a new transaction. On PostgreSQL the table's row is first read with a
        locking read (`FOR SHARE`, bounded by `LOCK_TIMEOUT_SECONDS`) as `_commit_landed`
        does: the rollback's transaction holds that row's lock from its `UPDATE` until it ends,
        so a commit still inside its `COMMIT` is waited for and its event is then visible. No
        transaction is left open.

        Parameters
        ----------
        ref
            The checked table.
        task_run_id
            The task run whose write is set back.
        claim
            The claim of the attempt that asks.
        table_id
            The table's id, for the locking read.

        Returns
        -------
        RollbackOutcome or None
            The committed outcome the event names, or `None` if this attempt left no event.
        """
        try:
            if self._session.get_bind().dialect.name == "postgresql":
                self._session.execute(
                    text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_SECONDS}s'")  # nosec B608
                )
            self._session.execute(
                select(Table.id).where(Table.id == table_id).with_for_update(read=True)
            ).first()
            details = self._session.execute(
                select(AuditEvent.detail).where(
                    AuditEvent.event_type == AuditEventType.TABLE_ROLLED_BACK,
                    AuditEvent.securable_environment == ref.environment,
                    AuditEvent.securable_table == ref.name,
                )
            ).scalars()
            for detail in details:
                if (
                    detail.get("task_run_id") == str(task_run_id)
                    and detail.get("claim") == claim
                    and detail.get("outcome") in _LANDED_OUTCOMES
                ):
                    return RollbackOutcome(detail["outcome"])
            return None
        finally:
            self._session.rollback()

    def _require_own_task(self, p: PrincipalRef, ref: TableRef, task_run_id: uuid.UUID) -> None:
        """Refuse a run context that is not the task whose write is being set back.

        The guard decides whether a context is live and, in a protected environment or in the
        claim form, whether the target is the task's own asset. The query-run form in an
        unprotected environment is left to grants by the guard; a rollback is held to the
        task's own asset in every form, and the ids the caller passes must be the context's.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The checked table.
        task_run_id
            The task run the caller says it is rolling back for.

        Raises
        ------
        PermissionDenied
            With `OTHER_ASSET_MESSAGE` if the principal's context names another task run than
            `task_run_id`, or its task's asset is not `ref`.
        """
        context = p.run_context
        if context is None:
            return
        asset = self._session.execute(
            select(TaskRun.asset).where(TaskRun.id == context.task_run_id)
        ).scalar_one_or_none()
        if (
            context.task_run_id != task_run_id
            or asset is None
            or tuple(asset.split(".")) != table_target(ref)
        ):
            raise PermissionDenied(
                OTHER_ASSET_MESSAGE.format(
                    asset=shown(asset or ""), operation="commit", target=shown(ref.dotted())
                )
            )

    def _roll_back_once(
        self,
        p: PrincipalRef,
        ref: TableRef,
        securable: Securable,
        run_id: uuid.UUID,
        task_run_id: uuid.UUID,
        written: Written,
        detail: dict[str, object],
    ) -> Rollback:
        """Decide by what the table is now, and commit when there is something to set back.

        Everything the rollback changes (the marker, the cleared stamp, the note put back when
        the table's note names this run's data, the audit event, the results, the pointer) is one
        transaction: `commit_table` commits it, and a
        `ConflictError` rolls all of it back in the caller. For an outcome that changes
        nothing, the caller records the event.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The checked table.
        securable
            Its securable.
        run_id
            The run whose check failed.
        task_run_id
            The task run whose write is set back.
        written
            What the task run's write did.
        detail
            The audit detail so far; gains the schema ids on the path that moves `main`.

        Returns
        -------
        Rollback
            The outcome.

        Raises
        ------
        _RollbackReason
            With `_BEFORE_IS_GONE` if the snapshot to go back to no longer exists.
        ConflictError
            If another writer committed since the table was read.
        """
        try:
            _, row = self._find_table(ref)
        except NotFoundError:
            return Rollback(RollbackOutcome.CHANGED_SINCE, _DROPPED)
        if row.table_type is not TableType.ICEBERG:
            return Rollback(RollbackOutcome.NOT_A_TABLE)
        if row.id != written.table_id or row.metadata_location is None:
            return Rollback(RollbackOutcome.CHANGED_SINCE)
        table_id, expected = row.id, row.metadata_location
        resolved = self._storage.for_environment(ref.environment)  # reads the database
        with _storage_errors_as_lakana(True):
            current = read_metadata(resolved.file_io, expected)
        before, after = written.snapshot_before, written.snapshot_after
        if after is None and not written.publishes:
            return Rollback(RollbackOutcome.NOTHING_WRITTEN)
        if current.current_snapshot_id != after:
            return Rollback(RollbackOutcome.CHANGED_SINCE)
        outcome = RollbackOutcome.UNPUBLISHED if written.publishes else RollbackOutcome.ROLLED_BACK
        updates: list[TableUpdate] = []
        if after is not None:
            updates.append(
                SetSnapshotRefUpdate(
                    ref_name=failed_snapshot_tag(run_id),
                    type="tag",
                    snapshot_id=after,
                    max_ref_age_ms=FAILED_SNAPSHOT_KEEP_DAYS * 24 * 60 * 60 * 1000,
                )
            )
            if written.publishes or before is None:
                updates.append(RemoveSnapshotRefUpdate(ref_name="main"))
            else:
                before_snapshot, after_snapshot = (
                    current.snapshot_by_id(before),
                    current.snapshot_by_id(after),
                )
                if before_snapshot is None or after_snapshot is None:
                    raise _RollbackReason(_BEFORE_IS_GONE)
                schema_before, schema_after = before_snapshot.schema_id, after_snapshot.schema_id
                detail.update(schema_before=schema_before, schema_after=schema_after)
                updates.append(
                    SetSnapshotRefUpdate(ref_name="main", type="branch", snapshot_id=before)
                )
                if (
                    schema_before is not None
                    and schema_before != schema_after
                    and schema_after == current.current_schema_id
                ):
                    updates.append(SetCurrentSchemaUpdate(schema_id=schema_before))
        restores_schema = any(isinstance(u, SetCurrentSchemaUpdate) for u in updates)

        def settle() -> None:
            """Mark, clear the stamp, restore the note and record: the statements after the swap.

            A set-back that makes the schema before current again also drops the task run's
            column change (`TaskRun.schema_change_json`): the columns and their field ids are
            the old ones again, so neither the note, the warnings nor the notification line may
            say otherwise.
            """
            if restores_schema:
                self._session.execute(
                    update(TaskRun)
                    .where(TaskRun.id == task_run_id)
                    .values(schema_change_json=null())
                    .execution_options(synchronize_session=False)
                )
            if written.publishes:
                self._session.execute(
                    update(Table)
                    .where(Table.id == table_id)
                    .values(unpublished_run_id=run_id)
                    .execution_options(synchronize_session=False)
                )
            self._clear_stamp(task_run_id)
            restored = restore_kept(
                self._session,
                table_id,
                run_id=run_id,
                task_run_id=task_run_id,
                snapshot_before=written.snapshot_before,
                snapshot_after=written.snapshot_after,
            )
            self._record_rollback(
                p, securable, task_run_id, outcome, {**detail, **(restored or {})}
            )

        if not updates:  # a first build without rows: only the marker, no pointer swap
            # Conditional on the pointer this decision was made on: a commit that landed since
            # (it moves the pointer) must not find a marker on a table that now has data. Short
            # transaction, no storage I/O between its statements.
            marked = self._session.execute(
                update(Table)
                .where(Table.id == table_id, Table.metadata_location == expected)
                .values(unpublished_run_id=run_id)
                .execution_options(synchronize_session=False)
            )
            if rowcount(marked) != 1:
                raise ConflictError(f"{ref.dotted()} changed since the rollback was prepared.")
            self._clear_stamp(task_run_id)
            restored = restore_kept(
                self._session,
                table_id,
                run_id=run_id,
                task_run_id=task_run_id,
                snapshot_before=written.snapshot_before,
                snapshot_after=written.snapshot_after,
            )
            self._record_rollback(
                p, securable, task_run_id, outcome, {**detail, **(restored or {})}
            )
            self._guard.record_pending()
            self._session.commit()
            return Rollback(outcome)
        # The statements of `settle` run after the metadata file is uploaded and the pointer
        # swapped, so that no row lock is held across the upload and the lock order is a
        # write's (`tables`, then `task_runs`).
        self.commit_table(
            p,
            ref,
            requirements=[
                AssertTableUUID(uuid=table_id),
                AssertRefSnapshotId(ref="main", snapshot_id=after),
            ],
            updates=updates,
            expected_metadata_location=expected,
            internal_rollback=True,
            after_swap=settle,
        )
        return Rollback(outcome)

    def _clear_stamp(self, task_run_id: uuid.UUID) -> None:
        """Clear a task run's *written* stamp, uncommitted: its write has been dealt with.

        The tip of the task run's own chain of writes (`own_snapshot_id`) goes with it: the
        table is back at the baseline, so the task run has no write of its own in it any
        more. If the task is tried again (the worker was lost before it recorded the failure
        that this set-back belongs to), its next write is staged on the baseline and starts a
        new chain; with the old tip left in place that write would not be recognised, and its
        failed check would leave the data and blame another writer.

        Parameters
        ----------
        task_run_id
            The task run.
        """
        self._session.execute(
            update(TaskRun)
            .where(TaskRun.id == task_run_id)
            .values(
                written_table_id=None,
                written_snapshot_before=None,
                written_snapshot_after=None,
                written_publishes=None,
                own_snapshot_id=None,
            )
            .execution_options(synchronize_session=False)
        )

    def _record_rollback(
        self,
        p: PrincipalRef,
        securable: Securable,
        task_run_id: uuid.UUID,
        outcome: RollbackOutcome,
        detail: dict[str, object],
    ) -> None:
        """Add a rollback's audit event and the outcome on the task's failed results, uncommitted.

        Parameters
        ----------
        p
            The acting principal.
        securable
            The checked table's securable.
        task_run_id
            The task run whose blocking results get the outcome.
        outcome
            What the rollback did.
        detail
            The audit detail, without the outcome.
        """
        AuditService(self._session).record(
            p.id,
            AuditEventType.TABLE_ROLLED_BACK,
            securable=securable,
            detail={**detail, "outcome": outcome.value},
        )
        self._session.execute(
            update(CheckResult)
            .where(
                CheckResult.task_run_id == task_run_id,
                CheckResult.on_failure == "block",
                CheckResult.outcome.in_([CheckOutcome.FAILED, CheckOutcome.ERRORED]),
            )
            .values(rollback_outcome=outcome)
            .execution_options(synchronize_session=False)
        )

    def check_can_append(
        self,
        p: PrincipalRef,
        ref: TableRef,
        *,
        expected_snapshot_id: ExpectedSnapshot | None = None,
    ) -> None:
        """Cheaply verify an append would be authorized, before any data is read.

        A server-side caller reading a request body incrementally (the
        governed Arrow-append route) wants to refuse an unauthorized or
        nonexistent-table request before spending any time or memory on the
        body at all, rather than only after decoding it. This method is
        exactly :meth:`append`'s own opening authorization and table-kind
        check, factored out so a caller can run it first; :meth:`append`
        calls it too and then re-authorizes and re-resolves the table
        itself, so this early check is an optimization, never a substitute
        for the authoritative one immediately before the actual write —
        the table's grants or type could still change between the two
        calls.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table an append is being considered for.
        expected_snapshot_id
            The caller's precondition, if any. ``none`` is refused here when the table already
            has a snapshot, so the caller's body is never read.

        Raises
        ------
        NotFoundError
            If the table does not exist.
        ValidationError
            If the table is not `ICEBERG` (managed); an artifact gets its own
            message.
        PermissionDenied
            If the principal may not write to this table.
        ConflictError
            If `expected_snapshot_id` is ``none`` and the table already has a snapshot.
        """
        securable = Securable.table_scope(ref.environment, ref.namespace, ref.name)
        self._authz.authorize(p, Action.WRITE_TABLE, securable)
        _, row = self._find_table(ref)
        if row.table_type is TableType.ARTIFACT:
            raise ValidationError(artifact_refusal(ref.dotted(), "appending rows"))
        if row.table_type is not TableType.ICEBERG:
            raise ValidationError("Appends are only supported on managed (Iceberg) tables.")
        self._refuse_create_pin_on_snapshot(ref, row, expected_snapshot_id)

    def _refuse_create_pin_on_snapshot(
        self, ref: TableRef, row: Table, expected_snapshot_id: ExpectedSnapshot | None
    ) -> None:
        """Refuse ``expected_snapshot_id=none`` on a table that already has a snapshot.

        Parameters
        ----------
        ref
            The table.
        row
            Its row.
        expected_snapshot_id
            The caller's precondition, if any.

        Raises
        ------
        ConflictError
            If the pin is ``none`` and the table's metadata shows a current snapshot.
        """
        if (
            expected_snapshot_id == "none"
            and self._to_loaded_table(ref, row).metadata.current_snapshot_id is not None
        ):
            raise ConflictError(
                f"{ref.dotted()} already has data, so it cannot be created again. A run or "
                "load that creates a table: retry, it will update the table. A call that sets "
                "expected_snapshot_id: pass the table's current snapshot id or leave it out."
            )

    def _existing_table(self, ref: TableRef) -> Table | None:
        """Look a table up without raising.

        Parameters
        ----------
        ref
            The table.

        Returns
        -------
        Table or None
            The row, or `None` if the table, its namespace or its
            environment does not exist.
        """
        try:
            _, row = self._find_table(ref)
        except NotFoundError:
            return None
        return row

    def check_can_replace(
        self,
        p: PrincipalRef,
        ref: TableRef,
        *,
        expected_snapshot_id: ExpectedSnapshot | None = None,
    ) -> None:
        """Cheaply verify a replace would be authorized, before any data is read.

        `check_can_append`'s counterpart for `replace`, which may create its
        target (`expected_snapshot_id="none"` on a missing table). `WRITE_TABLE`
        is checked first in every case, so a caller without it gets the same
        denial whether or not the table exists.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table.
        expected_snapshot_id
            The caller's precondition, if any.

        Raises
        ------
        PermissionDenied
            Without `WRITE_TABLE`, or without `CREATE_TABLE` when the upload
            would create the table.
        NotFoundError
            If the table does not exist and no precondition was given.
        ConflictError
            If the table does not exist and a snapshot id was expected.
        ValidationError
            If the table is not a managed table.
        """
        self._check_can_write_or_create(
            p,
            ref,
            expected_snapshot_id,
            artifact_operation="replacing rows",
            unmanaged="Replaces are only supported on managed (Iceberg) tables.",
        )

    def check_can_merge(
        self,
        p: PrincipalRef,
        ref: TableRef,
        *,
        expected_snapshot_id: ExpectedSnapshot | None = None,
    ) -> None:
        """Cheaply verify a merge would be authorized, before any data is read.

        The rules of `check_can_replace` (`WRITE_TABLE` first, `CREATE_TABLE` as well when the
        merge would create a missing table with `expected_snapshot_id="none"`), and `READ_TABLE`
        right after `WRITE_TABLE`: a merge compares the batch with the table's rows, and its
        counts and refusals tell what it found. For the same reason a merge is refused while a
        row filter or column mask on the table applies to `p`, as `read_watermark` is.

        One exception in how the `READ_TABLE` step is checked: a task attempt's bound token
        merging into its own asset. Such a token may read only the task's declared inputs,
        never its own asset, but writing that asset is the task's job. So for it, `READ_TABLE`
        is checked against the principal's own grants without the token's narrowing (the
        principal must still hold it, as its personal token would need to), after
        `WRITE_TABLE` has held the token to its environment and its own asset; the write
        guard's own-asset rule (`WriteOperation.MERGE`) holds it there too. The row filter and
        column mask refusal runs against the same principal, so a task of a filtered or masked
        principal cannot merge either.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table.
        expected_snapshot_id
            The caller's precondition, if any.

        Raises
        ------
        PermissionDenied
            Without `WRITE_TABLE` or `READ_TABLE`, or without `CREATE_TABLE` when the merge
            would create the table.
        NotFoundError
            If the table does not exist and no precondition was given.
        ConflictError
            If the table does not exist and a snapshot id was expected.
        ValidationError
            If the table is not a managed table, or a row filter or column mask on it applies
            to `p`.
        """
        self._check_can_compare(
            p,
            ref,
            expected_snapshot_id,
            artifact_operation="merging rows",
            unmanaged="Merges are only supported on managed (Iceberg) tables.",
            hidden=(
                f"Merges into {ref.dotted()} are refused while a row filter or column mask "
                "applies to you on it: a merge compares your rows with rows you can't see."
            ),
        )

    def check_can_history(
        self,
        p: PrincipalRef,
        ref: TableRef,
        *,
        expected_snapshot_id: ExpectedSnapshot | None = None,
    ) -> None:
        """Cheaply verify a history write would be authorized, before any data is read.

        The rules of `check_can_merge`, for the same reasons: `WRITE_TABLE`, then `READ_TABLE`
        (a bound task token on its own asset checked without its narrowing), `CREATE_TABLE` as
        well when the write would create the table, and refused while a row filter or column mask
        on the table applies to `p`: a history write reads every open version, and its counts
        tell what it found.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table.
        expected_snapshot_id
            The caller's precondition, if any.

        Raises
        ------
        PermissionDenied
            Without `WRITE_TABLE` or `READ_TABLE`, or without `CREATE_TABLE` for a create.
        NotFoundError
            For a missing table without a precondition.
        ConflictError
            For a missing table where a snapshot id was expected.
        ValidationError
            For a table that is not managed, or a row filter or column mask on the table that
            applies to `p`.
        """
        self._check_can_compare(
            p,
            ref,
            expected_snapshot_id,
            artifact_operation="writing history",
            unmanaged="History writes are only supported on managed (Iceberg) tables.",
            hidden=(
                f"History writes into {ref.dotted()} are refused while a row filter or column "
                "mask applies to you on it: a history write compares your rows with rows you "
                "can't see."
            ),
        )

    def _check_can_compare(
        self,
        p: PrincipalRef,
        ref: TableRef,
        expected_snapshot_id: ExpectedSnapshot | None,
        *,
        artifact_operation: str,
        unmanaged: str,
        hidden: str,
    ) -> None:
        """Run the pre-check of a write that compares its rows with the table's.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table.
        expected_snapshot_id
            The caller's precondition, if any.
        artifact_operation
            What was attempted, for `artifact_refusal`.
        unmanaged
            The refusal for a table that is neither managed nor an artifact.
        hidden
            The first sentence of the refusal while a row filter or column mask applies to `p`.

        Raises
        ------
        ValidationError
            While a row filter or column mask on the table applies to `p`; the rest as
            `_check_can_write_or_create` raises it.
        """
        row = self._check_can_write_or_create(
            p,
            ref,
            expected_snapshot_id,
            artifact_operation=artifact_operation,
            unmanaged=unmanaged,
            read_as=replace(p, task_scope=None) if _bound_to_own_asset(p, ref) else p,
        )
        if row is None:
            return
        policy = PolicyResolver(self._session).resolve(p, ref, table_id=row.id)
        if policy.row_filters or policy.column_masks:
            raise ValidationError(
                f"{hidden} Ask someone the table is not filtered or masked for to run it, or ask "
                "an owner to lift the filter or mask for you."
            )

    def _check_can_write_or_create(
        self,
        p: PrincipalRef,
        ref: TableRef,
        expected_snapshot_id: ExpectedSnapshot | None,
        *,
        artifact_operation: str,
        unmanaged: str,
        read_as: PrincipalRef | None = None,
    ) -> Table | None:
        """Run the pre-check of a write that may create its target.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table.
        expected_snapshot_id
            The caller's precondition, if any.
        artifact_operation
            What was attempted, for `artifact_refusal`.
        unmanaged
            The refusal for a table that is neither managed nor an artifact.
        read_as
            Authorize `READ_TABLE` too, right after `WRITE_TABLE`, for this principal (`p`, or
            `p` without its task token's narrowing).

        Returns
        -------
        Table or None
            The table's row, or `None` when the write would create the table.

        Raises
        ------
        PermissionDenied
            As `check_can_replace`.
        NotFoundError
            As `check_can_replace`.
        ConflictError
            As `check_can_replace`.
        ValidationError
            As `check_can_replace`.
        """
        securable = Securable.table_scope(ref.environment, ref.namespace, ref.name)
        self._authz.authorize(p, Action.WRITE_TABLE, securable)
        if read_as is not None:
            self._authz.authorize(read_as, Action.READ_TABLE, securable)
        row = self._existing_table(ref)
        if row is None:
            if expected_snapshot_id is None:
                self._find_table(ref)
            if expected_snapshot_id != "none":
                raise ConflictError(target_changed_message(ref.dotted()))
            self._authz.authorize(p, Action.CREATE_TABLE, securable)
            return None
        if row.table_type is TableType.ARTIFACT:
            raise ValidationError(artifact_refusal(ref.dotted(), artifact_operation))
        if row.table_type is not TableType.ICEBERG:
            raise ValidationError(unmanaged)
        self._refuse_create_pin_on_snapshot(ref, row, expected_snapshot_id)
        return row

    def check_can_load(
        self,
        p: PrincipalRef,
        ref: TableRef,
        *,
        write: str,
        expected_snapshot_id: ExpectedSnapshot | None,
        schema: pa.Schema | None = None,
    ) -> bool:
        """Verify a load (`lakana load`) may write its target, before any data is read.

        In this order: the target is in the bronze layer (`require_bronze`); the caller may
        write it (`check_can_append`, or `check_can_replace`, which also asks `CREATE_TABLE`
        when `expected_snapshot_id` is ``none`` and the table is missing); the environment's
        write guard allows it (a protected environment needs break glass with
        `MANAGE_ENVIRONMENT`; recorded by the write, not here); the namespace of a table the
        load would create exists, and a missing table has the pin ``none``. With `schema`, a
        table the load would create gets no column name twice, and an existing table is checked
        against it as the write would check the rows: column for column for an append, by the
        schema rules of a replace without evolving it.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table.
        write
            ``replace`` or ``append``.
        expected_snapshot_id
            The caller's precondition; ``none`` on a first load.
        schema
            The schema of the rows the load would write, or `None` to check only the target.

        Returns
        -------
        bool
            `True` when the load would create the table.

        Raises
        ------
        ValidationError
            For a target outside bronze, a table that is not managed, a repeated column name
            for a table the load would create, or a schema the write would refuse (the message
            shows the difference).
        PermissionDenied
            As `check_can_append` and `check_can_replace`, and as the write guard refuses a
            write to a protected environment.
        NotFoundError
            For a missing namespace (naming the command that creates it), or a missing table
            without ``none`` (naming the pin that creates it).
        ConflictError
            When the table's current snapshot is not `expected_snapshot_id` (``none`` on a
            table that exists included), as the write would find.
        """
        require_bronze(ref)
        missing_without_pin = False
        try:
            if write == "append" and expected_snapshot_id != "none":
                self.check_can_append(p, ref)
            else:
                self.check_can_replace(p, ref, expected_snapshot_id=expected_snapshot_id)
        except NotFoundError:
            # Only a missing table without a pin gets here, after `WRITE_TABLE` was granted:
            # name the namespace if that is missing too, else the pin that creates the table.
            if expected_snapshot_id is not None or self._existing_table(ref) is not None:
                raise
            missing_without_pin = True
        row = self._existing_table(ref)
        appends = row is not None and write == "append" and expected_snapshot_id != "none"
        self._guard.check(
            p,
            ref.environment,
            WriteOperation.APPEND if appends else WriteOperation.WRITE,
            table_target(ref),
            record_break_glass=False,
        )
        if row is None:
            namespace = ".".join(ref.namespace.parts)
            try:
                self._find_namespace(self._find_environment(ref.environment), ref.namespace)
            except NotFoundError:
                raise NotFoundError(
                    f"Namespace `{shown(namespace)}` does not exist in "
                    f"{shown(ref.environment)!r}: create it with `lakana ns create "
                    f"{shown(namespace)} --env {shown(ref.environment)}`, then load again."
                ) from None
            if missing_without_pin:
                raise NotFoundError(
                    f"Table `{shown(ref.dotted())}` does not exist in {shown(ref.environment)!r}: "
                    "a first load passes expected_snapshot_id=none, which creates it."
                )
            if schema is not None:
                seen: set[str] = set()
                for name in schema.names:
                    if name in seen:
                        raise ValidationError(
                            f"Column {shown(name)!r} appears more than once in the rows sent; "
                            "rename one and load again."
                        )
                    seen.add(name)
            return True
        current = self._to_loaded_table(ref, row).metadata
        if (
            expected_snapshot_id is not None
            and expected_snapshot_value(expected_snapshot_id) != current.current_snapshot_id
        ):
            raise ConflictError(target_changed_message(ref.dotted()))
        if schema is not None:
            if write == "append":
                validate_arrow_schema(current.schema(), schema.empty_table())
            else:
                plan_schema(
                    current.schema(),
                    arrow_to_iceberg_schema(schema),
                    evolve=False,
                    last_column_id=current.last_column_id,
                    next_schema_id=max(s.schema_id for s in current.schemas) + 1,
                    table_name=ref.dotted(),
                )
        return False

    def append(
        self,
        p: PrincipalRef,
        ref: TableRef,
        data: pa.Table,
        *,
        sources: Sequence[str] = (),
        source_versions: Mapping[str, int] | None = None,
        expected_snapshot_id: ExpectedSnapshot | None = None,
    ) -> AppendResult:
        """Append rows to a managed table.

        Authorization and the managed-table-type check are :meth:`check_can_append`'s
        (called first, below) — this method always re-runs both itself
        rather than trusting a caller's earlier call to still hold, since
        the table's grants or type could have changed since.

        `data`'s field and schema metadata is stripped first
        (`strip_field_metadata`): pyiceberg's
        writer honours a `PARQUET:field_id` metadata entry when present,
        writing that column by its declared id rather than by name, even
        though the schema check right after validates identity purely by
        name — caller-supplied metadata with a stale or mismatched
        `field_id` was reproduced committing swapped columns (`a=99, b=1`)
        despite passing that by-name check. With metadata stripped,
        pyiceberg has nothing left to key off but the same column
        order/name `validate_arrow_schema` already checks. `data`'s Arrow
        schema is then checked, column by column, against the table's
        Iceberg schema before anything is written or committed — an extra
        column, a missing column, a type mismatch, a nullability mismatch,
        or a null in a column the table requires to be non-null, is
        refused by name, before any I/O. The write itself is staged with
        pyiceberg against a `StaticTable` built from this environment's
        resolved `FileIO` (so a remote backend works, unlike
        `StaticTable.from_metadata`'s own scheme-guessing default), then
        published through :meth:`commit_table`, so an append is subject to
        the same the snapshot guards (location immutability, manifest-list
        confinement) as every other write.

        The `TABLE_APPENDED` audit event is added to the session, not
        committed, before :meth:`commit_table` is called: `commit_table`
        performs the actual compare-and-swap and commits the session
        itself, so the pending audit row rides along in the same
        transaction as the pointer move — both persist together, or (on a
        lost race, or any other failure from recording the event or from
        `commit_table`) neither does, since the session is rolled back
        before the error is re-raised.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table to append to.
        data
            The rows to append.
        sources
            Dotted names of the tables `data` was derived from.
            Each must exist and be READ-able by `p`, checked before anything
            is written; one lineage edge per source is recorded with the
            commit.
        source_versions
            The version of each source that is an artifact, by dotted name,
            when the caller read a specific one; see
            :meth:`resolve_lineage_sources`.
        expected_snapshot_id
            The table's current snapshot as the caller read it, or ``none``
            for "no snapshot yet"; the append commits only while that still
            holds, even for zero rows. A missing table is `NotFoundError`
            whatever is expected.

        Returns
        -------
        AppendResult
            What was written. `snapshot_id` is `None`, and nothing is
            committed or audited, when `data` has zero rows.

        Raises
        ------
        NotFoundError
            If the table does not exist, or a declared source does not exist.
        ValidationError
            If the table is not `ICEBERG` (managed), or `data`'s schema does
            not match the table's.
        ConflictError
            If another writer committed first, or `expected_snapshot_id` no
            longer matches.
        PermissionDenied
            If the principal may not write to this table, or may not read a
            declared source.

        Notes
        -----
        The data files, manifests, and manifest list this append's
        transaction wrote are listed once, immediately after staging —
        never inside a failure handler. Listing them requires reading those
        same files back from storage, and a failure doing so (the same
        outage that may go on to fail the commit) must not itself replace
        the caller's real error the way it would if it ran inside an
        `except` block; at this point nothing has been audited or attempted
        yet, so letting that failure propagate directly is the correct
        "abort cleanly" outcome, and it is fine for it to surface unchanged
        .

        If recording the audit event or `commit_table` then raises for any
        reason — a lost race (`ConflictError`), a denial on its own
        re-authorization (`PermissionDenied`), a snapshot-guard refusal
        (`ValidationError`), or a metadata I/O error — those already-listed
        files are deleted best-effort (never a prior snapshot's, since they
        were identified from this transaction's own new snapshot only), and
        only then is the session rolled back — cleanup never touches the
        session, so a lost DB connection must not be allowed to skip it.
        `commit_table`'s own `_find_table` autoflushes the pending audit
        insert before its CAS can fail, so without this rollback the row
        would survive in the open transaction and land falsely on whatever
        this session next commits. Rolling back can itself fail (the same
        lost connection); that failure is logged, by exception class alone,
        and never allowed to replace the original error. The original error
        is always what gets re-raised.
        """
        self._commit_sent = False
        self.check_can_append(p, ref)
        self._guard.check(p, ref.environment, WriteOperation.APPEND, table_target(ref))
        lineage = self.resolve_lineage_sources(
            p, ref.environment, sources, versions=source_versions
        )
        securable = Securable.table_scope(ref.environment, ref.namespace, ref.name)
        ns, row = self._find_table(ref)
        environment_id, table_id = ns.environment_id, row.id
        loaded = self._to_loaded_table(ref, row)
        pins: list[TableRequirement] = []
        if expected_snapshot_id is not None:
            wanted = expected_snapshot_value(expected_snapshot_id)
            if loaded.metadata.current_snapshot_id != wanted:
                raise ConflictError(target_changed_message(ref.dotted()))
            pins.append(AssertRefSnapshotId(ref="main", snapshot_id=wanted))
        data = strip_field_metadata(data)
        validate_arrow_schema(loaded.metadata.schema(), data)
        if data.num_rows == 0:
            return AppendResult(rows=0, files=0, snapshot_id=None)

        resolved = self._storage.for_environment(ref.environment)
        requirements, updates = build_transaction(
            loaded.metadata, loaded.metadata_location, resolved.file_io, data
        )
        add_snapshot = next(u for u in updates if isinstance(u, AddSnapshotUpdate))
        new_snapshot = add_snapshot.snapshot
        # Listed once, eagerly, before anything below is attempted -- deliberately outside
        # the try/except that follows. See the "Notes" section above: a failure reading these
        # back must abort directly rather than being caught and mistaken for the commit's own
        # failure. The staged transaction has already written real data and manifest files to
        # storage by this point -- they do exist, and would be orphaned
        # (never referenced by any committed snapshot, so never cleaned up) if this listing
        # step itself fails; there is simply nothing yet in `cleanup_paths` to delete them with.
        written_paths = new_data_file_paths(new_snapshot, resolved.file_io)
        cleanup_paths = [*written_paths, *new_manifest_paths(new_snapshot, resolved.file_io)]
        rows, files, snapshot_id = data.num_rows, len(written_paths), new_snapshot.snapshot_id

        try:
            AuditService(self._session).record(
                p.id,
                AuditEventType.TABLE_APPENDED,
                securable=securable,
                detail={"rows": rows, "files": files, "snapshot_id": snapshot_id},
            )
            self._add_lineage_edges(
                environment_id=environment_id,
                sources=lineage,
                target_id=table_id,
                target_name=ref.dotted(),
                target_snapshot_id=snapshot_id,
                query_run_id=None,
                connections=self._connection_sources(p, environment_id),
            )
            self.commit_table(
                p,
                ref,
                requirements=[*pins, *requirements],
                updates=updates,
                own_data_files=True,
            )
        except Exception as exc:
            landed = self._settle_failed_write(
                exc,
                ref,
                table_id=table_id,
                snapshot_id=snapshot_id,
                new_table=False,
                commit_sent=self._commit_sent,
                clean_up=lambda: cleanup_written_paths(resolved, cleanup_paths),
            )
            if not landed:
                if isinstance(exc, ConflictError):
                    raise ConflictError(target_changed_message(ref.dotted())) from exc
                raise
        return AppendResult(rows=rows, files=files, snapshot_id=snapshot_id)

    def replace(
        self,
        p: PrincipalRef,
        ref: TableRef,
        data: pa.Table,
        *,
        evolve_schema: bool = False,
        sources: Sequence[str] = (),
        source_versions: Mapping[str, int] | None = None,
        expected_snapshot_id: ExpectedSnapshot | None = None,
    ) -> WriteResult:
        """Replace every row of a managed table with Arrow data (`tables.replace`).

        The same governed path as a SQL `replace` (`write_batches`): one
        `OVERWRITE` snapshot, the same schema rules, the same audit event.
        `data`'s field metadata is stripped first, as `append` does, so a
        stale `PARQUET:field_id` can never redirect a column.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table.
        data
            The new rows.
        evolve_schema
            Let the table adopt `data`'s schema.
        sources
            Dotted names of the tables `data` came from; each must exist and
            be READ-able, checked before anything is written.
        source_versions
            The version of each source that is an artifact, by dotted name,
            when the caller read a specific one; see
            :meth:`resolve_lineage_sources`.
        expected_snapshot_id
            The target's current snapshot as the caller read it, or ``none``;
            the write commits only while that still holds, and ``none`` on a
            missing table creates it from `data`.

        Returns
        -------
        WriteResult
            What was written.

        Raises
        ------
        PermissionDenied
            If `p` may not write the table or read a source, or lacks
            `CREATE_TABLE` where the upload would create the table.
        NotFoundError
            If the table or a source does not exist.
        SchemaMismatchError
            If `data` is incompatible and `evolve_schema` is off.
        ValidationError
            If the table is not a managed table.
        ConflictError
            If another writer committed first, or `expected_snapshot_id` no
            longer matches.
        """
        self.check_can_replace(p, ref, expected_snapshot_id=expected_snapshot_id)
        lineage = self.resolve_lineage_sources(
            p, ref.environment, sources, versions=source_versions
        )
        data = strip_field_metadata(data)
        row = self._existing_table(ref)
        if row is None and expected_snapshot_id == "none":
            try:
                return self.write_batches(
                    p,
                    ref,
                    mode=WriteMode.CREATE,
                    schema=data.schema,
                    batches=data.to_batches(),
                    sources=lineage,
                )
            except AlreadyExistsError as exc:
                raise ConflictError(target_changed_message(ref.dotted())) from exc
        pin = None
        if expected_snapshot_id is not None:
            if row is None:
                raise ConflictError(target_changed_message(ref.dotted()))
            pin = SnapshotPin(row.id, expected_snapshot_value(expected_snapshot_id))
        return self.write_batches(
            p,
            ref,
            mode=WriteMode.REPLACE,
            schema=data.schema,
            batches=data.to_batches(),
            evolve_schema=evolve_schema,
            sources=lineage,
            pin=pin,
        )

    @_arrow_memory_returned()
    def merge(
        self,
        p: PrincipalRef,
        ref: TableRef,
        data: pa.Table,
        *,
        key: Sequence[str],
        sources: Sequence[str] = (),
        source_versions: Mapping[str, int] | None = None,
        expected_snapshot_id: ExpectedSnapshot | None = None,
        pin: SnapshotPin | None = None,
        query_run_id: uuid.UUID | None = None,
        watermark: str | None = None,
        advice: MergeAdvice = SDK_ADVICE,
    ) -> MergeResult:
        """Update the rows whose key matches, insert the rest, in one commit (`tables.merge`).

        The batch is checked first (`check_merge_batch`: the key columns exist, no key is null
        or repeats; counts, never values), then against the table's schema as an append is.
        `stage_upsert` reads the rows of the table that the batch's keys match, refuses a key
        that matches more than one of them, and stages one snapshot; it is committed through
        `commit_table` with the `TABLE_WRITE` event (`mode: merge`, both counts, the key) and
        one lineage edge per source in the same transaction. A reader sees the table before or
        after, never a half. Nothing is deleted. A failure deletes every file the merge wrote,
        as for `append`.

        A missing table with `expected_snapshot_id="none"` is created from the batch (the same
        batch checks first) by the create path of `replace`, whose audit event is the create's.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table.
        data
            The rows.
        key
            The columns that identify a row.
        sources
            Dotted names of the tables `data` came from; each must exist and be READ-able,
            checked before anything is written.
        source_versions
            The version of each source that is an artifact, by dotted name, when the caller
            read a specific one; see :meth:`resolve_lineage_sources`.
        expected_snapshot_id
            The table's current snapshot as the caller read it, or ``none``; the merge commits
            only while that still holds, and ``none`` on a missing table creates it.
        pin
            The table and snapshot a worker's merge run computed its rows from (a SQL asset's
            watermark, or a SELECT of its own target), as for `write_batches`: the merge
            commits only while the table is still that table at that snapshot. `None` for a
            merge whose rows do not depend on the table; ignored where the merge creates it.
        query_run_id
            The run merging, for the audit event and the lineage edges.
        watermark
            The encoded watermark the run filtered on, for the audit event.
        advice
            How a refusal of the rows tells the caller to fix them: `SDK_ADVICE` for a caller
            that sends batches, `SQL_ADVICE` for a SQL asset's run.

        Returns
        -------
        MergeResult
            The new snapshot and the counts. `snapshot_id` is `None`, and nothing is committed
            or audited, when `data` has no rows or changes nothing.

        Raises
        ------
        PermissionDenied
            If `p` may not write or read the table or read a source, or lacks `CREATE_TABLE`
            where the merge would create the table.
        NotFoundError
            If the table or a source does not exist.
        ValidationError
            For a key the batch or the table cannot merge on, a null or repeated key in the
            batch, a key of several columns with more than `COMPOSITE_KEY_MAX_ROWS` rows, a key
            of the batch matching two rows of the table, data that does not fit the table, a
            table that is not managed, or a row filter or column mask on it that applies to
            `p` (see `check_can_merge`).
        ConflictError
            If another writer committed first, or `expected_snapshot_id` or `pin` no longer
            matches.
        """
        self._commit_sent = False
        self.check_can_merge(p, ref, expected_snapshot_id=expected_snapshot_id)
        self._guard.check(p, ref.environment, WriteOperation.MERGE, table_target(ref))
        lineage = self.resolve_lineage_sources(
            p, ref.environment, sources, versions=source_versions
        )
        data = strip_field_metadata(data)
        key = list(key)
        if self._existing_table(ref) is None and expected_snapshot_id == "none":
            check_merge_batch(
                data,
                key,
                table_columns=data.column_names,
                table_name=ref.dotted(),
                advice=advice,
            )
            try:
                created = self.write_batches(
                    p,
                    ref,
                    mode=WriteMode.CREATE,
                    schema=data.schema,
                    batches=data.to_batches(),
                    sources=lineage,
                    query_run_id=query_run_id,
                )
            except AlreadyExistsError as exc:
                raise ConflictError(target_changed_message(ref.dotted())) from exc
            return MergeResult(
                snapshot_id=created.snapshot_id,
                rows_inserted=created.rows,
                rows_updated=0,
                created=True,
                table_id=created.table_id,
            )

        ns, row = self._find_table(ref)
        loaded = self._to_loaded_table(ref, row)
        current = loaded.metadata
        pins = self._write_pins(ref, row.id, current, expected_snapshot_id, pin)
        schema = current.schema()
        check_merge_batch(
            data,
            key,
            table_columns=[column.name for column in schema.fields],
            table_name=ref.dotted(),
            advice=advice,
        )
        validate_arrow_schema(schema, data)
        nothing = MergeResult(snapshot_id=None, rows_inserted=0, rows_updated=0, table_id=row.id)
        if data.num_rows == 0:
            return nothing
        resolved = self._storage.for_environment(ref.environment)
        staged = stage_upsert(
            current, loaded.metadata_location, resolved.file_io, data, key, table_name=ref.dotted()
        )
        counts = staged.counts
        snapshot_id = self._commit_one_snapshot(
            p,
            ref,
            mode=WriteMode.MERGE,
            table_id=row.id,
            environment_id=ns.environment_id,
            current=current,
            unpublished=row.unpublished_run_id is not None,
            resolved=resolved,
            requirements=[*pins, *staged.requirements],
            updates=staged.updates,
            discarded=staged.discarded,
            rows=counts.rows_inserted + counts.rows_updated,
            lineage=lineage,
            query_run_id=query_run_id,
            watermark=watermark,
            detail={
                "rows_inserted": counts.rows_inserted,
                "rows_updated": counts.rows_updated,
                "key": key,
            },
        )
        if snapshot_id is None:
            return nothing
        return MergeResult(
            snapshot_id=snapshot_id,
            rows_inserted=counts.rows_inserted,
            rows_updated=counts.rows_updated,
            table_id=row.id,
        )

    @_arrow_memory_returned()
    def history(
        self,
        p: PrincipalRef,
        ref: TableRef,
        data: pa.Table,
        *,
        key: Sequence[str],
        valid_from: str | None = None,
        close_missing: bool = False,
        sources: Sequence[str] = (),
        source_versions: Mapping[str, int] | None = None,
        expected_snapshot_id: ExpectedSnapshot | None = None,
        pin: SnapshotPin | None = None,
        query_run_id: uuid.UUID | None = None,
        max_rows: int | None = None,
        max_open_versions: int = DEFAULT_MAX_HISTORY_OPEN_VERSIONS,
        advice: MergeAdvice = SDK_ADVICE,
    ) -> HistoryResult:
        """Keep the versions of a dimension's rows: open, close and leave alone, in one commit.

        The batch is the current state of every row. It is checked first
        (`check_history_batch`: the added names, the `valid_from` column, the key; counts, never
        values). A missing table with `expected_snapshot_id="none"` is created with every row
        as an open version and the property `lakana.history-key` (the audit event is the
        create's). Otherwise the table must be a history table of this key, the batch must have
        its columns, and `plan_history` diffs it against the open versions; the closed and
        opened rows are staged as one snapshot (`stage_history`) and committed through
        `commit_table` with the `TABLE_WRITE` event (`mode: history`) and one lineage edge per
        source in the same transaction. Nothing is deleted. The run clock is the start of the
        run `p`'s run context belongs to (`run_clock`); no caller sends one.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table.
        data
            The rows: the current state of every row, with the `valid_from` column if named.
        key
            The natural key's columns.
        valid_from
            The column each version's start is read from; `None` for the run clock.
        close_missing
            Close the open version of each key `data` lacks, at the run clock.
        sources
            Dotted names of the tables `data` came from; each must exist and be READ-able.
        source_versions
            The version of each source that is an artifact, by dotted name.
        expected_snapshot_id
            The table's current snapshot as the caller read it, or ``none``, which also creates
            a missing table.
        pin
            The table and snapshot a worker's run computed its rows from, or `None`.
        query_run_id
            The run writing, for the audit event and the lineage edges.
        max_rows
            Most rows of the batch and most open versions (`QueryLimitsConfig.max_write_rows`),
            or `None`.
        max_open_versions
            Most open versions the table may have (`RunConfig.max_history_open_versions`):
            the open versions counted from the manifests and the result's rows before any
            version is read, and the open versions the planned write would leave before it is
            staged.
        advice
            How a repeated key is explained: `SDK_ADVICE` or `SQL_ADVICE`.

        Returns
        -------
        HistoryResult
            The snapshot and the counts. `snapshot_id` is `None`, and nothing is committed or
            audited, when nothing changed.

        Raises
        ------
        PermissionDenied
            If `p` may not write or read the table or read a source, or lacks `CREATE_TABLE`
            where the write would create the table.
        NotFoundError
            If the table or a source does not exist.
        ValidationError
            For a batch `check_history_batch` refuses, more rows, open versions or closed
            versions of returning keys than `max_rows`, a table that is not a history table of
            this key, a batch without the table's columns, a refusal of `plan_history`, a change
            by a run whose own write is the table's current snapshot (`written_by_run`), a table
            that is not managed, a row filter or column mask that applies to `p`, more open
            versions or result rows than `max_open_versions` (before any version is read), more
            open versions after the planned write (before anything is staged), or first versions
            to replace or close after a run that started later wrote the table
            (`check_first_versions_order`).
        UnsupportedTableError
            For a table with row-level delete files (`count_open_versions`).
        ConflictError
            If another writer committed first, or `expected_snapshot_id` or `pin` no longer
            matches.
        """
        self._commit_sent = False
        self.check_can_history(p, ref, expected_snapshot_id=expected_snapshot_id)
        self._guard.check(p, ref.environment, WriteOperation.HISTORY, table_target(ref))
        lineage = self.resolve_lineage_sources(
            p, ref.environment, sources, versions=source_versions
        )
        data = strip_field_metadata(data)
        key = list(key)
        if max_rows is not None and data.num_rows > max_rows:
            raise ValidationError(max_rows_message(max_rows))
        check_history_batch(
            data,
            key,
            table=ref.dotted(),
            valid_from=valid_from,
            close_missing=close_missing,
            advice=advice,
        )
        clock = run_clock(self._session, p.run_context)
        starts = version_starts(data, valid_from, clock)
        rows = data.drop_columns([valid_from]) if valid_from is not None else data
        del data  # `rows` is the batch from here on; a cast copy must not sit next to it
        if self._existing_table(ref) is None and expected_snapshot_id == "none":
            if rows.num_rows > max_open_versions:
                raise ValidationError(
                    too_many_open_versions_message(
                        ref.dotted(),
                        rows.num_rows,
                        max_open_versions,
                        state="create",
                        holder=advice.holder,
                    )
                )
            check_starts(starts, clock, valid_from)
            earliest = pa.repeat(pa.scalar(EARLIEST_START, UTC_MICROS), rows.num_rows)
            first = first_versions(rows, key=key, table=ref.dotted(), starts=earliest)
            try:
                created = self._write_new_table(
                    p,
                    ref,
                    schema=first.schema,
                    batches=first.to_batches(),
                    max_rows=max_rows,
                    sources=lineage,
                    query_run_id=query_run_id,
                    properties={HISTORY_KEY_PROPERTY: json.dumps(key)},
                )
            except AlreadyExistsError as exc:
                raise ConflictError(target_changed_message(ref.dotted())) from exc
            return HistoryResult(
                snapshot_id=created.snapshot_id,
                versions_opened=created.rows,
                versions_closed=0,
                closed_missing=0,
                created=True,
                table_id=created.table_id,
            )

        ns, row = self._find_table(ref)
        loaded = self._to_loaded_table(ref, row)
        current = loaded.metadata
        schema = current.schema()
        check_history_table(
            schema,
            current.properties,
            key=key,
            table=ref.dotted(),
            environment=ref.environment,
        )
        rows = conform_batch(schema, rows, table=ref.dotted())
        pins = self._write_pins(ref, row.id, current, expected_snapshot_id, pin)
        resolved = self._storage.for_environment(ref.environment)
        file_io = resolved.file_io
        open_count = count_open_versions(
            current, file_io, table=ref.dotted(), environment=ref.environment
        )
        if open_count > max_open_versions:
            raise ValidationError(
                too_many_open_versions_message(
                    ref.dotted(),
                    open_count,
                    max_open_versions,
                    state="open",
                    holder=advice.holder,
                )
            )
        if rows.num_rows > max_open_versions:
            raise ValidationError(
                too_many_open_versions_message(
                    ref.dotted(),
                    rows.num_rows,
                    max_open_versions,
                    state="result",
                    holder=advice.holder,
                )
            )
        plan = plan_history(
            rows,
            read_open_versions(current, file_io, max_rows=max_rows),
            key=key,
            table=ref.dotted(),
            schema=schema.as_arrow(),
            starts=starts,
            clock=clock,
            valid_from=valid_from,
            close_missing=close_missing,
            last_ends=lambda wanted, after: read_last_ends(
                current, file_io, wanted, after, key=key, table=ref.dotted(), max_rows=max_rows
            ),
            environment=ref.environment,
        )
        if not plan.changed:
            return HistoryResult(None, 0, 0, 0, table_id=row.id)
        if written_by_run(current, p.run_context):
            raise ValidationError(RUN_WROTE_MESSAGE.format(table=ref.dotted()))
        remaining = open_count + plan.versions_opened - plan.versions_closed
        if remaining > max_open_versions:
            raise ValidationError(
                too_many_open_versions_message(
                    ref.dotted(),
                    remaining,
                    max_open_versions,
                    state="after",
                    holder=advice.holder,
                )
            )
        check_first_versions_order(
            plan,
            clock,
            lambda: newest_run_start(self._session, current),
            table=ref.dotted(),
        )
        staged = stage_history(
            current,
            loaded.metadata_location,
            file_io,
            plan,
            table=ref.dotted(),
            environment=ref.environment,
        )
        snapshot_id = self._commit_one_snapshot(
            p,
            ref,
            mode=WriteMode.HISTORY,
            table_id=row.id,
            environment_id=ns.environment_id,
            current=current,
            unpublished=row.unpublished_run_id is not None,
            resolved=resolved,
            requirements=[*pins, *staged.requirements],
            updates=staged.updates,
            discarded=staged.discarded,
            rows=plan.versions_opened + plan.versions_closed,
            lineage=lineage,
            query_run_id=query_run_id,
            watermark=None,
            detail={
                "versions_opened": plan.versions_opened,
                "versions_closed": plan.versions_closed,
                "closed_missing": plan.closed_missing,
                "key": key,
                "valid_from": valid_from or "run",
                "run_started_at": clock.isoformat(),
            },
        )
        return HistoryResult(
            snapshot_id=snapshot_id,
            versions_opened=plan.versions_opened,
            versions_closed=plan.versions_closed,
            closed_missing=plan.closed_missing,
            table_id=row.id,
        )

    def _write_pins(
        self,
        ref: TableRef,
        table_id: uuid.UUID,
        current: TableMetadata,
        expected_snapshot_id: ExpectedSnapshot | None,
        pin: SnapshotPin | None,
    ) -> list[TableRequirement]:
        """Build the requirements of a write that commits against the table it read.

        Parameters
        ----------
        ref
            The table, for the message.
        table_id
            Its id.
        current
            Its current metadata.
        expected_snapshot_id
            The caller's precondition, if any.
        pin
            The table and snapshot the rows were computed from, if any.

        Returns
        -------
        list of TableRequirement
            The table's identity and schema, and each precondition as a snapshot requirement.

        Raises
        ------
        ConflictError
            If `expected_snapshot_id` or `pin` no longer matches.
        """
        pins: list[TableRequirement] = [
            AssertTableUUID(uuid=table_id),
            AssertCurrentSchemaId(current_schema_id=current.current_schema_id),
        ]
        if expected_snapshot_id is not None:
            wanted = expected_snapshot_value(expected_snapshot_id)
            if current.current_snapshot_id != wanted:
                raise ConflictError(target_changed_message(ref.dotted()))
            pins.append(AssertRefSnapshotId(ref="main", snapshot_id=wanted))
        if pin is not None:
            if pin != SnapshotPin(table_id, current.current_snapshot_id):
                raise ConflictError(target_changed_message(ref.dotted()))
            pins.append(AssertRefSnapshotId(ref="main", snapshot_id=pin.snapshot_id))
        return pins

    def _commit_one_snapshot(
        self,
        p: PrincipalRef,
        ref: TableRef,
        *,
        mode: WriteMode,
        table_id: uuid.UUID,
        environment_id: uuid.UUID,
        current: TableMetadata,
        unpublished: bool,
        resolved: ResolvedStorage,
        requirements: Sequence[TableRequirement],
        updates: Sequence[TableUpdate],
        discarded: Sequence[str],
        rows: int,
        lineage: Sequence[LineageSource],
        query_run_id: uuid.UUID | None,
        watermark: str | None,
        detail: Mapping[str, object],
    ) -> int | None:
        """Commit a staged single-snapshot write (a merge or a history write) with its evidence.

        The `TABLE_WRITE` event, the lineage edges and the guard's numbers are added in the
        transaction of `commit_table`, which stamps the snapshot with the writing task attempt.
        A failure known not to have landed deletes every file the write staged and raises a lost
        race as `target_changed_message`; the files of folded snapshots are deleted either way.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table.
        mode
            `WriteMode.MERGE` or `WriteMode.HISTORY`, for the event.
        table_id
            The table's id.
        environment_id
            Its environment's id.
        current
            The metadata the write was staged on.
        unpublished
            Whether the table is not published yet.
        resolved
            The environment's storage.
        requirements
            The pins and the staging's own requirements.
        updates
            The staged updates.
        discarded
            Files of folded snapshots.
        rows
            The rows the event reports.
        lineage
            The sources, one edge each.
        query_run_id
            The run writing, if any.
        watermark
            The encoded watermark, if any.
        detail
            The event's own fields.

        Returns
        -------
        int or None
            The committed snapshot's id; `None` when `updates` adds no snapshot (nothing
            committed or audited).

        Raises
        ------
        ConflictError
            If another writer committed first, or a pin no longer holds.
        """
        added = [u.snapshot for u in updates if isinstance(u, AddSnapshotUpdate)]
        if not added:
            return None
        snapshot = added[0]
        # Listed before the try, as in `append`: a failure reading them back aborts directly.
        written_paths = new_data_file_paths(snapshot, resolved.file_io)
        cleanup_paths = [
            *written_paths,
            *new_manifest_paths(snapshot, resolved.file_io),
            *discarded,
        ]
        securable = Securable.table_scope(ref.environment, ref.namespace, ref.name)
        try:
            self._record_write(
                p,
                securable,
                ref,
                mode,
                WriteResult(
                    table_id=table_id,
                    rows=rows,
                    files=len(written_paths),
                    snapshot_id=snapshot.snapshot_id,
                    schema_diff=SchemaDiff(),
                ),
                query_run_id,
                watermark,
                lineage,
                environment_id,
                snapshot_before=current.current_snapshot_id,
                publishes=unpublished,
                detail=detail,
            )
            self.commit_table(
                p, ref, requirements=requirements, updates=updates, own_data_files=True
            )
        except Exception as exc:
            landed = self._settle_failed_write(
                exc,
                ref,
                table_id=table_id,
                snapshot_id=snapshot.snapshot_id,
                new_table=False,
                commit_sent=self._commit_sent,
                clean_up=lambda: cleanup_written_paths(resolved, cleanup_paths),
            )
            if not landed:
                if isinstance(exc, ConflictError):
                    raise ConflictError(target_changed_message(ref.dotted())) from exc
                raise
        cleanup_written_paths(resolved, discarded)
        return snapshot.snapshot_id

    def resolve_lineage_sources(
        self,
        p: PrincipalRef,
        environment: str,
        dotted_names: Sequence[str],
        *,
        versions: Mapping[str, int] | None = None,
    ) -> list[LineageSource]:
        """Resolve the tables a write read from into lineage sources.

        Every name must exist and be READ-able by `p`; a view or an
        external table resolves with no snapshot id. An artifact is accepted
        on purpose: a Python asset's table write records it as a source
        , and its `snapshot_id` is a version number: the
        one in `versions` when given, else the current version, else `None`
        when it has no live version. `create_or_replace_view` is what refuses
        an artifact as a view's reference.

        A version is looked up only after `READ_TABLE` on its artifact has
        passed, so a caller who may not read an artifact learns nothing
        about its versions. A version that was deleted since the caller read
        it (pruned by a producer, or removed by hand) is still a valid source:
        the lineage names what was read, and the number is already known to a
        caller who read it. Only a number that never had a row is refused.

        Parameters
        ----------
        p
            The acting principal.
        environment
            The environment the names are in.
        dotted_names
            Dotted table or view names, possibly repeated.
        versions
            The version of each artifact among `dotted_names` that the caller
            read, by dotted name (see `parse_source_versions`).

        Returns
        -------
        list of LineageSource
            One per distinct table, in first-seen order.

        Raises
        ------
        NotFoundError
            If a name does not exist.
        PermissionDenied
            If `p` may not read one of them.
        ValidationError
            If there are more than `MAX_SOURCE_VERSIONS` names or one is longer than any
            table's name can be (`check_sources`); if a name is not a dotted
            ``namespace.table``; if `versions` names something that is not an artifact among
            `dotted_names`; or if it names a version the artifact never had.
        """
        check_sources(dotted_names)
        refs = [TableRef.from_dotted(environment, dotted) for dotted in dotted_names]
        wanted = versions or {}
        stray = sorted(set(wanted) - {ref.dotted() for ref in refs})
        if stray:
            raise ValidationError(_not_an_artifact_source(stray[0]))
        sources: dict[uuid.UUID, LineageSource] = {}
        for ref in refs:
            self._authz.authorize(
                p,
                Action.READ_TABLE,
                Securable.table_scope(ref.environment, ref.namespace, ref.name),
            )
            _, row = self._find_table(ref)
            snapshot_id = None
            version = wanted.get(ref.dotted())
            if row.table_type is TableType.ARTIFACT:
                if version is None:
                    snapshot_id = current_version(self._session, row.id)
                else:
                    found = version_row(self._session, row.id, version)
                    if found is None:
                        raise ValidationError(f"{ref.dotted()} has no version {version}.")
                    snapshot_id = version
            elif version is not None:
                raise ValidationError(_not_an_artifact_source(ref.dotted()))
            elif row.table_type is TableType.ICEBERG and row.metadata_location is not None:
                file_io = self._storage.for_environment(environment).file_io
                snapshot_id = read_metadata(file_io, row.metadata_location).current_snapshot_id
            sources.setdefault(
                row.id, LineageSource(table_id=row.id, name=ref.dotted(), snapshot_id=snapshot_id)
            )
        return list(sources.values())

    def write_batches(
        self,
        p: PrincipalRef,
        ref: TableRef,
        *,
        mode: WriteMode,
        schema: pa.Schema,
        batches: Iterable[pa.RecordBatch],
        evolve_schema: bool = False,
        max_rows: int | None = None,
        sources: Sequence[LineageSource] = (),
        query_run_id: uuid.UUID | None = None,
        watermark: str | None = None,
        pin: SnapshotPin | None = None,
    ) -> WriteResult:
        """Write a stream of record batches into a managed table, committing once.

        The schema is planned before a single batch is
        read, so an incompatible result writes nothing. Files are written
        under the table's own location; the one snapshot — a fast append,
        or for `replace` one `OVERWRITE` deleting every live file
        — is committed through `commit_table`, so the snapshot guards
        apply. The `TABLE_WRITE` audit row and one lineage edge per source
        are added to the session first and commit with it, or not at all.
        Any failure deletes every file this write wrote and rolls back.

        The write also tells the guard what it is about to commit: the table's
        current snapshot when the write was staged (`snapshot_before`, `None` for a new table)
        and whether the write `publishes` the table (it creates it, or finds it not
        published). A worker's write run records both in the commit's transaction, and
        stamps the task run *written* when its asset has checks, which is what `roll_back`
        later works from.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The target.
        mode
            `CREATE`, `REPLACE` or `APPEND`.
        schema
            The batches' Arrow schema.
        batches
            The rows. Consumed once.
        evolve_schema
            Let the target adopt the result's schema (`replace`/`append`).
        max_rows
            Most rows to accept (`QueryLimitsConfig.max_write_rows`), or
            `None`.
        sources
            The tables the rows came from, one edge each.
        query_run_id
            The run writing, for the audit event and the edges.
        watermark
            The encoded watermark an incremental append filtered on.
        pin
            The target snapshot the rows were computed from, for a write
            whose input depends on the target (an incremental append, a
            query reading its own target); `None` for one that doesn't.
            The write commits only while the target is still that table at
            that snapshot. `create` ignores it: its target can't exist yet,
            so there is nothing a concurrent writer could make it duplicate
            or drop.

        Returns
        -------
        WriteResult
            What was written.

        Raises
        ------
        PermissionDenied
            If `p` lacks CREATE_TABLE (`create`) or WRITE_TABLE (`replace`,
            `append`) on the target.
        NotFoundError
            If the target (or, for `create`, its namespace) does not exist.
        AlreadyExistsError
            If `create` names an existing table, view or artifact.
        SchemaMismatchError
            If the result is incompatible and `evolve_schema` is off.
        ValidationError
            If `mode` is not a data write, the target is not a managed
            table, or the write is refused mid-stream (`DataFileWriter`).
        ConflictError
            If another writer committed first, or `pin` no longer matches
            the target ("<table> changed during the write; retry.").
        """
        self._commit_sent = False
        if mode is WriteMode.CREATE:
            return self._write_new_table(
                p,
                ref,
                schema=schema,
                batches=batches,
                max_rows=max_rows,
                sources=sources,
                query_run_id=query_run_id,
            )
        if mode not in (WriteMode.REPLACE, WriteMode.APPEND):
            raise ValidationError(f"{mode.value} is not a data write.")
        securable = Securable.table_scope(ref.environment, ref.namespace, ref.name)
        self._authz.authorize(p, Action.WRITE_TABLE, securable)
        self._guard.check(p, ref.environment, WriteOperation.WRITE, table_target(ref))
        ns, row = self._find_table(ref)
        if row.table_type is not TableType.ICEBERG:
            article = "an" if row.kind[0] in "AEIOU" else "a"
            next_step = (
                f" Drop the artifact (`lakana artifact drop {ref.dotted()}`) or write to "
                "another name."
                if row.table_type is TableType.ARTIFACT
                else ""
            )
            raise ValidationError(
                f"{ref.dotted()} is {article} {row.kind.lower()}; only managed tables can be "
                f"written.{next_step}"
            )
        table_id, environment_id = row.id, ns.environment_id
        unpublished = row.unpublished_run_id is not None
        loaded = self._to_loaded_table(ref, row)
        current = loaded.metadata
        # Refused before a file is written; the matching commit requirement
        # below catches a writer that lands after this check.
        if pin is not None and pin != SnapshotPin(table_id, current.current_snapshot_id):
            raise ConflictError(target_changed_message(ref.dotted()))
        pin_requirements: list[TableRequirement] = (
            [] if pin is None else [AssertRefSnapshotId(ref="main", snapshot_id=pin.snapshot_id)]
        )
        plan = plan_schema(
            current.schema(),
            arrow_to_iceberg_schema(schema),
            evolve=evolve_schema,
            last_column_id=current.last_column_id,
            next_schema_id=max(s.schema_id for s in current.schemas) + 1,
            table_name=ref.dotted(),
        )
        changed = {} if plan.schema is None else changed_columns(current.schema(), plan.schema)
        broken = broken_policies(self._session, table_id, ref.dotted(), changed)
        schema_updates: list[TableUpdate] = (
            []
            if plan.schema is None
            else [AddSchemaUpdate(schema_=plan.schema), SetCurrentSchemaUpdate(schema_id=-1)]
        )
        base = apply_updates(current, schema_updates)
        resolved = self._storage.for_environment(ref.environment)
        writer = DataFileWriter(base, resolved.file_io, max_rows=max_rows)
        manifest_paths: list[str] = []
        staged_snapshot_id: int | None = None
        try:
            data_files = writer.write(batches)
            if mode is WriteMode.APPEND and writer.rows == 0:
                result = WriteResult(
                    table_id=table_id, rows=0, files=0, snapshot_id=None, schema_diff=SchemaDiff()
                )
                self._record_write(
                    p,
                    securable,
                    ref,
                    mode,
                    result,
                    query_run_id,
                    watermark,
                    sources,
                    environment_id,
                    snapshot_before=current.current_snapshot_id,
                    publishes=unpublished,
                )
                self._guard.record_pending()
                self._session.commit()
                return result
            requirements, snapshot_updates, snapshot = stage_snapshot(
                base,
                loaded.metadata_location,
                resolved.file_io,
                data_files,
                replace=mode is WriteMode.REPLACE,
            )
            manifest_paths = new_manifest_paths(snapshot, resolved.file_io)
            staged_snapshot_id = snapshot.snapshot_id
            result = WriteResult(
                table_id=table_id,
                rows=writer.rows,
                files=len(data_files),
                snapshot_id=snapshot.snapshot_id,
                schema_diff=plan.diff,
            )
            self._record_write(
                p,
                securable,
                ref,
                mode,
                result,
                query_run_id,
                watermark,
                sources,
                environment_id,
                snapshot_before=current.current_snapshot_id,
                publishes=unpublished,
                policy_warnings=broken,
                columns_changed=bool(changed),
            )
            self.commit_table(
                p,
                ref,
                requirements=[
                    AssertTableUUID(uuid=table_id),
                    AssertCurrentSchemaId(current_schema_id=current.current_schema_id),
                    *pin_requirements,
                    *requirements,
                ],
                updates=[*schema_updates, *snapshot_updates],
                own_data_files=True,
            )
        except Exception as exc:
            landed = self._settle_failed_write(
                exc,
                ref,
                table_id=table_id,
                snapshot_id=staged_snapshot_id,
                new_table=False,
                commit_sent=self._commit_sent,
                clean_up=lambda: cleanup_written_paths(
                    resolved, [*writer.attempted_paths, *manifest_paths]
                ),
            )
            if not landed:
                if isinstance(exc, ConflictError):
                    raise ConflictError(target_changed_message(ref.dotted())) from exc
                raise
        return result

    def read_watermark(
        self, p: PrincipalRef, ref: TableRef, key: str
    ) -> tuple[Watermark | None, SnapshotPin]:
        """Read an incremental append's watermark from its target.

        Needs WRITE_TABLE (the write) and READ_TABLE (the watermark is a
        value from the target's rows), and is refused while a row filter or
        column mask on the target applies to `p`: the watermark is computed
        over every row, including rows `p` can't see.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The target.
        key
            The incremental key.

        Returns
        -------
        tuple of (Watermark or None, SnapshotPin)
            The watermark, or `None` when nothing needs filtering, and the
            snapshot it was read from — the pin the append must commit
            against, so a concurrent writer's rows are never appended twice.

        Raises
        ------
        PermissionDenied
            If `p` lacks WRITE_TABLE or READ_TABLE on the target.
        NotFoundError
            If the target does not exist.
        ValidationError
            If the target is not a managed table, a row filter or column
            mask applies to `p` on it, or `key` is not an orderable column.
        """
        securable = Securable.table_scope(ref.environment, ref.namespace, ref.name)
        self._authz.authorize(p, Action.WRITE_TABLE, securable)
        self._authz.authorize(p, Action.READ_TABLE, securable)
        _, row = self._find_table(ref)
        if row.table_type is TableType.ARTIFACT:
            raise ValidationError(artifact_refusal(ref.dotted(), "an incremental key"))
        if row.table_type is not TableType.ICEBERG:
            raise ValidationError(f"{ref.dotted()} is not a managed table.")
        policy = PolicyResolver(self._session).resolve(p, ref, table_id=row.id)
        if policy.row_filters or policy.column_masks:
            raise ValidationError(
                f"Incremental appends into {ref.dotted()} are refused while a row filter or "
                "column mask applies to you on it: the watermark would cover rows you can't see."
            )
        loaded = self._to_loaded_table(ref, row)
        check_incremental_key(loaded.metadata.schema(), key, table_name=ref.dotted())
        resolved = self._storage.for_environment(ref.environment)
        pin = SnapshotPin(table_id=row.id, snapshot_id=loaded.metadata.current_snapshot_id)
        return compute_watermark(loaded.metadata, resolved.file_io, key), pin

    def find_view(self, p: PrincipalRef, ref: TableRef) -> ViewDefinition | None:
        """Return a view's definition for expansion, or `None` if `ref` is not a view.

        A reader needs READ on the view itself here, and on every
        underlying table when those are resolved after expansion.
        A reference whose id no longer exists, or no longer carries the
        name the view was created against, makes the view stale.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The name a query referenced.

        Returns
        -------
        ViewDefinition or None
            The view; `None` for a table, or a name that doesn't exist (left
            for resolution to report).

        Raises
        ------
        PermissionDenied
            If `ref` is a view `p` may not read.
        ValidationError
            If the view is stale. The new name is named only when `p` can
            read it: a reader
            with READ on the view alone must not learn where a reference it
            can't otherwise see was renamed to, nor tell such a rename from
            a drop — both get `_stale_reference_message`, word for word.
        """
        try:
            _, row = self._find_table(ref)
        except NotFoundError:
            return None
        if row.table_type is not TableType.VIEW:
            return None
        self._authz.authorize(
            p, Action.READ_TABLE, Securable.table_scope(ref.environment, ref.namespace, ref.name)
        )
        definition = row.view_definition or {}
        references = definition.get("references", [])
        for reference in references:
            current = self._session.get(Table, uuid.UUID(reference["id"]))
            namespace = (
                None if current is None else self._session.get(Namespace, current.namespace_id)
            )
            if current is None or namespace is None:
                raise ValidationError(_stale_reference_message(ref, reference["name"]))
            current_path = NamespacePath.parse(namespace.path)
            current_name = f"{current_path}.{current.name}"
            if current_name != reference["name"]:
                visible = self._authz.permits(
                    p,
                    Action.READ_TABLE,
                    Securable.table_scope(ref.environment, current_path, current.name),
                )
                if not visible:
                    raise ValidationError(_stale_reference_message(ref, reference["name"]))
                raise ValidationError(
                    f"View {ref.dotted()!r} references {reference['name']!r}, which was renamed "
                    f"to {current_name!r}; re-create the view."
                )
        return ViewDefinition(
            name=ref.dotted(),
            sql=definition["sql"],
            references=tuple(reference["name"] for reference in references),
        )

    def create_or_replace_view(
        self,
        p: PrincipalRef,
        ref: TableRef,
        *,
        sql: str,
        references: Sequence[LineageSource],
        columns: Sequence[ColumnSpec],
        query_run_id: uuid.UUID | None = None,
    ) -> Table:
        """Create a view, or replace an existing one, without executing it.

        The worker has already checked the SQL against the AST guard,
        expanded and resolved everything it references — which authorized
        READ on all of it — and planned its columns. This
        authorizes the view itself: CREATE_TABLE for a new view, WRITE_TABLE
        to replace one. Its view → base lineage edges are replaced
        — without snapshot ids, since a view pins none — and a
        `TABLE_WRITE` event (mode ``view``) recorded, in the same transaction.

        Parameters
        ----------
        p
            The acting principal, recorded as the creator.
        ref
            The view.
        sql
            Its SELECT.
        references
            The tables and views its SQL names directly.
        columns
            Its planned output columns.
        query_run_id
            The run creating it, if any.

        Returns
        -------
        Table
            The view's row.

        Raises
        ------
        PermissionDenied
            If `p` lacks CREATE_TABLE (new) or WRITE_TABLE (existing view), or
            READ_TABLE on a referenced artifact.
        AlreadyExistsError
            If `ref` names a table or an artifact, or a concurrent create won.
        NotFoundError
            If the environment or namespace does not exist.
        UnsupportedTableError
            If a reference is an artifact: SQL cannot read one.
        ValidationError
            If the namespace is not under bronze, silver or gold, or a new
            view's name contains a ``.``.
        """
        securable = Securable.table_scope(ref.environment, ref.namespace, ref.name)
        # Which action applies depends on what is there, so the target is
        # looked up without raising; the 404s for a missing environment or
        # namespace come only after authorization (and its audited denial),
        # so an ungranted caller cannot probe for either.
        existing_id = resolve_table_id(
            self._session, ref.environment, ref.namespace.joined(), ref.name
        )
        existing = None if existing_id is None else self._session.get(Table, existing_id)
        if existing is not None and existing.table_type is TableType.VIEW:
            self._authz.authorize(p, Action.WRITE_TABLE, securable)
        else:
            self._authz.authorize(p, Action.CREATE_TABLE, securable)
        self._guard.check(p, ref.environment, WriteOperation.CREATE_VIEW, table_target(ref))
        env = self._find_environment(ref.environment)
        ns = self._find_namespace(env, ref.namespace)
        for reference in references:
            referenced = self._session.get(Table, reference.table_id)
            if referenced is not None and referenced.table_type is TableType.ARTIFACT:
                # Authorized first, so a caller who may not read the artifact is
                # not told it is one. Only for an artifact: a table reference
                # keeps the behaviour it had (its READ was checked by the worker).
                referenced_path = self._session.execute(
                    select(Namespace.path).where(Namespace.id == referenced.namespace_id)
                ).scalar_one()
                self._authz.authorize(
                    p,
                    Action.READ_TABLE,
                    Securable.table_scope(
                        ref.environment, NamespacePath.parse(referenced_path), referenced.name
                    ),
                )
                raise UnsupportedTableError(
                    f"{reference.name} is an artifact; SQL cannot read it. Only a Python asset "
                    "can read an artifact: declare it in the asset's inputs."
                )
        if existing is None:
            validate_path_component(ref.name, what="Table name")
            refuse_percent_in_new_name(ref.name, what="Table name")
        if existing is not None and existing.table_type is TableType.ARTIFACT:
            raise AlreadyExistsError(
                f"{ref.dotted()} is an artifact; a view can't replace it. Drop it first "
                f"(`lakana artifact drop {ref.dotted()}`), or choose another name."
            )
        if existing is not None and existing.table_type is not TableType.VIEW:
            raise AlreadyExistsError(f"{ref.dotted()} is a table; a view can't replace it.")
        top_level = ref.namespace.parts[0]
        if top_level not in _MEDALLION_TOP_LEVEL_NAMESPACES:
            raise ValidationError(
                f"Views may only be created under a top-level namespace of "
                f"bronze, silver, or gold; got {top_level!r}."
            )
        definition = {
            "sql": sql,
            "references": [{"id": str(s.table_id), "name": s.name} for s in references],
            "columns": [{"name": c.name, "type": c.type, "nullable": c.nullable} for c in columns],
        }
        if existing is None:
            row = Table(
                id=uuid.uuid4(),
                namespace_id=ns.id,
                name=ref.name,
                table_type=TableType.VIEW,
                metadata_location=None,
                previous_metadata_location=None,
                owner_id=p.id,
                view_definition=definition,
            )
            self._session.add(row)
            sync_created_table(self._session, env.id, row, ref.dotted())
        else:
            row = existing
            row.view_definition = definition
            self._session.execute(delete(LineageEdge).where(LineageEdge.target_table_id == row.id))
        view_id, environment_id = row.id, env.id
        sync_table_columns(self._session, view_id, columns)
        self._add_lineage_edges(
            environment_id=environment_id,
            sources=[
                LineageSource(table_id=s.table_id, name=s.name, snapshot_id=None)
                for s in references
            ],
            target_id=view_id,
            target_name=ref.dotted(),
            target_snapshot_id=None,
            query_run_id=query_run_id,
        )
        AuditService(self._session).record(
            p.id,
            AuditEventType.TABLE_WRITE,
            securable=securable,
            detail={
                "mode": WriteMode.VIEW.value,
                "target": ref.dotted(),
                "snapshot_id": None,
                "rows": 0,
                "files": 0,
                "watermark": None,
                "schema_diff": None,
                "run_id": None if query_run_id is None else str(query_run_id),
                "principal": p.name,
            },
        )
        self._guard.note_result(
            ref.environment, table_target(ref), rows=0, snapshot_id=None, table_id=view_id
        )
        try:
            self._guard.record_pending()
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise AlreadyExistsError(f"{ref.dotted()} already exists.") from exc
        return row

    def maintain(
        self,
        p: PrincipalRef,
        ref: TableRef,
        *,
        keep_days: int = DEFAULT_KEEP_DAYS,
        keep_last: int = DEFAULT_KEEP_LAST,
        dry_run: bool = False,
        older_than_hours: int = DEFAULT_ORPHAN_AGE_HOURS,
        now: datetime | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> MaintenanceReport:
        """Expire a table's old snapshots and delete its orphaned files.

        Expiry is one `RemoveSnapshotsUpdate` through `commit_table`,
        authorized as `MAINTAIN_TABLE`. Orphans are then chosen
        against the metadata that remains and deleted one by one, confined
        to the table's own `data/` and `metadata/` directories.
        Orphan removal is skipped while a write run into this table is
        running, since its uncommitted files can be older than the cutoff.
        `TABLE_MAINTAINED` is recorded after deletion; a dry run changes and
        records nothing.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table.
        keep_days
            Snapshots younger than this many days are kept.
        keep_last
            The most recent this many snapshots are kept.
        dry_run
            Report what would happen without changing anything.
        older_than_hours
            Unreferenced files younger than this are kept (default 72, Iceberg's
            own default; at least 1, at most `MAX_ORPHAN_AGE_HOURS`).
        now
            The current time; `None` for the real one. Tests pass a later
            time to age files past the cutoff.
        should_stop
            Asked before the expiry, before the orphans are looked for and before each file is
            deleted; `None` never stops. When it answers `True` the run ends at that point.

        Returns
        -------
        MaintenanceReport
            What was (or would be) expired and deleted.

        Raises
        ------
        PermissionDenied
            If `p` lacks MAINTAIN_TABLE on the table.
        NotFoundError
            If the table does not exist.
        ValidationError
            If the table is external, was created by an older Lakana (its location does not
            end in its id), or the keep values or `older_than_hours` are out of range.
        MaintenanceStopped
            If `should_stop` answered `True`. What was done stays done (the expiry is one
            commit, files go one by one), the audit event records it as stopped, and a later
            run continues with what is left; the exception carries the report so far.

        Notes
        -----
        If the table changes between selecting snapshots and committing the
        expiry, nothing is expired or deleted: the report sets `conflict`
        and its note asks for a retry. If it changes after the expiry but
        before orphans are deleted — the metadata pointer is re-read
        immediately before deletion — the expiry stands, no file is
        deleted, and the note says so; that is not a conflict.
        """
        check_orphan_age(older_than_hours)
        securable = Securable.table_scope(ref.environment, ref.namespace, ref.name)
        self._authz.authorize(p, Action.MAINTAIN_TABLE, securable)
        ns, row = self._find_table(ref)
        if row.table_type is TableType.VIEW:
            return MaintenanceReport(
                dry_run=dry_run, note=f"{ref.dotted()} is a view; views have no files to maintain."
            )
        if row.table_type is TableType.ARTIFACT:
            raise ValidationError(artifact_refusal(ref.dotted(), "maintenance"))
        if row.table_type is not TableType.ICEBERG:
            raise ValidationError(
                "Only managed tables can be maintained; an external table's files belong to "
                "whoever produced them."
            )
        now = now or datetime.now(UTC)
        # Before anything commits: a cutoff that can't be computed must fail
        # the run before the expiry, never after it, unaudited.
        cutoff = now - timedelta(hours=older_than_hours)
        table_id, environment_id = row.id, ns.environment_id
        loaded = self._to_loaded_table(ref, row)
        location = loaded.metadata.location
        if not is_id_suffixed_location(location, table_id):
            raise ValidationError(
                f"{ref.dotted()} was created by an older Lakana, with a location that does "
                "not end in its id; re-create it before maintaining it."
            )
        metadata, metadata_location = loaded.metadata, loaded.metadata_location
        if should_stop is not None and should_stop():
            untouched = MaintenanceReport(
                dry_run=dry_run, note="Stopped before anything was changed."
            )
            if not dry_run:
                self._record_maintenance(
                    p, securable, untouched, keep_days, keep_last, stopped=True
                )
            raise MaintenanceStopped(untouched)
        old_tags = old_failed_tags(metadata, now_ms=int(now.timestamp() * 1000))
        expired = select_expired_snapshots(
            metadata,
            now=now,
            keep_days=keep_days,
            keep_last=keep_last,
            old_tags=old_tags,
            spare=self._snapshots_running_tasks_need(table_id, metadata),
        )
        # The refs go first, one by one: a tag whose snapshot stays has to go too, and a ref
        # that `RemoveSnapshotsUpdate` would take with its snapshot is already gone by then.
        expiry: list[TableUpdate] = [RemoveSnapshotRefUpdate(ref_name=name) for name in old_tags]
        if expired:
            expiry.append(RemoveSnapshotsUpdate(snapshot_ids=expired))
        if expiry and not dry_run:
            try:
                committed = self.commit_table(
                    p,
                    ref,
                    requirements=[
                        AssertTableUUID(uuid=table_id),
                        AssertRefSnapshotId(ref="main", snapshot_id=metadata.current_snapshot_id),
                    ],
                    updates=expiry,
                    action=Action.MAINTAIN_TABLE,
                    expected_metadata_location=metadata_location,
                )
            except ConflictError:
                return MaintenanceReport(
                    dry_run=dry_run, note="table changed during maintenance, retry", conflict=True
                )
            metadata, metadata_location = committed.metadata, committed.metadata_location
        elif expiry:
            metadata = apply_updates(metadata, expiry)
        note = None
        deleted: list[StoredObject] = []
        # The expiry is behind us; skipping the orphans leaves a consistent table that the next
        # run cleans up, so this is the cheapest point to honour a stop.
        stopped = should_stop is not None and should_stop()
        if stopped:
            note = "Stopped before orphan removal; the next run removes the orphans."
        elif self._write_in_flight(table_id):
            note = "A write into this table is running; orphan removal was skipped."
        else:
            resolved = self._storage.for_environment(ref.environment)
            root_prefix = resolved.config.root.rstrip("/") + "/"
            relative = location.removeprefix(root_prefix)
            referenced = referenced_paths(metadata, metadata_location, resolved.file_io)
            orphans: list[StoredObject] = []
            if has_foreign_reference(referenced, location):
                note = "referenced paths outside the table location; orphan removal skipped."
            else:
                orphans = select_orphans(
                    [
                        stored
                        for directory in ("data", "metadata")
                        for stored in resolved.storage.list_objects(f"{relative}/{directory}")
                    ],
                    root_prefix=root_prefix,
                    table_location=location,
                    nested_locations=nested_locations_within(
                        location, self._other_table_locations(environment_id, table_id, location)
                    ),
                    referenced=referenced,
                    cutoff=cutoff,
                )
            if orphans and self._metadata_location_of(table_id) != metadata_location:
                note = "the table changed before orphan removal; orphan removal skipped."
                orphans = []
            if dry_run:
                deleted = orphans
            else:
                deleted, stopped = self._delete_quietly(resolved, orphans, should_stop)
                if stopped:
                    note = (
                        f"Stopped after {len(deleted)} of {len(orphans)} orphaned files; "
                        "the next run removes the rest."
                    )
        report = MaintenanceReport(
            dry_run=dry_run,
            snapshots_expired=tuple(expired),
            files_deleted=len(deleted),
            bytes_deleted=sum(stored.size for stored in deleted),
            note=note,
            tags_removed=tuple(old_tags),
        )
        if not dry_run:
            self._record_maintenance(p, securable, report, keep_days, keep_last, stopped=stopped)
        if stopped:
            raise MaintenanceStopped(report)
        return report

    def _record_maintenance(
        self,
        p: PrincipalRef,
        securable: Securable,
        report: MaintenanceReport,
        keep_days: int,
        keep_last: int,
        *,
        stopped: bool,
    ) -> None:
        """Record `TABLE_MAINTAINED` for a run and commit.

        A dry run records nothing, so a stopped dry run is not passed here.

        Parameters
        ----------
        p
            The acting principal.
        securable
            The table.
        report
            What the run did.
        keep_days
            The retention in days the run used.
        keep_last
            The number of snapshots the run kept at least.
        stopped
            The run ended early because it was asked to.
        """
        AuditService(self._session).record(
            p.id,
            AuditEventType.TABLE_MAINTAINED,
            securable=securable,
            detail={
                **report.to_json(),
                "keep_days": keep_days,
                "keep_last": keep_last,
                **({"stopped": True} if stopped else {}),
            },
        )
        self._session.commit()

    def _snapshots_running_tasks_need(
        self, table_id: uuid.UUID, metadata: TableMetadata
    ) -> set[int]:
        """Find the snapshots a task run still in progress may set this table back to.

        A SQL task's write records the table and the snapshot before it. A Python task's
        baseline records only a snapshot id; snapshot ids are random 63-bit values, so a
        baseline that is among this table's snapshots is taken to be this table's. Task runs
        are few, so the baselines are compared here rather than passed to the database as a
        list as long as the table's history.

        Parameters
        ----------
        table_id
            The table.
        metadata
            Its current metadata.

        Returns
        -------
        set of int
            The snapshot ids maintenance must not expire.
        """
        rows = self._session.execute(
            select(
                TaskRun.written_table_id,
                TaskRun.written_snapshot_before,
                TaskRun.baseline_snapshot_id,
            ).where(
                TaskRun.status.not_in(TERMINAL_TASK_STATUSES),
                or_(
                    TaskRun.written_table_id == table_id, TaskRun.baseline_snapshot_id.is_not(None)
                ),
            )
        ).all()
        snapshot_ids = {s.snapshot_id for s in metadata.snapshots}
        needed: set[int] = set()
        for written_table_id, before, baseline in rows:
            if written_table_id == table_id and before is not None:
                needed.add(before)
            if baseline in snapshot_ids:
                needed.add(baseline)
        return needed

    def _metadata_location_of(self, table_id: uuid.UUID) -> str | None:
        """Read a table's current metadata pointer straight from the database.

        A column select, not the ORM row, so it is never served from the
        session's identity map: it sees a commit another session made since
        (Postgres' read-committed isolation).

        Parameters
        ----------
        table_id
            The table.

        Returns
        -------
        str or None
            Its metadata location; `None` if it has none or no longer exists.
        """
        return self._session.execute(
            select(Table.metadata_location).where(Table.id == table_id)
        ).scalar_one_or_none()

    def _write_in_flight(self, table_id: uuid.UUID) -> bool:
        """Report whether a create, replace or append run into a table is running.

        Parameters
        ----------
        table_id
            The table.

        Returns
        -------
        bool
            `True` if such a run is `RUNNING`.
        """
        return (
            self._session.execute(
                select(QueryRun.id).where(
                    QueryRun.target_table_id == table_id,
                    QueryRun.status == QueryRunStatus.RUNNING,
                    QueryRun.write_mode.in_(
                        [WriteMode.CREATE, WriteMode.REPLACE, WriteMode.APPEND]
                    ),
                )
            ).first()
            is not None
        )

    def _other_table_locations(
        self, environment_id: uuid.UUID, table_id: uuid.UUID, location: str
    ) -> list[str]:
        """List every other table's location in an environment, dropped ones too.

        Parameters
        ----------
        environment_id
            The environment.
        table_id
            The table to leave out.
        location
            Its own location, left out.

        Returns
        -------
        list of str
            `other_table_locations`: the records that keep files (live, claimed and dropped
            tables) and the roots derived from the other tables' metadata files, read in one
            statement.
        """
        return other_table_locations(
            self._session, environment_id=environment_id, table_id=table_id, location=location
        )

    @staticmethod
    def _delete_quietly(
        resolved: ResolvedStorage,
        orphans: Sequence[StoredObject],
        should_stop: Callable[[], bool] | None = None,
    ) -> tuple[list[StoredObject], bool]:
        """Delete orphans one by one, keeping going past a failure.

        Parameters
        ----------
        resolved
            The environment's storage.
        orphans
            What to delete.
        should_stop
            Asked before each file; `None` never stops.

        Returns
        -------
        deleted : list of StoredObject
            The ones actually deleted; a failure is logged by exception
            class alone and not counted.
        stopped : bool
            `True` if `should_stop` ended the deletion with files left.
        """
        deleted: list[StoredObject] = []
        for stored in orphans:
            if should_stop is not None and should_stop():
                return deleted, True
            try:
                resolved.storage.delete(stored.path)
            except Exception as exc:
                logger.warning("Failed to delete an orphaned file: %s", type(exc).__name__)
                continue
            deleted.append(stored)
        return deleted, False

    def _write_new_table(
        self,
        p: PrincipalRef,
        ref: TableRef,
        *,
        schema: pa.Schema,
        batches: Iterable[pa.RecordBatch],
        max_rows: int | None,
        sources: Sequence[LineageSource],
        query_run_id: uuid.UUID | None,
        properties: Mapping[str, str] | None = None,
    ) -> WriteResult:
        """Create a table from a result and publish it with its first snapshot, atomically.

        The location's claim is committed first (`_prepare_table`); the data
        files are written under the claimed, not yet published, location; the
        fast append is applied to the prepared metadata; `_publish_table` then
        inserts the row with the data already current, behind the publish
        fence. On a failure known not to have committed, the fresh location —
        which nothing else can use — is emptied, its claim removed and the
        session rolled back (`_release_location`); a failure in doubt keeps
        the files and the claim for the sweep.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The new table.
        schema
            The result's Arrow schema, which becomes the table's.
        batches
            The rows.
        max_rows
            Most rows to accept, or `None`.
        sources
            The tables the rows came from.
        query_run_id
            The run writing, if any.
        properties
            Table properties to create the table with (a history table's key), or `None`.

        Returns
        -------
        WriteResult
            What was written; `snapshot_id` is `None` for zero rows.

        Raises
        ------
        PermissionDenied
            If `p` may not create the table.
        AlreadyExistsError
            If the name is taken.
        ValidationError
            If the result's schema can't be stored, or the write is refused
            mid-stream.
        """
        self._commit_sent = False
        securable = Securable.table_scope(ref.environment, ref.namespace, ref.name)
        self._authz.authorize(p, Action.CREATE_TABLE, securable)
        self._guard.check(p, ref.environment, WriteOperation.WRITE, table_target(ref))
        prepared = self._prepare_table(
            ref,
            schema=arrow_to_iceberg_schema(schema),
            partition_spec=None,
            sort_order=None,
            properties=properties,
            location=None,
            holder_run_id=claim_holder(p, query_run_id),
        )
        writer = DataFileWriter(prepared.metadata, prepared.resolved.file_io, max_rows=max_rows)
        try:
            data_files = writer.write(batches)
            metadata, snapshot_id = prepared.metadata, None
            if data_files:
                _, updates, snapshot = stage_snapshot(
                    prepared.metadata,
                    prepared.location,
                    prepared.resolved.file_io,
                    data_files,
                    replace=False,
                )
                metadata = apply_updates(prepared.metadata, stamp_snapshots(updates, p.run_context))
                snapshot_id = snapshot.snapshot_id
            result = WriteResult(
                table_id=prepared.table_id,
                rows=writer.rows,
                files=len(data_files),
                snapshot_id=snapshot_id,
                schema_diff=SchemaDiff(),
            )
            self._record_write(
                p,
                securable,
                ref,
                WriteMode.CREATE,
                result,
                query_run_id,
                None,
                sources,
                prepared.environment_id,
                snapshot_before=None,
                publishes=True,
            )
            self._publish_table(prepared, metadata, owner_id=p.id)
        except Exception as exc:
            landed = self._settle_failed_write(
                exc,
                ref,
                table_id=prepared.table_id,
                snapshot_id=None,
                new_table=True,
                commit_sent=self._commit_sent,
                clean_up=lambda: self._release_location(prepared),
            )
            if not landed:
                raise
        return result

    def _record_write(
        self,
        p: PrincipalRef,
        securable: Securable,
        ref: TableRef,
        mode: WriteMode,
        result: WriteResult,
        query_run_id: uuid.UUID | None,
        watermark: str | None,
        sources: Sequence[LineageSource],
        environment_id: uuid.UUID,
        *,
        snapshot_before: int | None = None,
        publishes: bool = False,
        detail: Mapping[str, object] | None = None,
        policy_warnings: Sequence[BrokenPolicy] = (),
        columns_changed: bool = False,
    ) -> None:
        """Add a write's `TABLE_WRITE` event and lineage edges to the session, uncommitted.

        Also hands the numbers to the guard (`note_result`), which records them on the
        write's query run in the statement that checks the write fence, right before the
        commit. Nothing is written to the run here: a lock taken now would be held across the
        metadata file's upload.

        Parameters
        ----------
        p
            The acting principal.
        securable
            The target's securable.
        ref
            The target.
        mode
            The write mode.
        result
            What was written.
        query_run_id
            The run writing, if any.
        watermark
            The encoded watermark, if incremental.
        sources
            The tables read. No edges are added when nothing was committed.
        environment_id
            The target's environment.
        snapshot_before
            The target's current snapshot when the write was staged; `None` for a new table,
            a table without a snapshot and a view.
        publishes
            Whether the write creates the table or finds it not published.
        detail
            More fields for the event, after the common ones (a merge's counts and key).
        policy_warnings
            The row filters and column masks the write's schema change leaves out of date: named
            in the event (``broken_policies``, only when there are any) and handed to the guard,
            which records them on the task run of a write in a run.
        columns_changed
            Whether the write removed or replaced a column: the guard records that on the task run
            of a write in a run, with or without a policy, so that what a reader who manages no
            policies is shown does not tell whether one exists.
        """
        stored = tuple(warning.to_json() for warning in policy_warnings)
        AuditService(self._session).record(
            p.id,
            AuditEventType.TABLE_WRITE,
            securable=securable,
            detail={
                "mode": mode.value,
                "target": ref.dotted(),
                "snapshot_id": result.snapshot_id,
                "rows": result.rows,
                "files": result.files,
                "watermark": watermark,
                "schema_diff": None
                if result.schema_diff.is_empty()
                else result.schema_diff.to_json(),
                "run_id": None if query_run_id is None else str(query_run_id),
                "principal": p.name,
                **({"broken_policies": list(stored)} if stored else {}),
                **(detail or {}),
            },
        )
        if result.snapshot_id is not None:
            self._add_lineage_edges(
                environment_id=environment_id,
                sources=sources,
                target_id=result.table_id,
                target_name=ref.dotted(),
                target_snapshot_id=result.snapshot_id,
                query_run_id=query_run_id,
                connections=self._connection_sources(p, environment_id),
            )
        self._guard.note_result(
            ref.environment,
            table_target(ref),
            rows=result.rows,
            snapshot_id=result.snapshot_id,
            table_id=result.table_id,
            snapshot_before=snapshot_before,
            publishes=publishes,
        )
        if columns_changed:
            self._guard.note_schema_change(
                ref.environment, table_target(ref), table=ref.dotted(), policies=stored
            )

    def _connection_sources(
        self, p: PrincipalRef, environment_id: uuid.UUID
    ) -> list[ConnectionSource]:
        """Find the connections a write by `p` records as lineage sources.

        Parameters
        ----------
        p
            The acting principal; only a token bound to a task attempt names any.
        environment_id
            The target's environment.

        Returns
        -------
        list of ConnectionSource
            See `lakana_core.catalog.connection_lineage.connection_sources`.
        """
        return connection_sources(self._session, p, environment_id)

    def _add_lineage_edges(
        self,
        *,
        environment_id: uuid.UUID,
        sources: Sequence[LineageSource],
        target_id: uuid.UUID,
        target_name: str,
        target_snapshot_id: int | None,
        query_run_id: uuid.UUID | None,
        connections: Sequence[ConnectionSource] = (),
    ) -> None:
        """Add one lineage edge per source to the session, uncommitted.

        Parameters
        ----------
        environment_id
            The environment both ends live in.
        sources
            The source tables.
        target_id
            The target's id.
        target_name
            The target's dotted name.
        target_snapshot_id
            The target's new snapshot, or `None` for a view.
        query_run_id
            The run writing, if any.
        connections
            The connections the writing task's asset declares (`_connection_sources`), one
            edge each; never from request input.
        """
        now = datetime.now(UTC)
        self._session.add_all(
            connection_edges(
                connections,
                environment_id=environment_id,
                target_id=target_id,
                target_name=target_name,
                target_snapshot_id=target_snapshot_id,
                query_run_id=query_run_id,
                created_at=now,
            )
        )
        for source in sources:
            self._session.add(
                LineageEdge(
                    id=uuid.uuid4(),
                    environment_id=environment_id,
                    source_table_id=source.table_id,
                    source_name=source.name,
                    source_snapshot_id=source.snapshot_id,
                    target_table_id=target_id,
                    target_name=target_name,
                    target_snapshot_id=target_snapshot_id,
                    query_run_id=query_run_id,
                    created_at=now,
                )
            )

    def _commit_landed(
        self, ref: TableRef, *, table_id: uuid.UUID, snapshot_id: int | None, new_table: bool
    ) -> bool:
        """Say whether a write's commit reached the database, from the table's own state.

        The question is asked fresh: the session was rolled back, so this starts a new
        transaction and reads the stored columns of the `tables` row (never the ORM objects the
        failed attempt left in the session), then the metadata file the pointer names. A write
        that created the table landed if its row exists. Any other landed if the current
        metadata lists the snapshot the write produced (a later commit by someone else does not
        hide it: the snapshot stays in the list).

        The pointer is read with a locking read (`FOR SHARE`) on Postgres, bounded by
        `LOCK_TIMEOUT_SECONDS`. A plain read cannot see a backend that is still inside its
        `COMMIT`: a commit that was sent, answered with an error to the client, and is applied by
        the server only after the plain read would be judged "not landed", its files deleted, and
        then it lands. The transaction that moves the pointer holds the row's lock from its
        `UPDATE` until it ends, so the locking read waits for it and then reads the final row.
        On SQLite a commit is synchronous and the clause does nothing. A wait that times out
        raises (the caller treats it as "unknown"), it never answers "not landed". A new table
        has no row to lock until it is committed; the caller does not delete its files when the
        failure came from the commit.

        Whatever happens, no transaction is left open on the session.

        Parameters
        ----------
        ref
            The table, for the storage to read the metadata from.
        table_id
            The table's id (the new id for a create).
        snapshot_id
            The snapshot the write produced; `None` when it produced none.
        new_table
            Whether the write created the table.

        Returns
        -------
        bool
            `True` if the write is committed, `False` if it is not.
        """
        try:
            resolved = self._storage.for_environment(ref.environment)
            if self._session.get_bind().dialect.name == "postgresql":
                self._session.execute(
                    text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_SECONDS}s'")  # nosec B608
                )
            location = self._session.execute(
                select(Table.metadata_location)
                .where(Table.id == table_id)
                .with_for_update(read=True)
            ).scalar_one_or_none()
            self._session.rollback()  # release the lock before any file is read
            if new_table:
                return location is not None
            if location is None or snapshot_id is None:
                return False
            return read_metadata(resolved.file_io, location).snapshot_by_id(snapshot_id) is not None
        finally:
            self._session.rollback()

    def _settle_failed_write(
        self,
        exc: Exception,
        ref: TableRef,
        *,
        table_id: uuid.UUID,
        snapshot_id: int | None,
        new_table: bool,
        commit_sent: bool,
        clean_up: Callable[[], None],
    ) -> bool:
        """Decide, after a write raised, whether its files may be deleted, and delete them if so.

        The database may have applied the commit and failed only on the way back (a connection
        that dropped after the `COMMIT`, a failure in code after `session.commit()` returned).
        The table's new snapshot then names the write's files, and deleting them breaks the
        table. So the session is rolled back first and, when the commit may have been sent, the
        table is read again (`_commit_landed`) before anything is deleted:

        - landed: every file is kept and the caller reports the write as committed;
        - not landed: `clean_up` runs, as it always did, except for a create whose failure came
          from the commit itself: nothing can show a table that is still inside its commit, so
          its files are kept and the outcome is unknown;
        - the check itself fails or times out (the database is unreachable): every file is kept
          and `WriteOutcomeUnknownError` says so. An orphan is harmless, a deleted live file is
          not. `lakana table maintain` removes the orphans of a write into an existing table;
          a create in doubt keeps its claim `CLAIMED`, and the location sweep
          (`lakana_core.catalog.location_sweep`) removes its files once it has proven, under the
          claim's lock, that the commit did not land.

        A `LakanaError` is raised before the commit by construction (a conflict, a refusal, a
        validation), and so is any exception when `commit_sent` is false: those need no check,
        the files are deleted and the caller re-raises. A write that produced no snapshot (an
        append of no rows) wrote no file a snapshot could name and is treated the same.

        Parameters
        ----------
        exc
            What the write raised.
        ref
            The table.
        table_id
            The table's id (the new id for a create).
        snapshot_id
            The snapshot the write produced, or `None`.
        new_table
            Whether the write created the table.
        commit_sent
            Whether the write reached the point where its `COMMIT` was sent.
        clean_up
            Deletes the files the write wrote.

        Returns
        -------
        bool
            `True` if the write turned out to be committed (the caller returns its result),
            `False` if the files were deleted (the caller re-raises `exc`).

        Raises
        ------
        WriteOutcomeUnknownError
            If the table could not be read to tell, or a create failed inside its commit and
            is not visible; nothing was deleted.
        """
        self._rollback_quietly()
        if (
            isinstance(exc, LakanaError)
            or not commit_sent
            or (snapshot_id is None and not new_table)
        ):
            clean_up()
            return False
        try:
            landed = self._commit_landed(
                ref, table_id=table_id, snapshot_id=snapshot_id, new_table=new_table
            )
        except Exception as check:
            logger.warning(
                "A write to a table failed (%s) and the table could not be read to tell whether "
                "it was committed (%s); its files were kept.",
                type(exc).__name__,
                type(check).__name__,
            )
            self._rollback_quietly()
            raise WriteOutcomeUnknownError(self._unknown_message(ref, new_table)) from exc
        if landed:
            logger.warning(
                "A write to a table was committed although it raised (%s); its files were kept "
                "and the write is reported as committed.",
                type(exc).__name__,
            )
            return True
        if new_table:
            logger.warning(
                "A create failed inside its commit (%s) and the table is not visible; its files "
                "were kept.",
                type(exc).__name__,
            )
            raise WriteOutcomeUnknownError(self._unknown_message(ref, new_table)) from exc
        clean_up()
        return False

    def _release_location(self, prepared: _PreparedTable) -> None:
        """Remove what a create known not to have committed wrote: claim first, files, record.

        The claim is made `ABANDONED` (`release_claim`) before a file is deleted, and the record
        is deleted only once every file is gone (`forget_location`), so a deletion that fails
        leaves the record to the sweep, never a directory without one. A record that says the
        location is in use keeps every file. Every failure is logged by its exception class
        alone and swallowed, so it never replaces the caller's real error; the claim then stays
        and the sweep decides.

        Parameters
        ----------
        prepared
            The failed create.
        """
        try:
            deletable = release_claim(self._session, prepared.location)
        except Exception as exc:
            self._rollback_quietly()
            logger.warning(
                "The claim of a failed create could not be released (%s); its files were kept "
                "for the clean-up.",
                type(exc).__name__,
            )
            return
        if not deletable:
            logger.warning("A failed create's location is recorded as in use; its files were kept.")
            return
        if not cleanup_location(prepared.resolved, prepared.location):
            return
        try:
            forget_location(self._session, prepared.location)
        except Exception as exc:
            self._rollback_quietly()
            logger.warning(
                "The record of a failed create could not be removed (%s); the clean-up removes it.",
                type(exc).__name__,
            )

    @staticmethod
    def _unknown_message(ref: TableRef, new_table: bool) -> str:
        """Build the text of `WriteOutcomeUnknownError`.

        Parameters
        ----------
        ref
            The table.
        new_table
            Whether the write was a create.

        Returns
        -------
        str
            The message; it says what stays behind and what removes it.
        """
        return WRITE_OUTCOME_UNKNOWN_MESSAGE.format(
            table=ref.dotted(),
            leftovers=(
                "They stay in the environment's storage until the worker's clean-up has proven "
                "that the commit did not land: it removes them then, and keeps them if the table "
                "turns out to exist. Creating the table again is safe."
                if new_table
                else "Files of a write that was not committed are removed by "
                "'lakana table maintain'."
            ),
        )

    def _rollback_quietly(self) -> None:
        """Roll the session back after a failed write, never masking the real error.

        A rollback can itself fail (the same lost connection that failed
        the write); that failure is logged by exception class alone.
        """
        try:
            self._session.rollback()
        except Exception as exc:
            logger.warning(
                "Failed to roll back the session after a failed write: %s", type(exc).__name__
            )

    def drop_table(
        self, p: PrincipalRef, ref: TableRef, *, purge: bool = False, include_views: bool = True
    ) -> None:
        """Remove a table from the catalog; a managed table's files follow after the undo period.

        Every active grant, row filter, and column mask bound to this
        table's id is soft-revoked first, in the same transaction
        — otherwise a table later re-created at the same location or
        under the same name would silently inherit the old table's grants
        and policies.

        A managed table's location record becomes its tombstone in the same transaction
        (`lakana_core.catalog.tombstones.tombstone`): `DROPPED`, with the row's snapshot, the
        policies revoked here and ``purge_after`` (now plus the undo period). Its files stay
        until then, so `undrop_table` can restore it, and the location sweep deletes them once
        ``purge_after`` has passed. An external table, a view, and a managed table without a
        location record of its own keep their files. One `TABLE_DROPPED` audit event records
        the drop.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table reference.
        purge
            When True, the files are due at once: the next sweep deletes them, and the table
            cannot be restored. When False, they wait for the undo period.
        include_views
            `False` refuses to drop a view (`NotFoundError`), for the
            Iceberg REST surface, which does not expose views: without this, `DELETE`
            disagreed with `HEAD`, which already reports a view absent.
            `table delete` (drops either kind) keeps the default
            `True`.

        Raises
        ------
        NotFoundError
            If the table does not exist, or `include_views` is `False` and
            it is a view or an artifact.
        ValidationError
            If it is an artifact: those are dropped with `lakana artifact
            drop`, which also deletes the bytes.
        PermissionDenied
            If the principal may not drop this table.
        """
        self._authz.authorize(
            p, Action.DROP_TABLE, Securable.table_scope(ref.environment, ref.namespace, ref.name)
        )
        self._guard.check(p, ref.environment, WriteOperation.DROP_TABLE, table_target(ref))
        _, row = self._find_table(ref)
        if row.table_type is TableType.ARTIFACT:
            if not include_views:
                raise NotFoundError(_not_exposed_artifact_message(ref.dotted()))
            raise ValidationError(
                f"{ref.dotted()} is an artifact; drop it with `lakana artifact drop "
                f"{ref.dotted()}`, which also deletes its versions' bytes."
            )
        if not include_views and row.table_type is TableType.VIEW:
            raise NotFoundError(
                f"{ref.dotted()} is a view; views are not exposed over the Iceberg REST catalog."
            )
        row = self._lock_table_row(ref, row.id)
        now = db_now(self._session)
        drop_row(
            self._session,
            p,
            ref,
            row,
            now=now,
            purge_after=now if purge else now + timedelta(days=self._undo_days),
            reason="purge" if purge else "user",
        )
        self._guard.record_pending()
        self._session.commit()

    def _lock_table_row(self, ref: TableRef, table_id: uuid.UUID) -> Table:
        """Take a table row's lock for the rest of the transaction and read it again.

        A no-op `UPDATE` (a row lock on Postgres, the write lock on SQLite), so a commit that
        lands between the lookup and the drop is in the row the drop's snapshot records, not
        lost to it.

        Parameters
        ----------
        ref
            The table, for the error.
        table_id
            Its id.

        Returns
        -------
        Table
            The row as it is now.

        Raises
        ------
        NotFoundError
            If the row is gone (dropped at the same moment).
        """
        row = lock_row(self._session, Table, table_id)
        if row is None:
            raise NotFoundError(
                f"Table '{shown(ref.name)}' not found in {shown(str(ref.namespace))}."
            )
        return row

    def undrop_table(
        self, p: PrincipalRef, ref: TableRef, *, new_name: str | None = None
    ) -> LoadedTable:
        """Restore a dropped table within its undo period: the same table, as it was.

        The newest tombstone of `ref`'s name in its namespace is restored, in one transaction,
        under the tombstone's lock (the lock the location sweep takes before each step of a
        purge): the row with the same id and the same metadata location, its column
        descriptions and indexed columns, the location record back to `LIVE`, and exactly the
        grants, row filters and column masks the drop revoked, as they were. A policy revoked
        before the drop stays revoked. One `TABLE_UNDROPPED` audit event lists what came back.
        A second tombstone of the same name is restored by a second call, under another name.

        Needs `DROP_TABLE` on the namespace and `CREATE_TABLE` on the name it comes back under,
        and passes the protected-environment guard as a create does.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The dropped table, by the name it had.
        new_name
            Restore it under this name in the same namespace instead.

        Returns
        -------
        LoadedTable
            The restored table.

        Raises
        ------
        ValidationError
            If `new_name` is not a valid table name.
        PermissionDenied
            Without either right, or in a protected environment outside a run without a
            break-glass reason.
        NotFoundError
            If no dropped table of that name can be restored, or its namespace is gone.
        AlreadyExistsError
            If a table of the name exists now (`NAME_TAKEN_MESSAGE` names `--as`).
        ConflictError
            If the undo period has ended or the purge has started, the table's metadata file is
            gone, or the write failed for another reason (the namespace dropped at the same
            moment).
        """
        target = ref if new_name is None else replace(ref, name=new_name)
        if new_name is not None:
            validate_path_component(new_name, what="Table name")
            refuse_percent_in_new_name(new_name, what="Table name")
        self._authz.authorize(
            p, Action.DROP_TABLE, Securable.namespace_scope(ref.environment, ref.namespace)
        )
        self._authz.authorize(
            p,
            Action.CREATE_TABLE,
            Securable.table_scope(target.environment, target.namespace, target.name),
        )
        self._guard.check(p, ref.environment, WriteOperation.CREATE_TABLE, table_target(target))
        env = self._find_environment(ref.environment)
        if not lock_live_environment(self._session, env.id):
            self._session.rollback()
            raise NotFoundError(f"No environment named {ref.environment!r}.")
        record_id = find_tombstone(self._session, env.id, ref.namespace, ref.name)
        record = None if record_id is None else lock_location(self._session, record_id)
        snapshot = None if record is None else record.dropped_table
        if record is None or record.state is not TableLocationState.DROPPED or not snapshot:
            self._session.rollback()
            raise NotFoundError(
                NO_TOMBSTONE_MESSAGE.format(
                    name=shown(ref.name), namespace=shown(str(ref.namespace))
                )
            )
        if record.purge_started_at is not None:
            self._session.rollback()
            raise ConflictError(PURGE_STARTED_MESSAGE.format(table=shown(ref.dotted())))
        assert record.purge_after is not None and record.dropped_at is not None  # nosec B101
        if db_now(self._session) >= record.purge_after:
            purge_after = record.purge_after.isoformat(timespec="seconds")
            self._session.rollback()
            raise ConflictError(
                PERIOD_PASSED_MESSAGE.format(table=shown(ref.dotted()), purge_after=purge_after)
            )
        namespace = self._lookup_namespace(env, ref.namespace)
        if namespace is None:
            self._session.rollback()
            raise NotFoundError(
                NAMESPACE_GONE_MESSAGE.format(
                    table=shown(ref.dotted()), namespace=shown(str(ref.namespace))
                )
            )
        taken = self._lookup_table(namespace, target.name)
        if taken is not None:
            holder = taken.table_type
            self._session.rollback()
            raise AlreadyExistsError(self._undrop_taken_message(ref, target, holder))
        if not self._metadata_file_exists(ref.environment, snapshot["row"]["metadata_location"]):
            self._session.rollback()
            raise ConflictError(FILES_GONE_MESSAGE.format(table=shown(ref.dotted())))
        namespace_id = namespace.id
        now = db_now(self._session)
        try:
            row = restore_table(
                self._session, snapshot, namespace_id=namespace_id, name=target.name
            )
            retention_cleared = refresh_restored(row, p.id, now)
            restored = restore_policies(
                self._session,
                record,
                table_id=row.id,
                namespace=target.namespace.joined(),
                name=target.name,
            )
            AuditService(self._session).record(
                p.id,
                AuditEventType.TABLE_UNDROPPED,
                securable=Securable.table_scope(target.environment, target.namespace, target.name),
                detail={
                    "table_id": str(row.id),
                    "name": target.name,
                    "dropped_name": ref.name,
                    "dropped_at": record.dropped_at.isoformat(),
                    "restored_policies": restored,
                    "retention_cleared": retention_cleared,
                },
            )
            revive(record)
            self._guard.record_pending()
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            holder = self._session.scalar(
                select(Table.table_type).where(
                    Table.namespace_id == namespace_id, Table.name == target.name
                )
            )
            if holder is not None:
                raise AlreadyExistsError(self._undrop_taken_message(ref, target, holder)) from exc
            logger.warning(
                "An undrop failed on an integrity error that is not a name clash: %s",
                type(exc).__name__,
            )
            raise ConflictError(
                f"Could not restore {shown(ref.dotted())}: the write failed for a reason other "
                "than a name collision. Check that the namespace still exists, then try again."
            ) from exc
        return self._to_loaded_table(target, row)

    def _metadata_file_exists(self, environment: str, metadata_location: str) -> bool:
        """Say whether a dropped table's metadata file is still in storage.

        The tombstone's lock keeps the sweep from starting a purge meanwhile; this guards the
        undrop against files deleted by anything else, so it never commits a row that points
        at nothing.

        Parameters
        ----------
        environment
            The environment.
        metadata_location
            The snapshot's metadata location; a tombstone is only written for a table that has
            one.

        Returns
        -------
        bool
            Whether the file exists.
        """
        resolved = self._storage.for_environment(environment)
        return bool(resolved.file_io.new_input(metadata_location).exists())

    @staticmethod
    def _undrop_taken_message(ref: TableRef, target: TableRef, holder: TableType) -> str:
        """Word the refusal of an undrop whose name is taken.

        Parameters
        ----------
        ref
            The dropped table.
        target
            Where it was to come back.
        holder
            The type of the row that holds the name.

        Returns
        -------
        str
            `NAME_TAKEN_MESSAGE`, filled in.
        """
        return NAME_TAKEN_MESSAGE.format(
            table=shown(ref.dotted()),
            name=shown(target.name),
            namespace=shown(str(target.namespace)),
            holder=name_holder(holder),
        )

    def list_dropped_tables(
        self, p: PrincipalRef, environment: str, namespace: NamespacePath
    ) -> list[DroppedTable]:
        """List the dropped tables of a namespace whose files are not deleted yet.

        Parameters
        ----------
        p
            The acting principal.
        environment
            Environment name.
        namespace
            The namespace the tables were dropped from.

        Returns
        -------
        list of DroppedTable
            Newest drop first, each with its purge date and whether it can still be restored.
            One the caller may not read by its name (a token narrowed to some tables) is left
            out, as the table list leaves out a table.

        Raises
        ------
        NotFoundError
            If the environment or namespace does not exist.
        PermissionDenied
            If the principal may not read this namespace (the rule of `list_tables`).
        """
        self._authz.authorize(
            p, Action.READ_TABLE, Securable.namespace_scope(environment, namespace)
        )
        env = self._find_environment(environment)
        self._find_namespace(env, namespace)
        dropped = list_tombstones(self._session, env.id, namespace, now=db_now(self._session))
        return [
            entry
            for entry in dropped
            if self.can_read_table(p, TableRef(environment, namespace, entry.name))
        ]

    def rename_table(self, p: PrincipalRef, source: TableRef, destination: TableRef) -> LoadedTable:
        """Move a table to a new namespace or name.

        Authorization is checked against **both** securables before
        anything is looked up or mutated: `DROP_TABLE` on the source and
        `CREATE_TABLE` on the destination. Authorizing only the source
        would let a principal move a table into a namespace they cannot
        write to; only the destination would let them move it out of one
        they cannot drop from. This holds even when source and destination
        name the same namespace — that degenerate case is not
        special-cased.

        The metadata location is unchanged: a rename is a catalog operation
        and does not move data.

        Every grant, row filter, and column mask bound to this table's id
        keeps that binding and so keeps applying at the new
        name; only their display-only `namespace`/`table` columns are
        updated here, in the same transaction, so they stay accurate for
        anyone reading them directly rather than through the id.

        Parameters
        ----------
        p
            The acting principal.
        source
            The table to move.
        destination
            Where to move it.

        Returns
        -------
        LoadedTable
            The table at its new reference.

        Raises
        ------
        ValidationError
            If source and destination are in different environments, or the
            destination name contains a ``.``. The source name is not
            checked, so a table named before that rule can be renamed away.
        NotFoundError
            If the source table or the destination namespace does not
            exist, or the source is a view or an artifact (an artifact is
            never renamed).
        AlreadyExistsError
            If a table, view or artifact already exists at the destination,
            also when another writer took the name at the same moment; for an
            artifact the message says so and names how to drop it.
        ConflictError
            If the write failed for another reason, such as the destination
            namespace being dropped at the same moment.
        PermissionDenied
            If the principal may not drop the source or create the
            destination.
        """
        validate_path_component(destination.name, what="Table name")
        refuse_percent_in_new_name(destination.name, what="Table name")
        self._authz.authorize(
            p,
            Action.DROP_TABLE,
            Securable.table_scope(source.environment, source.namespace, source.name),
        )
        self._authz.authorize(
            p,
            Action.CREATE_TABLE,
            Securable.table_scope(destination.environment, destination.namespace, destination.name),
        )
        self._guard.check(p, source.environment, WriteOperation.RENAME_TABLE, table_target(source))
        if source.environment != destination.environment:
            raise ValidationError(
                "A table cannot be renamed across environments "
                f"({source.environment!r} to {destination.environment!r})."
            )
        _, row = self._find_table(source)
        if row.table_type is TableType.VIEW:
            raise NotFoundError(f"{source.dotted()} is a view; views can't be renamed here.")
        if row.table_type is TableType.ARTIFACT:
            raise NotFoundError(f"{source.dotted()} is an artifact; artifacts can't be renamed.")
        env = self._find_environment(destination.environment)
        target_ns = self._find_namespace(env, destination.namespace)
        taken = self._lookup_table(target_ns, destination.name)
        if taken is not None:
            raise AlreadyExistsError(
                _name_taken_message(taken, destination.name, destination.namespace)
            )
        target_namespace_id = target_ns.id
        row.namespace_id = target_namespace_id
        row.name = destination.name
        destination_namespace = destination.namespace.joined()
        try:
            for bound in (Grant, RowFilter, ColumnMask):
                self._session.execute(
                    update(bound)
                    .where(bound.table_id == row.id)
                    .values(namespace=destination_namespace, table=destination.name)
                )
            self._guard.record_pending()
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            # Another writer took the name between the check above and this write (the
            # unique constraint arbitrates), or the namespace was dropped: the same two
            # readings `create_table` makes of its own IntegrityError.
            conflicting = self._session.execute(
                select(Table).where(
                    Table.namespace_id == target_namespace_id, Table.name == destination.name
                )
            ).scalar_one_or_none()
            if conflicting is not None:
                raise AlreadyExistsError(
                    _name_taken_message(conflicting, destination.name, destination.namespace)
                ) from exc
            logger.warning(
                "A table rename failed on an integrity error that is not a name clash: %s",
                type(exc).__name__,
            )
            raise ConflictError(
                f"Could not rename table {source.name!r} to {destination.name!r} in "
                f"{destination.namespace}: the write failed for a reason other than a name "
                "collision. Check that the namespace still exists, then try again."
            ) from exc
        return self._to_loaded_table(destination, row)

    def table_exists(self, p: PrincipalRef, ref: TableRef) -> bool:
        """Report whether a table exists.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table reference.

        Returns
        -------
        bool
            True when the table exists. A view or an artifact is reported as
            absent: this backs the Iceberg REST `HEAD`, which exposes neither.

        Raises
        ------
        NotFoundError
            If the environment or namespace does not exist.
        PermissionDenied
            If the principal may not read this table.
        """
        self._authz.authorize(
            p, Action.READ_TABLE, Securable.table_scope(ref.environment, ref.namespace, ref.name)
        )
        env = self._find_environment(ref.environment)
        ns = self._find_namespace(env, ref.namespace)
        row = self._lookup_table(ns, ref.name)
        return row is not None and row.table_type not in (TableType.VIEW, TableType.ARTIFACT)

    def list_tables(
        self,
        p: PrincipalRef,
        environment: str,
        namespace: NamespacePath,
        *,
        include_views: bool = True,
    ) -> list[TableRef]:
        """List the tables in a namespace.

        Parameters
        ----------
        p
            The acting principal.
        environment
            Environment name.
        namespace
            The namespace to list.
        include_views
            `False` leaves views and artifacts out, for the Iceberg REST
            surface.

        Returns
        -------
        list of TableRef
            References to the namespace's tables, ordered by name.

        Raises
        ------
        NotFoundError
            If the environment or namespace does not exist.
        PermissionDenied
            If the principal may not read this namespace.
        """
        self._authz.authorize(
            p, Action.READ_TABLE, Securable.namespace_scope(environment, namespace)
        )
        env = self._find_environment(environment, p)
        ns = self._find_namespace(env, namespace)
        query = select(Table).where(Table.namespace_id == ns.id)
        if not include_views:
            query = query.where(Table.table_type.not_in([TableType.VIEW, TableType.ARTIFACT]))
        rows = self._session.execute(query.order_by(Table.name)).scalars().all()
        return [
            TableRef(environment=environment, namespace=namespace, name=row.name) for row in rows
        ]
