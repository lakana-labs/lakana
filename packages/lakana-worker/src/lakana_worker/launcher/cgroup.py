"""Read a container's peak memory and processor time from its cgroup.

The launcher holds the Docker socket, so it can name a task's container; the host's cgroup tree,
mounted read-only (`LAKANA_LAUNCHER__CGROUP_ROOT`), holds the kernel's figures for it. Everything
here is best effort: a file that is missing, unreadable or malformed, cgroup v1, or a container
that is already gone gives no figure, never a guess and never an exception. This module imports
the standard library only (the ``launcher-stands-alone`` contract).
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("lakana_worker.launcher")

DEFAULT_ROOT = Path("/sys/fs/cgroup")
"""Where the host's cgroup v2 tree is when the launcher runs on the host."""
_ID = re.compile(r"[0-9a-f]{64}")
_MAX_READ = 4096
_MAX_INT64 = (1 << 63) - 1
_DIGITS = re.compile(r"[0-9]{1,19}")


@dataclass(frozen=True)
class CgroupReading:
    """What the cgroup said; either figure may be unknown.

    Parameters
    ----------
    peak_memory_bytes
        ``memory.peak`` in bytes, or `None`.
    cpu_seconds
        ``usage_usec`` of ``cpu.stat`` in seconds, or `None`.
    """

    peak_memory_bytes: int | None
    cpu_seconds: float | None


def cgroup_directory(container_id: str, root: Path = DEFAULT_ROOT) -> Path | None:
    """Find a container's cgroup directory.

    Parameters
    ----------
    container_id
        The id Docker returned; only 64 lower-case hex digits build a path.
    root
        The cgroup tree.

    Returns
    -------
    Path or None
        The first existing of ``system.slice/docker-<id>.scope`` (systemd driver),
        ``docker/<id>`` (cgroupfs driver) and ``<id>``; `None` if none exists or the id is bad.
    """
    if _ID.fullmatch(container_id) is None:
        return None
    for relative in (
        f"system.slice/docker-{container_id}.scope",
        f"docker/{container_id}",
        container_id,
    ):
        candidate = root / relative
        if candidate.is_dir():
            return candidate
    return None


def _text(path: Path) -> str | None:
    """Read at most 4 KiB of a small kernel file.

    Parameters
    ----------
    path
        The file.

    Returns
    -------
    str or None
        Its text, or `None` if it cannot be read.
    """
    try:
        with path.open("rb") as handle:
            return handle.read(_MAX_READ).decode("ascii", errors="replace")
    except OSError:
        return None


def _count(text: str | None) -> int | None:
    """Parse a non-negative decimal integer, nothing else.

    Parameters
    ----------
    text
        File content or one field.

    Returns
    -------
    int or None
        The number, or `None` for ``max``, a sign, a fraction, an overflow or no text.
    """
    if text is None or _DIGITS.fullmatch(text.strip()) is None:
        return None
    value = int(text)
    return value if value <= _MAX_INT64 else None


def read_cgroup(directory: Path) -> CgroupReading:
    """Read the two figures from a cgroup v2 directory.

    Parameters
    ----------
    directory
        The container's cgroup.

    Returns
    -------
    CgroupReading
        ``memory.peak`` (kernel 5.19 and later) and ``usage_usec``; each `None` when absent or
        malformed.
    """
    peak = _count(_text(directory / "memory.peak"))
    micros = None
    for line in (_text(directory / "cpu.stat") or "").splitlines():
        name, _, value = line.partition(" ")
        if name == "usage_usec":
            micros = _count(value)
            break
    return CgroupReading(peak, None if micros is None else micros / 1_000_000)


class CgroupSampler:
    """Read a running container's cgroup in the background; keep the highest memory.

    It reads at once, then every `interval` seconds, and the launcher reads once more when the
    output ends and again after the container is waited for. The cgroup is deleted when the
    container's process ends, so the figures are the highest values seen by the last successful
    read: lower bounds, and none for a task that ended before the first read.

    Parameters
    ----------
    container_id
        The container's id.
    interval
        Seconds between reads.
    root
        The cgroup tree.
    sleep
        Waits `interval` seconds and returns `True` once `stop` was called; tests inject it.
    """

    def __init__(
        self,
        container_id: str,
        *,
        interval: float = 0.25,
        root: Path = DEFAULT_ROOT,
        sleep: Callable[[float], bool] | None = None,
    ) -> None:
        self._id = container_id
        self._interval = interval
        self._root = root
        self._done = threading.Event()
        self._sleep = sleep or self._done.wait
        self._peak: int | None = None
        self._cpu: float | None = None
        self._logged = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start sampling in a daemon thread; if none can start, there are no samples."""
        try:
            thread = threading.Thread(target=self._run, daemon=True, name="cgroup-sampler")
            thread.start()
        except RuntimeError as error:
            logger.debug("cgroup sampler not started: %s", error)
            return
        self._thread = thread

    def sample(self) -> None:
        """Read once now, e.g. as the container's output ends, before its cgroup is deleted."""
        self._sample()

    def stop(self) -> CgroupReading:
        """Stop sampling, read once more and give what was seen.

        Returns
        -------
        CgroupReading
            The highest memory and the latest processor time seen; unknown if never readable.
        """
        self._done.set()
        if self._thread is not None:
            self._thread.join(self._interval + 5)
        self._sample()
        return CgroupReading(self._peak, self._cpu)

    def _run(self) -> None:
        """Read at once, then sample until stopped."""
        self._sample()
        while not self._sleep(self._interval):
            self._sample()

    def _sample(self) -> None:
        """Read once; failures are logged once at debug level and swallowed."""
        try:
            directory = cgroup_directory(self._id, self._root)
            if directory is None:
                return
            reading = read_cgroup(directory)
            if reading.peak_memory_bytes is not None:
                self._peak = max(self._peak or 0, reading.peak_memory_bytes)
            if reading.cpu_seconds is not None:
                self._cpu = reading.cpu_seconds
        except Exception as error:  # a figure is never worth failing a task
            if not self._logged:
                self._logged = True
                logger.debug("cgroup figures unavailable: %s", error)
