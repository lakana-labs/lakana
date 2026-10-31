"""The translated texts of a table, its columns and its measures, one row per language and field."""

import uuid

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lakana_core.models.base import Base, EditTrailMixin
from lakana_core.models.table import Table


class TextTranslation(Base, EditTrailMixin):
    """One text of a table in one language other than its main one.

    A deploy writes only `text_project`, from the project's translation files; an edit through
    the API only `text_edited`, which is shown when set. A row with neither is deleted. The
    table's `text_languages` lists the languages that have rows, so a table without any is never
    read for them.

    Attributes
    ----------
    id : uuid.UUID
        Unique identifier for this row.
    table_id : uuid.UUID
        The table; the row goes with the table.
    subject : str
        ``table``, ``column`` or ``measure``.
    name : str
        ``""`` for the table, else the column's name.
    language : str
        The canonical BCP 47 tag.
    field : str
        ``label``, ``description`` or ``grain``.
    text_project : str or None
        The text of the translation file.
    text_edited : str or None
        The text set through the API; never for a measure or a grain.
    updated_by : uuid.UUID or None
        Who last set or reset the edit; `None` once they are deleted.
    updated_at : datetime or None
        When the edit was last set or reset.
    """

    __tablename__ = "text_translations"
    __table_args__ = (UniqueConstraint("table_id", "subject", "name", "language", "field"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    table_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tables.id", ondelete="CASCADE"))
    subject: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(1024))
    language: Mapped[str] = mapped_column(String(35))
    field: Mapped[str] = mapped_column(String(16))
    text_project: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    text_edited: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)

    table: Mapped[Table] = relationship()
    """Declared so that a new table is inserted before its translations in one commit."""
