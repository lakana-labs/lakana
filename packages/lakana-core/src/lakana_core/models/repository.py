"""The repository model: a git repository registered once, referenced by name."""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, TimestampMixin


class RepositoryKind(enum.StrEnum):
    """How the worker reaches a repository.

    The database stores the member *names*; the lower-case values are the
    REST API's spelling.
    """

    LOCAL = "local"
    HTTPS = "https"
    SSH = "ssh"


class Repository(Base, TimestampMixin):
    """A git repository deployers can name, with the credential an admin bound to it.

    Instance-wide: one row serves every environment. The credential is a
    secret *name*, resolved in the target environment's secret scope at
    deploy time, so prod and dev may hold different tokens under the same
    name. Deleting a repository cascades to its deploy runs, project versions and
    pointers; `RepositoryService.delete` refuses while any pointer exists,
    and the audit log keeps the history.
    """

    __tablename__ = "repositories"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    kind: Mapped[RepositoryKind] = mapped_column(
        SAEnum(RepositoryKind, native_enum=False, length=8)
    )
    url: Mapped[str] = mapped_column(Text)
    credential_secret: Mapped[str | None] = mapped_column(String(255), nullable=True)
    credential_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ssh_known_hosts: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="SET NULL"), nullable=True
    )
