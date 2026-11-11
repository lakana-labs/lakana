"""Profile pictures: an uploaded image, re-encoded, and who may set, see and remove one.

An upload is untrusted bytes handed to native decoders, so the decode is fenced in: only JPEG,
PNG and WebP are read, chosen by the media type and confirmed by the bytes themselves (the
filename is never seen); the declared size is checked before a pixel is decoded; and what is
stored is a new 256 x 256 WebP drawn from the pixels alone, so no EXIF (GPS, camera), XMP, ICC
profile, comment, trailing data or second frame of the upload survives.

Setting a picture is self-service and audited; an administrator (`MANAGE_PRINCIPAL` on the
instance) may remove another person's. Every signed-in person with access sees every picture,
and a person without any grant yet only their own. A task or CI token does none of this: a
picture belongs to a person.
"""

from __future__ import annotations

import hashlib
import io
import struct
import uuid
import warnings
from collections.abc import Iterable

from PIL import Image, ImageOps
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.errors import NotFoundError, PermissionDenied, UnsupportedPicture, ValidationError
from lakana_core.grants.service import GrantService
from lakana_core.identity.interface_state import refuse_tokens, require_self
from lakana_core.identity.sessions import utc_now
from lakana_core.models import AuditEventType, Principal, ProfilePicture

PICTURE_MAX_BYTES = 5 * 1024 * 1024
"""The largest upload read, 5 MiB; the server refuses a longer body with 413 while it arrives."""

PICTURE_MAX_PIXELS = 40_000_000
"""The most pixels an upload may declare (40 megapixels; a phone photo has 12 to 50)."""

PICTURE_SIZE = 256
"""The side of the stored square picture, in pixels."""

MEDIA_TYPES = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}
"""The media types read, each with the only Pillow format its bytes are opened as."""

_QUALITY = 85
"""WebP quality of the stored picture."""

_WORKING_SIDE = 2 * PICTURE_SIZE
"""A large picture is first reduced by a whole factor to no less than this on its shorter side."""

_UNSUPPORTED = "Use a JPEG, PNG or WebP image. SVG and other formats are refused."
_NOT_READABLE = (
    "This file is not a JPEG, PNG or WebP image Lakana can read. Save it again as JPEG or PNG "
    "and try once more."
)
_HIDDEN = "Pictures are shown to people who have access."

_PILLOW_FAILURES: tuple[type[BaseException], ...] = (
    OSError,
    ValueError,
    EOFError,
    SyntaxError,
    struct.error,
    Image.DecompressionBombError,
    Image.DecompressionBombWarning,
)
"""What Pillow raises for a file it cannot or will not read (``struct.error``: a short header)."""

# Pillow's own bomb guard at the pictures' limit: it warns above it and raises above twice it.
# The warning is an error here, and `_reencode` checks the size itself before any decode, so
# the refusal does not depend on the process's warning filters.
Image.MAX_IMAGE_PIXELS = PICTURE_MAX_PIXELS
warnings.filterwarnings("error", category=Image.DecompressionBombWarning)


def reencode_picture(data: bytes, media_type: str) -> bytes:
    """Turn an uploaded image into the stored picture: a 256 x 256 WebP of its pixels only.

    The bytes are opened as the format `media_type` names and no other, so an SVG, HTML or a
    GIF sent as ``image/png`` is refused; the declared width x height is checked against
    `PICTURE_MAX_PIXELS` before anything is decoded; a JPEG is decoded at a reduced scale; only
    the first frame of an animation is read; the EXIF orientation is applied; the square is
    cut from the centre; and the result is a new image, saved without EXIF, XMP or ICC.

    Parameters
    ----------
    data
        The uploaded file, at most `PICTURE_MAX_BYTES` (the caller enforces it).
    media_type
        The upload's media type, without parameters: a key of `MEDIA_TYPES`.

    Returns
    -------
    bytes
        The WebP file, quality 85, RGB, or RGBA for a picture with transparency.

    Raises
    ------
    UnsupportedPicture
        For a media type outside `MEDIA_TYPES` (415).
    ValidationError
        For bytes that are not a readable image of that type, are truncated, or declare more
        than `PICTURE_MAX_PIXELS` pixels (422).
    """
    pillow_format = MEDIA_TYPES.get(media_type)
    if pillow_format is None:
        raise UnsupportedPicture(_UNSUPPORTED)
    try:
        return _reencode(data, pillow_format)
    except _PILLOW_FAILURES as exc:
        raise ValidationError(_NOT_READABLE) from exc


def _reencode(data: bytes, pillow_format: str) -> bytes:
    """Decode, orient, crop and re-encode, letting Pillow's errors through.

    Parameters
    ----------
    data
        The uploaded file.
    pillow_format
        The one Pillow format to open it as.

    Returns
    -------
    bytes
        The WebP file.

    Raises
    ------
    ValidationError
        For more than `PICTURE_MAX_PIXELS` declared pixels, before any decode.
    """
    with Image.open(io.BytesIO(data), formats=[pillow_format]) as image:
        width, height = image.size
        if width * height > PICTURE_MAX_PIXELS:
            raise ValidationError(_NOT_READABLE)
        if pillow_format == "JPEG":
            image.draft("RGB", (_WORKING_SIDE, _WORKING_SIDE))
        # `Image.open` stands on the first frame; nothing seeks further. Reducing before the
        # turn keeps one full-size copy in memory at most; scaling and turning commute.
        factor = max(1, min(image.size) // _WORKING_SIDE)
        reduced = image.reduce(factor) if factor > 1 else image.copy()
        upright = ImageOps.exif_transpose(reduced)
    transparent = upright.mode in ("RGBA", "LA", "PA") or "transparency" in upright.info
    mode = "RGBA" if transparent else "RGB"
    fitted = ImageOps.fit(
        upright.convert(mode), (PICTURE_SIZE, PICTURE_SIZE), method=Image.Resampling.LANCZOS
    )
    # A new image from the pixels alone: `fit` and `convert` carry the upload's `info` (EXIF,
    # XMP, ICC profile, comments) along, and the WebP writer would save some of it.
    clean = Image.frombytes(mode, fitted.size, fitted.tobytes())
    out = io.BytesIO()
    clean.save(out, "WEBP", quality=_QUALITY)
    return out.getvalue()


def _stored(session: Session, principal_id: uuid.UUID) -> ProfilePicture | None:
    """Read a person's picture row as the database has it now.

    Parameters
    ----------
    session
        Database session.
    principal_id
        The person.

    Returns
    -------
    ProfilePicture or None
        The row, or `None` without a picture.
    """
    return session.get(ProfilePicture, principal_id, populate_existing=True)


class PictureService:
    """Set, read and remove profile pictures.

    Parameters
    ----------
    session
        Database session; `set` and `remove` commit.
    authz
        Authorization service, asked for `MANAGE_PRINCIPAL` on the instance when a caller removes
        another person's picture.
    """

    def __init__(self, session: Session, authz: AuthorizationService) -> None:
        self._session = session
        self._authz = authz

    def check_can_set(self, p: PrincipalRef, name: str, media_type: str) -> None:
        """Refuse an upload before a byte of it is read.

        Parameters
        ----------
        p
            The caller.
        name
            The person named in the request.
        media_type
            The upload's media type.

        Raises
        ------
        PermissionDenied
            For a task or CI token, or another person's name.
        UnsupportedPicture
            For a media type outside `MEDIA_TYPES`.
        """
        require_self(p, name, "profile picture")
        if media_type not in MEDIA_TYPES:
            raise UnsupportedPicture(_UNSUPPORTED)

    def set(self, p: PrincipalRef, name: str, data: bytes, media_type: str) -> str:
        """Set the caller's own picture from an uploaded file; audited.

        Parameters
        ----------
        p
            The caller.
        name
            The person; must be the caller.
        data
            The uploaded file, at most `PICTURE_MAX_BYTES`.
        media_type
            Its media type.

        Returns
        -------
        str
            The new picture's etag: the SHA-256 of the stored WebP, in hex.

        Raises
        ------
        PermissionDenied
            For a task or CI token, or another person's name.
        UnsupportedPicture
            For a media type outside `MEDIA_TYPES`.
        ValidationError
            For a file that is not a readable image of that type.
        NotFoundError
            If the caller's principal no longer exists.
        """
        self.check_can_set(p, name, media_type)
        image = reencode_picture(data, media_type)
        etag = hashlib.sha256(image).hexdigest()
        if self._session.get(Principal, p.id) is None:
            raise NotFoundError("Your principal no longer exists.")
        now = utc_now()
        row = _stored(self._session, p.id)
        if row is None:
            try:
                with self._session.begin_nested():
                    self._session.execute(
                        insert(ProfilePicture).values(
                            principal_id=p.id, image=image, etag=etag, updated_at=now
                        )
                    )
            except IntegrityError:
                # Another upload of the same person wrote the first row since this one looked;
                # this, the later one, replaces it.
                row = _stored(self._session, p.id)
        if row is not None:
            row.image, row.etag, row.updated_at = image, etag, now
        AuditService(self._session).record(
            p.id, AuditEventType.PROFILE_PICTURE_SET, detail={"principal": name}
        )
        self._session.commit()
        return etag

    def remove(self, p: PrincipalRef, name: str) -> None:
        """Remove a person's picture: one's own, or anyone's with `MANAGE_PRINCIPAL`; audited.

        Removing a picture that is not there changes and records nothing.

        Parameters
        ----------
        p
            The caller.
        name
            The person.

        Raises
        ------
        PermissionDenied
            For a task or CI token, or another person's name without `MANAGE_PRINCIPAL` on the
            instance.
        NotFoundError
            If no principal has that name.
        """
        refuse_tokens(p, "profile picture")
        if name != p.name:
            self._authz.authorize(p, Action.MANAGE_PRINCIPAL, Securable.instance_scope())
        principal_id = self._session.execute(
            select(Principal.id).where(Principal.name == name)
        ).scalar_one_or_none()
        if principal_id is None:
            raise NotFoundError(f"No principal named {name!r}.")
        row = self._session.get(ProfilePicture, principal_id)
        if row is None:
            return
        self._session.delete(row)
        detail = {"principal": name} if name == p.name else {"principal": name, "by": p.name}
        AuditService(self._session).record(
            p.id, AuditEventType.PROFILE_PICTURE_REMOVED, detail=detail
        )
        self._session.commit()

    def get(self, p: PrincipalRef, name: str) -> tuple[bytes, str]:
        """Read a person's picture.

        One's own always; anyone's for a caller with any grant (`GrantService.has_any_grant`)
        or the `ADMIN` role on the instance.

        Parameters
        ----------
        p
            The caller.
        name
            The person.

        Returns
        -------
        tuple of (bytes, str)
            The WebP file and its etag.

        Raises
        ------
        PermissionDenied
            For a task or CI token, or a caller without access asking about someone else.
        NotFoundError
            If the person has no picture, or no principal has that name (the same answer).
        """
        refuse_tokens(p, "profile picture")
        if name != p.name:
            grants = GrantService(self._session, self._authz)
            if not (grants.has_any_grant(p) or grants.is_admin(p)):
                raise PermissionDenied(_HIDDEN)
        found = self._session.execute(
            select(ProfilePicture.image, ProfilePicture.etag)
            .join(Principal, Principal.id == ProfilePicture.principal_id)
            .where(Principal.name == name)
        ).one_or_none()
        if found is None:
            raise NotFoundError(f"{name!r} has no profile picture.")
        return found.image, found.etag

    def etag_of(self, principal_id: uuid.UUID) -> str | None:
        """Read the etag of one person's picture, for a caller who may already see the person.

        Parameters
        ----------
        principal_id
            The person.

        Returns
        -------
        str or None
            The etag, or `None` without a picture.
        """
        return self.etags_of([principal_id]).get(principal_id)

    def etags_of(self, principal_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
        """Read the etags of several people's pictures in one statement.

        Asks nothing of the authorization service: the callers (a principal list, whoami)
        already decided the caller may see these people.

        Parameters
        ----------
        principal_ids
            The people.

        Returns
        -------
        dict of uuid.UUID to str
            The etag of each person who has a picture.
        """
        ids = list(principal_ids)
        if not ids:
            return {}
        rows = self._session.execute(
            select(ProfilePicture.principal_id, ProfilePicture.etag).where(
                ProfilePicture.principal_id.in_(ids)
            )
        )
        return {row.principal_id: row.etag for row in rows}
