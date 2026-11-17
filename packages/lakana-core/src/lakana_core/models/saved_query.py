"""A saved query: SQL text a person keeps under a name in one environment."""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, TimestampMixin


class SavedQueryVisibility(enum.StrEnum):
    """Who sees a saved query besides its owner and the instance administrators.

    `PRIVATE`: nobody else. `EVERYONE`: everyone who may submit a query in its environment.
    """

    PRIVATE = "private"
    EVERYONE = "everyone"


class SavedQuery(Base, TimestampMixin):
    """SQL text kept under a name, in one environment, by its owner.

    Only text and an environment: running it is an ordinary query submitted by whoever runs it,
    with their own rights (`lakana_core.catalog.saved_queries`).

    The owner is held twice. `owner_id` sets `NULL` when the owner's principal row is deleted,
    which keeps a shared query (``former owner``); `private_owner_id` is the owner of a private
    query only and cascades, so the same deletion removes the private ones. The check
    `private_owner` keeps the two in step.

    Attributes
    ----------
    id : uuid.UUID
        The saved query's id.
    environment_id : uuid.UUID
        Its environment, fixed at creation; the query goes when the environment is purged.
    name : str
        Its name: one line of 1 to 200 visible characters, unique per environment and owner.
    sql_text : str
        The SQL, stored as sent.
    visibility : str
        A `SavedQueryVisibility` value.
    owner_id : uuid.UUID or None
        The owner; `NULL` once the owner's principal row is deleted.
    private_owner_id : uuid.UUID or None
        The owner while the query is private; `NULL` for a shared one.
    """

    __tablename__ = "saved_queries"
    __table_args__ = (
        UniqueConstraint(
            "environment_id", "owner_id", "name", name="uq_saved_queries_environment_owner_name"
        ),
        CheckConstraint(
            "(visibility = 'private' AND private_owner_id IS NOT NULL "
            "AND private_owner_id = owner_id) "
            "OR (visibility = 'everyone' AND private_owner_id IS NULL)",
            name="private_owner",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    sql_text: Mapped[str] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(String(16))
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="SET NULL"), nullable=True, index=True
    )
    private_owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="CASCADE"), nullable=True, index=True
    )
