"""The OIDC sign-in's flow state: one row per authorization request, used once.

`state` and the browser's flow-cookie secret are stored only as their SHA-256 digest
(`lakana_core.identity.tokens.hash_secret`). The ``nonce`` and the PKCE ``code_verifier`` are
stored as they are: the server sends them to the provider, they live 10 minutes and they work
once.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, UTCDateTime


class OidcFlow(Base):
    """One OIDC authorization request, from `start` to `finish`.

    Called back and finished are two separate facts (`called_back_at`, `finished_at`); expired is
    `expires_at <= now`. `outcome` is written by the callback: ``{"ok": true, "iss", "sub",
    "email", "name", "auth_time"}`` or ``{"ok": false, "reason", ...}``.
    """

    __tablename__ = "oidc_flows"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    browser_hash: Mapped[str] = mapped_column(String(64), unique=True)
    nonce: Mapped[str] = mapped_column(String(64))
    code_verifier: Mapped[str] = mapped_column(String(128))
    purpose: Mapped[str] = mapped_column(String(16))
    """``sign_in``."""
    return_to: Mapped[str] = mapped_column(String(2048))
    """The app path the person wanted, checked (`safe_return_to`)."""
    principal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="CASCADE"), nullable=True, index=True
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("browser_sessions.id", ondelete="CASCADE"), nullable=True, index=True
    )
    link_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sign_in_links.id", ondelete="CASCADE"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    called_back_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    outcome: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
