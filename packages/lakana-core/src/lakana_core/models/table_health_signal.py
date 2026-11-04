"""The table health signal model."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, UTCDateTime


class TableHealthSignal(Base):
    """One observation of one health signal of a table (#340).

    A commit adds one row per signal and column (`lakana_core.catalog.health_record`), the
    scheduler's sweep one per change of a table's freshness, and the sweep deletes the rows older
    than `LAKANA_HEALTH__RETENTION_DAYS` except the newest of each signal and column.

    Attributes
    ----------
    id : uuid.UUID
        Unique identifier.
    environment_id : uuid.UUID
        The table's environment; the rows go with it.
    table_id : uuid.UUID
        The table; the rows go with it.
    signal : str
        ``freshness``, ``volume``, ``null_rate``, ``range`` or ``schema``.
    column_name : str or None
        The column of a ``null_rate`` or ``range`` observation; `None` for the others and for a
        signal that is off for the whole table.
    snapshot_id : int or None
        The snapshot observed; for ``freshness``, the table's last snapshot.
    observed : dict or None
        What was observed (`docs/site/reference/table-health.md` lists the keys per signal).
    band : dict or None
        The usual values it was compared with; `None` without a band.
    state : str
        ``ok``, ``fired``, ``no_history``, ``no_metrics`` or ``off``.
    effect : str
        ``warn``, ``block`` or ``off``, as the table's `health:` said when it was observed.
    observed_at : datetime.datetime
        When, by the database's clock.
    """

    __tablename__ = "table_health_signals"
    __table_args__ = (
        Index("ix_table_health_signals_table_id_observed_at", "table_id", "observed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE")
    )
    table_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tables.id", ondelete="CASCADE"))
    signal: Mapped[str] = mapped_column(String(16))
    column_name: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    snapshot_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    observed: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    band: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    state: Mapped[str] = mapped_column(String(16))
    effect: Mapped[str] = mapped_column(String(8))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)
