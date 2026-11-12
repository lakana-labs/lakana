"""Pydantic models for the platform API's request and response bodies."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from lakana_core.auth import Action, SecurableKind
from lakana_core.catalog.maintenance import (
    DEFAULT_KEEP_DAYS,
    DEFAULT_KEEP_LAST,
    DEFAULT_ORPHAN_AGE_HOURS,
    MAX_ORPHAN_AGE_HOURS,
    MIN_ORPHAN_AGE_HOURS,
)
from lakana_core.catalog.orphaned_locations import DEFAULT_OLDER_THAN_DAYS
from lakana_core.catalog.writes import MAX_SOURCE_NAME, MAX_SOURCE_VERSIONS
from lakana_core.connections.kinds import ConnectionKind
from lakana_core.grants.roles import Role
from lakana_core.home.attention import AttentionKind
from lakana_core.models import AuditEventType


def _check_storage_options(value: object) -> object:
    """Refuse storage `options` that are not a flat text-to-text object, naming no key.

    Storage options can carry inline S3 credentials, and pydantic puts a mapping key into the
    location of its error, so a credential typed into the wrong place would be echoed. The error
    here names neither a key nor a value. `None` passes (the update request may leave it out).

    Parameters
    ----------
    value
        What the client sent as `options`.

    Returns
    -------
    object
        `value`, unchanged, when it is `None` or every key and value is a string.

    Raises
    ------
    pydantic_core.PydanticCustomError
        If `value` is not an object or any of its values is not a string.
    """
    if value is None:
        return value
    if not isinstance(value, dict) or not all(isinstance(item, str) for item in value.values()):
        raise PydanticCustomError(
            "storage_options",
            "options must be an object whose entries are all text, such as "
            '{"endpoint": "https://s3.corp"}; send each entry as a string.',
        )
    return value


StorageOptions = Annotated[dict[str, str], BeforeValidator(_check_storage_options)]
"""An environment's storage options; a bad shape is refused without echoing any of it."""

OptionalStorageOptions = Annotated[dict[str, str] | None, BeforeValidator(_check_storage_options)]
"""`StorageOptions`, or `None` when the request leaves them unchanged."""


class StorageConfigIn(BaseModel):
    """Storage configuration as accepted from a client.

    Parameters
    ----------
    scheme
        Storage backend.
    root
        Root URI for the environment.
    options
        Backend-specific options.
    """

    scheme: Literal["file", "s3", "abfs"]
    root: str
    options: StorageOptions = {}


class EnvironmentCreate(BaseModel):
    """Request body for creating an environment.

    Parameters
    ----------
    name
        Unique environment name. Not ``instance`` in any case, without a ``.``, at most 255
        characters and without blanks at either end: the access file could not write it.
    storage
        Where the environment's data lives.
    is_protected
        Whether deleting this environment and changing its storage are
        refused (`CatalogService.delete_environment`/
        `update_environment_storage`) until cleared via
        `PATCH /api/v1/environments/{name}`.
    properties
        Free-form metadata.
    """

    name: str
    storage: StorageConfigIn
    is_protected: bool = False
    properties: dict[str, str] = {}


class EnvironmentOut(BaseModel):
    """Response body for an environment.

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
        The retention of its tables that set none of their own, in days; `null` when off.
    deleted_at
        When it was deleted; `null` unless it is (`GET /api/v1/environments?deleted=true`).
    purge_after
        For a deleted environment, until when `POST /api/v1/environments/{name}/restore` can
        restore it; from then on the worker's clean-up deletes its tables' files. `null`
        unless deleted.
    """

    name: str
    storage_root: str
    storage_secret_ref: str | None
    is_protected: bool
    properties: dict[str, str]
    retention_days: int | None = None
    deleted_at: datetime | None = None
    purge_after: datetime | None = None

    model_config = {"from_attributes": True}


class EnvironmentRetentionIn(BaseModel):
    """Request body that sets an environment's retention, or turns it off.

    Parameters
    ----------
    days
        Its tables that set no retention of their own expire this many days (1 to 3,650) after
        their last activity; `null` turns it off. The field must be sent.
    """

    model_config = ConfigDict(extra="forbid")

    days: int | None


class OrphanedLocationsDeleteIn(BaseModel):
    """Request body that deletes an environment's table directories without a location record.

    Parameters
    ----------
    older_than_days
        Only directories whose newest file is older than this many days (1 to 3,650) are
        deleted; 30 when left out.
    """

    model_config = ConfigDict(extra="forbid")

    older_than_days: int = DEFAULT_OLDER_THAN_DAYS


class OrphanedLocationOut(BaseModel):
    """One table directory under ``managed/`` that no location record names.

    Parameters
    ----------
    location
        Its path relative to the environment's storage root.
    files
        How many files it held when it was listed.
    bytes
        Their total size.
    newest_file_at
        When its newest file was last changed; `null` when the storage does not say.
    recent
        Whether a file is newer than `older_than_days`, or of unknown age: it is never deleted.
    deleted
        Whether the deletion removed it.
    files_deleted
        The files the deletion removed from it.
    bytes_deleted
        Their total size.
    kept_because
        Why the deletion left it; `null` in a listing and for a deleted directory.
    """

    model_config = {"from_attributes": True}

    location: str
    files: int
    bytes: int
    newest_file_at: datetime | None
    recent: bool
    deleted: bool
    files_deleted: int
    bytes_deleted: int
    kept_because: str | None


class OrphanedLocationsOut(BaseModel):
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
        `false` when the server's time limit stopped the call with work left; call again to
        continue.
    """

    model_config = {"from_attributes": True}

    environment: str
    older_than_days: int
    locations: list[OrphanedLocationOut]
    complete: bool


class UsagePartOut(BaseModel):
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

    model_config = {"from_attributes": True}

    count: int
    bytes: int
    unmeasured: int


class EnvironmentUsageOut(BaseModel):
    """How much storage an environment uses, from the figures the catalog holds.

    Parameters
    ----------
    environment
        The environment.
    tables
        Its managed tables: the files of each one's current snapshot.
    dropped_tables
        Its dropped managed tables whose files wait for the end of their undo period.
    artifacts
        Its artifacts: `bytes` is the size of the files stored for them.
    code_archives
        The code archives of its project versions.
    total_bytes
        The four sums together.
    complete
        `false` when a part has unmeasured entries: the true figure is higher.
    """

    model_config = {"from_attributes": True}

    environment: str
    tables: UsagePartOut
    dropped_tables: UsagePartOut
    artifacts: UsagePartOut
    code_archives: UsagePartOut
    total_bytes: int
    complete: bool


class TableRetentionIn(BaseModel):
    """Request body that sets when a managed table expires: exactly one of the three fields.

    Parameters
    ----------
    days
        Expire this many days (1 to 3,650) after the table's last activity: the later of its
        last change and its last recorded read.
    on
        Expire from midnight UTC of this day on (``YYYY-MM-DD``); it must be after today.
    never
        `true`: never expire, whatever the environment's retention says.
    """

    model_config = ConfigDict(extra="forbid")

    days: int | None = None
    on: date | None = None
    never: StrictBool = False


class TableRetentionOut(BaseModel):
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
        ``table`` when the table sets its own, ``environment`` when its environment's applies;
        `null` when it has none.
    days
        The days after its last activity it expires.
    on
        The day it expires on.
    never
        Whether the table says it never expires.
    set_by
        Who set the retention that applies; `null` when nobody did or that principal is gone.
    set_at
        When it was set.
    last_activity_at
        The later of the table's last change and its last recorded read.
    expires_at
        When it expires; `null` when it never does.
    kept
        Why a table that has a retention never expires: ``protected`` (a protected environment)
        or ``project_asset`` (a deployed project declares it); `null` otherwise.
    """

    name: str
    namespace: str
    uuid: str
    source: Literal["table", "environment"] | None
    days: int | None
    on: date | None
    never: bool
    set_by: str | None
    set_at: datetime | None
    last_activity_at: datetime
    expires_at: datetime | None
    kept: Literal["protected", "project_asset"] | None


class EnvironmentPatch(BaseModel):
    """Request body for toggling an environment's protected flag.

    Parameters
    ----------
    is_protected
        The new value. Required, unlike `EnvironmentStorageUpdate`'s
        fields: there is exactly one thing `PATCH /api/v1/environments/{name}`
        can change, so there is no "leave unchanged if omitted" case for it
        to support. `StrictBool`, not `bool`: pydantic's lax `bool` coerces
        a string like `"true"` or an int like `1`, which would silently
        accept a client's mistake on an endpoint whose entire job is
        flipping one flag correctly.
    """

    is_protected: StrictBool

    model_config = {"extra": "forbid"}


class NamespaceCreate(BaseModel):
    """Request body for creating a namespace.

    Parameters
    ----------
    path
        Dot-joined namespace path, such as ``gold.sales``.
    properties
        Free-form metadata.
    break_glass_reason
        Write to a protected environment outside a run; needs `MANAGE_ENVIRONMENT`,
        audited.
    """

    path: str
    properties: dict[str, str] = {}
    break_glass_reason: str | None = Field(default=None, max_length=500)


class NamespaceOut(BaseModel):
    """Response body for a namespace.

    Parameters
    ----------
    path
        Dot-joined namespace path.
    properties
        Free-form metadata.
    """

    path: str
    properties: dict[str, str]


class WriteTargetIn(BaseModel):
    """Where a write run puts its result.

    Parameters
    ----------
    table
        The target's dotted name.
    mode
        `create`, `replace`, `append` or `view`. `merge` is accepted only to be refused with
        what merges instead (an asset with `write: merge`, or `tables.merge`).
    incremental_key
        The key column, `append` only.
    evolve_schema
        Let the target adopt the result's schema, `replace`/`append` only.
    """

    model_config = ConfigDict(extra="forbid")

    table: str = Field(min_length=1, max_length=1024)
    mode: Literal["create", "replace", "append", "view", "merge"]
    incremental_key: str | None = Field(default=None, max_length=255)
    evolve_schema: bool = False


class QuerySubmit(BaseModel):
    """Request body for submitting a query.

    Parameters
    ----------
    sql
        The SQL text to run.
    target
        Makes this a write run: the result goes into this table or
        view instead of a result file.
    break_glass_reason
        Write to a protected environment outside a run; needs `MANAGE_ENVIRONMENT`,
        audited.
    """

    sql: str
    target: WriteTargetIn | None = None
    break_glass_reason: str | None = Field(default=None, max_length=500)


class CheckRowsRequest(BaseModel):
    """Request body for the query that shows the rows a check of a run found.

    Parameters
    ----------
    check
        The check as `lakana check list` names it: ``<namespace>.<table>/<check>``. The run
        comes from the path; the snapshot the check read and the stored result are looked up
        by the server, never named by the caller.
    """

    model_config = ConfigDict(extra="forbid")

    check: str = Field(min_length=1, max_length=1000)


class QuerySubmitOut(BaseModel):
    """Response body for a freshly submitted query.

    Parameters
    ----------
    query_id
        The submitted query's id, to poll or fetch results with.
    """

    query_id: str


class ArtifactLimitsOut(BaseModel):
    """What one artifact version may hold on this server (`LAKANA_ARTIFACTS__MAX_*`).

    Parameters
    ----------
    max_files
        Most files in one version.
    max_file_bytes
        Largest single file.
    max_bytes
        Largest total size of one version.
    """

    max_files: int
    max_file_bytes: int
    max_bytes: int


class InfoOut(BaseModel):
    """Response body for `GET /api/v1/info`.

    Parameters
    ----------
    version
        The platform version the server runs. A project's `lakana[assets]` pin must be
        compatible with it.
    artifact_limits
        The artifact limits the operator set, so a client can check a tree against them before
        it sends a byte. An older server does not send it.
    upload_max_bytes
        The largest Arrow body one append, replace, merge or load may send
        (`LAKANA_APPEND__MAX_BYTES`), so `lakana load` can refuse a file before it reads or
        sends it. An older server does not send it.
    """

    version: str
    artifact_limits: ArtifactLimitsOut
    upload_max_bytes: int


class QueryStatusOut(BaseModel):
    """Response body for a query's current status.

    Parameters
    ----------
    query_id
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
        A maintenance run's `{"request", "report"}`. The report's `conflict`
        is `true` when the table changed during maintenance and nothing was
        applied (retry); its `note` says in words why part of the run was
        skipped.
    error_cause
        Why a `FAILED` or `CANCELLED` run ended: `not_found`, `permission_denied`,
        `result_too_large`, `invalid_query`, `out_of_memory`, `timeout`, `cancelled`, `worker_lost`
        or `internal`.
        `null` for a run that did not end that way and for a failure recorded without one
        (a run from before the field existed).
    peak_memory_bytes
        Highest resident memory of the query's process in bytes; `null` if not measured. A query
        that failed or was stopped keeps what it measured before it ended.
    cpu_seconds
        Processor time the query used, in seconds; `null` if not measured.
    bytes_read
        Bytes DuckDB read (a query) or the SDK readers read (a Python task); `null` if unknown.
    waiting_for
        Why a `QUEUED` query has not started, or `null` for any other status: a stable `code`
        (`no_worker`, `no_worker_of_kind`, `user_limit`, `lock_timeout`, `starting` or
        `workers_busy`), one
        sentence in `message`, and the numbers the sentence uses. `workers` is the number of
        workers that take this kind of work and `ahead` the queued work those workers take
        before this query; both count the whole instance. `no_worker_of_kind` (workers are
        running and none takes this kind of work) carries `workers` (0) and `other_workers`.
        `user_limit` (the submitter already runs as many ad-hoc queries as the limit per user
        allows) carries `running`, the submitter's own number, and `limit`, and no `ahead`; a
        manager who reads another principal's row gets `limit` only. `lock_timeout` (a worker
        waited too long for the lock that serialises the start of the submitter's queries, so
        workers pass them by for a few seconds) carries `workers` and `next_attempt_at`, the end
        of that time.
    """

    query_id: str
    status: str
    error_message: str | None = None
    lineage: dict[str, list[dict[str, str]]] | None = None
    write_mode: str | None = None
    target: str | None = None
    rows_written: int | None = None
    snapshot_id: int | None = None
    watermark: str | None = None
    schema_diff: dict[str, list[dict[str, str]]] | None = None
    maintenance: dict | None = None
    error_cause: str | None = None
    peak_memory_bytes: int | None = Field(
        default=None,
        description=(
            "Highest resident memory of the attempt's process in bytes; null if not measured "
            "(for a container task: the container's cgroup peak)."
        ),
    )
    cpu_seconds: float | None = Field(
        default=None,
        description=(
            "Processor time (user plus system) the attempt used, in seconds; null if not measured."
        ),
    )
    bytes_read: int | None = Field(
        default=None,
        description=(
            "Bytes DuckDB read for a query (its `io.total_bytes_read` metric, null when DuckDB "
            "answered from file metadata); for a Python task the bytes the SDK readers "
            "downloaded or read; null if unknown."
        ),
    )
    waiting_for: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Why this is waiting, or null when it is not: a stable `code`, one sentence in "
            "`message`, and the numbers the sentence uses."
        ),
    )


class ResultColumnOut(BaseModel):
    """One column of a JSON result page.

    Documents the page `lakana_server.result_json.json_page` writes; the route returns those
    bytes as they are and never builds this model.

    Parameters
    ----------
    name
        The column's name.
    type
        The column's Arrow type name, cut to 256 characters.
    type_cut
        `true` when the type name was cut; absent otherwise.
    """

    name: str
    type: str
    type_cut: bool | None = Field(
        default=None, description="`true` when the type name was cut; absent otherwise."
    )


class MaskedColumnOut(BaseModel):
    """A column a mask was applied to for the reader, as the worker recorded it.

    Parameters
    ----------
    table
        The masked table's dotted name.
    column
        The column, named as in the read.
    """

    table: str
    column: str


class PreviewOut(BaseModel):
    """The table a preview showed, named from the run's own SQL.

    Parameters
    ----------
    table
        The dotted name, cut to 1,024 characters.
    cut
        `true` when the name was cut; absent otherwise.
    """

    table: str
    cut: bool | None = Field(
        default=None, description="`true` when the name was cut; absent otherwise."
    )


class ResultPageOut(BaseModel):
    """One page of a query result as JSON (`GET …/result?format=json`).

    Documents the page `lakana_server.result_json.json_page` writes; the route returns those
    bytes as they are and never builds this model. The page is at most 4 MiB plus a small
    envelope, and each cell at most 64 KiB: a cut cell is ``{"cut": true}``, or
    ``{"cut": true, "text": <prefix>}`` for a string, and a cut list ends with
    ``{"cut": true}`` after 100 elements.

    Parameters
    ----------
    columns
        The result's columns, in order.
    columns_cut
        How many columns were left out: when the header filled the page, the columns that did not
        fit and no row is sent; when the page budget cut a row inside, the cells of that row that
        did not fit.
    masked_columns
        The columns masked for the reader, `[]` when none was; `null` when not recorded (a run
        from before this was recorded).
    masked_columns_cut
        How many further masked columns were left out because the page was full.
    rows
        The rows of the page, each a list of cells in column order. 64-bit integers and
        decimals are strings, binary is ``{"binary": <bytes>}``, and NaN and infinities are
        strings.
    row_cut
        The positions on this page of the rows that were cut inside.
    row_count
        The rows the result has, at most 100 for a preview.
    next_offset
        The offset of the next page, or `null` at the end.
    duration_ms
        How long the query ran, or `null` when unknown.
    preview
        The previewed table for a preview, `null` for any other query.
    """

    # jscpd:ignore-start
    # The keys `json_page` writes; the SDK's `_models.py` mirrors them for `QueryRun.page`.
    columns: list[ResultColumnOut]
    columns_cut: int
    masked_columns: list[MaskedColumnOut] | None
    masked_columns_cut: int
    rows: list[list[Any]]
    row_cut: list[int]
    row_count: int
    next_offset: int | None
    duration_ms: int | None
    preview: PreviewOut | None
    # jscpd:ignore-end


class TableDefinitionOut(BaseModel):
    """Where the environment's current deployment defines a table.

    Every field but `defined` is `null` for a table no current deployment defines.

    Parameters
    ----------
    defined
        Whether a current deployment defines the table.
    repository
        The repository's name.
    path
        The file's path in the repository.
    commit
        The deployed commit, never the branch head.
    line
        The line the file defines the table on.
    project_version_id
        The deployed project version.
    deployed_at
        When that version was first recorded in the environment.
    browse_url
        An HTTPS link to the line on the repository's git host, or `null` when none can be
        built (a host other than the five public ones, or a local repository).
    """

    # jscpd:ignore-start
    # The SDK's `_models.py` mirrors these fields for `TableDefinition`.
    defined: bool
    repository: str | None
    path: str | None
    commit: str | None
    line: int | None
    project_version_id: str | None
    deployed_at: datetime | None
    browse_url: str | None
    # jscpd:ignore-end


class TableDefinitionFileOut(TableDefinitionOut):
    """A table's definition with the file's text, or the reason the text is not returned.

    Flat: the location's fields, then the file's. Core's `TableDefinitionFile` nests the
    location; `lakana_server.api.platform.tables._definition_file_out` flattens it.

    Parameters
    ----------
    content
        The file's text (UTF-8, with line-feed line endings), or `null`.
    bytes
        The size of the text's UTF-8 encoding, set only with `content`.
    reason
        Why `content` is `null`: `restricted` (the reader does not pass the content rule),
        `not_recorded` (the deployed version has no record of the file) or `too_large` (over
        1 MiB); `null` with content and for a table no deployment defines.
    message
        The sentence for `reason`, naming no table, or `null`.
    """

    # jscpd:ignore-start
    # The SDK's `_models.py` mirrors these fields for `TableDefinitionFile`.
    content: str | None
    bytes: int | None
    reason: Literal["restricted", "not_recorded", "too_large"] | None
    message: str | None
    # jscpd:ignore-end


class ExternalTableCreate(BaseModel):
    """Request body for registering a raw-zone table.

    Parameters
    ----------
    name
        Table name.
    location_pattern
        A pattern shaped `"raw/<dir...>/<basename-glob>"`. The directory
        part is this table's exclusive claim within the environment; the
        final component is a basename-only glob matched against direct
        children of that directory only.
    format
        `"parquet"`, `"csv"`, or `"json"`.
    break_glass_reason
        Write to a protected environment outside a run; needs `MANAGE_ENVIRONMENT`,
        audited.
    """

    name: str
    location_pattern: str
    format: str
    break_glass_reason: str | None = Field(default=None, max_length=500)


class ExternalTableOut(BaseModel):
    """Response body for a registered raw-zone table.

    The wire field is still named `"schema"` (`Field(alias="schema")`); the
    Python attribute is `table_schema`, not `schema`, because `BaseModel`
    itself defines a (deprecated, v1-compat) `schema()` method, and pydantic
    warns at class-definition time that a field named `schema` shadows it
    . `populate_by_name=True` keeps construction
    by either name working — `tables.py` builds this from a dict keyed
    `"schema"` — and `serialize_by_alias=True` keeps the JSON key `schema`
    for callers that call `.model_dump()` directly instead of going through
    FastAPI's `response_model` (which already serializes by alias).

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

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    name: str
    namespace: str
    location_pattern: str
    format: str
    table_schema: dict[str, str] = Field(alias="schema")


class ColumnIn(BaseModel):
    """One column in a caller-declared table schema.

    `extra="forbid"` refuses an unrecognised field
    the same way `TableCreate` does at the table level.

    Parameters
    ----------
    name
        Column name. Bounded to 255 characters — generous for any real
        column name, small enough to keep a pathological request cheap to
        reject.
    type
        An Iceberg primitive type name — one of `boolean`, `int`, `long`,
        `float`, `double`, `decimal(p,s)`, `date`, `time`, `timestamp`,
        `timestamptz`, `string`, `uuid`, or `binary`. Nested types are out
        of scope for M4. `lakana_core.catalog.json_schema.to_iceberg_schema`
        is the single place that maps this name onto a pyiceberg type.
        Bounded to 64 characters — the longest real value here,
        `decimal(38,38)`-shaped, is nowhere near that.
    nullable
        Whether the column may hold nulls.
    """

    name: str = Field(max_length=255)
    type: str = Field(max_length=64)
    nullable: bool = True

    model_config = {"extra": "forbid"}


class TableCreate(BaseModel):
    """Request body for creating a managed table from a JSON schema.

    `extra="forbid"` refuses an unrecognised field outright (422) rather
    than silently ignoring it — in particular, a caller-sent `"location"`
    is refused the same way the Iceberg router already refuses one, instead
    of being accepted and quietly dropped, which would misleadingly suggest
    it had any effect.

    The wire field is still named `"schema"` (`Field(alias="schema")`); the
    Python attribute is `columns`, not `schema`, because `BaseModel` itself
    defines a (deprecated, v1-compat) `schema()` method, and pydantic warns
    at class-definition time that a field named `schema` shadows it — `ExternalTableOut` has the
    same shape (`table_schema`
    aliased to `schema`), fixed the same way.

    Parameters
    ----------
    name
        Table name.
    columns
        The table's columns, in order. Field ids are assigned `1..n` in
        this order. Bounded to 1000 columns.
    break_glass_reason
        Write to a protected environment outside a run; needs `MANAGE_ENVIRONMENT`,
        audited.
    """

    name: str
    columns: list[ColumnIn] = Field(alias="schema", max_length=1000)
    break_glass_reason: str | None = Field(default=None, max_length=500)

    model_config = {"extra": "forbid", "populate_by_name": True}


class AppendOut(BaseModel):
    """Response body for a governed Arrow append.

    Parameters
    ----------
    rows
        Number of rows appended.
    files
        Number of data files this append wrote. `0` when `rows` is `0`.
    snapshot_id
        The id of the snapshot this append committed, or `None` when `rows`
        is `0` — an empty append commits nothing.
    """

    rows: int
    files: int
    snapshot_id: int | None


class LoadOut(BaseModel):
    """Response body for a load into the bronze layer (`lakana load`).

    Parameters
    ----------
    snapshot_id
        The snapshot the load committed; `None` for a dry run and for a load without rows.
    rows
        Rows written; `0` for a dry run.
    created
        Whether the load created the table (for a dry run: would create it).
    dry_run
        Whether this was a dry run, which checks and writes nothing.
    """

    snapshot_id: int | None
    rows: int
    created: bool
    dry_run: bool


class MergeOut(BaseModel):
    """Response body for a governed Arrow merge.

    Parameters
    ----------
    snapshot_id
        The snapshot the merge committed, or `None` when it changed nothing (no rows, or every
        row equal to the table's).
    rows_inserted
        Rows whose key the table did not have.
    rows_updated
        Rows whose key matched and whose values changed.
    """

    snapshot_id: int | None
    rows_inserted: int
    rows_updated: int


class HistoryOut(BaseModel):
    """What a history write answers: the snapshot and how many versions it opened and closed.

    Parameters
    ----------
    snapshot_id
        The committed snapshot; `None` when the rows matched the open versions already.
    versions_opened
        New versions: a key seen for the first time, or a key with changed values.
    versions_closed
        Ended versions: a key with changed values, or a key the rows lack (`close_missing`).
    closed_missing
        The part of `versions_closed` that ended because the key is gone from the rows.
    created
        `True` when this write created the table (`expected_snapshot_id=none`).
    """

    snapshot_id: int | None
    versions_opened: int
    versions_closed: int
    closed_missing: int
    created: bool


class ReplaceOut(BaseModel):
    """The outcome of an Arrow replace.

    Parameters
    ----------
    rows
        Rows written.
    files
        Data files written.
    snapshot_id
        The snapshot committed.
    schema_diff
        How the table's schema changed, or `None` when it didn't.
    """

    rows: int
    files: int
    snapshot_id: int | None
    schema_diff: dict[str, list[dict[str, str]]] | None = None


class TermRefOut(BaseModel):
    """A short reference to a term, as a table or column shows it.

    Parameters
    ----------
    key
        The term's id.
    name
        The term's name in the reader's language.
    """

    key: str
    name: str


class ColumnOut(BaseModel):
    """One column of a table's schema, in the JSON column form.

    Parameters
    ----------
    name
        Column name.
    type
        The Iceberg type in the JSON form (`"long"`, `"decimal(10,2)"`, ...),
        or the Iceberg string for a nested type.
    nullable
        Whether the column accepts nulls.
    description
        The column's description: the edit when it has one, else the project's text; `None`
        when there is none.
    description_source
        ``edited`` or ``project``; `None` when there is no description.
    label
        The column's short name for people, in the reader's language where it has one, else
        the main language's; `None` when there is none.
    label_source
        ``edited`` or ``project``; `None` when there is no label.
    label_language
        The language of `label`; `None` exactly when `label` is.
    description_language
        The language of `description`; `None` exactly when `description` is.
    terms
        The glossary terms that link the column, in the reader's language; empty without
        ``READ_ENVIRONMENT``.
    """

    name: str
    type: str
    nullable: bool
    description: str | None = None
    description_source: Literal["project", "edited"] | None = None
    label: str | None = None
    label_source: Literal["project", "edited"] | None = None
    label_language: str | None = None
    description_language: str | None = None
    terms: list[TermRefOut] = []


class TranslationCoverageOut(BaseModel):
    """How many of a table's main-language texts one language has.

    Parameters
    ----------
    language
        The language.
    translated
        Texts it has.
    total
        Texts the table shows in its main language.
    """

    language: str
    translated: int
    total: int


class TableReferenceOut(BaseModel):
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


class OwnerOut(BaseModel):
    """Who owns a table.

    Parameters
    ----------
    name
        The principal's name.
    kind
        ``USER`` or ``SERVICE_ACCOUNT``.
    """

    name: str
    kind: Literal["USER", "SERVICE_ACCOUNT"]


class HealthSummaryOut(BaseModel):
    """A table's health in one word.

    Parameters
    ----------
    state
        ``failing``, ``warning``, ``healthy`` or ``not_judged``.
    item
        The word's name.
    text
        The word as it is shown.
    others
        How many other findings the word leaves out.
    """

    state: Literal["failing", "warning", "healthy", "not_judged"]
    item: str
    text: str
    others: int


class TableMatchOut(BaseModel):
    """What part of a table a search text matched.

    Parameters
    ----------
    kind
        ``table`` for the table's own text, ``column`` for one of its columns'.
    matched
        The field: ``name``, ``label``, ``description``, ``grain`` or ``measure``.
    column
        For a column match, the column's name.
    column_type
        For a column match, its type.
    snippet
        For a label, description, grain or measure match, the text around the match.
    language
        The language of the matched text; `None` for a name.
    translated
        Whether the matched text is a translation.
    """

    # jscpd:ignore-start
    # The fields of `lakana_core.catalog.table_listing.TableMatch`, which this answer is built
    # from attribute by attribute (`from_attributes`): the REST contract names each one itself.
    kind: str
    matched: str
    column: str | None
    column_type: str | None
    snippet: str | None
    language: str | None
    translated: bool
    # jscpd:ignore-end


class TableListItemOut(BaseModel):
    """One table of the catalog list.

    Parameters
    ----------
    environment
        Its environment.
    namespace
        Its namespace, dotted.
    name
        Its name.
    uuid
        Its id.
    kind
        ``TABLE``, ``EXTERNAL``, ``VIEW`` or ``ARTIFACT``.
    model_kind
        The ``kind`` its asset header declares, if any.
    state
        ``not_published`` or ``kept`` for a managed table so marked; ``published`` otherwise.
    owner
        Its owner; `None` when the owner was deleted.
    created_at
        When it was created.
    row_count
        Its rows, as stored; `None` when not known.
    size_bytes
        The size of its data files; `None` when not known, and whenever `last_write_at` is.
    last_write_at
        When its current snapshot was committed; `None` without one.
    last_read_at
        When a succeeded query last read it; `None` when none did.
    readers_7d
        How many principals read it in the last 7 UTC days, today included.
    health_summary
        Its health in one word, as the caller may see it.
    match
        With a search text, what of the table matched; `None` without one.
    """

    # jscpd:ignore-start
    # The fields of `lakana_core.catalog.table_listing.TableListItem`, which this answer is built
    # from attribute by attribute (`from_attributes`): the REST contract names each one itself.
    environment: str
    namespace: str
    name: str
    uuid: str = Field(validation_alias=AliasChoices("uuid", "id"))
    kind: Literal["TABLE", "EXTERNAL", "VIEW", "ARTIFACT"]
    model_kind: str | None
    state: Literal["published", "not_published", "kept"]
    owner: OwnerOut | None
    created_at: datetime
    row_count: int | None
    size_bytes: int | None
    last_write_at: datetime | None
    last_read_at: datetime | None
    readers_7d: int
    health_summary: HealthSummaryOut
    match: TableMatchOut | None
    # jscpd:ignore-end

    @field_validator("uuid", mode="before")
    @classmethod
    def _id_as_text(cls, value: object) -> object:
        """Write an id as text, as `TableOut.uuid` does.

        Parameters
        ----------
        value
            The id, or text.

        Returns
        -------
        object
            The text of a UUID; anything else unchanged.
        """
        return str(value) if isinstance(value, uuid.UUID) else value


class TableListPageOut(BaseModel):
    """One page of the catalog list.

    Parameters
    ----------
    items
        The tables, in the list's order.
    next_cursor
        Pass it as `cursor` for the next page; `null` on the last.
    capped
        Whether a search text stopped early, with more readable candidates than one search reads.
    """

    items: list[TableListItemOut]
    next_cursor: str | None
    capped: bool


class TreeNamespaceOut(BaseModel):
    """A namespace of the catalog tree.

    Parameters
    ----------
    path
        Its path, dotted.
    count
        The readable tables in it and beneath it.
    """

    path: str
    count: int


class TreeEnvironmentOut(BaseModel):
    """An environment of the catalog tree.

    Parameters
    ----------
    name
        Its name.
    count
        The readable tables in it.
    namespaces
        Its namespaces the caller is shown, by path.
    """

    name: str
    count: int
    namespaces: list[TreeNamespaceOut]


class TableTreeOut(BaseModel):
    """The counts of readable tables, by environment and namespace.

    Parameters
    ----------
    count
        The readable tables in every environment.
    environments
        The environments the caller is shown, by name.
    """

    count: int
    environments: list[TreeEnvironmentOut]


class TableOut(BaseModel):
    """Response body for a table, external table or view, read-only.

    Parameters
    ----------
    name
        Table name.
    namespace
        Dot-joined namespace path the table lives in.
    uuid
        The table's id (the Iceberg ``table-uuid`` for a managed table).
    kind
        ``TABLE``, ``EXTERNAL``, ``VIEW`` or ``ARTIFACT``.
    metadata_location
        Absolute URI of the current metadata file; `None` for an external
        table or a view.
    columns
        The columns, serialized as `schema`.
    sql
        A view's SELECT. `None` if the caller may not read every reference
        .
    references
        A view's references. Empty if the caller may not read every
        reference.
    hidden_references
        The number of the view's references the caller may not read, and
        so are withheld from `sql` and `references`.
    snapshot_id
        The current snapshot of a managed table; `None`
        without one, and for a view or an external table.
    state
        ``not_published`` for a table whose first build did not finish, or failed a blocking
        check, and that no later run has published; ``published`` for everything else.
    state_message
        Why it is not published, naming the check and the run; `None` otherwise.
    description
        The table's description: the edit when it has one, else the project's text; `None`
        when there is none.
    description_source
        ``edited`` or ``project``; `None` when there is no description.
    last_write_at
        When the current snapshot was committed; `None` without one, and for a view, an
        external table and an artifact.
    row_count
        The rows of the current snapshot, from its summary; `None` when missing.
    size_bytes
        The size of its data files, from its summary; `None` when missing.
    file_count
        Its data files, from its summary; `None` when missing.
    expires_at
        When a managed table's retention makes it expire; `None` when it never does.
    expires_soon
        Whether that is within the server's warning window
        (``LAKANA_TABLES__EXPIRY_WARNING_DAYS``, 7 days by default); always `false` when that
        is 0.
    model
        The model the asset header declares (kind, grain, key, references, measures and the
        checks they generate); `None` for a table whose header declares none.
    label
        The table's short name for people, chosen like `description`; `None` for none.
    label_source
        Where `label` came from (``edited`` or ``project``); `None` without a label.
    label_language
        Which language `label` is in; `None` exactly when there is no label.
    description_language
        Which language `description` is in; `None` exactly when there is no description.
    language
        The language the texts were chosen for: `lang`, else your profile's, else the first of
        Accept-Language, else the instance's main language.
    main_language
        The table's main language: its project's `main_language`, else the instance's; a text
        with no translation is shown in it.
    translations
        Per language that has texts for the table, how many of its main-language texts it has.
    terms
        The glossary terms that link the table itself, in the reader's language; empty without
        ``READ_ENVIRONMENT`` on the environment.
    health
        The effects of the asset's `health:` that differ from `warn`, by ``signal`` or
        ``signal.column`` (``range.price``), sorted by key; empty when every signal warns, and
        for every kind but a managed table.
    health_missing
        The keys of `health` whose column the table does not have: the deploy cannot check the
        columns of a ``SELECT *`` or Python asset. Such an entry has no effect.
    health_never_judged
        The ``block`` column entries of `health` that no commit can judge, by key, with the
        reason (``a string column has no range``, ``only the first 200 columns are judged``): a
        run warns on such an entry instead of blocking.
    owner
        Who owns the table; `None` when the owner was deleted, and for an answer that does not
        look it up (a create or a commit).
    created_at
        When the table was created.
    last_read_at
        When a succeeded query last read it; `None` when none did.
    readers_7d
        How many principals read it in the last 7 UTC days, today included; never their names.
    health_summary
        Its health in one word, as the caller may see it.
    """

    name: str
    namespace: str
    uuid: str
    kind: Literal["TABLE", "EXTERNAL", "VIEW", "ARTIFACT"] = "TABLE"
    metadata_location: str | None
    columns: list[ColumnOut] = Field(serialization_alias="schema")
    sql: str | None = None
    references: list[TableReferenceOut] | None = None
    hidden_references: int = 0
    snapshot_id: int | None = None
    state: Literal["published", "not_published", "kept"] = "published"
    state_message: str | None = None
    description: str | None = None
    description_source: Literal["project", "edited"] | None = None
    last_write_at: datetime | None = None
    row_count: int | None = None
    size_bytes: int | None = None
    file_count: int | None = None
    expires_at: datetime | None = None
    expires_soon: bool = False
    model: TableModelOut | None = None
    label: str | None = None
    label_source: Literal["project", "edited"] | None = None
    label_language: str | None = None
    description_language: str | None = None
    language: str = "en"
    main_language: str = "en"
    translations: list[TranslationCoverageOut] = Field(default_factory=list)
    terms: list[TermRefOut] = []
    health: dict[str, str] = {}
    health_missing: list[str] = []
    health_never_judged: dict[str, str] = {}
    owner: OwnerOut | None = None
    created_at: datetime | None = None
    last_read_at: datetime | None = None
    readers_7d: int = 0
    health_summary: HealthSummaryOut | None = None


class LastMaintenanceOut(BaseModel):
    """The last maintenance run that changed a table.

    Parameters
    ----------
    run_id
        The query run that did it.
    finished_at
        When it ended.
    status
        ``complete``, or ``stopped`` when it ran past its time limit and stopped at a safe point;
        running maintenance again continues.
    snapshots_expired
        Snapshots it expired.
    files_deleted
        Orphaned files it deleted.
    bytes_deleted
        Their total size.
    tags_removed
        Tags of failed checks it removed.
    """

    run_id: uuid.UUID
    finished_at: datetime
    status: Literal["complete", "stopped"]
    snapshots_expired: int
    files_deleted: int
    bytes_deleted: int
    tags_removed: int


class CheckHealthOut(BaseModel):
    """The newest result of one check of a table, as `CheckHealth` holds it.

    Parameters
    ----------
    name
        `CheckHealth.name`.
    check
        `CheckHealth.check`.
    run_id
        `CheckHealth.run_id`.
    outcome
        `CheckHealth.outcome`.
    on_failure
        `CheckHealth.on_failure`.
    failing_rows
        `CheckHealth.failing_rows`.
    error
        `CheckHealth.error`.
    rollback_outcome
        `CheckHealth.rollback_outcome`.
    kept
        `CheckHealth.kept`.
    not_published
        `CheckHealth.not_published`.
    finished_at
        `CheckHealth.finished_at`.
    generated
        Whether the check was generated from the asset's header (`key:`, `references:`)
        rather than written under `checks/`.
    """

    name: str
    check: str
    run_id: uuid.UUID
    outcome: str
    on_failure: str
    failing_rows: int | None
    error: str | None
    rollback_outcome: str | None
    kept: bool
    not_published: bool
    finished_at: datetime
    generated: bool = False


class ModelReferenceOut(BaseModel):
    """One reference a table's header declares, as `ModelReferenceView` holds it.

    Parameters
    ----------
    id
        ``<ns.table>/ref_<column>``: the name of the reference's generated check.
    table
        The dotted name of the table that declares it.
    column
        The referencing column.
    target
        The dotted name of the table it points at, as the header names it.
    target_column
        The column of the target it must match.
    target_exists
        Whether a table of that name exists now. A reference whose target the caller may not
        read is never listed, so this says nothing about such a table.
    at
        The referencing table's time column (`at:`) when the target keeps history (`write:
        history`): each row's value must have a version of the target valid at that time;
        `None` otherwise.
    """

    id: str
    table: str
    column: str
    target: str
    target_column: str
    target_exists: bool
    at: str | None = None


class MeasureOut(BaseModel):
    """One measure a table's header declares, as `MeasureView` holds it.

    Parameters
    ----------
    id
        ``<ns.table>/measure_<column>``.
    column
        The measure's column.
    aggregation
        ``sum``, ``count``, ``min``, ``max``, ``avg``, ``count_distinct`` or ``none``.
    additivity
        ``additive``, ``semi`` (not over `not_over`) or ``non``.
    not_over
        The columns a ``semi`` measure must not be summed over.
    unit
        The unit, or `None` when it has a currency.
    currency
        The ISO 4217 currency, or `None` when it has a unit.
    label
        A short name in the reader's language where it has one, else the main language's, or
        `None`.
    description
        What it means, chosen like `label`, or `None`.
    label_language
        The language of `label`; `None` exactly when `label` is.
    description_language
        The language of `description`; `None` exactly when `description` is.
    """

    id: str
    column: str
    aggregation: str
    additivity: str
    not_over: list[str]
    unit: str | None
    currency: str | None
    label: str | None
    description: str | None
    label_language: str | None = None
    description_language: str | None = None


class TableModelOut(BaseModel):
    """The model a table's asset header declares, as the caller may see it.

    Parameters
    ----------
    kind
        ``fact``, ``dimension``, ``wide`` or ``staging``; `None` when the header names none.
    grain
        What one row is, in one sentence, in the reader's language where it has one, else the
        main language's; `None` when the header says nothing.
    key
        The columns that identify one row; empty for no key and for `key: none`.
    key_none
        Whether the header says `key: none`: no columns are unique, and the grain is not
        checked.
    references
        The references whose target the caller may read, by id.
    hidden_references
        How many references point at a table the caller may not read; they are not listed.
    measures
        Every measure the header declares, by column; a reader of the table sees all of them.
    checks
        The newest result of each check the current header generates, by name.
    grain_language
        The language of `grain`; `None` exactly when `grain` is.
    """

    kind: str | None
    grain: str | None
    key: list[str]
    key_none: bool
    references: list[ModelReferenceOut]
    hidden_references: int
    measures: list[MeasureOut]
    checks: list[CheckHealthOut]
    grain_language: str | None = None


class TableReferencesOut(BaseModel):
    """Response body of `GET .../tables/{name}/references`.

    Parameters
    ----------
    table
        The table's dotted name.
    outgoing
        The references its header declares whose target the caller may read, by id.
    hidden_outgoing
        How many of its references point at a table the caller may not read.
    incoming
        The references other tables' headers declare to it, from tables the caller may read,
        by id.
    hidden_incoming
        How many references to it come from tables the caller may not read; never named.
    """

    table: str
    outgoing: list[ModelReferenceOut]
    hidden_outgoing: int
    incoming: list[ModelReferenceOut]
    hidden_incoming: int


class ReaderOut(BaseModel):
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


class HealthSignalOut(BaseModel):
    """One health signal of a table, as the caller may see it.

    Parameters
    ----------
    id
        The observation's id.
    signal
        ``freshness``, ``volume``, ``null_rate``, ``range`` or ``schema``.
    column
        The column, for a column signal.
    state
        ``ok``, ``fired``, ``no_history``, ``no_metrics`` or ``off``.
    effect
        The effect that applied to the observation.
    snapshot_id
        The snapshot observed.
    observed_at
        When it was recorded.
    observed
        What was observed, keys per signal in the table health reference; `null` when hidden.
    band
        The usual band the observation was judged against; `null` when hidden or none.
    alert
        The text of the newest fired observation (current state) or of this one (history);
        `null` when it did not fire or its values are hidden.
    values_hidden
        Whether a column mask or row filter that applies to the caller left out `observed`,
        `band` and `alert`.
    last_fired_at
        When this signal and column last fired; only in the current state.
    set_back
        Whether a failed check set the observed snapshot back: the row is no longer the table's
        data and shows only in the history, kept as the reason a blocking check gave.
    """

    # jscpd:ignore-start
    # The fields of `lakana_core.catalog.health_read.SignalView`, which this answer is built
    # from attribute by attribute (`from_attributes`): the REST contract names each one itself.
    id: uuid.UUID
    signal: str
    column: str | None
    state: str
    effect: str
    snapshot_id: int | None
    observed_at: datetime
    observed: dict[str, Any] | None
    band: dict[str, Any] | None
    alert: str | None
    values_hidden: bool
    last_fired_at: datetime | None = None
    set_back: bool = False
    # jscpd:ignore-end


class HealthSignalPageOut(BaseModel):
    """One page of the history of a table's health signals, newest first.

    Parameters
    ----------
    items
        The observations.
    next_cursor
        Pass it as `cursor` for the next page; `null` on the last.
    """

    items: list[HealthSignalOut]
    next_cursor: str | None


class TableHealthOut(BaseModel):
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
        Who read the table in the window, most reads first; `None` for a caller who may not see
        the table's grants.
    hidden_checks
        How many results were left out of `checks`: generated reference checks whose target the
        caller may not read.
    signals
        The current state of each health signal, values hidden where a column mask or row filter
        applies to the caller.
    """

    days: int
    last_maintenance: LastMaintenanceOut | None
    checks: list[CheckHealthOut]
    readers: list[ReaderOut] | None
    hidden_checks: int = 0
    signals: list[HealthSignalOut] = []


class DescriptionIn(BaseModel):
    """Request body that sets or resets a description, a label or both, in one language.

    Send one or both; a field left out stays as it is, `null` removes its edit.

    Parameters
    ----------
    description
        The new text (1 to 4,000 characters, not blank), or `null` to remove the edit so that the
        project's text shows again.
    label
        The new label (one line, 1 to 100 characters, not blank), or `null` to remove the edit so
        that the project's label shows again.
    """

    description: str | None = None
    label: str | None = None

    @model_validator(mode="after")
    def _sends_a_field(self) -> DescriptionIn:
        """Refuse a body that changes nothing.

        Returns
        -------
        DescriptionIn
            `self`.

        Raises
        ------
        ValueError
            When neither field was sent.
        """
        if not self.model_fields_set & {"description", "label"}:
            raise ValueError("send `description`, `label` or both; null removes an edit")
        return self


class UndropIn(BaseModel):
    """Request body that restores a dropped table.

    Parameters
    ----------
    new_name
        Restore it under this name in the same namespace, for when a table of its own name
        exists now; `null` (the default) restores it under the name it had.
    break_glass_reason
        Write to a protected environment outside a run; needs `MANAGE_ENVIRONMENT`,
        audited.
    """

    model_config = ConfigDict(extra="forbid")

    new_name: str | None = Field(default=None, max_length=255)
    break_glass_reason: str | None = Field(default=None, max_length=500)


class DroppedTableOut(BaseModel):
    """One dropped table whose files are not deleted yet.

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
        From when on the worker's sweep deletes its files; it can be restored until then.
    restorable
        Whether `POST .../tables/{name}/undrop` would still restore it: `false` once
        `purge_after` has passed or the sweep has started deleting its files.
    """

    name: str
    namespace: str
    uuid: str
    kind: str
    dropped_at: datetime
    purge_after: datetime
    restorable: bool


class LineageNodeOut(BaseModel):
    """One node of a lineage graph.

    Parameters
    ----------
    id
        The table's id, or the connection's.
    name
        Its dotted name, or the connection's name.
    kind
        ``TABLE``, ``EXTERNAL``, ``VIEW``, ``ARTIFACT`` or ``CONNECTION``; `None` once a
        table is dropped. A ``CONNECTION`` is a connection the writing asset declared, shown
        only to a reader of the environment's connections.
    dropped
        Whether the table or connection no longer exists.
    connection_kind
        For a ``CONNECTION``, its kind (for example ``postgresql``); `None` otherwise and once
        the connection is deleted.
    """

    id: str
    name: str
    kind: str | None
    dropped: bool
    connection_kind: str | None = None


class LineageEdgeOut(BaseModel):
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


class MaintenanceIn(BaseModel):
    """Request body for table maintenance.

    Parameters
    ----------
    keep_days
        Snapshots younger than this many days are kept.
    keep_last
        The most recent this many snapshots are always kept.
    dry_run
        Report what would happen without changing anything.
    older_than_hours
        Unreferenced files younger than this many hours are never deleted;
        1 to 87600 (ten years).
    """

    model_config = ConfigDict(extra="forbid")

    keep_days: int = Field(default=DEFAULT_KEEP_DAYS, ge=0)
    keep_last: int = Field(default=DEFAULT_KEEP_LAST, ge=1)
    dry_run: bool = False
    older_than_hours: int = Field(
        default=DEFAULT_ORPHAN_AGE_HOURS, ge=MIN_ORPHAN_AGE_HOURS, le=MAX_ORPHAN_AGE_HOURS
    )


class LineageOut(BaseModel):
    """Response body for a table's lineage.

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
        One edge per source/target pair, the most recent.
    hidden_nodes
        Nodes omitted because the caller can't read them.
    truncated
        Whether the walk stopped at the node cap with a readable node left
        out. A node the caller can't read never sets it.
    """

    root_id: str
    direction: str
    depth: int
    nodes: list[LineageNodeOut]
    edges: list[LineageEdgeOut]
    hidden_nodes: int
    truncated: bool


class PrincipalCreate(BaseModel):
    """Request body for creating a principal.

    Parameters
    ----------
    name
        Unique principal name.
    type
        Whether this is a human user or a service account.
    """

    name: str
    type: Literal["USER", "SERVICE_ACCOUNT"]


class PrincipalOut(BaseModel):
    """Response body for a principal.

    Parameters
    ----------
    name
        Principal name.
    type
        Whether this is a human user or a service account.
    is_active
        Whether this principal can currently authenticate.
    picture
        The etag of the principal's profile picture (`GET /api/v1/principals/{name}/picture`),
        or `null` without one; it changes whenever the picture does.
    """

    name: str
    type: str
    is_active: bool
    picture: str | None = None

    model_config = {"from_attributes": True}


class PictureOut(BaseModel):
    """Response body for `PUT /api/v1/principals/{name}/picture`.

    Parameters
    ----------
    etag
        The stored picture's etag: the SHA-256 of the re-encoded WebP, in hex.
    """

    etag: str


class GroupCreate(BaseModel):
    """Request body for creating a group.

    Parameters
    ----------
    name
        Unique group name.
    """

    name: str


class GroupOut(BaseModel):
    """Response body for a group.

    Parameters
    ----------
    name
        Group name.
    """

    name: str

    model_config = {"from_attributes": True}


class TokenIssueIn(BaseModel):
    """Request body for issuing a PAT.

    Parameters
    ----------
    name
        A human label for this PAT (e.g. "laptop", "CI").
    """

    name: str


class TokenIssueOut(BaseModel):
    """Response body for a freshly issued PAT.

    Parameters
    ----------
    key_id
        The public lookup key.
    name
        The label given at issuance.
    token
        The full token value. Shown here exactly once — never retrievable
        again.
    """

    key_id: str
    name: str
    token: str


class TokenOut(BaseModel):
    """Response body for a listed PAT. Never includes a secret.

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
    last_used_at: datetime | None = None

    model_config = {"from_attributes": True}


class TaskTokenMintIn(BaseModel):
    """Request body for minting a task token.

    Parameters
    ----------
    principal
        The identity the token authenticates as. At most 255 characters (the
        `principals.name` column), because a refused mint records it in the
        audit log.
    environment
        The one environment this task may touch.
    asset_scope
        The declared asset name/pattern strings.
    ttl_seconds
        Seconds until the token expires, from now. Bounded to `> 0` here, at
        the HTTP boundary, since an already-dead (non-positive) TTL is
        never a sensible request. The *upper* bound is deliberately not
        enforced here: `TaskTokenService.mint` is the single source of that
        cap (`max_ttl_seconds`, operator-configured via
        `LAKANA_TASK_TOKENS__MAX_TTL_SECONDS`), so raising the operator's
        configured cap above this schema's own default actually takes
        effect, instead of being silently overridden by a value hard-coded
        here. `TaskTokenService.mint` itself accepts any
        `int`, including negative values, for tests that construct an
        already-expired token by calling the service directly.
    """

    principal: str = Field(max_length=255)
    environment: str
    asset_scope: list[str]
    ttl_seconds: int = Field(gt=0)


class TaskTokenMintOut(BaseModel):
    """Response body for a freshly minted task token.

    Parameters
    ----------
    key_id
        The public lookup key.
    token
        The full token value. Shown here exactly once.
    """

    key_id: str
    token: str


class GrantCreate(BaseModel):
    """Request body for creating a grant.

    Exactly one of `principal`/`group` and exactly one of `role`/`action`
    must be given; `GrantService.grant` raises `ValidationError` otherwise.
    `environment`/`namespace`/`table` must match what `kind` expects — an
    ENVIRONMENT-kind grant requires `environment` and forbids `namespace`/
    `table`, and so on; `GrantService.grant` validates this too.

    Parameters
    ----------
    principal
        Name of the principal to grant to, or `None` if granting to a group.
    group
        Name of the group to grant to, or `None` if granting to a principal.
    role
        A role template to grant, or `None` if granting a raw action.
    action
        A raw action to grant, or `None` if granting a role.
    kind
        Which tier of the securable hierarchy this grant applies to.
    environment
        The environment, for an ENVIRONMENT/NAMESPACE/TABLE-scoped grant.
    namespace
        The dot-joined namespace path, such as ``gold.sales``, for a
        NAMESPACE/TABLE-scoped grant.
    table
        The table name, for a TABLE-scoped grant.
    """

    principal: str | None = None
    group: str | None = None
    role: Role | None = None
    action: Action | None = None
    kind: SecurableKind
    environment: str | None = None
    namespace: str | None = None
    table: str | None = None


class GrantOut(BaseModel):
    """Response body for a grant.

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
        The principal who created this grant.
    granted_at
        When this grant was created.
    note
        ``no effect: <ACTION> needs the ADMIN role`` for a single-action grant
        of `MANAGE_GRANT`, `MANAGE_GROUP` or `MANAGE_TOKEN`, which work only
        with the `ADMIN` role on the instance; `None` otherwise.
    """

    id: uuid.UUID
    principal_id: uuid.UUID | None
    group_id: uuid.UUID | None
    role: Role | None
    action: Action | None
    kind: SecurableKind
    environment: str | None
    namespace: str | None
    table: str | None
    granted_by: uuid.UUID
    granted_at: datetime
    note: str | None = None


# jscpd:ignore-start
# The SDK (`lakana._models`) mirrors these field for field; it may not import the server.
class ScopeOut(BaseModel):
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

    kind: SecurableKind
    environment: str | None
    namespace: str | None
    table: str | None


class ViaOut(BaseModel):
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

    kind: Literal["grant", "role", "group"]
    grant_id: uuid.UUID
    group: str | None
    role: Role | None
    scope: ScopeOut


class PermissionOut(BaseModel):
    """Whether a principal may do one action on the object, and why.

    Parameters
    ----------
    action
        The action.
    allowed
        The answer of the authorizer the routes use.
    via
        The grants that give it, oldest first; empty when refused.
    reason
        Why it is refused, or `None` when allowed.
    """

    action: Action
    allowed: bool
    via: list[ViaOut]
    reason: str | None


class PermissionsOut(BaseModel):
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
        One per action the routes ask for on an object of this kind.
    note
        Set for a deactivated principal: its tokens no longer work, whatever its grants allow.
    """

    principal: str
    active: bool
    scope: ScopeOut
    permissions: list[PermissionOut]
    note: str | None


# jscpd:ignore-end


class RowFilterCreate(BaseModel):
    """Request body for creating a row filter.

    Exactly one of `principal`/`group` must be given;
    `RowFilterService.create` raises `ValidationError` otherwise, and
    validates `expression` against the restricted subset.

    Parameters
    ----------
    principal
        Name of the principal this filter applies to, or `None` if
        applying to a group.
    group
        Name of the group this filter applies to, or `None` if applying to
        a principal.
    environment
        The environment the filtered table lives in.
    namespace
        The dot-joined namespace path, such as ``gold.sales``.
    table
        The table name.
    expression
        The restricted-subset predicate.
    """

    principal: str | None = None
    group: str | None = None
    environment: str
    namespace: str
    table: str
    expression: str


class RowFilterOut(BaseModel):
    """Response body for a row filter.

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
        The restricted-subset predicate, in the table's current column names.
    created_by
        The principal who created this filter.
    created_at
        When this filter was created.
    bound
        `true` when the policy follows its columns by field id (a raw table's, by name). `false`
        right after an upgrade, while the worker binds earlier policies over the next passes (see
        the upgrade notes); only a policy that stays `false` after that needs creating again.
    """

    id: uuid.UUID
    principal_id: uuid.UUID | None
    group_id: uuid.UUID | None
    environment: str
    namespace: str
    table: str
    expression: str
    created_by: uuid.UUID
    created_at: datetime
    bound: bool


class ColumnMaskCreate(BaseModel):
    """Request body for creating a column mask.

    Exactly one of `principal`/`group` must be given;
    `ColumnMaskService.create` raises `ValidationError` otherwise, and
    validates `expression` against the mask grammar (the restricted subset
    plus `substr`/`substring`/`||`/`md5`).

    Parameters
    ----------
    principal
        Name of the principal this mask applies to, or `None` if applying
        to a group.
    group
        Name of the group this mask applies to, or `None` if applying to a
        principal.
    environment
        The environment the masked table lives in.
    namespace
        The dot-joined namespace path, such as ``gold.sales``.
    table
        The table name.
    column
        The masked column's name.
    expression
        The replacement value.
    """

    principal: str | None = None
    group: str | None = None
    environment: str
    namespace: str
    table: str
    column: str
    expression: str


class ColumnMaskOut(BaseModel):
    """Response body for a column mask.

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
        The masked column's name, as the table has it now.
    expression
        The replacement value.
    created_by
        The principal who created this mask.
    created_at
        When this mask was created.
    bound
        As `RowFilterOut.bound`.
    """

    id: uuid.UUID
    principal_id: uuid.UUID | None
    group_id: uuid.UUID | None
    environment: str
    namespace: str
    table: str
    column: str
    expression: str
    created_by: uuid.UUID
    created_at: datetime
    bound: bool


def _check_secret_values(value: object) -> object:
    """Refuse a secret's `values` that is not a flat text-to-text object, naming no key.

    pydantic puts a mapping key into the location of its error, and a secret typed into
    the wrong place can be that key; the error here names neither a key nor a value.

    Parameters
    ----------
    value
        What the client sent as `values`.

    Returns
    -------
    object
        `value`, unchanged, when every key and value is a string.

    Raises
    ------
    pydantic_core.PydanticCustomError
        If `value` is not an object or any of its values is not a string.
    """
    if not isinstance(value, dict) or not all(isinstance(item, str) for item in value.values()):
        raise PydanticCustomError(
            "secret_values",
            "values must be an object whose entries are all text, such as "
            '{"access_key_id": "..."}; send each entry as a string.',
        )
    return value


SecretValues = Annotated[dict[str, str], BeforeValidator(_check_secret_values)]
"""A secret's plaintext key-value bag; a bad shape is refused without echoing any of it."""


class SecretBind(BaseModel):
    """Request body for binding a secret to its use.

    Parameters
    ----------
    kind
        ``package_index``, ``repository``, ``connection`` or ``environment``.
    target
        The index's, repository's or connection's name; absent or null for the environment.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["package_index", "repository", "connection", "environment"]
    target: str | None = None


class SecretCreate(BaseModel):
    """Request body for creating a secret.

    Parameters
    ----------
    name
        The secret's name, unique within its environment.
    values
        The plaintext key-value bag to encrypt and store. Never echoed
        back in any response.
    use
        What the secret is for, so that it is bound as it is created; absent leaves it unbound.
    """

    name: str
    values: SecretValues
    use: SecretBind | None = None


class SecretRotate(BaseModel):
    """Request body for rotating a secret.

    Parameters
    ----------
    values
        The new plaintext key-value bag.
    """

    values: SecretValues


class SecretOut(BaseModel):
    """Response body for a secret. Structurally cannot carry a value or ciphertext field.

    Parameters
    ----------
    id
        The secret's id.
    name
        The secret's name.
    environment
        The environment this secret belongs to.
    created_by
        The principal who created this secret.
    created_at
        When this secret was created.
    format_version
        How the stored secret is encrypted. 1 is the original format; a
        secret still at 1 becomes the current format when it is set again.
    key_id
        Which key provider and key wrapped the secret's data key. Safe to
        show: it does not reveal the key.
    use
        What the secret is bound to: ``package_index``, ``repository``, ``environment``, or
        `None` for an unbound secret.
    use_target
        The index's or repository's name; `None` otherwise, also when that entry was removed.
    use_target_url
        Its URL, which is where the secret is sent (for a connection, every ``host:port`` it
        sends the secret to, joined by ``, ``); `None` when `use_target` is.
    """

    id: uuid.UUID
    name: str
    environment: str
    created_by: uuid.UUID
    created_at: datetime
    format_version: int
    key_id: str
    use: str | None = None
    use_target: str | None = None
    use_target_url: str | None = None


class AuditEventOut(BaseModel):
    """Response body for one audit event.

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

    id: uuid.UUID
    occurred_at: datetime
    principal_id: uuid.UUID | None
    event_type: AuditEventType
    securable_kind: SecurableKind | None
    securable_environment: str | None
    securable_namespace: str | None
    securable_table: str | None
    detail: dict


class AuditEventPage(BaseModel):
    """One page of audit events, newest first.

    Parameters
    ----------
    items
        The events on this page.
    next_cursor
        Pass back as `?cursor=` to fetch the next page; `None` when this is
        the last page.
    """

    items: list[AuditEventOut]
    next_cursor: str | None


class TaskTokenScopeOut(BaseModel):
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


class CiTokenScopeOut(BaseModel):
    """What a CI token caller may do; never the token itself.

    Parameters
    ----------
    rule_id
        The CI trust rule the token was issued under.
    environment
        The one environment it may deploy to.
    repository
        The one repository it may deploy.
    kinds
        What it may queue, once: ``deploy`` and/or ``dry_run``, sorted.
    sha
        The commit a deploy is pinned to, or `None`.
    expires_at
        When the token stops working.
    used
        Whether it has already queued its one deploy or dry run.
    """

    rule_id: uuid.UUID
    environment: str
    repository: str
    kinds: list[str]
    sha: str | None
    expires_at: datetime | None
    used: bool


class WhoamiOut(BaseModel):
    """Response body for `GET /api/v1/whoami`.

    Parameters
    ----------
    name
        The caller's principal name.
    type
        The caller's principal type.
    task_token
        The task token's scope when the caller authenticated with one;
        `None` for a PAT.
    ci_token
        The CI token's scope when the caller authenticated with one; `None` otherwise.
    language
        Your preferred language for catalog texts; `null` for none.
    admin
        Whether you hold the `ADMIN` role on the instance (directly or through a group).
    has_access
        Whether you have any grant at all, at any scope, directly or through a group; `false`
        for a new person nobody has granted anything yet.
    may_deploy
        Whether you may deploy in at least one environment (`DEPLOY`, directly, through a role
        or a group); the web interface shows the deploy steps and Your work from it.
    picture
        The etag of your profile picture, or `null` without one.
    """

    name: str
    type: Literal["USER", "SERVICE_ACCOUNT"]
    task_token: TaskTokenScopeOut | None
    ci_token: CiTokenScopeOut | None = None
    language: str | None = None
    admin: bool = False
    has_access: bool = False
    may_deploy: bool = False
    picture: str | None = None


class ProfileIn(BaseModel):
    """Request body that sets or resets the caller's preferred language.

    Parameters
    ----------
    language
        A language tag such as `fr` or `de-CH`, or `null` to remove the preference. Must be sent.
    """

    language: str | None


class InterfaceStateOut(BaseModel):
    """What the web interface remembers for you.

    Parameters
    ----------
    tour_seen
        Whether you finished or skipped the tour; `false` until then, so it shows on your next
        sign-in.
    get_started_hidden
        Whether you hid Get started on Home.
    update_note_seen
        The last release whose update note you saw (`x.y.z`), or `null`.
    """

    tour_seen: bool
    get_started_hidden: bool
    update_note_seen: str | None


class InterfaceStateIn(BaseModel):
    """Request body that changes what the web interface remembers for you.

    Every field is optional: a field left out stays as it is. A refused request changes
    nothing, so send `tour_seen` on its own whenever `update_note_seen` might be refused.

    Parameters
    ----------
    tour_seen
        `true` once you finished or skipped the tour, `false` to see it again.
    get_started_hidden
        `true` to hide Get started, `false` to show it again.
    update_note_seen
        The release whose update note you saw, as `x.y.z` (for example `0.23.0`); a development
        build's version is refused (422). `null` clears it.
    """

    model_config = ConfigDict(extra="forbid")

    tour_seen: bool | None = None
    get_started_hidden: bool | None = None
    update_note_seen: str | None = Field(default=None, max_length=64)


class RecentIn(BaseModel):
    """Request body that records that you opened an object.

    Parameters
    ----------
    kind
        `table`, `query`, `job` or `run`.
    environment
        The object's environment.
    ref
        The object: a dotted table name (`gold.sales`), a query's or a run's id, or
        `repository/job`.
    """

    kind: Literal["table", "query", "job", "run"]
    environment: str = Field(max_length=255)
    ref: str = Field(max_length=1024)


class RecentOut(BaseModel):
    """One object you opened recently.

    Parameters
    ----------
    kind
        `table`, `query`, `job` or `run`.
    environment
        The object's environment.
    ref
        The object, as it was recorded.
    opened_at
        When you last opened it.
    """

    kind: Literal["table", "query", "job", "run"]
    environment: str
    ref: str
    opened_at: datetime


class GetStartedStepOut(BaseModel):
    """One row of Get started.

    Parameters
    ----------
    id
        The step: `connect_repository`, `invite_team`, `add_passkey`, `deploy_project`,
        `deploy_project_local`, `ci_deploy`, `open_table` or `run_query`.
    done
        Whether the server's state shows it done.
    """

    id: Literal[
        "connect_repository",
        "invite_team",
        "add_passkey",
        "deploy_project",
        "deploy_project_local",
        "ci_deploy",
        "open_table",
        "run_query",
    ]
    done: bool


class GetStartedOut(BaseModel):
    """Your Get started list on Home.

    Parameters
    ----------
    audience
        `admin`, `deployer` (may deploy somewhere), `reader` (has other access) or `none` (no
        access yet: the interface says to ask an administrator).
    local
        Whether the server runs in local mode (`lakana server start --local`), which leaves out
        what needs a team: inviting, CI, connecting a repository.
    steps
        The rows in order, each with whether it is done; empty for `none`.
    hidden
        Whether you hid the list.
    complete
        Whether every step is done; the interface then shows the list no more.
    """

    audience: Literal["admin", "deployer", "reader", "none"]
    local: bool
    steps: list[GetStartedStepOut]
    hidden: bool
    complete: bool


class AttentionItemOut(BaseModel):
    """One row of Needs your attention on Home.

    Parameters
    ----------
    kind
        `run_failed` (a job's newest finished run failed), `table_late` (its freshness signal
        fired), `check_failed` (a check's newest result failed or errored), `query_failed`
        (one of your own queries failed), `recovery_link_open` or `worker_lost`; the last two,
        and failed runs, only for administrators.
    environment
        The environment's name; `null` for a recovery link and a worker.
    title
        The object's name: a job, a dotted table name, a check, `Query` and the first eight
        characters of its id, the person a recovery link signs in, a worker's host. Never a
        value, an error text or SQL.
    ref
        The object the row opens: a run's id (a failed run or check), a dotted table name, a
        query's id, a recovery link's id or a worker's id.
    at
        When it happened.
    """

    kind: AttentionKind
    environment: str | None
    title: str
    ref: str
    at: datetime


class AttentionOut(BaseModel):
    """Needs your attention on Home.

    Parameters
    ----------
    items
        At most 50 rows, newest first; empty when nothing needs you.
    truncated
        Whether more rows were left out.
    """

    items: list[AttentionItemOut]
    truncated: bool


class WorkEnvironmentOut(BaseModel):
    """Where one of your repositories is deployed.

    Parameters
    ----------
    environment
        The environment's name.
    last_deploy_status
        How the newest deploy there ended: `queued`, `running`, `succeeded` or `failed`;
        `null` when no deploy run is recorded.
    last_deploy_at
        When that deploy ended, or was requested while it has not.
    """

    environment: str
    last_deploy_status: str | None
    last_deploy_at: datetime | None


class WorkRepositoryOut(BaseModel):
    """A repository you created or deployed.

    Parameters
    ----------
    name
        The repository's name.
    environments
        The environments you may read where it is deployed, by name.
    """

    name: str
    environments: list[WorkEnvironmentOut]


class WorkJobOut(BaseModel):
    """A job of one of your repositories.

    Parameters
    ----------
    environment
        The environment's name.
    repository
        The repository's name.
    job
        The job's name.
    last_run_status
        The state of its newest run; `null` before the first.
    last_run_at
        When that run was started.
    """

    environment: str
    repository: str
    job: str
    last_run_status: str | None
    last_run_at: datetime | None


class WorkOut(BaseModel):
    """Your work on Home: only for people who may deploy somewhere.

    Parameters
    ----------
    repositories
        At most 20, the most recently deployed (else created) first.
    jobs
        At most 20 of their jobs, the most recently run first.
    """

    repositories: list[WorkRepositoryOut]
    jobs: list[WorkJobOut]


class EnvironmentStorageUpdate(BaseModel):
    """Request body for updating an environment's storage configuration.

    Parameters
    ----------
    options
        Replaces the environment's storage options entirely, if given.
    secret_ref
        Replaces the environment's storage secret reference, if given.
        Setting this requires `MANAGE_SECRET` on the environment, not just
        `MANAGE_ENVIRONMENT` — see `CatalogService.update_environment_storage`.
    """

    options: OptionalStorageOptions = None
    secret_ref: str | None = None


class RepositoryCreate(BaseModel):
    """Request body for registering a repository.

    Parameters
    ----------
    name
        Unique name; no dots.
    kind
        ``local``, ``https`` or ``ssh``.
    url
        ``https://…``, ``ssh://…``, ``[user@]host:path``, or an absolute path.
    credential_secret
        Secret name resolved in the target environment at deploy time.
    credential_username
        HTTPS username; default ``oauth2``.
    ssh_known_hosts
        ``known_hosts`` lines; required for ``ssh``.
    default_ref
        Ref deployed when none is given.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: Literal["local", "https", "ssh"]
    url: str
    credential_secret: str | None = None
    credential_username: str | None = None
    ssh_known_hosts: str | None = None
    default_ref: str | None = None


class RepositoryUpdate(BaseModel):
    """Request body for changing a repository; omitted fields stay, `null` clears.

    Parameters
    ----------
    url
        New URL.
    credential_secret
        New secret name.
    credential_username
        New HTTPS username.
    ssh_known_hosts
        New ``known_hosts`` lines.
    default_ref
        New default ref.
    """

    model_config = ConfigDict(extra="forbid")

    url: str | None = None
    credential_secret: str | None = None
    credential_username: str | None = None
    ssh_known_hosts: str | None = None
    default_ref: str | None = None


class RepositoryDeployKeyOut(BaseModel):
    """The public half of one environment's deploy key.

    Parameters
    ----------
    environment
        The environment whose secret holds the private half.
    public_key
        ``ssh-ed25519 <base64> <comment>``: add it at the git host as a read-only deploy key.
    fingerprint
        ``SHA256:…``, as `ssh-keygen -l` and the git host show it.
    """

    environment: str
    public_key: str
    fingerprint: str


class RepositoryOut(BaseModel):
    """Response body for a repository. Carries a secret's name, never a value.

    Parameters
    ----------
    id
        The repository's id.
    name
        Its name.
    kind
        ``local``, ``https`` or ``ssh``.
    url
        Its URL.
    credential_secret
        The bound secret's name.
    credential_username
        HTTPS username, or `None` for ``oauth2``.
    ssh_known_hosts
        Trusted host keys.
    default_ref
        Default ref.
    created_by
        Who registered it.
    created_at
        When.
    updated_at
        Last change.
    deploy_keys
        The public deploy keys Lakana made for it, one per environment you may read, to add at
        the git host; empty when it names no such key. Never a private key.
    """

    id: uuid.UUID
    name: str
    kind: str
    url: str
    credential_secret: str | None
    credential_username: str | None
    ssh_known_hosts: str | None
    default_ref: str | None
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    deploy_keys: list[RepositoryDeployKeyOut] = []


class DeployKeyIn(BaseModel):
    """Request body for making deploy keys; every field is optional.

    Parameters
    ----------
    secret
        The secret name the keys are stored under; ``<repository>-deploy-key`` by default.
    environments
        Only these environments; by default every environment where you may manage secrets
        and the repository has no key yet.
    replace
        Replace the keys of `environments` (required with it). The old key of each stops
        working.
    """

    model_config = ConfigDict(extra="forbid")

    secret: str | None = Field(default=None, max_length=255)
    environments: list[str] | None = Field(default=None, max_length=100)
    replace: bool = False


class DeployKeyMadeOut(RepositoryDeployKeyOut):
    """One environment's deploy key in the answer of a call that makes keys.

    Parameters
    ----------
    environment
        The environment whose secret holds the private half.
    public_key
        ``ssh-ed25519 <base64> <comment>``.
    fingerprint
        ``SHA256:…``.
    made
        Whether this call made it; `false` for a key made earlier, listed unchanged.
    """

    made: bool


class DeployKeyOut(BaseModel):
    """Response body of a call that makes deploy keys. Never carries a private key.

    Parameters
    ----------
    secret
        The secret name every key is stored under; now the repository's credential.
    keys
        One per environment the call wrote or found a key in, by environment name.
    skipped
        Environments without a key where you may not manage secrets: someone who may makes
        theirs.
    replaced
        Environments whose key this call replaced.
    notes
        For each replaced key: the old key stops working, replace it at the git host now.
    """

    secret: str
    keys: list[DeployKeyMadeOut]
    skipped: list[str]
    replaced: list[str]
    notes: list[str]


class PackageIndexCreate(BaseModel):
    """Request body for registering a package index.

    Parameters
    ----------
    name
        Unique name; no dots.
    url
        ``https://host/path`` of the index, without a user name or password.
    credential_secret
        Name of the secret that holds ``username`` and ``password``, per environment.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    url: str
    credential_secret: str | None = None


class PackageIndexUpdate(BaseModel):
    """Request body for changing a package index; omitted fields stay, `null` clears the secret.

    Parameters
    ----------
    url
        New URL.
    credential_secret
        New secret name.
    """

    model_config = ConfigDict(extra="forbid")

    url: str | None = None
    credential_secret: str | None = None


class PackageIndexOut(BaseModel):
    """Response body for a package index. Carries a secret's name, never a value.

    Parameters
    ----------
    id
        The index's id.
    name
        Its name.
    url
        Its URL, in stored form.
    credential_secret
        The bound secret's name.
    created_by
        Who registered it.
    created_at
        When.
    updated_at
        Last change.
    """

    id: uuid.UUID
    name: str
    url: str
    credential_secret: str | None
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


def _check_connection_secret_values(value: object) -> object:
    """Refuse `secret_values` that is not a flat text-to-text object, naming no key.

    As `_check_secret_values`, with an example that fits every kind of connection.

    Parameters
    ----------
    value
        What the client sent as `secret_values`.

    Returns
    -------
    object
        `value`, unchanged, when every value is a string.

    Raises
    ------
    pydantic_core.PydanticCustomError
        If `value` is not an object or any of its values is not a string.
    """
    if not isinstance(value, dict) or not all(isinstance(item, str) for item in value.values()):
        raise PydanticCustomError(
            "secret_values",
            "secret_values must be an object whose entries are all text, such as "
            '{"password": "..."}; send each entry as a string.',
        )
    return value


ConnectionSecretValues = Annotated[
    dict[str, SecretStr], BeforeValidator(_check_connection_secret_values)
]
"""New values of a connection's secret; never echoed, a bad shape refused without repeating it."""

_CONNECTION_KIND = Field(
    description="The kind of source.",
    json_schema_extra={"enum": [kind.value for kind in ConnectionKind]},
)


class ConnectionCreate(BaseModel):
    """Request body for creating a connection.

    `kind`, `address` and `settings` are judged by the service after the right is checked, so a
    caller without `MANAGE_CONNECTION` learns nothing from a 422.

    Parameters
    ----------
    name
        Lower-case letters, digits and ``-``, starting with a letter, at most 63; unique in
        the environment. Never changes.
    kind
        ``postgresql``, ``mariadb``, ``sqlite-file``, ``http``, ``s3``, ``gcs``, ``azure``,
        ``directory`` or ``tcp``. Never changes.
    address
        Where the source is, by kind (``host``, ``port``, ``database``; ``base_url``; ...).
    settings
        How to reach it, by kind (``user``, ``tls``, ``ca_certificate``; ``auth``; ...).
    secret
        The name of an existing secret of the environment to use.
    secret_values
        Values of a new secret ``<name>-connection`` (or the next free
        ``<name>-connection-N``), born bound to the connection. Never echoed. At most one of
        `secret` and `secret_values`.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: str = _CONNECTION_KIND
    address: dict[str, Any]
    settings: dict[str, Any] = Field(default_factory=dict)
    secret: str | None = None
    secret_values: ConnectionSecretValues | None = None


class ConnectionUpdate(BaseModel):
    """Request body for changing a connection; only what is given changes.

    Parameters
    ----------
    address
        Address keys to change; a key set to `null` is removed.
    settings
        Settings keys to change; a key set to `null` is removed.
    secret
        The name of a secret of the environment to use instead.
    secret_values
        New values: they set the connection's own secret again, or create
        ``<name>-connection`` (or the next free name) when it has none. Never echoed.
    clear
        ``secret`` (stop using the secret and unbind it) and settings keys to remove.
    """

    model_config = ConfigDict(extra="forbid")

    address: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None
    secret: str | None = None
    secret_values: ConnectionSecretValues | None = None
    clear: list[str] = Field(default_factory=list)


class DeclaringAssetOut(BaseModel):
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


class ConnectionOut(BaseModel):
    """Response body for a connection. Carries its secret's name, never a value.

    Parameters
    ----------
    name
        Its name.
    kind
        Its kind.
    address
        Its address, with defaults filled in.
    settings
        Its settings, with defaults filled in; a ``ca_certificate`` is shown as its SHA-256
        fingerprints (``sha256:<hex>``), never as the certificate.
    secret
        The name of the secret it uses, or `null`.
    declared_by
        The assets whose deployed version declares it; a delete is refused while any does.
    created_by
        The name of the principal who created it.
    created_at
        When it was created.
    updated_at
        When it was last changed.
    """

    name: str
    kind: str
    address: dict[str, Any]
    settings: dict[str, Any]
    secret: str | None
    declared_by: list[DeclaringAssetOut]
    created_by: str
    created_at: datetime
    updated_at: datetime


class ConnectionPage(BaseModel):
    """One page of an environment's connections, newest first.

    Parameters
    ----------
    items
        The connections of this page.
    next_cursor
        Pass back as `?cursor=` to fetch the next page; `null` on the last page.
    """

    items: list[ConnectionOut]
    next_cursor: str | None


class TermLinkOut(BaseModel):
    """One table, column or measure a glossary term points to.

    Parameters
    ----------
    table
        The dotted table name, such as `silver.orders`.
    column
        The column or measure name; `null` for a link to the whole table.
    kind
        `table`, `column` or `measure`.
    missing
        `true` when the table does not exist in the environment (yet), or when the table no
        longer has the linked column.
    """

    table: str
    column: str | None
    kind: Literal["table", "column", "measure"]
    missing: bool


class RelatedTermOut(BaseModel):
    """A term another term names as related.

    Parameters
    ----------
    key
        The related term's id.
    name
        Its name in the reader's language; `null` when no such term exists.
    missing
        `true` when no term of that id exists in the environment.
    """

    key: str
    name: str | None
    missing: bool


class TermOut(BaseModel):
    """A business glossary term, with its texts in the reader's language.

    A link appears only when the caller holds `READ_TABLE` on its table, except a link to a
    table that does not exist, which shows as `missing` (as does a link to a column a readable
    table no longer has).

    Parameters
    ----------
    key
        The term's id in the glossary.
    id
        The term's UUID, stable across deploys.
    name
        The term's name in `language`.
    definition
        What the term means, in `language`.
    synonyms
        Other names for the term.
    owner
        Who answers for the definition.
    repository
        The repository whose glossary defines the term.
    language
        The language of `name` and `definition`.
    links
        What the term points to that the caller may read.
    related
        The terms this term names as related.
    related_by
        The ids of the terms that name this term as related.
    """

    key: str
    id: str
    name: str
    definition: str
    synonyms: list[str]
    owner: str
    repository: str
    language: str
    links: list[TermLinkOut]
    related: list[RelatedTermOut]
    related_by: list[str]


class TermPage(BaseModel):
    """One page of an environment's glossary terms, sorted by key.

    Parameters
    ----------
    items
        The terms of this page.
    next_cursor
        Pass back as `?cursor=` to fetch the next page; `null` on the last page.
    """

    items: list[TermOut]
    next_cursor: str | None


class GraphNodeOut(BaseModel):
    """A node of the metadata graph, with the attributes the caller may see.

    Parameters
    ----------
    id
        Its stable id (`table:<uuid>`, `column:<uuid>/<name>`, `asset:<repository>/<name>`,
        `reference:<uuid>`, `measure:<uuid>`, `term:<uuid>`, `check:<uuid>/<name>`,
        `row_filter:<uuid>`, `column_mask:<uuid>`, `job:<repository>/<name>`); store it and pass
        it back as `node`.
    kind
        `table`, `column`, `asset`, `reference`, `measure`, `term`, `check`, `row_filter`,
        `column_mask` or `job`.
    name
        A name to read: a dotted table, `<table>.<column>`, a term's key.
    distance
        Steps from the start: assets crossed for lineage, 1 for a neighbour.
    table_type
        A table's `managed`, `external`, `view` or `artifact`.
    model_kind
        A table's declared kind.
    grain
        A table's grain, in your language.
    key
        A table's declared key columns.
    owner
        The owner of a table's asset (`None` for a reader without ``READ_ENVIRONMENT``), an
        asset or a term.
    repository
        The repository that declares it (or builds its table).
    label
        A table's label, or a term's name, in your language.
    column_type
        A column's type.
    reason
        Always `null` for lineage and neighbours.
    may_use
        Always `false` for lineage and neighbours.
    """

    id: str
    kind: str
    name: str
    distance: int
    table_type: str | None = None
    model_kind: str | None = None
    grain: str | None = None
    key: list[str] | None = None
    owner: str | None = None
    repository: str | None = None
    label: str | None = None
    column_type: str | None = None
    reason: str | None = None
    may_use: bool = False


class GraphEdgeOut(BaseModel):
    """An edge of the metadata graph, from `source` to `target` (both node ids).

    Parameters
    ----------
    kind
        `reads`, `builds`, `references`, `measures`, `checks`, `filters`, `masks`, `links`,
        `runs` or `has_column`.
    source
        The node it leaves.
    target
        The node it reaches.
    """

    kind: str
    source: str
    target: str


class GraphOut(BaseModel):
    """One page of a metadata graph answer: at most 500 nodes, sorted by distance, then id.

    Parameters
    ----------
    nodes
        The nodes of this page; the start is the first node of the first page.
    edges
        The edges whose later endpoint is on this page; all pages together carry each edge once.
    next_cursor
        Pass back as `?cursor=` with the same question for the next page; `null` on the last.
    """

    nodes: list[GraphNodeOut]
    edges: list[GraphEdgeOut]
    next_cursor: str | None


class JoinStepOut(BaseModel):
    """One step of a join path.

    Parameters
    ----------
    reference
        The reference it follows (`reference:<uuid>`).
    from_table
        The table it leaves (`table:<uuid>`).
    from_name
        Its dotted name.
    to_table
        The table it reaches.
    to_name
        Its dotted name.
    from_columns
        The columns of `from_table` to join on.
    to_columns
        The columns of `to_table` they equal, in the same order.
    direction
        `many_to_one` along the reference (from the table that declares it), `one_to_many`
        against it.
    fans_out
        `true` for a `one_to_many` step: each row of `from_table` may meet many rows.
    from_model_kind
        `from_table`'s declared kind, such as `fact` or `dimension`.
    from_grain
        Its grain, in your language.
    to_model_kind
        `to_table`'s declared kind.
    to_grain
        Its grain, in your language.
    """

    reference: str
    from_table: str
    from_name: str
    to_table: str
    to_name: str
    from_columns: list[str]
    to_columns: list[str]
    direction: Literal["many_to_one", "one_to_many"]
    fans_out: bool
    from_model_kind: str | None
    from_grain: str | None
    to_model_kind: str | None
    to_grain: str | None


class JoinPathOut(BaseModel):
    """A join path.

    Parameters
    ----------
    steps
        Its steps, in order, from `from` to `to`.
    """

    steps: list[JoinStepOut]


class JoinPathsOut(BaseModel):
    """Every declared join path of minimal length between two tables: at most 4 steps, 10 paths.

    Parameters
    ----------
    paths
        The paths, in the order of their references' ids; empty when there is none.
    reason
        Why there is none, and what to do; `null` when there are paths.
    more_paths
        `true` when more than 10 paths of that length exist.
    """

    paths: list[JoinPathOut]
    reason: str | None
    more_paths: bool


class ConnectionTestOut(BaseModel):
    """Response body for a connection test.

    Parameters
    ----------
    id
        The test's id.
    state
        ``queued``, ``running`` or ``done``.
    outcome
        What the test found once ``done``: ``ok``, ``dns``, ``refused``, ``timeout``, ``tls``,
        ``not_allowed``, ``root_unavailable``, ``not_found``, ``credentials`` or ``failed``;
        `null` before.
    message
        What it found, redacted, at most 1,000 characters; `null` before it is done.
    created_at
        When it was requested.
    finished_at
        When it was done; `null` before.
    """

    id: uuid.UUID
    state: str
    outcome: str | None
    message: str | None
    created_at: datetime
    finished_at: datetime | None


class RepositoryProbeIn(BaseModel):
    """Request body that queues a repository check; a worker runs it.

    Send exactly the fields of its kind: ``host_keys`` takes `url`; ``access`` takes
    `repository` and `environment`.

    Parameters
    ----------
    kind
        ``host_keys`` fetches the SSH host keys of the URL's host; ``access`` tests that a
        worker can read a registered repository with an environment's credential.
    url
        The SSH repository URL whose host is scanned (``host_keys`` only).
    repository
        The registered repository's name (``access`` only).
    environment
        The environment whose secret of the repository's credential is used (``access`` only).
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["host_keys", "access"]
    url: str | None = Field(default=None, max_length=2048)
    repository: str | None = Field(default=None, max_length=255)
    environment: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def _fields_of_its_kind(self) -> RepositoryProbeIn:
        """Refuse a body that lacks a field of its kind or sends one of the other kind.

        Returns
        -------
        RepositoryProbeIn
            `self`.

        Raises
        ------
        ValueError
            When the fields do not match the kind.
        """
        sent = {name for name in ("url", "repository", "environment") if getattr(self, name)}
        wanted = {"url"} if self.kind == "host_keys" else {"repository", "environment"}
        if sent != wanted:
            raise ValueError(
                "a host_keys check takes exactly `url`; an access check takes exactly "
                "`repository` and `environment`"
            )
        return self


class HostKeyOut(BaseModel):
    """One SSH host key a check found.

    Parameters
    ----------
    type
        The key type, e.g. ``ssh-ed25519``.
    fingerprint
        ``SHA256:…``, as `ssh-keygen -l` prints it: compare it with the git host's published
        fingerprints.
    line
        The known_hosts line for the repository's `ssh_known_hosts`.
    """

    type: str
    fingerprint: str
    line: str


class RepositoryProbeOut(BaseModel):
    """Response body for a repository check.

    Parameters
    ----------
    id
        The check's id.
    kind
        ``host_keys`` or ``access``.
    state
        ``queued``, ``running`` or ``done``.
    outcome
        What it found once ``done``: ``ok``, ``host_key_changed``, ``host_key_unknown``,
        ``auth_refused``, ``not_found``, ``unreachable``, ``refused_address``, ``timeout``,
        ``no_worker`` or ``failed``; `null` before.
    message
        What it found, with the next step; `null` before it is done.
    host_keys
        The keys of a ``host_keys`` check that found some; `null` otherwise.
    created_at
        When it was requested.
    finished_at
        When it was done; `null` before.
    """

    id: uuid.UUID
    kind: str
    state: str
    outcome: str | None
    message: str | None
    host_keys: list[HostKeyOut] | None
    created_at: datetime
    finished_at: datetime | None


class EndpointOut(BaseModel):
    """One host and port a connection reaches.

    Parameters
    ----------
    host
        The host, lower case.
    port
        The port.
    role
        ``service`` (the data) or ``token`` (where an OAuth2 token is fetched).
    """

    host: str
    port: int
    role: str


class TaskConnectionOut(BaseModel):
    """Response body of a running task's fetch of one of its connections.

    The only answer of the API that carries a secret's values. It is sent only to a bound task
    token of a running attempt, for a connection its deployed asset declares, with
    ``Cache-Control: no-store``; the helper keeps it in memory for the attempt.

    Parameters
    ----------
    name
        The connection's name.
    kind
        Its kind.
    address
        Its address, with defaults filled in.
    settings
        Its settings, with defaults filled in; a ``ca_certificate`` in full.
    endpoints
        Every host and port it reaches, in order.
    secret
        The values of its secret; `null` when it has none it may use.
    """

    name: str
    kind: str
    address: dict[str, Any]
    settings: dict[str, Any]
    endpoints: list[EndpointOut]
    secret: dict[str, str] | None


class DeployCreate(BaseModel):
    """Request body for a deploy, dry run or rollback.

    Parameters
    ----------
    repository
        Registered repository name.
    kind
        ``deploy`` (default), ``dry_run`` or ``rollback``.
    ref
        Deploy/dry run: ref or full SHA; default the repository's default ref, else ``HEAD``.
    to_sha
        Rollback: a full SHA already deployed to this environment.
    to_previous
        Rollback: the project version current before the present one.
    """

    model_config = ConfigDict(extra="forbid")

    repository: str
    kind: Literal["deploy", "dry_run", "rollback"] = "deploy"
    ref: str | None = None
    to_sha: str | None = None
    to_previous: StrictBool = False


class DeployAccepted(BaseModel):
    """Response body for an accepted deploy.

    Parameters
    ----------
    id
        The deploy run's id.
    status
        ``queued``.
    """

    id: uuid.UUID
    status: str


class DeployJobStartOut(BaseModel):
    """What a deploy did with one job that starts after each deploy (`on_deploy: true`).

    Parameters
    ----------
    job
        The job's name.
    outcome
        ``pending`` (the worker has not started it yet), ``started``, ``waiting`` (the project
        was busy; the start waits) or ``not_started``.
    run_id
        For ``started``, the queued run.
    waits_for_run_id
        For ``waiting``, the active run the start waits for.
    reason
        For ``not_started``: ``lacks_run`` (the deployer may not `RUN` in the environment),
        ``refused`` (refused as a start by hand would be), ``error`` or ``lost`` (the worker
        stopped before it recorded the start, which may or may not have been made).
    message
        For ``not_started``, one sentence that names the cause and the next step.
    """

    job: str
    outcome: Literal["pending", "started", "waiting", "not_started"]
    run_id: uuid.UUID | None = None
    waits_for_run_id: uuid.UUID | None = None
    reason: str | None = None
    message: str | None = None


class DeployOut(BaseModel):
    """Response body for a deploy run.

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
        The commit fetched.
    rollback_to_project_version_id
        A rollback's target.
    error_message
        Why it failed.
    diff
        Table names per change category.
    project_version_id
        The project version it made current.
    principal_id
        Who submitted it.
    created_at
        Queued at.
    started_at
        Claimed at.
    finished_at
        Finished at.
    hidden_adopted
        Adopted tables left out of `diff` because the caller may not read them.
    checks
        The checks the project has, table to ``[{"check", "on_failure"}]``; `None` for a run
        without checks.
    warnings
        What the deploy leaves for its owner to act on, as sentences (a removed asset whose
        table is kept after a failed check; a removed table or column that other repositories
        declare something on, shown only to a reader who may see it).
    waiting_for
        Why a queued deploy has not started: a stable `code`, one sentence in `message` that
        names the next step, and the numbers it uses; null when it is not waiting. Set on the
        single read (`GET .../deploys/{id}`) only, null in the history.
    job_starts
        The jobs with `on_deploy: true` that a successful deploy started, by name, each with
        what came of its start; empty for a run that starts no job (a dry run, a rollback, a
        failed deploy, a deploy of the commit already current).
    """

    id: uuid.UUID
    environment: str
    repository: str
    kind: str
    status: str
    requested_ref: str | None
    resolved_sha: str | None
    rollback_to_project_version_id: uuid.UUID | None
    error_message: str | None
    diff: dict[str, list[str]] | None
    project_version_id: uuid.UUID | None
    principal_id: uuid.UUID | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    hidden_adopted: int = 0
    checks: dict[str, list[dict[str, str]]] | None = None
    warnings: list[str] = Field(default_factory=list)
    waiting_for: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Why this is waiting, or null when it is not: a stable `code`, one sentence in "
            "`message`, and the numbers the sentence uses."
        ),
    )
    job_starts: list[DeployJobStartOut] = Field(default_factory=list)


class DeploymentOut(BaseModel):
    """The current project version of one repository.

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
    project_version_id: uuid.UUID
    commit_sha: str
    updated_at: datetime
    run_as: str | None = None


class RunAsIn(BaseModel):
    """Bind a run principal to a deployment.

    Parameters
    ----------
    principal
        Name of an active service account.
    """

    model_config = ConfigDict(extra="forbid")

    principal: str = Field(min_length=1, max_length=255)


class ProjectVersionOut(BaseModel):
    """A project version with its manifest filtered for the caller.

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
        Assets, DAG edges, external inputs, resolved config; SQL withheld
        where the caller cannot read every referenced table.
    """

    id: uuid.UUID
    environment: str
    repository: str
    commit_sha: str
    code_uri: str
    code_sha256: str
    deployed_by: uuid.UUID | None
    created_at: datetime
    manifest: dict


class OrphanOut(BaseModel):
    """One orphaned table.

    Parameters
    ----------
    repository
        The repository that used to produce it.
    table
        Dotted table name.
    """

    repository: str
    table: str


class OrphansOut(BaseModel):
    """Orphans visible to the caller.

    Parameters
    ----------
    orphans
        Readable orphans.
    hidden
        Orphans the caller may not read.
    """

    orphans: list[OrphanOut]
    hidden: int


class RunCreate(BaseModel):
    """Start a run.

    Parameters
    ----------
    repository
        Repository name.
    select
        Shell-glob patterns over dotted table names; empty runs everything.
    upstream
        Add every ancestor of the matched assets.
    downstream
        Add every descendant of the matched assets.
    """

    model_config = ConfigDict(extra="forbid")

    repository: str = Field(min_length=1, max_length=255)
    select: list[str] = Field(default_factory=list, max_length=100)
    upstream: bool = False
    downstream: bool = False


class CheckResultOut(BaseModel):
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
        The table's id; `None` when it was not resolved.
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
        What the rollback after a failed blocking check did, if there was one.
    policy_applied
        Whether a row filter or column mask applied to the run's principal.
    policy_note
        Text saying what that means for the result, or `None`.
    query_run_id
        The query that ran the check.
    started_at
        Start.
    finished_at
        End.
    generated
        Whether the check was generated from the asset's header (`key:`, `references:`)
        rather than written under `checks/`.
    """

    run_id: uuid.UUID
    task: str
    name: str
    check: str
    path: str
    table: str
    table_id: uuid.UUID | None
    on_failure: str
    outcome: str
    failing_rows: int | None
    error: str | None
    checked_snapshot_id: int | None
    rollback_outcome: str | None
    policy_applied: bool
    policy_note: str | None
    query_run_id: uuid.UUID | None
    started_at: datetime
    finished_at: datetime
    generated: bool = False


class CheckResultPage(BaseModel):
    """One page of check results, newest first.

    Parameters
    ----------
    items
        The results on this page.
    next_cursor
        Pass back as `?cursor=` to fetch the next page; `None` on the last page.
    hidden
        How many results of this page were left out: generated reference checks whose target
        the caller may not read. A page can hold fewer results than `limit` and still have a
        next one.
    """

    items: list[CheckResultOut]
    next_cursor: str | None
    hidden: int = 0


class FailureOut(BaseModel):
    """Why a task or an attempt failed.

    Parameters
    ----------
    code
        A stable code: `build_failed`, `code_changed`, `secret_refused`, `no_runtime`,
        `upstream_failed`, `upstream_missing`, `check_blocked`, `check_error`,
        `checks_unfinished`, `query_invalid`, `permission_denied`, `not_found`, `memory`,
        `timeout`, `cancelled`, `worker_lost`, `conflict` or `internal`; `unknown` for a failure
        recorded before the codes existed
        (never stored). A client treats a code it does not know like `internal`.
    cause
        One sentence: what happened. The sentences may be reworded; the codes do not change.
    next_step
        One sentence: what to do about it.
    """

    code: str
    cause: str
    next_step: str


class PolicyWarningOut(BaseModel):
    """A row filter or column mask a task's write left out of date; shown to administrators only.

    Parameters
    ----------
    kind
        ``row_filter`` or ``column_mask``.
    column
        The column the policy uses, as the policy spells it.
    change
        ``removed`` (the write removed the column) or ``replaced`` (its type changed in a way
        that does not widen, so it is a new column under the same name).
    policy_id
        The policy's id.
    message
        What happened and the next step, in one paragraph.
    """

    kind: str
    column: str
    change: str
    policy_id: uuid.UUID
    message: str


class ImpactUseOut(BaseModel):
    """One use of a retyped column by another repository.

    Parameters
    ----------
    kind
        The metadata graph's node kind: ``asset``, ``reference``, ``measure``, ``check`` or
        ``term``.
    name
        Its name, as the metadata graph shows it.
    repository
        Its repository.
    """

    kind: str
    name: str
    repository: str


class ImpactWarningOut(BaseModel):
    """A column a task's write retyped, and what other repositories use it for.

    Parameters
    ----------
    table
        The column's table.
    column
        The retyped column.
    uses
        Its uses the caller may see (`ImpactUseOut`); empty when Lakana could not check them in
        time, which `message` then says.
    message
        What happened, the next step and the uses, in one paragraph.
    """

    table: str
    column: str
    uses: list[ImpactUseOut] = Field(default_factory=list)
    message: str


class TaskAttemptResourcesOut(BaseModel):
    """What one attempt of a task used, in the run detail.

    Parameters
    ----------
    attempt
        Attempt number.
    peak_memory_bytes
        Highest resident memory of the attempt's process in bytes; `null` if not measured.
    cpu_seconds
        Processor time the attempt used, in seconds; `null` if not measured.
    bytes_read
        Bytes DuckDB read (a query) or the SDK readers read (a Python task); `null` if unknown.
    """

    attempt: int
    peak_memory_bytes: int | None = Field(
        default=None,
        description=(
            "Highest resident memory of the attempt's process in bytes; null if not measured "
            "(for a container task: the container's cgroup peak)."
        ),
    )
    cpu_seconds: float | None = Field(
        default=None,
        description=(
            "Processor time (user plus system) the attempt used, in seconds; null if not measured."
        ),
    )
    bytes_read: int | None = Field(
        default=None,
        description=(
            "Bytes DuckDB read for a query (its `io.total_bytes_read` metric, null when DuckDB "
            "answered from file metadata); for a Python task the bytes the SDK readers "
            "downloaded or read; null if unknown."
        ),
    )


class TaskRunOut(BaseModel):
    """One task run.

    Parameters
    ----------
    asset
        Dotted table name.
    status
        Task status.
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
        Last failure, as free text: the detail of `failure`.
    query_run_id
        The write that executed the last attempt.
    rows_written
        Rows it wrote.
    failure
        Why the task failed, was cancelled or never ran, with its stable code, the cause and the
        next step (`FailureOut`); `null` for a task with no error. A task that failed before the
        codes existed has the code `unknown`.
    waiting_for
        Why a `pending` or `ready` task of a running run has not started, or `null`: a stable
        `code` (`upstream`, `retry`, `setting_back`, `no_worker`, `no_worker_of_kind`,
        `workers_busy` or `starting`), one sentence in `message`, and the numbers or names it
        uses. A task is pipeline work: `no_worker_of_kind` means workers are running and none
        takes pipeline work (`workers` is 0, `other_workers` the workers running), and `workers` in
        `workers_busy` counts the workers that take pipeline work. For `upstream`, `upstream`
        lists the first 50 unfinished upstream tasks whose tables the caller may read (raw
        names), `upstream_count` is how many there are in all, and `hidden_upstream` (present
        only when not 0) how many the caller may not read, counted and never named; the
        sentence names five, escaped.
    kind
        ``asset`` for a table the run builds, ``source_check`` for a table it only checks.
    checks
        The task's check results, by name.
    hidden_checks
        How many results were left out of `checks`: generated reference checks whose target the
        caller may not read.
    kept_check
        ``true`` for a ``source_check`` task that checks a table the project builds, because
        that table was kept after a failed check; ``false`` for every other task; ``null`` for a
        ``source_check`` task of a run whose project version is gone, which cannot tell.
    setting_back
        ``true`` while the task has not ended and its next claim only sets its table back:
        its worker was lost after it may have written the table, or its run was cancelled
        while it waited with rows no check had passed. The task is then ``ready``
        (`waiting_for` has the code `setting_back`), ``running`` with that claim, or
        ``pending`` again after that claim's worker drained. ``false`` once it has ended.
    schema_note
        One neutral sentence when one of the task's writes removed or replaced a column of its
        table (``This run removed or retyped a column of the table. An administrator can see in
        this run what else that affects; if other people read this table, tell one.``), else
        `null`. The same for every caller, whether or not a row filter or column
        mask uses the column.
    policy_warnings
        For a caller who may manage policies (MANAGE_POLICY), the row filters and column masks
        those writes left out of date (`PolicyWarningOut`); reads by the people they apply to are
        refused until an administrator creates them again. Always empty for every other caller,
        who may not learn which policies exist.
    impact_warnings
        Per column those writes retyped (its type changed in a way that does not widen), what
        other repositories than the run's use it for: references, measures, checks, glossary terms
        and assets that read it (`ImpactWarningOut`), computed when the run wrote the table. Only
        what the caller may see: READ_ENVIRONMENT, and READ_TABLE on the retyped table and on
        every table a use names; a use the caller may not see is left out without a count. Empty
        for a task without a retyped column that others use.
    attempts
        What each attempt used (`TaskAttemptResourcesOut`), oldest first; empty before the first
        attempt.
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
    query_run_id: uuid.UUID | None
    rows_written: int | None
    waiting_for: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Why this is waiting, or null when it is not: a stable `code`, one sentence in "
            "`message`, and the numbers the sentence uses."
        ),
    )
    kind: str = "asset"
    checks: list[CheckResultOut] = Field(default_factory=list)
    hidden_checks: int = 0
    kept_check: bool | None = False
    setting_back: bool = False
    failure: FailureOut | None = None
    schema_note: str | None = None
    policy_warnings: list[PolicyWarningOut] = Field(default_factory=list)
    impact_warnings: list[ImpactWarningOut] = Field(default_factory=list)
    attempts: list[TaskAttemptResourcesOut] = Field(default_factory=list)


class NotificationOut(BaseModel):
    """One notification of a run.

    Parameters
    ----------
    id
        The delivery id: the ``delivery_id`` a receiver sees, and what
        ``GET /environments/{env}/notifications/{id}`` takes.
    event
        ``failed``, ``recovered``, ``columns_changed``, ``health`` or ``test``.
    state
        ``pending``, ``delivered`` or ``failed``.
    attempts
        Attempts made so far.
    max_attempts
        Attempts before the delivery counts as failed.
    last_result
        A short text on the last attempt; never the URL.
    delivered_at
        When it was delivered.
    """

    id: uuid.UUID
    event: str
    state: str
    attempts: int
    max_attempts: int
    last_result: str | None
    delivered_at: datetime | None


class DeliveryOut(NotificationOut):
    """One notification delivery on its own, such as the test of a webhook.

    Parameters
    ----------
    job
        The job.
    repository
        Repository name.
    next_attempt_at
        When the next attempt is due; null once the delivery is over.
    """

    job: str
    repository: str
    next_attempt_at: datetime | None


class NotifyTestOut(BaseModel):
    """The test of a webhook, queued.

    Parameters
    ----------
    delivery_id
        The delivery to read with ``GET /environments/{env}/notifications/{delivery_id}``.
    """

    delivery_id: uuid.UUID


class RunOut(BaseModel):
    """One run.

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
        Run status.
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
        The job the run belongs to; null for an ad-hoc run.
    trigger
        ``schedule``, ``api`` (a service account called the API), ``manual`` or ``deploy`` (a
        deploy started the job as its deployer); null for an ad-hoc run.
    scheduled_for
        For a scheduled run, the time the schedule named.
    missed
        How many earlier scheduled times this run stands in for.
    notifications
        The notifications the run queued, oldest first (empty in listings).
    failed_tasks
        How many tasks ended ``failed`` (not ``upstream_failed`` or ``cancelled``); also in
        listings.
    """

    id: uuid.UUID
    environment: str
    repository: str
    project_version_id: uuid.UUID
    commit_sha: str
    kind: str
    status: str
    cancel_requested: bool
    triggered_by: str | None
    run_as: str | None
    resumed_from_run_id: uuid.UUID | None
    selection: dict[str, Any]
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    tasks: list[TaskRunOut]
    job: str | None = None
    trigger: str | None = None
    scheduled_for: datetime | None = None
    missed: int = 0
    notifications: list[NotificationOut] = Field(default_factory=list)
    failed_tasks: int = 0


class JobRunRequest(BaseModel):
    """Name the repository of a job for a start, a pause or a resume.

    Parameters
    ----------
    repository
        Repository name.
    """

    model_config = ConfigDict(extra="forbid")

    repository: str = Field(min_length=1, max_length=255)


class WaitingStartOut(BaseModel):
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
        Who asked, if that principal still exists; null for a scheduled start.
    scheduled_for
        For a scheduled start, the time the schedule named.
    missed
        How many earlier scheduled times the start stands in for.
    merged
        How many starts it stands for.
    message
        One sentence for a person: what it waits for and that it starts then.
    """

    job: str
    waits_for_run_id: uuid.UUID
    trigger: str
    requested_at: datetime
    requested_by: str | None
    scheduled_for: datetime | None
    missed: int
    merged: int
    message: str


class JobStartOut(BaseModel):
    """What a start of a job did: the run (201) or the waiting start (202).

    Parameters
    ----------
    run
        The queued run, or null when the start waits.
    waiting
        The waiting start, or null when a run was queued.
    """

    run: RunOut | None
    waiting: WaitingStartOut | None


class ScheduleTriggerOut(BaseModel):
    """A trigger that starts the job at the times a cron expression names.

    Parameters
    ----------
    kind
        Always ``schedule``.
    cron
        The cron expression.
    timezone
        The schedule's IANA zone.
    paused
        Whether the schedule is paused.
    next_run_at
        The next scheduled time (UTC), or null for a paused schedule.
    """

    kind: Literal["schedule"] = "schedule"
    cron: str
    timezone: str
    paused: bool
    next_run_at: datetime | None


class DeployTriggerOut(BaseModel):
    """A trigger that starts the job after each deploy that changes the project version.

    Parameters
    ----------
    kind
        Always ``deploy``.
    """

    kind: Literal["deploy"] = "deploy"


class OnDemandTriggerOut(BaseModel):
    """The trigger every job has: a start through the API, the SDK or the CLI.

    Parameters
    ----------
    kind
        Always ``on_demand``.
    """

    kind: Literal["on_demand"] = "on_demand"


TriggerOut = Annotated[
    ScheduleTriggerOut | DeployTriggerOut | OnDemandTriggerOut, Field(discriminator="kind")
]
"""One way a job starts. A client must ignore a ``kind`` it does not know: kinds may be added."""


class JobOut(BaseModel):
    """One job and the platform's state for it.

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
        The cron expression, or null for a job without a schedule here.
    timezone
        The schedule's IANA zone, or null without a schedule.
    state
        ``scheduled``, ``paused``, ``waiting`` (a start waits for the active run),
        ``waiting to start`` (a start waits and the project is free), ``no schedule``,
        ``no run principal`` or ``run principal unusable``.
    paused
        Whether the schedule is paused.
    next_run_at
        The next scheduled time (UTC), or null.
    next_run_local
        The same moment in the job's zone, ISO 8601 with the offset.
    waiting_for
        Why a start waits (`job_waits`, `job_starts_soon`) or why the schedule cannot start
        (`no_run_principal`, `run_principal_unusable`): a stable `code` and one sentence in
        `message`; null otherwise.
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
        How the job starts, schedule first: a ``schedule`` trigger when the job has a schedule
        in this environment, a ``deploy`` trigger when it starts after each deploy here, and the
        ``on_demand`` trigger every job has, last. Kinds may be added.
    """

    name: str
    environment: str
    repository: str
    select: list[str]
    upstream: bool
    downstream: bool
    cron: str | None
    timezone: str | None
    state: str
    paused: bool
    next_run_at: datetime | None
    next_run_local: str | None
    waiting_for: dict[str, Any] | None
    waits_for_run_id: uuid.UUID | None
    last_run_id: uuid.UUID | None
    last_run_status: str | None
    last_run_trigger: str | None
    last_run_at: datetime | None
    triggers: list[TriggerOut]


class TaskAttemptOut(BaseModel):
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
        Why it failed, as free text.
    failure
        The code, cause and next step of an attempt that did not succeed (`FailureOut`);
        `null` for an attempt that succeeded or is running.
    peak_memory_bytes
        Highest resident memory of the attempt's process in bytes; `null` if not measured.
    cpu_seconds
        Processor time the attempt used, in seconds; `null` if not measured.
    bytes_read
        Bytes DuckDB read (a query) or the SDK readers read (a Python task); `null` if unknown.
    """

    attempt: int
    worker: str
    started_at: datetime
    finished_at: datetime | None
    outcome: str | None
    rows_written: int | None
    error_message: str | None
    failure: FailureOut | None = None
    peak_memory_bytes: int | None = Field(
        default=None,
        description=(
            "Highest resident memory of the attempt's process in bytes; null if not measured "
            "(for a container task: the container's cgroup peak)."
        ),
    )
    cpu_seconds: float | None = Field(
        default=None,
        description=(
            "Processor time (user plus system) the attempt used, in seconds; null if not measured."
        ),
    )
    bytes_read: int | None = Field(
        default=None,
        description=(
            "Bytes DuckDB read for a query (its `io.total_bytes_read` metric, null when DuckDB "
            "answered from file metadata); for a Python task the bytes the SDK readers "
            "downloaded or read; null if unknown."
        ),
    )


class TaskHistoryEntryOut(BaseModel):
    """One attempt in the history of a task.

    Parameters
    ----------
    run_id
        The run the attempt belongs to.
    attempt
        Attempt number within its task run, from 1.
    status
        How the attempt ended: `succeeded`, `failed`, `timed_out`, `lost`, `released` or
        `cancelled`; `running` while it has not ended.
    started_at
        When it started.
    finished_at
        When it ended; `null` while it runs.
    duration_seconds
        Seconds from start to end; `null` while it runs.
    peak_memory_bytes
        Highest resident memory of the attempt's process in bytes; `null` if not measured.
    cpu_seconds
        Processor time the attempt used, in seconds; `null` if not measured.
    bytes_read
        Bytes read; `null` if unknown.
    failure_code
        Why the attempt failed, when it did and the reason is known; otherwise `null`.
    """

    run_id: uuid.UUID
    attempt: int
    status: str
    started_at: datetime
    finished_at: datetime | None
    duration_seconds: float | None
    peak_memory_bytes: int | None = Field(
        default=None, description="Highest resident memory in bytes; null if not measured."
    )
    cpu_seconds: float | None = Field(
        default=None, description="Processor time in seconds; null if not measured."
    )
    bytes_read: int | None = Field(
        default=None,
        description=(
            "Bytes DuckDB read for a query, or the bytes the SDK readers read for a Python "
            "task; null if unknown."
        ),
    )
    failure_code: str | None = None


class TaskHistoryPage(BaseModel):
    """One page of a task's attempts across runs, newest first.

    Parameters
    ----------
    attempts
        The attempts on this page.
    next_cursor
        Pass back as `?cursor=` to fetch the next page; `null` on the last page.
    """

    attempts: list[TaskHistoryEntryOut]
    next_cursor: str | None


class TaskLogOut(BaseModel):
    """A task's logs.

    Parameters
    ----------
    run_id
        The run.
    asset
        The task.
    text
        Free-text log.
    attempts
        Structured attempts.
    next_offset
        The byte offset of the end of `text` in the stored log; send it as `offset` to
        read only what comes after.
    more
        Whether stored bytes remain after `next_offset` (one response is cut at 1 MiB).
    complete
        Whether the task has finished and `text` reaches the end of its log.
    reset
        Whether the requested `offset` lay beyond the end of the log, which only happens when
        the log is shorter than before; start again from `next_offset`.
    """

    run_id: uuid.UUID
    asset: str
    text: str
    attempts: list[TaskAttemptOut]
    next_offset: int
    more: bool
    complete: bool
    reset: bool


class QueryListOut(BaseModel):
    """One row of the list of an environment's queries.

    Never the SQL text, an error message, lineage, a result location or the worker.

    Parameters
    ----------
    id
        The query's id.
    submitter
        The submitting principal's name, `null` when it was deleted.
    kind
        `read`, `create`, `replace`, `append`, `view` or `maintain`.
    target
        A write run's target table, `null` for a read.
    status
        `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED` or `CANCELLED`.
    created_at
        When it was submitted.
    started_at
        When a worker started it.
    finished_at
        When it ended.
    error_cause
        Why a `FAILED` or `CANCELLED` run ended, as on the status read; never the message.
    waiting_for
        Why a `QUEUED` query has not started, as on the status read (including `user_limit` and
        `no_worker_of_kind`); `null` for other statuses. On another principal's row a manager
        reads `limit` and never the number running.
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
    waiting_for: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Why this is waiting, or null when it is not: a stable `code`, one sentence in "
            "`message`, and the numbers the sentence uses."
        ),
    )


class WorkerCachesOut(BaseModel):
    """The sizes of a worker's caches, as it last measured them.

    Parameters
    ----------
    environments_bytes
        The Python environments of projects.
    packages_bytes
        uv's package download cache, without the files it shares with an environment.
    tasks_bytes
        The working directories of Python tasks.
    complete
        `false` when the measurement stopped at its time limit: the figures are a floor.
    measured_at
        When the worker stored them.
    """

    environments_bytes: int
    packages_bytes: int
    tasks_bytes: int
    complete: bool
    measured_at: datetime


class WorkerMemoryOut(BaseModel):
    """The memory a worker last reported.

    Parameters
    ----------
    used_bytes
        The resident memory of the worker and of every process it started.
    limit_bytes
        Its limit (`LAKANA_WORKER__MEMORY_LIMIT`, 4 GB by default); `null` when it has none.
    measured_at
        When the worker last wrote the figures, not when it last measured: it writes them with a
        heartbeat only once they moved by 1 MiB or more, so steady use keeps an old time.
    """

    used_bytes: int
    limit_bytes: int | None
    measured_at: datetime


class WorkerOut(BaseModel):
    """A registered worker.

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
        Its last heartbeat.
    status
        `alive` (heartbeat younger than the lease) or `lost`.
    takes
        The work it takes: `all`, `interactive` (ad-hoc queries) or `pipeline` (deploys, pipeline
        tasks and table maintenance).
    query_run_id
        Its oldest running query, if any.
    deploy_run_id
        Its oldest running deploy, if any.
    task_run_id
        The running task whose lease it holds, if any.
    caches
        The sizes of its caches; `null` before its first measurement.
    memory
        The memory it last reported; `null` before its first report and for a worker without
        a memory limit.
    replaced
        `true` for a lost worker that a worker restarted on the same host replaced (it started
        after this one's last heartbeat); such a row does not count as a worker that is down.
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
    caches: WorkerCachesOut | None = None
    memory: WorkerMemoryOut | None = None
    replaced: bool = False


class StatusDetailOut(BaseModel):
    """What an administrator sees of the instance beyond its state.

    Parameters
    ----------
    slots_busy
        The busy task slots of the alive workers: an `all` worker has two (one for ad-hoc
        queries, one for pipeline tasks and deploys), an `interactive` or `pipeline` one has one.
    slots_total
        All task slots of the alive workers.
    memory_used_bytes
        The memory the alive workers last reported, added up.
    memory_limit_bytes
        Their limits, added up; `null` when an alive worker reports no limit or none is alive.
    workers
        Every registered worker, as `GET /api/v1/workers` lists them.
    """

    slots_busy: int
    slots_total: int
    memory_used_bytes: int
    memory_limit_bytes: int | None
    workers: list[WorkerOut]


class InstanceStatusOut(BaseModel):
    """Whether the instance works, as the caller may see it.

    Parameters
    ----------
    state
        `healthy`, `degraded` (work gets done, slower or at risk) or `down` (nothing gets done).
    causes
        Why, in this order: `database`, `storage`, `no_worker`, `worker_lost`, `memory_high`.
        A caller who may not manage environments on the instance sees only `no_worker` and
        `worker_lost`; a failed database or storage still makes their state `down`.
    workers_alive
        The workers whose heartbeat is younger than the lease.
    workers_total
        Every registered worker, alive and lost, except lost ones a restart on their host
        replaced.
    detail
        Compute, memory and the workers; `null` unless the caller may manage environments on
        the instance.
    """

    state: str
    causes: list[str]
    workers_alive: int
    workers_total: int
    detail: StatusDetailOut | None


class SchedulerOut(BaseModel):
    """The scheduler role and the queue.

    Parameters
    ----------
    holder
        Current holder.
    heartbeat_at
        Its last heartbeat.
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


_MAX_VERSION = 2**31 - 1
"""Largest version number: the catalog column is a 32-bit integer."""

"""A namespace path (1024) and an object name (255) joined by a dot."""


class ArtifactUploadOut(BaseModel):
    """An open upload (`POST .../artifacts/{name}/uploads`).

    Parameters
    ----------
    upload_id
        The upload's id, for the blob and commit requests.
    missing
        The SHA-256 of each file the store does not have yet; send exactly these.
    expires_at
        When the upload stops being usable.
    """

    upload_id: str
    missing: list[str]
    expires_at: datetime


class ArtifactBlobOut(BaseModel):
    """The outcome of one file's upload.

    Parameters
    ----------
    sha256
        The file's SHA-256.
    stored
        `True` if this request stored it; `False` if the artifact already had it.
    """

    sha256: str
    stored: bool


class ArtifactSourceIn(BaseModel):
    """One thing an artifact version was made from, for lineage.

    Parameters
    ----------
    name
        Dotted name of a table, view or artifact.
    version
        The artifact version that was read; omitted records the current one.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_SOURCE_NAME)
    version: Annotated[int, Field(strict=True, ge=1, le=_MAX_VERSION)] | None = None


class ArtifactCommitIn(BaseModel):
    """Body of the commit request.

    Parameters
    ----------
    sources
        What the version was made from; each name once, at most `MAX_SOURCE_VERSIONS`.
    """

    model_config = ConfigDict(extra="forbid")

    sources: list[ArtifactSourceIn] = Field(default_factory=list, max_length=MAX_SOURCE_VERSIONS)

    @model_validator(mode="after")
    def _each_source_once(self) -> ArtifactCommitIn:
        """Refuse a source named twice.

        Returns
        -------
        ArtifactCommitIn
            This model.

        Raises
        ------
        ValueError
            If two sources have one name.
        """
        names = [source.name for source in self.sources]
        if len(set(names)) != len(names):
            raise ValueError("name each source once, with at most one version")
        return self


class ArtifactOut(BaseModel):
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
        How many versions it has had, deleted ones included.
    size_bytes
        The summed size of its live versions.
    created_at
        When it was created.
    """

    name: str
    namespace: str
    uuid: str
    kind: Literal["ARTIFACT"]
    current_version: int | None
    versions: int
    size_bytes: int
    created_at: datetime


class ArtifactFileOut(BaseModel):
    """One file of an artifact version.

    Parameters
    ----------
    path
        Its path inside the artifact, `/`-separated.
    size
        Its size in bytes.
    sha256
        The SHA-256 of its contents.
    """

    path: str
    size: int
    sha256: str


class ArtifactVersionOut(BaseModel):
    """One version of an artifact.

    Parameters
    ----------
    version
        The number; versions can have gaps, since a number is never reused.
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
        When it was committed.
    deleted_at
        When it was deleted; `None` while it is live.
    deleted_by
        The name of the principal that deleted it.
    files
        Its files; empty in a listing and for a deleted version.
    deleted_versions
        The numbers of the versions this commit pruned (`keep_versions`).
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
    created_at: datetime
    deleted_at: datetime | None
    deleted_by: str | None
    files: list[ArtifactFileOut]
    deleted_versions: list[int] = Field(default_factory=list)


class ArtifactKeptOut(BaseModel):
    """Files a delete left in storage, for one reason.

    Parameters
    ----------
    reason
        ``other_version`` (a live version also has them), ``open_upload`` (an open upload of the
        artifact names them) or ``left_to_sweep`` (this request did not remove them and the
        server's periodic clean-up decides).
    files
        How many stored files; identical files of one version count once.
    bytes
        Their summed size.
    version
        For ``other_version``, the newest live version that has them, and only for a caller who
        may read the artifact; `None` otherwise.
    """

    reason: Literal["other_version", "open_upload", "left_to_sweep"]
    files: int
    bytes: int
    version: int | None


class ArtifactVersionDeletedOut(ArtifactVersionOut):
    """The answer to deleting a version: the version, and what happened to its files.

    The fields of `ArtifactVersionOut` come first, so a client that reads only those still
    works. The counts are of this request's delete.

    Parameters
    ----------
    freed_files
        Stored files this request removed from storage.
    freed_bytes
        Their summed size.
    kept_files
        Stored files that stayed in storage.
    kept_bytes
        Their summed size.
    kept
        The files that stayed, by reason; empty when none did.
    """

    freed_files: int
    freed_bytes: int
    kept_files: int
    kept_bytes: int
    kept: list[ArtifactKeptOut]


class ArtifactUploadItemOut(BaseModel):
    """One open upload of the caller's, as `GET .../artifacts/uploads` lists it.

    Parameters
    ----------
    upload_id
        The upload's id, for the withdraw route.
    artifact
        The artifact it adds a version to, as ``namespace.name``.
    created_at
        When it was begun.
    expires_at
        When it stops being usable and frees its place.
    """

    upload_id: str
    artifact: str
    created_at: datetime
    expires_at: datetime


class ArtifactUploadPage(BaseModel):
    """One page of the caller's open uploads, newest first.

    Parameters
    ----------
    items
        The uploads on this page.
    next_cursor
        Pass back as `?cursor=` to fetch the next page; `None` on the last page.
    """

    items: list[ArtifactUploadItemOut]
    next_cursor: str | None


class SearchHitOut(BaseModel):
    """One search hit.

    Parameters
    ----------
    kind
        What was found: ``table``, ``column``, ``run``, ``principal`` or ``term``.
    name
        A display name: ``namespace.table``, ``namespace.table.column``, the run id, the
        principal's name or the term's key.
    matched
        The field that matched: ``name``, ``label``, ``description``, ``grain``, ``measure`` (a
        column's measure label or description), ``id``, ``job``, ``repository``, ``synonym`` or
        ``definition`` (a term's) or ``term`` (a table or column found by a term's name or synonym).
    snippet
        For a label, description, grain, measure or term match, and for every term hit, at most
        160 characters of the text around the hit.
    namespace
        For a table or column, its namespace, dotted.
    table
        For a table or column, the table's name.
    table_kind
        For a table, ``TABLE``, ``EXTERNAL``, ``VIEW`` or ``ARTIFACT``.
    model_kind
        For a table whose asset header declares one, its ``kind``: ``fact``, ``dimension``,
        ``wide`` or ``staging``.
    column
        For a column, its name.
    column_type
        For a column, its type.
    run_id
        For a run, its id.
    job
        For a run, its job, if it has one.
    repository
        For a run or a term, its repository.
    status
        For a run, its status.
    principal
        For a principal, its name.
    principal_type
        For a principal, ``USER`` or ``SERVICE_ACCOUNT``.
    language
        For a table or column that a text matched, the language of that text (a translation in
        one of your languages, or the table's main language); for a term that a translated text
        matched, its language; ``null`` for a name match, a term match, a term's main-language
        text, and for runs and principals.
    translated
        Whether the text that matched is a translation, in a language other than the table's or
        term's main one; ``false`` for a header text, a name, a run and a principal.
    term
        For a term, its key; for a table or column with `matched` ``term``, the key of the term
        whose name or synonym matched.
    """

    model_config = ConfigDict(from_attributes=True)

    kind: Literal["table", "column", "run", "principal", "term"]
    name: str
    matched: Literal[
        "name",
        "label",
        "description",
        "grain",
        "measure",
        "id",
        "job",
        "repository",
        "synonym",
        "definition",
        "term",
    ]
    snippet: str | None
    namespace: str | None
    table: str | None
    table_kind: str | None
    model_kind: str | None
    column: str | None
    column_type: str | None
    run_id: uuid.UUID | None
    job: str | None
    repository: str | None
    status: str | None
    principal: str | None
    principal_type: str | None
    language: str | None = None
    translated: bool = False
    term: str | None = None


class SearchOut(BaseModel):
    """One page of search hits.

    Parameters
    ----------
    results
        The hits, best first: exact matches of a name or label, then prefix, then substring,
        then description matches; within each, tables before columns before runs before
        principals before terms. A table, column or term appears once, however many of its
        texts match.
    limit
        The page size asked for.
    offset
        How many hits were skipped.
    has_more
        Whether a hit follows this page.
    """

    model_config = ConfigDict(from_attributes=True)

    results: list[SearchHitOut]
    limit: int
    offset: int
    has_more: bool


class CiTrustRuleCreate(BaseModel):
    """Request body for creating a CI trust rule.

    Parameters
    ----------
    issuer
        ``github``, ``gitlab`` or the URL of an issuer in `LAKANA_CI_OIDC__ISSUERS`.
    principal
        The active service account the CI job acts as; it needs its own `DEPLOY` grant.
    environment
        The environment the CI job may deploy to.
    repository
        The registered repository it may deploy.
    kinds
        ``deploy`` and/or ``dry_run`` (default ``["deploy"]``).
    conditions
        Claim name to the exact text the ID token's claim must have. Must contain the numeric
        repository id (``repository_id`` on GitHub, ``project_id`` on GitLab) and a ``ref`` or
        ``environment``.
    start_on_deploy_jobs
        Let a deploy accepted under the rule start the deployed project's jobs with
        `on_deploy: true` in its environment, as the CI identity, and nothing else; false by
        default.
    """

    model_config = ConfigDict(extra="forbid")

    issuer: str = Field(max_length=255)
    principal: str = Field(max_length=255)
    environment: str = Field(max_length=255)
    repository: str = Field(max_length=255)
    kinds: list[str] = Field(default_factory=lambda: ["deploy"], max_length=2)
    conditions: dict[str, str]
    start_on_deploy_jobs: bool = False


class CiTrustRuleUpdate(BaseModel):
    """Request body for changing a CI trust rule; omitted fields stay.

    Every change ends the tokens issued under the rule before it.

    Parameters
    ----------
    principal
        A new service account.
    kinds
        New kinds.
    conditions
        New conditions, replacing all of the old ones.
    start_on_deploy_jobs
        Whether a deploy under the rule starts the project's `on_deploy` jobs.
    """

    model_config = ConfigDict(extra="forbid")

    principal: str | None = Field(default=None, max_length=255)
    kinds: list[str] | None = Field(default=None, max_length=2)
    conditions: dict[str, str] | None = None
    start_on_deploy_jobs: bool | None = None


class CiTrustRuleOut(BaseModel):
    """Response body for a CI trust rule.

    Parameters
    ----------
    id
        The rule's id.
    issuer
        The issuer URL.
    principal
        The service account.
    environment
        The environment.
    repository
        The repository.
    kinds
        ``deploy`` and/or ``dry_run``.
    conditions
        Claim name to required text.
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
        Whether a deploy accepted under it starts the project's `on_deploy` jobs.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
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


class CiTrustExplainIn(BaseModel):
    """Request body for explaining which rule a set of ID token claims would match.

    Parameters
    ----------
    claims
        The ID token's claims, decoded (no token, no signature).
    environment
        The environment the exchange would ask for.
    repository
        The repository it would ask for.
    issuer
        The issuer; the ``iss`` claim when left out.
    """

    model_config = ConfigDict(extra="forbid")

    claims: dict[str, Any]
    environment: str = Field(max_length=255)
    repository: str = Field(max_length=255)
    issuer: str | None = Field(default=None, max_length=255)


class CiTrustRuleExplanationOut(BaseModel):
    """Why one rule does or does not accept the claims.

    Parameters
    ----------
    rule_id
        The rule.
    matches
        Whether it accepts them.
    reasons
        Why not; empty when it matches.
    """

    model_config = ConfigDict(from_attributes=True)

    rule_id: uuid.UUID
    matches: bool
    reasons: list[str]


class CiTrustExplainOut(BaseModel):
    """What an exchange with the claims would do.

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

    model_config = ConfigDict(from_attributes=True)

    issuer: str
    environment: str
    repository: str
    verdict: str
    matching: list[uuid.UUID]
    rules: list[CiTrustRuleExplanationOut]


class CiTokenExchangeIn(BaseModel):
    """Request body of the CI token exchange (at most 16 KiB).

    Parameters
    ----------
    id_token
        The CI job's OIDC ID token, requested for the audience in `LAKANA_CI_OIDC__AUDIENCE`.
    environment
        The environment the job deploys to.
    repository
        The registered Lakana repository it deploys.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id_token: str
    environment: str
    repository: str


class CiTokenOut(BaseModel):
    """Response body of the CI token exchange: the token, shown once.

    Parameters
    ----------
    token
        ``lakana_ci_…``; send it as ``Authorization: Bearer <token>``.
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
        The commit a deploy is pinned to (the ID token's ``sha``), or `None`.
    rule_id
        The CI trust rule that accepted the ID token.
    """

    token: str
    expires_at: datetime
    principal: str
    environment: str
    repository: str
    kinds: list[str]
    sha: str | None
    rule_id: uuid.UUID


class AccessExportOut(BaseModel):
    """The access file written for some scopes.

    Parameters
    ----------
    text
        The file's YAML text; planning it unedited plans nothing.
    """

    text: str


class AccessFileIn(BaseModel):
    """Request body for planning an access file.

    The text has no length limit here: the server names the limit in its answer (1 MiB) and
    the request body limit bounds the request.

    Parameters
    ----------
    file_name
        The file's base name, which the messages use, such as ``access.yaml``.
    text
        The file's content.
    allow_lockout
        Accept a file that drops the caller's own ``ADMIN`` role while another administrator
        remains.
    """

    model_config = ConfigDict(extra="forbid")

    file_name: str = "access.yaml"
    text: str
    allow_lockout: bool = False


class AccessChangeOut(BaseModel):
    """One change a plan makes.

    Parameters
    ----------
    op
        ``create``, ``change`` (a CI trust rule's settings) or ``remove``.
    entry
        Where the file lists the entry, such as ``grants[3]``; `null` for a removal.
    text
        The change in words.
    """

    op: str
    entry: str | None
    text: str


class AccessDriftOut(BaseModel):
    """Access in a managed scope that did not come from the file.

    Parameters
    ----------
    scope
        The scope it lies in.
    text
        What it is and what an apply does with it.
    by
        Who made it, when known.
    reason
        Why, when known.
    at
        When, when known.
    """

    scope: str
    text: str
    by: str | None
    reason: str | None
    at: datetime | None


class AccessPlanOut(BaseModel):
    """What applying an access file would change, and every problem that stops it.

    Parameters
    ----------
    file_name
        The base name the messages use.
    file_sha256
        Hash of the file's text.
    manages
        The scopes the file manages; empty when it did not parse.
    creates
        Entries the apply creates.
    changed
        CI trust rules the apply changes in place.
    removals
        Entries the apply removes.
    releases
        Managed scopes the file no longer lists.
    starts
        Scopes the file manages that no apply manages yet; changes made by hand there are
        refused once the file is applied.
    drift
        Access in the scopes that did not come from the file.
    problems
        Every problem; an apply refuses while there is one.
    fingerprint
        Hash of the live state of the managed scopes, which an apply checks again.
    """

    file_name: str
    file_sha256: str
    manages: list[str]
    creates: list[AccessChangeOut]
    changed: list[AccessChangeOut]
    removals: list[AccessChangeOut]
    releases: list[str]
    starts: list[str]
    drift: list[AccessDriftOut]
    problems: list[str]
    fingerprint: str


class AccessApplyIn(BaseModel):
    """Request body for applying an access file.

    Parameters
    ----------
    file_name
        The file's base name, which the messages and the record use.
    text
        The file's content.
    plan_fingerprint
        The ``fingerprint`` of the plan the caller reviewed (64 hex digits); the apply is
        refused with 409 when the file or access changed since.
    source_commit
        The commit the file came from, 7 to 64 hex digits; recorded with the apply.
    allow_lockout
        Accept a file that drops the caller's own ``ADMIN`` role while another administrator
        remains.
    """

    model_config = ConfigDict(extra="forbid")

    file_name: str = "access.yaml"
    text: str
    plan_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_commit: str | None = None
    allow_lockout: bool = False


class AccessApplyOut(BaseModel):
    """What an apply did, as the REST answer of `AccessService.apply`.

    Parameters
    ----------
    apply_id
        Find the apply's audit events by it: each carries it as ``access_apply_id``.
    file_sha256
        SHA-256 of the applied text, 64 hex digits.
    source_commit
        `--source-commit` as sent, lower case, or `null`.
    created
        Count of new entries.
    changed
        Count of CI trust rules updated in place.
    removed
        Count of entries taken away.
    released
        Scopes that are no longer managed after this apply.
    applied_at
        Commit time of the apply, UTC.
    """

    apply_id: uuid.UUID
    file_sha256: str
    source_commit: str | None
    created: int
    changed: int
    removed: int
    released: list[str]
    applied_at: datetime


class ManagedScopeOut(BaseModel):
    """A scope an access file manages, and its last apply.

    Parameters
    ----------
    scope
        ``instance`` or an environment's name.
    file_name
        The name of the file that last applied it.
    file_sha256
        That file's hash.
    source_commit
        The commit the file came from, when given.
    applied_by
        Who applied it; `null` once that principal is deleted.
    applied_at
        When.
    """

    scope: str
    file_name: str
    file_sha256: str
    source_commit: str | None
    applied_by: str | None
    applied_at: datetime


class AccessOverrideOut(BaseModel):
    """A change made by hand in a managed scope that no apply has taken into account yet.

    Parameters
    ----------
    scope
        The managed scope.
    change
        What was changed.
    reason
        Why.
    by
        Who changed it; `null` once that principal is deleted.
    at
        When.
    """

    scope: str
    change: str
    reason: str
    by: str | None
    at: datetime


class AccessStatusOut(BaseModel):
    """The scopes access files manage, and the open overrides.

    Parameters
    ----------
    scopes
        Every managed scope, sorted by name.
    overrides
        Every open override, oldest first.
    """

    scopes: list[ManagedScopeOut]
    overrides: list[AccessOverrideOut]


class SessionOut(BaseModel):
    """Response body for `GET /api/v1/sign-in/session`: the browser session of the caller.

    Carries no secret: neither the cookie, nor the CSRF token, nor any hash of them.

    Parameters
    ----------
    name
        The signed-in person's principal name.
    display_name
        The person's display name; `null` for none.
    type
        The principal type (always ``USER``: service accounts cannot sign in).
    via
        How the session began: ``setup``, ``invite``, ``recovery``, ``local`` or ``passkey``.
    step_up_until
        Until when actions that need a fresh passkey touch are allowed without another touch;
        `null` when the session has no touch.
    needs_passkey
        Whether the person must register a passkey before going on (a session from a setup,
        invite or recovery link on an account without a usable passkey).
    offer_passkey
        Whether the interface offers, without forcing, to register a passkey (a local sign-in
        on an account without one).
    absolute_expires_at
        When the session ends at the latest, however active.
    """

    name: str
    display_name: str | None
    type: Literal["USER", "SERVICE_ACCOUNT"]
    via: str
    step_up_until: datetime | None
    needs_passkey: bool
    offer_passkey: bool
    absolute_expires_at: datetime


class BrowserSessionOut(BaseModel):
    """One open browser session of a person, in `GET /api/v1/principals/{name}/sessions`.

    Carries no secret and no part of the cookie.

    Parameters
    ----------
    id
        The session's id.
    via
        How the session began.
    created_at
        When it began.
    last_seen_at
        About when the person last used it (written at most once a minute).
    absolute_expires_at
        When it ends at the latest.
    user_agent
        The browser it began in, as the browser named itself.
    browser
        The same in plain words for the common browsers ("Firefox 158 on Ubuntu"), otherwise
        `user_agent` itself; empty when the browser sent none.
    peer
        The address it began from, as the server saw it.
    current
        Whether this is the session of the request that asks.
    """

    id: uuid.UUID
    via: str
    created_at: datetime
    last_seen_at: datetime
    absolute_expires_at: datetime
    user_agent: str
    browser: str
    peer: str
    current: bool


class SignInBoundsOut(BaseModel):
    """The limits an administrator may choose the session lengths between.

    Parameters
    ----------
    idle_timeout_minutes_min
        The shortest idle timeout, in minutes.
    idle_timeout_minutes_max
        The longest idle timeout, in minutes.
    absolute_lifetime_minutes_min
        The shortest absolute lifetime, in minutes.
    absolute_lifetime_minutes_max
        The longest absolute lifetime, in minutes.
    """

    idle_timeout_minutes_min: int
    idle_timeout_minutes_max: int
    absolute_lifetime_minutes_min: int
    absolute_lifetime_minutes_max: int


class SignInSettingsOut(BaseModel):
    """Response body of `GET` and `PATCH /api/v1/settings/sign-in`.

    Parameters
    ----------
    idle_timeout_minutes
        How long a session lasts without a request the person started, in minutes.
    absolute_lifetime_minutes
        How long a session lasts at most, in minutes.
    bounds
        The limits both may be chosen between.
    """

    idle_timeout_minutes: int
    absolute_lifetime_minutes: int
    bounds: SignInBoundsOut


class SignInSettingsIn(BaseModel):
    """Request body of `PATCH /api/v1/settings/sign-in`; a field left out keeps its value.

    A change applies to sessions that begin afterwards; open sessions keep their lengths.

    Parameters
    ----------
    idle_timeout_minutes
        The new idle timeout, in minutes (15 to 1440).
    absolute_lifetime_minutes
        The new absolute lifetime, in minutes (60 to 43200, which is 30 days); not shorter than
        the idle timeout.
    """

    model_config = ConfigDict(extra="forbid")

    idle_timeout_minutes: int | None = Field(default=None, ge=1, le=10_000_000)
    absolute_lifetime_minutes: int | None = Field(default=None, ge=1, le=10_000_000)


class LinkIdIn(BaseModel):
    """Request body of `POST /api/v1/sign-in/links/describe`: the link's public id only.

    Parameters
    ----------
    link_id
        The public id, the ``l=`` part after the ``#`` of the link.
    """

    model_config = ConfigDict(extra="forbid")

    link_id: str = Field(min_length=1, max_length=64)


class LinkRedeemIn(BaseModel):
    """Request body of `POST /api/v1/sign-in/links/redeem`: the link's two halves.

    Parameters
    ----------
    link_id
        The public id, the ``l=`` part after the ``#`` of the link.
    secret
        The secret, the ``s=`` part. Never logged and never stored.
    """

    model_config = ConfigDict(extra="forbid")

    link_id: str = Field(min_length=1, max_length=64)
    secret: str = Field(min_length=1, max_length=128)


class LinkDescriptionOut(BaseModel):
    """Response body of `POST /api/v1/sign-in/links/describe`: what the sign-in page shows.

    Parameters
    ----------
    kind
        ``setup``, ``invite``, ``recovery`` or ``local``.
    name
        The principal name the link signs in as (an invite: the account it creates).
    display_name
        The person's display name, if one is known.
    state
        Always ``open`` here; a link that cannot be used answers 410 with its state.
    """

    kind: str
    name: str
    display_name: str | None
    state: str


class SignInOut(BaseModel):
    """Response body of `POST /api/v1/sign-in/links/redeem`; the session comes as a cookie.

    Parameters
    ----------
    name
        The principal name now signed in.
    needs_passkey
        Whether the person must register a passkey now (setup, invite and recovery links on an
        account without one).
    offer_passkey
        Whether the interface offers, never forces, a passkey (a local link on an account
        without one).
    csrf_token
        The session's CSRF token, shown here only: send it in `X-Lakana-CSRF` with every
        request that carries the session cookie, reads too. Keep it in the page's own storage;
        the server keeps only its hash and never returns it again.
    """

    name: str
    needs_passkey: bool
    offer_passkey: bool
    csrf_token: str


class IssuedLinkOut(BaseModel):
    """Response body of `POST /api/v1/sign-in/setup-link` and `/local-link`.

    Parameters
    ----------
    url
        The link, shown only here: whoever has it can sign in once until it expires.
    expires_at
        When it stops working.
    """

    url: str
    expires_at: datetime


class InviteIn(BaseModel):
    """Request body of `POST /api/v1/invites`.

    Parameters
    ----------
    name
        The new person's principal name; refused if any principal or open invite has it in
        any letter case.
    display_name
        The person's name as the interface shows it; optional.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    display_name: str | None = Field(default=None, max_length=255)


class InviteOut(BaseModel):
    """Response body of `POST /api/v1/invites`: the invite with its link, shown only here.

    Parameters
    ----------
    id
        The invite's id, to withdraw it by.
    name
        The principal name the invite creates.
    display_name
        The display name, if one was given.
    url
        The link to send to the person; valid for 7 days, once.
    expires_at
        When it stops working.
    """

    id: uuid.UUID
    name: str
    display_name: str | None
    url: str
    expires_at: datetime


class OpenInviteOut(BaseModel):
    """One open invite in `GET /api/v1/invites`; without its link, which is shown only once.

    Parameters
    ----------
    id
        The invite's id.
    name
        The principal name it creates.
    display_name
        The display name, if one was given.
    created_at
        When it was issued.
    expires_at
        When it stops working.
    """

    id: uuid.UUID
    name: str
    display_name: str | None
    created_at: datetime
    expires_at: datetime


class RecoveryLinkOut(BaseModel):
    """Response body of `POST /api/v1/principals/{name}/recovery-link`, shown only here.

    Parameters
    ----------
    id
        The link's id, to withdraw it by.
    name
        The person it recovers.
    url
        The link to send to the person; valid for 1 hour, once.
    expires_at
        When it stops working.
    """

    id: uuid.UUID
    name: str
    url: str
    expires_at: datetime


class OpenRecoveryLinkOut(BaseModel):
    """One open recovery link in `GET /api/v1/recovery-links`; without the link itself.

    Parameters
    ----------
    id
        The link's id.
    name
        The person it recovers.
    created_at
        When it was issued.
    expires_at
        When it stops working.
    """

    id: uuid.UUID
    name: str
    created_at: datetime
    expires_at: datetime


class SignInNoticeOut(BaseModel):
    """One sign-in notice of the caller, in `GET /api/v1/sign-in/notices`.

    Parameters
    ----------
    id
        The notice's id, to dismiss it by.
    kind
        What happened, such as ``recovery_used`` (a recovery link was used).
    subject
        The name of the person it is about, if any.
    created_at
        When it happened.
    """

    id: uuid.UUID
    kind: str
    subject: str | None
    created_at: datetime


class PasskeyOptionsOut(BaseModel):
    """Response body of the passkey options routes: one WebAuthn ceremony to answer.

    Parameters
    ----------
    ceremony_id
        Sent back with the browser's answer. The challenge works once, for 5 minutes.
    public_key
        The ``publicKey`` options in their JSON form: the interface passes them to
        `PublicKeyCredential.parseCreationOptionsFromJSON` (adding a passkey) or
        `parseRequestOptionsFromJSON` (signing in, step-up).
    """

    ceremony_id: uuid.UUID
    public_key: dict[str, Any]


class PasskeyAnswerIn(BaseModel):
    """Request body of `POST /api/v1/sign-in/passkey` and `POST /api/v1/sign-in/step-up`.

    Parameters
    ----------
    ceremony_id
        The id the options route returned.
    answer
        Sent as ``credential``: the browser's `credential.toJSON()` for that ceremony. Never
        logged and never stored.
    """

    model_config = ConfigDict(extra="forbid")

    ceremony_id: uuid.UUID
    # Named `answer` in Python, so no route reads a `.credential` attribute: that name is the
    # principal's credential kind, which the session parity test keeps out of route modules.
    answer: dict[str, Any] = Field(alias="credential")


class PasskeyRegisterIn(BaseModel):
    """Request body of `POST /api/v1/principals/{name}/passkeys`.

    Parameters
    ----------
    ceremony_id
        The id `POST /api/v1/principals/{name}/passkeys/options` returned.
    answer
        Sent as ``credential``: the browser's `credential.toJSON()` for that ceremony.
    name
        What to call the passkey, such as the device it is on; ``Passkey`` when left out.
    """

    model_config = ConfigDict(extra="forbid")

    ceremony_id: uuid.UUID
    answer: dict[str, Any] = Field(alias="credential")
    name: str | None = Field(default=None, max_length=100)


class PasskeyOut(BaseModel):
    """One passkey of a person; never its key or its credential id.

    Parameters
    ----------
    id
        The passkey's id, for removing it.
    name
        What its person called it.
    created_at
        When it was added.
    last_used_at
        When it last signed in or confirmed a step-up; `None` for never.
    backup_eligible
        Whether it is a synced passkey (for example in a password manager or a platform
        account), which may exist on several devices.
    disabled_reason
        ``possible_clone`` when it was disabled because its signature counter went backwards;
        `None` while it works.
    usable_here
        Whether it was registered for this server's current public address. Changing the
        address (`LAKANA_UI__PUBLIC_URL`) leaves earlier passkeys unusable.
    """

    id: uuid.UUID
    name: str
    created_at: datetime
    last_used_at: datetime | None
    backup_eligible: bool
    disabled_reason: str | None
    usable_here: bool


class StepUpOut(BaseModel):
    """Response body of `POST /api/v1/sign-in/step-up`.

    Parameters
    ----------
    step_up_until
        Until when step-up actions (such as adding or removing a passkey) need no new passkey
        touch in this session.
    """

    step_up_until: datetime
