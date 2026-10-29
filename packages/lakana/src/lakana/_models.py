"""Frozen dataclasses returned by the SDK's data-plane resources.

Each class mirrors one of `lakana_server.api.platform.schemas`'s `*Out`
response bodies, field for field — the resource modules in
`lakana.resources` are the only code that builds these from a JSON response.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class Environment:
    """A platform environment.

    Parameters
    ----------
    name
        Environment name.
    storage_root
        The environment's storage root URI.
    storage_secret_ref
        The name of the secret backing this environment's storage
        credentials, or `None` if it has none.
    is_protected
        Whether deleting this environment and changing its storage are
        refused.
    properties
        Free-form metadata.
    retention_days
        The retention of its tables that set none of their own, in days; `None` when off, and
        from a server older than retention.
    deleted_at
        When it was deleted; `None` unless it is (`Environments.list(deleted=True)`).
    purge_after
        For a deleted environment, until when `Environments.restore` can restore it; from then
        on the server's clean-up deletes its tables' files. `None` unless deleted.
    """

    name: str
    storage_root: str
    storage_secret_ref: str | None
    is_protected: bool
    properties: dict[str, str]
    retention_days: int | None = None
    deleted_at: datetime | None = None
    purge_after: datetime | None = None


@dataclass(frozen=True)
class OrphanedLocation:
    """A table directory under ``managed/`` that no location record names.

    Left by a release before location records (`Environments.orphaned_locations`).

    Parameters
    ----------
    location
        Its path relative to the environment's storage root.
    files
        How many files it held when it was listed.
    bytes
        Their total size.
    newest_file_at
        When its newest file was last changed; `None` when the storage does not say.
    recent
        Whether a file is newer than the age asked for, or of unknown age: never deleted.
    deleted
        Whether `Environments.delete_orphaned_locations` removed it.
    files_deleted
        The files the deletion removed from it.
    bytes_deleted
        Their total size.
    kept_because
        Why the deletion left it; `None` in a listing and for a deleted directory.
    """

    location: str
    files: int
    bytes: int
    newest_file_at: datetime | None
    recent: bool
    deleted: bool = False
    files_deleted: int = 0
    bytes_deleted: int = 0
    kept_because: str | None = None


@dataclass(frozen=True)
class OrphanedLocations:
    """What a listing or a deletion of directories without a location record found and did.

    Parameters
    ----------
    environment
        The environment whose storage root was looked at.
    older_than_days
        The age asked for.
    locations
        The directories.
    complete
        `False` when the server's time limit stopped the call with work left; call again to
        continue.
    """

    environment: str
    older_than_days: int
    locations: list[OrphanedLocation]
    complete: bool


@dataclass(frozen=True)
class UsagePart:
    """The storage one kind of thing uses in an environment.

    Parameters
    ----------
    count
        How many there are.
    bytes
        The sum of the sizes that are known.
    unmeasured
        How many have no size yet, or one measured on an earlier version of a table (its last
        known size is in `bytes`); the worker's next sweeps measure them.
    """

    count: int
    bytes: int
    unmeasured: int


@dataclass(frozen=True)
class EnvironmentUsage:
    """What `Client.environments.usage` returns: the storage one environment takes.

    Parameters
    ----------
    environment
        The environment's name.
    tables
        Managed tables, sized by the files their current snapshot names.
    dropped_tables
        Dropped managed tables that can still be restored; their files are kept until then.
    artifacts
        Artifacts, sized by the files stored for all their versions.
    code_archives
        One code archive per deployed project version.
    total_bytes
        `tables`, `dropped_tables`, `artifacts` and `code_archives` added up.
    complete
        Whether every size was known; when `False`, `total_bytes` is a floor.
    """

    environment: str
    tables: UsagePart
    dropped_tables: UsagePart
    artifacts: UsagePart
    code_archives: UsagePart
    total_bytes: int
    complete: bool


@dataclass(frozen=True)
class Namespace:
    """A namespace within an environment.

    Parameters
    ----------
    path
        Dot-joined namespace path, such as ``gold.sales``.
    properties
        Free-form metadata.
    """

    path: str
    properties: dict[str, str]


@dataclass(frozen=True)
class Column:
    """One column of a table's schema.

    Parameters
    ----------
    name
        Column name.
    type
        The Iceberg type in the API's JSON form, e.g. `"long"` or
        `"decimal(10,2)"`.
    nullable
        Whether the column accepts nulls.
    description
        The column's description; `None` when it has none, and from a server older than
        descriptions.
    description_source
        ``project`` for the text of the asset header, ``edited`` for one set through the API;
        `None` without a description.
    """

    name: str
    type: str
    nullable: bool
    description: str | None = None
    description_source: str | None = None


@dataclass(frozen=True)
class TaskTokenScope:
    """The scope a task-token caller is narrowed to.

    Parameters
    ----------
    environment
        The one environment the token may touch.
    asset_scope
        The declared asset patterns.
    """

    environment: str
    asset_scope: list[str]


@dataclass(frozen=True)
class CiTokenScope:
    """What a CI token (``lakana_ci_…``) caller may do; never the token itself.

    Parameters
    ----------
    rule_id
        The CI trust rule that accepted the job's ID token.
    environment
        The one environment it may deploy to.
    repository
        The one repository it may deploy.
    kinds
        What it may queue, once: ``deploy`` and/or ``dry_run``.
    sha
        The commit a deploy is pinned to, or `None`.
    expires_at
        When the token stops working.
    used
        Whether it has already queued its one deploy or dry run.
    """

    rule_id: str
    environment: str
    repository: str
    kinds: list[str]
    sha: str | None
    expires_at: datetime | None
    used: bool


@dataclass(frozen=True)
class Whoami:
    """Who the configured credential authenticates as.

    Parameters
    ----------
    name
        The principal's name.
    type
        `"USER"` or `"SERVICE_ACCOUNT"`.
    task_token
        The scope, when the credential is a task token; `None` for a PAT.
    ci_token
        The scope, when the credential is a CI token; `None` otherwise.
    """

    name: str
    type: str
    task_token: TaskTokenScope | None
    ci_token: CiTokenScope | None = None


@dataclass(frozen=True)
class InstanceInfo:
    """What an instance says about itself.

    Parameters
    ----------
    version
        The platform version the server runs.
    """

    version: str


@dataclass(frozen=True)
class TableReference:
    """One table or view a view references.

    Parameters
    ----------
    id
        Its id.
    name
        Its dotted name when the view was created.
    """

    id: str
    name: str


@dataclass(frozen=True)
class Table:
    """A managed (Iceberg) table, an external table, or a view.

    Parameters
    ----------
    name
        Table name.
    namespace
        Dot-joined namespace path the table lives in.
    uuid
        The Iceberg ``table-uuid``.
    metadata_location
        Absolute URI of the current metadata file. `None` for an external
        table or a view.
    columns
        The table's current schema.
    kind
        ``TABLE``, ``EXTERNAL``, ``VIEW`` or ``ARTIFACT``.
    sql
        A view's SELECT.
    references
        A view's references. Empty if the caller may not read every one.
    hidden_references
        How many of the view's references the caller may not read; when
        non-zero, `sql` and `references` are withheld.
    snapshot_id
        The table's current snapshot id; `None` without one, for a view or an
        external table, and from an older server.
    state
        ``not_published`` for a table whose first build did not finish, or failed a blocking
        check, and that no later run has published; ``kept`` for a published table that keeps
        the data that failed a ``keep`` check; ``published`` otherwise, and from a server older
        than checks.
    state_message
        Why it is not published or is kept, naming the check and the run; `None` otherwise.
    description
        The table's description: the edit when it has one, else the asset header's text; `None`
        when there is none, and from a server older than descriptions.
    description_source
        ``project`` or ``edited``; `None` without a description.
    last_write_at
        When the current snapshot was committed; `None` without one, for a view, an external
        table or an artifact, and from a server older than table health.
    row_count
        The rows of the current snapshot; `None` when the server does not know.
    size_bytes
        The size of its data files; `None` when the server does not know.
    file_count
        Its data files; `None` when the server does not know.
    expires_at
        When the table's retention makes it expire; `None` when it never does, and from a
        server older than retention.
    expires_soon
        Whether that is within the server's warning window (7 days by default); always
        `False` when the server's window is 0.
    """

    name: str
    namespace: str
    uuid: str
    metadata_location: str | None
    columns: list[Column] = field(default_factory=list)
    kind: str = "TABLE"
    sql: str | None = None
    references: list[TableReference] = field(default_factory=list)
    hidden_references: int = 0
    snapshot_id: int | None = None
    state: str = "published"
    state_message: str | None = None
    description: str | None = None
    description_source: str | None = None
    last_write_at: datetime | None = None
    row_count: int | None = None
    size_bytes: int | None = None
    file_count: int | None = None
    expires_at: datetime | None = None
    expires_soon: bool = False


@dataclass(frozen=True)
class TableRetention:
    """A managed table's retention, and when it expires.

    Parameters
    ----------
    name
        The table's name.
    namespace
        Dot-joined namespace path.
    uuid
        The table's id.
    source
        ``table`` when the table sets its own retention, ``environment`` when its
        environment's applies; `None` when it has none.
    days
        The days after its last activity it expires.
    on
        The day it expires on.
    never
        Whether the table says it never expires.
    set_by
        Who set the retention that applies; `None` when nobody did or that principal is gone.
    set_at
        When it was set.
    last_activity_at
        The later of the table's last change and its last recorded read.
    expires_at
        When it expires; `None` when it never does.
    kept
        Why a table that has a retention never expires: ``protected`` (a protected
        environment) or ``project_asset`` (a deployed project declares it); `None` otherwise.
    """

    name: str
    namespace: str
    uuid: str
    source: str | None
    days: int | None
    on: date | None
    never: bool
    set_by: str | None
    set_at: datetime | None
    last_activity_at: datetime
    expires_at: datetime | None
    kept: str | None


# jscpd:ignore-start
# The SDK depends on no other package, so these mirror the server's response models.
@dataclass(frozen=True)
class LastMaintenance:
    """The last maintenance run that changed a table.

    Parameters
    ----------
    run_id
        The query run that did it.
    finished_at
        When it ended.
    status
        ``complete``, or ``stopped`` when it ran past its time limit and stopped at a safe
        point; running maintenance again continues.
    snapshots_expired
        Snapshots it expired.
    files_deleted
        Orphaned files it deleted.
    bytes_deleted
        Their total size.
    tags_removed
        Tags of failed checks it removed.
    """

    run_id: str
    finished_at: datetime
    status: str
    snapshots_expired: int
    files_deleted: int
    bytes_deleted: int
    tags_removed: int


@dataclass(frozen=True)
class CheckHealth:
    """The newest result of one check of a table.

    Parameters
    ----------
    name
        ``<table>/<check>``.
    check
        The check's short name.
    run_id
        The run the result belongs to.
    outcome
        ``passed``, ``failed``, ``warned`` or ``errored``.
    on_failure
        ``block``, ``keep`` or ``warn``.
    failing_rows
        The count of rows that are wrong, when the check ran.
    error
        Why the check could not run.
    rollback_outcome
        What the rollback after a failed blocking check did, if there was one.
    kept
        The check failed or errored and its ``keep`` setting left the data in the table.
    not_published
        The rollback left the table not published.
    finished_at
        When the check ended.
    generated
        Whether the check was generated from the asset's header (``key:``, ``references:``)
        rather than written under ``checks/``; ``False`` from an older server.
    """

    name: str
    check: str
    run_id: str
    outcome: str
    on_failure: str
    failing_rows: int | None
    error: str | None
    rollback_outcome: str | None
    kept: bool
    not_published: bool
    finished_at: datetime
    generated: bool = False


@dataclass(frozen=True)
class Reader:
    """A principal's use of a table over the window.

    Parameters
    ----------
    principal
        The principal's name.
    read_count
        How many of its succeeded queries named the table.
    last_read_at
        When the last of them completed.
    """

    principal: str
    read_count: int
    last_read_at: datetime


# jscpd:ignore-end


@dataclass(frozen=True)
class TableHealth:
    """A table's health at a glance.

    Parameters
    ----------
    days
        The window `readers` covers.
    last_maintenance
        The last maintenance that changed the table; `None` if there is none.
    checks
        The newest result of each check of the table.
    readers
        Who read the table in the window, most reads first; `None` when the caller may not see
        the table's grants.
    """

    days: int
    last_maintenance: LastMaintenance | None
    checks: list[CheckHealth]
    readers: list[Reader] | None


@dataclass(frozen=True)
class DroppedTable:
    """A dropped table whose files are not deleted yet.

    Parameters
    ----------
    name
        The table's name when it was dropped.
    namespace
        Dot-joined namespace path it was dropped from.
    uuid
        The table's id, which a restore keeps.
    kind
        ``TABLE``: only managed tables can be restored.
    dropped_at
        When it was dropped.
    purge_after
        From when on the server's sweep deletes its files.
    restorable
        Whether `Tables.undrop` would still restore it: `False` once `purge_after` has passed
        or the deletion has started.
    """

    name: str
    namespace: str
    uuid: str
    kind: str
    dropped_at: datetime
    purge_after: datetime
    restorable: bool


@dataclass(frozen=True)
class ExternalTable:
    """A registered raw-zone table backed by files already on storage.

    Parameters
    ----------
    name
        Table name.
    namespace
        Dot-joined namespace path.
    location_pattern
        The glob it was registered with.
    format
        Its file format.
    schema
        The inferred column-name-to-type mapping.
    """

    name: str
    namespace: str
    location_pattern: str
    format: str
    schema: dict[str, str]


@dataclass(frozen=True)
class AppendResult:
    """The outcome of a governed Arrow append.

    Parameters
    ----------
    rows
        Number of rows appended.
    files
        Number of data files this append wrote. `0` when `rows` is `0`.
    snapshot_id
        The id of the snapshot this append committed, or `None` when `rows`
        is `0`.
    """

    rows: int
    files: int
    snapshot_id: int | None


@dataclass(frozen=True)
class LoadResult:
    """The outcome of a load into the bronze layer (`Tables.load`).

    Parameters
    ----------
    snapshot_id
        The new snapshot of the table, or `None` when nothing was committed (a dry run, or no
        rows).
    rows
        How many rows the load wrote (`0` after a dry run).
    created
        `True` when the table did not exist before; after a dry run, when the load would
        create it.
    dry_run
        `True` when only the checks ran and nothing was written.
    """

    snapshot_id: int | None
    rows: int
    created: bool
    dry_run: bool


@dataclass(frozen=True)
class MergeResult:
    """The outcome of a governed Arrow merge.

    Parameters
    ----------
    snapshot_id
        The id of the snapshot the merge committed, or `None` when it changed nothing (no
        rows, or every row equal to the table's).
    rows_inserted
        Rows whose key the table did not have.
    rows_updated
        Rows whose key matched and whose values changed.
    """

    snapshot_id: int | None
    rows_inserted: int
    rows_updated: int


@dataclass(frozen=True)
class WriteResult:
    """The outcome of a governed Arrow replace.

    Parameters
    ----------
    rows
        Rows written.
    files
        Data files written.
    snapshot_id
        The snapshot committed.
    schema_diff
        How the table's schema changed — ``{"added", "dropped", "renamed",
        "retyped"}`` — or `None` when it didn't.
    """

    rows: int
    files: int
    snapshot_id: int | None
    schema_diff: dict[str, list[dict[str, str]]] | None = None


@dataclass(frozen=True)
class LineageNode:
    """One node of a lineage graph.

    Parameters
    ----------
    id
        The table's id, or the connection's.
    name
        Its dotted name, or the connection's name.
    kind
        ``TABLE``, ``EXTERNAL``, ``VIEW``, ``ARTIFACT`` or ``CONNECTION``; `None` once a
        table is dropped. A ``CONNECTION`` is a connection the writing asset declared.
    dropped
        Whether the table or connection no longer exists.
    connection_kind
        For a ``CONNECTION``, its kind (for example ``postgresql``); `None` otherwise, once
        the connection is deleted, and from a server that predates the field.
    """

    id: str
    name: str
    kind: str | None
    dropped: bool
    connection_kind: str | None = None


@dataclass(frozen=True)
class LineageEdge:
    """One edge of a lineage graph.

    Parameters
    ----------
    source_id
        The source table's id.
    target_id
        The target table's id.
    source_snapshot_id
        The source snapshot read, if known.
    target_snapshot_id
        The target snapshot committed, if any.
    query_run_id
        The run that wrote it, if any.
    created_at
        When it was written.
    """

    source_id: str
    target_id: str
    source_snapshot_id: int | None
    target_snapshot_id: int | None
    query_run_id: str | None
    created_at: datetime


@dataclass(frozen=True)
class Lineage:
    """A table's lineage graph.

    Parameters
    ----------
    root_id
        The table asked about.
    direction
        `upstream` or `downstream`.
    depth
        The hops walked.
    nodes
        Every node the caller may read.
    edges
        One edge per source/target pair.
    hidden_nodes
        Nodes omitted because the caller can't read them.
    truncated
        Whether the walk stopped at the server's node cap with a readable
        node left out. `False` from a server that predates the field.
    """

    root_id: str
    direction: str
    depth: int
    nodes: list[LineageNode]
    edges: list[LineageEdge]
    hidden_nodes: int
    truncated: bool


@dataclass(frozen=True)
class WaitingFor:
    """Why a queued query or a waiting task has not started.

    Computed by the server when asked and never stored. The code is a stable string; a newer
    server may send a code this SDK does not know, so treat it as opaque and show `message`.

    Parameters
    ----------
    code
        `no_worker` (no worker is running), `starting` (a worker is free, the work is about to
        be claimed), `workers_busy` (workers are running that take this kind of work and none
        is free), `no_worker_of_kind` (workers are running and none takes this kind of work),
        `user_limit` (the submitter already runs as many ad-hoc queries as the limit per user
        allows), `lock_timeout` (a worker waited too long for the lock that serialises the
        start of the submitter's queries, so workers pass them by for a few seconds),
        `upstream` (a task waits for upstream tasks of its run), `retry` (a task
        failed and its next attempt is in the future), `job_waits` (a start of a job waits for
        the active run of its project) or `no_run_principal` (a job has a schedule and no run
        principal is bound, so it does not start by itself).
    message
        One plain sentence that names the cause and, where there is one, the next step.
    workers
        Workers that are running and take this kind of work, counted over the whole instance
        (0 for `no_worker` and `no_worker_of_kind`); for those two, `starting` and
        `workers_busy`.
    ahead
        Queued queries and deploys, of every environment, that the workers for this kind take
        before this query (the other kind too while a worker takes both); for a query's
        `starting` and `workers_busy`. A count, not a promise.
    upstream
        The first 50 upstream tasks of the same run that have not succeeded (raw names); for
        `upstream`.
    upstream_count
        How many upstream tasks have not succeeded in all; for `upstream`.
    attempt
        Attempts made so far; for `retry`.
    max_attempts
        Attempts allowed; for `retry`.
    next_attempt_at
        When the next attempt may start (timezone-aware, UTC); for `retry`. For `lock_timeout`,
        until when the submitter's queries are passed by.
    other_workers
        Workers that are running although none takes this kind of work (they take the other
        kind only); for `no_worker_of_kind`. `None` from a server that predates the field.
    running
        The caller's own ad-hoc queries that are running; for `user_limit`. `None` when the
        caller is not that principal (a manager reading another user's row), and from a server
        that predates the field.
    limit
        The limit of running ad-hoc queries per user; for `user_limit`. `None` from a server
        that predates the field.
    """

    code: str
    message: str
    workers: int | None = None
    ahead: int | None = None
    upstream: tuple[str, ...] | None = None
    upstream_count: int | None = None
    attempt: int | None = None
    max_attempts: int | None = None
    next_attempt_at: datetime | None = None
    other_workers: int | None = None
    running: int | None = None
    limit: int | None = None


@dataclass(frozen=True)
class Failure:
    """Why a task or an attempt failed: a stable code, the cause and the next step.

    The code is a stable string; the sentences may be reworded. A newer server may send a code
    this SDK does not know, so treat it as opaque and show `cause` and `next_step`. The codes are
    listed in the reference page "Why did it fail?".

    Parameters
    ----------
    code
        `build_failed`, `code_changed`, `secret_refused`, `no_runtime`, `upstream_failed`,
        `upstream_missing`, `check_blocked`, `check_error`, `query_invalid`, `permission_denied`,
        `not_found`, `memory`, `timeout`, `cancelled`, `worker_lost`, `conflict` or `internal`;
        `unknown` for a failure recorded before the codes existed.
    cause
        One sentence: what happened.
    next_step
        One sentence: what to do about it.
    """

    code: str
    cause: str
    next_step: str


@dataclass(frozen=True)
class QueryListEntry:
    """One query in the list of an environment's queries: never its SQL or its error message.

    The error message and the result are read through the query's own handle
    (`client.queries.get`); only its submitter can.

    Parameters
    ----------
    id
        The query's id.
    submitter
        The submitting principal's name; `None` when that principal was deleted.
    kind
        `read`, or the write mode: `create`, `replace`, `append`, `view` or `maintain`.
    target
        A write run's target table, `None` for a read.
    status
        `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED` or `CANCELLED`.
    created_at
        When it was submitted.
    started_at
        When a worker started it.
    finished_at
        When it ended.
    error_cause
        Why a `FAILED` or `CANCELLED` query ended, as `QueryStatus.error_cause`; the message is
        only on the status.
    waiting_for
        Why a `QUEUED` query has not started; `None` for any other status.
    """

    id: str
    submitter: str | None
    kind: str
    target: str | None
    status: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_cause: str | None
    waiting_for: WaitingFor | None


@dataclass(frozen=True)
class QueryStatus:
    """A query run's current status.

    Parameters
    ----------
    id
        The query's id.
    status
        `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED` or `CANCELLED`.
    error_message
        Set when `status` is `FAILED` or `CANCELLED`.
    lineage
        Column lineage, set only when `status` is `SUCCEEDED`.
    write_mode
        A write run's mode, `None` for a read.
    target
        A write run's target, as submitted.
    rows_written
        Rows a write run wrote.
    snapshot_id
        The snapshot a write run committed, if any.
    watermark
        The type-tagged watermark an incremental append filtered on.
    schema_diff
        How a write changed its target's schema, if it did.
    maintenance
        A maintenance run's `{"request", "report"}`. `report["conflict"]` is
        `True` when the table changed during maintenance and nothing was
        applied (retry); `report["note"]` says in words why part of the run
        was skipped, if it was.
    error_cause
        Why a `FAILED` or `CANCELLED` run ended: `not_found`, `permission_denied`,
        `result_too_large`, `invalid_query`, `out_of_memory`, `timeout`, `cancelled`, `worker_lost`
        or `internal`.
        `None` when the run did not end that way, when the server predates the field and
        for a failure recorded without one.
    waiting_for
        Why a `QUEUED` query has not started; `None` for any other status and when the server
        predates the field.
    """

    id: str
    status: str
    error_message: str | None
    lineage: dict[str, list[dict[str, str]]] | None
    write_mode: str | None = None
    target: str | None = None
    rows_written: int | None = None
    snapshot_id: int | None = None
    watermark: str | None = None
    schema_diff: dict[str, list[dict[str, str]]] | None = None
    maintenance: dict | None = None
    error_cause: str | None = None
    waiting_for: WaitingFor | None = None


@dataclass(frozen=True)
class Principal:
    """A platform principal: a human user or a service account.

    Parameters
    ----------
    name
        Principal name.
    type
        `"USER"` or `"SERVICE_ACCOUNT"`.
    is_active
        Whether this principal can currently authenticate.
    """

    name: str
    type: str
    is_active: bool


@dataclass(frozen=True)
class Group:
    """A group of principals, grantable as a single unit.

    Parameters
    ----------
    name
        Group name.
    """

    name: str


@dataclass(frozen=True)
class IssuedToken:
    """A freshly issued PAT, shown here exactly once.

    Parameters
    ----------
    key_id
        The public lookup key.
    name
        The label given at issuance.
    token
        The full token value. `repr=False` so it never appears in a stray
        `repr()` or log line, the same rule `Client.__repr__` follows via
        `lakana._tokens.mask_token`.
    """

    key_id: str
    name: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class Token:
    """A previously issued PAT, as listed. Never carries a secret.

    Parameters
    ----------
    key_id
        The public lookup key.
    name
        The label given at issuance.
    last_used_at
        When this PAT last successfully authenticated a request, or `None`
        if never.
    """

    key_id: str
    name: str
    last_used_at: datetime | None


@dataclass(frozen=True)
class MintedTaskToken:
    """A freshly minted task token, shown here exactly once.

    Parameters
    ----------
    key_id
        The public lookup key.
    token
        The full token value. `repr=False` so it never appears in a stray
        `repr()` or log line.
    """

    key_id: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class Grant:
    """A grant of a role or a raw action to a principal or a group.

    Parameters
    ----------
    id
        The grant's id.
    principal_id
        The principal this grant applies to, or `None` if it applies to a
        group.
    group_id
        The group this grant applies to, or `None` if it applies to a
        principal.
    role
        The role template granted, or `None` if a raw action was granted.
    action
        The raw action granted, or `None` if a role was granted.
    kind
        Which tier of the securable hierarchy this grant applies to.
    environment
        The environment this grant applies to, if any.
    namespace
        The dot-joined namespace path this grant applies to, if any.
    table
        The table this grant applies to, if any.
    granted_by
        The id of the principal who created this grant.
    granted_at
        When this grant was created.
    note
        ``no effect: <ACTION> needs the ADMIN role`` for a single-action grant of
        `MANAGE_GRANT`, `MANAGE_GROUP` or `MANAGE_TOKEN`, which work only with the `ADMIN`
        role on the instance; `None` otherwise, and from a server that sends none.
    """

    id: str
    principal_id: str | None
    group_id: str | None
    role: str | None
    action: str | None
    kind: str
    environment: str | None
    namespace: str | None
    table: str | None
    granted_by: str
    granted_at: datetime
    note: str | None = None


@dataclass(frozen=True)
class GrantPage:
    """One page of grants, newest first.

    Parameters
    ----------
    items
        The grants on this page.
    next_cursor
        Pass back as ``cursor=`` to fetch the next page; ``None`` on the last page.
    """

    items: list[Grant]
    next_cursor: str | None


@dataclass(frozen=True)
class PrincipalPage:
    """One page of principals, newest first.

    Parameters
    ----------
    items
        The principals on this page.
    next_cursor
        Pass back as ``cursor=`` to fetch the next page; ``None`` on the last page.
    """

    items: list[Principal]
    next_cursor: str | None


@dataclass(frozen=True)
class GroupPage:
    """One page of groups, newest first.

    Parameters
    ----------
    items
        The groups on this page.
    next_cursor
        Pass back as ``cursor=`` to fetch the next page; ``None`` on the last page.
    """

    items: list[Group]
    next_cursor: str | None


# jscpd:ignore-start
# The SDK depends on no other package, so these mirror the server's response models.
@dataclass(frozen=True)
class Scope:
    """The object a permission or a grant is on.

    Parameters
    ----------
    kind
        ``INSTANCE``, ``ENVIRONMENT``, ``NAMESPACE`` or ``TABLE``.
    environment
        The environment, below the instance.
    namespace
        The dot-joined namespace, for a namespace or a table.
    table
        The table, for a table.
    """

    kind: str
    environment: str | None
    namespace: str | None
    table: str | None


@dataclass(frozen=True)
class PermissionVia:
    """One grant that gives a permission, and how it reaches the principal.

    Parameters
    ----------
    kind
        ``grant`` (a single action granted to the principal), ``role`` (a role granted to it)
        or ``group`` (either, granted to a group it is a member of).
    grant_id
        The grant's id, as `lakana grant revoke` takes it.
    group
        The group's name, for ``group``.
    role
        The role granted, or `None` for a single action.
    scope
        What the grant is on: the object asked about or an ancestor of it.
    """

    kind: str
    grant_id: str
    group: str | None
    role: str | None
    scope: Scope


@dataclass(frozen=True)
class Permission:
    """Whether a principal may do one action on the object, and why.

    Parameters
    ----------
    action
        The action.
    allowed
        The answer of the authorizer the server's routes use.
    via
        The grants that give it, oldest first; empty when refused.
    reason
        Why it is refused, or `None` when allowed.
    """

    action: str
    allowed: bool
    via: list[PermissionVia]
    reason: str | None


@dataclass(frozen=True)
class Permissions:
    """A principal's effective permissions on one object.

    Parameters
    ----------
    principal
        The principal's name.
    active
        Whether it may sign in.
    scope
        The object asked about.
    permissions
        One per action the server asks for on an object of this kind.
    note
        Set for a deactivated principal: its tokens no longer work, whatever its grants allow.
    """

    principal: str
    active: bool
    scope: Scope
    permissions: list[Permission]
    note: str | None


# jscpd:ignore-end


@dataclass(frozen=True)
class RowFilter:
    """A row filter restricting which rows a principal or group may read.

    Parameters
    ----------
    id
        The row filter's id.
    principal_id
        The principal this filter applies to, or `None` if it applies to a
        group.
    group_id
        The group this filter applies to, or `None` if it applies to a
        principal.
    environment
        The environment this filter applies to.
    namespace
        The dot-joined namespace path this filter applies to.
    table
        The table this filter applies to.
    expression
        The restricted-subset predicate.
    created_by
        The id of the principal who created this filter.
    created_at
        When this filter was created.
    """

    id: str
    principal_id: str | None
    group_id: str | None
    environment: str
    namespace: str
    table: str
    expression: str
    created_by: str
    created_at: datetime


@dataclass(frozen=True)
class ColumnMask:
    """A column mask replacing a column's value for a principal or group.

    Parameters
    ----------
    id
        The column mask's id.
    principal_id
        The principal this mask applies to, or `None` if it applies to a
        group.
    group_id
        The group this mask applies to, or `None` if it applies to a
        principal.
    environment
        The environment this mask applies to.
    namespace
        The dot-joined namespace path this mask applies to.
    table
        The table this mask applies to.
    column
        The masked column's name.
    expression
        The replacement value.
    created_by
        The id of the principal who created this mask.
    created_at
        When this mask was created.
    """

    id: str
    principal_id: str | None
    group_id: str | None
    environment: str
    namespace: str
    table: str
    column: str
    expression: str
    created_by: str
    created_at: datetime


@dataclass(frozen=True)
class SecretCreate:
    """A secret-creation request, held only long enough to build the request body.

    Parameters
    ----------
    name
        The secret's name.
    values
        The plaintext key-value bag to encrypt and store. `repr=False` so a
        `SecretCreate` never leaks its plaintext through a stray `repr()`
        or log line, the same rule an issued token's own `token` field
        follows.
    """

    name: str
    values: dict[str, str] = field(repr=False)


@dataclass(frozen=True)
class Secret:
    """A secret's metadata. Structurally cannot carry a value or ciphertext.

    Parameters
    ----------
    id
        The secret's id.
    name
        The secret's name.
    environment
        The environment this secret belongs to.
    created_by
        The id of the principal who created this secret.
    created_at
        When this secret was created.
    format_version
        How the secret is stored. 1 is the original format; set the secret
        again (`lakana secret rotate`) to store it in the current one.
    key_id
        Which key provider and key wrapped the secret's data key.
    use
        What the secret is bound to (``package_index``, ``repository`` or ``environment``);
        `None` for an unbound secret.
    use_target
        The bound index's or repository's name, when there is one.
    use_target_url
        Its URL, which is where the secret is sent, when there is one (for a connection,
        every ``host:port`` it sends the secret to, joined by ``, ``).
    """

    id: str
    name: str
    environment: str
    created_by: str
    created_at: datetime
    format_version: int
    key_id: str
    use: str | None = None
    use_target: str | None = None
    use_target_url: str | None = None


@dataclass(frozen=True)
class AuditEvent:
    """One recorded audit event.

    Parameters
    ----------
    id
        The event's id.
    occurred_at
        When the event occurred.
    principal_id
        The principal attributed to this event, or `None` for a failed
        authentication.
    event_type
        The kind of event.
    securable_kind
        Which tier of the securable hierarchy this event's target belongs
        to, if any.
    securable_environment
        The environment this event's target belongs to, if any.
    securable_namespace
        The dot-joined namespace path this event's target belongs to, if
        any.
    securable_table
        The table this event's target refers to, if any.
    detail
        Event-specific facts not already captured by the fields above.
    """

    id: str
    occurred_at: datetime
    principal_id: str | None
    event_type: str
    securable_kind: str | None
    securable_environment: str | None
    securable_namespace: str | None
    securable_table: str | None
    detail: dict


@dataclass(frozen=True)
class AuditEventPage:
    """One page of audit events, newest first.

    Parameters
    ----------
    items
        The events on this page.
    next_cursor
        Pass back as `cursor=` to fetch the next page; `None` when this is
        the last page.
    """

    items: list[AuditEvent]
    next_cursor: str | None


@dataclass(frozen=True)
class Repository:
    """A registered git repository. Carries a secret's name, never a value.

    Parameters
    ----------
    id
        The repository's id.
    name
        Its name.
    kind
        ``local``, ``https`` or ``ssh``.
    url
        Its URL or local path.
    credential_secret
        The bound secret's name, resolved per environment.
    credential_username
        HTTPS username, or `None` for ``oauth2``.
    ssh_known_hosts
        Trusted SSH host keys.
    default_ref
        Ref deployed when none is given.
    created_by
        Who registered it.
    created_at
        When.
    updated_at
        Last change.
    """

    id: str
    name: str
    kind: str
    url: str
    credential_secret: str | None
    credential_username: str | None
    ssh_known_hosts: str | None
    default_ref: str | None
    created_by: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class PackageIndex:
    """A registered package index. Carries a secret's name, never a value.

    Parameters
    ----------
    id
        The index's id.
    name
        Its name.
    url
        Its URL, in stored form.
    credential_secret
        The bound secret's name, resolved per environment.
    created_by
        Who registered it.
    created_at
        When.
    updated_at
        Last change.
    """

    id: str
    name: str
    url: str
    credential_secret: str | None
    created_by: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class DeclaringAsset:
    """An asset whose deployed version declares a connection.

    Parameters
    ----------
    repository
        The repository's name.
    asset
        The asset's name.
    """

    repository: str
    asset: str


@dataclass(frozen=True)
class Connection:
    """A connection of an environment. Carries its secret's name, never a value.

    Parameters
    ----------
    name
        Its name.
    kind
        Its kind: ``postgresql``, ``mariadb``, ``sqlite-file``, ``http``, ``s3``, ``gcs``,
        ``azure``, ``directory`` or ``tcp``.
    address
        Where the source is, with defaults filled in.
    settings
        How to reach it, with defaults filled in; a CA certificate as its SHA-256
        fingerprints.
    secret
        The name of the secret it uses, or `None`.
    declared_by
        The assets whose deployed version declares it.
    created_by
        The name of the principal who created it.
    created_at
        When it was created.
    updated_at
        When it was last changed.
    """

    name: str
    kind: str
    address: dict
    settings: dict
    secret: str | None
    declared_by: list[DeclaringAsset]
    created_by: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ConnectionTest:
    """A test of a connection: queued, taken by a worker, then done with an outcome.

    Parameters
    ----------
    id
        The test's id.
    state
        ``queued``, ``running`` or ``done``.
    outcome
        Once done: ``ok``, ``dns``, ``refused``, ``timeout``, ``tls``, ``not_allowed``,
        ``root_unavailable``, ``not_found``, ``credentials`` or ``failed``; `None` before.
    message
        What the test found, redacted; `None` before it is done.
    created_at
        When it was requested.
    finished_at
        When it was done; `None` before.
    """

    id: str
    state: str
    outcome: str | None
    message: str | None
    created_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class ConnectionPage:
    """One page of an environment's connections, newest first.

    Parameters
    ----------
    items
        The connections of this page.
    next_cursor
        Pass back as `cursor=` to fetch the next page; `None` on the last page.
    """

    items: list[Connection]
    next_cursor: str | None


@dataclass(frozen=True)
class DeployAccepted:
    """A queued deploy run.

    Parameters
    ----------
    id
        The run's id.
    status
        ``queued``.
    """

    id: str
    status: str


@dataclass(frozen=True)
class DeployJobStart:
    """What a deploy did with one job that starts after each deploy (`on_deploy: true`).

    Parameters
    ----------
    job
        The job's name.
    outcome
        ``pending`` (the worker has not started it yet), ``started``, ``waiting`` (the project
        was busy; the start waits for the active run) or ``not_started``.
    run_id
        The queued run when `outcome` is ``started``; follow it with `client.runs.wait`.
    waits_for_run_id
        The run of the project the start waits behind when `outcome` is ``waiting``.
    reason
        Why a ``not_started`` job did not start: ``lacks_run`` (you may not `RUN` in the
        environment), ``refused`` (a start by hand would be refused too), ``error`` (the
        worker's log has it) or ``lost`` (the worker stopped before it recorded the start;
        the job may have run).
    message
        What to do about a ``not_started`` job, as one sentence for a person.
    """

    job: str
    outcome: str
    run_id: str | None = None
    waits_for_run_id: str | None = None
    reason: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class Deploy:
    """A deploy, dry run or rollback and its outcome.

    Parameters
    ----------
    id
        The run's id.
    environment
        Environment name.
    repository
        Repository name.
    kind
        ``deploy``, ``dry_run`` or ``rollback``.
    status
        ``queued``, ``running``, ``succeeded`` or ``failed``.
    requested_ref
        Ref or SHA asked for.
    resolved_sha
        Commit fetched.
    rollback_to_project_version_id
        A rollback's target.
    error_message
        Why it failed.
    diff
        Table names per change category.
    project_version_id
        The project version made current.
    principal_id
        Who submitted it.
    created_at
        Queued at.
    started_at
        Claimed at.
    finished_at
        Finished at.
    hidden_adopted
        Adopted tables left out of `diff` because you may not read them.
    checks
        The checks the project has, table to ``[{"check", "on_failure"}]``; `None` for a run
        without checks and from a server that predates checks.
    warnings
        What the deploy leaves for you to act on, as sentences; empty from a server that
        predates them.
    waiting_for
        Why a queued deploy has not started (the codes `no_worker`, `no_worker_of_kind`,
        `workers_busy` and `starting`); `None` when it is not waiting, in a listing, and from
        a server that predates the field.
    job_starts
        The jobs with `on_deploy: true` the deploy started, by name; empty for a run that
        starts no job and from a server that predates the field.
    """

    id: str
    environment: str
    repository: str
    kind: str
    status: str
    requested_ref: str | None
    resolved_sha: str | None
    rollback_to_project_version_id: str | None
    error_message: str | None
    diff: dict[str, list[str]] | None
    project_version_id: str | None
    principal_id: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    hidden_adopted: int = 0
    checks: dict[str, list[dict[str, str]]] | None = None
    warnings: tuple[str, ...] = ()
    waiting_for: WaitingFor | None = None
    job_starts: tuple[DeployJobStart, ...] = ()


@dataclass(frozen=True)
class Deployment:
    """The current project version of one repository in an environment.

    Parameters
    ----------
    repository
        Repository name.
    project_version_id
        Current project version.
    commit_sha
        Its commit.
    updated_at
        When the pointer moved.
    run_as
        The bound run principal, if any.
    """

    repository: str
    project_version_id: str
    commit_sha: str
    updated_at: datetime
    run_as: str | None = None


@dataclass(frozen=True)
class ProjectVersion:
    """An immutable, validated commit for one environment.

    Parameters
    ----------
    id
        The project version's id.
    environment
        Environment name.
    repository
        Repository name.
    commit_sha
        The commit.
    code_uri
        Code tarball location.
    code_sha256
        Its digest.
    deployed_by
        Who first deployed it.
    created_at
        When.
    manifest
        The (caller-filtered) manifest.
    """

    id: str
    environment: str
    repository: str
    commit_sha: str
    code_uri: str
    code_sha256: str
    deployed_by: str | None
    created_at: datetime
    manifest: dict[str, Any]


@dataclass(frozen=True)
class Orphan:
    """A table an earlier project version produced and the current one does not.

    Parameters
    ----------
    repository
        The repository that used to produce it.
    table
        Dotted table name.
    """

    repository: str
    table: str


@dataclass(frozen=True)
class Orphans:
    """Orphans visible to the caller.

    Parameters
    ----------
    orphans
        Readable orphans.
    hidden
        Orphans the caller may not read.
    """

    orphans: list[Orphan]
    hidden: int


@dataclass(frozen=True)
class CheckResult:
    """The result of one data quality check. It holds a count, never a row of the table.

    Parameters
    ----------
    run_id
        The run.
    task
        The task (the checked table) it belongs to.
    name
        ``<table>/<check>``.
    check
        The check's short name.
    path
        Repository-relative path of the check's file.
    table
        The checked table, dotted.
    table_id
        The table's id, or ``None`` when it was not resolved.
    on_failure
        ``block`` or ``warn``.
    outcome
        ``passed``, ``failed``, ``warned`` or ``errored``.
    failing_rows
        The count of rows that are wrong, when the check ran.
    error
        Why a check could not run.
    checked_snapshot_id
        The snapshot it read.
    rollback_outcome
        What the rollback after a failed blocking check did, if there was one: ``rolled_back``,
        ``unpublished``, ``nothing_written``, ``not_a_table``, ``changed_since`` or ``failed``.
    policy_applied
        Whether a row filter or column mask applied to the run's principal.
    policy_note
        Text saying what that means for the result, or ``None``.
    query_run_id
        The query that ran the check.
    started_at
        Start.
    finished_at
        End.
    generated
        Whether the check was generated from the asset's header (``key:``, ``references:``)
        rather than written under ``checks/``; ``False`` from an older server.
    """

    run_id: str
    task: str
    name: str
    check: str
    path: str
    table: str
    table_id: str | None
    on_failure: str
    outcome: str
    failing_rows: int | None
    error: str | None
    checked_snapshot_id: int | None
    rollback_outcome: str | None
    policy_applied: bool
    policy_note: str | None
    query_run_id: str | None
    started_at: datetime
    finished_at: datetime
    generated: bool = False


@dataclass(frozen=True)
class CheckResultPage:
    """One page of check results, newest first.

    Parameters
    ----------
    items
        The results on this page.
    next_cursor
        Pass back as ``cursor=`` to fetch the next page; ``None`` on the last page.
    """

    items: list[CheckResult]
    next_cursor: str | None


@dataclass(frozen=True)
class RunPage:
    """One page of runs, newest first.

    Parameters
    ----------
    items
        The runs on this page, without tasks.
    next_cursor
        Pass back as ``cursor=`` to fetch the next page; ``None`` on the last page.
    """

    items: list[Run]
    next_cursor: str | None


@dataclass(frozen=True)
class QueryListPage:
    """One page of the queries of an environment.

    Parameters
    ----------
    items
        The entries on this page.
    next_cursor
        Pass back as ``cursor=`` to fetch the next page; ``None`` on the last page.
    """

    items: list[QueryListEntry]
    next_cursor: str | None


@dataclass(frozen=True)
class DeployPage:
    """One page of deploy runs, newest first.

    Parameters
    ----------
    items
        The deploy runs on this page.
    next_cursor
        Pass back as ``cursor=`` to fetch the next page; ``None`` on the last page.
    """

    items: list[Deploy]
    next_cursor: str | None


@dataclass(frozen=True)
class RunTask:
    """One task run of a run.

    Parameters
    ----------
    asset
        Dotted table name.
    status
        ``pending``, ``ready``, ``running``, ``succeeded``, ``failed``,
        ``upstream_failed`` or ``cancelled``.
    attempt
        Attempts counted.
    max_attempts
        Retries + 1.
    timeout_seconds
        Per-attempt limit.
    next_attempt_at
        When a pending retry may start.
    started_at
        First claim.
    finished_at
        Final state.
    error_message
        Last failure.
    query_run_id
        The write that executed the last attempt.
    rows_written
        Rows it wrote.
    waiting_for
        Why a ``pending`` or ``ready`` task of a running run has not started; ``None`` for any
        other task and when the server predates the field.
    kind
        ``asset`` for a table the run builds, ``source_check`` for a table it only checks.
    checks
        The task's check results, by name; empty in a listing and for a server that predates
        checks.
    kept_check
        Whether this ``source_check`` task checks a table the project builds, because that table
        was kept after a failed check, rather than a table the project only reads; ``False`` for
        every other task and for a server that predates the field; `None` for a ``source_check``
        task of a run whose project version is gone, which cannot tell.
    setting_back
        Whether the task has not ended and its next claim only sets its table back: its worker
        was lost after it may have written the table, or its run was cancelled while it waited
        with rows no check had passed. ``False`` once the task has ended, and for a server that
        predates the field.
    failure
        Why the task failed, was cancelled or never ran (`Failure`); `None` for a task with no
        error, and for a server that predates the field. `error_message` is its detail.
    """

    asset: str
    status: str
    attempt: int
    max_attempts: int
    timeout_seconds: int
    next_attempt_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None
    query_run_id: str | None
    rows_written: int | None
    waiting_for: WaitingFor | None = None
    kind: str = "asset"
    checks: tuple[CheckResult, ...] = ()
    kept_check: bool | None = False
    setting_back: bool = False
    failure: Failure | None = None


@dataclass(frozen=True)
class Notification:
    """One notification of a run: the message to a job's webhook and how its delivery went.

    Parameters
    ----------
    id
        The delivery id, the same as the ``delivery_id`` of the message the receiver got.
    event
        ``failed``, ``recovered`` or ``test``.
    state
        ``pending`` (waiting for a first or another attempt), ``delivered`` or ``failed``.
    attempts
        Attempts made so far.
    max_attempts
        Attempts before the delivery counts as failed.
    last_result
        A short text on the last attempt, such as ``the server answered 404``; never the URL.
        `None` before the first attempt.
    delivered_at
        When it was delivered; `None` until then.
    """

    id: str
    event: str
    state: str
    attempts: int
    max_attempts: int
    last_result: str | None
    delivered_at: datetime | None


@dataclass(frozen=True)
class Delivery(Notification):
    """One notification delivery on its own, such as the test of a webhook.

    Parameters
    ----------
    job
        The job.
    repository
        Repository name.
    next_attempt_at
        When the next attempt is due; `None` once the delivery is over.
    """

    job: str
    repository: str
    next_attempt_at: datetime | None


@dataclass(frozen=True)
class Run:
    """A run of a deployed project version.

    Parameters
    ----------
    id
        Run id.
    environment
        Environment name.
    repository
        Repository name.
    project_version_id
        The project version it runs.
    commit_sha
        Its commit.
    kind
        ``full``, ``selection`` or ``resume``.
    status
        ``queued``, ``running``, ``succeeded``, ``failed`` or ``cancelled``.
    cancel_requested
        Whether a cancel was requested.
    triggered_by
        Who started it.
    run_as
        Who it writes as.
    resumed_from_run_id
        The run a resume continues.
    selection
        Patterns and flags.
    error_message
        Run-level failure.
    created_at
        Created.
    started_at
        Started.
    finished_at
        Finished.
    tasks
        Task runs (empty in listings).
    job
        The job the run belongs to; `None` for an ad-hoc run (and from a server that predates
        jobs).
    trigger
        ``schedule``, ``api`` (a service account called the API), ``manual`` or ``deploy`` (a
        deploy started the job as its deployer); `None` for an ad-hoc run.
    scheduled_for
        For a scheduled run, the time the schedule named.
    missed
        How many earlier scheduled times this run stands in for.
    notifications
        The notifications the run queued, oldest first (empty in listings, and from a server
        that predates them).
    failed_tasks
        How many tasks ended ``failed`` (not ``upstream_failed`` or ``cancelled``), also in
        listings; 0 from a server that predates the field.
    """

    id: str
    environment: str
    repository: str
    project_version_id: str
    commit_sha: str
    kind: str
    status: str
    cancel_requested: bool
    triggered_by: str | None
    run_as: str | None
    resumed_from_run_id: str | None
    selection: dict[str, Any]
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    tasks: list[RunTask]
    job: str | None = None
    trigger: str | None = None
    scheduled_for: datetime | None = None
    missed: int = 0
    notifications: list[Notification] = field(default_factory=list)
    failed_tasks: int = 0


@dataclass(frozen=True)
class Trigger:
    """One way a job starts.

    Parameters
    ----------
    kind
        ``schedule`` (the job starts at the times a cron expression names), ``deploy`` (it
        starts after each deploy that changes the project version) or ``on_demand`` (a start
        through the API, the SDK or the CLI, which every job has). A newer server may add
        kinds; the SDK leaves out one it does not know.
    cron
        For ``schedule``, the cron expression; `None` otherwise.
    timezone
        For ``schedule``, the IANA zone; `None` otherwise.
    paused
        For ``schedule``, whether it is paused; `None` otherwise.
    next_run_at
        For ``schedule``, the next scheduled time (UTC); `None` when paused or for another kind.
    """

    kind: str
    cron: str | None = None
    timezone: str | None = None
    paused: bool | None = None
    next_run_at: datetime | None = None


@dataclass(frozen=True)
class Job:
    """A job of a deployed project and the platform's state for it.

    Parameters
    ----------
    name
        The job's name.
    environment
        Environment name.
    repository
        Repository name.
    select
        The selection's patterns.
    upstream
        Whether ancestors are added.
    downstream
        Whether descendants are added.
    cron
        The cron expression, or `None` for a job without a schedule here.
    timezone
        The schedule's IANA zone, or `None` without a schedule.
    state
        ``scheduled``, ``paused``, ``waiting`` (a start waits for the active run),
        ``waiting to start`` (a start waits and the project is free), ``no schedule``,
        ``no run principal`` or ``run principal unusable``. A paused job with a waiting start
        shows ``waiting``; `paused` says it is paused.
    paused
        Whether the schedule is paused.
    next_run_at
        The next scheduled time (UTC), or `None`.
    next_run_local
        The same moment in the job's zone, ISO 8601 with the offset.
    waiting_for
        Why a start waits (`job_waits`, `job_starts_soon`) or why the schedule cannot start
        (`no_run_principal`, `run_principal_unusable`); `None` otherwise.
    waits_for_run_id
        For ``waiting``, the run it waits for.
    last_run_id
        The job's latest run, if any.
    last_run_status
        Its status.
    last_run_trigger
        What started it.
    last_run_at
        When it was created.
    triggers
        How the job starts, schedule first; empty from a server that does not send them.
    """

    name: str
    environment: str
    repository: str
    select: tuple[str, ...]
    upstream: bool
    downstream: bool
    cron: str | None
    timezone: str | None
    state: str
    paused: bool
    next_run_at: datetime | None
    next_run_local: str | None
    waiting_for: WaitingFor | None
    waits_for_run_id: str | None
    last_run_id: str | None
    last_run_status: str | None
    last_run_trigger: str | None
    last_run_at: datetime | None
    triggers: tuple[Trigger, ...] = ()


@dataclass(frozen=True)
class WaitingStart:
    """A start of a job that waits for the active run of its project.

    Parameters
    ----------
    job
        The job.
    waits_for_run_id
        The active run it waits for.
    trigger
        ``schedule``, ``api``, ``manual`` or ``deploy``.
    requested_at
        When it was requested.
    requested_by
        Who asked, if that principal still exists; `None` for a scheduled start.
    scheduled_for
        For a scheduled start, the time the schedule named.
    missed
        How many earlier scheduled times the start stands in for.
    merged
        How many starts it stands for.
    message
        One sentence: what it waits for and that it starts then.
    """

    job: str
    waits_for_run_id: str
    trigger: str
    requested_at: datetime
    requested_by: str | None
    scheduled_for: datetime | None
    missed: int
    merged: int
    message: str


@dataclass(frozen=True)
class JobStart:
    """What a start of a job did.

    Parameters
    ----------
    run
        The queued run, or `None` when the start waits.
    waiting
        The waiting start, or `None` when a run was queued.
    """

    run: Run | None
    waiting: WaitingStart | None


@dataclass(frozen=True)
class TaskAttempt:
    """One attempt of a task run.

    Parameters
    ----------
    attempt
        Attempt number.
    worker
        Worker id.
    started_at
        Start.
    finished_at
        End.
    outcome
        How it ended.
    rows_written
        Rows written.
    error_message
        Why it failed.
    failure
        The code, cause and next step of an attempt that did not succeed (`Failure`); `None`
        for one that succeeded or is running, and for a server that predates the field.
    """

    attempt: int
    worker: str
    started_at: datetime
    finished_at: datetime | None
    outcome: str | None
    rows_written: int | None
    error_message: str | None
    failure: Failure | None = None


@dataclass(frozen=True)
class TaskLog:
    """A task's free-text log and attempts.

    Parameters
    ----------
    run_id
        The run.
    asset
        The task.
    text
        The free-text log.
    attempts
        Attempts, oldest first.
    next_offset
        The byte offset where `text` ends in the stored log; pass it as `offset` to read on.
    more
        Whether stored bytes remain after `next_offset` (one answer holds at most 1 MiB).
    complete
        Whether the task is finished and `text` reaches the end of its log.
    reset
        Whether the requested offset lay beyond the end of the log (it shrank); continue
        from `next_offset`.
    """

    run_id: str
    asset: str
    text: str
    attempts: list[TaskAttempt]
    next_offset: int
    more: bool
    complete: bool
    reset: bool


@dataclass(frozen=True)
class SchedulerStatus:
    """The scheduler role's holder and queue lengths.

    Parameters
    ----------
    holder
        Current holder.
    heartbeat_at
        Last heartbeat.
    active_runs
        Queued and running runs.
    pending
        Pending task runs.
    ready
        Ready task runs.
    running
        Running task runs.
    """

    holder: str | None
    heartbeat_at: datetime | None
    active_runs: int
    pending: int
    ready: int
    running: int


@dataclass(frozen=True)
class WorkerInfo:
    """A registered worker (admin).

    Parameters
    ----------
    id
        The worker's id.
    host
        Its host name.
    version
        The Lakana version it runs.
    started_at
        When it registered.
    heartbeat_at
        Its last heartbeat (the server's clock).
    status
        `alive` (the heartbeat is younger than the lease) or `lost` (the worker stopped; its
        row stays for an hour, and a run it held is ended).
    takes
        The work it takes: `all`, `interactive` (ad-hoc queries) or `pipeline` (deploys,
        pipeline tasks and table maintenance). `all` for a server of an earlier release, whose
        workers take everything; a value a newer server adds is kept as it is.
    query_run_id
        Its oldest running query, `None` when none.
    deploy_run_id
        Its oldest running deploy, `None` when none.
    task_run_id
        The running pipeline task whose lease it holds, `None` when none.
    caches
        The sizes of its caches, as it last measured them; `None` before its first measurement
        and from a server of an earlier release.
    """

    id: str
    host: str
    version: str
    started_at: datetime
    heartbeat_at: datetime
    status: str
    takes: str
    query_run_id: str | None
    deploy_run_id: str | None
    task_run_id: str | None
    caches: WorkerCaches | None = None


@dataclass(frozen=True)
class WorkerCaches:
    """The sizes of a worker's caches, as it last measured them (every sweep, hourly).

    Parameters
    ----------
    environments_bytes
        The Python environments of projects.
    packages_bytes
        uv's package download cache, without the files it shares with an environment.
    tasks_bytes
        The working directories of Python tasks.
    complete
        `False` when the measurement stopped at its time limit: the figures are a floor.
    measured_at
        When the worker stored them.
    """

    environments_bytes: int
    packages_bytes: int
    tasks_bytes: int
    complete: bool
    measured_at: datetime

    @property
    def total_bytes(self) -> int:
        """The three sizes together.

        Returns
        -------
        int
            Environments, package cache and task directories.
        """
        return self.environments_bytes + self.packages_bytes + self.tasks_bytes


@dataclass(frozen=True)
class ArtifactFile:
    """One file of an artifact version.

    Parameters
    ----------
    path
        Its path inside the artifact, ``/``-separated.
    size
        Its size in bytes.
    sha256
        The SHA-256 of its contents.
    """

    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ArtifactVersion:
    """One version of an artifact.

    Parameters
    ----------
    version
        The number. Numbers can have gaps (a number is never reused), so the next version is
        not necessarily this one plus one.
    manifest_sha256
        The digest of the canonical manifest: the identity of the content.
    size_bytes
        The summed size of its files.
    file_count
        The number of its files.
    is_directory
        Whether it is a directory (otherwise one file).
    task_run_id
        The task run that committed it, if one did.
    run_id
        That task run's run.
    project_version_id
        The project version the run executed.
    commit_sha
        The commit of that project version.
    created_by
        The name of the principal that committed it.
    created_at
        When it was committed, ISO 8601.
    deleted_at
        When it was deleted; `None` while it is live.
    deleted_by
        The name of the principal that deleted it.
    files
        Its files; empty for a deleted version.
    deleted_versions
        The numbers of the versions this commit pruned (`keep_versions`).
    files_sent
        After an upload: how many files it sent over the network. `None` for a version that
        was read rather than uploaded. Not part of equality.
    files_reused
        After an upload: how many files the store already had. `None` as for `files_sent`.
        Not part of equality.
    """

    version: int
    manifest_sha256: str
    size_bytes: int
    file_count: int
    is_directory: bool
    task_run_id: str | None
    run_id: str | None
    project_version_id: str | None
    commit_sha: str | None
    created_by: str | None
    created_at: str
    deleted_at: str | None
    deleted_by: str | None
    files: list[ArtifactFile] = field(default_factory=list)
    deleted_versions: list[int] = field(default_factory=list)
    files_sent: int | None = field(default=None, compare=False)
    files_reused: int | None = field(default=None, compare=False)


@dataclass(frozen=True)
class ArtifactKept:
    """Files a delete left in storage, for one reason.

    Parameters
    ----------
    reason
        ``other_version`` (a live version also has them), ``open_upload`` (an open upload of the
        artifact names them) or ``left_to_sweep`` (the delete did not remove them and Lakana's
        periodic clean-up decides). A client should treat a word it does not know like
        ``left_to_sweep``.
    files
        How many stored files; identical files of one version count once.
    bytes
        Their summed size.
    version
        For ``other_version``, the newest live version that has them, if you may read the
        artifact; `None` otherwise.
    """

    reason: str
    files: int
    bytes: int
    version: int | None


@dataclass(frozen=True)
class DeletedArtifactVersion(ArtifactVersion):
    """A deleted version, and what the delete did to its files.

    It is an `ArtifactVersion`, so code that only reads the version keeps working. The counts
    are `None` when the server is older than the release that reports them.

    Parameters
    ----------
    freed_files
        Stored files this delete removed from storage.
    freed_bytes
        Their summed size.
    kept_files
        Stored files that stayed in storage.
    kept_bytes
        Their summed size.
    kept
        The files that stayed, by reason; empty when none did.
    """

    freed_files: int | None = None
    freed_bytes: int | None = None
    kept_files: int | None = None
    kept_bytes: int | None = None
    kept: list[ArtifactKept] = field(default_factory=list)


@dataclass(frozen=True)
class Artifact:
    """One artifact, as a listing or a lookup shows it.

    Parameters
    ----------
    name
        The artifact's name.
    namespace
        Dot-joined namespace path.
    uuid
        The artifact's id.
    kind
        Always ``ARTIFACT``.
    current_version
        The highest live version; `None` while it has none.
    versions
        How many versions it has had, deleted ones included. Version numbers can have gaps, so
        this is not necessarily the highest number.
    size_bytes
        The summed size of its live versions.
    created_at
        When it was created, ISO 8601.
    """

    name: str
    namespace: str
    uuid: str
    kind: str
    current_version: int | None
    versions: int
    size_bytes: int
    created_at: str


@dataclass(frozen=True)
class ArtifactUpload:
    """An open upload of yours: begun and not yet committed or withdrawn.

    Parameters
    ----------
    upload_id
        The upload's id, for `client.artifacts.abort_upload`.
    artifact
        The artifact it adds a version to, ``namespace.name``.
    created_at
        When it was begun, ISO 8601.
    expires_at
        When it stops being usable and frees its place, ISO 8601.
    """

    upload_id: str
    artifact: str
    created_at: str
    expires_at: str


@dataclass(frozen=True)
class ArtifactUploadPage:
    """One page of your open uploads, newest first.

    Parameters
    ----------
    items
        The uploads on this page.
    next_cursor
        Pass back as `cursor=` to fetch the next page; `None` on the last page.
    """

    items: list[ArtifactUpload]
    next_cursor: str | None


@dataclass(frozen=True)
class ArtifactDownload:
    """What `client.artifacts.download` wrote.

    Parameters
    ----------
    path
        For a directory artifact the directory; for a single file ``<directory>/<file name>``.
    version
        The version downloaded.
    manifest_sha256
        The digest of its manifest: the identity of the content.
    size_bytes
        The summed size of the files written.
    file_count
        How many files were written.
    """

    path: str
    version: int
    manifest_sha256: str
    size_bytes: int
    file_count: int


# jscpd:ignore-start
# The SDK depends on no other package, so these mirror the server's response models.
@dataclass(frozen=True)
class SearchHit:
    """One search hit.

    Parameters
    ----------
    kind
        What was found: ``table``, ``column``, ``run`` or ``principal``.
    name
        A display name: ``namespace.table``, ``namespace.table.column``, the run id or the
        principal's name.
    matched
        The field that matched: ``name``, ``description``, ``id``, ``job`` or ``repository``.
    snippet
        For a description match, at most 160 characters of the description around the hit.
    namespace
        For a table or column, its namespace, dotted.
    table
        For a table or column, the table's name.
    table_kind
        For a table, ``TABLE``, ``EXTERNAL``, ``VIEW`` or ``ARTIFACT``.
    column
        For a column, its name.
    column_type
        For a column, its type.
    run_id
        For a run, its id.
    job
        For a run, its job, if it has one.
    repository
        For a run, its repository.
    status
        For a run, its status.
    principal
        For a principal, its name.
    principal_type
        For a principal, ``USER`` or ``SERVICE_ACCOUNT``.
    """

    kind: str
    name: str
    matched: str
    snippet: str | None = None
    namespace: str | None = None
    table: str | None = None
    table_kind: str | None = None
    column: str | None = None
    column_type: str | None = None
    run_id: str | None = None
    job: str | None = None
    repository: str | None = None
    status: str | None = None
    principal: str | None = None
    principal_type: str | None = None


@dataclass(frozen=True)
class SearchPage:
    """One page of search hits.

    Parameters
    ----------
    results
        The hits, best first.
    limit
        The page size asked for.
    offset
        How many hits were skipped.
    has_more
        Whether a hit follows this page; ask again with a larger `offset`.
    """

    results: list[SearchHit]
    limit: int
    offset: int
    has_more: bool


# jscpd:ignore-end


@dataclass(frozen=True)
class CiTrustRule:
    """A CI trust rule: which CI jobs may deploy a repository to an environment, as whom.

    Parameters
    ----------
    id
        The rule's id.
    issuer
        The issuer URL (GitHub Actions, GitLab).
    principal
        The service account the CI job acts as.
    environment
        The environment it may deploy to.
    repository
        The repository it may deploy.
    kinds
        ``deploy`` and/or ``dry_run``.
    conditions
        Claim name to the exact text the ID token's claim must have.
    enabled
        Whether it accepts ID tokens.
    created_by
        Who created it.
    created_at
        When.
    updated_by
        Who changed it last.
    updated_at
        When it last changed.
    start_on_deploy_jobs
        Whether a deploy accepted under the rule starts the project's `on_deploy` jobs; false
        from a server that predates the setting.
    """

    id: str
    issuer: str
    principal: str
    environment: str
    repository: str
    kinds: list[str]
    conditions: dict[str, str]
    enabled: bool
    created_by: str | None
    created_at: datetime
    updated_by: str | None
    updated_at: datetime
    start_on_deploy_jobs: bool = False


@dataclass(frozen=True)
class CiTrustRuleExplanation:
    """Why one rule does or does not accept a set of claims.

    Parameters
    ----------
    rule_id
        The rule.
    matches
        Whether it accepts them.
    reasons
        Why not; empty when it matches.
    """

    rule_id: str
    matches: bool
    reasons: list[str]


@dataclass(frozen=True)
class CiTrustExplanation:
    """What an exchange of an ID token with these claims would do.

    Parameters
    ----------
    issuer
        The issuer compared.
    environment
        The environment asked for.
    repository
        The repository asked for.
    verdict
        One sentence: the rule that accepts the claims, or why the exchange is refused.
    matching
        The ids of the rules that match.
    rules
        Every rule, with its reasons.
    """

    issuer: str
    environment: str
    repository: str
    verdict: str
    matching: list[str]
    rules: list[CiTrustRuleExplanation]


@dataclass(frozen=True)
class CiToken:
    """A Lakana CI token, received for a CI job's OIDC ID token.

    Parameters
    ----------
    token
        ``lakana_ci_…``; use it as the token of a `Client`. Shown once.
    expires_at
        When it stops working.
    principal
        The service account it acts as.
    environment
        The one environment it may deploy to.
    repository
        The one repository it may deploy.
    kinds
        What it may queue, once: ``deploy`` and/or ``dry_run``.
    sha
        The commit a deploy is pinned to, or `None`.
    rule_id
        The CI trust rule that accepted the ID token.
    """

    token: str = field(repr=False)
    expires_at: datetime
    principal: str
    environment: str
    repository: str
    kinds: list[str]
    sha: str | None
    rule_id: str
