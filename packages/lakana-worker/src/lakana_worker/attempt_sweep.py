"""Removal of attempt directories a dead worker left behind.

A Python task's private directory lives under ``<worker cache>/attempts`` and
is removed when the attempt ends, as is a connection check's under the Docker
runner. A worker that is killed mid-task never gets there. `AttemptSweeper`
removes such leftovers, and it is the only code that deletes in that directory
without having created what it deletes, so it is deliberately narrow:

- it lists only the direct children of the worker's own work directory, and
  only when that directory is the private one the worker itself requires
  (a real directory, owned by the worker's user, mode 0700);
- it removes only a real directory, owned by the worker's user, whose name is
  one `tempfile.mkdtemp(prefix="attempt-")` produces, or the
  ``attempt-check_`` of a connection check's directory; anything else is left
  alone and logged once;
- removal is `remove_private_tree`, which never follows a symlink;
- a worker holds a shared `flock` on a descriptor of its attempt directory from
  its creation until the attempt is removed, and the sweeper removes a directory
  only while holding an exclusive, non-blocking one: a live attempt of this or
  of another worker sharing the cache is never swept, and the lock dies with
  the worker process, whatever the clocks say;
- age is the second guard: nothing younger than `max_age` is touched, which also
  covers the instant between `mkdtemp` and the worker's `flock`;
- a directory that user code set to mode 000 is made accessible again (mode 0700,
  never through a symlink) only after the age guard, and the lock and the
  inode check still follow; a live attempt older than `max_age` that did this finds
  its directory back at 0700, which its own user and open descriptors never needed.
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path

from lakana_core.identifiers import shown
from lakana_worker.processes import (
    is_private_directory,
    remove_private_tree,
    restore_directory_access,
)

logger = logging.getLogger(__name__)

_ATTEMPT_NAME = re.compile(r"attempt-(?:check_)?[a-z0-9_]{8}")
"""What `tempfile.mkdtemp` creates with the prefix ``attempt-`` (a task) or ``attempt-check_`` (a
connection check): eight lowercase letters, digits or ``_`` after it."""


_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _open_attempt(path: Path) -> int | None:
    """Open an old attempt directory, restoring access its own code took away.

    User code in a task can set its directory to mode 000, which stops the
    worker's user from opening it. This is called only for a directory that
    has already passed the age guard, and it runs before the lock and the
    inode check, which still decide whether anything is removed. As in
    `lakana_worker.processes`, the directory is made accessible again by
    `restore_directory_access`, relative to a descriptor of the work directory
    and never through a link; anything but a directory owned by the worker's
    user is refused.

    Parameters
    ----------
    path
        The directory.

    Returns
    -------
    int or None
        A descriptor, or `None` if it cannot be opened even after the restore.

    Raises
    ------
    FileNotFoundError
        If the directory is gone.
    """
    try:
        return os.open(path, _OPEN_FLAGS)
    except FileNotFoundError:
        raise
    except PermissionError:
        pass
    except OSError:
        return None
    try:
        parent = os.open(path.parent, _OPEN_FLAGS)
    except OSError:
        return None
    try:
        if not restore_directory_access(path.name, parent, path):
            return None
        try:
            return os.open(path.name, _OPEN_FLAGS, dir_fd=parent)
        except OSError:
            return None
    finally:
        os.close(parent)


def lock_exclusive_if_same(descriptor: int, path: Path) -> bool:
    """Take the sweeper's exclusive lock and check it is about the directory at `path`.

    The owner of a directory holds a shared `flock` on it for as long as it lives;
    this succeeds only when nobody does. Another sweeper may have removed the
    directory and a worker created a new one of the same name while this one
    waited, so after locking, the descriptor and the path must be one inode.

    Parameters
    ----------
    descriptor
        An open descriptor of the directory.
    path
        The directory's path.

    Returns
    -------
    bool
        `True` if the lock is held and `path` still is that directory; `False` if
        somebody else holds a lock, or the path is gone or another directory now.

    Raises
    ------
    OSError
        If the lock fails for any reason but being held (e.g. an unsupported filesystem).
    """
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    try:
        held, current = os.fstat(descriptor), os.lstat(path)
    except FileNotFoundError:
        return False
    return (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino)


class AttemptSweeper:
    """Remove old attempt directories nobody holds locked, at most once per interval.

    Parameters
    ----------
    work_dir
        The worker's ``attempts`` directory.
    max_age
        Seconds an attempt directory must have gone unchanged before it is
        removed; longer than a task can run.
    interval
        Seconds between sweeps; 0 disables `maybe_sweep`.
    clock
        Wall-clock time in seconds; compared with directory mtimes.
    monotonic
        Monotonic time in seconds; paces `maybe_sweep`.
    """

    def __init__(
        self,
        work_dir: Path,
        *,
        max_age: float,
        interval: float,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._work_dir = work_dir
        self._max_age = max_age
        self._interval = interval
        self._clock = clock
        self._monotonic = monotonic
        self._held: dict[str, int] = {}
        self._held_lock = threading.Lock()
        self._warned: set[str] = set()
        self._last: float | None = None

    def hold(self, attempt_dir: Path) -> None:
        """Lock an attempt directory shared for as long as its task runs.

        Parameters
        ----------
        attempt_dir
            The directory the task runs in, just created.

        Raises
        ------
        OSError
            If the directory cannot be opened or locked; the caller removes it and fails
            the attempt.
        """
        descriptor = os.open(
            attempt_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH)
        except OSError:
            os.close(descriptor)
            raise
        with self._held_lock:
            self._held[attempt_dir.name] = descriptor

    def release(self, attempt_dir: Path) -> None:
        """Drop the lock of an attempt directory; safe to call twice.

        Parameters
        ----------
        attempt_dir
            The directory the task ran in.
        """
        with self._held_lock:
            descriptor = self._held.pop(attempt_dir.name, None)
        if descriptor is not None:
            os.close(descriptor)

    def holding(self) -> int:
        """Count the attempt directories this worker holds.

        Returns
        -------
        int
            How many `hold` calls have not been released.
        """
        with self._held_lock:
            return len(self._held)

    def maybe_sweep(self) -> None:
        """Sweep if none has run for `interval` seconds; the first call sweeps at once."""
        if self._interval <= 0:
            return
        now = self._monotonic()
        if self._last is not None and now - self._last < self._interval:
            return
        self._last = now
        removed = self.sweep()
        if removed:
            logger.info("Removed %d orphaned task attempt director(ies).", removed)

    def sweep(self) -> int:
        """Remove every old, unheld attempt directory.

        Returns
        -------
        int
            How many directories were removed.
        """
        if not os.path.lexists(self._work_dir):
            return 0
        if not is_private_directory(self._work_dir, 0o077):
            self._warn_once(
                str(self._work_dir),
                "The task directory %s is not private (a real directory owned by the worker's "
                "user, mode 0700); its old attempt directories are not swept.",
            )
            return 0
        try:
            names = os.listdir(self._work_dir)
        except OSError as exc:
            logger.warning(
                "The task directory %s could not be listed (%s); not swept.",
                self._work_dir,
                exc.strerror or "filesystem error",
            )
            return 0
        removed = 0
        for name in sorted(names):
            removed += self._sweep_entry(name)
        return removed

    def _sweep_entry(self, name: str) -> int:
        """Remove one entry of the work directory if it is an old, unheld attempt directory.

        Parameters
        ----------
        name
            The entry's name.

        Returns
        -------
        int
            1 if it was removed, else 0.
        """
        path = self._work_dir / name
        if _ATTEMPT_NAME.fullmatch(name) is None:
            self._warn_once(
                name, "Left alone in the task directory: %s is not an attempt directory."
            )
            return 0
        try:
            info = os.lstat(path)
        except OSError:
            return 0
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            self._warn_once(
                name,
                "Left alone in the task directory: %s is not a directory the worker created.",
            )
            return 0
        if self._clock() - info.st_mtime <= self._max_age:
            return 0
        try:
            descriptor = _open_attempt(path)
        except FileNotFoundError:
            return 0
        if descriptor is None:
            self._warn_once(name, "Left alone in the task directory: %s could not be opened.")
            return 0
        try:
            # What was opened, not the path looked at before, must pass the same rules.
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or self._clock() - opened.st_mtime <= self._max_age
            ):
                return 0
            try:
                if not lock_exclusive_if_same(descriptor, path):
                    return 0
            except OSError as exc:
                if "flock" not in self._warned:
                    self._warned.add("flock")
                    logger.warning(
                        "The lock on task directory %s could not be taken (%s); directories "
                        "are not swept while that fails.",
                        path,
                        exc.strerror or "filesystem error",
                    )
                return 0
            return 1 if remove_private_tree(path) else 0
        finally:
            os.close(descriptor)

    def _warn_once(self, key: str, message: str) -> None:
        """Log a warning the first time `key` is seen.

        Parameters
        ----------
        key
            What the warning is about; also its one argument.
        message
            A ``%s`` format string.
        """
        if key not in self._warned:
            self._warned.add(key)
            logger.warning(message, shown(key))
