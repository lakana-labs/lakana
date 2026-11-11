"""A person's profile picture, stored as the re-encoded WebP the interface shows."""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, UTCDateTime, utcnow


class ProfilePicture(Base):
    """One person's profile picture.

    Only the re-encoded picture is kept (`lakana_core.identity.pictures.reencode_picture`): a
    256 x 256 WebP without metadata, a few kilobytes, never the uploaded file. No row means no
    picture; the interface shows the person's initials.

    Attributes
    ----------
    principal_id : uuid.UUID
        The person; the picture goes with the principal.
    image : bytes
        The WebP file.
    etag : str
        The SHA-256 of `image` in hex: the `ETag` it is served with, and the `picture` field of
        a principal and of whoami.
    updated_at : datetime.datetime
        When the picture was last set.
    """

    __tablename__ = "profile_pictures"

    principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("principals.id", ondelete="CASCADE"), primary_key=True
    )
    image: Mapped[bytes] = mapped_column(LargeBinary)
    etag: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
