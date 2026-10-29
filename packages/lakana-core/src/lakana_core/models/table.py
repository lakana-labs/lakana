"""The table model."""

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, TimestampMixin, UTCDateTime


class TableType(enum.StrEnum):
    """How a table's data is managed.

    `ARTIFACT` is a versioned file or directory, not a table: it has no
    Iceberg metadata, no columns and no view definition. It shares the registry
    so a name means one thing.
    """

    ICEBERG = "ICEBERG"
    EXTERNAL = "EXTERNAL"
    VIEW = "VIEW"
    ARTIFACT = "ARTIFACT"


_KINDS: dict[TableType, str] = {
    TableType.ICEBERG: "TABLE",
    TableType.EXTERNAL: "EXTERNAL",
    TableType.VIEW: "VIEW",
    TableType.ARTIFACT: "ARTIFACT",
}
"""The API's `kind` for each `table_type`."""


def table_kind(table_type: TableType) -> str:
    """Name a table type the way the API does.

    Parameters
    ----------
    table_type
        The row's type.

    Returns
    -------
    str
        ``"TABLE"`` for a managed (Iceberg) table, ``"EXTERNAL"`` for a raw-zone table,
        ``"VIEW"`` for a view, ``"ARTIFACT"`` for an artifact.
    """
    return _KINDS[table_type]


class Table(Base, TimestampMixin):
    """A catalog entry for one table.

    For Iceberg tables ``id`` is the Iceberg ``table-uuid``, so Lakana's identity
    and the identity external engines observe are the same value. The row holds
    only a pointer to the current metadata file: Iceberg's own metadata stays in
    object storage, and ``previous_metadata_location`` supports the
    compare-and-swap that makes commits atomic. ``owner_id`` cascades to
    ``NULL`` when the owning principal is deleted, rather than cascading the
    delete: a principal leaving must not delete the tables they owned.
    """

    __tablename__ = "tables"
    __table_args__ = (UniqueConstraint("namespace_id", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    namespace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("namespaces.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    table_type: Mapped[TableType] = mapped_column(Enum(TableType, native_enum=False))
    metadata_location: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    previous_metadata_location: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True, default=None)
    """The size of a managed table's current files (``total-files-size`` of its snapshot).

    Measured by the worker's housekeeping (`lakana_core.catalog.usage`), never at request time;
    `None` before the first measurement and when the snapshot's summary does not say. A tombstone
    keeps it, so a dropped table's size is known for its undo period.
    """
    size_metadata_location: Mapped[str | None] = mapped_column(
        String(1024), nullable=True, default=None
    )
    """The metadata file `size_bytes` was read from; differs from `metadata_location` once the
    table changed after it was measured."""
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="SET NULL"), nullable=True
    )
    external_config: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    """Location, format, and inferred schema, for ``EXTERNAL`` tables only.

    ``None`` for every ``ICEBERG`` table. Set once, at creation, by
    :meth:`lakana_core.catalog.service.CatalogService.create_external_table` — raw-zone
    tables have no commit path, so unlike an Iceberg table's metadata this is never
    updated in place.
    """
    external_claim: Mapped[str | None] = mapped_column(String(1024), nullable=True, default=None)
    """The ``raw/`` directory this external table claims.

    The authoritative binding for file-confinement matching; the name
    columns are display-only. ``None`` for every ``ICEBERG`` table and for
    an ``EXTERNAL`` table created before this column existed.
    """
    view_definition: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    """A view's SQL, references and output columns, for `VIEW` rows only.

    ``{"sql": str, "references": [{"id", "name"}], "columns": [{"name",
    "type", "nullable"}]}``. `None` for every other table type. Written only
    by `CatalogService.create_or_replace_view`.
    """

    unpublished_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, default=None)
    """The run whose first build of this table failed a blocking check.

    Set by `CatalogService.roll_back` in the transaction that removes the table's current
    snapshot; cleared by the commit that gives the table a current snapshot. A table with a
    value here is *not published*: it has a row and no data a reader may see. No foreign key,
    so the marker outlives its run's row.
    """

    kept_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, default=None)
    """The run whose data this table keeps after a failed `keep` check.

    Set by the worker, after the verdict of a check phase of a task that built the table, in
    which only `keep` checks failed; cleared when a later check phase passes and by a rebuild of
    a table that has no checks any more. A rollback to data that the table's current verdict does
    not describe puts the mark that data had back (`kept_earlier`), or, when the history has none,
    makes the mark one of unknown checks (`kept_unknown`); a rollback of a marked table whose
    mark names another run leaves the mark. A table with a value here
    holds data that failed a check and is still published; tasks do not read it. No foreign key,
    so the note outlives its run.
    """
    kept_task_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, default=None)
    """The task run of the check phase that set `kept_run_id`; set and cleared with it."""
    kept_check: Mapped[str | None] = mapped_column(String(1024), nullable=True, default=None)
    """The text the state message shows for the check: the first failing `keep` check, by name.

    ``" and N more"`` follows when more than one failed. Set and cleared with `kept_run_id`.
    """

    kept_unchecked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    """Whether the note names a check that could not run, not one that failed.

    Set with `kept_run_id` when a table was rebuilt and every `keep` check that did not pass could
    not run (it read another project's kept table): the table holds the new run's data, which no
    check could run on. Cleared with the note, and `False` when a failed check moves it.
    """

    kept_unknown: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    """Whether the mark says the table was set back to data whose checks are not known.

    Set with `kept_run_id` by a set-back that finds no entry of `kept_earlier` for the snapshot it
    goes back to (the history ran out, or never existed): the run and task run it names are the
    ones that were set back, `kept_check` is empty. Readers are refused like for any mark, and
    the next passing checks clear it like any mark. `False` for a mark of a failed check.
    """

    kept_snapshot_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, default=None)
    """The snapshot whose verdict the table's current mark state is.

    With a mark, the snapshot the marked run wrote (the data that failed); for a mark that a
    set-back restored or made (unknown checks), the snapshot the table was set back to. Without
    a mark, the snapshot that a passing check judged, whether it cleared a mark or recorded a
    clean write (`lakana_core.runs.kept.KEPT_SNAPSHOT_UNKNOWN` when a clear did not know it), or
    the snapshot an unmarked state was restored for. `None` for a table never judged since this
    column exists, and for a row from before it. An entry of `kept_earlier` carries the same kind
    of value.
    """

    kept_earlier: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    """The states this table had before the current one, the newest last.

    A state is a mark, or "no mark", as a verdict on one snapshot. `note_kept` pushes the state it
    replaces (an empty entry when the data before had no mark), `clear_kept` pushes the mark it
    removes, and `record_clean_in_attempt` pushes the clean state it replaces; an older entry for
    the same snapshot goes. An entry is ``{"run_id", "task_run_id", "check", "unchecked",
    "snapshot_id"}`` (the empty entry is ``{"snapshot_id"}`` alone), where ``snapshot_id`` is the
    snapshot the entry was a verdict on. A set-back to a snapshot that `kept_snapshot_id` does not
    describe takes the newest entry for that snapshot back into the mark columns and drops the
    entries above it, so the table states what the data it holds again was left with. Only the
    newest `lakana_core.runs.kept.KEPT_EARLIER_LIMIT` entries are kept. A set-back with no entry
    for its snapshot (a row from before this column, history that ran out) gives a mark of unknown
    checks. An entry of a mark of unknown checks has ``"unknown": true``.
    """

    description_project: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    """The description the project's asset header gives the table (`description:`).

    Written only by a deploy, a rollback, or the table's creation by a run; never by an edit.
    """

    description_edited: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    """The description set through the API, which is shown instead of `description_project`.

    Written only by `lakana_core.catalog.descriptions`; `None` once reset. A deploy never
    changes it.
    """

    model_kind: Mapped[str | None] = mapped_column(String(16), nullable=True, default=None)
    """The `kind:` the project's asset header declares (``fact``, ``dimension``, ``wide``,
    ``staging``), or `None`.

    The three model columns and the table's `DeclaredReference` rows are written only by a deploy,
    a rollback, or the table's creation by a run (`lakana_core.catalog.model_metadata`), never
    through the API; a deploy that removes the asset clears them.
    """
    model_grain: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    """The `grain:` the asset header declares, or `None`."""
    model_key: Mapped[list[str] | str | None] = mapped_column(JSON, nullable=True, default=None)
    """The `key:` the asset header declares: a list of columns, the string ``"none"`` for
    `key: none`, or `None` when it declares no key."""

    retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    """Days after its last activity this table expires (`lakana table retention --days`).

    At most one of `retention_days`, `retention_on` and `retention_never` is set; with none,
    the environment's retention applies. Written only by `CatalogService.set_table_retention`.
    """
    retention_on: Mapped[date | None] = mapped_column(Date, nullable=True, default=None)
    """The UTC day from which on this table expires (`--on`), whatever its activity."""
    retention_never: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    """Whether this table never expires, whatever its environment's retention says (`--never`)."""
    retention_set_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="SET NULL"), nullable=True, default=None
    )
    """Who last changed the table's retention; `NULL` once that principal is deleted."""
    retention_set_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True, default=None
    )
    """When the table's retention was last changed; `None` if it never was."""

    @property
    def kind(self) -> str:
        """The API's name for this row's type.

        Returns
        -------
        str
            ``"TABLE"`` for a managed (Iceberg) table, ``"EXTERNAL"`` for a
            raw-zone table, ``"VIEW"`` for a view, ``"ARTIFACT"`` for an artifact.
        """
        return table_kind(self.table_type)
