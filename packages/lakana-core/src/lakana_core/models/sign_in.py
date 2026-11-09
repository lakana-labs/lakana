"""The sign-in models: links, browser sessions, passkeys, challenges, notices and settings.

No row stores a secret: a link's secret and a session's secret are kept only as their SHA-256
digest (`lakana_core.identity.tokens.hash_secret`) and compared in constant time; so is a
session's CSRF token, a second random secret (`BrowserSession.csrf_hash`). The public halves
(`SignInLink.public_id`, `BrowserSession.key_id`) are the indexed lookup keys.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, UTCDateTime


class SignInLink(Base):
    """One single-use sign-in link: setup, invite, recovery or local.

    Bound to one principal; an open invite has none until it is accepted and reserves its
    name in `reserved_name` (unique) while open. Used, withdrawn and expired are three
    separate facts: `used_at`, `withdrawn_at`, and `expires_at <= now`.
    """

    __tablename__ = "sign_in_links"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True)
    secret_hash: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    """``setup``, ``invite``, ``recovery`` or ``local``."""
    principal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="CASCADE"), nullable=True, index=True
    )
    invite_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    invite_display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reserved_name: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    issued_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="SET NULL"), nullable=True
    )
    source_pat_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("personal_access_tokens.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    withdrawn_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        # `browser_sessions.link_id` points back here: the cycle needs one constraint added
        # after both tables exist.
        ForeignKey(
            "browser_sessions.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_sign_in_links_session_id_browser_sessions",
        ),
        nullable=True,
    )
    used_peer: Mapped[str | None] = mapped_column(String(64), nullable=True)
    used_user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)


class BrowserSession(Base):
    """One browser session: the interface's credential, sent as an HttpOnly cookie.

    The cookie is ``<key_id>_<secret>``; only `secret_hash` is stored. The page holds a second,
    unrelated secret, the CSRF token, which every cookie request must carry; only `csrf_hash` is
    stored. `idle_seconds` and
    `absolute_expires_at` are fixed when the session starts, so changing the instance's limits
    affects new sessions only. An ended session keeps its row with `ended_at` and `end_reason`.
    """

    __tablename__ = "browser_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    key_id: Mapped[str] = mapped_column(String(32), unique=True)
    secret_hash: Mapped[str] = mapped_column(String(64))
    csrf_hash: Mapped[str] = mapped_column(String(64))
    """The SHA-256 of the session's CSRF token: random, never derived from the cookie, returned
    only by the sign-in that started the session."""
    principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("principals.id", ondelete="CASCADE"), index=True
    )
    via: Mapped[str] = mapped_column(String(16))
    """``setup``, ``invite``, ``recovery``, ``local`` or ``passkey``."""
    link_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sign_in_links.id", ondelete="SET NULL"), nullable=True
    )
    source_pat_id: Mapped[uuid.UUID | None] = mapped_column(
        # Deleting the token deletes the sessions made through it, so none outlives it.
        ForeignKey("personal_access_tokens.id", ondelete="CASCADE"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime)
    idle_seconds: Mapped[int] = mapped_column(Integer)
    absolute_expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_touch_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    end_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    peer: Mapped[str] = mapped_column(String(64))
    user_agent: Mapped[str] = mapped_column(String(255))


class Passkey(Base):
    """One registered WebAuthn credential of a person.

    `credential_id` is unique across the instance (a stored id is refused at registration);
    a decreasing signature counter disables the passkey (`disabled_at`, `disabled_reason`).
    """

    __tablename__ = "passkeys"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("principals.id", ondelete="CASCADE"), index=True
    )
    credential_id: Mapped[str] = mapped_column(String(1366), unique=True)
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    sign_count: Mapped[int] = mapped_column(Integer)
    rp_id: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(100))
    backup_eligible: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("browser_sessions.id", ondelete="SET NULL"), nullable=True
    )
    created_user_agent: Mapped[str] = mapped_column(String(255))
    disabled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    disabled_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)


class WebauthnChallenge(Base):
    """One WebAuthn challenge: 32 random bytes, single use, short-lived."""

    __tablename__ = "webauthn_challenges"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    challenge: Mapped[bytes] = mapped_column(LargeBinary(32))
    purpose: Mapped[str] = mapped_column(String(16))
    """``register``, ``sign_in`` or ``step_up``."""
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("browser_sessions.id", ondelete="CASCADE"), nullable=True
    )
    principal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="CASCADE"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class SignInNotice(Base):
    """One notice for a person about a sign-in event (e.g. a passkey disabled as a clone)."""

    __tablename__ = "sign_in_notices"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    recipient_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("principals.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    subject_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="SET NULL"), nullable=True
    )
    detail: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    dismissed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class InstanceSetting(Base):
    """One instance-wide setting an administrator changes at run time (e.g. session limits).

    Keys are dotted names such as ``sign_in.idle_seconds``; a missing key means the default.
    """

    __tablename__ = "instance_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[object] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="SET NULL"), nullable=True
    )
