"""Iceberg metadata construction, serialisation and update application.

This module is the half of the catalog that has no database. It knows about
pyiceberg and object storage and nothing else, so it can be tested with a temp
directory and no session. It is also the boundary where third-party exception
vocabularies — pyiceberg's and, transitively, pydantic's, which pyiceberg
itself wraps when a stored document fails its schema — become Lakana's own:
`lakana-server` cannot rely on being able to name every third-party exception
type (it must not import pyiceberg at all, per architecture Constraint 2), so
nothing from either vocabulary may reach it untranslated.
"""

from __future__ import annotations

import uuid as uuid_module
from collections.abc import Iterable, Iterator, Mapping, Sequence

from pyiceberg.catalog import TABLE_METADATA_FILE_NAME_REGEX
from pyiceberg.exceptions import CommitFailedException
from pyiceberg.exceptions import ValidationError as IcebergValidationError
from pyiceberg.io import FileIO
from pyiceberg.partitioning import UNPARTITIONED_PARTITION_SPEC, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.serializers import FromInputFile, ToOutputFile
from pyiceberg.table.locations import load_location_provider
from pyiceberg.table.metadata import TableMetadata
from pyiceberg.table.metadata import new_table_metadata as _new_table_metadata
from pyiceberg.table.sorting import UNSORTED_SORT_ORDER, SortOrder
from pyiceberg.table.update import (
    AddSchemaUpdate,
    TableRequirement,
    TableUpdate,
    update_table_metadata,
)
from pyiceberg.types import ListType, MapType, NestedField, StructType

from lakana_core.errors import ConflictError, LakanaError
from lakana_core.errors import ValidationError as LakanaValidationError
from lakana_core.identifiers import check_new_column_name


def parse_metadata_version(location: str | None) -> int:
    """Read the version number out of a metadata file location.

    The version is the numeric prefix of the file name, as in
    ``00007-<uuid>.metadata.json``. Anything that does not match that shape —
    including `None` — yields ``-1``, so that the next version computed from
    it is ``0``.

    Parameters
    ----------
    location
        A metadata file location, or `None` when the table has none yet.

    Returns
    -------
    int
        The parsed version, or ``-1`` when there is none to parse.
    """
    if location is None:
        return -1
    match = TABLE_METADATA_FILE_NAME_REGEX.fullmatch(location.split("/")[-1])
    if match is None:
        return -1
    try:
        uuid_module.UUID(match.group(2))
    except ValueError:
        return -1
    return int(match.group(1))


def next_metadata_location(
    *, table_location: str, current_location: str | None, properties: Mapping[str, str]
) -> str:
    """Compute where the next metadata file for a table should be written.

    The returned name carries both the incremented version and a fresh UUID.
    The UUID is what makes write-then-CAS safe: two writers that compute the
    same version still target different files, so the loser of the
    compare-and-swap orphans a file rather than corrupting one.

    Parameters
    ----------
    table_location
        The table's root location.
    current_location
        The metadata location being replaced, or `None` for a new table.
    properties
        Table properties, which may configure the location provider.

    Returns
    -------
    str
        An absolute URI for the new metadata file.
    """
    provider = load_location_provider(
        table_location=table_location, table_properties=dict(properties)
    )
    return provider.new_table_metadata_file_location(parse_metadata_version(current_location) + 1)


def _field_names(fields: Iterable[NestedField]) -> Iterator[str]:
    """Walk every field name of a schema, nested struct, list and map fields included.

    Parameters
    ----------
    fields
        The top-level fields.

    Yields
    ------
    str
        Each field's own name (not its dotted path), depth first.
    """
    for field in fields:
        yield field.name
        kind = field.field_type
        if isinstance(kind, StructType):
            yield from _field_names(kind.fields)
        elif isinstance(kind, ListType):
            yield from _field_names([kind.element_field])
        elif isinstance(kind, MapType):
            yield from _field_names([kind.key_field, kind.value_field])


def refuse_hidden_column_names(schema: Schema, *, stored: frozenset[str] = frozenset()) -> None:
    """Refuse a new column name that breaks a rule of `check_new_column_name`.

    Parameters
    ----------
    schema
        A new table's schema, or a schema a commit or write adds.
    stored
        Names the table's schemas already hold; they keep working and are not judged.

    Raises
    ------
    LakanaValidationError
        From `check_new_column_name` (an invisible character, a space at either end, a
        character drawn as nothing or another look-alike, or a name not in NFC), for the first
        new name that breaks a rule.
    """
    for name in _field_names(schema.fields):
        if name not in stored:
            check_new_column_name(name)


def build_table_metadata(
    *,
    schema: Schema,
    location: str,
    partition_spec: PartitionSpec | None,
    sort_order: SortOrder | None,
    properties: Mapping[str, str],
    table_uuid: uuid_module.UUID | None = None,
    stored_columns: frozenset[str] = frozenset(),
) -> TableMetadata:
    """Construct metadata for a new table.

    Named to distinguish it from the pyiceberg function it wraps
    (`pyiceberg.table.metadata.new_table_metadata`), which the contract
    originally named this function after.

    Parameters
    ----------
    schema
        The table's schema.
    location
        The table's root location.
    partition_spec
        Partitioning, or `None` for unpartitioned.
    sort_order
        Sort order, or `None` for unsorted.
    properties
        Table properties.
    table_uuid
        The table's id. `create_table` generates this id before computing
        `location` (the location is suffixed with its hex
        digits), so it is passed in here rather than letting pyiceberg
        generate its own — the catalog row's id and the metadata's
        ``table-uuid`` must be the same value. `None` lets pyiceberg
        generate a fresh one, which is only exercised where a caller has no
        such id to thread through (`test_metadata.py`).
    stored_columns
        Column names a dropped table of the same name held; they are not new and are not
        judged (`refuse_hidden_column_names`), so a rebuild can re-create them.

    Returns
    -------
    TableMetadata
        Freshly constructed metadata, with `table_uuid` as its
        ``table-uuid`` when given, or a freshly generated one otherwise.

    Raises
    ------
    LakanaValidationError
        If a column name holds an invisible or line-breaking character
        (`refuse_hidden_column_names`).
    """
    refuse_hidden_column_names(schema, stored=stored_columns)
    return _new_table_metadata(
        schema=schema,
        partition_spec=partition_spec or UNPARTITIONED_PARTITION_SPEC,
        sort_order=sort_order or UNSORTED_SORT_ORDER,
        location=location,
        properties=dict(properties),
        table_uuid=table_uuid,
    )


def write_metadata(file_io: FileIO, metadata: TableMetadata, location: str) -> str:
    """Write metadata to object storage.

    Parameters
    ----------
    file_io
        pyiceberg's view of the backing store.
    metadata
        The metadata to serialise.
    location
        Absolute URI to write to.

    Returns
    -------
    str
        The location written, for convenience at call sites.
    """
    ToOutputFile.table_metadata(metadata, file_io.new_output(location))
    return location


def read_metadata(file_io: FileIO, location: str) -> TableMetadata:
    """Read metadata back from object storage.

    Parameters
    ----------
    file_io
        pyiceberg's view of the backing store.
    location
        Absolute URI to read.

    Returns
    -------
    TableMetadata
        The deserialised metadata.

    Raises
    ------
    LakanaError
        If the document at `location` cannot be parsed as valid Iceberg
        table metadata (malformed JSON, or a structurally invalid document).
        pyiceberg wraps pydantic's own validation failure in its own
        `ValidationError`; this is translated to `LakanaError` rather than
        `lakana_core.errors.ValidationError` because unreadable stored state is
        not a caller mistake — a corrupt file left behind by a partial write
        is not the fault of whoever asked to read it.
    """
    try:
        return FromInputFile.table_metadata(file_io.new_input(location))
    except IcebergValidationError as exc:
        raise LakanaError(f"Metadata at {location!r} could not be parsed") from exc


def apply_updates(base: TableMetadata, updates: Sequence[TableUpdate]) -> TableMetadata:
    """Apply a sequence of Iceberg updates to metadata.

    Parameters
    ----------
    base
        The metadata to update.
    updates
        Updates to apply, in order. These may be client-supplied (via the
        REST catalog's commit endpoint), so a structurally invalid update
        must not surface as an unhandled error.

    Returns
    -------
    TableMetadata
        New metadata. `base` is not modified.

    Raises
    ------
    ValidationError
        If applying `updates` leaves the partition spec or sort order
        incompatible with the schema, if pyiceberg otherwise rejects the
        updates as malformed (for example, referencing an id that does not
        exist), or if an update names a key the metadata does not have.
        pyiceberg raises its own `ValidationError`, a plain `ValueError`, or
        — from `RemovePropertiesUpdate`, which pops each removal off the
        properties dict unguarded — a `KeyError`, which is a `LookupError`
        and so is caught by neither of the other two. All three are
        translated here because `lakana-server` cannot import pyiceberg and so
        cannot catch any of them. Also if an added schema has a new column name that holds an
        invisible or line-breaking character (`refuse_hidden_column_names`); a name one of
        `base`'s schemas already holds is kept.
    """
    added = [update.schema_ for update in updates if isinstance(update, AddSchemaUpdate)]
    if added:
        stored = frozenset(name for schema in base.schemas for name in _field_names(schema.fields))
        for schema in added:
            refuse_hidden_column_names(schema, stored=stored)
    # pyiceberg requires a tuple here, not an arbitrary sequence.
    try:
        return update_table_metadata(base, tuple(updates))
    except IcebergValidationError as exc:
        raise LakanaValidationError(str(exc)) from exc
    except ValueError as exc:
        raise LakanaValidationError(str(exc)) from exc
    except KeyError as exc:
        raise LakanaValidationError(f"Update refers to a key that is not present: {exc}") from exc


def check_requirements(
    base: TableMetadata | None, requirements: Sequence[TableRequirement]
) -> None:
    """Validate every commit requirement against the current metadata.

    `TableRequirement` is a union type rather than a base class, so there is
    no shared method to call on the union itself; each member is validated in
    turn.

    Parameters
    ----------
    base
        Current metadata, or `None` when the table does not exist yet.
    requirements
        Requirements the caller asserts hold.

    Raises
    ------
    ConflictError
        If any requirement is not satisfied. pyiceberg raises
        `CommitFailedException`; it is translated here because `lakana-server`
        cannot import pyiceberg and so cannot catch it.
    """
    for requirement in requirements:
        try:
            requirement.validate(base)
        except CommitFailedException as exc:
            raise ConflictError(str(exc)) from exc
