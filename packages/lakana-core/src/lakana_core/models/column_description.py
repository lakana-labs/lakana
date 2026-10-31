"""The column description model."""

import uuid

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lakana_core.models.base import Base, EditTrailMixin
from lakana_core.models.table import Table


class ColumnDescription(Base, EditTrailMixin):
    """The description of one column of a table, from the project and from an edit.

    Two texts, like `Table.description_project` and `Table.description_edited`: a deploy writes
    only `description_project`, an edit through the API only `description_edited`, and the edit
    is shown when it is set. A row outlives its column: the text of a column that is no longer
    in the table's schema stays and is not shown, so it is there again if the column returns.

    Attributes
    ----------
    id : uuid.UUID
        Unique identifier for this row.
    table_id : uuid.UUID
        The table; the row goes with the table.
    column : str
        The column's name.
    description_project : str or None
        The text of the asset header's `columns:` map.
    description_edited : str or None
        The text set through the API.
    label_project : str or None
        The label of the asset header's `columns:` map, in the table's main language.
    label_edited : str or None
        The label set through the API, shown instead of `label_project`.
    updated_by : uuid.UUID or None
        The principal who last set or reset the edit; `None` once they are deleted.
    updated_at : datetime or None
        When the edit was last set or reset.
    """

    __tablename__ = "column_descriptions"
    __table_args__ = (UniqueConstraint("table_id", "column"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    table_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tables.id", ondelete="CASCADE"))
    column: Mapped[str] = mapped_column(String(1024))
    description_project: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    description_edited: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    label_project: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    label_edited: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)

    table: Mapped[Table] = relationship()
    """The table. Declared so that the unit of work inserts a table before its descriptions
    when both are new in one commit (a run creates the table the project describes)."""
