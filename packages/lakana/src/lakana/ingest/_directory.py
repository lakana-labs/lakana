"""Read files below an ingestion root without ever leaving it.

The root directory is the operator's (`LAKANA_LAUNCHER__INGESTION_ROOTS` or
`LAKANA_RUN__INGESTION_ROOTS`; under the Docker runner its mount, ``/ingest/<root>``). It is
opened with ``O_DIRECTORY | O_NOFOLLOW``, and everything below it one component at a time
relative to its parent's descriptor with ``O_NOFOLLOW``, so a symbolic link anywhere below is
never followed (it is listed as skipped), ``..`` is refused before any open, and only regular
files are read (``fstat`` after open). Nothing is ever written, moved or deleted. Under the
Docker runner the root is also a read-only, non-recursive mount.

A walk descends only into directories the pattern can still match, and is bounded: at most
`MAX_DEPTH` levels below the base (a directory deeper than that is listed as skipped) and
`MAX_ENTRIES` entries looked at.
"""

from __future__ import annotations

import datetime as dt
import errno
import fnmatch
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import ClassVar

from lakana.ingest._errors import CapExceeded, IngestError

MAX_DEPTH = 32
"""The most path segments below the base a walk descends to."""
MAX_ENTRIES = 1_000_000
"""The most directory entries one walk looks at, matching or not."""
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC


class DirectoryError(IngestError):
    """A path or pattern the walker refuses, or a file it cannot read; every attempt would."""

    retryable: ClassVar[bool | None] = False


class RootUnavailable(DirectoryError):
    """The root directory does not exist, is not a directory, or is a symbolic link."""


@dataclass(frozen=True)
class FileEntry:
    """One regular file the pattern matched.

    Parameters
    ----------
    path
        Its path relative to the base, with ``/``.
    size
        Bytes.
    modified
        Its modification time, UTC, truncated to the microsecond (the precision of a stored
        timestamp, so that a watermark read back compares equal); `None` only from a source
        that does not say (an HTTPS server without ``Last-Modified``).
    """

    path: str
    size: int
    modified: dt.datetime | None


@dataclass
class Listing:
    """What a walk found.

    Parameters
    ----------
    files
        Matching regular files, sorted by path.
    skipped
        Paths that matched (or that the pattern would have descended into) but are symbolic
        links, not regular files, or directories deeper than `MAX_DEPTH` (with a trailing
        ``/``).
    """

    files: list[FileEntry] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _segments(text: str, what: str) -> list[str]:
    """Split a relative path or pattern, refusing what could leave the base.

    Parameters
    ----------
    text
        The path or pattern.
    what
        ``base``, ``pattern`` or ``file``, for the message.

    Returns
    -------
    list of str
        Its non-empty segments other than ``.``.

    Raises
    ------
    DirectoryError
        For an absolute path, a ``..`` segment, a NUL, or more than `MAX_DEPTH` segments.
    """
    if text.startswith("/") or "\x00" in text or ".." in text.split("/"):
        raise DirectoryError(
            f"the {what} {text!r} must be relative to the connection's path and may not contain .."
        )
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if len(parts) > MAX_DEPTH:
        raise DirectoryError(f"the {what} {text!r} has more than {MAX_DEPTH} segments")
    return parts


class DirectoryIOError(DirectoryError):
    """An I/O error on a path that is there; another attempt may end differently."""

    retryable: ClassVar[bool | None] = None


_PERMANENT = (FileNotFoundError, PermissionError, NotADirectoryError, IsADirectoryError)


def _failure(exc: OSError, message: str) -> DirectoryError:
    """Choose the error for an operating-system failure by its cause.

    A path that is missing, not a directory, a link or denied fails every attempt; anything
    else (an I/O error, a stale handle) may pass.

    Parameters
    ----------
    exc
        What the system call raised.
    message
        The message, naming the path and the next step.

    Returns
    -------
    DirectoryError
        Not retried for a permanent cause, `DirectoryIOError` otherwise.
    """
    if isinstance(exc, _PERMANENT) or exc.errno in (errno.ELOOP, errno.ENOTDIR):
        return DirectoryError(message)
    return DirectoryIOError(message)


def _why(directory: int, name: str, exc: OSError) -> str:
    """Explain why a component below the root could not be opened.

    Parameters
    ----------
    directory
        The open directory the component is looked up in.
    name
        The component.
    exc
        What the open raised.

    Returns
    -------
    str
        The reason, naming the component.
    """
    try:
        if stat.S_ISLNK(os.lstat(name, dir_fd=directory).st_mode):
            return f"{name!r} is a symbolic link, which Lakana does not follow"
    except OSError:
        pass
    if isinstance(exc, FileNotFoundError):
        return f"{name!r} does not exist"
    if isinstance(exc, PermissionError):
        return f"this worker may not read {name!r} ({exc.strerror})"
    if exc.errno == errno.ENOTDIR:
        return f"{name!r} is not a directory"
    return f"{name!r} cannot be opened ({exc.strerror})"


def _open_dir(root_dir: str, parts: list[str]) -> int:
    """Open the root, then each directory below it, without following links.

    Parameters
    ----------
    root_dir
        The root as the task sees it.
    parts
        Directory names below it.

    Returns
    -------
    int
        A descriptor of the last directory; the caller closes it.

    Raises
    ------
    RootUnavailable
        If the root is missing, not a directory, or a symbolic link.
    DirectoryError
        If a component below is missing, a link or not a directory.
    """
    try:
        fd = os.open(root_dir.rstrip("/") or "/", _DIR)
    except OSError as exc:
        raise RootUnavailable(
            f"the ingestion root {root_dir} is not available ({exc.strerror})"
        ) from None
    try:
        for part in parts:
            try:
                below = os.open(part, _DIR, dir_fd=fd)
            except OSError as exc:
                reason = _why(fd, part, exc)
                if isinstance(exc, FileNotFoundError) or exc.errno == errno.ENOTDIR:
                    reason += (
                        "; check the connection's path with `lakana connection show`, or "
                        "create the directory under the root"
                    )
                failure = _failure(exc, reason)
                raise failure from None
            os.close(fd)
            fd = below
    except BaseException:
        os.close(fd)
        raise
    return fd


def _step(pattern: list[str], states: frozenset[int], name: str) -> frozenset[int]:
    """Advance a pattern's match by one path segment.

    The states are the pattern positions the path so far can have reached; ``**`` spans zero
    or more segments. A walk descends below a directory only while some state is short of the
    pattern's end, and a file matches when the end is among its states.

    Parameters
    ----------
    pattern
        Pattern segments.
    states
        Positions reached before `name`, closed over ``**``.
    name
        The next segment.

    Returns
    -------
    frozenset of int
        Positions reached after it, closed over ``**``; empty when nothing can match.
    """
    reached = set()
    for i in states:
        if i < len(pattern):
            if pattern[i] == "**":
                reached.add(i)
            elif fnmatch.fnmatchcase(name, pattern[i]):
                reached.add(i + 1)
    return _closure(pattern, reached)


def _closure(pattern: list[str], states: set[int]) -> frozenset[int]:
    """Add the position after every ``**`` reached, since it may span no segment.

    Parameters
    ----------
    pattern
        Pattern segments.
    states
        Positions reached.

    Returns
    -------
    frozenset of int
        The positions with their ``**`` successors.
    """
    todo = list(states)
    while todo:
        i = todo.pop()
        if i < len(pattern) and pattern[i] == "**" and i + 1 not in states:
            states.add(i + 1)
            todo.append(i + 1)
    return frozenset(states)


def pattern_segments(pattern: str) -> list[str]:
    """Split a glob pattern, refusing one that is empty or could leave the base.

    The object stores of `lakana.ingest.files` use it too, so a pattern means the same on
    every kind.

    Parameters
    ----------
    pattern
        A glob relative to the connection's path or prefix.

    Returns
    -------
    list of str
        Its segments.

    Raises
    ------
    DirectoryError
        For an empty pattern, an absolute one, ``..``, a NUL, or more than `MAX_DEPTH` segments.
    """
    wanted = _segments(pattern, "pattern")
    if not wanted:
        raise DirectoryError("a pattern is needed, such as '*.csv' or '**/*.parquet'")
    return wanted


def matches(pattern: list[str], path: list[str]) -> bool:
    """Say whether path segments match pattern segments; ``**`` spans zero or more segments.

    Parameters
    ----------
    pattern
        Segments from `pattern_segments`.
    path
        Path segments, such as an object key split at ``/``.

    Returns
    -------
    bool
        Whether the whole path matches.
    """
    states = _closure(pattern, {0})
    for name in path:
        states = _step(pattern, states, name)
        if not states:
            return False
    return len(pattern) in states


def _entries(
    fd: int, prefix: list[str], pattern: list[str], states: frozenset[int], budget: list[int]
) -> Iterator[tuple[list[str], os.stat_result | None]]:
    """List the entries below a directory that the pattern matches, without following links.

    Parameters
    ----------
    fd
        An open directory.
    prefix
        Its path below the base.
    pattern
        Pattern segments.
    states
        The pattern positions `prefix` reached.
    budget
        One element: how many more entries the walk may look at; decremented here.

    Yields
    ------
    tuple
        Path segments and the entry's ``lstat`` for a matching regular file; `None` as the stat
        for an entry to list as skipped (a link, a FIFO, a socket, a device, a directory deeper
        than `MAX_DEPTH`).

    Raises
    ------
    DirectoryError
        For a directory the pattern needs that cannot be listed, or more than `MAX_ENTRIES`
        entries.
    """
    shown = "/".join(prefix) or "."
    names: list[str] = []
    try:
        with os.scandir(fd) as scanner:
            for found in scanner:
                budget[0] -= 1
                if budget[0] < 0:
                    raise DirectoryError(
                        f"the walk looked at more than {MAX_ENTRIES} entries; narrow the pattern"
                    )
                names.append(found.name)
    except OSError as exc:
        failure = _failure(
            exc,
            f"{shown!r} cannot be listed ({exc.strerror}); give this worker read access to it "
            "or narrow the pattern",
        )
        raise failure from None
    names.sort()
    end = len(pattern)
    for name in names:
        after = _step(pattern, states, name)
        if not after:
            continue
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as exc:
            failure = _failure(
                exc,
                f"{shown!r} cannot be searched ({exc.strerror}); give this worker read and "
                "search access to it or narrow the pattern",
            )
            raise failure from None
        path = [*prefix, name]
        deeper = any(i < end for i in after)
        if stat.S_ISDIR(info.st_mode):
            if not deeper:
                continue
            if len(path) >= MAX_DEPTH:
                yield [*path, ""], None
                continue
            try:
                below = os.open(name, _DIR, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENOENT):
                    # Swapped for a link or a file, or removed, since it was listed.
                    yield path, None
                    continue
                failure = _failure(
                    exc,
                    f"{_why(fd, name, exc)}; give this worker read access to it or narrow the "
                    "pattern",
                )
                raise failure from None
            try:
                yield from _entries(below, path, pattern, after, budget)
            finally:
                os.close(below)
        elif stat.S_ISREG(info.st_mode):
            if end in after:
                yield path, info
        elif end in after or (stat.S_ISLNK(info.st_mode) and deeper):
            yield path, None


def walk(root_dir: str, base: str, pattern: str, *, max_files: int) -> Listing:
    """List the regular files below `root_dir/base` that match `pattern`.

    Parameters
    ----------
    root_dir
        The ingestion root as the task sees it.
    base
        The connection's path below the root.
    pattern
        A glob relative to the base (``*``, ``?``, ``[...]`` within a segment; ``**`` for any
        number of directories).
    max_files
        The most files a walk returns.

    Returns
    -------
    Listing
        Files and skipped paths, each sorted by path.

    Raises
    ------
    DirectoryError
        For a refused base or pattern, a directory the pattern needs that cannot be listed,
        or more than `MAX_ENTRIES` entries looked at.
    CapExceeded
        For more matches than `max_files` (the message names the cap and how to narrow).
    """
    wanted = pattern_segments(pattern)
    fd = _open_dir(root_dir, _segments(base, "base"))
    listing = Listing()
    try:
        start = _closure(wanted, {0})
        for path, info in _entries(fd, [], wanted, start, [MAX_ENTRIES]):
            if info is None:
                listing.skipped.append("/".join(path))
                continue
            if len(listing.files) == max_files:
                raise CapExceeded(
                    f"the pattern {pattern!r} matches more than max_files ({max_files}) files; "
                    "narrow the pattern or pass since="
                )
            try:
                modified = _EPOCH + dt.timedelta(microseconds=info.st_mtime_ns // 1000)
            except OverflowError:
                raise DirectoryError(
                    f"{'/'.join(path)!r} has a modification time past the year 9999, which "
                    "cannot be read; set a time that is not in the far future"
                ) from None
            listing.files.append(FileEntry("/".join(path), info.st_size, modified))
    finally:
        os.close(fd)
    return listing


def read(root_dir: str, base: str, relative: str, *, max_bytes: int) -> bytes:
    """Read one regular file below the base, opening every component without following links.

    Parameters
    ----------
    root_dir
        The ingestion root.
    base
        The connection's path.
    relative
        The file's path relative to the base, as `walk` listed it.
    max_bytes
        The most bytes to read.

    Returns
    -------
    bytes
        The content.

    Raises
    ------
    DirectoryError
        If any component is a link or missing, or the file is not a regular file.
    CapExceeded
        If the file is larger than `max_bytes`.
    """
    parts = _segments(relative, "file")
    if not parts:
        raise DirectoryError("a file path is needed")
    fd = _open_dir(root_dir, [*_segments(base, "base"), *parts[:-1]])
    try:
        try:
            handle = os.open(parts[-1], _FILE, dir_fd=fd)
        except OSError as exc:
            failure = _failure(exc, f"{relative!r} cannot be read: {_why(fd, parts[-1], exc)}")
            raise failure from None
    finally:
        os.close(fd)
    try:
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode):
            raise DirectoryError(f"{relative!r} is not a regular file")
        if info.st_size > max_bytes:
            raise CapExceeded(
                f"{relative!r} is {info.st_size} bytes, more than max_file_bytes ({max_bytes})"
            )
        chunks, total = [], 0
        while chunk := os.read(handle, 1 << 20):
            total += len(chunk)
            if total > max_bytes:
                raise CapExceeded(
                    f"{relative!r} grew past max_file_bytes ({max_bytes}) while it was read"
                )
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(handle)
