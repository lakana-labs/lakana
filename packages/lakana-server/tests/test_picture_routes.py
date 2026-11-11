"""Tests for profile pictures over REST: upload, read with caching headers, remove."""

from __future__ import annotations

import asyncio
import hashlib
import io
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.models import AuditEvent, AuditEventType
from lakana_server.api.platform import pictures as routes
from tests.api.platform.test_whoami import _new_person, _task_token_header
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment

_MINE = "/api/v1/principals/admin/picture"
_FIVE_MIB = 5 * 1024 * 1024


def _jpeg(colour: str = "red") -> bytes:
    """Build a small JPEG.

    Parameters
    ----------
    colour
        Its colour.

    Returns
    -------
    bytes
        The file.
    """
    buffer = io.BytesIO()
    Image.new("RGB", (80, 60), colour).save(buffer, "JPEG")
    return buffer.getvalue()


def _put(
    client: TestClient, path: str, headers: dict[str, str], data: bytes, media: str
) -> httpx.Response:
    """Upload a picture.

    Parameters
    ----------
    client
        The test client.
    path
        The picture's path.
    headers
        The credential.
    data
        The body.
    media
        Its `Content-Type`.

    Returns
    -------
    httpx.Response
        The answer.
    """
    return client.put(path, headers={**headers, "Content-Type": media}, content=data)


def _events(client: TestClient, kind: AuditEventType) -> list[AuditEvent]:
    """Read the audit events of one type.

    Parameters
    ----------
    client
        The test client; its app's engine is used.
    kind
        The type.

    Returns
    -------
    list of AuditEvent
        The events.
    """
    with Session(client.app.state.engine) as session:
        return list(
            session.execute(select(AuditEvent).where(AuditEvent.event_type == kind)).scalars()
        )


def test_set_is_self_only_and_audited(client: TestClient) -> None:
    response = _put(client, _MINE, AUTH_HEADER, _jpeg(), "image/jpeg")
    assert response.status_code == 200, response.text
    etag = response.json()["etag"]
    assert len(etag) == 64
    [event] = _events(client, AuditEventType.PROFILE_PICTURE_SET)
    assert event.detail == {"principal": "admin"}
    kim = _new_person(client, "kim")
    refused = _put(client, _MINE, kim, _jpeg(), "image/jpeg")
    assert refused.status_code == 403
    assert "your own profile picture" in refused.json()["detail"]
    token = _task_token_header(client)
    assert _put(client, _MINE, token, _jpeg(), "image/jpeg").status_code == 403
    assert len(_events(client, AuditEventType.PROFILE_PICTURE_SET)) == 1


def test_a_person_without_access_sets_and_sees_their_own(client: TestClient) -> None:
    kim = _new_person(client, "kim")
    response = _put(client, "/api/v1/principals/kim/picture", kim, _jpeg(), "image/png")
    assert response.status_code == 422  # JPEG bytes sent as PNG
    assert "Save it again as JPEG or PNG" in response.json()["detail"]
    response = _put(client, "/api/v1/principals/kim/picture", kim, _jpeg(), "image/jpeg")
    assert response.status_code == 200, response.text
    own = client.get("/api/v1/principals/kim/picture", headers=kim)
    assert own.status_code == 200 and own.content.startswith(b"RIFF")


def test_over_five_megabytes_is_refused_naming_the_limit(client: TestClient) -> None:
    too_big = b"\xff\xd8" + b"\x00" * (_FIVE_MIB - 1)
    response = _put(client, _MINE, AUTH_HEADER, too_big, "image/jpeg")
    assert response.status_code == 413
    assert response.json()["detail"] == (
        "The picture is larger than 5 MB. Choose a smaller image or crop it first."
    )

    def chunks() -> Iterator[bytes]:
        """Send a body without a declared length, in pieces.

        Yields
        ------
        bytes
            One MiB at a time, six times.
        """
        for _ in range(6):
            yield b"\x00" * (1024 * 1024)

    streamed = client.put(
        _MINE, headers={**AUTH_HEADER, "Content-Type": "image/jpeg"}, content=chunks()
    )
    assert streamed.status_code == 413
    assert "5 MB" in streamed.json()["detail"]
    assert _events(client, AuditEventType.PROFILE_PICTURE_SET) == []


def test_exactly_five_mebibytes_is_read_and_judged_as_an_image(client: TestClient) -> None:
    exactly = b"\x00" * _FIVE_MIB
    response = _put(client, _MINE, AUTH_HEADER, exactly, "image/jpeg")
    assert response.status_code == 422  # read in full, then not an image


def test_svg_and_a_missing_type_are_refused_with_415(client: TestClient) -> None:
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
    for media in ("image/svg+xml", "image/svg+xml; charset=utf-8", "application/octet-stream"):
        response = _put(client, _MINE, AUTH_HEADER, svg, media)
        assert response.status_code == 415, media
        assert response.json()["detail"] == (
            "Use a JPEG, PNG or WebP image. SVG and other formats are refused."
        )
    bare = client.put(_MINE, headers=AUTH_HEADER, content=svg)
    assert bare.status_code == 415
    # A parameter after the type is ignored; the type itself decides.
    ok = _put(client, _MINE, AUTH_HEADER, _jpeg(), "Image/JPEG; q=1")
    assert ok.status_code == 200, ok.text


def test_admin_may_remove_anothers_picture_and_it_is_audited(client: TestClient) -> None:
    kim = _new_person(client, "kim")
    assert _put(client, "/api/v1/principals/kim/picture", kim, _jpeg(), "image/jpeg").is_success
    assert client.delete(_MINE, headers=kim).status_code == 403
    removed = client.delete("/api/v1/principals/kim/picture", headers=AUTH_HEADER)
    assert removed.status_code == 204
    [event] = _events(client, AuditEventType.PROFILE_PICTURE_REMOVED)
    assert event.detail == {"principal": "kim", "by": "admin"}
    assert client.get("/api/v1/principals/kim/picture", headers=kim).status_code == 404
    gone = client.delete("/api/v1/principals/nobody/picture", headers=AUTH_HEADER)
    assert gone.status_code == 404


def test_person_without_access_sees_no_picture(client: TestClient) -> None:
    assert _put(client, _MINE, AUTH_HEADER, _jpeg(), "image/jpeg").is_success
    kim = _new_person(client, "kim")
    response = client.get(_MINE, headers=kim)
    assert response.status_code == 403
    assert response.json()["detail"] == "Pictures are shown to people who have access."
    create_environment(client, "dev")
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": "kim", "kind": "ENVIRONMENT", "environment": "dev", "role": "VIEWER"},
    )
    assert granted.status_code == 201, granted.text
    assert client.get(_MINE, headers=kim).status_code == 200


def test_a_task_token_may_not_read_a_picture(client: TestClient) -> None:
    assert _put(client, _MINE, AUTH_HEADER, _jpeg(), "image/jpeg").is_success
    token = _task_token_header(client)
    response = client.get(_MINE, headers=token)
    assert response.status_code == 403
    assert "personal access token" in response.json()["detail"]
    assert client.delete(_MINE, headers=token).status_code == 403


def test_picture_response_headers_and_304(client: TestClient) -> None:
    etag = _put(client, _MINE, AUTH_HEADER, _jpeg(), "image/jpeg").json()["etag"]
    response = client.get(_MINE, headers=AUTH_HEADER)
    assert response.status_code == 200
    assert hashlib.sha256(response.content).hexdigest() == etag
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["etag"] == f'"{etag}"'
    assert response.headers["cache-control"] == "private, max-age=86400"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "default-src 'none'; sandbox"
    for match in (f'"{etag}"', f'W/"{etag}"', f'"other", "{etag}"', "*"):
        cached = client.get(_MINE, headers={**AUTH_HEADER, "If-None-Match": match})
        assert cached.status_code == 304, match
        assert cached.content == b""
        assert cached.headers["etag"] == f'"{etag}"'
        assert cached.headers["cache-control"] == "private, max-age=86400"
    stale = client.get(_MINE, headers={**AUTH_HEADER, "If-None-Match": '"old"'})
    assert stale.status_code == 200 and stale.content == response.content


def test_principal_and_whoami_carry_the_etag(client: TestClient) -> None:
    assert client.get("/api/v1/whoami", headers=AUTH_HEADER).json()["picture"] is None
    kim = _new_person(client, "kim")
    assert client.get("/api/v1/principals/kim", headers=AUTH_HEADER).json()["picture"] is None
    etag = _put(client, _MINE, AUTH_HEADER, _jpeg(), "image/jpeg").json()["etag"]
    kims = _put(client, "/api/v1/principals/kim/picture", kim, _jpeg("blue"), "image/jpeg")
    assert client.get("/api/v1/whoami", headers=AUTH_HEADER).json()["picture"] == etag
    assert client.get("/api/v1/whoami", headers=kim).json()["picture"] == kims.json()["etag"]
    listed = client.get("/api/v1/principals", headers=AUTH_HEADER).json()
    assert {p["name"]: p["picture"] for p in listed} == {
        "admin": etag,
        "kim": kims.json()["etag"],
    }
    one = client.get("/api/v1/principals/admin", headers=AUTH_HEADER).json()
    assert one["picture"] == etag
    client.delete(_MINE, headers=AUTH_HEADER)
    assert client.get("/api/v1/whoami", headers=AUTH_HEADER).json()["picture"] is None


def test_group_members_carry_the_etag(client: TestClient) -> None:
    etag = _put(client, _MINE, AUTH_HEADER, _jpeg(), "image/jpeg").json()["etag"]
    assert client.post("/api/v1/groups", headers=AUTH_HEADER, json={"name": "team"}).is_success
    added = client.put("/api/v1/groups/team/members/admin", headers=AUTH_HEADER)
    assert added.is_success, added.text
    members = client.get("/api/v1/groups/team/members", headers=AUTH_HEADER).json()
    assert [(m["name"], m["picture"]) for m in members] == [("admin", etag)]


def test_pictures_need_a_credential(client: TestClient) -> None:
    assert client.get(_MINE).status_code == 401
    assert client.put(_MINE, content=_jpeg()).status_code == 401
    assert client.delete(_MINE).status_code == 401


async def _trickle(iterator: AsyncIterator[bytes], idle: float) -> bytes | None:
    """Hand out one byte every 50 ms, for ever.

    Parameters
    ----------
    iterator
        Ignored.
    idle
        Ignored.

    Returns
    -------
    bytes
        One byte.
    """
    await asyncio.sleep(0.05)
    return b" "


def test_a_picture_that_takes_too_long_in_all_is_a_timeout(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A byte at a time, each in time, still ends at the total deadline."""
    client.app.state.settings.append.read_timeout_seconds = 0.2
    monkeypatch.setattr(routes, "next_chunk", _trickle)
    slow = _put(client, _MINE, AUTH_HEADER, _jpeg(), "image/jpeg")
    assert slow.status_code == 408
    assert "did not finish arriving within 0.2s" in slow.json()["detail"]


def test_a_picture_has_its_own_shorter_deadline_than_an_append(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two slow uploads cannot hold both decode slots for the append's 300 s."""
    assert client.app.state.settings.append.read_timeout_seconds == 300
    assert routes.PICTURE_READ_TIMEOUT_SECONDS == 120
    monkeypatch.setattr(routes, "PICTURE_READ_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(routes, "next_chunk", _trickle)
    slow = _put(client, _MINE, AUTH_HEADER, _jpeg(), "image/jpeg")
    assert slow.status_code == 408
    assert "did not finish arriving within 0.2s" in slow.json()["detail"]


_YOURS_BUSY = (
    "Another picture upload of yours is still in progress. Wait for it to finish, then try again."
)
_ALL_BUSY = "The server is busy with other picture uploads. Try again in a moment."


def _no_read(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record every attempt to read the body.

    Parameters
    ----------
    monkeypatch
        Pytest's monkeypatch.

    Returns
    -------
    list of float
        One entry per read attempt.
    """
    reads: list[float] = []

    async def counting(iterator: AsyncIterator[bytes], idle: float) -> bytes | None:
        """Record the read and end the body.

        Parameters
        ----------
        iterator
            Ignored.
        idle
            Recorded.

        Returns
        -------
        None
            The end of the body.
        """
        reads.append(idle)
        await asyncio.sleep(0)
        return None

    monkeypatch.setattr(routes, "next_chunk", counting)
    return reads


def test_a_second_upload_of_the_same_person_is_503_before_its_body_is_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Session(client.app.state.engine) as session:
        from lakana_core.models import Principal

        principal_id = session.execute(
            select(Principal.id).where(Principal.name == "admin")
        ).scalar_one()
    reads = _no_read(monkeypatch)
    client.app.state.picture_principals.add(principal_id)
    try:
        busy = _put(client, _MINE, AUTH_HEADER, _jpeg(), "image/jpeg")
    finally:
        client.app.state.picture_principals.discard(principal_id)
    assert busy.status_code == 503
    assert busy.headers["Retry-After"] == "1"
    assert busy.json()["detail"] == _YOURS_BUSY
    assert reads == []


def test_an_upload_when_every_decode_slot_is_taken_is_503_before_its_body_is_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = _no_read(monkeypatch)
    slots: asyncio.Semaphore = client.app.state.picture_decode_slots
    taken = 0
    while not slots.locked():
        slots._value -= 1  # take a slot without an event loop; given back below
        taken += 1
    try:
        busy = _put(client, _MINE, AUTH_HEADER, _jpeg(), "image/jpeg")
    finally:
        slots._value += taken
    assert busy.status_code == 503
    assert busy.json()["detail"] == _ALL_BUSY
    assert reads == []
    assert taken == routes.PICTURE_DECODE_SLOTS


def test_the_hold_is_given_back_however_the_upload_ends(client: TestClient) -> None:
    too_big = b"\x00" * (_FIVE_MIB + 1)
    assert _put(client, _MINE, AUTH_HEADER, too_big, "image/jpeg").status_code == 413
    assert _put(client, _MINE, AUTH_HEADER, b"junk", "image/png").status_code == 422
    assert _put(client, _MINE, AUTH_HEADER, _jpeg(), "image/jpeg").status_code == 200
    assert client.app.state.picture_principals == set()
    slots: asyncio.Semaphore = client.app.state.picture_decode_slots
    assert slots._value == routes.PICTURE_DECODE_SLOTS
