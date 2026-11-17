"""Streaming storage against a real S3-compatible server (SeaweedFS)."""

import gc
from collections.abc import Iterator

import pytest
from core_test_helpers import SeaweedFsContainer

from lakana_core.errors import NotFoundError
from lakana_core.storage import ChildEntry, FsspecStorage

pytestmark = pytest.mark.integration

MIB = 1024 * 1024
BUCKET = "lakana"


@pytest.fixture(scope="module")
def storage() -> Iterator[FsspecStorage]:
    """Provide storage over a real SeaweedFS bucket.

    Yields
    ------
    FsspecStorage
        Storage rooted at ``s3://lakana/dev``.
    """
    with SeaweedFsContainer() as seaweedfs:
        seaweedfs.get_client().make_bucket(BUCKET)
        yield FsspecStorage(
            f"s3://{BUCKET}/dev",
            {
                "s3.endpoint": f"http://{seaweedfs.get_config()['endpoint']}",
                "s3.access-key-id": seaweedfs.access_key,
                "s3.secret-access-key": seaweedfs.secret_key,
            },
        )


def _open_uploads(storage: FsspecStorage) -> list[str]:
    """List the multipart uploads still open in the bucket.

    Parameters
    ----------
    storage
        The storage under test.

    Returns
    -------
    list of str
        Keys with an unfinished multipart upload.
    """
    response = storage._fs.call_s3("list_multipart_uploads", Bucket=BUCKET)
    return [upload["Key"] for upload in response.get("Uploads", [])]


def test_a_small_stream_is_invisible_until_commit_and_discarded_by_abort(
    storage: FsspecStorage,
) -> None:
    writer = storage.open_write("small/a")
    writer.write(b"hello")
    assert storage.exists("small/a") is False
    writer.commit()
    assert storage.read("small/a") == b"hello"
    assert list(storage.read_chunks("small/a", chunk_bytes=2)) == [b"he", b"ll", b"o"]

    dropped = storage.open_write("small/b")
    dropped.write(b"x")
    dropped.abort()
    dropped.abort()
    assert storage.exists("small/b") is False

    empty = storage.open_write("small/empty")
    empty.commit()
    assert storage.read("small/empty") == b""
    assert list(storage.read_chunks("small/empty")) == []
    with pytest.raises(NotFoundError):
        storage.read_chunks("small/none")
    assert _open_uploads(storage) == []


def test_a_multipart_stream_commits_replaces_and_aborts_cleanly(
    storage: FsspecStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Five MiB is s3fs's smallest part: six 1 MiB writes make one full part and a rest."""
    monkeypatch.setattr("lakana_core.storage.fsspec_storage.STREAM_BLOCK_BYTES", 5 * MIB)
    piece = bytes(range(256)) * (MIB // 256)
    storage.write("multi/x", b"old")

    writer = storage.open_write("multi/x")
    for _ in range(6):
        writer.write(piece)
    assert storage.read("multi/x") == b"old"
    assert len(_open_uploads(storage)) == 1
    writer.commit()
    assert _open_uploads(storage) == []
    sizes = [len(chunk) for chunk in storage.read_chunks("multi/x", chunk_bytes=MIB)]
    assert sizes == [MIB] * 6

    dropped = storage.open_write("multi/y")
    for _ in range(6):
        dropped.write(piece)
    assert len(_open_uploads(storage)) == 1
    dropped.abort()
    assert _open_uploads(storage) == []
    assert storage.exists("multi/y") is False
    del dropped
    gc.collect()
    assert storage.exists("multi/y") is False
    assert _open_uploads(storage) == []


def test_list_children_page_reads_one_page_at_a_time_from_s3(storage: FsspecStorage) -> None:
    """Pages of a real bucket follow the key order and never repeat a folder after a cursor."""
    for path in ("pages/a.csv", "pages/a/x.csv", "pages/a/y.csv", "pages/b.csv", "pages/c/z"):
        storage.write(path, b"12345"[: len(path) % 5 + 1])

    first = storage.list_children_page("pages", after=None, limit=2)
    assert first == [ChildEntry("a.csv", False, 2), ChildEntry("a", True, None)]
    rest = storage.list_children_page("pages", after=first[-1].key, limit=5)
    assert rest == [ChildEntry("b.csv", False, 2), ChildEntry("c", True, None)]
    assert storage.list_children_page("nope", after=None, limit=5) == []
