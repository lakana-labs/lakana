"""The SDK's copy of the artifact manifest's path rules and canonical form.

`lakana_core.artifacts.manifest` holds the server's rules; the SDK imports
nothing of core, so the same functions live here. They must judge every path
and hash every manifest alike:
`packages/lakana-worker/tests/test_artifact_manifest_parity.py` runs both on
the same inputs. Change one copy, change the other.

The character classes, case folding and normalisation used here come from the
Unicode tables of the Python that runs them. An SDK and a server on different
Python versions can therefore disagree about a newly assigned character. The
server's answer is the one that counts; a disagreement ends as a refused
upload, never as a path that is stored unchecked.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from lakana.errors import ArtifactCorrupt, ValidationError


class ManifestError(ValidationError):
    """A local artifact cannot be stored as it is.

    The text names the path and what to do.
    """


class StoredManifestInvalid(ManifestError):
    """The manifest a server stored for a version breaks a rule this client enforces.

    It is damaged or over a limit (a path it refuses, a size out of range, a path twice). It is
    the same manifest on every download, so asking again cannot help; the task harness
    fails an attempt that meets it for good. A `ManifestError`, so callers that catch that, or
    `ValidationError`, still do.
    """


class StoredManifestDamaged(ArtifactCorrupt):
    """The manifest a server stored for a version does not match its own digest.

    The record is damaged on the server, so every download meets it again; the task harness
    fails an attempt that meets it for good. An `ArtifactCorrupt`, so callers that catch that
    still do. A downloaded *file* that does not match is a plain `ArtifactCorrupt`: it may be
    a transient fault, and stays retryable.
    """


MANIFEST_FORMAT = 1
MAX_PATH_BYTES = 1024
MAX_PATH_SEGMENTS = 32
MAX_SEGMENT_BYTES = 255

_UNSAFE = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
_SHOWN_CHARS = 200
_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"]
    + [f"{device}{digit}" for device in ("COM", "LPT") for digit in "0123456789\u00b9\u00b2\u00b3"]
)
_ASCII_UPPER = str.maketrans("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")


READ_BYTES = 1024 * 1024
"""Bytes read from a local file at a time."""

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_FILE_FLAGS = os.O_RDONLY | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
_BY_DESCRIPTOR = (
    hasattr(os, "O_DIRECTORY")
    and _NOFOLLOW != 0
    and os.open in os.supports_dir_fd
    and os.scandir in os.supports_fd
)
"""Whether a directory can be walked, and a file opened, by descriptor without following a link.

True on Linux and macOS. Where it is not (Windows) the scan lists and opens by path and refuses
a link as it finds it, but a directory swapped for a link between the listing and the opening is
not noticed; the hash check at upload time still guarantees that what is sent is what was
scanned."""
_SWAPPED = (
    "{path} was replaced by a link or by a file of another kind while it was being scanned, so "
    "nothing was stored from it. Wait until whatever changes the directory has finished, then "
    "run the upload again."
)
_LINK = (
    "{path} is a symbolic link; an artifact holds only regular files and directories. "
    "Copy what it points to instead."
)
_NOT_REGULAR = (
    "{path} is not a regular file (a device, a socket or a pipe); an artifact holds only "
    "regular files."
)
CHANGED = (
    "{path} changed after it was scanned, so nothing was stored from it. Wait until whatever "
    "writes it has finished, then run the upload again."
)
_ENTRIES_PER_FILE = 10
"""Directory entries (files and directories) a scan visits per allowed file."""
_ROOT_SHOWN = 200
"""Characters of the path given to the scan that a message shows, the end of it kept."""


@dataclass(frozen=True)
class ScanLimits:
    """How much a scan may look at before it gives up, so a wrong path fails fast.

    The defaults are those of the server's `LAKANA_ARTIFACTS__MAX_FILES`,
    `LAKANA_ARTIFACTS__MAX_FILE_BYTES` and `LAKANA_ARTIFACTS__MAX_BYTES`. The server enforces its
    own; pass larger values here only when its operator raised them.

    Parameters
    ----------
    max_files
        Most files.
    max_file_bytes
        Largest single file.
    max_bytes
        Largest total size.
    """

    max_files: int = 10_000
    max_file_bytes: int = 2 * 1024**3
    max_bytes: int = 10 * 1024**3


@dataclass(frozen=True)
class LocalFile:
    """One file of a local artifact.

    Parameters
    ----------
    path
        Its path inside the artifact, ``/``-separated.
    source
        Where it is on this machine.
    size
        Its size in bytes, as hashed.
    sha256
        The SHA-256 of its contents.
    root
        The directory it was found below, or `None` for a single file. Where the platform
        allows, the file is opened again from this directory by descriptor, one component at a
        time without following a link, so a directory swapped for a link after the scan is
        not followed.
    dir_fd
        The descriptor the scan was given, or `None`: `root` (or `source` for a single file) is
        then looked up relative to it, never relative to the working directory. The caller of
        the scan keeps it open until the upload is done.
    """

    path: str
    source: str
    size: int
    sha256: str
    root: str | None = None
    dir_fd: int | None = None


@dataclass(frozen=True)
class LocalArtifact:
    """A scanned local file or directory, ready to upload.

    Parameters
    ----------
    is_directory
        Whether it is a directory.
    files
        Its files, sorted by the path's UTF-8 bytes.
    """

    is_directory: bool
    files: tuple[LocalFile, ...]

    def document(self) -> dict[str, object]:
        """Return the manifest document the server expects.

        Returns
        -------
        dict
            ``format``, ``is_directory`` and ``files``.
        """
        return {
            "format": MANIFEST_FORMAT,
            "is_directory": self.is_directory,
            "files": [{"path": f.path, "size": f.size, "sha256": f.sha256} for f in self.files],
        }

    def digest(self) -> str:
        """Return the manifest's digest, as the server will compute it.

        Returns
        -------
        str
            `manifest_digest` of this artifact.
        """
        return manifest_digest(self.is_directory, [(f.path, f.size, f.sha256) for f in self.files])


def shown(path: object) -> str:
    """Make a path safe to put into a message.

    Parameters
    ----------
    path
        What a manifest gave as a path.

    Returns
    -------
    str
        The ASCII-escaped path, at most 200 characters; the type's name for
        anything that is not a string.
    """
    if not isinstance(path, str):
        return type(path).__name__
    return ascii(path)[1:-1][:_SHOWN_CHARS]


def path_problem(path: object) -> str | None:
    """Say what is wrong with one manifest path.

    Parameters
    ----------
    path
        The path as given.

    Returns
    -------
    str or None
        The problem, worded to follow ``Path <path> ``; `None` for a relative,
        ``/``-separated path that passes these rules, checked in this order
        (the first failure is returned): a non-empty string; no control,
        format or surrogate character or line separator; no backslash; no
        leading ``/``; no empty, ``.`` or ``..`` segment; then, segment by
        segment, no ``:``, no ending in ``.`` or a space, and no name Windows
        reserves (``CON``, ``PRN``, ``AUX``, ``NUL``, ``COM0`` to ``COM9``,
        ``LPT0`` to ``LPT9`` and the superscript forms, ``CONIN$`` and ``CONOUT$``,
        compared as Windows does, with ASCII-only upper-casing, and with or
        without an extension: the name is the part before the first
        ``.``); then at most `MAX_PATH_BYTES` bytes, no segment of more than
        `MAX_SEGMENT_BYTES` bytes (UTF-8, as a file system counts a name) and
        at most `MAX_PATH_SEGMENTS` segments.
    """
    if not isinstance(path, str) or not path:
        return "a path must be a non-empty string"
    if any(unicodedata.category(char) in _UNSAFE for char in path):
        return "must not contain a control, format or surrogate character or a line separator"
    if "\\" in path:
        return "must not contain a backslash; separate segments with '/'"
    if path.startswith("/"):
        return "must be relative, without a leading '/'"
    segments = path.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        return "must not have an empty, '.' or '..' segment"
    for segment in segments:
        if ":" in segment:
            return "must not contain ':'"
        if segment.endswith((".", " ")):
            return "must not have a part ending in '.' or a space"
        if segment.split(".", 1)[0].rstrip(" ").translate(_ASCII_UPPER) in _RESERVED_NAMES:
            return "must not use a name Windows reserves, such as 'NUL'"
    if len(path.encode("utf-8")) > MAX_PATH_BYTES:
        return f"is longer than {MAX_PATH_BYTES} bytes"
    if any(len(segment.encode("utf-8")) > MAX_SEGMENT_BYTES for segment in segments):
        return f"must not have a part longer than {MAX_SEGMENT_BYTES} bytes"
    if len(segments) > MAX_PATH_SEGMENTS:
        return f"has more than {MAX_PATH_SEGMENTS} segments"
    return None


def fold_path(path: str) -> str:
    """Fold a path the way a case- and normalisation-insensitive file system does.

    Parameters
    ----------
    path
        A valid manifest path.

    Returns
    -------
    str
        ``NFD(casefold(NFD(path)))``; two paths such a file system treats as
        one fold equal.
    """
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", path).casefold())


def paths_problem(paths: Sequence[str]) -> str | None:
    """Say why a set of valid paths cannot be one tree on every file system.

    Parameters
    ----------
    paths
        Paths that each passed `path_problem`.

    Returns
    -------
    str or None
        A whole sentence naming two files, or two directories, that differ
        only by case or Unicode normalisation, or a path that is both a file
        and a directory; `None` when the paths are one unambiguous tree.
    """
    files: dict[str, str] = {}
    directories: dict[str, str] = {}
    seen: set[str] = set()
    for path in paths:
        segments = path.split("/")
        raw = ""
        folded = ""
        for segment in segments[:-1]:
            raw = f"{raw}/{segment}" if raw else segment
            folded = f"{folded}/{fold_path(segment)}" if folded else fold_path(segment)
            if raw in seen:
                continue
            seen.add(raw)
            known = directories.setdefault(folded, raw)
            if known != raw:
                return (
                    f"Directories {shown(known)} and {shown(raw)} differ only by case or "
                    "Unicode normalisation; rename one."
                )
        folded_path = f"{folded}/{fold_path(segments[-1])}" if folded else fold_path(segments[-1])
        other = files.setdefault(folded_path, path)
        if other != path:
            return (
                f"Paths {shown(other)} and {shown(path)} differ only by case or "
                "Unicode normalisation; rename one."
            )
    for folded, raw in files.items():
        if folded in directories:
            return f"{shown(raw)} is both a file and a directory."
    return None


def canonical_manifest(is_directory: bool, files: Sequence[tuple[str, int, str]]) -> bytes:
    """Serialise a manifest in its one canonical form.

    Parameters
    ----------
    is_directory
        Whether the artifact is a directory.
    files
        ``(path, size, sha256)``; sorted here by the path's UTF-8 bytes, so
        the order given does not matter.

    Returns
    -------
    bytes
        ASCII JSON with sorted keys and no whitespace.
    """
    ordered = sorted(files, key=lambda file: file[0].encode("utf-8", "surrogatepass"))
    document = {
        "files": [{"path": path, "sha256": digest, "size": size} for path, size, digest in ordered],
        "format": MANIFEST_FORMAT,
        "is_directory": is_directory,
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def manifest_digest(is_directory: bool, files: Sequence[tuple[str, int, str]]) -> str:
    """Hash a manifest's canonical form: the identity of the content.

    Parameters
    ----------
    is_directory
        Whether the artifact is a directory.
    files
        ``(path, size, sha256)``, sorted by the path's UTF-8 bytes.

    Returns
    -------
    str
        The SHA-256, lower-case hex.
    """
    return hashlib.sha256(canonical_manifest(is_directory, files)).hexdigest()


def root_label(path: object) -> str:
    """Show the path given to the scan in a message, keeping its end, which is what identifies it.

    Parameters
    ----------
    path
        The path as given.

    Returns
    -------
    str
        The ASCII-escaped path; when it is longer than `_ROOT_SHOWN` it starts with ``...``.
    """
    text = ascii(str(path))[1:-1]
    return text if len(text) <= _ROOT_SHOWN else "..." + text[-(_ROOT_SHOWN - 3) :]


def _below(root: str, parts: list[str], dir_fd: int | None = None) -> int:
    """Open a file below a directory by descriptor, never following a link on the way.

    Each directory is opened relative to the one before it with `O_NOFOLLOW`, so a directory
    that was replaced by a link (or a file) since it was listed is refused, not entered.

    Parameters
    ----------
    root
        The directory the walk started in.
    parts
        The path components below it; the last is the file.
    dir_fd
        A directory `root` is relative to, or `None` for the working directory.

    Returns
    -------
    int
        The open file descriptor; the caller closes it.

    Raises
    ------
    OSError
        If a component cannot be opened; `ELOOP` and `ENOTDIR` mean it was swapped.
    """
    directory = os.open(root, _DIR_FLAGS, dir_fd=dir_fd)
    try:
        for part in parts[:-1]:
            child = os.open(part, _DIR_FLAGS, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(parts[-1], _FILE_FLAGS, dir_fd=directory)
    finally:
        os.close(directory)


def open_regular(source: str, path: str, root: str | None = None, dir_fd: int | None = None) -> int:
    """Open a local file for reading without following a link, and check what it is.

    Parameters
    ----------
    source
        The file on this machine.
    path
        Its path inside the artifact, for messages.
    root
        The directory it was found below; when given and the platform allows, every directory
        on the way is opened by descriptor without following a link (`_BY_DESCRIPTOR`).
    dir_fd
        A directory `root`, or `source` when `root` is `None`, is relative to; `None` for the
        working directory.

    Returns
    -------
    int
        The open file descriptor; the caller closes it.

    Raises
    ------
    ManifestError
        If it cannot be opened, is a link, was swapped for a link, or is not a regular file when
        opened.
    """
    try:
        if root is not None and _BY_DESCRIPTOR:
            descriptor = _below(root, path.split("/"), dir_fd)
        else:
            descriptor = os.open(source, _FILE_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ManifestError(_SWAPPED.format(path=shown(path))) from exc
        raise ManifestError(
            f"{shown(path)} could not be read: {exc.strerror or type(exc).__name__}. "
            "Check that it exists and that you may read it, then run the upload again."
        ) from exc
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ManifestError(_NOT_REGULAR.format(path=shown(path)))
    return descriptor


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    """Name a file's state, to notice that it changed.

    Parameters
    ----------
    info
        Its `stat` result.

    Returns
    -------
    tuple
        Device, inode, size and modification time in nanoseconds.
    """
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _hashed(
    source: str,
    path: str,
    expected: os.stat_result | None = None,
    root: str | None = None,
    dir_fd: int | None = None,
) -> LocalFile:
    """Hash one regular file without following a link, noticing a change under the hash.

    Parameters
    ----------
    source
        The file on this machine.
    path
        Its path inside the artifact.
    expected
        What the scan saw of the file; a different file, size or modification time is a change.
    root
        The directory it was found below, as for `open_regular`.
    dir_fd
        The directory `root` or `source` is relative to, as for `open_regular`.

    Returns
    -------
    LocalFile
        The file with its size and hash.

    Raises
    ------
    ManifestError
        If it is a link or not a regular file when opened, cannot be read, or changed while it
        was scanned.
    """
    descriptor = open_regular(source, path, root, dir_fd)
    digest = hashlib.sha256()
    size = 0
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if expected is not None and _identity(before) != _identity(expected):
            raise ManifestError(CHANGED.format(path=shown(path)))
        try:
            while chunk := handle.read(READ_BYTES):
                size += len(chunk)
                if size > before.st_size:
                    raise ManifestError(CHANGED.format(path=shown(path)))
                digest.update(chunk)
        except OSError as exc:
            raise ManifestError(
                f"{shown(path)} could not be read: {exc.strerror or type(exc).__name__}. "
                "The upload was stopped and can be tried again once the file is readable."
            ) from exc
        if _identity(os.fstat(handle.fileno())) != _identity(before) or size != before.st_size:
            raise ManifestError(CHANGED.format(path=shown(path)))
    return LocalFile(
        path=path, source=source, size=size, sha256=digest.hexdigest(), root=root, dir_fd=dir_fd
    )


def _unreadable(path: str, exc: OSError) -> str:
    """Word a directory or entry that could not be listed.

    Parameters
    ----------
    path
        Its path inside the artifact, ``"."`` for the top.
    exc
        What the operating system said.

    Returns
    -------
    str
        The message.
    """
    return (
        f"{shown(path)} could not be read: {exc.strerror or type(exc).__name__}. "
        "Check its permissions, or leave it out of the upload."
    )


def _within(name: str, size: int, total: int, limits: ScanLimits) -> int:
    """Check one file's size against the limits, before anything is hashed.

    Parameters
    ----------
    name
        The file's path inside the artifact.
    size
        Its size as the directory listing says.
    total
        The sizes of the files so far.
    limits
        What a scan may look at.

    Returns
    -------
    int
        `size`, so a caller can add it to its total.

    Raises
    ------
    ManifestError
        If the file, or the total with it, is larger than allowed.
    """
    if size > limits.max_file_bytes:
        raise ManifestError(
            f"{shown(name)} is {size} bytes, more than the {limits.max_file_bytes} bytes one file "
            "may have (the server's operator sets this limit, LAKANA_ARTIFACTS__MAX_FILE_BYTES). "
            "Store a smaller file, or split it, or ask the operator for a larger limit."
        )
    if total + size > limits.max_bytes:
        raise ManifestError(
            f"The files add up to more than the {limits.max_bytes} bytes one artifact may have "
            "(the server's operator sets this limit, LAKANA_ARTIFACTS__MAX_BYTES). Store fewer or "
            "smaller files, or ask the operator for a larger limit."
        )
    return size


class _Collector:
    """The files a walk found, and the limits it keeps to.

    Parameters
    ----------
    limits
        What a scan may look at.
    """

    def __init__(self, limits: ScanLimits) -> None:
        self.limits = limits
        self.found: list[tuple[str, str, os.stat_result]] = []
        self._max_entries = limits.max_files * _ENTRIES_PER_FILE
        self._visited = 0
        self._total = 0

    def visit(self) -> None:
        """Count one directory entry.

        Raises
        ------
        ManifestError
            When more entries than the limits allow were seen: a wrong path (``/``, a home
            directory) ends here, in moments, before anything is hashed.
        """
        self._visited += 1
        if self._visited > self._max_entries:
            raise ManifestError(
                f"The directory holds more than {self._max_entries} entries (ten for each of the "
                f"{self.limits.max_files} files the server's operator allows with "
                "LAKANA_ARTIFACTS__MAX_FILES); an artifact is one model or dataset. Point the "
                "upload at the directory that holds just that, or pack many small files into one "
                "archive."
            )

    def file(self, name: str, source: str, info: os.stat_result) -> None:
        """Take one regular file.

        Parameters
        ----------
        name
            Its path inside the artifact.
        source
            Its path on this machine.
        info
            Its `lstat`.

        Raises
        ------
        ManifestError
            If it, or the count of files, passes a limit.
        """
        self._total += _within(name, info.st_size, self._total, self.limits)
        self.found.append((name, source, info))
        if len(self.found) > self.limits.max_files:
            raise ManifestError(
                f"The directory has more than the {self.limits.max_files} files one artifact may "
                "have (the server's operator sets this limit, LAKANA_ARTIFACTS__MAX_FILES). An "
                "artifact is one model or dataset: point the upload at the directory that holds "
                "just that, or pack many small files into one archive, or ask the operator for a "
                "larger limit."
            )


def _walk_by_descriptor(
    collector: _Collector, root: str, directory: int, prefix: str, depth: int
) -> None:
    """List a directory by descriptor and descend into its subdirectories the same way.

    Parameters
    ----------
    collector
        Where files and limits are kept.
    root
        The directory the walk started in, to build each file's path on this machine.
    directory
        An open descriptor of the directory to list.
    prefix
        Its path inside the artifact with a trailing ``/``, empty for the top.
    depth
        How many directories deep it is.

    Raises
    ------
    ManifestError
        For a link, a device, socket or pipe, a directory swapped for a link, an unreadable
        entry, a tree deeper than a path may be, or a limit passed.
    """
    if depth > MAX_PATH_SEGMENTS:
        raise ManifestError(
            f"{shown(prefix.rstrip('/'))} is nested more than {MAX_PATH_SEGMENTS} levels deep, "
            "more than a path of an artifact may be; store a shallower directory."
        )
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                collector.visit()
                name = prefix + entry.name
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise ManifestError(_unreadable(name, exc)) from exc
                if stat.S_ISLNK(info.st_mode):
                    raise ManifestError(_LINK.format(path=shown(name)))
                if stat.S_ISDIR(info.st_mode):
                    try:
                        child = os.open(entry.name, _DIR_FLAGS, dir_fd=directory)
                    except OSError as exc:
                        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                            raise ManifestError(_SWAPPED.format(path=shown(name))) from exc
                        raise ManifestError(_unreadable(name, exc)) from exc
                    try:
                        _walk_by_descriptor(collector, root, child, name + "/", depth + 1)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode):
                    collector.file(name, os.path.join(root, *name.split("/")), info)
                else:
                    raise ManifestError(_NOT_REGULAR.format(path=shown(name)))
    except OSError as exc:
        raise ManifestError(_unreadable(prefix.rstrip("/") or ".", exc)) from exc


def _walk_by_path(collector: _Collector, root: str) -> None:
    """List a directory by path, where the platform cannot do it by descriptor.

    Parameters
    ----------
    collector
        Where files and limits are kept.
    root
        The directory.

    Raises
    ------
    ManifestError
        For a link, a device, socket or pipe, an unreadable entry, or a limit passed.
    """
    pending = [(root, "")]
    while pending:
        directory, prefix = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    collector.visit()
                    name = prefix + entry.name
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        raise ManifestError(_LINK.format(path=shown(name)))
                    if stat.S_ISDIR(info.st_mode):
                        pending.append((entry.path, name + "/"))
                    elif stat.S_ISREG(info.st_mode):
                        # A directory entry's own stat lacks the inode and link count on Windows.
                        collector.file(name, entry.path, os.lstat(entry.path))
                    else:
                        raise ManifestError(_NOT_REGULAR.format(path=shown(name)))
        except OSError as exc:
            raise ManifestError(_unreadable(prefix.rstrip("/") or ".", exc)) from exc


def _walk(
    root: str, limits: ScanLimits, dir_fd: int | None = None
) -> list[tuple[str, str, os.stat_result]]:
    """List the regular files below a directory, giving up as soon as a limit is passed.

    Links are neither followed nor accepted, entries are visited as they are listed (a directory
    of millions of names is never read whole), and the walk stops at the first entry beyond the
    limits. Where the platform allows (`_BY_DESCRIPTOR`) every directory is opened relative to its
    parent with `O_NOFOLLOW`, so one swapped for a link while the walk runs is refused.

    Parameters
    ----------
    root
        The directory.
    limits
        What a scan may look at.
    dir_fd
        A directory `root` is relative to, or `None` for the working directory; given only where
        the walk is by descriptor.

    Returns
    -------
    list of tuple
        ``(path inside the artifact, path on this machine, lstat)`` for each file, unordered.

    Raises
    ------
    ManifestError
        For a link, a device, socket or pipe, an unreadable directory, or a limit passed.
    """
    collector = _Collector(limits)
    if not _BY_DESCRIPTOR:
        _walk_by_path(collector, root)
        return collector.found
    try:
        top = os.open(root, _DIR_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ManifestError(_SWAPPED.format(path=root_label(root))) from exc
        raise ManifestError(_unreadable(root_label(root), exc)) from exc
    try:
        _walk_by_descriptor(collector, root, top, "", 0)
    finally:
        os.close(top)
    return collector.found


def scan(
    path: Path, limits: ScanLimits | None = None, *, dir_fd: int | None = None
) -> LocalArtifact:
    """Turn a local file or directory into an artifact, refusing what cannot be stored.

    Everything in a directory is stored, hidden files (a name starting with a dot, such as
    ``.env``) included: point the scan at a directory that holds only the artifact. Nothing is
    hashed until every name and size has passed the checks, so a wrong path fails before any
    file is read.

    Parameters
    ----------
    path
        A regular file, or a directory.
    limits
        What the scan may look at; the server's defaults when `None`.
    dir_fd
        An open descriptor of a directory: `path` is then ``.`` (that directory) or one name in
        it, and nothing is looked up relative to the working directory, now or when the upload
        opens the files again. The caller keeps it open until the upload is done. Needs a
        platform that opens files relative to a directory (Linux, macOS).

    Returns
    -------
    LocalArtifact
        The files with their sizes and hashes.

    Raises
    ------
    ValueError
        If `dir_fd` is given with a path that is not ``.`` or one name, or on a platform that
        cannot open files relative to a directory.
    ManifestError
        For a missing or unreadable path, a symbolic link anywhere, a device, socket or
        pipe, a file hard-linked from outside, a name the server would
        refuse, two names a file system may treat as one, more files or bytes
        than the limits allow, a file that changed while it was read, or no
        file at all.
    """
    limits = limits or ScanLimits()
    if dir_fd is not None:
        _check_relative_name(path)
    try:
        top = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            raise ManifestError(
                f"{root_label(path)} does not exist. Check the path, then run the upload again."
            ) from exc
        raise ManifestError(
            f"{root_label(path)} could not be read: {exc.strerror or type(exc).__name__}. "
            "Check that you may read it, then run the upload again."
        ) from exc
    root: str | None = None
    if stat.S_ISLNK(top.st_mode):
        raise ManifestError(_LINK.format(path=root_label(path)))
    if stat.S_ISDIR(top.st_mode):
        root = str(path)
        found = _walk(root, limits, dir_fd)
    elif stat.S_ISREG(top.st_mode):
        _within(Path(path).name, top.st_size, 0, limits)
        found = [(Path(path).name, str(path), top)]
    else:
        raise ManifestError(_NOT_REGULAR.format(path=root_label(path)))
    if not found:
        raise ManifestError(
            "The directory has no files; an artifact must contain at least one file."
        )
    seen: dict[tuple[int, int], int] = {}
    for _, _, info in found:
        seen[info.st_dev, info.st_ino] = seen.get((info.st_dev, info.st_ino), 0) + 1
    for relative, _, info in found:
        problem = path_problem(relative)
        if problem is not None:
            advice = "store a shallower directory" if problem.endswith("segments") else "rename it"
            raise ManifestError(f"{shown(relative)} {problem}; {advice}.")
        if info.st_nlink > seen[info.st_dev, info.st_ino]:
            if root is None:
                raise ManifestError(
                    f"{shown(relative)} has another hard link, so its content is reachable "
                    "under a second name; copy the file and give the copy."
                )
            raise ManifestError(
                f"{shown(relative)} is hard-linked from outside the directory; copy the file "
                "instead."
            )
    found.sort(key=lambda item: item[0].encode("utf-8"))
    collision = paths_problem([relative for relative, _, _ in found])
    if collision is not None:
        raise ManifestError(collision)
    return LocalArtifact(
        is_directory=root is not None,
        files=tuple(
            _hashed(entry, relative, info, root, dir_fd) for relative, entry, info in found
        ),
    )


def _check_relative_name(path: Path) -> None:
    """Refuse a path that cannot be looked up below a directory descriptor by one step.

    Parameters
    ----------
    path
        What the caller gave with `dir_fd`.

    Raises
    ------
    ValueError
        If it is absolute, has more than one component or is ``..``, or the platform cannot
        open files relative to a directory without following a link.
    """
    if not _BY_DESCRIPTOR:
        raise ValueError(
            "a path with dir_fd needs a platform that opens files relative to a directory "
            "without following links (Linux or macOS)"
        )
    text = os.fspath(path)
    if text != "." and (text in ("", "..") or os.sep in text or "/" in text):
        raise ValueError(
            f"a path with dir_fd must be '.' or one name in that directory, got {shown(text)}"
        )
