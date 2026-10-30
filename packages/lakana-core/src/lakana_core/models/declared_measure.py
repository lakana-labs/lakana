"""The declared measure model: one row per measure an asset header declares."""

import uuid

from sqlalchemy import JSON, ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lakana_core.models.base import Base
from lakana_core.models.table import Table


class DeclaredMeasure(Base):
    """One measure a project's asset header declares (`measures:`), as a row.

    Written only by `lakana_core.catalog.model_metadata` at a deploy, a rollback and the creation
    of the table by a run; never through the API. Its stable identifier is
    ``<ns.table>/measure_<column>``, built with the table's current name: a renamed column or table
    is a removal plus an addition. It names only columns of its own table, so a reader of the table
    may see all of it.

    Attributes
    ----------
    id : uuid.UUID
        Unique identifier for this row; kept while the declaration stays.
    environment_id : uuid.UUID
        The environment of the table; the rows go with it.
    table_id : uuid.UUID
        The table that declares the measure; the rows go with it.
    column : str
        The measure's column.
    aggregation : str
        The default aggregation.
    additivity : str
        ``additive``, ``semi`` or ``non``, resolved.
    not_over : list of str
        The columns a ``semi`` measure must not be summed over.
    unit : str or None
        The unit, or `None`.
    currency : str or None
        The ISO 4217 currency, or `None`.
    label : str or None
        The label, in the main language.
    description : str or None
        The description, in the main language.
    """

    __tablename__ = "table_measures"
    __table_args__ = (UniqueConstraint("table_id", "column"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE")
    )
    table_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tables.id", ondelete="CASCADE"))
    column: Mapped[str] = mapped_column(String(1024))
    aggregation: Mapped[str] = mapped_column(String(16))
    additivity: Mapped[str] = mapped_column(String(16))
    not_over: Mapped[list[str]] = mapped_column(JSON)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True, default=None)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True, default=None)
    label: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    description: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)

    table: Mapped[Table] = relationship()
    """The table. Declared so that the unit of work inserts a table before its measures when both
    are new in one commit."""
