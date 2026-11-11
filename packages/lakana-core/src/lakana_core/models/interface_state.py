"""The web interface's state per person: the tour, Get started, the update note, recently opened."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, String, UniqueConstraint, Uuid, false
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, UTCDateTime, utcnow


class InterfaceState(Base):
    """What the web interface remembers for one person.

    No row means the defaults: the tour not seen, Get started shown, no update note seen
    (`lakana_core.identity.interface_state.InterfaceStateService`). Not audited: a person's own
    display state, which changes nothing anyone else sees.

    Attributes
    ----------
    principal_id : uuid.UUID
        The person; the row goes with the principal.
    tour_seen_at : datetime.datetime or None
        When the person first finished or skipped the tour; `None` while they have not.
    get_started_hidden : bool
        Whether the person hid the Get started list on Home.
    update_note_seen : str or None
        The last release (``x.y.z``) whose update note the person saw; `None` for none.
    updated_at : datetime.datetime
        When the row last changed.
    """

    __tablename__ = "interface_states"

    principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("principals.id", ondelete="CASCADE"), primary_key=True
    )
    tour_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    get_started_hidden: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    update_note_seen: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class RecentOpen(Base):
    """An object a person opened in the web interface, for Home's Recently opened.

    One row per person, kind, environment and object; opening it again moves its time
    (`lakana_core.home.recent.RecentService`), and only the newest `RECENT_KEEP` are kept.

    Attributes
    ----------
    id : uuid.UUID
        Unique identifier for this row.
    principal_id : uuid.UUID
        The person; the rows go with the principal.
    kind : str
        ``table``, ``query``, ``job`` or ``run``.
    environment_id : uuid.UUID
        The object's environment; the rows go with the environment.
    ref : str
        The object: a dotted table name, a query's or a run's id, or ``repository/job``.
    opened_at : datetime.datetime
        When the person last opened it.
    """

    __tablename__ = "recent_opens"
    __table_args__ = (
        UniqueConstraint("principal_id", "kind", "environment_id", "ref"),
        # The newest entries of one person, for the list and the trim to the newest twenty.
        Index("ix_recent_opens_principal_id_opened_at", "principal_id", "opened_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(8))
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE")
    )
    ref: Mapped[str] = mapped_column(String(1024))
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime)
