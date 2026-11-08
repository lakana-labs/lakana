"""The PAT and task-token models.

Neither stores a plaintext secret — only `secret_hash`, a SHA-256 digest.
`key_id` is the indexed lookup column that finds a row without hashing or
scanning every stored credential; verification then hashes the presented
secret and compares it, in constant time, against the stored hash for that
one row (`lakana_core.identity.tokens.verify_secret`).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, CheckConstraint, ForeignKey, Integer, String, Uuid, false
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, UTCDateTime


class PersonalAccessToken(Base):
    """One issued PAT for a principal.

    `principal_id` cascades to delete: an orphaned credential with no
    principal to authenticate as can never be used, unlike `QueryRun`,
    whose `principal_id` is kept as `NULL` because the run itself still has
    audit value without its author.
    """

    __tablename__ = "personal_access_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id", ondelete="CASCADE"))
    key_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    secret_hash: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=lambda: datetime.now(UTC))
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    bootstrap: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    """Whether this is the operator's token: the one `lakana_core.bootstrap.bootstrap_if_needed`
    minted. Set by nothing else (the REST schema has no such field); migration 0069 marks an
    earlier unrevoked PAT named ``bootstrap`` of the bootstrap principal."""


class TaskToken(Base):
    """One minted task token, scoped to one environment and declared assets.

    Unlike a PAT, `expires_at` is required — a task token is never
    unbounded. For a token minted through the API, `asset_scope` and
    `environment` are enforced by the grant authorization
    (`GrantAuthorization._check_task_scope`): the token may act only in its
    environment and on assets its patterns declare.

    A token with `task_run_id` and `claim` set is **bound**: the
    worker minted it for one attempt of one Python task, and it is valid
    only while that task run is `running` with that claim counter
    (`resolve_task_token_scope`). Its readable inputs come from the
    project version manifest and its writes are limited to its own asset
    (`GrantAuthorization._check_bound_scope`), not from `asset_scope`. A
    REST-minted token has neither.
    """

    __tablename__ = "task_tokens"
    __table_args__ = (
        CheckConstraint("(task_run_id IS NULL) = (claim IS NULL)", name="bound_pair"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id", ondelete="CASCADE"))
    environment: Mapped[str] = mapped_column(String(255))
    asset_scope: Mapped[list] = mapped_column(JSON)
    key_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    secret_hash: Mapped[str] = mapped_column(String(64))
    issued_at: Mapped[datetime] = mapped_column(UTCDateTime, default=lambda: datetime.now(UTC))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    task_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    claim: Mapped[int | None] = mapped_column(Integer, nullable=True)
