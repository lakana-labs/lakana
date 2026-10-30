"""Tests for `install_exception_handlers`, independent of the full app.

A minimal `FastAPI` instance with one throwaway route is enough to prove the
handler maps each `LakanaError` subclass to its documented status code and
shape — `create_app`'s own routes do not exist until later tasks.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from lakana_core.errors import (
    AlreadyExistsError,
    AuthenticationError,
    ConflictError,
    KeptAfterCheckError,
    LakanaError,
    NotFoundError,
    NotPublishedError,
    PermissionDenied,
    StorageRefusedError,
    ValidationError,
)
from lakana_server.errors import install_exception_handlers


def _client_raising(exc: LakanaError) -> TestClient:
    """Build a TestClient whose one route always raises `exc`.

    Parameters
    ----------
    exc
        The exception instance for the throwaway route to raise.

    Returns
    -------
    TestClient
        A client for the app under test.
    """
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/boom")
    def boom() -> None:
        raise exc

    return TestClient(app)


@pytest.mark.parametrize(
    ("exc", "expected_status"),
    [
        (AuthenticationError("bad creds"), 401),
        (NotFoundError("missing"), 404),
        (AlreadyExistsError("dup"), 409),
        (ConflictError("conflict"), 409),
        (ValidationError("bad input"), 422),
        (PermissionDenied("no"), 403),
        (StorageRefusedError("refused"), 502),
    ],
)
def test_each_lakana_error_maps_to_its_documented_status_code(
    exc: LakanaError, expected_status: int
) -> None:
    """Every entry in `_STATUS_CODES` is exercised, not just `NotFoundError`."""
    with _client_raising(exc) as client:
        response = client.get("/boom")

    assert response.status_code == expected_status
    assert response.json() == {"detail": str(exc), "type": type(exc).__name__}


def test_an_unmapped_lakana_error_defaults_to_500() -> None:
    """A `LakanaError` subclass with no entry in the status map still gets a response."""

    class _UnmappedError(LakanaError):
        """A LakanaError subclass deliberately absent from `_STATUS_CODES`."""

    with _client_raising(_UnmappedError("unmapped")) as client:
        response = client.get("/boom")

    assert response.status_code == 500
    assert response.json() == {"detail": "unmapped", "type": "_UnmappedError"}


def test_a_subclass_maps_to_its_nearest_mapped_base() -> None:
    """`SchemaMismatchError` is a `ValidationError`, so it is a 422, not a 500."""
    from lakana_core.catalog.schema_compat import SchemaDiff, SchemaMismatchError

    response = _client_raising(SchemaMismatchError("gold.t", SchemaDiff())).get("/boom")

    assert response.status_code == 422
    assert response.json()["type"] == "SchemaMismatchError"


def test_a_validation_error_is_a_list_of_problems_without_the_input_or_a_crash() -> None:
    """A-I5: the shape stays `{"detail": [{"type", "loc", "msg"}]}`; the value is not echoed."""
    app = FastAPI()
    install_exception_handlers(app)

    class Body(BaseModel):
        """A throwaway request body."""

        name: str = Field(max_length=3)
        count: int

    @app.post("/body")
    def body(item: Body) -> None:
        """Take a body."""

    with TestClient(app) as client:
        plain = client.post("/body", json={"name": "secret value", "count": "x"})
        lone = client.post(
            "/body",
            headers={"Content-Type": "application/json"},
            content=b'{"name": "a\\ud800b", "count": 1, "a\\ud800": 1}',
        )
    assert plain.status_code == lone.status_code == 422
    errors = plain.json()["detail"]
    assert [(e["type"], e["loc"]) for e in errors] == [
        ("string_too_long", ["body", "name"]),
        ("int_parsing", ["body", "count"]),
    ]
    assert all({"type", "loc", "msg"} <= set(e) for e in errors)
    assert "secret value" not in plain.text
    assert [e["loc"] for e in lone.json()["detail"]] == [["body", "name"]]


def test_not_published_is_a_409_on_the_platform_api() -> None:
    """A read of a table whose first build failed a check is a conflict, with the text as detail."""
    text = "`gold.sales` has not been published: its first build failed a check (run r)."
    with _client_raising(NotPublishedError(text)) as client:
        response = client.get("/boom")

    assert response.status_code == 409
    assert response.json() == {"detail": text, "type": "NotPublishedError"}


def test_kept_after_check_is_a_409_on_the_platform_api() -> None:
    """A task refused a table that was kept after a failed check: a conflict, text as detail."""
    text = "`gold.sales` was kept after a failed check (`gold.sales/a`, run r)."
    with _client_raising(KeptAfterCheckError(text)) as client:
        response = client.get("/boom")

    assert response.status_code == 409
    assert response.json() == {"detail": text, "type": "KeptAfterCheckError"}


def test_a_storage_refusal_does_not_ask_the_client_to_retry() -> None:
    """Wrong credentials stay wrong: 502 without `Retry-After`, unlike an unreachable storage."""
    with _client_raising(StorageRefusedError("refused")) as client:
        response = client.get("/boom")
    assert response.status_code == 502
    assert "Retry-After" not in response.headers
