"""A table read on a storage that cannot be reached says so, names the environment and what next."""

from __future__ import annotations

import errno
from pathlib import Path

import pyarrow as pa
import pytest
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.service import CatalogService
from lakana_core.config import StorageConfig
from lakana_core.errors import StorageUnavailableError
from lakana_core.identifiers import TableRef
from lakana_core.models import WriteMode
from lakana_core.storage.resolver import StorageResolver
from lakana_core.storage.unavailable import TABLE_STORAGE_UNREACHABLE, is_storage_unavailable

ORDERS = TableRef.from_dotted("dev", "bronze.orders")


@pytest.fixture
def catalog(session: Session, principal: PrincipalRef, tmp_path: Path) -> CatalogService:
    """Provide a catalog with one environment `dev` and one table in it.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    tmp_path
        Pytest's per-test directory, the environment's storage root.

    Returns
    -------
    CatalogService
        The catalog.
    """
    built = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    built.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    data = pa.table({"id": pa.array([1], pa.int64())})
    built.write_batches(
        principal, ORDERS, mode=WriteMode.CREATE, schema=data.schema, batches=data.to_batches()
    )
    return built


def _failing(error: BaseException) -> object:
    """Build a stand-in for `read_metadata` that raises.

    Parameters
    ----------
    error
        What it raises.

    Returns
    -------
    object
        The stand-in.
    """

    def read_metadata(*args: object) -> None:
        """Raise the error.

        Parameters
        ----------
        *args
            Ignored.

        Raises
        ------
        BaseException
            `error`, always.
        """
        raise error

    return read_metadata


def test_an_unreachable_storage_names_the_environment_and_the_next_step(
    catalog: CatalogService,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        "lakana_core.catalog.service.read_metadata",
        _failing(OSError(errno.EIO, "AWS Error NETWORK_CONNECTION during HeadObject")),
    )

    with pytest.raises(StorageUnavailableError) as raised:
        catalog.load_table(principal, ORDERS)

    message = str(raised.value)
    assert message == TABLE_STORAGE_UNREACHABLE.format(environment="dev")
    assert "'dev'" in message and "Try again" in message and "operator" in message
    assert str(tmp_path) not in message and "AWS" not in message
    (line,) = caplog.records
    assert line.event == "storage_unreachable"  # ty: ignore[missing-attribute]
    assert "AWS" not in line.getMessage() and "OSError" in line.getMessage()


def test_a_missing_metadata_file_is_not_called_unreachable(
    catalog: CatalogService, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "lakana_core.catalog.service.read_metadata", _failing(FileNotFoundError("gone"))
    )

    with pytest.raises(FileNotFoundError):
        catalog.load_table(principal, ORDERS)


@pytest.mark.parametrize(
    ("error", "unavailable"),
    [
        (OSError(errno.EIO, "network"), True),
        (ConnectionRefusedError("refused"), True),
        (TimeoutError("slow"), True),
        (FileNotFoundError("gone"), False),
        (IsADirectoryError("a directory"), False),
        (PermissionError("access denied"), False),
        (ValueError("not an I/O error"), False),
    ],
)
def test_is_storage_unavailable_tells_an_unreachable_storage_from_a_missing_file(
    error: BaseException, unavailable: bool
) -> None:
    assert is_storage_unavailable(error) is unavailable
