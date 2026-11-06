"""The models of access as code: the scopes a file manages, and the overrides of them (#46)."""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, UTCDateTime


class AccessManagedScope(Base):
    """A scope that an access file manages.

    Attributes
    ----------
    scope : str
        The scope's name; the primary key.
    file_sha256 : str
        The SHA-256 of the file that last applied the scope.
    file_name : str
        The name of that file.
    source_commit : str or None
        The commit the file came from, when it is known.
    apply_id : uuid.UUID
        The apply that last changed the scope; its audit events carry the same id.
    applied_by : uuid.UUID or None
        The principal who applied it; `None` once that principal is deleted.
    applied_at : datetime
        When the scope was last applied.
    """

    __tablename__ = "access_managed_scopes"

    scope: Mapped[str] = mapped_column(String(255), primary_key=True)
    file_sha256: Mapped[str] = mapped_column(String(64))
    file_name: Mapped[str] = mapped_column(String(255))
    source_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    apply_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    applied_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("principals.id", ondelete="SET NULL"), nullable=True
    )
    applied_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AccessOverride(Base):
    """A change made by hand inside a managed scope, with the reason given for it.

    Attributes
    ----------
    id : uuid.UUID
        Unique identifier.
    scope : str
        The managed scope the change was made in.
    change : str
        What was changed.
    reason : str
        Why it was changed.
    principal_id : uuid.UUID
        Who changed it.
    created_at : datetime
        When.
    audit_event_id : uuid.UUID
        The audit event that recorded it.
    resolved_at : datetime or None
        When an apply took the change into account; `None` while it stands.
    resolved_by_apply : uuid.UUID or None
        The apply that did.
    """

    __tablename__ = "access_overrides"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    scope: Mapped[str] = mapped_column(String(255), index=True)
    change: Mapped[str] = mapped_column(String(255))
    reason: Mapped[str] = mapped_column(Text)
    principal_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("principals.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    audit_event_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    resolved_by_apply: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
