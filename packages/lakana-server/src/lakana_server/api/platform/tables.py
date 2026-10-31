"""The tables resource of the platform API — list, read, drop, create and append.

Committing new Iceberg *metadata updates* to an existing table — schema
evolution, partition changes, and the like — is still Iceberg-router-only
(contract: exactly one write path for that). Governed table *creation* from
a caller-declared JSON schema and governed *appends* of Arrow data are a
second, deliberately narrower write path: the SDK
needs "create a table -> query it" to work without a client embedding
pyiceberg or holding its own storage credentials, so `create_table` and
`append_table_data` below call straight into `CatalogService.create_table`/
`CatalogService.append`, the same core operations the Iceberg router itself
uses, under the same medallion, location and authorization rules. Dropping
a table writes no metadata — it is a catalog-lifecycle act, the same kind
this API already exposes for environments and namespaces — so it lives
here too, as the program-design contract's endpoint table has always
specified.
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, TypeVar

import pyarrow as pa
import pyarrow.ipc
from fastapi import APIRouter, Depends, Query, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session
from starlette.requests import ClientDisconnect

from lakana_core.auth import PrincipalRef
from lakana_core.catalog.descriptions import KEEP, _Keep
from lakana_core.catalog.json_schema import ColumnSpec, from_iceberg_schema, to_iceberg_schema
from lakana_core.catalog.lineage_graph import LineageDirection, LineageService
from lakana_core.catalog.loads import require_bronze
from lakana_core.catalog.query_service import QueryService
from lakana_core.catalog.retention import TableRetention
from lakana_core.catalog.service import CatalogService, LoadedTable, TableDescription
from lakana_core.catalog.writes import (
    ExpectedSnapshot,
    check_sources,
    parse_expected_snapshot,
    parse_source_versions,
)
from lakana_core.errors import LakanaError, ValidationError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.projects.asset_model import require_model_kind
from lakana_server.api.platform.languages import LANG_DESCRIPTION, reader_languages
from lakana_server.api.platform.schemas import (
    AppendOut,
    ColumnOut,
    DescriptionIn,
    DroppedTableOut,
    ExternalTableCreate,
    ExternalTableOut,
    LineageEdgeOut,
    LineageNodeOut,
    LineageOut,
    LoadOut,
    MaintenanceIn,
    MergeOut,
    QuerySubmitOut,
    ReplaceOut,
    TableCreate,
    TableHealthOut,
    TableModelOut,
    TableOut,
    TableReferenceOut,
    TableReferencesOut,
    TableRetentionIn,
    TableRetentionOut,
    TranslationCoverageOut,
    UndropIn,
)
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.body_limit import streams_its_own_body
from lakana_server.concurrency import AppendConcurrencyLimiter
from lakana_server.deps import (
    get_catalog,
    get_lineage_service,
    get_main_language,
    get_principal,
    get_query_service,
    get_session,
    get_write_principal,
    with_break_glass,
)
from lakana_server.errors import (
    AppendConcurrencyLimitError,
    AppendReadTimeoutError,
    ClientDisconnectedError,
    PayloadTooLargeError,
)

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/environments/{env}/namespaces/{path}/tables",
    tags=["tables"],
    responses=errors(401),
)
dropped_router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/environments/{env}/namespaces/{path}/dropped-tables",
    tags=["tables"],
    responses=errors(401),
)
"""The dropped tables of a namespace: a path of its own, so no table name can shadow it."""
expiring_router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/environments/{env}/expiring-tables",
    tags=["tables"],
    responses=errors(401),
)
"""The managed tables of an environment that their retention makes expire soon."""

_ARROW_STREAM_MEDIA_TYPE = "application/vnd.apache.arrow.stream"

_MESSAGE_HEADER_FIELD = 2
"""`Message.header`'s field index in Arrow's FlatBuffers `Message` table.

Field 0 is `version`; field 1 is `header_type`, the union discriminant
`flatc` inserts automatically ahead of a `header: MessageHeader` union
field; field 2 is `header` itself, the union's table-offset value."""

_DICTIONARY_BATCH_DATA_FIELD = 1
"""`DictionaryBatch.data`'s field index (field 0 is `id`)."""

_RECORD_BATCH_COMPRESSION_FIELD = 3
"""`RecordBatch.compression`'s field index (0 `length`, 1 `nodes`, 2 `buffers`)."""


def _vtable_field_offset(buf: bytes, table_pos: int, field_index: int) -> int | None:
    """Return a FlatBuffers table field's absolute byte position, or `None` if absent.

    Parameters
    ----------
    buf
        The FlatBuffers-encoded bytes the table lives in.
    table_pos
        The table's own absolute byte position within `buf`.
    field_index
        The field's zero-based index in its table's declared field order
        (`flatc`'s vtable slot order, not a byte offset).

    Returns
    -------
    int or None
        The absolute position of the field's stored value, or `None` if
        the field is absent from this table's vtable (never written, or
        left at its default).
    """
    vtable_soffset = struct.unpack_from("<i", buf, table_pos)[0]
    vtable_pos = table_pos - vtable_soffset
    vtable_size = struct.unpack_from("<H", buf, vtable_pos)[0]
    voffset_index = 4 + field_index * 2
    if voffset_index + 2 > vtable_size:
        return None
    field_voffset = struct.unpack_from("<H", buf, vtable_pos + voffset_index)[0]
    return None if field_voffset == 0 else table_pos + field_voffset


def _dereference_table_offset(buf: bytes, field_pos: int) -> int:
    """Resolve a FlatBuffers table-offset field to the table it points at.

    Parameters
    ----------
    buf
        The FlatBuffers-encoded bytes the field lives in.
    field_pos
        The absolute position of a field whose stored value is a
        `uoffset_t` (a table/vector/string offset), such as one
        `_vtable_field_offset` returned.

    Returns
    -------
    int
        The absolute position of the table the field points at.
    """
    return field_pos + struct.unpack_from("<I", buf, field_pos)[0]


def _message_declares_compression(message: pyarrow.ipc.Message) -> bool:
    """Check whether an IPC record-batch or dictionary-batch message is compressed.

    Reads exactly one optional field — `RecordBatch.compression` — out of
    the message's FlatBuffers-encoded metadata, without touching its body
    (the actual, possibly-compressed array bytes) at all. This is how a
    compression bomb is refused before any large allocation is attempted:
    `pyarrow.ipc.RecordBatchStreamReader` allocates each buffer at its
    *declared uncompressed* length before decompressing into it, so a tiny
    compressed body can otherwise force an allocation orders of magnitude
    larger than itself — reproduced at 2,288 wire bytes decoding to 64 MB,
    a ~28,000:1 ratio, with `zstd`. There is
    no public pyarrow API to ask "is this message compressed" without
    decoding it, so this parses just enough of the FlatBuffers table/vtable
    binary layout by hand — a stable, versioned wire format Arrow itself
    specifies exactly, not a pyarrow implementation detail that could shift
    between releases — to answer that one boolean question.

    Parameters
    ----------
    message
        One message read from `pyarrow.ipc.MessageReader.open_stream`,
        already known (via `message.type`) to be a `"record batch"` or
        `"dictionary"` (dictionary-batch) message; any other message type
        returns `False` immediately, since only these two ever carry a
        `RecordBatch` header.

    Returns
    -------
    bool
        `True` if the message's (possibly nested, for a dictionary batch)
        `RecordBatch` header declares a `compression` codec for any of its
        buffers — `False` if every buffer in the message body is stored
        uncompressed.

    Raises
    ------
    ArrowInvalid
        If the message's metadata is too short or structurally malformed
        to contain the FlatBuffers layout this reads — treated the same as
        any other decode failure by `_decode_arrow_stream`, this
        function's only caller.
    """
    buf = message.metadata.to_pybytes()
    try:
        root = struct.unpack_from("<I", buf, 0)[0]
        header_field = _vtable_field_offset(buf, root, _MESSAGE_HEADER_FIELD)
        if header_field is None:
            return False
        header_table = _dereference_table_offset(buf, header_field)
        if message.type == "dictionary":
            data_field = _vtable_field_offset(buf, header_table, _DICTIONARY_BATCH_DATA_FIELD)
            if data_field is None:
                return False
            record_batch_table = _dereference_table_offset(buf, data_field)
        elif message.type == "record batch":
            record_batch_table = header_table
        else:
            return False
    except struct.error as exc:
        raise pa.ArrowInvalid("Malformed IPC message metadata.") from exc
    return (
        _vtable_field_offset(buf, record_batch_table, _RECORD_BATCH_COMPRESSION_FIELD) is not None
    )


def _media_type(content_type: str | None) -> str:
    """Extract a `Content-Type` header's bare media type, case-insensitively.

    Parameters
    ----------
    content_type
        The raw header value, such as
        `"application/vnd.apache.arrow.stream; charset=utf-8"`, or `None`
        if the header was not sent at all.

    Returns
    -------
    str
        The media type alone: lowercased, stripped of surrounding
        whitespace, with any `;`-separated parameters discarded. `""` if
        `content_type` is `None`.
    """
    if content_type is None:
        return ""
    return content_type.split(";", 1)[0].strip().lower()


_ALLOWED_SCALAR_ARROW_TYPES = frozenset(
    {
        pa.int32(),
        pa.int64(),
        pa.float32(),
        pa.float64(),
        pa.bool_(),
        pa.string(),
        pa.large_string(),
        pa.binary(),
        pa.large_binary(),
        pa.date32(),
        pa.uuid(),
    }
)
"""Every allowed Arrow type that isn't parametric (decimal128/time64/timestamp).

**Keep this in sync with `lakana_core.catalog.json_schema`/`lakana_core.catalog.append`.**
This is a plain-pyarrow mirror of the Arrow types `lakana_core.catalog.append.
_types_compatible` accepts against an Iceberg-derived schema (itself built from
`lakana_core.catalog.json_schema._PRIMITIVE_TYPES` via pyiceberg's
`schema_to_pyarrow`) — `lakana-server` cannot import pyiceberg
(`server-goes-through-core`, `.importlinter`), so this list is duplicated by
hand rather than imported. `string`/`binary` are listed alongside
`large_string`/`large_binary` because ordinary caller data (including
pyarrow's own type inference from a plain Python list of `str`) produces the
non-`large` variant, which `_types_compatible` treats as equivalent.
`pa.uuid()` (`extension<arrow.uuid>`, storage `fixed_size_binary(16)`) is
listed explicitly rather than falling under a generic "any extension type"
rule, since it's the one extension type `to_iceberg_schema` can ever produce
(`UUIDType()` -> pyiceberg's `schema_to_pyarrow` -> `pa.uuid()`) — it was
once missing, which refused a caller's `uuid` column outright. If a new Iceberg primitive type is
ever added to
`json_schema._PRIMITIVE_TYPES`, add its pyarrow equivalent here too, or a
caller's data in that type will be refused here before `CatalogService.append`
ever gets a chance to accept it.
"""


def _is_allowed_arrow_type(arrow_type: pa.DataType) -> bool:
    """Check an Arrow type against the allowlist an appended column may use.

    Run once per field, on the stream's schema, before any batch is
    decoded — never on already-decoded data. This closes two problems a
    later, per-value check cannot: an unbounded type like `dictionary`,
    `list`, or `string_view`/`binary_view` can carry a validation cost (or,
    a crash risk) wildly out of proportion
    to its wire size — a `string_view` array can hold many short views into
    one shared buffer, so `Table.validate(full=True)`'s UTF-8 check ends up
    re-validating the same bytes once per view, with no bound tied to the
    wire or decoded-size caps (reproduced: a request within the byte cap
    describing views whose combined *logical* length reaches into the
    gibibytes). None of these types is one `to_iceberg_schema` can ever
    produce from a caller's JSON column list in the first place, so
    refusing them here costs a legitimate caller nothing.

    Parameters
    ----------
    arrow_type
        The Arrow type to check.

    Returns
    -------
    bool
        `True` if `arrow_type` is one `CatalogService.append`'s own schema
        check could ever accept.
    """
    if pa.types.is_decimal128(arrow_type):
        return True
    if pa.types.is_time64(arrow_type):
        return arrow_type.unit == "us"
    if pa.types.is_timestamp(arrow_type):
        return arrow_type.unit == "us"
    return arrow_type in _ALLOWED_SCALAR_ARROW_TYPES


def _validate_batch_before_use(batch: pa.RecordBatch) -> int:
    """Fully validate a decoded batch before reading anything else about it.

    This ordering — `validate` strictly before `nbytes`, or any other
    attribute of an unvalidated batch — is the actual fix: calling `.nbytes` on a batch whose
    internal
    lengths are corrupted but not yet validated was reproduced segfaulting
    the process outright, a crash no Python `except` clause can catch (see
    `_decode_arrow_stream`'s docstring for the full reproduction). Factored
    out to its own function so this ordering can be tested directly
    against a plain recording stand-in, independent of whatever type
    allowlist or other check might otherwise short-circuit before this
    code is ever reached for a given input — `pyarrow.RecordBatch` is an
    immutable extension type that cannot itself be monkeypatched to record
    call order, so a real batch cannot be used for that test either.

    Parameters
    ----------
    batch
        The freshly decoded batch to validate.

    Returns
    -------
    int
        The batch's `nbytes`, read only once `validate` has succeeded.
    """
    batch.validate(full=True)
    return batch.nbytes


_MAX_SCHEMA_COLUMNS = 1000
"""Most columns a decoded stream's schema may declare.

Matches `TableCreate.columns`'s own 1000-column bound: a wide schema costs proportionally more to
validate and hold
per batch, one more dimension a byte cap alone does not bound (see
`_decode_arrow_stream`'s docstring)."""


def _decode_arrow_stream(payload: pa.Buffer, max_bytes: int, max_arrays: int) -> pa.Table:
    """Decode a caller-submitted Arrow IPC stream, refusing what a byte cap alone cannot.

    Checks beyond what a plain `pyarrow.ipc.open_stream(...).read_all()`
    provides, run in this order:

    1. Once the stream's schema is available (opening it reads only the
       schema message, no batch), the schema is refused outright if it
       declares zero fields (a
       zero-column batch contributes ~0 to the byte cap below and exactly
       0 to the array cap below, so a stream of arbitrarily many
       zero-column batches defeats both caps; reproduced at 2.5M empty
       batches, +723 MB RSS) or more than `_MAX_SCHEMA_COLUMNS` fields, and
       otherwise every field's type is checked against
       `_is_allowed_arrow_type` — *before* any batch is decoded or
       validated, and before step 2 walks the stream, so a refused
       schema costs no per-message CPU. The type check is a distinct,
       cheaper check from the schema comparison `CatalogService.append`
       performs later against the table's actual Iceberg schema: it only
       rules out types that are never valid here at all (`dictionary`,
       `list`, `string_view`, ...), independent of which table this data is
       destined for, closing off a class of type whose validation or memory
       cost cannot be bounded by a byte cap (see
       `_is_allowed_arrow_type`'s docstring).
    2. Every message is scanned for a declared compression codec
       (`_message_declares_compression`) *before* any batch is decoded.
       Compression is refused outright, never decoded — there is no safe
       "decode it and check the size afterwards" here, since the outsized
       allocation a compression bomb forces already happened by then (see
       `_message_declares_compression`'s docstring).
    3. Batches are decoded one at a time — not `read_all()`'s single bulk
       decode — and **each batch is fully validated
       (`_validate_batch_before_use`, `batch.validate(full=True)`) before
       anything else touches it, including reading its own `.nbytes`.**
       This ordering is load-bearing, not a style choice: `batch.nbytes`
       on a batch whose declared-but-not-yet-validated internal lengths
       are corrupted has been reproduced *segfaulting the process* — a crash no `except` clause
       here or
       anywhere else in Python can catch, since the process is gone.
       Validating first means a corrupt batch is refused with this
       function's own `ValidationError` before `nbytes` (or anything
       else) ever reads its buffers. Only once a batch passes does its
       `nbytes` count toward `max_bytes`, refused as soon as the running
       total exceeds it — a second bound on decoded size, independent of
       the request's own streaming byte cap on wire size. Its
       `num_columns` also counts toward `max_arrays`, refused the same
       way: a batch's own object overhead (in decode, and later in
       pyiceberg) is per-array, not per-byte, so many tiny batches inside
       an otherwise byte-cap-respecting body can still amplify memory and
       CPU cost far beyond what the byte cap alone implies (measured: 1.7M one-row batches inside a
       256 MiB body
       cost +1.46 GB RSS and 7s to decode, then +3.3 GB and 32s in the
       pyiceberg append). Both cumulative checks stop as soon as either is
       exceeded, without decoding the rest of the stream.
    4. `data.validate(full=True)` runs once more on the assembled table,
       as a final check (each batch was already individually validated in
       step 3, so this mainly guards `Table.from_batches` itself), and the
       result is passed through `combine_chunks()` before being returned —
       collapsing it to one chunk per column bounds the same per-array
       overhead point 3 caps during decode, for whatever reads the
       returned table next (`CatalogService.append`, and pyiceberg after
       it).

    Every one of these steps runs synchronously and can be CPU-bound
    (`Table.validate(full=True)` scales with a batch's *logical* content,
    not its wire size); the caller (`append_table_data`) runs this whole
    function via `run_in_threadpool`, never inline on the event loop.

    Parameters
    ----------
    payload
        The complete, already-capped request body — bytes, or (as the
        caller uses, for a zero-copy view over the accumulated
        `bytearray`) anything `pyarrow.BufferReader` accepts.
    max_bytes
        The configured cap (`Settings.append.max_bytes`) to check the
        cumulative decoded size against.
    max_arrays
        The configured cap (`Settings.append.max_arrays`) to check the
        cumulative batches-times-columns count against.

    Returns
    -------
    pa.Table
        The decoded, fully-validated, single-chunk-per-column table.

    Raises
    ------
    lakana_core.errors.ValidationError
        If any message declares body compression; if the schema declares
        zero fields or more than `_MAX_SCHEMA_COLUMNS` fields; if any
        field's type is not in `_is_allowed_arrow_type`'s allowlist; if the
        stream is not a valid Arrow IPC stream at all; or if the decoded
        data fails full validation.
    lakana_server.errors.PayloadTooLargeError
        If the cumulative decoded size of the batches read so far exceeds
        `max_bytes`, or their cumulative batches-times-columns count
        exceeds `max_arrays`.
    """
    try:
        reader = pyarrow.ipc.open_stream(pa.BufferReader(payload))
        if len(reader.schema) == 0:
            # Both caps below are computed per-batch from the batch's own bytes and
            # `num_columns`: a zero-column schema means every batch contributes ~0 to
            # `total_bytes` and exactly 0 to `total_arrays`, so neither cap ever fires no
            # matter how many batches the stream declares (reproduced: 2.5M empty batches decoded,
            # +723 MB RSS,
            # before either cap would have stopped it). A zero-column append could never carry a
            # row's worth
            # of data anyway, so refusing it outright costs a legitimate caller nothing.
            raise ValidationError("The stream has no columns.")
        if len(reader.schema) > _MAX_SCHEMA_COLUMNS:
            raise ValidationError(
                f"Schema has {len(reader.schema)} columns; the maximum is {_MAX_SCHEMA_COLUMNS}."
            )
        for field in reader.schema:
            if not _is_allowed_arrow_type(field.type):
                raise ValidationError(
                    f"Column {field.name!r} has an unsupported Arrow type: {field.type}."
                )

        for message in pyarrow.ipc.MessageReader.open_stream(pa.BufferReader(payload)):
            if _message_declares_compression(message):
                raise ValidationError("Compressed Arrow IPC is not supported; send uncompressed.")

        batches: list[pa.RecordBatch] = []
        total_bytes = 0
        total_arrays = 0
        for batch in reader:
            total_bytes += _validate_batch_before_use(batch)
            if total_bytes > max_bytes:
                raise PayloadTooLargeError(
                    f"Decoded append data exceeds the configured {max_bytes}-byte cap."
                )
            total_arrays += batch.num_columns
            if total_arrays > max_arrays:
                raise PayloadTooLargeError(
                    f"Decoded append data exceeds the configured {max_arrays}-array "
                    "(batches x columns) cap; send fewer, larger batches."
                )
            batches.append(batch)
        data = pa.Table.from_batches(batches, schema=reader.schema)
        data.validate(full=True)
        data = data.combine_chunks()
    except (pa.ArrowException, ValueError, OSError) as exc:
        # `pa.ArrowException` covers every pyarrow-specific decode failure
        # (ArrowInvalid, ArrowNotImplementedError, ArrowMemoryError,
        # ArrowTypeError, ...). `ValueError` is caught in addition, not
        # instead: `Table.validate(full=True)` on data with a corrupted
        # but structurally-plausible string array has been observed to
        # raise a bare `UnicodeDecodeError` (a `ValueError` subclass, but
        # not a `pyarrow.lib.ArrowException`) while checking UTF-8
        # validity, which would otherwise leak past this handler as an
        # unhandled 500.
        raise ValidationError("Body is not a valid Arrow IPC stream.") from exc
    return data


def _to_out(namespace: str, loaded: LoadedTable) -> TableOut:
    """Build a response body from a loaded table.

    Parameters
    ----------
    namespace
        Dot-joined namespace path the table lives in.
    loaded
        The table, as `CatalogService.load_table` returns it.

    Returns
    -------
    TableOut
        The read-only response body.
    """
    return TableOut(
        name=loaded.ref.name,
        namespace=namespace,
        uuid=str(loaded.uuid),
        kind="TABLE",
        metadata_location=loaded.metadata_location,
        columns=[
            ColumnOut(name=c.name, type=c.type, nullable=c.nullable)
            for c in from_iceberg_schema(loaded.metadata.schema())
        ],
        snapshot_id=loaded.metadata.current_snapshot_id,
    )


def _column_out(column: ColumnSpec, description: TableDescription) -> ColumnOut:
    """Build one column of a response body, with its label and description.

    Parameters
    ----------
    column
        The column.
    description
        From `CatalogService.describe_table`, which holds the columns' chosen texts.

    Returns
    -------
    ColumnOut
        The column.
    """
    text = description.column_descriptions.get(column.name)
    label = description.column_labels.get(column.name)
    return ColumnOut(
        name=column.name,
        type=column.type,
        nullable=column.nullable,
        description=None if text is None else text.text,
        description_source=None if text is None else text.source,
        description_language=None if text is None else text.language,
        label=None if label is None else label.text,
        label_source=None if label is None else label.source,
        label_language=None if label is None else label.language,
    )


def _describe_out(namespace: str, description: TableDescription) -> TableOut:
    """Build a response body from any kind of table.

    Parameters
    ----------
    namespace
        Dot-joined namespace path the table lives in.
    description
        From `CatalogService.describe_table`.

    Returns
    -------
    TableOut
        The read-only response body.
    """
    return TableOut(
        name=description.ref.name,
        namespace=namespace,
        uuid=str(description.id),
        kind=description.kind,  # ty: ignore[invalid-argument-type]
        metadata_location=description.metadata_location,
        columns=[_column_out(c, description) for c in description.columns],
        sql=description.sql,
        references=None
        if description.kind != "VIEW"
        else [TableReferenceOut(id=i, name=n) for i, n in description.references],
        hidden_references=description.hidden_references,
        snapshot_id=description.snapshot_id,
        state=description.state,
        state_message=description.state_message,
        description=description.description,
        description_source=description.description_source,
        description_language=description.description_language,
        label=description.label,
        label_source=description.label_source,
        label_language=description.label_language,
        language=description.language,
        main_language=description.main_language,
        translations=[
            TranslationCoverageOut.model_validate(c, from_attributes=True)
            for c in description.translations
        ],
        last_write_at=description.last_write_at,
        row_count=description.row_count,
        size_bytes=description.size_bytes,
        file_count=description.file_count,
        expires_at=description.expires_at,
        expires_soon=description.expires_soon,
        model=None
        if description.model is None
        else TableModelOut.model_validate(description.model, from_attributes=True),
    )


def _retention_out(retention: TableRetention) -> TableRetentionOut:
    """Build the response body of a table's retention.

    Parameters
    ----------
    retention
        From `CatalogService.table_retention` and its kin.

    Returns
    -------
    TableRetentionOut
        The body.
    """
    return TableRetentionOut(
        name=retention.ref.name,
        namespace=".".join(retention.ref.namespace.parts),
        uuid=str(retention.table_id),
        source=retention.source,
        days=retention.days,
        on=retention.on,
        never=retention.never,
        set_by=retention.set_by,
        set_at=retention.set_at,
        last_activity_at=retention.last_activity_at,
        expires_at=retention.expires_at,
        kept=retention.kept,
    )


def _expected_snapshot(request: Request, value: str | None) -> ExpectedSnapshot | None:
    """Parse `expected_snapshot_id`, refusing a repeated parameter.

    Parameters
    ----------
    request
        The current request.
    value
        The parameter as FastAPI bound it (the last value if repeated).

    Returns
    -------
    int or "none" or None
        As `parse_expected_snapshot`.

    Raises
    ------
    ValidationError
        If the parameter is repeated or malformed.
    """
    if len(request.query_params.getlist("expected_snapshot_id")) > 1:
        raise ValidationError("expected_snapshot_id must be sent at most once.")
    return parse_expected_snapshot(value)


def _arrow_write_target(
    request: Request,
    env: str,
    path: str,
    name: str,
    source: list[str] | None,
    source_version: list[str] | None,
    expected_snapshot_id: str | None,
) -> tuple[ExpectedSnapshot | None, dict[str, int], TableRef]:
    """Check the query parameters every Arrow write takes, before the body is read.

    Parameters
    ----------
    request
        The current request.
    env
        Environment name.
    path
        Dot-joined namespace path.
    name
        Table name.
    source
        The `source` parameters.
    source_version
        The `source_version` parameters.
    expected_snapshot_id
        The `expected_snapshot_id` parameter.

    Returns
    -------
    tuple
        The precondition, the source versions by name, and the table.

    Raises
    ------
    ValidationError
        If a parameter is malformed or repeated where it may not be.
    """
    expected = _expected_snapshot(request, expected_snapshot_id)
    check_sources(source or [])
    versions = parse_source_versions(source_version or [])
    ref = TableRef(environment=env, namespace=NamespacePath.from_parts(path.split(".")), name=name)
    return expected, versions, ref


_T = TypeVar("_T")


async def _governed_arrow_write(
    request: Request,
    principal: PrincipalRef,
    catalog: CatalogService,
    session: Session,
    ref: TableRef,
    write: Callable[[pa.Table], _T],
    precheck: Callable[[], None],
) -> _T:
    """Read, bound and decode an Arrow IPC body, then hand it to a catalog write.

    Shared by `append_table_data`, `replace_table_data` and `merge_table_data` so every body
    control applies to all three.

    `precheck` (`check_can_append`, `check_can_replace` or `check_can_merge`) runs
    first — before the body is read at all — so an unauthorized or
    nonexistent-table request is refused
    without spending any time or memory on a body that was never going to
    be written anyway (this used to run only
    after the whole body was read and decoded). Immediately after that
    pre-check, `session.rollback()` returns its pooled database connection
    to the pool: `session` is the exact same
    instance `catalog` was built from (FastAPI caches a `Depends` result
    per request, so two dependants of `get_session` share one instance),
    and without this the connection stayed checked out for the rest of
    this coroutine — including the client-paced body read below, which can
    run for as long as `read_timeout_seconds`. The reviewer reproduced 16
    stalled uploads exhausting the default connection pool this way and
    freezing an unrelated request for the full pool-checkout timeout;
    `append` re-authorizes and re-resolves the table itself immediately
    before the actual write regardless, so releasing the connection here
    costs nothing.

    Before any of that, `app.state.append_concurrency_limiter`
    (`lakana_server.concurrency.AppendConcurrencyLimiter`,
    `Settings.append.max_concurrent`) must grant this request a slot, or it
    is refused outright with `AppendConcurrencyLimitError` (503,
    `Retry-After: 1`) — a non-blocking check, never a queue, so a request
    past the bound fails fast instead of waiting behind other uploads (unboundedly many concurrent
    appends can each add
    their own decode/validate memory and CPU cost on top of the pool
    exhaustion above). The slot is released in a `finally`, however this
    function exits.

    The body itself is then read incrementally (`request.stream()`),
    accumulating into a running total that is checked after every chunk —
    so a body without a `Content-Length` header (a chunked request, or one
    whose length the client never declared) is bounded exactly the same as
    one with it, and reading stops the moment the configured cap
    (`Settings.append.max_bytes`, `LAKANA_APPEND__MAX_BYTES`) is crossed,
    before the rest of the body is ever read. The whole read is wrapped in
    `asyncio.timeout(read_timeout_seconds)`: a
    client that stops sending — deliberately or not — is refused with
    `AppendReadTimeoutError` (408) rather than holding its slot and
    connection indefinitely. A client that instead closes its connection
    outright mid-upload raises Starlette's own `ClientDisconnect`, caught
    here and re-raised as `ClientDisconnectedError`: the client is already gone, so no one reads
    the 400 this maps
    to, but raising a *handled* `LakanaError` rather than letting
    `ClientDisconnect` propagate unhandled is what keeps Starlette from
    logging it at `ERROR` with a full traceback ("Exception in ASGI
    application") — the concurrency slot is still released by the
    surrounding `finally` either way. Decoding (`_decode_arrow_stream`) applies
    further, independent bounds a wire-byte cap alone cannot: it refuses a
    schema declaring more than 1000 columns, the same bound `TableCreate`
    enforces at table-creation time; it refuses any compressed message
    outright (a compression bomb inflates during decode, not on the wire);
    refuses any field whose Arrow type is outside a fixed allowlist before
    any batch is decoded (closing off types whose validation cost or
    memory layout isn't bounded by a byte cap, such as `string_view`);
    fully validates each decoded batch before reading anything else about
    it, including its own `.nbytes` (an unvalidated batch's `.nbytes` has
    been reproduced segfaulting the process — see `_decode_arrow_stream`'s
    docstring); caps cumulative *decoded* size the same
    way the wire read above caps wire size; and caps the cumulative
    batches-times-columns count (`Settings.append.max_arrays`,
    `LAKANA_APPEND__MAX_ARRAYS`) — many tiny batches can amplify memory and
    CPU cost per array far beyond what the byte cap alone implies. The synchronous
    `Depends`-provided `principal`/`catalog`/
    `session` above are safe to use from this `async def` route the same
    way FastAPI already resolves them for a sync one; both catalog calls
    (`check_can_append`, then `write`, which perform blocking I/O), the
    rollback, and the decode call itself (CPU-bound, not I/O-bound, but
    just as capable of blocking the event loop for a noticeable time — full
    validation scales with a batch's logical content) are pushed onto
    FastAPI's threadpool via `run_in_threadpool`, so none of them blocks
    the event loop the streaming read runs on.

    Parameters
    ----------
    request
        The current request.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.
    session
        The request's session, released after the pre-check.
    ref
        The table.
    write
        The catalog write to run, in the threadpool, on the decoded table.
    precheck
        The catalog's cheap authorization and existence check, run in the
        threadpool before the body is read.

    Returns
    -------
    object
        Whatever `write` returns.

    Raises
    ------
    lakana_core.errors.ValidationError
        As `append_table_data` documents.
    lakana_server.errors.PayloadTooLargeError
        As `append_table_data` documents.
    lakana_server.errors.AppendConcurrencyLimitError
        As `append_table_data` documents.
    lakana_server.errors.AppendReadTimeoutError
        As `append_table_data` documents.
    lakana_server.errors.ClientDisconnectedError
        As `append_table_data` documents.
    """
    if _media_type(request.headers.get("content-type")) != _ARROW_STREAM_MEDIA_TYPE:
        raise ValidationError(f"Content-Type must be {_ARROW_STREAM_MEDIA_TYPE!r}.")

    limiter: AppendConcurrencyLimiter = request.app.state.append_concurrency_limiter
    if not limiter.try_acquire():
        raise AppendConcurrencyLimitError("Too many concurrent appends; retry shortly.")
    try:
        await run_in_threadpool(precheck)
        await run_in_threadpool(session.rollback)

        append_config = request.app.state.settings.append
        max_bytes = append_config.max_bytes
        buffer = bytearray()
        try:
            async with asyncio.timeout(append_config.read_timeout_seconds):
                async for chunk in request.stream():
                    buffer.extend(chunk)
                    if len(buffer) > max_bytes:
                        raise PayloadTooLargeError(
                            f"Append body exceeds the configured {max_bytes}-byte cap."
                        )
        except TimeoutError as exc:
            raise AppendReadTimeoutError(
                f"Append body did not finish arriving within {append_config.read_timeout_seconds}s."
            ) from exc
        except ClientDisconnect as exc:
            # The client is already gone -- no one will ever read this response. Raising a
            # handled `LakanaError` (rather than letting `ClientDisconnect` propagate) is the
            # actual fix: an *unhandled* exception is what
            # Starlette logs at ERROR with a full traceback ("Exception in ASGI
            # application"); routing it through the same handled-error path every other
            # append failure uses is silent, the same way a normal 4xx response is.
            raise ClientDisconnectedError(
                "The client disconnected before the append body finished arriving."
            ) from exc

        data = await run_in_threadpool(
            _decode_arrow_stream, pa.py_buffer(buffer), max_bytes, append_config.max_arrays
        )
        return await run_in_threadpool(write, data)
    finally:
        limiter.release()


@router.post("", response_model=TableOut, status_code=201, responses=errors(403, 404, 409))
def create_table(
    env: str,
    path: str,
    body: TableCreate,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> TableOut:
    """Create a table.

    Creates a managed Iceberg table from the columns in the request. Needs `CREATE_TABLE` on
    the namespace. Answers 404 when the environment or namespace does not exist, 409 when a
    table of that name exists and 422 for an unknown column type, an empty or duplicate
    column name, or a namespace not rooted at `bronze`, `silver` or `gold`.

    Parameters
    ----------
    env
        Environment name.
    path
        Dot-joined namespace path.
    body
        The table's name and columns.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Returns
    -------
    TableOut
        The created table.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    lakana_core.errors.AlreadyExistsError
        Mapped to 409, if a table of that name already exists.
    lakana_core.errors.ValidationError
        Mapped to 422: an unrecognised column type, an empty or duplicate
        column name, or a namespace not rooted at `bronze`, `silver`, or
        `gold`.
    lakana_core.errors.PermissionDenied
        Mapped to 403 by `install_exception_handlers`.
    """
    ns = NamespacePath.from_parts(path.split("."))
    ref = TableRef(environment=env, namespace=ns, name=body.name)
    columns = [ColumnSpec(name=c.name, type=c.type, nullable=c.nullable) for c in body.columns]
    loaded = catalog.create_table(
        with_break_glass(principal, body.break_glass_reason), ref, schema=to_iceberg_schema(columns)
    )
    return _to_out(path, loaded)


@router.get("", response_model=list[TableOut], responses=errors(403, 404, 503))
def list_tables(
    env: str,
    path: str,
    request: Request,
    model_kind: str | None = Query(
        default=None,
        description="Only tables whose asset header declares this kind: fact, dimension, wide "
        "or staging.",
    ),
    lang: str | None = Query(default=None, description=LANG_DESCRIPTION),
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
    main_language: str = Depends(get_main_language),
) -> list[TableOut]:
    """List the tables of a namespace.

    Lists the tables of the namespace that the caller may read, with their texts in the
    reader's language. Needs `READ_TABLE` on the namespace; tables the caller may not read are
    left out. Answers 404 when the namespace does not exist, 422 for a `model_kind` no header
    can declare or a `lang` that is not a language tag, and 503 when the environment's storage
    cannot be reached (try again after `Retry-After`).

    Parameters
    ----------
    env
        Environment name.
    path
        Dot-joined namespace path.
    request
        The current request, for `Accept-Language`.
    model_kind
        Keep only the tables whose header declares this kind (`model.kind`); every table when
        absent.
    lang
        The language of the texts; see `reader_languages` for the order without it.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.
    main_language
        `LAKANA_MAIN_LANGUAGE`, the last language a reader without a choice gets.

    Returns
    -------
    list of TableOut
        Every table, external table, view and artifact in the namespace that
        the caller may read, each described individually. One the
        caller may not read is left out, as if it did not exist, so a token
        that declares one asset gets a listing of that one instead of a
        refusal that names another.
    """
    if model_kind is not None:
        require_model_kind(model_kind)
    languages = reader_languages(request, lang, principal, main_language)
    ns = NamespacePath.from_parts(path.split("."))
    refs = catalog.list_tables(principal, env, ns)
    described = [
        catalog.describe_table(principal, ref, languages=languages)
        for ref in refs
        if catalog.can_read_table(principal, ref)
    ]
    return [
        _describe_out(path, d)
        for d in described
        if model_kind is None or (d.model is not None and d.model.kind == model_kind)
    ]


@router.get("/{name}", response_model=TableOut, responses=errors(403, 404, 503))
def get_table(
    env: str,
    path: str,
    name: str,
    request: Request,
    lang: str | None = Query(default=None, description=LANG_DESCRIPTION),
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
    main_language: str = Depends(get_main_language),
) -> TableOut:
    """Show a table.

    Shows its texts in the reader's language, each saying which language it is in. Needs
    `READ_TABLE`. Answers 404 when the table does not exist, 422 for a `lang` that is not a
    language tag, and 503 when the environment's storage cannot be reached (try again after
    `Retry-After`).

    Parameters
    ----------
    env
        Environment name.
    path
        Dot-joined namespace path.
    name
        Table name.
    request
        The current request, for `Accept-Language`.
    lang
        The language of the texts; see `reader_languages` for the order without it.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.
    main_language
        `LAKANA_MAIN_LANGUAGE`, the last language a reader without a choice gets.

    Returns
    -------
    TableOut
        The table.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    lakana_core.errors.ValidationError
        Mapped to 422 for a `lang` that is not a language tag.
    """
    languages = reader_languages(request, lang, principal, main_language)
    ns = NamespacePath.from_parts(path.split("."))
    ref = TableRef(environment=env, namespace=ns, name=name)
    return _describe_out(path, catalog.describe_table(principal, ref, languages=languages))


@router.get("/{name}/health", response_model=TableHealthOut, responses=errors(403, 404, 503))
def get_table_health(
    env: str,
    path: str,
    name: str,
    request: Request,
    days: int | None = Query(default=None),
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> TableHealthOut:
    """Show a table's health.

    Shows the table's last maintenance, the newest result of each of its checks and, when
    asked, who read it. Needs `READ_TABLE`. Answers 404 when the table does not exist and
    422 when `days` is outside 1 to the period the server keeps. Answers 503 when `readers`
    names someone other than the caller and the audit entry for that could not be written;
    nothing is returned then.

    Parameters
    ----------
    env
        Environment name.
    path
        Dot-joined namespace path.
    name
        Table name.
    request
        The current request, for the `LAKANA_TABLE_READS__*` settings.
    days
        The window of the readers in days, from 1 to the kept period
        (`LAKANA_TABLE_READS__KEEP_DAYS`, 90 by default); the default window
        (`LAKANA_TABLE_READS__DEFAULT_DAYS`, 30) when absent.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Returns
    -------
    TableHealthOut
        The health. `readers` is `null` unless the caller may see the table's grants.

    Raises
    ------
    lakana_core.errors.ValidationError
        Mapped to 422 when `days` is outside 1 to the kept period.
    lakana_core.errors.NotFoundError
        Mapped to 404.
    lakana_core.errors.PermissionDenied
        Mapped to 403 without READ_TABLE.
    lakana_core.errors.AuditUnavailableError
        Mapped to 503 with `Retry-After`, if `readers` names someone other than the caller and
        its `ACTIVITY_VIEWED` audit row could not be written; nothing is returned.
    """
    reads = request.app.state.settings.table_reads
    if days is None:
        days = reads.default_days
    elif not 1 <= days <= reads.keep_days:
        raise ValidationError(
            f"days must be between 1 and {reads.keep_days} (got {days}). The platform keeps "
            f"reads for {reads.keep_days} days; an administrator can change that with "
            "LAKANA_TABLE_READS__KEEP_DAYS."
        )
    ref = TableRef(environment=env, namespace=NamespacePath.from_parts(path.split(".")), name=name)
    health = catalog.table_health(principal, ref, days=days, now=datetime.now(UTC))
    return TableHealthOut.model_validate(health, from_attributes=True)


_EDIT_LANG_DESCRIPTION = (
    "The language edited, such as fr; without it, the table's main language. The answer's texts "
    "are in it too."
)
"""The description of the `lang` query parameter of the two description routes."""


def _sent(body: DescriptionIn, field: Literal["description", "label"]) -> str | _Keep | None:
    """Read one field of a description body, or `KEEP` when it was left out.

    Parameters
    ----------
    body
        The request body.
    field
        ``"description"`` or ``"label"``.

    Returns
    -------
    str or None or _Keep
        The text, `None` to reset, or `KEEP` for a field the body did not send.
    """
    return getattr(body, field) if field in body.model_fields_set else KEEP


@router.patch("/{name}", response_model=TableOut, responses=errors(403, 404))
def describe_table(
    env: str,
    path: str,
    name: str,
    body: DescriptionIn,
    request: Request,
    lang: str | None = Query(default=None, description=_EDIT_LANG_DESCRIPTION),
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
    main_language: str = Depends(get_main_language),
) -> TableOut:
    """Set a table's description and label.

    Sets the texts shown for the table in one language (`lang`, else the table's main language),
    or with `null` removes an edit so that the text of the project shows again. A field left out
    of the body stays as it is. A deploy never replaces a text set here. The table's owner or a
    holder of `DROP_TABLE` may do this, and must be able to read the table. Answers 404 when the
    table does not exist and 422 for a body with neither field, a blank text, a description over
    4,000 characters, a label over 100 characters or on several lines, or a `lang` that is not a
    language tag.

    Parameters
    ----------
    env
        Environment name.
    path
        Dot-joined namespace path.
    name
        Table name.
    body
        The new `description` and/or `label`, each a text or `null`.
    request
        The current request, for `Accept-Language`.
    lang
        The language edited; also the language of the answer's texts.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.
    main_language
        `LAKANA_MAIN_LANGUAGE`, the last language a reader without a choice gets.

    Returns
    -------
    TableOut
        The table, with the texts it shows now, in the reader's language.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404.
    lakana_core.errors.PermissionDenied
        Mapped to 403, unless the caller owns the table or holds `DROP_TABLE` on it.
    lakana_core.errors.ValidationError
        Mapped to 422 for a text that breaks its rules or a `lang` that is not a tag.
    """
    languages = reader_languages(request, lang, principal, main_language)
    ref = TableRef(environment=env, namespace=NamespacePath.from_parts(path.split(".")), name=name)
    catalog.set_table_description(
        principal,
        ref,
        _sent(body, "description"),
        label=_sent(body, "label"),
        language=lang,
    )
    return _describe_out(path, catalog.describe_table(principal, ref, languages=languages))


@router.put(
    "/{name}/columns/{column}/description", response_model=TableOut, responses=errors(403, 404)
)
def describe_column(
    env: str,
    path: str,
    name: str,
    column: str,
    body: DescriptionIn,
    request: Request,
    lang: str | None = Query(default=None, description=_EDIT_LANG_DESCRIPTION),
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
    main_language: str = Depends(get_main_language),
) -> TableOut:
    """Set a column's description and label.

    Sets the texts shown for a column in one language (`lang`, else the table's main language),
    or with `null` removes an edit so that the text of the project shows again. A field left out
    of the body stays as it is. The table's owner or a holder of `DROP_TABLE` may do this, and
    must be able to read the table. Answers 404 when the table or column does not exist and 422
    for a body with neither field, a blank text, a description over 4,000 characters, a label
    over 100 characters or on several lines, or a `lang` that is not a language tag.

    Parameters
    ----------
    env
        Environment name.
    path
        Dot-joined namespace path.
    name
        Table name.
    column
        The column's name; it must be in the table's schema.
    body
        The new `description` and/or `label`, each a text or `null`.
    request
        The current request, for `Accept-Language`.
    lang
        The language edited; also the language of the answer's texts.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.
    main_language
        `LAKANA_MAIN_LANGUAGE`, the last language a reader without a choice gets.

    Returns
    -------
    TableOut
        The table, with the texts it shows now, in the reader's language.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 for a missing table or column.
    lakana_core.errors.PermissionDenied
        Mapped to 403, unless the caller owns the table or holds `DROP_TABLE` on it, and may
        read it.
    lakana_core.errors.ValidationError
        Mapped to 422 for a text that breaks its rules or a `lang` that is not a tag.
    """
    languages = reader_languages(request, lang, principal, main_language)
    ref = TableRef(environment=env, namespace=NamespacePath.from_parts(path.split(".")), name=name)
    catalog.set_column_description(
        principal,
        ref,
        column,
        _sent(body, "description"),
        label=_sent(body, "label"),
        language=lang,
    )
    return _describe_out(path, catalog.describe_table(principal, ref, languages=languages))


@router.get("/{name}/lineage", response_model=LineageOut, responses=errors(403, 404))
def get_table_lineage(
    env: str,
    path: str,
    name: str,
    direction: Literal["upstream", "downstream"] = Query(default="upstream"),
    depth: int = Query(default=3, ge=1, le=10),
    principal: PrincipalRef = Depends(get_principal),
    lineage: LineageService = Depends(get_lineage_service),
) -> LineageOut:
    """Show a table's lineage.

    Walks the recorded lineage of the table: the tables it was derived from and the tables
    derived from it. A table or artifact written by a Python asset that declares connections
    also lists each connection as a source (`kind` ``CONNECTION``), shown only to a caller who
    may read the environment's connections. Needs `READ_TABLE` on the table itself. Answers
    404 when the table does not exist.

    Parameters
    ----------
    env
        Environment name.
    path
        Dot-joined namespace path.
    name
        Table name.
    direction
        `upstream` (the default) or `downstream`.
    depth
        Hops to walk, 1 to 10 (default 3).
    principal
        The authenticated caller.
    lineage
        The lineage service for this request.

    Returns
    -------
    LineageOut
        The visible nodes and edges, and how many nodes were hidden.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404.
    lakana_core.errors.PermissionDenied
        Mapped to 403, if the caller can't read the table itself.
    """
    ref = TableRef(environment=env, namespace=NamespacePath.from_parts(path.split(".")), name=name)
    graph = lineage.lineage(principal, ref, direction=LineageDirection(direction), depth=depth)
    return LineageOut(
        root_id=str(graph.root_id),
        direction=graph.direction.value,
        depth=graph.depth,
        nodes=[
            LineageNodeOut(
                id=str(n.id),
                name=n.name,
                kind=n.kind,
                dropped=n.dropped,
                connection_kind=n.connection_kind,
            )
            for n in graph.nodes
        ],
        edges=[
            LineageEdgeOut(
                source_id=str(e.source_id),
                target_id=str(e.target_id),
                source_snapshot_id=e.source_snapshot_id,
                target_snapshot_id=e.target_snapshot_id,
                query_run_id=None if e.query_run_id is None else str(e.query_run_id),
                created_at=e.created_at,
            )
            for e in graph.edges
        ],
        hidden_nodes=graph.hidden_nodes,
        truncated=graph.truncated,
    )


@router.get("/{name}/references", response_model=TableReferencesOut, responses=errors(403, 404))
def get_table_references(
    env: str,
    path: str,
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> TableReferencesOut:
    """Show what a table's header says it references, and which tables reference it.

    Needs `READ_TABLE` on the table. A reference whose target, or whose referencing table, the
    caller may not read is counted in `hidden_outgoing` or `hidden_incoming`, never named.
    These are the references the model declares, not lineage. Answers 404 when the table does
    not exist.

    Parameters
    ----------
    env
        Environment name.
    path
        Dot-joined namespace path.
    name
        Table name.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Returns
    -------
    TableReferencesOut
        Outgoing and incoming references.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404.
    lakana_core.errors.PermissionDenied
        Mapped to 403, if the caller can't read the table itself.
    """
    ref = TableRef(environment=env, namespace=NamespacePath.from_parts(path.split(".")), name=name)
    found = catalog.table_references(principal, ref)
    return TableReferencesOut.model_validate(found, from_attributes=True)


@router.post(
    "/{name}/maintenance",
    response_model=QuerySubmitOut,
    status_code=202,
    responses=errors(403, 404),
)
def maintain_table(
    env: str,
    path: str,
    name: str,
    body: MaintenanceIn,
    principal: PrincipalRef = Depends(get_principal),
    queries: QueryService = Depends(get_query_service),
) -> QuerySubmitOut:
    """Queue maintenance for a table.

    Queues expiring the table's old snapshots and deleting its orphaned files, and answers
    202 with a query id to poll. A worker does the work; the finished status carries a
    `maintenance.report` whose `note` says why part of the run was skipped, if it was. Needs
    `MAINTAIN_TABLE` on the table and `SUBMIT_QUERY` on the environment. Answers 404 when
    the table does not exist.

    Parameters
    ----------
    env
        Environment name.
    path
        Dot-joined namespace path.
    name
        Table name.
    body
        `keep_days`, `keep_last`, `dry_run` and `older_than_hours`.
    principal
        The authenticated caller.
    queries
        The query service for this request.

    Returns
    -------
    QuerySubmitOut
        The maintenance run's id.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404, if the table does not exist.
    lakana_core.errors.PermissionDenied
        Mapped to 403, without MAINTAIN_TABLE on the table or SUBMIT_QUERY
        on the environment.
    """
    run = queries.submit_maintenance(
        principal,
        env,
        f"{path}.{name}",
        keep_days=body.keep_days,
        keep_last=body.keep_last,
        dry_run=body.dry_run,
        older_than_hours=body.older_than_hours,
    )
    return QuerySubmitOut(query_id=str(run.id))


@router.delete("/{name}", status_code=204, responses=errors(403, 404))
def drop_table(
    env: str,
    path: str,
    name: str,
    purge: bool = False,
    principal: PrincipalRef = Depends(get_write_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> None:
    """Drop a table.

    Removes the table from the catalog at once, with its grants, row filters and column
    masks. Needs `DROP_TABLE`. A managed table keeps its files for the undo period (7 days
    unless the operator changed it), during which it can be restored, and then they are
    deleted; an external table's files and a view are never deleted. With `purge` set, the
    files are due at once, the next clean-up deletes them and the table cannot be restored.
    Answers 404 when the table does not exist.

    Parameters
    ----------
    env
        Environment name.
    path
        Dot-joined namespace path.
    name
        Table name.
    purge
        `true` makes the files due at once: the next sweep deletes them, and the table cannot be
        restored.
    principal
        The authenticated caller, with any `?break_glass_reason=` attached.
    catalog
        The catalog service for this request.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    ns = NamespacePath.from_parts(path.split("."))
    catalog.drop_table(principal, TableRef(environment=env, namespace=ns, name=name), purge=purge)


@router.post("/{name}/undrop", response_model=TableOut, responses=errors(403, 404, 409))
def undrop_table(
    env: str,
    path: str,
    name: str,
    body: UndropIn,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> TableOut:
    """Restore a dropped table.

    Brings a dropped table back within its undo period, with its data and policies. Needs
    `DROP_TABLE` and `CREATE_TABLE` on the namespace. Answers 404 when no dropped table of
    that name can be restored or its namespace is gone, and 409 when a table of the name
    exists (send `new_name`), the undo period has ended or the clean-up has started deleting
    the files.

    Parameters
    ----------
    env
        Environment name.
    path
        Dot-joined namespace path the table was dropped from.
    name
        The name the table had when it was dropped.
    body
        A new name, if its own is taken, and a break-glass reason.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Returns
    -------
    TableOut
        The restored table.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404: no dropped table of that name can be restored, or its namespace is gone.
    lakana_core.errors.AlreadyExistsError
        Mapped to 409: a table of the name exists; send `new_name`.
    lakana_core.errors.ConflictError
        Mapped to 409: the undo period has ended, or the sweep has started deleting the files.
    lakana_core.errors.PermissionDenied
        Mapped to 403, without `DROP_TABLE` and `CREATE_TABLE` on the namespace.
    """
    ns = NamespacePath.from_parts(path.split("."))
    loaded = catalog.undrop_table(
        with_break_glass(principal, body.break_glass_reason),
        TableRef(environment=env, namespace=ns, name=name),
        new_name=body.new_name,
    )
    return _to_out(path, loaded)


@router.get("/{name}/retention", response_model=TableRetentionOut, responses=errors(403, 404))
def get_table_retention(
    env: str,
    path: str,
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> TableRetentionOut:
    """Show a table's retention.

    Shows how long the managed table is kept after its last activity, where that setting
    comes from and when the table expires. Needs `READ_TABLE`. Answers 404 when the table
    does not exist and 422 for a view, an external table or an artifact.

    Parameters
    ----------
    env
        Environment name.
    path
        Dot-joined namespace path.
    name
        Table name.
    principal
        The authenticated caller; needs `READ_TABLE`.
    catalog
        The catalog service for this request.

    Returns
    -------
    TableRetentionOut
        The retention.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404.
    lakana_core.errors.ValidationError
        Mapped to 422 for a view, an external table or an artifact.
    """
    ref = TableRef(environment=env, namespace=NamespacePath.from_parts(path.split(".")), name=name)
    return _retention_out(catalog.table_retention(principal, ref))


@router.put("/{name}/retention", response_model=TableRetentionOut, responses=errors(403, 404))
def set_table_retention(
    env: str,
    path: str,
    name: str,
    body: TableRetentionIn,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> TableRetentionOut:
    """Set a table's retention.

    Sets when a managed table expires: `days` after its last activity, `on` a given day, or
    `never`. The table's owner may always. Anyone else needs `DROP_TABLE` for a setting that
    makes the table expire earlier than now, or at all, and `WRITE_TABLE` to make it expire
    later or never; a refusal is audited. Answers 404 when the table does not exist and 422
    unless exactly one valid rule is given, the table is managed, and, for `days` or `on`,
    the environment is not protected.

    Parameters
    ----------
    env
        Environment name.
    path
        Dot-joined namespace path.
    name
        Table name.
    body
        Exactly one of `days`, `on` and `never`.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Returns
    -------
    TableRetentionOut
        The retention now.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404.
    lakana_core.errors.PermissionDenied
        Mapped to 403, without the right the change needs; audited.
    lakana_core.errors.ValidationError
        Mapped to 422: not exactly one valid rule, not a managed table, or `days` or `on` in a
        protected environment.
    """
    ref = TableRef(environment=env, namespace=NamespacePath.from_parts(path.split(".")), name=name)
    retention = catalog.set_table_retention(
        principal, ref, days=body.days, on=body.on, never=body.never
    )
    return _retention_out(retention)


@expiring_router.get("", response_model=list[TableRetentionOut], responses=errors(403, 404))
def list_expiring_tables(
    env: str,
    request: Request,
    within_days: int | None = Query(default=None),
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> list[TableRetentionOut]:
    """List the tables about to expire.

    Lists the managed tables of the environment that expire within `within_days`, soonest
    first. Needs `READ_ENVIRONMENT`. Answers 404 when the environment does not exist and 422
    when `within_days` is out of range.

    Parameters
    ----------
    env
        Environment name.
    request
        The current request, for `LAKANA_TABLES__EXPIRY_WARNING_DAYS`.
    within_days
        The window, 1 to 3,650 days; the server's warning window (7 days by default), or 1
        when that is 0, when absent.
    principal
        The authenticated caller; needs `READ_ENVIRONMENT`, and sees the tables it may read.
    catalog
        The catalog service for this request.

    Returns
    -------
    list of TableRetentionOut
        Each with its expiry, the rule and who set it; a table already due is listed too.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404, if the environment does not exist.
    lakana_core.errors.ValidationError
        Mapped to 422 when `within_days` is out of range.
    """
    if within_days is None:
        within_days = max(request.app.state.settings.tables.expiry_warning_days, 1)
    return [
        _retention_out(entry)
        for entry in catalog.list_expiring_tables(principal, env, within_days=within_days)
    ]


@dropped_router.get("", response_model=list[DroppedTableOut], responses=errors(403, 404))
def list_dropped_tables(
    env: str,
    path: str,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> list[DroppedTableOut]:
    """List the dropped tables of a namespace.

    Lists the tables dropped from the namespace whose files are not deleted yet and that can
    therefore still be restored. Needs `READ_TABLE` on the namespace. Answers 404 when the
    namespace does not exist.

    Parameters
    ----------
    env
        Environment name.
    path
        Dot-joined namespace path.
    principal
        The authenticated caller; needs `READ_TABLE` on the namespace.
    catalog
        The catalog service for this request.

    Returns
    -------
    list of DroppedTableOut
        Newest drop first, with the time each one's files are deleted.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404, if the namespace does not exist.
    """
    ns = NamespacePath.from_parts(path.split("."))
    return [
        DroppedTableOut(
            name=dropped.name,
            namespace=path,
            uuid=str(dropped.table_id),
            kind=dropped.kind,
            dropped_at=dropped.dropped_at,
            purge_after=dropped.purge_after,
            restorable=dropped.restorable,
        )
        for dropped in catalog.list_dropped_tables(principal, env, ns)
    ]


@router.post(
    "/external", response_model=ExternalTableOut, status_code=201, responses=errors(403, 404, 409)
)
def create_external_table(
    env: str,
    path: str,
    body: ExternalTableCreate,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> ExternalTableOut:
    """Register an external table.

    Registers a raw-zone table backed by files that are already in storage. Needs
    `CREATE_TABLE`. Answers 404 when the environment or namespace does not exist and 422
    when `location_pattern` is not shaped `raw/<directory>/<file-glob>`, claims files
    another external table in the environment claims, leaves the storage root, or the
    `format` is not supported.

    Parameters
    ----------
    env
        Environment name.
    path
        Dot-joined namespace path.
    body
        The table to register.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Returns
    -------
    ExternalTableOut
        The registered table and its inferred schema.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    lakana_core.errors.ValidationError
        Mapped to 422, if `location_pattern` is not shaped
        `"raw/<dir...>/<basename-glob>"`, its claim overlaps another active
        external table's claim in this environment, it escapes the storage
        root, or `format` is unsupported.
    lakana_core.errors.LakanaError
        Mapped to 500: the table just registered has no `external_config`,
        which `create_external_table` always sets.
    """
    ns = NamespacePath.from_parts(path.split("."))
    ref = TableRef(environment=env, namespace=ns, name=body.name)
    table = catalog.create_external_table(
        with_break_glass(principal, body.break_glass_reason),
        ref,
        location_pattern=body.location_pattern,
        format=body.format,
    )
    config = table.external_config
    if config is None:  # pragma: no cover
        # create_external_table sets external_config in the same transaction
        # that inserts the row, so a table it just returned always has one;
        # unreachable in practice, and kept only so a type checker can narrow
        # `config` from `dict | None`, the same reasoning `queries.py`
        # documents for its own untestable narrowing branch. A bare LakanaError
        # maps to 500 (`lakana_server.errors`), which is what a broken internal
        # invariant is — not a 4xx blaming the caller. Not an `assert`: those
        # are stripped under `-O`, so the narrowing would silently vanish.
        raise LakanaError("Registered external table has no external_config recorded.")
    return ExternalTableOut(
        name=table.name,
        namespace=path,
        location_pattern=config["location_pattern"],
        format=config["format"],
        schema=config["schema"],
    )


@router.post(
    "/{name}/data", response_model=AppendOut, responses=errors(403, 404, 409, 408, 413, 503)
)
@streams_its_own_body
async def append_table_data(
    env: str,
    path: str,
    name: str,
    request: Request,
    source: list[str] | None = Query(default=None),
    source_version: list[str] | None = Query(default=None),
    expected_snapshot_id: str | None = Query(default=None, max_length=64),
    principal: PrincipalRef = Depends(get_write_principal),
    catalog: CatalogService = Depends(get_catalog),
    session: Session = Depends(get_session),
) -> AppendOut:
    """Append rows to a table.

    Adds the rows of an Arrow IPC stream, sent as the request body, to a managed table. Each
    `source` (repeatable) names a table the rows were derived from; it must exist and be
    readable by the caller, and records a lineage edge. Needs `WRITE_TABLE` on the table.
    Answers 404 when the table or a source does not exist, 409 when `expected_snapshot_id`
    no longer matches, 413 for a body over the server's limit, 408 when the body arrives too
    slowly and 503 when the server is busy with other uploads (try again after
    `Retry-After`).

    Parameters
    ----------
    env
        Environment name.
    path
        Dot-joined namespace path.
    name
        Table name.
    request
        The current request.
    source
        Dotted names of the tables the rows came from.
    source_version
        ``<version>:<dotted name>`` for each source that is an artifact, when the
        task read a specific version; the edge records it instead of the current one.
        A malformed value is a 422 before the body is read.
    expected_snapshot_id
        The table's current snapshot as the caller read it, or `none`; the
        write commits only while that still holds (409 otherwise).
    principal
        The authenticated caller, with any `?break_glass_reason=` attached.
    catalog
        The catalog service for this request.
    session
        The request's session.

    Returns
    -------
    AppendOut
        How many rows and files were written, and the committed snapshot's id.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404: the table or a declared source does not exist.
    lakana_core.errors.PermissionDenied
        Mapped to 403: the caller may not write the table or read a source.
    lakana_core.errors.ConflictError
        Mapped to 409: `expected_snapshot_id` no longer matches.

    Notes
    -----
    The body controls are shared with `replace_table_data` through `_governed_arrow_write`.
    """
    expected, versions, ref = _arrow_write_target(
        request, env, path, name, source, source_version, expected_snapshot_id
    )
    result = await _governed_arrow_write(
        request,
        principal,
        catalog,
        session,
        ref,
        lambda data: catalog.append(
            principal,
            ref,
            data,
            sources=source or [],
            source_versions=versions,
            expected_snapshot_id=expected,
        ),
        precheck=lambda: catalog.check_can_append(principal, ref, expected_snapshot_id=expected),
    )
    return AppendOut(rows=result.rows, files=result.files, snapshot_id=result.snapshot_id)


@router.put(
    "/{name}/data", response_model=ReplaceOut, responses=errors(403, 404, 409, 408, 413, 503)
)
@streams_its_own_body
async def replace_table_data(
    env: str,
    path: str,
    name: str,
    request: Request,
    evolve_schema: bool = Query(default=False),
    source: list[str] | None = Query(default=None),
    source_version: list[str] | None = Query(default=None),
    expected_snapshot_id: str | None = Query(default=None, max_length=64),
    principal: PrincipalRef = Depends(get_write_principal),
    catalog: CatalogService = Depends(get_catalog),
    session: Session = Depends(get_session),
) -> ReplaceOut:
    """Replace all rows of a table.

    Replaces every row of a managed table with the rows of an Arrow IPC stream, sent as the
    request body, in one snapshot; the table keeps its history. Takes the same `source` and
    `expected_snapshot_id` as the append. Needs `WRITE_TABLE` on the table, and
    `CREATE_TABLE` as well to create a missing table with `expected_snapshot_id=none`.
    Answers 404 when the table or a source does not exist, 409 when `expected_snapshot_id`
    no longer matches, 413 for a body over the server's limit, 408 when the body arrives too
    slowly, 503 when the server is busy with other uploads (try again after `Retry-After`)
    and 422 when the upload does not fit the table (the message shows the difference).

    Parameters
    ----------
    env
        Environment name.
    path
        Dot-joined namespace path.
    name
        Table name.
    request
        The current request.
    evolve_schema
        Let the table adopt the upload's schema.
    source
        Dotted names of the tables the rows came from.
    source_version
        ``<version>:<dotted name>`` for each source that is an artifact, when the
        task read a specific version; the edge records it instead of the current one.
        A malformed value is a 422 before the body is read.
    expected_snapshot_id
        The table's current snapshot as the caller read it, or `none`; the
        write commits only while that still holds (409 otherwise). On replace, `none`
        also creates a missing table from the upload.
    principal
        The authenticated caller, with any `?break_glass_reason=` attached.
    catalog
        The catalog service for this request.
    session
        The request's session.

    Returns
    -------
    ReplaceOut
        What was written, and how the schema changed.

    Raises
    ------
    lakana_core.errors.ValidationError
        Mapped to 422: the upload is incompatible (the message carries the
        diff) or any body check fails.
    lakana_core.errors.NotFoundError
        Mapped to 404: the table or a declared source does not exist.
    lakana_core.errors.PermissionDenied
        Mapped to 403.
    lakana_core.errors.ConflictError
        Mapped to 409: `expected_snapshot_id` no longer matches.

    Notes
    -----
    The body controls are shared with `append_table_data` through `_governed_arrow_write`.
    """
    expected, versions, ref = _arrow_write_target(
        request, env, path, name, source, source_version, expected_snapshot_id
    )
    result = await _governed_arrow_write(
        request,
        principal,
        catalog,
        session,
        ref,
        lambda data: catalog.replace(
            principal,
            ref,
            data,
            evolve_schema=evolve_schema,
            sources=source or [],
            source_versions=versions,
            expected_snapshot_id=expected,
        ),
        precheck=lambda: catalog.check_can_replace(principal, ref, expected_snapshot_id=expected),
    )
    return ReplaceOut(
        rows=result.rows,
        files=result.files,
        snapshot_id=result.snapshot_id,
        schema_diff=None if result.schema_diff.is_empty() else result.schema_diff.to_json(),
    )


@router.post(
    "/{name}/merge", response_model=MergeOut, responses=errors(403, 404, 409, 408, 413, 503)
)
@streams_its_own_body
async def merge_table_data(
    env: str,
    path: str,
    name: str,
    request: Request,
    source: list[str] | None = Query(default=None),
    source_version: list[str] | None = Query(default=None),
    expected_snapshot_id: str | None = Query(default=None, max_length=64),
    key: list[str] = Query(min_length=1),
    principal: PrincipalRef = Depends(get_write_principal),
    catalog: CatalogService = Depends(get_catalog),
    session: Session = Depends(get_session),
) -> MergeOut:
    """Merge rows into a table on a key.

    Sends the rows of an Arrow IPC stream as the request body. A row whose `key` (repeatable,
    required: one or more columns) matches a row of the table replaces it; any other row is
    inserted; nothing is deleted. One snapshot: readers see the table before or after, never a
    half. Takes the same `source`, `source_version` and `expected_snapshot_id` as the append,
    and the same body limits. Needs `WRITE_TABLE` and `READ_TABLE` on the table (a merge
    compares the rows with the table's), and `CREATE_TABLE` as well to create a missing table
    with `expected_snapshot_id=none`; it is refused (422) while a row filter or column mask on
    the table applies to the caller. Answers 404 when the table or a
    source does not exist, 409 when `expected_snapshot_id` no longer matches, 413 for a body
    over the server's limit, 408 when the body arrives too slowly, 503 when the server is busy
    with other uploads (try again after `Retry-After`) and 422 for a key column the table or
    the rows do not have, a null key, a key that repeats in the rows or matches two rows of the
    table (counts only, never values), a key of several columns with more than 5,000 rows, or
    rows that do not fit the table.

    Parameters
    ----------
    env
        Environment name.
    path
        Dot-joined namespace path.
    name
        Table name.
    request
        The current request.
    key
        The columns that identify a row; repeat the parameter for a key of several columns.
    source
        Dotted names of the tables the rows came from.
    source_version
        ``<version>:<dotted name>`` for each source that is an artifact, when the
        task read a specific version; the edge records it instead of the current one.
        A malformed value is a 422 before the body is read.
    expected_snapshot_id
        The table's current snapshot as the caller read it, or `none`; the merge commits only
        while that still holds (409 otherwise). `none` also creates a missing table from the
        rows.
    principal
        The authenticated caller, with any `?break_glass_reason=` attached.
    catalog
        The catalog service for this request.
    session
        The request's session.

    Returns
    -------
    MergeOut
        The committed snapshot's id and how many rows were inserted and updated.

    Raises
    ------
    lakana_core.errors.ValidationError
        Mapped to 422: a key column the table or the rows do not have, a null key, a key that
        repeats in the rows or matches two rows of the table, a key of several columns with
        more than 5,000 rows, rows that do not fit the table, a row filter or column mask that
        applies to the caller, or any body check.
    lakana_core.errors.NotFoundError
        Mapped to 404: the table or a declared source does not exist.
    lakana_core.errors.PermissionDenied
        Mapped to 403.
    lakana_core.errors.ConflictError
        Mapped to 409: `expected_snapshot_id` no longer matches, or another writer committed
        first.

    Notes
    -----
    The body controls are shared with `append_table_data` through `_governed_arrow_write`.
    """
    expected, versions, ref = _arrow_write_target(
        request, env, path, name, source, source_version, expected_snapshot_id
    )
    result = await _governed_arrow_write(
        request,
        principal,
        catalog,
        session,
        ref,
        lambda data: catalog.merge(
            principal,
            ref,
            data,
            key=key,
            sources=source or [],
            source_versions=versions,
            expected_snapshot_id=expected,
        ),
        precheck=lambda: catalog.check_can_merge(principal, ref, expected_snapshot_id=expected),
    )
    return MergeOut(
        snapshot_id=result.snapshot_id,
        rows_inserted=result.rows_inserted,
        rows_updated=result.rows_updated,
    )


@router.post("/{name}/load", response_model=LoadOut, responses=errors(403, 404, 409, 408, 413, 503))
@streams_its_own_body
async def load_table_data(
    env: str,
    path: str,
    name: str,
    request: Request,
    write: Literal["replace", "append"] = Query(default="replace"),
    expected_snapshot_id: str | None = Query(default=None, max_length=64),
    dry_run: bool = Query(default=False),
    principal: PrincipalRef = Depends(get_write_principal),
    catalog: CatalogService = Depends(get_catalog),
    session: Session = Depends(get_session),
) -> LoadOut:
    """Load rows into a table of the bronze layer.

    The route `lakana load` calls: it writes the rows of an Arrow IPC stream, sent as the
    request body, into a table whose first namespace segment is `bronze`, and refuses any other
    target with 422 before it reads the body, for every client alike. `expected_snapshot_id=none`
    creates a missing table (`CREATE_TABLE` as well as `WRITE_TABLE`); otherwise `write`
    replaces or appends (`WRITE_TABLE`), committing only while the table's snapshot is still
    `expected_snapshot_id`. `dry_run=true` runs every check, the schema of the (usually empty)
    stream against an existing table included, and writes nothing. In a protected environment
    a load, its dry run included, needs `break_glass_reason` and `MANAGE_ENVIRONMENT` (403
    before the body otherwise). Answers 404 when the namespace does not exist (naming the
    command that creates it) or the table does not and no pin was sent (naming `none`), 409 when
    the snapshot moved,
    413 for a body over the server's limit, 408 when it arrives too slowly and 503 when the
    server is busy with other uploads.

    Parameters
    ----------
    env
        Environment name.
    path
        Dot-joined namespace path; its first segment must be `bronze`.
    name
        Table name.
    request
        The current request.
    write
        ``replace`` (the default) or ``append``; a first load creates the table either way.
    expected_snapshot_id
        The table's current snapshot as the caller read it, or `none` for a missing table.
    dry_run
        Check only; write nothing.
    principal
        The authenticated caller, with any `?break_glass_reason=` attached.
    catalog
        The catalog service for this request.
    session
        The request's session.

    Returns
    -------
    LoadOut
        The committed snapshot, the rows written, and whether the table was created.

    Raises
    ------
    lakana_core.errors.ValidationError
        Mapped to 422: a target outside bronze, or rows the table refuses.
    lakana_core.errors.NotFoundError
        Mapped to 404: the namespace, or the table without `none`, does not exist.
    lakana_core.errors.PermissionDenied
        Mapped to 403.
    lakana_core.errors.ConflictError
        Mapped to 409: `expected_snapshot_id` no longer matches.
    """
    expected = _expected_snapshot(request, expected_snapshot_id)
    ref = TableRef(environment=env, namespace=NamespacePath.from_parts(path.split(".")), name=name)
    require_bronze(ref)
    created: list[bool] = []

    def precheck() -> None:
        """Run the load's checks and remember whether it creates the table."""
        created.append(
            catalog.check_can_load(principal, ref, write=write, expected_snapshot_id=expected)
        )

    def load(data: pa.Table) -> LoadOut:
        """Check, or write, the decoded rows.

        Parameters
        ----------
        data
            The rows.

        Returns
        -------
        LoadOut
            What was written.
        """
        if dry_run:
            would_create = catalog.check_can_load(
                principal, ref, write=write, expected_snapshot_id=expected, schema=data.schema
            )
            return LoadOut(snapshot_id=None, rows=0, created=would_create, dry_run=True)
        if created[0]:
            result = catalog.replace(principal, ref, data, expected_snapshot_id="none")
        elif write == "append":
            result = catalog.append(principal, ref, data, expected_snapshot_id=expected)
        else:
            result = catalog.replace(principal, ref, data, expected_snapshot_id=expected)
        return LoadOut(
            snapshot_id=result.snapshot_id, rows=result.rows, created=created[0], dry_run=False
        )

    return await _governed_arrow_write(
        request, principal, catalog, session, ref, load, precheck=precheck
    )
