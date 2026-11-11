"""The secret model."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, UTCDateTime


class SecretUseKind(enum.StrEnum):
    """What a secret may be used for.

    The database stores the member *names*; the lower-case values are the REST API's spelling.
    A secret with no use is unbound: the worker uses it for its environment's storage and
    webhooks, and for nothing else until an administrator binds it.
    """

    PACKAGE_INDEX = "package_index"
    REPOSITORY = "repository"
    ENVIRONMENT = "environment"
    CONNECTION = "connection"
    """A connection of its environment."""


class Secret(Base):
    """One envelope-encrypted secret, scoped to a single environment.

    The plaintext is never stored: `ciphertext` holds a JSON-serialized
    key-value bag encrypted under a random per-secret data key
    (AES-256-GCM); `wrapped_data_key` holds that data key wrapped by the
    deployment's master key (`lakana_core.secrets.keys.KeyProvider`);
    `nonce` is the 12-byte nonce used for the payload encryption (the
    data-key-wrapping step has its own, separately-generated nonce, kept
    inside `wrapped_data_key`'s own encoding — see
    `lakana_core.secrets.crypto`). Decrypting a `Secret` row requires the
    live `KeyProvider`; the row alone is not enough, by design.

    `format_version` says how the row was encrypted (1: no associated
    data; 2: both layers bound to the row, see
    `lakana_core.secrets.crypto`) and `key_id` names the provider and the
    key that wrapped the data key (`KeyProvider.key_id`). Rows from before
    these columns existed are format 1 with the bare key id ``env``.

    `name` is unique per `environment_id`, not globally — two different
    environments may each have a secret of the same name.

    `use_kind` and `use_target_id` say what the secret is for: a package index, a
    repository or a connection of its environment (by id), or its environment (storage,
    webhooks, tasks). A binding to an index or repository that is later deleted keeps
    pointing at the gone id, so it never becomes unbound by itself; deleting a connection
    unbinds its secrets (`lakana_core.connections.service`).

    Soft-deleted on revoke (`revoked_by`/`revoked_at`), matching every
    other model's convention: the encrypted blob is harmless at rest
    even after revocation, and the row has real audit value once the
    audit log lands.
    """

    __tablename__ = "secrets"
    __table_args__ = (
        UniqueConstraint("environment_id", "name", name="uq_secrets_environment_name"),
        CheckConstraint(
            "(use_kind IS NULL AND use_target_id IS NULL)"
            " OR (use_kind IS NOT NULL AND use_kind = 'ENVIRONMENT' AND use_target_id IS NULL)"
            " OR (use_kind IS NOT NULL"
            " AND use_kind IN ('PACKAGE_INDEX', 'REPOSITORY', 'CONNECTION')"
            " AND use_target_id IS NOT NULL)",
            name="use_consistent",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    wrapped_data_key: Mapped[bytes] = mapped_column(LargeBinary)
    format_version: Mapped[int] = mapped_column(Integer)
    key_id: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("principals.id"), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    use_kind: Mapped[SecretUseKind | None] = mapped_column(
        SAEnum(SecretUseKind, native_enum=False, length=16), nullable=True
    )
    use_target_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    generated_for: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    """The repository a deploy key Lakana made is for (`lakana_core.projects.deploy_keys`);
    `None` for a secret a person stored. Such a key is never bound to another use or rotated by
    hand. Like `use_target_id`, it keeps the id of a repository that is removed."""
    public_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The public half of a deploy key Lakana made (`ssh-ed25519 <base64> <comment>`), to add at
    the git host; `None` for any other secret. Never secret: the private half is in the
    ciphertext."""
