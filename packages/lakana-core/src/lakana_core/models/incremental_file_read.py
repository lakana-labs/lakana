"""The record of the files an incremental `files.read` handed to an asset (#344)."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, UTCDateTime


class IncrementalFileRead(Base):
    """One file a run of an incremental asset read, recorded with the snapshot it committed.

    `lakana_core.catalog.files_read` adds the rows in the transaction of the commit, deletes those
    of a snapshot a check sets back, and prunes those older than the watermark less 30 days.

    Attributes
    ----------
    id : uuid.UUID
        Unique identifier.
    table_id : uuid.UUID
        The target table; the rows go with it.
    connection : str
        The connection the file was read from.
    name : str
        Its path relative to the connection.
    size : int
        Its size in bytes, as listed.
    modified : datetime.datetime
        The time `files.read` used for it (its modification time, or the date in its name).
    etag : str or None
        Its ETag, where the store reports one.
    snapshot_id : int
        The snapshot whose commit recorded it.
    read_at : datetime.datetime
        When, by the database's clock.
    """

    __tablename__ = "incremental_files_read"
    __table_args__ = (
        Index(
            "ix_incremental_files_read_table_id_connection_modified",
            "table_id",
            "connection",
            "modified",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    table_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tables.id", ondelete="CASCADE"))
    connection: Mapped[str] = mapped_column(String(63))
    name: Mapped[str] = mapped_column(Text)
    size: Mapped[int] = mapped_column(BigInteger)
    modified: Mapped[datetime] = mapped_column(UTCDateTime)
    etag: Mapped[str | None] = mapped_column(String(512), nullable=True)
    snapshot_id: Mapped[int] = mapped_column(BigInteger)
    read_at: Mapped[datetime] = mapped_column(UTCDateTime)
