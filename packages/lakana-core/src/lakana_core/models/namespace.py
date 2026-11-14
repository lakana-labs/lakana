"""The namespace model."""

import uuid

from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base

NAMESPACE_PATH_MAX = 1024
"""The longest `Namespace.path` the catalog stores, in characters."""


class Namespace(Base):
    """A multi-level namespace within an environment.

    ``path`` holds the unit-separator-joined form, which makes prefix queries
    for child namespaces portable across SQLite and Postgres. ``owner_id``
    cascades to ``NULL`` when the owning principal is deleted, rather than
    cascading the delete: a principal leaving must not delete the namespaces
    they owned.
    """

    __tablename__ = "namespaces"
    __table_args__ = (UniqueConstraint("environment_id", "path"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE")
    )
    path: Mapped[str] = mapped_column(String(NAMESPACE_PATH_MAX))
    properties: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="SET NULL"), nullable=True
    )
