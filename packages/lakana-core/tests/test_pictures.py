"""Profile pictures: re-encoding an untrusted upload, and who may set, read and remove one."""

from __future__ import annotations

import contextlib
import hashlib
import io
import struct
import uuid
import zlib

import pytest
from home_test_helpers import grant, person
from PIL import ExifTags, Image, ImageCms
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, CiScope, PrincipalRef, TaskScope
from lakana_core.errors import NotFoundError, PermissionDenied, UnsupportedPicture, ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.identity import pictures as pictures_module
from lakana_core.identity.pictures import (
    PICTURE_MAX_PIXELS,
    PICTURE_SIZE,
    PictureService,
    reencode_picture,
)
from lakana_core.models import AuditEvent, AuditEventType, PrincipalType, ProfilePicture

_NOT_READABLE = "is not a JPEG, PNG or WebP image Lakana can read"
_RED = (255, 0, 0)
_BLUE = (0, 0, 255)


def _encode(image: Image.Image, fmt: str, **options: object) -> bytes:
    """Encode an image as a file in memory.

    Parameters
    ----------
    image
        The image.
    fmt
        Pillow's format name.
    **options
        Passed to `Image.save`.

    Returns
    -------
    bytes
        The file.
    """
    buffer = io.BytesIO()
    image.save(buffer, fmt, **options)
    return buffer.getvalue()


def _decode(data: bytes) -> Image.Image:
    """Open a re-encoded picture.

    Parameters
    ----------
    data
        The WebP bytes.

    Returns
    -------
    Image.Image
        The loaded image.
    """
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def _pixel(image: Image.Image, xy: tuple[int, int]) -> tuple[int, ...]:
    """Read one pixel of an RGB or RGBA image.

    Parameters
    ----------
    image
        The image.
    xy
        The pixel's position.

    Returns
    -------
    tuple of int
        Its channels.
    """
    value = image.getpixel(xy)
    assert isinstance(value, tuple)
    return value


def _close_to(pixel: tuple[int, ...], colour: tuple[int, int, int]) -> bool:
    """Say whether a pixel is near a colour (WebP is lossy).

    Parameters
    ----------
    pixel
        The pixel read.
    colour
        The colour expected.

    Returns
    -------
    bool
        Whether every channel is within 40.
    """
    return all(abs(a - b) <= 40 for a, b in zip(pixel[:3], colour, strict=True))


def _png_chunk(kind: bytes, body: bytes) -> bytes:
    """Build one PNG chunk with its CRC.

    Parameters
    ----------
    kind
        The four-letter chunk type.
    body
        The chunk's data.

    Returns
    -------
    bytes
        Length, type, data and CRC.
    """
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def _grey_png(width: int, height: int, idat: bytes) -> bytes:
    """Build an 8-bit greyscale PNG with the given size and compressed pixel data.

    Parameters
    ----------
    width
        The declared width.
    height
        The declared height.
    idat
        The zlib stream of the rows.

    Returns
    -------
    bytes
        The file.
    """
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


@pytest.fixture
def rotated_jpeg_with_gps() -> bytes:
    """Build a phone-like photo: wide, EXIF orientation 6, GPS, an ICC profile and XMP.

    The left half is red and the right half blue; orientation 6 asks a viewer to turn it a
    quarter clockwise, so upright the red half is on top.

    Returns
    -------
    bytes
        The JPEG.
    """
    image = Image.new("RGB", (300, 200), _RED)
    image.paste(_BLUE, (150, 0, 300, 200))
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = 6
    exif[ExifTags.Base.Make] = "PhoneMaker"
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    gps[ExifTags.GPS.GPSLatitudeRef] = "N"
    gps[ExifTags.GPS.GPSLatitude] = (52.0, 31.0, 12.0)
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    return _encode(
        image, "JPEG", exif=exif, icc_profile=icc, xmp=b"<x:xmpmeta>home address</x:xmpmeta>"
    )


def test_exif_orientation_applied_and_metadata_gone(rotated_jpeg_with_gps: bytes) -> None:
    source = Image.open(io.BytesIO(rotated_jpeg_with_gps))
    assert source.getexif().get_ifd(ExifTags.IFD.GPSInfo)  # the fixture carries GPS
    data = reencode_picture(rotated_jpeg_with_gps, "image/jpeg")
    out = _decode(data)
    assert out.size == (PICTURE_SIZE, PICTURE_SIZE) and out.format == "WEBP"
    assert not out.getexif() and "xmp" not in out.info and "icc_profile" not in out.info
    assert b"EXIF" not in data and b"XMP " not in data and b"ICCP" not in data
    assert b"PhoneMaker" not in data and b"home address" not in data
    # Upright, the red half is on top and the blue half below.
    assert _close_to(_pixel(out, (128, 40)), _RED)
    assert _close_to(_pixel(out, (128, 215)), _BLUE)


def test_non_square_is_cropped_to_its_centre() -> None:
    strip = Image.new("RGB", (300, 100), _RED)
    strip.paste(_BLUE, (100, 0, 200, 100))
    out = _decode(reencode_picture(_encode(strip, "PNG"), "image/png"))
    for corner in ((4, 4), (251, 4), (4, 251), (251, 251), (128, 128)):
        assert _close_to(_pixel(out, corner), _BLUE), corner


def test_a_webp_and_a_transparent_png_are_accepted() -> None:
    webp = reencode_picture(_encode(Image.new("RGB", (40, 40), _RED), "WEBP"), "image/webp")
    assert _decode(webp).size == (PICTURE_SIZE, PICTURE_SIZE)
    clear = Image.new("RGBA", (40, 40), (0, 0, 255, 0))
    out = _decode(reencode_picture(_encode(clear, "PNG"), "image/png"))
    assert out.mode == "RGBA" and _pixel(out, (10, 10))[3] == 0
    palette = Image.new("P", (30, 20))
    assert _decode(reencode_picture(_encode(palette, "PNG"), "image/png")).mode == "RGB"
    cmyk = Image.new("CMYK", (30, 20), (0, 255, 255, 0))
    assert _decode(reencode_picture(_encode(cmyk, "JPEG"), "image/jpeg")).mode == "RGB"


def test_svg_is_refused_naming_the_formats() -> None:
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    for media_type in ("image/svg+xml", "image/gif", "text/html", ""):
        with pytest.raises(UnsupportedPicture, match="Use a JPEG, PNG or WebP image") as refused:
            reencode_picture(svg, media_type)
        assert "SVG and other formats are refused" in str(refused.value)


def test_an_svg_named_as_a_png_is_refused() -> None:
    svg = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>'
    with pytest.raises(ValidationError, match=_NOT_READABLE):
        reencode_picture(svg, "image/png")


def test_bytes_that_are_not_the_declared_type_are_refused() -> None:
    png = _encode(Image.new("RGB", (20, 20), _RED), "PNG")
    with pytest.raises(ValidationError, match=_NOT_READABLE) as refused:
        reencode_picture(png, "image/jpeg")
    assert not isinstance(refused.value, UnsupportedPicture)
    assert "Save it again as JPEG or PNG" in str(refused.value)
    gif = _encode(Image.new("RGB", (20, 20), _RED), "GIF")
    with pytest.raises(ValidationError, match=_NOT_READABLE):
        reencode_picture(gif, "image/png")


def test_a_huge_declared_size_is_refused_by_lakanas_own_check_without_decoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """64 megapixels is under Pillow's default bomb limit: only Lakana's own check refuses it."""
    assert (Image.MAX_IMAGE_PIXELS or 0) > 8000 * 8000
    loads: list[object] = []
    monkeypatch.setattr(Image.Image, "load", lambda self: loads.append(self))
    from PIL import ImageFile

    monkeypatch.setattr(ImageFile.ImageFile, "load", lambda self: loads.append(self))
    huge = _grey_png(8000, 8000, zlib.compress(b"\x00" * 8001))
    with pytest.raises(ValidationError, match=_NOT_READABLE):
        reencode_picture(huge, "image/png")
    assert loads == []
    # Over twice the limit Pillow itself refuses at open; still the same answer.
    larger = _grey_png(20_000, 20_000, zlib.compress(b"\x00" * 20_001))
    with pytest.raises(ValidationError, match=_NOT_READABLE):
        reencode_picture(larger, "image/png")
    assert loads == []


def test_just_over_the_pixel_limit_is_refused_and_just_under_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pictures_module, "PICTURE_MAX_PIXELS", 100)
    with pytest.raises(ValidationError, match=_NOT_READABLE):
        reencode_picture(_encode(Image.new("L", (101, 1)), "PNG"), "image/png")
    assert _decode(reencode_picture(_encode(Image.new("L", (10, 10)), "PNG"), "image/png"))


def test_a_decompression_bomb_is_refused() -> None:
    """A PNG of a few kilobytes that would decode to 64 megapixels of zeros."""
    side = 8000
    stream = zlib.compressobj(9)
    row = b"\x00" * (side + 1)
    idat = b"".join(stream.compress(row) for _ in range(side)) + stream.flush()
    bomb = _grey_png(side, side, idat)
    assert len(bomb) < 200_000 and side * side > PICTURE_MAX_PIXELS
    with pytest.raises(ValidationError, match=_NOT_READABLE):
        reencode_picture(bomb, "image/png")


def test_importing_pictures_changes_no_process_wide_pillow_or_warning_setting() -> None:
    import importlib
    import warnings

    filters = list(warnings.filters)
    importlib.reload(pictures_module)
    assert Image.MAX_IMAGE_PIXELS == (1024 * 1024 * 1024 // 4 // 3)  # Pillow's default
    assert list(warnings.filters) == filters


def test_pillows_bomb_warning_is_the_same_refusal() -> None:
    """Between Pillow's default limit and twice it, Pillow only warns; the warning is an error."""
    import warnings

    side = 10_000  # 100 megapixels: above Pillow's 89 million, below twice that
    png = _grey_png(side, side, zlib.compress(b"\x00" * (side + 1)))
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        with pytest.raises(ValidationError, match=_NOT_READABLE) as refused:
            reencode_picture(png, "image/png")
    assert isinstance(refused.value.__cause__, Image.DecompressionBombWarning)
    assert seen == []  # turned into the refusal, not left in the log


@pytest.mark.parametrize(
    ("mode", "options"),
    [("P", {}), ("P", {"transparency": 0}), ("1", {}), ("I;16", {}), ("LA", {}), ("L", {})],
)
def test_large_palette_bilevel_and_sixteen_bit_pngs_are_read(
    mode: str, options: dict[str, object]
) -> None:
    """A short side of 1024 or more makes the picture be reduced first, whatever its mode."""
    image = Image.new(mode, (1200, 1100))
    data = reencode_picture(_encode(image, "PNG", **options), "image/png")
    out = _decode(data)
    assert out.size == (PICTURE_SIZE, PICTURE_SIZE) and out.format == "WEBP"
    expected = "RGBA" if "transparency" in options or mode == "LA" else "RGB"
    assert out.mode == expected


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
def test_a_truncated_file_is_refused(fmt: str) -> None:
    whole = _encode(Image.effect_noise((200, 200), 60).convert("RGB"), fmt)
    media_type = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}[fmt]
    for cut in (len(whole) // 2, 30, 8, 0):
        with pytest.raises(ValidationError, match=_NOT_READABLE):
            reencode_picture(whole[:cut], media_type)


def test_garbage_and_flipped_bytes_never_escape_as_another_error() -> None:
    whole = bytearray(_encode(Image.effect_noise((64, 64), 60).convert("RGB"), "PNG"))
    for at in range(8, len(whole), max(1, len(whole) // 60)):
        flipped = bytearray(whole)
        flipped[at] ^= 0xFF
        with contextlib.suppress(ValidationError):
            reencode_picture(bytes(flipped), "image/png")


def test_a_polyglot_keeps_nothing_but_pixels() -> None:
    """A PNG with HTML in a text chunk and a script after its end comes out as pixels only."""
    from PIL import PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "<html><script>alert(1)</script></html>")
    png = _encode(Image.new("RGB", (40, 40), _RED), "PNG", pnginfo=info)
    polyglot = png + b"<script>alert(document.cookie)</script>"
    data = reencode_picture(polyglot, "image/png")
    assert b"<script" not in data and b"<html" not in data
    assert data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    jpeg = _encode(Image.new("RGB", (40, 40), _RED), "JPEG", comment=b"<svg onload=alert(1)>")
    assert b"<svg" not in reencode_picture(jpeg, "image/jpeg")


def test_only_the_first_frame_of_an_animation_is_kept() -> None:
    frames = [Image.new("RGB", (64, 64), _RED), Image.new("RGB", (64, 64), _BLUE)]
    animated = _encode(frames[0], "WEBP", save_all=True, append_images=frames[1:], duration=100)
    assert getattr(Image.open(io.BytesIO(animated)), "n_frames", 1) == 2
    data = reencode_picture(animated, "image/webp")
    out = _decode(data)
    assert getattr(out, "n_frames", 1) == 1 and b"ANIM" not in data
    assert _close_to(_pixel(out, (128, 128)), _RED)


# --- PictureService ---------------------------------------------------------------------------


def _jpeg() -> bytes:
    """Build a small JPEG.

    Returns
    -------
    bytes
        The file.
    """
    return _encode(Image.new("RGB", (64, 48), _RED), "JPEG")


def _service(session: Session) -> PictureService:
    """Build the service with grant-backed authorization.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PictureService
        The service under test.
    """
    return PictureService(session, GrantAuthorization(session))


def _events(session: Session, kind: AuditEventType) -> list[AuditEvent]:
    """Read the audit events of one type, oldest first.

    Parameters
    ----------
    session
        Database session.
    kind
        The type.

    Returns
    -------
    list of AuditEvent
        The events.
    """
    query = select(AuditEvent).where(AuditEvent.event_type == kind)
    return list(session.execute(query.order_by(AuditEvent.occurred_at)).scalars())


def _tokens(who: PrincipalRef) -> list[PrincipalRef]:
    """Build a task token's and a CI token's reference of a person.

    Parameters
    ----------
    who
        The person.

    Returns
    -------
    list of PrincipalRef
        The task token's, then the CI token's.
    """
    task = PrincipalRef(
        id=who.id,
        name=who.name,
        type=who.type,
        task_scope=TaskScope(environment="dev", asset_scope=["gold.*"], token_id=uuid.uuid4()),
    )
    ci = PrincipalRef(
        id=who.id,
        name=who.name,
        type=who.type,
        ci_scope=CiScope(
            token_id=uuid.uuid4(),
            rule_id=uuid.uuid4(),
            environment="dev",
            environment_id=uuid.uuid4(),
            repository="shop",
            repository_id=uuid.uuid4(),
            kinds=frozenset({"deploy"}),
            sha=None,
        ),
    )
    return [task, ci]


def test_set_stores_the_reencoded_picture_and_audits(session: Session) -> None:
    kim = person(session, "kim")
    service = _service(session)
    etag = service.set(kim, "kim", _jpeg(), "image/jpeg")
    row = session.get(ProfilePicture, kim.id)
    assert row is not None and row.etag == etag == hashlib.sha256(row.image).hexdigest()
    assert row.image.startswith(b"RIFF")
    assert service.get(kim, "kim") == (row.image, etag)
    assert service.etag_of(kim.id) == etag
    [event] = _events(session, AuditEventType.PROFILE_PICTURE_SET)
    assert event.principal_id == kim.id and event.detail == {"principal": "kim"}
    # A second picture replaces the first.
    other = service.set(kim, "kim", _encode(Image.new("RGB", (9, 9), _BLUE), "PNG"), "image/png")
    assert other != etag and service.etag_of(kim.id) == other
    assert len(_events(session, AuditEventType.PROFILE_PICTURE_SET)) == 2


def test_set_is_self_only(session: Session) -> None:
    kim = person(session, "kim")
    admin = person(session, "root")
    grant(session, admin, role=Role.ADMIN)
    person(session, "lee")
    with pytest.raises(PermissionDenied, match="your own profile picture"):
        _service(session).set(admin, "lee", _jpeg(), "image/jpeg")
    for token in _tokens(kim):
        with pytest.raises(PermissionDenied, match="personal access token"):
            _service(session).set(token, "kim", _jpeg(), "image/jpeg")
    assert session.execute(select(ProfilePicture)).first() is None


def test_check_can_set_refuses_before_any_byte_is_read(session: Session) -> None:
    kim = person(session, "kim")
    service = _service(session)
    service.check_can_set(kim, "kim", "image/png")
    with pytest.raises(PermissionDenied):
        service.check_can_set(kim, "lee", "image/png")
    with pytest.raises(UnsupportedPicture):
        service.check_can_set(kim, "kim", "image/svg+xml")


def test_a_refused_picture_stores_and_audits_nothing(session: Session) -> None:
    kim = person(session, "kim")
    with pytest.raises(ValidationError):
        _service(session).set(kim, "kim", b"not an image", "image/png")
    assert session.get(ProfilePicture, kim.id) is None
    assert _events(session, AuditEventType.PROFILE_PICTURE_SET) == []


def test_set_for_a_deleted_principal_is_not_found(session: Session) -> None:
    ghost = PrincipalRef(id=uuid.uuid4(), name="ghost", type=PrincipalType.USER)
    with pytest.raises(NotFoundError):
        _service(session).set(ghost, "ghost", _jpeg(), "image/jpeg")


def test_get_is_for_people_with_access(session: Session) -> None:
    kim = person(session, "kim")
    lee = person(session, "lee")
    root = person(session, "root")
    grant(session, root, role=Role.ADMIN)
    service = _service(session)
    etag = service.set(kim, "kim", _jpeg(), "image/jpeg")
    with pytest.raises(PermissionDenied, match=r"^Pictures are shown to people who have access\.$"):
        service.get(lee, "kim")
    assert service.get(root, "kim")[1] == etag
    grant(session, lee, action=Action.READ_TABLE, environment_name="dev")
    assert service.get(lee, "kim")[1] == etag
    for token in _tokens(root):
        with pytest.raises(PermissionDenied, match="personal access token"):
            service.get(token, "kim")


def test_get_without_a_picture_or_person_is_not_found(session: Session) -> None:
    kim = person(session, "kim")
    root = person(session, "root")
    grant(session, root, role=Role.ADMIN)
    service = _service(session)
    with pytest.raises(NotFoundError, match="has no profile picture"):
        service.get(kim, "kim")
    with pytest.raises(NotFoundError, match="has no profile picture"):
        service.get(root, "nobody")
    assert service.etag_of(kim.id) is None


def test_remove_own_picture_is_audited(session: Session) -> None:
    kim = person(session, "kim")
    service = _service(session)
    service.set(kim, "kim", _jpeg(), "image/jpeg")
    service.remove(kim, "kim")
    assert session.get(ProfilePicture, kim.id) is None
    [event] = _events(session, AuditEventType.PROFILE_PICTURE_REMOVED)
    assert event.principal_id == kim.id and event.detail == {"principal": "kim"}
    service.remove(kim, "kim")  # nothing left: nothing happens, nothing more is recorded
    assert len(_events(session, AuditEventType.PROFILE_PICTURE_REMOVED)) == 1


def test_admin_may_remove_anothers_picture_and_it_is_audited(session: Session) -> None:
    kim = person(session, "kim")
    lee = person(session, "lee")
    root = person(session, "root")
    grant(session, root, role=Role.ADMIN)
    service = _service(session)
    service.set(kim, "kim", _jpeg(), "image/jpeg")
    with pytest.raises(PermissionDenied):
        service.remove(lee, "kim")
    assert session.get(ProfilePicture, kim.id) is not None
    service.remove(root, "kim")
    assert session.get(ProfilePicture, kim.id) is None
    [event] = _events(session, AuditEventType.PROFILE_PICTURE_REMOVED)
    assert event.principal_id == root.id
    assert event.detail == {"principal": "kim", "by": "root"}
    with pytest.raises(NotFoundError):
        service.remove(root, "nobody")
    for token in _tokens(root):
        with pytest.raises(PermissionDenied, match="personal access token"):
            service.remove(token, "kim")


def test_etags_of_answers_for_many_people_in_one_go(session: Session) -> None:
    kim = person(session, "kim")
    lee = person(session, "lee")
    service = _service(session)
    etag = service.set(kim, "kim", _jpeg(), "image/jpeg")
    assert service.etags_of([kim.id, lee.id]) == {kim.id: etag}
    assert service.etags_of([]) == {}


def test_two_first_uploads_of_one_person_both_land(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The later of two racing first uploads replaces the row the earlier one wrote."""
    kim = person(session, "kim")
    service = _service(session)
    first = service.set(kim, "kim", _jpeg(), "image/jpeg")
    real = pictures_module._stored
    looked: list[uuid.UUID] = []

    def stale_once(session: Session, principal_id: uuid.UUID) -> ProfilePicture | None:
        """Miss the row the first time, as a request that looked before the other one wrote.

        Parameters
        ----------
        session
            Database session, passed on.
        principal_id
            The person, passed on.

        Returns
        -------
        ProfilePicture or None
            `None` once, then the row.
        """
        looked.append(principal_id)
        return None if len(looked) == 1 else real(session, principal_id)

    monkeypatch.setattr(pictures_module, "_stored", stale_once)
    second = service.set(kim, "kim", _encode(Image.new("RGB", (9, 9), _BLUE), "PNG"), "image/png")
    assert looked and second != first and service.etag_of(kim.id) == second
