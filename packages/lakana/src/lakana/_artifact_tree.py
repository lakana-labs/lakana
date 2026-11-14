"""Writing a downloaded artifact to disk, so that the manifest alone decides what is written.

The manifest comes over the network, and a compromised or buggy server, or a manifest stored
before a rule existed, may send anything. Nothing is trusted:

- every path is checked again with this package's own copy of the path rules before the first
  byte is written, and the whole download is refused if one fails (`check_manifest`);
- the tree is written into a fresh staging directory, never into the target's own files, and
  moved into place only once every file is complete and verified. The target is never replaced
  when it already exists (a person's shell may stand in it, it may be a mount point, it keeps
  its mode):

  * a target that does not exist is staged in a sibling directory
    (``.<name>.lakana-partial-<random>``) and that directory is renamed to the target in one
    step. A failed download leaves nothing at the target; a killed process leaves the staging
    directory, under a name that says it is unfinished, and the target does not exist;
  * a target that exists and is empty is staged in a directory of the same name *inside* it,
    and the finished tree's top-level entries are then moved into the target one by one, by
    descriptor, before the empty staging directory is removed. A failed download leaves the
    target empty and present. The move is **not atomic**: a process killed while moving leaves
    some entries in the target and the rest in the unfinished staging directory inside it; the
    person empties the target (including that hidden directory) and downloads again;
  * a target that exists and is not an empty directory, or is a link, is refused before
    anything is written;
- the target is opened exactly once, by descriptor: its parent is resolved and opened, and the
  target is opened *relative to the parent's descriptor* with ``O_NOFOLLOW``, so a link put at
  its name at any moment is refused at the open. Whether it is empty is read through that
  descriptor, and the staging directory and the moves use it, never the target's path, so a
  path swapped for a link while the download runs redirects nothing;
- no link is followed. Where the platform allows (Linux, macOS) directories and files are
  created relative to a directory descriptor, with ``O_NOFOLLOW`` on every component and
  ``O_EXCL`` on every new file, so a link that is already there, or put there while the download
  runs, cannot redirect a write: a swapped directory ends as a refusal naming the file, and
  descriptors keep a write inside the directory that was opened. Where it does not (Windows)
  files are written by path below an anchor resolved once, at the start; each directory is
  checked to lie inside it before anything is created below it, and the staging directory's
  device and inode, taken when it was made, are compared before each file and before the final
  step. A directory swapped for a link between a check and the write is not noticed there, and
  neither is a target swapped, during setup, for a link to an *empty* directory: the emptiness
  check passes on the other directory and the tree is built there;
- every file is written to a temporary name and renamed to its own name only after its size and
  SHA-256 equal the manifest's, so a file with its real name in the tree is always a verified one;
- permissions: a person's copy is private (files 0600 and directories 0700, less the umask,
  never executable), because the contents are access-controlled on the server and a
  world-readable copy on a shared machine would leak them. With ``read_only`` (a task's inputs)
  files are 0400 and directories 0500, sealed through descriptors, deepest first, before the
  tree is moved into place; when the target already existed it keeps its own mode, and its
  top-level directories are sealed right after they were moved, because Linux refuses to move
  a directory that cannot be written to into another directory. The staging directory is 0700
  while it is written;
- a failure of the file system (a name it refuses, a full disk, a permission) ends as a
  `ManifestError` that names the path and the system's reason, never as a raw `OSError`.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import re
import secrets
import shutil
import stat
from collections.abc import Generator, Iterable, Sequence
from pathlib import Path

from lakana._artifact_manifest import (
    ManifestError,
    path_problem,
    paths_problem,
    root_label,
    shown,
)
from lakana._models import ArtifactFile
from lakana.errors import ArtifactCorrupt

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_WRITE_BY_DESCRIPTOR = (
    hasattr(os, "O_DIRECTORY")
    and _NOFOLLOW != 0
    and os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
)
"""Whether files can be created, and directories walked, by descriptor without following a link."""

_STAGING_NAME_CHARS = 40
_FILE_MODE = 0o600
_DIR_MODE = 0o700
_SEALED_FILE_MODE = 0o400
_SEALED_DIR_MODE = 0o500
_SIZE_BELOW = 10**20
"""Sizes the server's own parser accepts are below this; a manifest entry at or above it is
refused, and no integer of unbounded length is ever formatted into a message."""
_NO_SPACE = (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC))
_TAKEN = "{target} already exists and is not an empty directory; give a new or an empty directory."
_TARGET_LINK = "{target} is a symbolic link; give a real directory."


def check_target_name(directory: str | os.PathLike[str]) -> str:
    """Refuse a download target given with `dir_fd` that is not one name in that directory.

    Parameters
    ----------
    directory
        What the caller gave.

    Returns
    -------
    str
        The name.

    Raises
    ------
    ValueError
        If it is empty, ``.``, ``..`` or more than one component, or the platform cannot
        write files relative to a directory without following a link.
    """
    if not _WRITE_BY_DESCRIPTOR:
        raise ValueError(
            "a download with dir_fd needs a platform that writes files relative to a "
            "directory without following links (Linux or macOS)"
        )
    name = os.fspath(directory)
    if name in ("", ".", "..") or os.sep in name or "/" in name:
        raise ValueError(
            f"a download with dir_fd needs one new name in that directory, got {shown(name)}"
        )
    return name


def check_manifest(files: Sequence[ArtifactFile], is_directory: bool) -> None:
    """Refuse a manifest whose files cannot be written safely.

    Parameters
    ----------
    files
        The version's files, as the server listed them.
    is_directory
        Whether the artifact is a directory.

    Raises
    ------
    ManifestError
        For no files, a bad size (not an integer from 0 to below 10^20) or hash, a path the
        rules refuse, the same path twice, two paths a file system may treat as one, or a
        single-file artifact that is not exactly one file name.
    """
    if not files:
        raise ManifestError("The version has no files; there is nothing to download.")
    for file in files:
        problem = path_problem(file.path)
        if problem is not None:
            raise ManifestError(
                f"The server's manifest has a path this client refuses: {shown(file.path)} "
                f"{problem}."
            )
        if (
            isinstance(file.size, bool)
            or not isinstance(file.size, int)
            or not 0 <= file.size < _SIZE_BELOW
            or not isinstance(file.sha256, str)
            or _SHA256.match(file.sha256) is None
        ):
            raise ManifestError(
                f"The server's manifest entry for {shown(file.path)} needs a size from 0 to "
                "below 10^20 bytes and a lower-case SHA-256."
            )
    seen: set[str] = set()
    for file in files:
        if file.path in seen:
            raise ManifestError(
                f"The server's manifest lists {shown(file.path)} more than once, which no valid "
                "artifact does. Ask the operator to check the server, then download again."
            )
        seen.add(file.path)
    collision = paths_problem([file.path for file in files])
    if collision is not None:
        raise ManifestError(collision)
    if not is_directory and (len(files) != 1 or "/" in files[0].path):
        raise ManifestError(
            "A single-file artifact must have exactly one file whose path is a file name."
        )


def _occupied(directory: Path) -> bool:
    """Say whether a directory has anything in it, links included, without following any.

    Parameters
    ----------
    directory
        An existing directory.

    Returns
    -------
    bool
        `True` if it has at least one entry.
    """
    with os.scandir(directory) as entries:
        return next(entries, None) is not None


def _existing_ancestor(path: Path) -> Path:
    """Find the closest part of a path that exists.

    Parameters
    ----------
    path
        Any absolute path.

    Returns
    -------
    Path
        `path` itself, or its nearest parent that exists.
    """
    while not path.exists():
        path = path.parent
    return path


def _inode_of(info: os.stat_result) -> tuple[int, int]:
    """Say which file a `stat` result describes.

    Parameters
    ----------
    info
        The result of `stat`, `lstat` or `fstat`.

    Returns
    -------
    tuple
        Its device and inode numbers.
    """
    return (info.st_dev, info.st_ino)


def _failure(exc: OSError, shown_path: str, target: Path) -> ManifestError:
    """Say in plain words why the file system refused a write.

    Parameters
    ----------
    exc
        What the system raised.
    shown_path
        The file's path inside the artifact, safe to show.
    target
        The directory the person asked for.

    Returns
    -------
    ManifestError
        A message naming the path and the system's reason, and what to do.
    """
    if exc.errno in _NO_SPACE:
        return ManifestError(
            f"There is no space left on the disk while writing {shown_path} for "
            f"{root_label(target)} (it is "
            "full, or a quota is reached). Nothing was kept; free some space or choose another "
            "directory, then download again."
        )
    reason = exc.strerror or type(exc).__name__
    return ManifestError(
        f"Cannot write {shown_path} for {root_label(target)}: {reason}. Nothing was kept; fix "
        "that or choose another directory, then download again."
    )


def _chmod_dirs(fd: int, mode: int, *, deepest_first: bool, include_self: bool = True) -> None:
    """Set the mode of a directory and every directory below it, by descriptor.

    Nothing is followed: an entry is a directory only if `lstat` says so, and it is opened with
    ``O_NOFOLLOW``, so a link put into the tree changes nothing outside it.

    Parameters
    ----------
    fd
        A descriptor of the directory.
    mode
        The mode to set.
    deepest_first
        Set a directory's mode after those below it (sealing) rather than before (opening up).
    include_self
        Whether `fd`'s own directory is changed, too; directories below it always are.
    """
    if include_self and not deepest_first:
        os.fchmod(fd, mode)
    for entry in os.listdir(fd):
        if stat.S_ISDIR(os.stat(entry, dir_fd=fd, follow_symlinks=False).st_mode):
            child = os.open(entry, _DIR_FLAGS, dir_fd=fd)
            try:
                _chmod_dirs(child, mode, deepest_first=deepest_first)
            finally:
                os.close(child)
    if include_self and deepest_first:
        os.fchmod(fd, mode)


def _chmod_dirs_by_path(
    directory: Path, mode: int, *, deepest_first: bool, include_self: bool = True
) -> None:
    """Set the mode of a directory and every directory below it, by path (no descriptors).

    A link is not followed. Used only where the platform has no directory descriptors.

    Parameters
    ----------
    directory
        The directory.
    mode
        The mode to set.
    deepest_first
        Set a directory's mode after those below it.
    include_self
        Whether `directory` itself is changed, too.
    """
    if include_self and not deepest_first:
        os.chmod(directory, mode)
    with os.scandir(directory) as entries:
        children = [e.path for e in entries if e.is_dir(follow_symlinks=False)]
    for child in children:
        _chmod_dirs_by_path(Path(child), mode, deepest_first=deepest_first)
    if include_self and deepest_first:
        os.chmod(directory, mode)


def _remove_children(fd: int) -> None:
    """Remove everything inside a directory, by descriptor, ignoring failures.

    Parameters
    ----------
    fd
        A descriptor of the directory; the directory itself stays.
    """
    for entry in os.listdir(fd):
        with contextlib.suppress(OSError):
            if stat.S_ISDIR(os.stat(entry, dir_fd=fd, follow_symlinks=False).st_mode):
                shutil.rmtree(entry, dir_fd=fd, ignore_errors=True)
            else:
                os.unlink(entry, dir_fd=fd)


class TreeWriter:
    """A download being written into a staging directory, then moved to its target.

    Use `begin`, then `write_file` for each file, then `commit`; call `discard` on any failure.
    See the module's docstring for the two strategies (a target that does not exist, and an
    empty one that does) and what each leaves behind.

    Parameters
    ----------
    target
        The directory the finished tree goes to; it does not exist yet, or is empty.
    staging
        The fresh directory the tree is written into: a sibling of `target`, or, when `target`
        already exists, a directory inside it.
    parent_fd
        A descriptor of `staging`'s parent, or `None` where descriptors are not used.
    root_fd
        A descriptor of `staging`, or `None` where descriptors are not used.
    inside
        Whether `staging` is inside an existing `target`.
    read_only
        Whether the finished copy is sealed (files 0400, directories 0500).
    """

    def __init__(
        self,
        target: Path,
        staging: Path,
        parent_fd: int | None,
        root_fd: int | None,
        *,
        inside: bool,
        read_only: bool,
    ) -> None:
        self.target = target
        self.staging = staging
        self._at = root_label(target)
        self._at_parent = root_label(target.parent)
        self._parent_fd = parent_fd
        self._root_fd = root_fd
        self._inside = inside
        self._read_only = read_only
        self._anchor = Path(os.path.realpath(staging))
        # Where descriptors are not used, the staging directory is known only by its path: its
        # device and inode, taken now, say later whether it is still the one this writer made.
        self._identity = None if root_fd is not None else _inode_of(os.lstat(staging))
        self._done = False
        self._sealed = False
        self._keep = False

    @classmethod
    def begin(
        cls, directory: Path, needed: int, *, read_only: bool = False, dir_fd: int | None = None
    ) -> TreeWriter:
        """Check the target and the free space, and create the staging directory.

        Nothing is created before the free space is known to be enough, and parents created for
        a refusal are removed again.

        Parameters
        ----------
        directory
            Where the finished tree goes: not existing yet, or an empty directory. If it does
            not exist its parents are created. A link in its parents is the user's own choice and
            is followed; the directory itself must not be a link.
        needed
            The version's size in bytes.
        read_only
            Seal the finished copy: files 0400 and directories 0500.
        dir_fd
            An open descriptor of the target's parent: `directory` is then one name in it, and
            nothing is looked up by path (see `_begin_at`).

        Returns
        -------
        TreeWriter
            A writer; the target is not touched until `commit`.

        Raises
        ------
        ManifestError
            If the directory is a link, exists with something in it or is not a directory, cannot
            be created or written to, or its disk has less free space than `needed`.
        """
        if dir_fd is not None:
            return cls._begin_at(dir_fd, directory, needed, read_only=read_only)
        absolute = Path(os.path.abspath(directory))
        taken = ManifestError(_TAKEN.format(target=root_label(directory)))
        if absolute.parent == absolute:
            raise taken
        # The checks up to the free-space figure are advice: they pick the message and the disk to
        # ask. Where descriptors are used the target is judged only once it is open (see
        # `_begin_by_descriptor`); a link or a swap after this point cannot change that.
        if absolute.is_symlink():
            raise ManifestError(_TARGET_LINK.format(target=root_label(directory)))
        base = _existing_ancestor(absolute)
        existing = base == absolute
        if existing and not base.is_dir():
            raise taken
        if not base.is_dir():
            raise cls._cannot_create(directory, OSError(errno.ENOTDIR, os.strerror(errno.ENOTDIR)))
        free = shutil.disk_usage(Path(os.path.realpath(base))).free
        if needed > free:
            raise ManifestError(
                f"The artifact needs {needed} bytes but only {free} are free in "
                f"{root_label(directory)}; free some space or choose another directory."
            )
        created: list[Path] = []
        try:
            if existing and not _WRITE_BY_DESCRIPTOR and _occupied(absolute):
                raise taken
            missing = [p for p in (absolute.parent, *absolute.parent.parents) if not p.exists()]
            for parent in reversed(missing):
                parent.mkdir()
                created.append(parent)
            home = Path(os.path.realpath(absolute.parent))
            name = f".{absolute.name[:_STAGING_NAME_CHARS]}.lakana-partial-{secrets.token_hex(8)}"
            if _WRITE_BY_DESCRIPTOR:
                return cls._begin_by_descriptor(
                    directory, home, absolute.name, name, read_only=read_only
                )
            return cls._begin_by_path(home, absolute.name, name, existing, read_only=read_only)
        except BaseException as exc:
            for parent in reversed(created):
                with contextlib.suppress(OSError):
                    parent.rmdir()
            if isinstance(exc, OSError):
                raise cls._cannot_create(directory, exc, inside=existing) from exc
            raise

    @classmethod
    def _begin_at(cls, dir_fd: int, directory: Path, needed: int, *, read_only: bool) -> TreeWriter:
        """Begin a download into one name of a directory that is already open.

        The parent is never looked up by path: the writer keeps its own copy of `dir_fd`, so the
        tree lands in the directory the descriptor names even when its path has since been
        moved or replaced by a link. The target's path, the staging directory's and the result's
        are relative to that directory.

        Parameters
        ----------
        dir_fd
            The open parent directory; the caller keeps and closes it.
        directory
            The target's name in it.
        needed
            The version's size in bytes.
        read_only
            Seal the finished copy: files 0400 and directories 0500.

        Returns
        -------
        TreeWriter
            A writer.

        Raises
        ------
        ValueError
            If `directory` is not one name, or the platform cannot write by descriptor.
        ManifestError
            If the target is a link, is not an empty directory, cannot be created, or the disk
            has less free space than `needed`.
        """
        name = check_target_name(directory)
        try:
            usage = os.statvfs(dir_fd)
        except OSError as exc:
            raise cls._cannot_create(directory, exc) from exc
        free = usage.f_bavail * usage.f_frsize
        if needed > free:
            raise ManifestError(
                f"The artifact needs {needed} bytes but only {free} are free in "
                f"{root_label(directory)}; free some space or choose another directory."
            )
        staging_name = f".{name[:_STAGING_NAME_CHARS]}.lakana-partial-{secrets.token_hex(8)}"
        try:
            parent_fd = os.dup(dir_fd)
        except OSError as exc:
            raise cls._cannot_create(directory, exc) from exc
        try:
            return cls._begin_by_descriptor(
                directory, Path(), name, staging_name, read_only=read_only, parent_fd=parent_fd
            )
        except OSError as exc:
            raise cls._cannot_create(directory, exc) from exc

    @staticmethod
    def _cannot_create(directory: Path, exc: OSError, *, inside: bool = False) -> ManifestError:
        """Build the refusal for a staging location that cannot be made.

        Parameters
        ----------
        directory
            What the person asked for.
        exc
            Why the system refused.
        inside
            Whether the directory exists and the staging directory was to go inside it.

        Returns
        -------
        ManifestError
            The message, with the reason and the next step.
        """
        where = "write into" if inside else "create"
        return ManifestError(
            f"Cannot {where} {root_label(directory)}: {exc.strerror}. "
            "Choose a directory you may write to."
        )

    @classmethod
    def _begin_by_descriptor(
        cls,
        directory: Path,
        home: Path,
        name: str,
        staging_name: str,
        *,
        read_only: bool,
        parent_fd: int | None = None,
    ) -> TreeWriter:
        """Open the target's parent, then the target inside it, and make the staging directory.

        The parent is resolved once (a link in its path is the user's own choice) and opened.
        The target is then opened *relative to that descriptor* with ``O_NOFOLLOW``, so a link
        put at its name at any moment is refused at the open, and what the descriptor refers to
        is what is listed for emptiness and what everything afterwards uses: the staging
        directory is made inside it and the finished entries are moved into it. No lookup of the
        target by path decides anything once the descriptor exists.

        Parameters
        ----------
        directory
            What the person asked for, for messages.
        home
            The real path of the target's parent directory.
        name
            The target's name in `home`.
        staging_name
            The staging directory's name.
        read_only
            Whether the copy is to be sealed.
        parent_fd
            The parent, already open (the writer takes it over and closes it); `None` opens
            `home`.

        Returns
        -------
        TreeWriter
            The writer. Nothing is left behind if opening fails.

        Raises
        ------
        ManifestError
            If the target is a link, is not a directory, or is not empty.
        OSError
            If the parent or the target cannot be opened, or the staging directory made.
        """
        if parent_fd is None:
            parent_fd = os.open(home, _DIR_FLAGS)
        held = parent_fd
        try:
            try:
                target_fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
            except FileNotFoundError:
                return cls._make_staging(
                    parent_fd, home / name, home / staging_name, inside=False, read_only=read_only
                )
            except OSError as exc:
                if exc.errno not in (errno.ELOOP, errno.ENOTDIR):
                    raise
                linked = stat.S_ISLNK(
                    os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode
                )
                raise ManifestError(
                    (_TARGET_LINK if linked else _TAKEN).format(target=root_label(directory))
                ) from None
            held = target_fd
            os.close(parent_fd)
            if os.listdir(target_fd):
                raise ManifestError(_TAKEN.format(target=root_label(directory)))
            target = home / name
            return cls._make_staging(
                target_fd, target, target / staging_name, inside=True, read_only=read_only
            )
        except BaseException:
            os.close(held)
            raise

    @classmethod
    def _make_staging(
        cls, parent_fd: int, target: Path, staging: Path, *, inside: bool, read_only: bool
    ) -> TreeWriter:
        """Create the staging directory (0700) in a directory that is already open, and open it.

        Parameters
        ----------
        parent_fd
            The descriptor of the directory the staging directory goes in: the target's parent,
            or, for an existing target, the target itself. The writer keeps it.
        target
            The real path of the target.
        staging
            The staging directory's path.
        inside
            Whether `parent_fd` is the target itself.
        read_only
            Whether the copy is to be sealed.

        Returns
        -------
        TreeWriter
            The writer. Nothing is left behind if opening fails (the caller closes `parent_fd`).
        """
        os.mkdir(staging.name, _DIR_MODE, dir_fd=parent_fd)
        try:
            root_fd = os.open(staging.name, _DIR_FLAGS, dir_fd=parent_fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.rmdir(staging.name, dir_fd=parent_fd)
            raise
        return cls(target, staging, parent_fd, root_fd, inside=inside, read_only=read_only)

    @classmethod
    def _begin_by_path(
        cls, home: Path, name: str, staging_name: str, inside: bool, *, read_only: bool
    ) -> TreeWriter:
        """Make the staging directory by path, where the platform has no directory descriptors.

        Parameters
        ----------
        home
            The real path of the target's parent directory.
        name
            The target's name in `home`.
        staging_name
            The staging directory's name.
        inside
            Whether the target exists, so that the staging directory goes inside it.
        read_only
            Whether the copy is to be sealed.

        Returns
        -------
        TreeWriter
            The writer, which remembers the staging directory's device and inode.
        """
        target = home / name
        staging = (target if inside else home) / staging_name
        os.mkdir(staging, _DIR_MODE)
        return cls(target, staging, None, None, inside=inside, read_only=read_only)

    @contextlib.contextmanager
    def _local(self, shown_path: str) -> Generator[None]:
        """Turn a refusal of the file system into a message that names the path.

        Parameters
        ----------
        shown_path
            The file's path inside the artifact, safe to show.

        Yields
        ------
        None
            Control for the file-system call.

        Raises
        ------
        ManifestError
            If the call raised `OSError`.
        """
        try:
            yield
        except OSError as exc:
            refusal = _failure(exc, shown_path, self.target)
            raise refusal from exc

    def _resolves_inside(self, directory: Path) -> bool:
        """Say whether a path inside the staging directory still leads to a place inside it.

        Parameters
        ----------
        directory
            A path below the staging directory (path-based fallback only).

        Returns
        -------
        bool
            `False` if a link on the way leads out of the staging directory.
        """
        real = os.path.realpath(directory)
        inside = str(self._anchor)
        return real == inside or real.startswith(inside + os.sep)

    def _check_staging_by_path(self) -> None:
        """Refuse to go on if the staging path now leads to another directory (path-based fallback).

        Raises
        ------
        ManifestError
            If the device and inode at the staging path are not those of the directory this
            writer made. The writer then lets go of it: it removes nothing there.
        """
        if _inode_of(os.lstat(self._anchor)) != self._identity:
            self._release()
            raise ManifestError(
                f"The directory this download was written in was replaced by another, so "
                "nothing more was written and the other directory at "
                f"{root_label(self._anchor)} was left as it is. Check who else writes there, "
                "then download again."
            )

    def _directory_of(self, segments: Sequence[str], shown_path: str) -> tuple[int | None, Path]:
        """Create and open the directories above a file, never through a link.

        Parameters
        ----------
        segments
            The directories of the file's path, outermost first.
        shown_path
            The file's path, safe to show, for the message.

        Returns
        -------
        tuple
            A descriptor of the innermost directory (the caller closes it), or `None` where
            descriptors are not used, and its path.

        Raises
        ------
        ManifestError
            If a directory on the way is a link or not a directory, or cannot be made.
        """
        refused = ManifestError(
            f"Refusing to write {shown_path}: its directory resolves outside {self._at}."
        )
        if self._root_fd is None:
            with self._local(shown_path):
                self._check_staging_by_path()
            directory = self._anchor
            for segment in segments:
                # Checked before anything is created below it: a directory that became a link
                # must not get a new directory made at the other end.
                if not self._resolves_inside(directory):
                    raise refused
                directory = directory / segment
                with self._local(shown_path):
                    directory.mkdir(mode=_DIR_MODE, exist_ok=True)
            if not self._resolves_inside(directory):
                raise refused
            return None, directory
        current = os.dup(self._root_fd)
        try:
            for segment in segments:
                with self._local(shown_path), contextlib.suppress(FileExistsError):
                    os.mkdir(segment, _DIR_MODE, dir_fd=current)
                try:
                    following = os.open(segment, _DIR_FLAGS, dir_fd=current)
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise refused from None
                    refusal = _failure(exc, shown_path, self.target)
                    raise refusal from exc
                os.close(current)
                current = following
        except BaseException:
            os.close(current)
            raise
        return current, self._anchor.joinpath(*segments)

    def write_file(self, file: ArtifactFile, chunks: Iterable[bytes]) -> None:
        """Write one file of the tree, verifying it as it arrives.

        Parameters
        ----------
        file
            The manifest entry; its path passed `check_manifest`.
        chunks
            The file's bytes.

        Raises
        ------
        ManifestError
            If the file's directory is, or became, a link out of the staging directory, or the
            file system refused a write (a name it does not allow, no space, a permission).
        ArtifactCorrupt
            If the size or the hash differs from the manifest. The file is removed.
        """
        *directories, leaf = file.path.split("/")
        shown_path = shown(file.path)
        directory_fd, directory = self._directory_of(directories, shown_path)
        temporary = f".{secrets.token_hex(8)}.lakana-part"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            with self._local(shown_path):
                if directory_fd is None:
                    descriptor = os.open(directory / temporary, flags, _FILE_MODE)
                else:
                    descriptor = os.open(temporary, flags, _FILE_MODE, dir_fd=directory_fd)
            try:
                self._fill(descriptor, file, chunks, shown_path, directory / temporary)
                with self._local(shown_path):
                    if directory_fd is None:
                        os.replace(directory / temporary, directory / leaf)
                    else:
                        os.rename(temporary, leaf, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            except BaseException:
                with contextlib.suppress(OSError):
                    if directory_fd is None:
                        os.unlink(directory / temporary)
                    else:
                        os.unlink(temporary, dir_fd=directory_fd)
                raise
        finally:
            if directory_fd is not None:
                os.close(directory_fd)

    def _fill(
        self,
        descriptor: int,
        file: ArtifactFile,
        chunks: Iterable[bytes],
        shown_path: str,
        temporary: Path,
    ) -> None:
        """Write the bytes of one file, check them against the manifest, then seal the file.

        Parameters
        ----------
        descriptor
            The open temporary file; closed here.
        file
            The manifest entry.
        chunks
            The bytes.
        shown_path
            The file's path, safe to show.
        temporary
            The temporary file's path, used only where descriptors are not.

        Raises
        ------
        ArtifactCorrupt
            If more bytes arrive than the manifest says (the rest is not read), or fewer, or
            others.
        ManifestError
            If the file system refused a write.
        """
        digest = hashlib.sha256()
        received = 0
        with os.fdopen(descriptor, "wb") as handle:
            for chunk in chunks:
                received += len(chunk)
                if received > file.size:
                    break
                digest.update(chunk)
                with self._local(shown_path):
                    handle.write(chunk)
            if received != file.size or digest.hexdigest() != file.sha256:
                raise ArtifactCorrupt(
                    f"The downloaded file {shown_path} does not match its manifest (size or "
                    "SHA-256); download it again."
                )
            with self._local(shown_path):
                handle.flush()
                if self._read_only and self._root_fd is not None:
                    os.fchmod(handle.fileno(), _SEALED_FILE_MODE)
        if self._read_only and self._root_fd is None:
            with self._local(shown_path):
                os.chmod(temporary, _SEALED_FILE_MODE)

    def commit(self) -> Path:
        """Move the finished tree to the target.

        A target that did not exist is replaced by the staging directory in one rename. One that
        exists (empty) keeps its identity: the staging directory's entries are moved into it
        one at a time, which is not atomic (see the module's docstring).

        Returns
        -------
        Path
            The target's real path.

        Raises
        ------
        ManifestError
            If something was put at the target meanwhile, the staging directory was replaced, or
            the move failed. Unless the move had already begun, the staging directory is removed.
        """
        try:
            if self._root_fd is None:
                self._check_staging_by_path()
            if self._inside:
                self._move_into_target()
            else:
                self._rename_to_target()
        except ManifestError:
            self.discard()
            raise
        except OSError as exc:
            self.discard()
            raise ManifestError(
                f"Cannot move the finished download into {self._at}: {exc.strerror}. Nothing "
                "was kept; fix that or choose another directory, then download again."
            ) from exc
        self._release()
        return self.target

    def _seal(self) -> None:
        """Seal every directory of the staging tree, itself included, deepest first."""
        self._sealed = True
        if self._root_fd is None:
            _chmod_dirs_by_path(self._anchor, _SEALED_DIR_MODE, deepest_first=True)
        else:
            _chmod_dirs(self._root_fd, _SEALED_DIR_MODE, deepest_first=True)

    def _seal_below_the_top(self) -> None:
        """Seal what is inside the top-level directories, deepest first, but not them.

        A directory that cannot be written to cannot be moved into another directory, so the
        top-level ones are sealed after the move (`_move_one`).
        """
        self._sealed = True
        if self._root_fd is None:
            with os.scandir(self._anchor) as entries:
                tops = [e.path for e in entries if e.is_dir(follow_symlinks=False)]
            for top in tops:
                _chmod_dirs_by_path(
                    Path(top), _SEALED_DIR_MODE, deepest_first=True, include_self=False
                )
            return
        for entry in os.listdir(self._root_fd):
            if stat.S_ISDIR(os.stat(entry, dir_fd=self._root_fd, follow_symlinks=False).st_mode):
                child = os.open(entry, _DIR_FLAGS, dir_fd=self._root_fd)
                try:
                    _chmod_dirs(child, _SEALED_DIR_MODE, deepest_first=True, include_self=False)
                finally:
                    os.close(child)

    def _replaced(self, root_fd: int, parent_fd: int) -> bool:
        """Say whether the staging directory's name leads to another directory than this writer's.

        Parameters
        ----------
        root_fd
            The descriptor of the directory this writer made.
        parent_fd
            The descriptor of the directory the staging name is in.

        Returns
        -------
        bool
            `True` if something else is at the name now.
        """
        now = os.stat(self.staging.name, dir_fd=parent_fd, follow_symlinks=False)
        mine = os.fstat(root_fd)
        return (now.st_dev, now.st_ino) != (mine.st_dev, mine.st_ino)

    def _exists(self, name: str, directory_fd: int | None, directory: Path) -> bool:
        """Say whether a name is taken in a directory, links included, without following any.

        Parameters
        ----------
        name
            The entry name.
        directory_fd
            A descriptor of the directory, or `None` to use `directory`.
        directory
            The directory's path.

        Returns
        -------
        bool
            `True` if something is there.
        """
        try:
            if directory_fd is None:
                os.lstat(directory / name)
            else:
                os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def _abandon(self, root_fd: int) -> None:
        """Remove what this writer put in its own directory, and let go of the descriptors.

        The directory is the one the descriptor refers to, whatever its name leads to now; it is
        made writable first (a sealed tree could not be emptied).

        Parameters
        ----------
        root_fd
            The descriptor of the directory this writer made.
        """
        _chmod_dirs(root_fd, _DIR_MODE, deepest_first=False)
        _remove_children(root_fd)
        self._release()

    def _rename_to_target(self) -> None:
        """Rename the staging directory to the target, which did not exist.

        Raises
        ------
        ManifestError
            If something is at the target now, or the directory there is not the one written.
        """
        if self._read_only:
            self._seal()
        taken = ManifestError(_TAKEN.format(target=self._at))
        if self._exists(self.target.name, self._parent_fd, self.target.parent):
            raise taken
        root_fd, parent_fd = self._root_fd, self._parent_fd
        if root_fd is None or parent_fd is None:
            os.rename(self._anchor, self.target)
            return
        if self._replaced(root_fd, parent_fd):
            self._abandon(root_fd)
            raise ManifestError(
                f"The directory this download was written in was replaced by another before it "
                f"was moved to {self._at}, so nothing was kept and {self._at} was not "
                f"created. Check who else writes in {self._at_parent}, then download again."
            )
        os.rename(self.staging.name, self.target.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        here = os.stat(self.target.name, dir_fd=parent_fd, follow_symlinks=False)
        if _inode_of(here) != _inode_of(os.fstat(root_fd)):
            # Someone swapped the staging directory for theirs between the check and the rename:
            # what is at the target is not ours and stays; only what this writer put in its own
            # directory is removed.
            self._abandon(root_fd)
            raise ManifestError(
                f"The directory this download was written in was replaced by another while it "
                f"was moved to {self._at}, so nothing was kept. The directory now at "
                f"{self._at} is not the one this download wrote: check it, and who else "
                f"writes in {self._at_parent}, then download again."
            )

    def _move_into_target(self) -> None:
        """Move the staging directory's entries into the existing, empty target.

        Raises
        ------
        ManifestError
            If the target gained entries meanwhile, the staging directory was replaced, or the
            move stopped half way (then nothing is removed: the message says what is where).
        OSError
            If the file system refused the very first move.
        """
        taken = ManifestError(_TAKEN.format(target=self._at))
        listing = (
            os.listdir(self._parent_fd) if self._parent_fd is not None else os.listdir(self.target)
        )
        if listing != [self.staging.name]:
            raise taken
        if (
            self._root_fd is not None
            and self._parent_fd is not None
            and self._replaced(self._root_fd, self._parent_fd)
        ):
            _remove_children(self._root_fd)
            self._release()
            raise ManifestError(
                f"The directory this download was written in was replaced by another, so "
                f"nothing was kept and {self._at} was left as it is. Check who else writes "
                "there, then download again."
            )
        names = os.listdir(self._root_fd) if self._root_fd is not None else os.listdir(self._anchor)
        if self._read_only:
            self._seal_below_the_top()
        moved = 0
        try:
            for name in names:
                self._move_one(name)
                moved += 1
        except OSError as exc:
            if moved:
                self._keep = True
                sealed = (
                    f" The copy is read-only, so run `chmod -R u+w {self._at}` first."
                    if self._read_only
                    else ""
                )
                raise ManifestError(
                    f"Moving the files into {self._at} stopped after {moved} of "
                    f"{len(names)} entries ({exc.strerror}). {self._at} holds some of the "
                    f"files and the hidden directory {self.staging.name} inside it holds the "
                    f"rest. Empty {self._at} (do not remove the directory itself: it may be "
                    f"a mount point or your working directory) and remove the hidden "
                    f"{self.staging.name} directory, then download again.{sealed}"
                ) from exc
            raise
        if self._parent_fd is None:
            os.rmdir(self._anchor)
        else:
            os.rmdir(self.staging.name, dir_fd=self._parent_fd)

    def _move_one(self, name: str) -> None:
        """Move one top-level entry into the target, then seal it if it is a directory.

        Parameters
        ----------
        name
            The entry's name.

        Raises
        ------
        FileExistsError
            If the name is taken in the target.
        """
        if self._exists(name, self._parent_fd, self.target):
            raise FileExistsError(errno.EEXIST, "an entry of that name appeared in it meanwhile")
        if self._root_fd is None:
            os.rename(self._anchor / name, self.target / name)
            if self._read_only and stat.S_ISDIR(os.lstat(self.target / name).st_mode):
                os.chmod(self.target / name, _SEALED_DIR_MODE)
            return
        directory = None
        if self._read_only and stat.S_ISDIR(
            os.stat(name, dir_fd=self._root_fd, follow_symlinks=False).st_mode
        ):
            directory = os.open(name, _DIR_FLAGS, dir_fd=self._root_fd)
        try:
            os.rename(name, name, src_dir_fd=self._root_fd, dst_dir_fd=self._parent_fd)
            if directory is not None:
                # A directory that cannot be written to cannot be moved to another parent, so a
                # top-level directory is sealed here, through the descriptor opened before the
                # move, and the descriptor is closed at once: one is open at a time, however many
                # top-level directories there are.
                os.fchmod(directory, _SEALED_DIR_MODE)
        finally:
            if directory is not None:
                os.close(directory)

    def _release(self) -> None:
        """Close the descriptors; the writer is finished."""
        self._done = True
        for descriptor in (self._root_fd, self._parent_fd):
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
        self._root_fd = self._parent_fd = None

    def discard(self) -> None:
        """Remove the staging directory and everything in it; a second call does nothing.

        Descriptors are used while they are still open, so what is removed is the directory this
        writer made, not whatever a name leads to now. A download whose move into the target had
        already begun is left as it is (the error said what is where).
        """
        if self._done:
            return
        if self._keep:
            self._release()
            return
        if self._root_fd is not None and self._parent_fd is not None:
            if self._sealed:
                with contextlib.suppress(OSError):
                    _chmod_dirs(self._root_fd, _DIR_MODE, deepest_first=False)
            shutil.rmtree(self.staging.name, dir_fd=self._parent_fd, ignore_errors=True)
            self._release()
            return
        self._release()
        if self._sealed:
            with contextlib.suppress(OSError):
                _chmod_dirs_by_path(self._anchor, _DIR_MODE, deepest_first=False)
        shutil.rmtree(self._anchor, ignore_errors=True)
