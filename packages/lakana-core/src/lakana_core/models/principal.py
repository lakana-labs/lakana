"""The principal model."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, Enum, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, TimestampMixin, UTCDateTime


class PrincipalType(enum.StrEnum):
    """What kind of actor a principal represents."""

    USER = "USER"
    SERVICE_ACCOUNT = "SERVICE_ACCOUNT"


class Principal(Base, TimestampMixin):
    """An actor that can be authorized.

    M1 creates only the bootstrap admin; M3 adds real authentication, groups and credentials.
    Deactivating a principal (is_active=False) revokes its access without deleting the row,
    preserving every grant, PAT, and audit entry that references it.
    """

    __tablename__ = "principals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    type: Mapped[PrincipalType] = mapped_column(Enum(PrincipalType, native_enum=False))
    name: Mapped[str] = mapped_column(String(255), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    language: Mapped[str | None] = mapped_column(String(35), nullable=True, default=None)
    """The language this principal reads catalog texts in (a canonical BCP 47 tag), or `None`
    to follow the request's `Accept-Language` and then the main language."""
    query_passed_by_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    """Until when workers pass this principal's ad-hoc queries by, written by a worker whose
    claim timed out on the principal's cap lock (`pools.pass_by`). `None` when none was. Read by
    the wait reason (`waiting.LOCK_TIMEOUT`); it ends by time only, and a past time means
    nothing."""
