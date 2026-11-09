"""Tests for the storage abstraction."""

import errno
import logging
import os
import tracemalloc
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fsspec.implementations.local import LocalFileSystem
from fsspec.implementations.memory import MemoryFile, MemoryFileSystem
from fsspec.spec import AbstractBufferedFile
from pyiceberg.io.pyarrow import PyArrowFileIO

from lakana_core.config import StorageConfig
from lakana_core.errors import ConflictError, NotFoundError, ValidationError
from lakana_core.storage import FsspecStorage, build_file_io, build_storage
from lakana_core.storage.fsspec_storage import (
    _modified_at,
    _RemoteStreamWriter,
    _translate_s3_options,
    url_prefix_from_signed,
)


@pytest.fixture
def config(tmp_path: Path) -> StorageConfig:
    """Provide a local-filesystem storage configuration.

    Parameters
    ----------
    tmp_path
        Pytest-provided temporary directory.

    Returns
    -------
    StorageConfig
        Configuration rooted at the temporary directory.
    """
    return StorageConfig(scheme="file", root=f"file://{tmp_path}", options={})


def test_write_then_read_round_trips(config: StorageConfig) -> None:
    """Bytes written to a path read back unchanged."""
    storage = build_storage(config)
    storage.write("a/b.txt", b"hello")
    assert storage.read("a/b.txt") == b"hello"


def test_exists_reflects_writes_and_deletes(config: StorageConfig) -> None:
    """Existence tracks the lifecycle of an object."""
    storage = build_storage(config)
    assert storage.exists("gone.txt") is False
    storage.write("gone.txt", b"x")
    assert storage.exists("gone.txt") is True
    storage.delete("gone.txt")
    assert storage.exists("gone.txt") is False


def test_reading_a_missing_path_raises_not_found(config: StorageConfig) -> None:
    """A missing object raises the domain error, not an fsspec error."""
    storage = build_storage(config)
    with pytest.raises(NotFoundError):
        storage.read("nope.txt")


def test_deleting_a_missing_path_raises_not_found(config: StorageConfig) -> None:
    """Deleting something absent raises the domain error."""
    storage = build_storage(config)
    with pytest.raises(NotFoundError):
        storage.delete("nope.txt")


def test_list_returns_paths_relative_to_the_root(config: StorageConfig) -> None:
    """Listing yields root-relative paths, not absolute ones."""
    storage = build_storage(config)
    storage.write("p/one.txt", b"1")
    storage.write("p/two.txt", b"2")
    storage.write("other/three.txt", b"3")
    assert sorted(storage.list("p")) == ["p/one.txt", "p/two.txt"]


@pytest.mark.parametrize("prefix", ["", ".", "/"])
def test_list_of_the_root_yields_clean_paths(config: StorageConfig, prefix: str) -> None:
    """Listing the whole root does not prefix results with `./`.

    `list("a")` already yields clean root-relative paths; `list("")`,
    `list(".")`, and `list("/")` are all ways of asking for "everything
    under the root" and must agree with that, rather than leaking a
    `./`-prefixed path that is an artifact of how the root itself was
    resolved to a backend path.
    """
    storage = build_storage(config)
    storage.write("a/b.txt", b"1")
    storage.write("c.txt", b"2")
    assert sorted(storage.list(prefix)) == ["a/b.txt", "c.txt"]


def test_list_on_a_nonexistent_prefix_yields_nothing(config: StorageConfig) -> None:
    """Listing a prefix that was never written yields no paths."""
    storage = build_storage(config)
    assert list(storage.list("never-written")) == []


def test_storage_resolver_and_resolved_storage_are_exported_from_the_package() -> None:
    """`lakana_core.storage` re-exports `StorageResolver` and `ResolvedStorage`.

    The interface contract
    lists both under `lakana_core.storage`, but the package's `__init__` only
    re-exported `FsspecStorage`, `Storage`, `build_file_io`, and
    `build_storage`, forcing callers to reach into the `resolver` submodule
    directly.
    """
    from lakana_core.storage import ResolvedStorage, StorageResolver

    assert StorageResolver.__module__ == "lakana_core.storage.resolver"
    assert ResolvedStorage.__module__ == "lakana_core.storage.resolver"


def test_build_file_io_returns_a_usable_pyiceberg_io(config: StorageConfig) -> None:
    """The pyiceberg FileIO can write and read through the same root."""
    io = build_file_io(config)
    location = f"{config.root}/io.txt"
    with io.new_output(location).create() as f:
        f.write(b"iceberg")
    with io.new_input(location).open() as f:
        assert f.read() == b"iceberg"


def test_build_file_io_returns_pyarrow_without_a_fallback_warning(
    config: StorageConfig, caplog: pytest.LogCaptureFixture
) -> None:
    """`build_file_io` returns `PyArrowFileIO`, with pyarrow installed as a real dependency.

    pyiceberg only picks up pyarrow through its own optional "pyarrow"
    extra, which lakana-core does not request; without pyarrow declared as a
    direct dependency, `load_file_io` cannot import `PyArrowFileIO`, logs a
    "Could not initialize FileIO" warning on every call, and silently falls
    back to `FsspecFileIO` — so the tests would exercise a different
    implementation than a real deployment, which also lacks pyarrow unless
    something else happens to pull it in.
    """
    with caplog.at_level(logging.WARNING, logger="pyiceberg.io"):
        io = build_file_io(config)
    assert isinstance(io, PyArrowFileIO)
    assert "Could not initialize FileIO" not in caplog.text


def test_storage_write_is_visible_through_file_io(config: StorageConfig) -> None:
    """Storage and FileIO address the same bytes: write via Storage, read via FileIO."""
    storage = build_storage(config)
    io = build_file_io(config)
    storage.write("shared.txt", b"written by storage")
    with io.new_input(f"{config.root}/shared.txt").open() as f:
        assert f.read() == b"written by storage"


def test_file_io_write_is_visible_through_storage(config: StorageConfig) -> None:
    """Storage and FileIO address the same bytes: write via FileIO, read via Storage."""
    storage = build_storage(config)
    io = build_file_io(config)
    with io.new_output(f"{config.root}/shared.txt").create() as f:
        f.write(b"written by file_io")
    assert storage.read("shared.txt") == b"written by file_io"


def test_read_rejects_a_path_that_escapes_the_root(config: StorageConfig) -> None:
    """A leading ``..`` segment is rejected rather than resolved against the root."""
    storage = build_storage(config)
    with pytest.raises(ValidationError):
        storage.read("../outside.txt")


def test_write_rejects_a_path_that_escapes_the_root(config: StorageConfig) -> None:
    """A leading ``..`` segment is rejected rather than resolved against the root."""
    storage = build_storage(config)
    with pytest.raises(ValidationError):
        storage.write("../outside.txt", b"malicious")


def test_delete_rejects_a_path_that_escapes_the_root(config: StorageConfig) -> None:
    """A leading ``..`` segment is rejected rather than resolved against the root."""
    storage = build_storage(config)
    with pytest.raises(ValidationError):
        storage.delete("../outside.txt")


def test_exists_rejects_a_path_that_escapes_the_root(config: StorageConfig) -> None:
    """A leading ``..`` segment is rejected rather than resolved against the root."""
    storage = build_storage(config)
    with pytest.raises(ValidationError):
        storage.exists("../outside.txt")


def test_write_does_not_leak_outside_the_root(config: StorageConfig, tmp_path: Path) -> None:
    """A rejected escaping write must not have touched anything outside the root."""
    storage = build_storage(config)
    with pytest.raises(ValidationError):
        storage.write("../outside.txt", b"malicious")
    assert not (tmp_path.parent / "outside.txt").exists()


def test_interior_dot_dot_that_stays_inside_the_root_is_allowed(config: StorageConfig) -> None:
    """A path with an interior ``..`` that normalises inside the root is legitimate."""
    storage = build_storage(config)
    storage.write("a/../b.txt", b"hi")
    assert storage.read("b.txt") == b"hi"


def test_absolute_path_is_confined_to_the_root(config: StorageConfig, tmp_path: Path) -> None:
    """An absolute-looking path is treated as root-relative, not escaping the root."""
    storage = build_storage(config)
    storage.write("/etc/passwd", b"not the real one")
    assert (tmp_path / "etc" / "passwd").read_bytes() == b"not the real one"


def test_exists_is_false_for_the_root_directory(config: StorageConfig) -> None:
    """`exists` agrees with `read`/`delete` that a directory is not an object.

    The root always exists as a directory on the backend, but `read("")`
    and `delete("")` both treat a directory as `NotFoundError` — an object
    store has no directories, only keys. `exists` must answer consistently
    rather than reporting the root present when nothing could actually be
    read or deleted there.
    """
    storage = build_storage(config)
    assert storage.exists("") is False


def test_exists_is_false_for_a_nested_directory(config: StorageConfig) -> None:
    """A directory created implicitly by a nested write is not itself an object."""
    storage = build_storage(config)
    storage.write("a/b.txt", b"nested")
    assert storage.exists("a") is False


def test_reading_the_root_itself_raises_not_found(config: StorageConfig) -> None:
    """Reading a directory raises the domain error, not IsADirectoryError."""
    storage = build_storage(config)
    with pytest.raises(NotFoundError):
        storage.read("")


def test_deleting_the_root_itself_raises_not_found(config: StorageConfig) -> None:
    """Deleting a directory raises the domain error, not IsADirectoryError."""
    storage = build_storage(config)
    with pytest.raises(NotFoundError):
        storage.delete("")


def _unlink_as_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the local filesystem's `rm_file` refuse a directory as macOS does (EPERM, not EISDIR).

    Parameters
    ----------
    monkeypatch
        Pytest's monkeypatch.
    """
    real = LocalFileSystem.rm_file

    def rm_file(self: LocalFileSystem, path: str) -> None:
        if os.path.isdir(path):
            raise PermissionError(errno.EPERM, "Operation not permitted", path)
        real(self, path)

    monkeypatch.setattr(LocalFileSystem, "rm_file", rm_file)


@pytest.mark.parametrize("path", ["", "dir"])
def test_deleting_a_directory_raises_not_found_where_unlink_says_eperm(
    config: StorageConfig, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """On macOS `unlink` of a directory is EPERM; the caller gets the same error as on Linux."""
    storage = build_storage(config)
    storage.write("dir/a", b"x")
    _unlink_as_on_macos(monkeypatch)
    with pytest.raises(NotFoundError):
        storage.delete(path)
    assert storage.read("dir/a") == b"x"


def test_a_denied_delete_on_an_object_store_stays_a_permission_error() -> None:
    """On S3 `x` can be an object and the prefix of `x/y`: a denied delete is not "no object"."""
    storage = FsspecStorage("memory://denied-lake", {})

    def refuse(path: str) -> None:
        raise PermissionError(errno.EACCES, "Access Denied", path)

    storage._fs = MemoryFileSystem(skip_instance_cache=True)
    storage._fs.rm_file = refuse
    storage._fs.isdir = lambda path: True
    with pytest.raises(PermissionError):
        storage.delete("x")


def test_a_permission_error_on_a_file_is_not_mistaken_for_a_directory(
    config: StorageConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a directory turns EPERM into "no object"; a refused file stays a refusal."""
    storage = build_storage(config)
    storage.write("a", b"x")

    def refuse(self: LocalFileSystem, path: str) -> None:
        raise PermissionError(errno.EPERM, "Operation not permitted", path)

    monkeypatch.setattr(LocalFileSystem, "rm_file", refuse)
    with pytest.raises(PermissionError):
        storage.delete("a")


def test_writing_through_an_existing_file_raises_conflict(config: StorageConfig) -> None:
    """Writing beneath a path that is already a file raises ConflictError."""
    storage = build_storage(config)
    storage.write("a", b"file")
    with pytest.raises(ConflictError):
        storage.write("a/b", b"nested")


def test_writing_to_a_path_that_is_already_a_directory_raises_conflict(
    config: StorageConfig,
) -> None:
    """Writing directly to a path that already exists as a directory raises `ConflictError`.

    `write` promises callers never see backend exceptions, but opening a
    directory for writing raises the backend's raw `IsADirectoryError`
    unless translated, just as the reverse case (a segment already a file)
    is translated to `ConflictError`.
    """
    storage = build_storage(config)
    storage.write("a/b.txt", b"nested")
    with pytest.raises(ConflictError):
        storage.write("a", b"file")


def test_write_rejects_a_path_containing_a_nul_byte(config: StorageConfig) -> None:
    """A NUL byte is never a legitimate path component."""
    storage = build_storage(config)
    with pytest.raises(ValidationError):
        storage.write("a\x00.txt", b"x")


def test_read_rejects_a_path_containing_a_nul_byte(config: StorageConfig) -> None:
    """A NUL byte is never a legitimate path component."""
    storage = build_storage(config)
    with pytest.raises(ValidationError):
        storage.read("a\x00.txt")


def test_delete_rejects_a_path_containing_a_nul_byte(config: StorageConfig) -> None:
    """A NUL byte is never a legitimate path component."""
    storage = build_storage(config)
    with pytest.raises(ValidationError):
        storage.delete("a\x00.txt")


def test_exists_rejects_a_path_containing_a_nul_byte(config: StorageConfig) -> None:
    """A NUL byte must raise, not be silently reported as absent."""
    storage = build_storage(config)
    with pytest.raises(ValidationError):
        storage.exists("a\x00.txt")


def test_list_rejects_a_prefix_containing_a_nul_byte(config: StorageConfig) -> None:
    """A NUL byte must raise on iteration, since ``list`` is a generator."""
    storage = build_storage(config)
    with pytest.raises(ValidationError):
        list(storage.list("a\x00.txt"))


def test_resolve_read_url_returns_the_local_path_unsigned(tmp_path: Path) -> None:
    """Local mode has nothing to sign — the resolved path is used as-is."""
    storage = FsspecStorage(f"file://{tmp_path}", {})
    storage.write("a/b.parquet", b"data")

    url = storage.resolve_read_url("a/b.parquet", ttl_seconds=60)

    assert url == f"{tmp_path}/a/b.parquet"


def test_a_local_read_prefix_is_the_absolute_directory_with_a_slash(tmp_path: Path) -> None:
    """Every local path `resolve_read_url` returns below the directory starts with the prefix."""
    storage = FsspecStorage(f"file://{tmp_path}", {})

    prefix = storage.read_url_prefix("managed/ns/t-1")

    assert prefix == f"{tmp_path}/managed/ns/t-1/"
    assert storage.resolve_read_url("managed/ns/t-1/data/a.parquet", ttl_seconds=60).startswith(
        prefix
    )


@pytest.mark.parametrize("path", ["", ".", "/", "a/..", "../x", "a/../../x", "a\x00b"])
def test_a_read_prefix_of_the_root_or_outside_it_is_refused(tmp_path: Path, path: str) -> None:
    """The root itself and anything outside it never become a readable prefix (fail closed)."""
    storage = FsspecStorage(f"file://{tmp_path}", {})

    with pytest.raises(ValidationError):
        storage.read_url_prefix(path)


def _s3_storage() -> FsspecStorage:
    """Build an S3 storage with dummy keys; signing a URL needs no network.

    Returns
    -------
    FsspecStorage
        Rooted at ``s3://bucket/root`` behind a path-style endpoint on a closed port.
    """
    return FsspecStorage(
        "s3://bucket/root",
        {
            "s3.endpoint": "http://127.0.0.1:9",
            "s3.access-key-id": "AKIAEXAMPLE",
            "s3.secret-access-key": "dummy",
            "s3.region": "us-east-1",
        },
    )


def test_a_remote_read_prefix_is_the_signed_urls_path_without_query() -> None:
    """Path style: the prefix is scheme, host, port and the directory, as the signer writes it."""
    storage = _s3_storage()

    prefix = storage.read_url_prefix("managed/ns/t-1")
    url = storage.resolve_read_url("managed/ns/t-1/data/a b.parquet", ttl_seconds=60)

    assert prefix == "http://127.0.0.1:9/bucket/root/managed/ns/t-1/"
    assert url.startswith(prefix)
    assert "?" not in prefix
    assert "Signature" not in prefix


@pytest.mark.parametrize(
    "signed",
    [
        "http://h:9/b/root/t/other?sig=1",
        "http://h:9/b/root/t/lakana-read-prefix/x",
        "ftp://h/b/root/t/lakana-read-prefix",
        "http://user@h/b/root/t/lakana-read-prefix",
        "http:///b/root/t/lakana-read-prefix",
        "http://h/lakana-read-prefix",
        "/local/b/root/t/lakana-read-prefix",
    ],
)
def test_a_signed_url_of_an_unexpected_shape_is_refused(signed: str) -> None:
    """Anything but ``http(s)://host[:port]/<path>/<sentinel>`` raises (fail closed)."""
    with pytest.raises(ValidationError, match="read prefix"):
        url_prefix_from_signed(signed)


def test_the_prefix_of_a_well_formed_signed_url_drops_query_and_fragment() -> None:
    """Query string and fragment (an S3 signature, an Azure SAS) are not part of the prefix."""
    assert (
        url_prefix_from_signed(
            "https://acct.blob.core.windows.net/c/root/t/lakana-read-prefix?sv=1#f"
        )
        == "https://acct.blob.core.windows.net/c/root/t/"
    )


def test_translate_s3_options_maps_all_recognised_keys() -> None:
    """Every pyiceberg-style S3 option this function knows about translates.

    `s3.region` is the one key the S3 integration test never exercises
    (that test only sets `s3.endpoint`, `s3.access-key-id`, and
    `s3.secret-access-key`), so this is its only coverage.
    """
    translated = _translate_s3_options(
        {
            "s3.endpoint": "http://s3.example:9000",
            "s3.access-key-id": "AKIA",
            "s3.secret-access-key": "secret",
            "s3.region": "us-east-1",
        }
    )

    assert translated == {
        "endpoint_url": "http://s3.example:9000",
        "key": "AKIA",
        "secret": "secret",
        "client_kwargs": {"region_name": "us-east-1"},
    }


def test_translate_s3_options_drops_unrecognised_keys() -> None:
    """A key this function does not know about is dropped, not passed through."""
    translated = _translate_s3_options({"s3.unknown-option": "value"})

    assert translated == {}


def test_list_objects_reports_size_and_an_aware_modification_time(config: StorageConfig) -> None:
    """Maintenance needs each object's age and size."""
    storage = build_storage(config)
    storage.write("t/data/a.parquet", b"12345")

    [found] = list(storage.list_objects("t"))

    assert found.path == "t/data/a.parquet"
    assert found.size == 5
    assert found.modified_at is not None
    assert found.modified_at.tzinfo is not None
    assert abs(datetime.now(UTC) - found.modified_at) < timedelta(minutes=5)


def test_list_objects_on_a_missing_prefix_yields_nothing(config: StorageConfig) -> None:
    """A table that never wrote a data file has nothing to list."""
    assert list(build_storage(config).list_objects("nope")) == []


def test_modified_at_reads_either_backends_shape() -> None:
    """Local filesystems report `mtime`, s3fs `LastModified` (naive or aware), else unknown."""
    aware = datetime(2026, 1, 1, tzinfo=UTC)

    assert _modified_at({"mtime": 0}) == datetime(1970, 1, 1, tzinfo=UTC)
    assert _modified_at({"LastModified": aware}) == aware
    assert _modified_at({"LastModified": aware.replace(tzinfo=None)}) == aware
    assert _modified_at({}) is None


def test_list_objects_on_a_non_local_backend_uses_the_backends_listing() -> None:
    """Only the local filesystem gets the symlink-safe walk; others use `find`."""
    storage = FsspecStorage("memory://lake", {})
    storage.write("t/data/a.parquet", b"abc")

    assert [(o.path, o.size) for o in storage.list_objects("t")] == [("t/data/a.parquet", 3)]
    assert list(storage.list_objects("nope")) == []


def test_a_stream_is_written_chunk_by_chunk_and_appears_only_on_commit(
    config: StorageConfig, tmp_path: Path
) -> None:
    """Nothing is buffered whole, and nothing is visible before commit."""
    storage = build_storage(config)
    writer = storage.open_write("artifacts/a/blobs/x")
    writer.write(b"hel")
    parts = [p for p in os.listdir(tmp_path / "artifacts/a/blobs") if p.endswith(".part")]
    assert len(parts) == 1
    assert parts[0].startswith(".x.")
    assert os.path.getsize(tmp_path / "artifacts/a/blobs" / parts[0]) in (0, 3)
    assert storage.exists("artifacts/a/blobs/x") is False
    writer.write(b"lo")
    writer.commit()
    assert storage.read("artifacts/a/blobs/x") == b"hello"
    assert os.listdir(tmp_path / "artifacts/a/blobs") == ["x"]


def test_an_aborted_stream_leaves_nothing(config: StorageConfig, tmp_path: Path) -> None:
    storage = build_storage(config)
    writer = storage.open_write("a/b")
    writer.write(b"junk")
    writer.abort()
    writer.abort()
    assert os.listdir(tmp_path / "a") == []
    assert storage.exists("a/b") is False
    with pytest.raises(ValueError, match="already closed"):
        writer.write(b"more")


def test_an_aborted_stream_keeps_the_object_it_would_have_replaced(
    config: StorageConfig, tmp_path: Path
) -> None:
    storage = build_storage(config)
    storage.write("a/b", b"old")
    writer = storage.open_write("a/b")
    writer.write(b"new")
    writer.abort()
    assert storage.read("a/b") == b"old"
    assert os.listdir(tmp_path / "a") == ["b"]


def test_abort_after_commit_keeps_the_object(config: StorageConfig) -> None:
    storage = build_storage(config)
    writer = storage.open_write("a/b")
    writer.write(b"kept")
    writer.commit()
    writer.abort()
    assert storage.read("a/b") == b"kept"


def test_commit_replaces_an_existing_object(config: StorageConfig) -> None:
    storage = build_storage(config)
    storage.write("a/b", b"old")
    writer = storage.open_write("a/b")
    writer.write(b"new")
    assert storage.read("a/b") == b"old"
    writer.commit()
    assert storage.read("a/b") == b"new"


def test_committing_twice_is_an_error(config: StorageConfig) -> None:
    storage = build_storage(config)
    writer = storage.open_write("a/b")
    writer.commit()
    with pytest.raises(ValueError, match="already closed"):
        writer.commit()


def test_open_write_refuses_a_path_through_a_file_or_onto_a_directory(
    config: StorageConfig,
) -> None:
    storage = build_storage(config)
    storage.write("a", b"file")
    with pytest.raises(ConflictError):
        storage.open_write("a/b")
    storage.write("d/x", b"1")
    with pytest.raises(ConflictError):
        storage.open_write("d")
    with pytest.raises(ValidationError):
        storage.open_write("../escape")
    with pytest.raises(ValidationError):
        storage.open_write("nul\x00byte")


def test_open_write_does_not_follow_a_symlinked_directory(
    config: StorageConfig, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    storage = build_storage(StorageConfig(scheme="file", root=f"file://{root}", options={}))
    os.symlink(outside, root / "link")
    with pytest.raises(ValidationError):
        storage.open_write("link/x")
    assert os.listdir(outside) == []


def test_commit_replaces_a_symlink_at_the_final_name_without_following_it(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")
    root = tmp_path / "root"
    root.mkdir()
    storage = build_storage(StorageConfig(scheme="file", root=f"file://{root}", options={}))
    os.symlink(outside, root / "x")
    writer = storage.open_write("x")
    writer.write(b"new")
    writer.commit()
    assert outside.read_bytes() == b"secret"
    assert not os.path.islink(root / "x")
    assert storage.read("x") == b"new"


def test_the_temporary_name_is_never_a_symlink_followed(
    config: StorageConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A planted link at the temporary name makes `open_write` fail, never write through it."""
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")
    (tmp_path / "d").mkdir()
    monkeypatch.setattr("secrets.token_hex", lambda _n: "0" * 16)
    os.symlink(outside, tmp_path / "d" / ".x.0000000000000000.part")
    storage = build_storage(config)
    with pytest.raises(ConflictError):
        storage.open_write("d/x")
    assert outside.read_bytes() == b"secret"


def test_read_chunks_yields_bounded_pieces(config: StorageConfig) -> None:
    storage = build_storage(config)
    storage.write("a/b", b"hello")
    assert list(storage.read_chunks("a/b", chunk_bytes=2)) == [b"he", b"ll", b"o"]
    storage.write("a/empty", b"")
    assert list(storage.read_chunks("a/empty")) == []


def test_read_chunks_raises_not_found_when_called(config: StorageConfig) -> None:
    storage = build_storage(config)
    with pytest.raises(NotFoundError):
        storage.read_chunks("nope")
    storage.write("d/x", b"1")
    with pytest.raises(NotFoundError):
        storage.read_chunks("d")


def test_streaming_a_few_mebibytes_never_holds_the_whole_content(
    config: StorageConfig, tmp_path: Path
) -> None:
    """Memory is the point: 4 MiB goes through in 64 KiB pieces, peak stays well below 4 MiB."""
    total, piece = 4 * 1024 * 1024, 64 * 1024
    block = bytes(range(256)) * (piece // 256)
    sizes: list[int] = []

    def source() -> Iterator[bytes]:
        for _ in range(total // piece):
            sizes.append(len(block))
            yield block

    storage = build_storage(config)
    tracemalloc.start()
    try:
        writer = storage.open_write("big/blob")
        for data in source():
            writer.write(data)
        writer.commit()
        received = []
        for chunk in storage.read_chunks("big/blob", chunk_bytes=piece):
            received.append(len(chunk))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert sizes == [piece] * (total // piece)
    assert received == [piece] * (total // piece)
    assert os.path.getsize(tmp_path / "big/blob") == total
    assert peak < total // 4


class _DeferredMemoryFileSystem(MemoryFileSystem):
    """A memory filesystem that, like s3fs, hides a file opened with `autocommit=False`.

    fsspec's own memory filesystem ignores `autocommit` and registers a file
    when it is opened, so it cannot stand in for an object store without this.
    """

    def _open(
        self,
        path: str,
        mode: str = "rb",
        block_size: int | None = None,
        autocommit: bool = True,
        cache_options: dict[str, object] | None = None,
        **kwargs: object,
    ) -> MemoryFile:
        """Open a file, deferring its appearance until `commit` when asked to.

        Parameters
        ----------
        path
            The path.
        mode
            The open mode.
        block_size
            Ignored.
        autocommit
            ``False`` keeps a written file invisible until its `commit`.
        cache_options
            Ignored.
        **kwargs
            Passed on.

        Returns
        -------
        MemoryFile
            The file.
        """
        self._intrans = not autocommit
        try:
            return super()._open(path, mode, block_size, autocommit, cache_options, **kwargs)
        finally:
            self._intrans = False


def test_streaming_on_a_non_local_backend_commits_and_discards() -> None:
    """Object stores take the multipart path; `memory://` stands in for it in the unit tier."""
    storage = FsspecStorage("memory://stream-lake", {})
    storage._fs = _DeferredMemoryFileSystem(global_store=False, skip_instance_cache=True)
    writer = storage.open_write("t/a")
    writer.write(b"ab")
    writer.write(b"c")
    assert storage.exists("t/a") is False
    writer.commit()
    assert b"".join(storage.read_chunks("t/a", chunk_bytes=2)) == b"abc"
    dropped = storage.open_write("t/b")
    dropped.write(b"x")
    dropped.abort()
    dropped.abort()
    assert storage.exists("t/b") is False
    done = storage.open_write("t/c")
    done.commit()
    done.abort()
    assert storage.exists("t/c") is True
    with pytest.raises(ValueError, match="already closed"):
        done.commit()
    with pytest.raises(NotFoundError):
        storage.read_chunks("t/none")


def test_a_failed_local_commit_removes_the_temporary_file(
    config: StorageConfig, tmp_path: Path
) -> None:
    storage = build_storage(config)
    writer = storage.open_write("a/b")
    writer.write(b"x")
    os.mkdir(tmp_path / "a" / "b")  # a directory appears at the final name meanwhile
    with pytest.raises(OSError):
        writer.commit()
    assert os.listdir(tmp_path / "a") == ["b"]
    assert os.path.isdir(tmp_path / "a" / "b")


def test_a_failed_remote_commit_aborts_the_upload() -> None:
    storage = FsspecStorage("memory://stream-lake", {})
    storage._fs = _DeferredMemoryFileSystem(global_store=False, skip_instance_cache=True)
    writer = storage.open_write("t/a")
    writer.write(b"x")
    calls: list[str] = []
    assert isinstance(writer, _RemoteStreamWriter)
    handle = writer._handle

    def fail() -> None:
        raise RuntimeError("upload failed")

    handle.commit = fail
    handle.discard = lambda: calls.append("discard")
    with pytest.raises(RuntimeError, match="upload failed"):
        writer.commit()
    assert calls == ["discard"]
    writer.abort()
    assert calls == ["discard"]
    with pytest.raises(ValueError, match="already closed"):
        writer.write(b"y")


def test_an_aborted_buffered_file_is_marked_closed_so_collecting_it_uploads_nothing() -> None:
    """s3fs handles are `AbstractBufferedFile`s; aborting one must stop a later flush."""
    calls: list[str] = []

    class Handle(AbstractBufferedFile):
        def __init__(self) -> None:
            self._closed = False

        def discard(self) -> None:
            calls.append("discard")

        def close(self) -> None:
            calls.append("close")

    handle = Handle()
    _RemoteStreamWriter(handle).abort()
    assert calls == ["discard"]
    assert handle.closed is True


def test_list_children_names_one_level_and_says_which_are_directories(
    config: StorageConfig, tmp_path: Path
) -> None:
    """The sweep walks `artifacts/` one level at a time, never listing every object."""
    storage = build_storage(config)
    storage.write("artifacts/aa/blobs/x", b"1")
    storage.write("artifacts/stray.txt", b"2")
    outside = tmp_path.parent / "outside-children"
    outside.mkdir(exist_ok=True)
    os.symlink(outside, tmp_path / "artifacts" / "linked")

    assert sorted(storage.list_children("artifacts")) == [("aa", True), ("stray.txt", False)]
    assert list(storage.list_children("nope")) == []
    assert list(storage.list_children("artifacts/stray.txt")) == []


def test_list_children_yields_nothing_below_a_symlinked_directory(
    config: StorageConfig, tmp_path: Path
) -> None:
    outside = tmp_path.parent / "outside-children-2"
    (outside / "inner").mkdir(parents=True, exist_ok=True)
    os.symlink(outside, tmp_path / "link")
    assert list(build_storage(config).list_children("link")) == []


def test_list_children_on_a_non_local_backend_uses_the_backends_listing() -> None:
    storage = FsspecStorage("memory://children-lake", {})
    storage.write("artifacts/aa/blobs/x", b"1")
    storage.write("artifacts/stray.txt", b"2")
    assert sorted(storage.list_children("artifacts")) == [("aa", True), ("stray.txt", False)]
    assert list(storage.list_children("nope")) == []
    assert list(storage.list_children("artifacts/stray.txt")) == []


def test_a_listing_drops_the_backends_cached_directories_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long-lived s3fs keeps directory listings; the sweep must see current state."""
    storage = FsspecStorage("memory://fresh-lake", {})
    storage.write("artifacts/aa/blobs/x", b"1")
    dropped: list[str] = []
    monkeypatch.setattr(storage._fs, "invalidate_cache", dropped.append)
    list(storage.list_objects("artifacts"))
    list(storage.list_children("artifacts"))
    assert dropped == [f"{storage._base}/artifacts"] * 2


def test_list_objects_skips_a_file_that_vanishes_while_the_directory_is_walked(
    config: StorageConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An upload that aborts removes its temporary file at any moment; a listing must not fail."""
    storage = build_storage(config)
    storage.write("t/keep", b"1")
    storage.write("t/gone", b"2")
    real = os.stat

    def stat(path: str, *, follow_symlinks: bool = True) -> os.stat_result:
        if str(path).endswith("/gone"):
            raise FileNotFoundError(path)
        return real(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "stat", stat)
    assert [found.path for found in storage.list_objects("t")] == ["t/keep"]
