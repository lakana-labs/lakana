"""The grants of `lakana login` through a browser: one row per loopback or device sign-in.

Every secret of a grant is stored only as its SHA-256 digest
(`lakana_core.identity.tokens.hash_secret`): the loopback code, the device code and the user
code. The PKCE challenge is public and stored as it is. A grant works once: `used_at` is set by
the one conditional ``UPDATE`` that hands its token out.
"""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, UTCDateTime


class CliGrant(Base):
    """One command-line sign-in through a browser, from its start to its token.

    A loopback grant is created approved, by the browser's confirmation, with its code and the
    command line's PKCE challenge. A device grant is created by the command line, pending, with
    its device and user codes, and approved or denied in a browser. Expired is
    `expires_at <= now`.
    """

    __tablename__ = "cli_sign_in_grants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    """``loopback`` or ``device``."""
    host: Mapped[str] = mapped_column(String(64))
    """The command line's computer, cleaned (`lakana_core.identity.cli_grants.clean_host`)."""
    code_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    code_challenge: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """The S256 PKCE challenge of a loopback grant: public, so stored as it is."""
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """The loopback port the browser was sent back to."""
    device_code_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    user_code_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    principal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="CASCADE"), nullable=True, index=True
    )
    """The person who approved; `None` while a device grant waits."""
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("browser_sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    """The browser session that approved; the token is issued only while it lasts."""
    requested_from: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """The client address that started a device grant, shown on its confirmation."""
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    denied_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    pat_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("personal_access_tokens.id", ondelete="SET NULL"), nullable=True
    )
    """The token the grant was exchanged for."""
