"""The storage protocol every backend implements."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

STREAM_CHUNK_BYTES = 1024 * 1024
"""Bytes per piece `read_chunks` yields unless told otherwise."""

STREAM_BLOCK_BYTES = 8 * 1024 * 1024
"""Bytes an object-store backend buffers before it sends one part of a multipart upload."""

PART_SUFFIX = ".part"
"""Suffix of the local backend's temporary file while a stream is being written."""


@dataclass(frozen=True)
class StoredObject:
    """One object a listing found.

    Parameters
    ----------
    path
        Root-relative path.
    size
        Size in bytes.
    modified_at
        Last modification, timezone-aware UTC, or `None` when the backend
        doesn't report it — maintenance then never treats it as old enough
        to delete.
    """

    path: str
    size: int
    modified_at: datetime | None


@dataclass(frozen=True)
class ChildEntry:
    """One entry directly beneath a prefix, as `Storage.list_children_page` reports it.

    Parameters
    ----------
    name
        The entry's name relative to the prefix.
    is_directory
        Whether it is a directory (a common key prefix on an object store).
    size
        A file's size in bytes; `None` for a directory.
    """

    name: str
    is_directory: bool
    size: int | None

    @property
    def key(self) -> str:
        """The entry's place in a listing: its name, and a slash after a directory's.

        Returns
        -------
        str
            The key, ordered as an object store orders its keys, so ``a.csv`` comes before the
            directory ``a``.
        """
        return f"{self.name}/" if self.is_directory else self.name


class StreamWriter(Protocol):
    """A file being written piece by piece. Nothing is visible until `commit`."""

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
        ...

    def commit(self) -> None:
        """Make the object visible at its path, replacing any object there.

        Raises
        ------
        ValueError
            If the stream was already committed or aborted.
        """
        ...

    def abort(self) -> None:
        """Throw the written bytes away. A no-op after `commit` or a second call."""
        ...


class Storage(Protocol):
    """Object storage scoped to a single root.

    All paths are relative to that root. Implementations translate backend
    errors into lakana-core's domain errors, so callers never see fsspec
    exceptions.
    """

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
            If no object exists at that path.
        ValidationError
            If the path contains a NUL byte, or escapes the configured storage
            root.
        """
        ...

    def write(self, path: str, data: bytes) -> None:
        """Write an object, replacing any existing one.

        Parameters
        ----------
        path
            Root-relative path.
        data
            Contents to write.

        Raises
        ------
        ConflictError
            If a segment of the path already exists as something other than
            a directory.
        ValidationError
            If the path contains a NUL byte, or escapes the configured storage
            root.
        """
        ...

    def delete(self, path: str) -> None:
        """Delete an object.

        Parameters
        ----------
        path
            Root-relative path.

        Raises
        ------
        NotFoundError
            If no object exists at that path.
        ValidationError
            If the path contains a NUL byte, or escapes the configured storage
            root.
        """
        ...

    def exists(self, path: str) -> bool:
        """Report whether an object exists.

        Parameters
        ----------
        path
            Root-relative path.

        Returns
        -------
        bool
            ``True`` if the object exists. A directory is not an object, so
            this agrees with `read` and `delete`, which both raise
            `NotFoundError` for a directory rather than treating it as
            present.

        Raises
        ------
        ValidationError
            If the path contains a NUL byte, or escapes the configured storage
            root.
        """
        ...

    def list(self, prefix: str) -> Iterator[str]:
        """List objects beneath a prefix.

        Parameters
        ----------
        prefix
            Root-relative prefix.

        Yields
        ------
        str
            Root-relative paths, in no particular order.

        Raises
        ------
        ValidationError
            If the prefix contains a NUL byte, or escapes the configured storage
            root.
        """
        ...

    def list_objects(self, prefix: str) -> Iterator[StoredObject]:
        """List objects beneath a prefix, with their size and age.

        Parameters
        ----------
        prefix
            Root-relative prefix.

        Yields
        ------
        StoredObject
            Each object, in no particular order.

        Raises
        ------
        ValidationError
            If the prefix contains a NUL byte, or escapes the configured
            storage root.
        """
        ...

    def list_children(self, prefix: str) -> Iterator[tuple[str, bool]]:
        """List the entries directly beneath a prefix, one level only.

        A backend with no directories (an object store) reports a common key prefix as a
        directory. The listing is current: a backend that caches listings drops its cache
        for the prefix first.

        Parameters
        ----------
        prefix
            Root-relative prefix.

        Yields
        ------
        tuple of (str, bool)
            The entry's name relative to `prefix` and whether it is a directory, in no
            particular order. A symbolic link is never reported.

        Raises
        ------
        ValidationError
            If the prefix contains a NUL byte, or escapes the configured storage root.
        """
        ...

    def list_children_page(self, prefix: str, *, after: str | None, limit: int) -> list[ChildEntry]:
        """List one page of the entries directly beneath a prefix, in key order.

        Reads one level of one directory, never below it, and on an object store no more than
        the one page asked for.

        Parameters
        ----------
        prefix
            Root-relative prefix.
        after
            The `ChildEntry.key` of the last entry of the previous page; `None` for the first.
        limit
            Most entries returned.

        Returns
        -------
        list of ChildEntry
            The entries after `after`, sorted by `ChildEntry.key`, files with their size. A
            symbolic link is never reported; a missing prefix, or one that is a file, has none.

        Raises
        ------
        ValidationError
            If the prefix contains a NUL byte, or escapes the configured storage root.
        """
        ...

    def resolve_read_url(self, path: str, *, ttl_seconds: int) -> str:
        """Return a URL a caller with no storage credential of its own can read from.

        Parameters
        ----------
        path
            Root-relative path.
        ttl_seconds
            How long the returned URL remains valid, for backends that support
            expiring credentials. Backends with no such concept (a local
            filesystem) ignore this.

        Returns
        -------
        str
            For a local filesystem, the resolved local path, unchanged — there
            is no credential to scope on a single host, so the caller reads it
            directly. For a cloud backend, a presigned URL requiring no
            credential of its own, valid for `ttl_seconds`.

        Raises
        ------
        ValidationError
            If the path contains a NUL byte, or escapes the configured storage
            root.
        """
        ...

    def read_url_prefix(self, path: str) -> str:
        """Return the prefix of every URL `resolve_read_url` returns for a path under `path`.

        The query process is allowed to open URLs under these prefixes and nothing else.

        Parameters
        ----------
        path
            Root-relative path of a directory: a table's location, an external table's claim.

        Returns
        -------
        str
            A string ending in ``/``: the absolute directory for a local filesystem; for a
            cloud backend, ``scheme://host[:port]/<path>/`` exactly as the backend writes the
            URLs of the files below it.

        Raises
        ------
        ValidationError
            If the path contains a NUL byte, escapes the configured storage root or names the
            root itself, or if the backend's URLs have a shape the prefix cannot be cut from.
        """
        ...

    def open_write(self, path: str) -> StreamWriter:
        """Open a stream to an object, written piece by piece.

        Nothing is visible at `path` until `StreamWriter.commit`, which replaces
        an existing object atomically. Object stores take a multipart upload,
        the local backend a temporary name next to the target.

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
            If the path contains a NUL byte, or escapes the configured storage
            root.
        """
        ...

    def read_chunks(self, path: str, *, chunk_bytes: int = STREAM_CHUNK_BYTES) -> Iterator[bytes]:
        """Read an object in pieces, without loading it.

        Parameters
        ----------
        path
            Root-relative path.
        chunk_bytes
            Most bytes per piece.

        Returns
        -------
        Iterator of bytes
            The object's contents in order; never an empty piece.

        Raises
        ------
        NotFoundError
            If no object exists at that path. Raised by this call, before the
            iterator is returned, so a caller can answer 404 before a response
            starts.
        ValidationError
            If the path contains a NUL byte, or escapes the configured storage
            root.
        """
        ...
