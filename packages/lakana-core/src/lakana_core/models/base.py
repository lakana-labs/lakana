"""Declarative base and shared column types."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, MetaData
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    # column_0_N_name joins every column's name, not just the first, so two
    # multi-column unique constraints sharing a leading column (as
    # namespaces' and tables' do not, but could) generate distinct names
    # instead of colliding.
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
"""Deterministic constraint names, so Alembic can alter them by name later."""


def utcnow() -> datetime:
    """Return the current UTC time, the one clock the models and services share.

    Returns
    -------
    datetime
        An aware UTC timestamp.
    """
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for every lakana-core model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UTCDateTime(TypeDecorator[datetime]):
    """A timezone-aware timestamp that stays aware on every backend.

    SQLite does not preserve the timezone flag on a ``DateTime(timezone=True)``
    column: a value written as UTC-aware reads back naive there, while Postgres
    returns the same column aware. Left alone, this makes identical application
    code behave differently depending on which database it happens to run
    against, which is exactly the kind of divergence Lakana cannot have between
    development (SQLite) and production (Postgres). This type normalises every
    value to UTC on the way in and reattaches ``UTC`` on the way out, so both
    backends behave identically.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """Normalise an outgoing value to an aware UTC datetime.

        Parameters
        ----------
        value
            The datetime being written, or ``None``.
        dialect
            The dialect the value is being written for. Unused: normalisation
            is the same for every backend.

        Returns
        -------
        datetime or None
            ``value`` converted to UTC, or ``None`` unchanged.

        Raises
        ------
        ValueError
            If ``value`` is naive (has no ``tzinfo``). A naive value has no
            defined offset, so it cannot be converted to UTC without silently
            guessing the caller's intent; every writer in this codebase already
            produces aware values via ``datetime.now(UTC)``, so this should
            never fire outside a programming error.
        """
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("UTCDateTime requires a timezone-aware datetime; got a naive one.")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """Reattach the UTC timezone to an incoming value.

        Parameters
        ----------
        value
            The datetime read back from the database, or ``None``. SQLite
            returns this naive; Postgres returns it already aware (and
            already in UTC, since :meth:`process_bind_param` only ever writes
            UTC).
        dialect
            The dialect the value was read from. Unused: both backends store
            UTC, so reattaching ``UTC`` is correct either way.

        Returns
        -------
        datetime or None
            ``value`` with ``tzinfo`` set to :data:`~datetime.UTC`, or
            ``None`` unchanged.
        """
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class TimestampMixin:
    """Adds creation and update timestamps to a model."""

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class EditTrailMixin:
    """Adds who last set or reset an edit through the API, and when.

    Attributes
    ----------
    updated_by : uuid.UUID or None
        The principal who last set or reset the edit; `None` once they are deleted.
    updated_at : datetime or None
        When the edit was last set or reset.
    """

    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="SET NULL"), nullable=True, default=None
    )
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, default=None)
