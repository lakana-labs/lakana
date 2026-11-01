"""The declared reference model: one row per reference an asset header declares."""

import uuid

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lakana_core.models.base import Base
from lakana_core.models.table import Table


class DeclaredReference(Base):
    """One reference a project's asset header declares (`references:`), as a row.

    Written only by `lakana_core.catalog.model_metadata` at a deploy, a rollback and the creation
    of the table by a run; never through the API. Its stable identifier is
    ``<ns.table>/ref_<column>``, the name of the generated check: a renamed column or table is a
    removal plus an addition. The target is held by name, as the header names it, so a reader
    resolves it (and checks its read rights on it) when it reads; a stored target id would follow
    a renamed table while the header still names the old one.

    Attributes
    ----------
    id : uuid.UUID
        Unique identifier for this row; kept while the declaration stays.
    environment_id : uuid.UUID
        The environment of the source table; the rows go with it.
    source_table_id : uuid.UUID
        The table that declares the reference; the rows go with it.
    source_column : str
        The referencing column of the source table.
    target_name : str
        The dotted name of the table it points at, in the same environment.
    target_column : str
        The column of the target it must match.
    at_column : str or None
        The referencing table's time column (`at:`) for a target that keeps history; `None`
        otherwise.
    """

    __tablename__ = "table_references"
    __table_args__ = (
        UniqueConstraint("source_table_id", "source_column"),
        Index("ix_table_references_environment_target", "environment_id", "target_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE")
    )
    source_table_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tables.id", ondelete="CASCADE"))
    source_column: Mapped[str] = mapped_column(String(1024))
    target_name: Mapped[str] = mapped_column(String(1024))
    target_column: Mapped[str] = mapped_column(String(1024))
    at_column: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    table: Mapped[Table] = relationship()
    """The source table. Declared so that the unit of work inserts a table before its references
    when both are new in one commit."""
