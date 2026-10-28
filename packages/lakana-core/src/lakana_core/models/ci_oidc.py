"""The CI trust rules, the Lakana tokens a CI job receives, and the ID tokens already used.

A CI job (GitHub Actions, GitLab CI) exchanges its OIDC ID token for a `CiToken`
(`lakana_core.ci_oidc.exchange`) when exactly one enabled `CiTrustRule` matches the token's
claims. The token allows one deploy or dry run of one repository in one environment, as the
rule's service account. `CiOidcJti` keeps each exchanged ID token's id until the ID token
expires, so that each ID token is exchanged once.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Uuid, false
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, UTCDateTime, utcnow


class CiTrustRule(Base):
    """Which CI jobs may deploy one repository to one environment, as which service account.

    Bound to the ids of the environment, repository and principal, never their names, so a
    rename or a drop-and-recreate under the same name does not inherit the rule. The issuer,
    environment and repository never change after creation; every other change (and enabling
    or disabling) raises `revision`, which ends every token issued under the rule before.
    """

    __tablename__ = "ci_trust_rules"
    __table_args__ = (Index(None, "issuer", "environment_id", "repository_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    issuer: Mapped[str] = mapped_column(String(255))
    """The issuer URL exactly as configured in `LAKANA_CI_OIDC__ISSUERS`."""
    principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("principals.id", ondelete="CASCADE"), index=True
    )
    """The service account the CI job acts as; it still needs its own `DEPLOY` grant."""
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE")
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), index=True
    )
    kinds: Mapped[list] = mapped_column(JSON)
    """``deploy`` and/or ``dry_run``, sorted; never a rollback or an undeploy."""
    conditions: Mapped[dict] = mapped_column(JSON)
    """Claim name to the exact text the claim must have."""
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    start_on_deploy_jobs: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    """Whether a deploy accepted under the rule starts the deployed project's jobs with
    ``on_deploy: true`` in its environment, as the CI identity; nothing else. Off by default."""
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class CiToken(Base):
    """A Lakana token issued for one CI ID token: one deploy or dry run, then only its own run.

    Stores the secret's SHA-256 only, like a PAT. Valid while it has not expired and its rule
    still exists, is enabled and has the revision it was issued under, and its principal is
    active (`lakana_core.ci_oidc.exchange.resolve_ci_scope`, at every authentication).
    `used_at` is set by the one submission it may make, in the transaction that queues it.
    """

    __tablename__ = "ci_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    key_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    secret_hash: Mapped[str] = mapped_column(String(64))
    rule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ci_trust_rules.id", ondelete="SET NULL"), nullable=True, index=True
    )
    rule_revision: Mapped[int] = mapped_column(Integer)
    principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("principals.id", ondelete="CASCADE"), index=True
    )
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE"), index=True
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), index=True
    )
    kinds: Mapped[list] = mapped_column(JSON)
    sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    """The signed ``sha`` claim: the only commit a deploy with this token may deploy."""
    jti_sha256: Mapped[str] = mapped_column(String(64))
    issued_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class CiOidcJti(Base):
    """One exchanged ID token's id, kept until the ID token expires, then swept.

    The key is the SHA-256 of the issuer and the ``jti``, so the column has a fixed size
    whatever the issuer writes. Inserting it is the single-use check: a second exchange of the
    same ID token fails on the primary key.
    """

    __tablename__ = "ci_oidc_jtis"

    jti_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
