"""A stored query result is streamed to storage, not built in memory a second time (#10)."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.ipc
import pytest

from lakana_core.errors import NotFoundError
from lakana_core.storage.base import StreamWriter
from lakana_core.storage.fsspec_storage import FsspecStorage
from lakana_worker.pipeline import _store_result

_PATH = "_artifacts/queries/abc/result.arrow"


class _Recording(FsspecStorage):
    """A real local storage that records how the result reached it.

    Parameters
    ----------
    root
        The storage root.
    fail_after
        Make the stream raise on the write after this many writes, or `None` never to.
    """

    def __init__(self, root: str, fail_after: int | None = None) -> None:
        super().__init__(root, {})
        self._fail_after = fail_after
        self.whole_writes = 0
        self.stream_writes = 0
        self.aborted = False

    def write(self, path: str, data: bytes) -> None:
        """Count a write of the whole object, then do it.

        Parameters
        ----------
        path
            Root-relative path.
        data
            The object.
        """
        self.whole_writes += 1
        super().write(path, data)

    def open_write(self, path: str) -> StreamWriter:
        """Open a stream whose writes are counted.

        Parameters
        ----------
        path
            Root-relative path.

        Returns
        -------
        StreamWriter
            The stream.
        """
        return _Stream(self, super().open_write(path))


class _Stream:
    """The stream of `_Recording`.

    Parameters
    ----------
    owner
        The recording storage.
    inner
        The real stream.
    """

    def __init__(self, owner: _Recording, inner: StreamWriter) -> None:
        self._owner = owner
        self._inner = inner

    def write(self, data: bytes) -> None:
        """Count the piece, fail when told to, else pass it on.

        Parameters
        ----------
        data
            The next piece.

        Raises
        ------
        OSError
            When the recording storage was told to fail here.
        """
        if (
            self._owner._fail_after is not None
            and self._owner.stream_writes >= self._owner._fail_after
        ):
            raise OSError("the storage went away")
        self._owner.stream_writes += 1
        self._inner.write(data)

    def commit(self) -> None:
        """Make the object visible."""
        self._inner.commit()

    def abort(self) -> None:
        """Throw the bytes away and note it."""
        self._owner.aborted = True
        self._inner.abort()


def test_a_result_is_streamed_and_reads_back(tmp_path: Path) -> None:
    table = pa.table({"id": pa.array(range(50_000), pa.int64())})
    storage = _Recording(f"file://{tmp_path}")

    _store_result(storage, _PATH, table)

    assert storage.whole_writes == 0
    assert storage.stream_writes >= 1
    with pyarrow.ipc.open_file(pa.BufferReader(storage.read(_PATH))) as reader:
        assert reader.read_all().equals(table)


def test_a_failed_stream_is_aborted_and_leaves_no_object(tmp_path: Path) -> None:
    table = pa.table({"id": pa.array(range(50_000), pa.int64())})
    storage = _Recording(f"file://{tmp_path}", fail_after=0)

    with pytest.raises(OSError, match="the storage went away"):
        _store_result(storage, _PATH, table)

    assert storage.aborted
    with pytest.raises(NotFoundError):
        storage.read(_PATH)
