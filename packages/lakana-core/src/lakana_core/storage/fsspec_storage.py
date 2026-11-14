"""An fsspec-backed implementation of the storage protocol."""

import contextlib
import errno
import io
import os
import posixpath
import secrets
import stat
import urllib.parse
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

import fsspec
from fsspec.implementations.local import LocalFileSystem
from fsspec.spec import AbstractBufferedFile
from pyiceberg.io import FileIO, load_file_io

from lakana_core.config import StorageConfig
from lakana_core.errors import ConflictError, NotFoundError, ValidationError
from lakana_core.storage.base import (
    PART_SUFFIX,
    STREAM_BLOCK_BYTES,
    STREAM_CHUNK_BYTES,
    Storage,
    StoredObject,
    StreamWriter,
)

_S3_OPTION_TRANSLATION = {
    "s3.endpoint": "endpoint_url",
    "s3.access-key-id": "key",
    "s3.secret-access-key": "secret",
}
"""Maps pyiceberg's S3 property names to s3fs's constructor keyword names.

`StorageConfig.options` is populated with pyiceberg's names (see
`lakana_core.storage.resolver`'s docstring and `build_file_io`, which passes the
same dict straight through to pyiceberg's `load_file_io`) because that is the
vocabulary the environment's storage configuration is written in. `s3fs`,
which `FsspecStorage` builds through `fsspec.core.url_to_fs`, uses a different
vocabulary for the same three settings, so without this translation an S3
environment's `Storage` silently tries to build an anonymous, endpoint-less
S3 connection regardless of what was configured.
"""


def _translate_s3_options(options: Mapping[str, str]) -> dict[str, object]:
    """Translate pyiceberg-style S3 options into s3fs constructor keywords.

    Parameters
    ----------
    options
        The environment's storage options, in pyiceberg's vocabulary.

    Returns
    -------
    dict
        Keyword arguments `s3fs.S3FileSystem` understands, for the subset of
        `options` this module knows how to translate. `s3.region`, if present,
        becomes `client_kwargs={"region_name": ...}`. Keys this function does
        not recognise are dropped, not passed through — an untranslated
        pyiceberg-style key would not mean what it says to `s3fs`.
    """
    translated: dict[str, object] = {
        fsspec_key: options[iceberg_key]
        for iceberg_key, fsspec_key in _S3_OPTION_TRANSLATION.items()
        if iceberg_key in options
    }
    if "s3.region" in options:
        translated["client_kwargs"] = {"region_name": options["s3.region"]}
    return translated


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
"""`O_NOFOLLOW` where the platform has it (not on Windows, where the module must still import)."""
_LOCAL_READ_FLAGS = (
    os.O_RDONLY | _NOFOLLOW | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
)
"""How the local backend opens an object to read it: never through a link at its own name, and
never blocking on a pipe someone put there."""


def _sync_directory(directory: str) -> None:
    """Write a directory's entries to disk, so that a rename in it survives a power loss.

    Parameters
    ----------
    directory
        The directory.
    """
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


_TEMP_NAME_BYTES = 100
"""Bytes of an object's name kept in its temporary name (file systems allow 255 per name)."""


class _LocalStreamWriter:
    """Write to a temporary name next to the target and rename on commit.

    Parameters
    ----------
    final
        The backend path of the object.
    """

    def __init__(self, final: str) -> None:
        directory, name = os.path.split(final)
        self._final = final
        # At most 100 bytes of the name, so that the temporary name fits a 255-byte limit too;
        # the leading dot and the suffix are what a sweep recognises it by.
        kept = os.fsdecode(os.fsencode(name)[:_TEMP_NAME_BYTES])
        self._temp = os.path.join(directory, f".{kept}.{secrets.token_hex(8)}{PART_SUFFIX}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        # 0666 less the process umask, like every other file the backend writes: the operator
        # decides who else may read objects (a worker in the server's group, with umask 027).
        self._handle: io.BufferedWriter | None = os.fdopen(os.open(self._temp, flags, 0o666), "wb")

    def write(self, data: bytes) -> None:
        """Add bytes.

        Parameters
        ----------
        data
            The next piece.

        Raises
        ------
        ValueError
            If the stream was already committed or aborted.
        """
        if self._handle is None:
            raise ValueError("the stream is already closed")
        self._handle.write(data)

    def commit(self) -> None:
        """Flush, sync and rename over the target, then sync the directory.

        The directory's sync makes the rename itself durable: without it, a power loss after
        the call returns can leave the old name (or none), while a database row written next
        already says the object is there.

        Raises
        ------
        ValueError
            If the stream was already committed or aborted.
        """
        handle, self._handle = self._handle, None
        if handle is None:
            raise ValueError("the stream is already closed")
        try:
            try:
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                handle.close()
            os.replace(self._temp, self._final)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self._temp)
            raise
        _sync_directory(os.path.dirname(self._final))

    def abort(self) -> None:
        """Remove the temporary file. A no-op once closed."""
        handle, self._handle = self._handle, None
        if handle is None:
            return
        handle.close()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self._temp)


class _RemoteStreamWriter:
    """Write through an fsspec file opened with ``autocommit=False`` (a multipart upload on S3).

    Parameters
    ----------
    handle
        The open file; its `commit` completes the upload, its `discard` aborts it.
    """

    def __init__(self, handle: Any) -> None:  # ruff: ignore[any-type]
        self._handle = handle
        self._open = True

    def write(self, data: bytes) -> None:
        """Add bytes.

        Parameters
        ----------
        data
            The next piece.

        Raises
        ------
        ValueError
            If the stream was already committed or aborted.
        """
        if not self._open:
            raise ValueError("the stream is already closed")
        self._handle.write(data)

    def commit(self) -> None:
        """Send what is buffered and complete the upload.

        Raises
        ------
        ValueError
            If the stream was already committed or aborted.
        """
        if not self._open:
            raise ValueError("the stream is already closed")
        self._open = False
        try:
            self._handle.close()
            self._handle.commit()
        except BaseException:
            self._discard()
            raise

    def abort(self) -> None:
        """Abort the upload. A no-op once closed."""
        if not self._open:
            return
        self._open = False
        self._discard()

    def _discard(self) -> None:
        """Abort the multipart upload, if one began, and make the handle inert."""
        with contextlib.suppress(Exception):
            self._handle.discard()
        if isinstance(self._handle, AbstractBufferedFile):
            # Otherwise collecting the handle would flush it and, on s3fs, upload it.
            self._handle.closed = True


def _chunks(handle: Any, chunk_bytes: int) -> Iterator[bytes]:  # ruff: ignore[any-type]
    """Yield an open file's contents and close it.

    Parameters
    ----------
    handle
        A file open for reading.
    chunk_bytes
        Most bytes per piece.

    Yields
    ------
    bytes
        The next piece; never an empty one.
    """
    try:
        while chunk := handle.read(chunk_bytes):
            yield chunk
    finally:
        handle.close()


FSSPEC_CHAIN = "::"
"""The separator of an fsspec URL chain: ``a::b`` is filesystem ``a`` layered over ``b``."""
ROOT_CHAIN_MESSAGE = (
    "The storage root {root!r} contains '::', which chains fsspec filesystems: the storage would "
    "read only the part before it, while Lakana compares locations with all of it. A storage "
    "root cannot be an fsspec chain; write it as a plain scheme://path, such as "
    "'file:///data/lakana' or 's3://bucket/lakana'."
)


READ_PREFIX_SENTINEL = "lakana-read-prefix"
"""The name `FsspecStorage.read_url_prefix` signs below a directory and cuts off again. Plain
ASCII, so every signer writes it unchanged."""

_READ_PREFIX_SCHEMES = frozenset({"http", "https"})


def url_prefix_from_signed(signed: str) -> str:
    """Cut a signed URL of `READ_PREFIX_SENTINEL` down to the prefix of its directory.

    Parameters
    ----------
    signed
        What the backend's signer returned for ``<directory>/<sentinel>``.

    Returns
    -------
    str
        ``scheme://host[:port]/<path>/``: the URL without query, fragment and sentinel.

    Raises
    ------
    ValidationError
        If the URL is not ``http(s)://host[:port]/<non-empty path>/<sentinel>`` (another
        scheme, no host, user information, or a path that does not end in the sentinel).
        Fail closed: a shape this function does not know is never turned into a prefix.
    """
    parts = urllib.parse.urlsplit(signed)
    suffix = "/" + READ_PREFIX_SENTINEL
    directory = parts.path[: -len(READ_PREFIX_SENTINEL)]
    if (
        parts.scheme not in _READ_PREFIX_SCHEMES
        or not parts.netloc
        or "@" in parts.netloc
        or not parts.path.endswith(suffix)
        or directory.strip("/") == ""
    ):
        raise ValidationError(
            "The storage signed a URL of a shape Lakana does not know, so no read prefix could "
            "be derived from it; the query is not run. Tell the operator: this storage backend "
            "is not supported for queries."
        )
    return f"{parts.scheme}://{parts.netloc}{directory}"


class FsspecStorage:
    """Storage backed by any filesystem fsspec can open.

    Parameters
    ----------
    root
        Root URI, such as ``file:///var/lakana/dev`` or ``s3://bucket/prefix``.
    options
        Backend-specific options forwarded to fsspec.

    Raises
    ------
    ValidationError
        If `root` contains ``::`` (`ROOT_CHAIN_MESSAGE`): `fsspec.core.url_to_fs` would read
        only the part before it, so a root stored before `check_storage_root` refused it fails
        closed here.
    """

    def __init__(self, root: str, options: Mapping[str, str]) -> None:
        if FSSPEC_CHAIN in root:
            raise ValidationError(ROOT_CHAIN_MESSAGE.format(root=root))
        self._root = root.rstrip("/")
        fs_options = _translate_s3_options(options) if root.startswith("s3://") else dict(options)
        self._fs, self._base = fsspec.core.url_to_fs(self._root, **fs_options)

    def _full(self, path: str) -> str:
        """Resolve a root-relative path to a backend path, confined to the root.

        The path is normalised (collapsing ``.`` and interior ``..`` segments)
        and rejected if it still escapes the root afterwards. This is the
        sole choke point between a caller-supplied path and the filesystem,
        so every method that reads or writes goes through it.

        Parameters
        ----------
        path
            Root-relative path. A leading ``/`` is treated as root-relative,
            not absolute.

        Returns
        -------
        str
            The backend-specific absolute path.

        Raises
        ------
        ValidationError
            If the path contains a NUL byte, or normalises to something
            outside the root, such as one starting with ``..``.
        """
        if "\x00" in path:
            raise ValidationError(f"Path contains a NUL byte: {path!r}.")
        relative = posixpath.normpath(path.lstrip("/"))
        if relative == ".." or relative.startswith("../"):
            raise ValidationError(f"Path escapes the storage root: {path!r}.")
        if relative == ".":
            # "", ".", and "/" all normalise to ".", meaning the root itself.
            # Returning `f"{self._base}/."` instead of `self._base` here
            # resolves to the same directory but makes `list` return paths
            # prefixed with "./", since the trailing "/." changes how the
            # backend reports names relative to that path.
            return self._base
        return f"{self._base}/{relative}"

    def read(self, path: str) -> bytes:
        """Read an object.

        Parameters
        ----------
        path
            Root-relative path.

        Returns
        -------
        bytes
            The object's contents.

        Raises
        ------
        NotFoundError
            If no object exists at that path, the path names a directory
            rather than an object, or (local backend) a link is at its name.
        ValidationError
            If the path contains a NUL byte, escapes the configured storage
            root, or (local backend) passes through a symbolic link.
        """
        full = self._full(path)
        if isinstance(self._fs, LocalFileSystem):
            with os.fdopen(self._open_local(full, path), "rb") as handle:
                return handle.read()
        try:
            return self._fs.cat_file(full)
        except (FileNotFoundError, IsADirectoryError) as exc:
            raise NotFoundError(f"No object at {path!r}.") from exc

    def _open_local(self, full: str, path: str) -> int:
        """Open a local object for reading, refusing what `open_write` refuses.

        Parameters
        ----------
        full
            The backend path, already confined to the root.
        path
            The root-relative path, for messages.

        Returns
        -------
        int
            An open descriptor of a regular file; the caller closes it.

        Raises
        ------
        ValidationError
            If a directory between the root and the object is a symbolic link.
        NotFoundError
            If nothing, a directory, a link or anything but a regular file is at that name: a
            link is not an object, as the listings skip it.
        PermissionError
            If the process may not read the file.
        """
        self._refuse_symlinked_parents(full)
        missing = NotFoundError(f"No object at {path!r}.")
        try:
            descriptor = os.open(full, _LOCAL_READ_FLAGS)
        except PermissionError:
            raise
        except OSError as exc:
            raise missing from exc
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise missing
        return descriptor

    def write(self, path: str, data: bytes) -> None:
        """Write an object, creating parent directories as needed.

        Parameters
        ----------
        path
            Root-relative path.
        data
            Contents to write.

        Raises
        ------
        ConflictError
            If a segment of the path already exists as a file, so it cannot
            also be used as a directory, or if `path` itself already exists
            as a directory.
        ValidationError
            If the path contains a NUL byte, escapes the configured storage
            root, or (local backend) passes through a symbolic link.
        """
        full = self._full(path)
        parent = full.rsplit("/", 1)[0]
        if isinstance(self._fs, LocalFileSystem):
            # The stream's way: a new name opened without following a link, synced, renamed
            # over the object (a link at its name is replaced, never written through).
            stream = self.open_write(path)
            try:
                stream.write(data)
            except BaseException:
                stream.abort()
                raise
            stream.commit()
            return
        try:
            self._fs.makedirs(parent, exist_ok=True)
            with self._fs.open(full, "wb") as handle:
                handle.write(data)
        except FileExistsError as exc:
            raise ConflictError(
                f"Cannot write {path!r}: a segment of the path is already a file."
            ) from exc
        except IsADirectoryError as exc:
            raise ConflictError(
                f"Cannot write {path!r}: it already exists as a directory."
            ) from exc

    def _make_local_parents(self, full: str) -> None:
        """Make the missing directories of a local object, each one durable in its parent.

        Parameters
        ----------
        full
            The backend path, already confined to the root and checked for links on the way.

        Raises
        ------
        NotADirectoryError
            If a component of the way exists and is not a directory.
        """
        if not os.path.isdir(self._base):
            # A root nothing was written to yet: made as fsspec made it, synced into its parent.
            os.makedirs(self._base, exist_ok=True)
            _sync_directory(os.path.dirname(self._base) or "/")
        walked = self._base
        for part in [p for p in full[len(self._base) :].split("/")[:-1] if p]:
            parent, walked = walked, f"{walked}/{part}"
            try:
                info = os.lstat(walked)
            except FileNotFoundError:
                try:
                    os.mkdir(walked)
                except FileExistsError:
                    pass  # made by a concurrent writer: judged below like an old one
                else:
                    _sync_directory(parent)
                    continue
                info = os.lstat(walked)
            if not stat.S_ISDIR(info.st_mode):
                raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), walked)

    def _refuse_symlinked_parents(self, full: str) -> None:
        """Refuse a path whose directories include a symbolic link (local backend).

        Parameters
        ----------
        full
            The backend path, already confined to the root.

        Raises
        ------
        ValidationError
            If a directory between the root and the object is a symlink, which
            could lead outside the root.
        """
        walked = self._base
        for part in [p for p in full[len(self._base) :].split("/")[:-1] if p]:
            walked = f"{walked}/{part}"
            if os.path.islink(walked):
                raise ValidationError(
                    "Path passes through a symbolic link inside the storage root; "
                    "remove the link or write elsewhere."
                )

    def read_url_prefix(self, path: str) -> str:
        """Return the prefix of every URL `resolve_read_url` returns for a path under `path`.

        Parameters
        ----------
        path
            Root-relative path of a directory: a table's location, an external table's claim.

        Returns
        -------
        str
            For a local filesystem, the absolute directory followed by ``/``. For a cloud
            backend, the URL the signer writes for ``<path>/<sentinel>``, without its query
            string, fragment and sentinel: the same signer and encoding as the files', so a
            path-style, virtual-host or SAS URL comes out right without parsing an endpoint.

        Raises
        ------
        ValidationError
            If the path contains a NUL byte, escapes the configured storage root or names the
            root itself (the whole store is never one table's prefix), or if the signed URL
            has a shape `url_prefix_from_signed` refuses.
        """
        full = self._full(path)
        if full == self._base:
            raise ValidationError(
                f"A read prefix must name a directory below the storage root: {path!r}."
            )
        if self._root.startswith("file://"):
            return full + "/"
        # Signing is offline for S3 (SigV2/SigV4 compute the signature locally); the TTL does
        # not matter, the query string is cut off.
        signed = self._fs.sign(f"{full}/{READ_PREFIX_SENTINEL}", expiration=60)
        return url_prefix_from_signed(signed)

    def open_write(self, path: str) -> StreamWriter:
        """Open a stream to an object; see `Storage.open_write`.

        Parameters
        ----------
        path
            Root-relative path.

        Returns
        -------
        StreamWriter
            The stream.

        Raises
        ------
        ConflictError
            If a segment of the path is a file, or `path` is a directory.
        ValidationError
            If the path contains a NUL byte, escapes the storage root, or (local
            backend) passes through a symbolic link.
        """
        full = self._full(path)
        parent = full.rsplit("/", 1)[0]
        local = isinstance(self._fs, LocalFileSystem)
        if local:
            self._refuse_symlinked_parents(full)
        try:
            if local:
                self._make_local_parents(full)
            else:
                self._fs.makedirs(parent, exist_ok=True)
            if self._fs.isdir(full):
                raise ConflictError(f"Cannot write {path!r}: it already exists as a directory.")
            if local:
                return _LocalStreamWriter(full)
            return _RemoteStreamWriter(
                self._fs.open(full, "wb", autocommit=False, block_size=STREAM_BLOCK_BYTES)
            )
        except (FileExistsError, NotADirectoryError) as exc:
            raise ConflictError(
                f"Cannot write {path!r}: a segment of the path is already a file."
            ) from exc
        except IsADirectoryError as exc:  # pragma: no cover -- a directory appeared after the check
            raise ConflictError(
                f"Cannot write {path!r}: it already exists as a directory."
            ) from exc

    def read_chunks(self, path: str, *, chunk_bytes: int = STREAM_CHUNK_BYTES) -> Iterator[bytes]:
        """Read an object in pieces; see `Storage.read_chunks`.

        Parameters
        ----------
        path
            Root-relative path.
        chunk_bytes
            Most bytes per piece.

        Returns
        -------
        Iterator of bytes
            The object's contents in order. The backend file is closed when
            the iterator is exhausted or closed.

        Raises
        ------
        NotFoundError
            If no object exists at that path, or (local backend) a link is at its name.
            Raised by this call, before the iterator is returned.
        ValidationError
            If the path contains a NUL byte, escapes the storage root, or (local backend)
            passes through a symbolic link.
        """
        full = self._full(path)
        if isinstance(self._fs, LocalFileSystem):
            return _chunks(os.fdopen(self._open_local(full, path), "rb"), chunk_bytes)
        if not self._fs.isfile(full):
            raise NotFoundError(f"No object at {path!r}.")
        try:
            handle = self._fs.open(full, "rb", block_size=STREAM_BLOCK_BYTES)
        except (FileNotFoundError, IsADirectoryError) as exc:  # pragma: no cover -- lost a race
            raise NotFoundError(f"No object at {path!r}.") from exc
        return _chunks(handle, chunk_bytes)

    def delete(self, path: str) -> None:
        """Delete an object.

        Parameters
        ----------
        path
            Root-relative path.

        Raises
        ------
        NotFoundError
            If no object exists at that path, or the path names a directory
            rather than an object.
        PermissionError
            If the backend refuses to delete the object.
        ValidationError
            If the path contains a NUL byte, escapes the configured storage
            root, or (local backend) passes through a symbolic link. A link at
            the object's own name is removed, never what it points to.
        """
        full = self._full(path)
        if isinstance(self._fs, LocalFileSystem):
            self._refuse_symlinked_parents(full)
        try:
            self._fs.rm_file(full)
        except (FileNotFoundError, IsADirectoryError) as exc:
            raise NotFoundError(f"No object at {path!r}.") from exc
        except PermissionError as exc:
            # macOS `unlink` refuses a directory with EPERM where Linux says EISDIR. On an object
            # store `x` can be an object and a prefix at once, so a denial there stays a denial.
            if not isinstance(self._fs, LocalFileSystem) or not self._fs.isdir(full):
                raise
            raise NotFoundError(f"No object at {path!r}.") from exc

    def exists(self, path: str) -> bool:
        """Report whether an object exists.

        Parameters
        ----------
        path
            Root-relative path.

        Returns
        -------
        bool
            ``True`` if the object exists. A directory, including the root
            itself, is not an object — an object store has no directories,
            only keys — so this agrees with `read` and `delete`, which both
            raise `NotFoundError` for a directory rather than treating it as
            present.

        Raises
        ------
        ValidationError
            If the path contains a NUL byte, or escapes the configured storage
            root.
        """
        full = self._full(path)
        if isinstance(self._fs, LocalFileSystem):
            return self._local_regular(full)
        return bool(self._fs.isfile(full))

    def _local_regular(self, full: str) -> bool:
        """Say whether a local path is a regular file reached without a link.

        Parameters
        ----------
        full
            The backend path, already confined to the root.

        Returns
        -------
        bool
            ``False`` for a link at the name or on the way, as the reads refuse both.
        """
        walked = self._base
        for part in [p for p in full[len(self._base) :].split("/")[:-1] if p]:
            walked = f"{walked}/{part}"
            if os.path.islink(walked):
                return False
        try:
            return stat.S_ISREG(os.lstat(full).st_mode)
        except OSError:
            return False

    def list(self, prefix: str) -> Iterator[str]:
        """List objects beneath a prefix.

        Parameters
        ----------
        prefix
            Root-relative prefix.

        Yields
        ------
        str
            Root-relative paths, sorted. On the local backend a symlink is skipped, and a
            symlink anywhere between the root and `prefix` yields nothing.

        Raises
        ------
        ValidationError
            If the prefix contains a NUL byte, or escapes the configured storage
            root.
        """
        full = self._full(prefix)
        if isinstance(self._fs, LocalFileSystem):
            if full != self._base and self._local_regular(full):
                yield full[len(self._base) + 1 :]  # a prefix that is one object, as `find` says
                return
            yield from sorted(found.path for found in self._list_local_objects(full))
            return
        if not self._fs.exists(full):
            return
        for found in self._fs.find(full):
            yield str(found)[len(self._base) + 1 :]

    def list_objects(self, prefix: str) -> Iterator[StoredObject]:
        """List objects beneath a prefix, with their size and age.

        Parameters
        ----------
        prefix
            Root-relative prefix.

        Yields
        ------
        StoredObject
            Each object.

        Raises
        ------
        ValidationError
            If the prefix contains a NUL byte, or escapes the configured
            storage root.
        """
        full = self._full(prefix)
        if isinstance(self._fs, LocalFileSystem):
            yield from self._list_local_objects(full)
            return
        # s3fs keeps directory listings for the life of the process; `exists` below and a
        # caller deciding what to delete must not read an old one.
        self._fs.invalidate_cache(full)
        if not self._fs.exists(full):
            return
        for found, info in self._fs.find(full, detail=True).items():
            yield StoredObject(
                path=str(found)[len(self._base) + 1 :],
                size=int(info.get("size") or 0),
                modified_at=_modified_at(info),
            )

    def list_children(self, prefix: str) -> Iterator[tuple[str, bool]]:
        """List the entries directly beneath a prefix; see `Storage.list_children`.

        Parameters
        ----------
        prefix
            Root-relative prefix.

        Yields
        ------
        tuple of (str, bool)
            Each entry's name and whether it is a directory. On the local backend a symlink
            is skipped, and a symlink anywhere between the root and `prefix` yields nothing.

        Raises
        ------
        ValidationError
            If the prefix contains a NUL byte, or escapes the configured storage root.
        """
        full = self._full(prefix)
        if isinstance(self._fs, LocalFileSystem):
            walked = self._base
            for part in [p for p in full[len(self._base) :].split("/") if p]:
                walked = f"{walked}/{part}"
                if os.path.islink(walked):
                    return
            try:
                scan = os.scandir(full)
            except (FileNotFoundError, NotADirectoryError):
                return
            with scan:
                for entry in scan:
                    if not entry.is_symlink():
                        yield entry.name, entry.is_dir(follow_symlinks=False)
            return
        self._fs.invalidate_cache(full)
        if not self._fs.isdir(full):
            return
        for info in self._fs.ls(full, detail=True):
            yield str(info["name"]).rstrip("/").rsplit("/", 1)[1], info["type"] == "directory"

    def _list_local_objects(self, full: str) -> Iterator[StoredObject]:
        """List a local directory without ever following or reporting a symlink.

        Parameters
        ----------
        full
            The backend path, already confined to the root.

        Yields
        ------
        StoredObject
            Each regular file. A symlink, to a file or a directory, is
            skipped, and a symlink anywhere between the root and `full`
            yields nothing, so nothing outside the root is ever listed.
        """
        relative = full[len(self._base) :].strip("/")
        walked = self._base
        for part in [p for p in relative.split("/") if p]:
            walked = f"{walked}/{part}"
            if os.path.islink(walked):
                return
        for directory, _, names in os.walk(full, followlinks=False):
            for name in names:
                path = os.path.join(directory, name)
                if os.path.islink(path):
                    continue
                try:
                    stat = os.stat(path)
                except FileNotFoundError:
                    continue  # removed since the directory was read: not there any more
                yield StoredObject(
                    path=path[len(self._base) + 1 :],
                    size=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, UTC),
                )

    def resolve_read_url(self, path: str, *, ttl_seconds: int) -> str:
        """Return a URL a caller with no storage credential of its own can read from.

        Parameters
        ----------
        path
            Root-relative path.
        ttl_seconds
            How long the returned URL remains valid. Ignored for a local
            filesystem, which has no such concept.

        Returns
        -------
        str
            The resolved local path, for a local filesystem. A presigned
            HTTPS URL valid for `ttl_seconds`, for S3 — `s3fs.S3FileSystem.sign`
            wraps boto3's `generate_presigned_url`, which other S3-compatible
            services also honour.

        Raises
        ------
        ValidationError
            If the path contains a NUL byte, or escapes the configured storage
            root.
        """
        full = self._full(path)
        if self._root.startswith("file://"):
            return full
        # Signing is offline (a unit test signs with dummy keys); that the store accepts the
        # URL is shown against a real SeaweedFS container
        # (test_resolve_read_url_against_real_seaweedfs in test_postgres_integration.py).
        return self._fs.sign(full, expiration=ttl_seconds)


def build_storage(config: StorageConfig) -> Storage:
    """Construct storage from configuration.

    Parameters
    ----------
    config
        Storage configuration.

    Returns
    -------
    Storage
        A ready-to-use storage instance.
    """
    return FsspecStorage(config.root, config.options)


def build_file_io(config: StorageConfig) -> FileIO:
    """Construct a pyiceberg FileIO from the same configuration.

    Iceberg reads and writes its own metadata through this, so both views of
    storage are built from one source of truth.

    Parameters
    ----------
    config
        Storage configuration.

    Returns
    -------
    FileIO
        A FileIO for the configured backend.
    """
    return load_file_io(properties=dict(config.options), location=config.root)


def _modified_at(info: Mapping[str, object]) -> datetime | None:
    """Read an fsspec listing entry's modification time.

    The local filesystem reports ``mtime`` (epoch seconds); s3fs reports
    ``LastModified`` (a datetime).

    Parameters
    ----------
    info
        One entry of `find(..., detail=True)`.

    Returns
    -------
    datetime or None
        Timezone-aware UTC, or `None` when the entry carries neither.
    """
    mtime = info.get("mtime")
    if isinstance(mtime, int | float):
        return datetime.fromtimestamp(mtime, UTC)
    last_modified = info.get("LastModified")
    if isinstance(last_modified, datetime):
        return last_modified if last_modified.tzinfo else last_modified.replace(tzinfo=UTC)
    return None
