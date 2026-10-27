"""The `tables` resource: `Client.tables`.

A table is addressed as a dot-joined `"namespace.table"` string, such as
`"bronze.sales"` or `"gold.finance.sales"` (the namespace itself may be
dotted; only the final component is the table name) — matching how the
platform API splits a table's namespace path from its name across two
distinct path parameters.
"""

from __future__ import annotations

import io
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any, Protocol

import httpx
import pyarrow as pa
import pyarrow.ipc

from lakana._arrow import (
    cast_timestamp_ns_columns_to_us,
    normalize_view_columns,
    schema_to_columns,
)
from lakana._http import HttpTransport
from lakana._models import (
    AppendResult,
    CheckHealth,
    Column,
    DroppedTable,
    ExternalTable,
    LastMaintenance,
    Lineage,
    LineageEdge,
    LineageNode,
    LoadResult,
    MergeResult,
    Reader,
    Table,
    TableHealth,
    TableReference,
    TableRetention,
    WriteResult,
)
from lakana._routes import ROUTES
from lakana.errors import Conflict, LakanaConnectionError, LakanaError, ValidationError
from lakana.resources.queries import QueryRun

_APPEND_CONTENT_TYPE = "application/vnd.apache.arrow.stream"
_APPEND_TIMEOUT_SECONDS = 300.0
"""Per-request timeout override for `Tables.append`.

Separate from `Client`'s general `timeout` (default 30s): an append body can
be large enough that 30s is not a reasonable bound on its own. A timeout
here is ambiguous, not a clean failure — the server may have already
committed the append by the time the client gives up waiting for the
response — so a caller that retries blindly on a timeout can duplicate
rows; only a `Conflict` (409) is safe to retry automatically, which is what
`append` already does below."""

_MAX_APPEND_BATCH_ROWS = 65_536
"""Largest batch `Tables.append` writes per Arrow IPC message. Bounding rows-per-batch bounds how
many batches a normal
SDK upload produces, keeping it far below the server's
`Settings.append.max_arrays` cap without a caller needing to think about
batching at all."""


def _add_source_versions(
    params: dict[str, object], source_versions: Mapping[str, int] | None
) -> None:
    """Add the `source_version` parameters of an Arrow write, sorted by name.

    Parameters
    ----------
    params
        The query parameters, extended in place.
    source_versions
        Artifact version by dotted name; nothing is added when empty or `None`.
    """
    if source_versions:
        params["source_version"] = [
            f"{version}:{name}" for name, version in sorted(source_versions.items())
        ]


def _split_namespace_table(qualified_name: str) -> tuple[str, str]:
    """Split a `"namespace.table"` string into its namespace and table name.

    Parameters
    ----------
    qualified_name
        A dot-joined `"namespace.table"` string. Split on the *last* `.`,
        so a dotted namespace (`"gold.finance.sales"` -> namespace
        `"gold.finance"`, table `"sales"`) is handled correctly.

    Returns
    -------
    tuple of str
        `(namespace, name)`.

    Raises
    ------
    ValidationError
        If `qualified_name` has no `.` separator at all.
    """
    namespace, separator, name = qualified_name.rpartition(".")
    if not separator:
        raise ValidationError(f"Expected a dotted 'namespace.table' name, got {qualified_name!r}.")
    return namespace, name


def _when(value: str | None) -> datetime | None:
    """Parse a time the platform API sent.

    Parameters
    ----------
    value
        An ISO 8601 time, or `None`.

    Returns
    -------
    datetime or None
        The time, or `None` for `None`.
    """
    return None if value is None else datetime.fromisoformat(value)


def _health_from_json(data: dict[str, Any]) -> TableHealth:
    """Build a `TableHealth` from the platform API's JSON response body.

    Parameters
    ----------
    data
        The decoded `TableHealthOut` JSON body.

    Returns
    -------
    TableHealth
        The parsed health.
    """
    maintenance = data["last_maintenance"]
    readers = data["readers"]
    return TableHealth(
        days=data["days"],
        last_maintenance=None
        if maintenance is None
        else LastMaintenance(
            run_id=maintenance["run_id"],
            finished_at=datetime.fromisoformat(maintenance["finished_at"]),
            status=maintenance["status"],
            snapshots_expired=maintenance["snapshots_expired"],
            files_deleted=maintenance["files_deleted"],
            bytes_deleted=maintenance["bytes_deleted"],
            tags_removed=maintenance["tags_removed"],
        ),
        checks=[
            CheckHealth(
                name=c["name"],
                check=c["check"],
                run_id=c["run_id"],
                outcome=c["outcome"],
                on_failure=c["on_failure"],
                failing_rows=c["failing_rows"],
                error=c["error"],
                rollback_outcome=c["rollback_outcome"],
                kept=c["kept"],
                not_published=c["not_published"],
                finished_at=datetime.fromisoformat(c["finished_at"]),
            )
            for c in data["checks"]
        ],
        readers=None
        if readers is None
        else [
            Reader(
                principal=r["principal"],
                read_count=r["read_count"],
                last_read_at=datetime.fromisoformat(r["last_read_at"]),
            )
            for r in readers
        ],
    )


def _table_from_json(data: dict[str, Any]) -> Table:
    """Build a `Table` from the platform API's JSON response body.

    Parameters
    ----------
    data
        The decoded `TableOut` JSON body. An older server sends no
        `schema`; `columns` is then empty. An older server
        sends no `kind`.

    Returns
    -------
    Table
        The parsed table.
    """
    return Table(
        name=data["name"],
        namespace=data["namespace"],
        uuid=data["uuid"],
        metadata_location=data.get("metadata_location"),
        columns=[
            Column(
                name=c["name"],
                type=c["type"],
                nullable=c["nullable"],
                description=c.get("description"),
                description_source=c.get("description_source"),
            )
            for c in data.get("schema", [])
        ],
        kind=data.get("kind", "TABLE"),
        sql=data.get("sql"),
        references=[
            TableReference(id=r["id"], name=r["name"]) for r in data.get("references") or []
        ],
        hidden_references=data.get("hidden_references", 0),
        snapshot_id=data.get("snapshot_id"),
        state=data.get("state", "published"),
        state_message=data.get("state_message"),
        description=data.get("description"),
        description_source=data.get("description_source"),
        last_write_at=_when(data.get("last_write_at")),
        row_count=data.get("row_count"),
        size_bytes=data.get("size_bytes"),
        file_count=data.get("file_count"),
        expires_at=_when(data.get("expires_at")),
        expires_soon=data.get("expires_soon", False),
    )


def _retention_from_json(data: dict[str, Any]) -> TableRetention:
    """Build a `TableRetention` from the platform API's JSON response body.

    Parameters
    ----------
    data
        The decoded `TableRetentionOut` JSON body.

    Returns
    -------
    TableRetention
        The parsed retention.
    """
    on = data.get("on")
    return TableRetention(
        name=data["name"],
        namespace=data["namespace"],
        uuid=data["uuid"],
        source=data.get("source"),
        days=data.get("days"),
        on=None if on is None else date.fromisoformat(on),
        never=data.get("never", False),
        set_by=data.get("set_by"),
        set_at=_when(data.get("set_at")),
        last_activity_at=datetime.fromisoformat(data["last_activity_at"]),
        expires_at=_when(data.get("expires_at")),
        kept=data.get("kept"),
    )


def _external_table_from_json(data: dict[str, Any]) -> ExternalTable:
    """Build an `ExternalTable` from the platform API's JSON response body.

    Parameters
    ----------
    data
        The decoded `ExternalTableOut` JSON body.

    Returns
    -------
    ExternalTable
        The parsed external table.
    """
    return ExternalTable(
        name=data["name"],
        namespace=data["namespace"],
        location_pattern=data["location_pattern"],
        format=data["format"],
        schema=data["schema"],
    )


def _append_result_from_json(data: dict[str, Any]) -> AppendResult:
    """Build an `AppendResult` from the platform API's JSON response body.

    Parameters
    ----------
    data
        The decoded `AppendOut` JSON body.

    Returns
    -------
    AppendResult
        The parsed append result.
    """
    return AppendResult(rows=data["rows"], files=data["files"], snapshot_id=data["snapshot_id"])


def _write_result_from_json(data: dict[str, Any]) -> WriteResult:
    """Build a `WriteResult` from the platform API's JSON response body.

    Parameters
    ----------
    data
        The decoded `ReplaceOut` JSON body.

    Returns
    -------
    WriteResult
        The parsed result.
    """
    return WriteResult(
        rows=data["rows"],
        files=data["files"],
        snapshot_id=data["snapshot_id"],
        schema_diff=data.get("schema_diff"),
    )


_DroppedList = list[DroppedTable]
"""Alias for `list[DroppedTable]`, resolved at module scope for the reason of `_TableList`."""

_RetentionList = list[TableRetention]
"""Alias for `list[TableRetention]`, resolved at module scope for the reason of `_TableList`."""

_TableList = list[Table]
"""Alias for `list[Table]`, resolved at module scope.

`Tables` defines a method named `list`, which shadows the builtin `list`
for any bare `list[...]` annotation written *inside* the class body — this
alias (and `_ColumnSchema` below) are resolved before that happens, and
referenced by name instead.
"""

_ColumnSchema = list[dict[str, Any]]
"""Alias for a JSON-shaped column list, for the same reason as `_TableList`."""


def _lineage_from_json(data: dict[str, Any]) -> Lineage:
    """Build a `Lineage` from the platform API's JSON response body.

    Parameters
    ----------
    data
        The decoded `LineageOut` JSON body. `truncated` may be missing from
        an older server's body and then reads as `False`; a node's `connection_kind`, as
        `None`.

    Returns
    -------
    Lineage
        The parsed graph.
    """
    return Lineage(
        root_id=data["root_id"],
        direction=data["direction"],
        depth=data["depth"],
        nodes=[
            LineageNode(
                id=n["id"],
                name=n["name"],
                kind=n["kind"],
                dropped=n["dropped"],
                connection_kind=n.get("connection_kind"),
            )
            for n in data["nodes"]
        ],
        edges=[
            LineageEdge(
                source_id=e["source_id"],
                target_id=e["target_id"],
                source_snapshot_id=e["source_snapshot_id"],
                target_snapshot_id=e["target_snapshot_id"],
                query_run_id=e["query_run_id"],
                created_at=datetime.fromisoformat(e["created_at"]),
            )
            for e in data["edges"]
        ],
        hidden_nodes=data["hidden_nodes"],
        truncated=data.get("truncated", False),
    )


class _ArrowSchemaLike(Protocol):
    """Anything exposing the Arrow PyCapsule schema interface, e.g. a polars `Schema`."""

    def __arrow_c_schema__(self) -> object:
        """Export the schema as an Arrow C schema capsule.

        Returns
        -------
        object
            The PyCapsule.
        """
        ...


def _coerce_to_table(data: pa.Table | pa.RecordBatch | object) -> pa.Table:
    """Coerce append data to a `pyarrow.Table`.

    Parameters
    ----------
    data
        A `pa.Table`, a `pa.RecordBatch`, or anything `pyarrow.table()`
        accepts (e.g. a dict of columns).

    Returns
    -------
    pa.Table
        The data as a table.
    """
    if isinstance(data, pa.Table):
        return data
    if isinstance(data, pa.RecordBatch):
        return pa.Table.from_batches([data])
    return pa.table(data)


def _serialize_ipc_stream(table: pa.Table) -> bytes:
    """Serialize a table to an uncompressed Arrow IPC stream, batched at a bounded size.

    Writes each of `table.to_batches(max_chunksize=_MAX_APPEND_BATCH_ROWS)`
    as its own IPC message, rather than `write_table`'s default chunking,
    so a normal SDK upload never produces more record batches than
    necessary — the caller (`Tables.append`)
    calls `table.combine_chunks()` first, so `to_batches` here starts from
    as few input chunks as possible and splits only where a batch would
    otherwise exceed the row bound.

    Parameters
    ----------
    table
        The table to serialize.

    Returns
    -------
    bytes
        The IPC stream bytes, as the platform API's append route expects.
    """
    sink = io.BytesIO()
    with pyarrow.ipc.new_stream(sink, table.schema) as writer:
        for batch in table.to_batches(max_chunksize=_MAX_APPEND_BATCH_ROWS):
            writer.write_batch(batch)
    return sink.getvalue()


_POLARS_SCHEMA_TOO_OLD = (
    "A polars Schema as the table's schema needs Polars 1.37 or newer; the installed Polars "
    "cannot hand its schema to Arrow. Upgrade Polars, or pass `pyarrow.schema(...)` or a list of "
    "columns instead."
)


class Tables:
    """The `tables` resource of the platform API.

    Parameters
    ----------
    transport
        The HTTP transport to send requests through.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def list(self, environment: str, namespace: str) -> _TableList:
        """List the tables in a namespace.

        Parameters
        ----------
        environment
            The environment name.
        namespace
            Dot-joined namespace path.

        Returns
        -------
        list of Table
            Every table in the namespace.

        Raises
        ------
        lakana.errors.NotFound
            If the namespace does not exist.
        """
        response = self._transport.request(
            ROUTES["tables.list"], path_params={"env": environment, "path": namespace}
        )
        return [_table_from_json(item) for item in response.json()]

    def get(self, environment: str, qualified_name: str) -> Table:
        """Fetch a table by its `"namespace.table"` name.

        Parameters
        ----------
        environment
            The environment name.
        qualified_name
            Dot-joined `"namespace.table"` name, such as `"bronze.sales"`.

        Returns
        -------
        Table
            The table.

        Raises
        ------
        lakana.errors.NotFound
            If no such table exists.
        """
        namespace, name = _split_namespace_table(qualified_name)
        response = self._transport.request(
            ROUTES["tables.get"], path_params={"env": environment, "path": namespace, "name": name}
        )
        return _table_from_json(response.json())

    def health(
        self, environment: str, qualified_name: str, *, days: int | None = None
    ) -> TableHealth:
        """Show a table's last maintenance, the newest result of each check, and its readers.

        Parameters
        ----------
        environment
            The environment name.
        qualified_name
            Dot-joined `"namespace.table"` name.
        days
            The window of the readers in days, from 1 to the server's kept period (90 by
            default); the server's default window (30) when `None`.

        Returns
        -------
        TableHealth
            The health. `readers` is `None` unless you may see the table's grants (the `ADMIN`
            role).

        Raises
        ------
        lakana.errors.NotFound
            If no such table exists.
        lakana.errors.PermissionDenied
            Without `READ_TABLE` on the table.
        lakana.errors.ValidationError
            If `days` is outside the range; the message names it.
        """
        namespace, name = _split_namespace_table(qualified_name)
        response = self._transport.request(
            ROUTES["tables.health"],
            path_params={"env": environment, "path": namespace, "name": name},
            params=None if days is None else {"days": days},
        )
        return _health_from_json(response.json())

    def describe(
        self,
        environment: str,
        qualified_name: str,
        description: str | None,
        *,
        column: str | None = None,
    ) -> Table:
        """Set the description of a table or one of its columns, or reset it.

        The text set here is shown instead of the one a deploy publishes from the asset header,
        and a deploy never replaces it. Resetting removes it, so the asset header's text shows
        again. Needs to own the table or hold `DROP_TABLE` on it.

        Parameters
        ----------
        environment
            The environment name.
        qualified_name
            Dot-joined `"namespace.table"` name.
        description
            The new text (1 to 4,000 characters, not blank), or `None` to reset.
        column
            A column of the table to describe instead of the table.

        Returns
        -------
        Table
            The table, with the descriptions it shows now.

        Raises
        ------
        lakana.errors.NotFound
            If the table, or the column, does not exist.
        lakana.errors.PermissionDenied
            Without the right to describe the table.
        lakana.errors.ValidationError
            If the text is blank or over 4,000 characters.
        """
        namespace, name = _split_namespace_table(qualified_name)
        path_params = {"env": environment, "path": namespace, "name": name}
        if column is None:
            route = ROUTES["tables.describe"]
        else:
            route = ROUTES["tables.describe_column"]
            path_params["column"] = column
        response = self._transport.request(
            route, path_params=path_params, json={"description": description}
        )
        return _table_from_json(response.json())

    def delete(
        self,
        environment: str,
        qualified_name: str,
        *,
        purge: bool = False,
        break_glass_reason: str | None = None,
    ) -> None:
        """Drop a table. A managed table's files are kept for the undo period, then deleted.

        Within the server's undo period (`LAKANA_TABLES__UNDO_DAYS`, 7 days by default)
        `undrop` restores the table with its data, grants, row filters and column masks.
        An external table's files and a view are never deleted.

        Parameters
        ----------
        environment
            The environment name.
        qualified_name
            Dot-joined `"namespace.table"` name.
        purge
            Delete the files at the server's next sweep instead; the table cannot be restored.
        break_glass_reason
            Write to a protected environment outside a run: 10-500 characters, needs
            `MANAGE_ENVIRONMENT`, audited. The next run of the deployed code may overwrite
            the fix.

        Raises
        ------
        lakana.errors.NotFound
            If no such table exists.
        """
        namespace, name = _split_namespace_table(qualified_name)
        params = {"purge": "true"} if purge else {}
        if break_glass_reason is not None:
            params["break_glass_reason"] = break_glass_reason
        self._transport.request(
            ROUTES["tables.delete"],
            path_params={"env": environment, "path": namespace, "name": name},
            params=params or None,
        )

    def undrop(
        self,
        environment: str,
        qualified_name: str,
        *,
        new_name: str | None = None,
        break_glass_reason: str | None = None,
    ) -> Table:
        """Restore a dropped table within its undo period: the same table, data and policies.

        Needs `DROP_TABLE` and `CREATE_TABLE` on the namespace. When the name was dropped more
        than once, the newest drop comes back; restore an older one under another name.

        Parameters
        ----------
        environment
            The environment name.
        qualified_name
            Dot-joined `"namespace.table"` name the table had.
        new_name
            Restore it under this name in the same namespace, when its own name is taken.
        break_glass_reason
            Write to a protected environment outside a run: 10-500 characters, needs
            `MANAGE_ENVIRONMENT`, audited.

        Returns
        -------
        Table
            The restored table.

        Raises
        ------
        lakana.errors.NotFound
            If no dropped table of that name can be restored, or its namespace is gone.
        lakana.errors.Conflict
            If a table of the name exists (pass `new_name`), the undo period has ended, or the
            files are being deleted.
        """
        namespace, name = _split_namespace_table(qualified_name)
        response = self._transport.request(
            ROUTES["tables.undrop"],
            path_params={"env": environment, "path": namespace, "name": name},
            json={"new_name": new_name, "break_glass_reason": break_glass_reason},
        )
        return _table_from_json(response.json())

    def retention(self, environment: str, qualified_name: str) -> TableRetention:
        """Show a managed table's retention, where it comes from and when the table expires.

        Parameters
        ----------
        environment
            The environment name.
        qualified_name
            Dot-joined `"namespace.table"` name.

        Returns
        -------
        TableRetention
            The retention; `expires_at` is `None` when the table never expires.

        Raises
        ------
        lakana.errors.NotFound
            If no such table exists.
        lakana.errors.ValidationError
            For a view, an external table or an artifact, which have no retention.
        """
        namespace, name = _split_namespace_table(qualified_name)
        response = self._transport.request(
            ROUTES["tables.retention"],
            path_params={"env": environment, "path": namespace, "name": name},
        )
        return _retention_from_json(response.json())

    def set_retention(
        self,
        environment: str,
        qualified_name: str,
        *,
        days: int | None = None,
        on: date | None = None,
        never: bool = False,
    ) -> TableRetention:
        """Set when a managed table expires. Give exactly one of `days`, `on` and `never`.

        An expired table is dropped by the server's sweep and can be restored with `undrop`
        during the undo period. The table's owner may always set it. Anyone else needs
        `DROP_TABLE` to make the table expire earlier (or at all), and `WRITE_TABLE` to make it
        expire later or never.

        Parameters
        ----------
        environment
            The environment name.
        qualified_name
            Dot-joined `"namespace.table"` name.
        days
            Expire this many days (1 to 3,650) after the table's last activity: the later of its
            last change and its last recorded read.
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
        lakana.errors.NotFound
            If no such table exists.
        lakana.errors.PermissionDenied
            Without the right the change needs.
        lakana.errors.ValidationError
            Unless exactly one valid rule is given; for anything but a managed table; and in a
            protected environment, whose tables never expire.
        """
        namespace, name = _split_namespace_table(qualified_name)
        response = self._transport.request(
            ROUTES["tables.set_retention"],
            path_params={"env": environment, "path": namespace, "name": name},
            json={"days": days, "on": None if on is None else on.isoformat(), "never": never},
        )
        return _retention_from_json(response.json())

    def expiring(self, environment: str, *, within_days: int | None = None) -> _RetentionList:
        """List the managed tables of an environment that expire soon, soonest first.

        Parameters
        ----------
        environment
            The environment name.
        within_days
            The window, 1 to 3,650 days; the server's warning window (7 days by default) when
            `None`.

        Returns
        -------
        list of TableRetention
            The tables you may read that expire within the window, with who set the retention.

        Raises
        ------
        lakana.errors.NotFound
            If the environment does not exist.
        lakana.errors.ValidationError
            If `within_days` is out of range.
        """
        response = self._transport.request(
            ROUTES["tables.expiring"],
            path_params={"env": environment},
            params=None if within_days is None else {"within_days": within_days},
        )
        return [_retention_from_json(item) for item in response.json()]

    def list_dropped(self, environment: str, namespace: str) -> _DroppedList:
        """List the dropped tables of a namespace whose files are not deleted yet.

        Parameters
        ----------
        environment
            The environment name.
        namespace
            Dot-joined namespace path.

        Returns
        -------
        list of DroppedTable
            Newest drop first, each with the time its files are deleted.

        Raises
        ------
        lakana.errors.NotFound
            If the namespace does not exist.
        """
        response = self._transport.request(
            ROUTES["tables.list_dropped"], path_params={"env": environment, "path": namespace}
        )
        return [
            DroppedTable(
                name=item["name"],
                namespace=item["namespace"],
                uuid=item["uuid"],
                kind=item["kind"],
                dropped_at=datetime.fromisoformat(item["dropped_at"]),
                purge_after=datetime.fromisoformat(item["purge_after"]),
                restorable=item["restorable"],
            )
            for item in response.json()
        ]

    def create(
        self,
        environment: str,
        qualified_name: str,
        schema: pa.Schema | _ArrowSchemaLike | _ColumnSchema,
        *,
        break_glass_reason: str | None = None,
    ) -> Table:
        """Create a managed (Iceberg) table from a schema.

        Parameters
        ----------
        environment
            The environment name.
        qualified_name
            Dot-joined `"namespace.table"` name.
        schema
            The table's columns: a `pyarrow.Schema`, anything exposing the
            Arrow C schema interface such as a polars `Schema` (both
            converted via `lakana._arrow.schema_to_columns`), or an
            already-JSON-shaped list of `{"name", "type", "nullable"}` dicts. A polars
            `Schema` needs Polars 1.37 or newer; with an older Polars pass
            `pyarrow.schema(...)` or a list of columns instead.
        break_glass_reason
            Write to a protected environment outside a run: 10-500 characters, needs
            `MANAGE_ENVIRONMENT`, audited. The next run of the deployed code may overwrite
            the fix.

        Returns
        -------
        Table
            The created table.

        Raises
        ------
        lakana.errors.NotFound
            If the namespace does not exist.
        lakana.errors.Conflict
            If a table of this name already exists.
        lakana.errors.ValidationError
            If `schema` names a column type the SDK or server cannot
            represent, or is a polars `Schema` and the installed Polars is older than 1.37
            (it cannot export its schema to Arrow).
        TypeError
            If `schema` is neither a list nor anything `pyarrow.schema` accepts (and is not a
            polars `Schema`).
        """
        namespace, name = _split_namespace_table(qualified_name)
        if isinstance(schema, list):
            columns = schema
        else:
            try:
                arrow_schema = pa.schema(schema)
            except TypeError as exc:
                if type(schema).__module__.split(".")[0] != "polars":
                    raise
                raise ValidationError(_POLARS_SCHEMA_TOO_OLD) from exc
            columns = schema_to_columns(arrow_schema)
        body: dict[str, object] = {"name": name, "schema": columns}
        if break_glass_reason is not None:
            body["break_glass_reason"] = break_glass_reason
        response = self._transport.request(
            ROUTES["tables.create"], path_params={"env": environment, "path": namespace}, json=body
        )
        return _table_from_json(response.json())

    def create_external(
        self,
        environment: str,
        qualified_name: str,
        location_pattern: str,
        format: str,
        *,
        break_glass_reason: str | None = None,
    ) -> ExternalTable:
        """Register a raw-zone table backed by files already on storage.

        Parameters
        ----------
        environment
            The environment name.
        qualified_name
            Dot-joined `"namespace.table"` name.
        location_pattern
            A pattern shaped `"raw/<dir...>/<basename-glob>"`.
        format
            `"parquet"`, `"csv"`, or `"json"`.
        break_glass_reason
            Write to a protected environment outside a run: 10-500 characters, needs
            `MANAGE_ENVIRONMENT`, audited. The next run of the deployed code may overwrite
            the fix.

        Returns
        -------
        ExternalTable
            The registered table and its inferred schema.

        Raises
        ------
        lakana.errors.NotFound
            If the namespace does not exist.
        lakana.errors.ValidationError
            If `location_pattern` or `format` is invalid.
        """
        namespace, name = _split_namespace_table(qualified_name)
        body: dict[str, object] = {
            "name": name,
            "location_pattern": location_pattern,
            "format": format,
        }
        if break_glass_reason is not None:
            body["break_glass_reason"] = break_glass_reason
        response = self._transport.request(
            ROUTES["tables.create_external"],
            path_params={"env": environment, "path": namespace},
            json=body,
        )
        return _external_table_from_json(response.json())

    def append(
        self,
        environment: str,
        qualified_name: str,
        data: pa.Table | pa.RecordBatch | object,
        *,
        sources: Sequence[str] | None = None,
        source_versions: Mapping[str, int] | None = None,
        break_glass_reason: str | None = None,
        expected_snapshot_id: int | str | None = None,
    ) -> AppendResult:
        """Append rows to a managed table.

        Serialises `data` as an uncompressed Arrow IPC stream and retries
        the request exactly once if the server reports a `Conflict` (409) —
        another writer committed to the table first, and a retry against
        the table's now-current state is a reasonable default for a client
        that just wants its rows written.

        Sent with its own `_APPEND_TIMEOUT_SECONDS` (300s) timeout, longer
        than `Client`'s general default, since an append body can be large
        enough that the general default is not a reasonable bound. **A timeout here is ambiguous,
        not a clean
        failure**: the server may have already committed the append by the
        time the client gives up waiting for a response, so a caller that
        blindly retries a timed-out append can duplicate rows — unlike the
        `Conflict` retry below, which is safe precisely because a conflict
        response proves nothing was committed by *this* request.

        Parameters
        ----------
        environment
            The environment name.
        qualified_name
            Dot-joined `"namespace.table"` name.
        data
            The rows to append: a `pa.Table`, a `pa.RecordBatch`, or
            anything `pyarrow.table()` accepts.
        sources
            Dotted names of the tables `data` was derived from, recorded as
            lineage. Each must exist and be readable by the caller.
        source_versions
            The version of each source that is an artifact, by dotted name, when the caller
            read a specific one; the lineage records it instead of the current version.
        break_glass_reason
            Write to a protected environment outside a run: 10-500 characters, needs
            `MANAGE_ENVIRONMENT`, audited. The next run of the deployed code may overwrite
            the fix.
        expected_snapshot_id
            The table's current snapshot as you read it (`Table.snapshot_id`),
            or ``"none"`` for "no snapshot yet". The write commits only while
            that still holds; otherwise `Conflict` is raised and nothing is
            retried.

        Returns
        -------
        AppendResult
            How many rows and files were written, and the committed
            snapshot's id.

        Raises
        ------
        lakana.errors.NotFound
            If the table does not exist.
        lakana.errors.PermissionDenied
            If the caller may not write to this table.
        lakana.errors.Conflict
            If a second attempt, after one retry, still conflicts; or at once
            when `expected_snapshot_id` no longer matches.
        lakana.errors.ValidationError
            If the data's schema does not match the table's; an unsupported
            Arrow type is used; or a nanosecond-precision timestamp column
            would be truncated by the platform API's microsecond precision.
        """
        params: dict[str, object] = {"source": list(sources)} if sources else {}
        _add_source_versions(params, source_versions)
        if break_glass_reason is not None:
            params["break_glass_reason"] = break_glass_reason
        if expected_snapshot_id is not None:
            params["expected_snapshot_id"] = str(expected_snapshot_id)
        response = self._send_arrow(
            "tables.append",
            environment,
            qualified_name,
            data,
            params,
            retry_conflict=expected_snapshot_id is None,
        )
        return _append_result_from_json(response.json())

    def replace(
        self,
        environment: str,
        qualified_name: str,
        data: pa.Table | pa.RecordBatch | object,
        *,
        evolve_schema: bool = False,
        sources: Sequence[str] | None = None,
        source_versions: Mapping[str, int] | None = None,
        break_glass_reason: str | None = None,
        expected_snapshot_id: int | str | None = None,
    ) -> WriteResult:
        """Replace every row of a managed table.

        One snapshot: readers see the old rows or the new ones, never a mix,
        and time travel keeps the old data. Serialised and retried exactly
        like `append`; a timeout is just as ambiguous here.

        Parameters
        ----------
        environment
            The environment name.
        qualified_name
            Dot-joined `"namespace.table"` name.
        data
            The new rows: a `pa.Table`, a `pa.RecordBatch`, or anything
            `pyarrow.table()` accepts.
        evolve_schema
            Let the table adopt `data`'s schema instead of refusing a
            mismatch.
        sources
            Dotted names of the tables `data` was derived from.
        source_versions
            The version of each source that is an artifact, by dotted name, when the caller
            read a specific one.
        break_glass_reason
            Write to a protected environment outside a run: 10-500 characters, needs
            `MANAGE_ENVIRONMENT`, audited. The next run of the deployed code may overwrite
            the fix.
        expected_snapshot_id
            The table's current snapshot as you read it (`Table.snapshot_id`),
            or ``"none"`` for "no snapshot yet". The write commits only while
            that still holds; otherwise `Conflict` is raised and nothing is
            retried. On `replace`, ``"none"`` also creates a missing
            table from `data`.

        Returns
        -------
        WriteResult
            What was written, and how the schema changed.

        Raises
        ------
        lakana.errors.NotFound
            If the table or a source does not exist.
        lakana.errors.PermissionDenied
            If the caller may not write the table or read a source.
        lakana.errors.Conflict
            If a second attempt, after one retry, still conflicts; or at once
            when `expected_snapshot_id` no longer matches.
        lakana.errors.ValidationError
            If `data` doesn't match the table and `evolve_schema` is off.
        """
        params: dict[str, object] = {"evolve_schema": "true" if evolve_schema else "false"}
        if sources:
            params["source"] = list(sources)
        _add_source_versions(params, source_versions)
        if break_glass_reason is not None:
            params["break_glass_reason"] = break_glass_reason
        if expected_snapshot_id is not None:
            params["expected_snapshot_id"] = str(expected_snapshot_id)
        response = self._send_arrow(
            "tables.replace",
            environment,
            qualified_name,
            data,
            params,
            retry_conflict=expected_snapshot_id is None,
        )
        return _write_result_from_json(response.json())

    def upload_limit(self) -> int | None:
        """Ask the server for the largest Arrow body one write may send.

        Returns
        -------
        int or None
            `LAKANA_APPEND__MAX_BYTES` of the server, or `None` when it does not say (an older
            server); its own 413 then applies.

        Raises
        ------
        lakana.errors.LakanaConnectionError
            If the server could not be reached.
        """
        try:
            response = self._transport.request(ROUTES["info"], retry=False)
        except LakanaConnectionError:
            raise
        except LakanaError:
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        value = data.get("upload_max_bytes") if isinstance(data, dict) else None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            return None
        return value

    def load(
        self,
        environment: str,
        qualified_name: str,
        data: pa.Table | pa.RecordBatch | object,
        *,
        write: str = "replace",
        expected_snapshot_id: int | str | None = None,
        break_glass_reason: str | None = None,
        dry_run: bool = False,
    ) -> LoadResult:
        """Load rows into a table of the bronze layer, as `lakana load` does.

        The server refuses a table whose first namespace segment is not `bronze`, before it
        reads the rows, with a `ValidationError` that says to load into `bronze.<name>` and
        transform it with an asset. Loads are for one-off files; anything that runs again
        belongs in an asset. Never retried on a conflict: a load always names the snapshot it
        expects.

        Parameters
        ----------
        environment
            The environment name.
        qualified_name
            Dot-joined `"bronze.table"` (or a deeper `"bronze.…"`) name.
        data
            The rows: a `pa.Table`, a `pa.RecordBatch`, or anything `pyarrow.table()`
            accepts. For a dry run, an empty table of the schema to check.
        write
            ``replace`` (the default) or ``append``. A first load (``expected_snapshot_id=
            "none"``) creates the table whichever is given.
        expected_snapshot_id
            The table's current snapshot as you read it (`Table.snapshot_id`), or ``"none"``
            for a table that does not exist yet, which the load then creates. Without it, a
            missing table is a `NotFound` that says to pass ``"none"``.
        break_glass_reason
            Write to a protected environment outside a run: 10-500 characters, needs
            `MANAGE_ENVIRONMENT`, audited.
        dry_run
            Run every check, the schema against an existing table included, and write nothing.

        Returns
        -------
        LoadResult
            What was written, and whether the table was created.

        Raises
        ------
        lakana.errors.ValidationError
            For a target outside bronze, or rows the table refuses.
        lakana.errors.NotFound
            If the namespace does not exist (the message names the command that creates it),
            or the table does not and `expected_snapshot_id` is not ``"none"`` (the message
            says to pass it).
        lakana.errors.PermissionDenied
            Without `WRITE_TABLE`, without `CREATE_TABLE` for a first load, or in a protected
            environment without `break_glass_reason` and `MANAGE_ENVIRONMENT`; a dry run is
            refused the same way.
        lakana.errors.Conflict
            If the table's snapshot is no longer `expected_snapshot_id`.
        """
        params: dict[str, object] = {"write": write, "dry_run": "true" if dry_run else "false"}
        if expected_snapshot_id is not None:
            params["expected_snapshot_id"] = str(expected_snapshot_id)
        if break_glass_reason is not None:
            params["break_glass_reason"] = break_glass_reason
        body = self._send_arrow(
            "tables.load", environment, qualified_name, data, params, retry_conflict=False
        ).json()
        return LoadResult(
            snapshot_id=body["snapshot_id"],
            rows=body["rows"],
            created=body["created"],
            dry_run=body["dry_run"],
        )

    def merge(
        self,
        environment: str,
        qualified_name: str,
        data: pa.Table | pa.RecordBatch | object,
        *,
        key: str | Sequence[str],
        sources: Sequence[str] | None = None,
        source_versions: Mapping[str, int] | None = None,
        break_glass_reason: str | None = None,
        expected_snapshot_id: int | str | None = None,
    ) -> MergeResult:
        """Merge rows into a managed table on a key.

        A row whose `key` matches a row of the table replaces it; any other row is inserted;
        nothing is deleted. One snapshot: readers see the table before or after, never a half.
        The rows must not repeat a key and no key may be null; a key that matches two rows of
        the table is refused too. Needs `WRITE_TABLE` and `READ_TABLE`, and is refused while a
        row filter or column mask on the table applies to you. A key of several columns takes
        at most 5,000 rows per call. Serialised, sent and retried exactly like `append`, and a
        timeout is just as ambiguous here (a merge sent twice is harmless: the second changes
        nothing).

        Parameters
        ----------
        environment
            The environment name.
        qualified_name
            Dot-joined `"namespace.table"` name.
        data
            The rows: a `pa.Table`, a `pa.RecordBatch`, or anything `pyarrow.table()`
            accepts.
        key
            The column, or the columns, that identify a row.
        sources
            Dotted names of the tables `data` was derived from, recorded as lineage. Each must
            exist and be readable by the caller.
        source_versions
            The version of each source that is an artifact, by dotted name, when the caller
            read a specific one.
        break_glass_reason
            Write to a protected environment outside a run: 10-500 characters, needs
            `MANAGE_ENVIRONMENT`, audited. The next run of the deployed code may overwrite
            the fix.
        expected_snapshot_id
            The table's current snapshot as you read it (`Table.snapshot_id`), or ``"none"``
            for "no snapshot yet". The merge commits only while that still holds; otherwise
            `Conflict` is raised and nothing is retried. ``"none"`` also creates a missing
            table from `data`.

        Returns
        -------
        MergeResult
            The committed snapshot's id and how many rows were inserted and updated.

        Raises
        ------
        ValueError
            If `key` names no column; nothing is sent.
        lakana.errors.NotFound
            If the table or a source does not exist.
        lakana.errors.PermissionDenied
            If the caller may not write the table or read a source.
        lakana.errors.Conflict
            If a second attempt, after one retry, still conflicts; or at once when
            `expected_snapshot_id` no longer matches.
        lakana.errors.ValidationError
            If a key column is missing, a key is null or repeats (the message gives the count
            of rows, never a value), a key of several columns comes with more than 5,000 rows,
            `data` does not match the table, or a row filter or column mask applies to you.
        """
        columns = [key] if isinstance(key, str) else list(key)
        if not columns:
            raise ValueError('A merge needs at least one key column: pass key="id".')
        params: dict[str, object] = {"key": columns}
        if sources:
            params["source"] = list(sources)
        _add_source_versions(params, source_versions)
        if break_glass_reason is not None:
            params["break_glass_reason"] = break_glass_reason
        if expected_snapshot_id is not None:
            params["expected_snapshot_id"] = str(expected_snapshot_id)
        response = self._send_arrow(
            "tables.merge",
            environment,
            qualified_name,
            data,
            params,
            retry_conflict=expected_snapshot_id is None,
        )
        body = response.json()
        return MergeResult(
            snapshot_id=body["snapshot_id"],
            rows_inserted=body["rows_inserted"],
            rows_updated=body["rows_updated"],
        )

    def lineage(
        self,
        environment: str,
        qualified_name: str,
        *,
        direction: str = "upstream",
        depth: int = 3,
    ) -> Lineage:
        """Walk a table's persisted lineage.

        Nodes the caller can't read are left out and counted in
        `hidden_nodes`; the walk never passes through one.

        Parameters
        ----------
        environment
            The environment name.
        qualified_name
            Dot-joined `"namespace.table"` name.
        direction
            `"upstream"` (what fed it) or `"downstream"` (what it fed).
        depth
            Hops to walk, 1 to 10.

        Returns
        -------
        Lineage
            The graph.

        Raises
        ------
        lakana.errors.NotFound
            If the table does not exist.
        lakana.errors.PermissionDenied
            If the caller can't read the table.
        lakana.errors.ValidationError
            If `direction` or `depth` is out of range.
        """
        namespace, name = _split_namespace_table(qualified_name)
        response = self._transport.request(
            ROUTES["tables.lineage"],
            path_params={"env": environment, "path": namespace, "name": name},
            params={"direction": direction, "depth": depth},
        )
        return _lineage_from_json(response.json())

    def maintain(
        self,
        environment: str,
        qualified_name: str,
        *,
        keep_days: int = 7,
        keep_last: int = 5,
        dry_run: bool = False,
        older_than_hours: int = 72,
    ) -> QueryRun:
        """Queue maintenance for a table: expire old snapshots, delete orphans.

        A worker runs it; poll the returned handle, whose finished status
        carries `maintenance["report"]`. The report's `conflict` is `True`
        when the table changed during maintenance and nothing was applied
        (retry); its `note` says in words why part of the run was skipped;
        its `tags_removed` lists the tags of failed checks (older than seven
        days) that were removed.

        Parameters
        ----------
        environment
            The environment name.
        qualified_name
            Dot-joined `"namespace.table"` name.
        keep_days
            Snapshots younger than this many days are kept.
        keep_last
            The most recent this many snapshots are kept.
        dry_run
            Report what would happen without changing anything.
        older_than_hours
            Unreferenced files younger than this many hours are never deleted
            (default 72, at least 1, at most 87600).

        Returns
        -------
        QueryRun
            A handle to the maintenance run.

        Raises
        ------
        lakana.errors.NotFound
            If the table does not exist.
        lakana.errors.PermissionDenied
            Without MAINTAIN_TABLE on the table or SUBMIT_QUERY on the
            environment.
        lakana.errors.ValidationError
            If `keep_days` is negative, `keep_last` is below 1, or
            `older_than_hours` is outside 1 to 87600.
        """
        namespace, name = _split_namespace_table(qualified_name)
        response = self._transport.request(
            ROUTES["tables.maintain"],
            path_params={"env": environment, "path": namespace, "name": name},
            json={
                "keep_days": keep_days,
                "keep_last": keep_last,
                "dry_run": dry_run,
                "older_than_hours": older_than_hours,
            },
        )
        return QueryRun(self._transport, id=response.json()["query_id"], environment=environment)

    def _send_arrow(
        self,
        route: str,
        environment: str,
        qualified_name: str,
        data: pa.Table | pa.RecordBatch | object,
        params: dict[str, object],
        retry_conflict: bool = True,
    ) -> httpx.Response:
        """Serialise `data` as an Arrow IPC stream and send it, retrying once on a conflict.

        Parameters
        ----------
        route
            `"tables.append"`, `"tables.replace"`, `"tables.merge"` or `"tables.load"`.
        environment
            The environment name.
        qualified_name
            Dot-joined `"namespace.table"` name.
        data
            The rows.
        params
            Query parameters.
        retry_conflict
            Retry once on a `Conflict`; off when the caller gave a
            precondition, because the conflict is then the answer.

        Returns
        -------
        httpx.Response
            The successful response.

        Raises
        ------
        Conflict
            If the write conflicts twice, or once when `retry_conflict` is off.
        """
        namespace, name = _split_namespace_table(qualified_name)
        table = normalize_view_columns(
            cast_timestamp_ns_columns_to_us(_coerce_to_table(data))
        ).combine_chunks()
        body = _serialize_ipc_stream(table)
        path_params = {"env": environment, "path": namespace, "name": name}
        headers = {"Content-Type": _APPEND_CONTENT_TYPE}
        try:
            return self._transport.request(
                ROUTES[route],
                path_params=path_params,
                params=params,
                content=body,
                headers=headers,
                timeout=_APPEND_TIMEOUT_SECONDS,
            )
        except Conflict:
            if not retry_conflict:
                raise
            return self._transport.request(
                ROUTES[route],
                path_params=path_params,
                params=params,
                content=body,
                headers=headers,
                timeout=_APPEND_TIMEOUT_SECONDS,
            )
