"""The resource figures of a child process: peak memory, processor time.

The kernel's ``ru_maxrss`` of a reaped child is not a measure of the child on Linux: at
``exec`` the kernel folds the high-water mark of the process that started it into the child's
(measured: 11 MiB from a small worker, 411 MiB after the worker touched 400 MiB). So on Linux the
peak is the child's own ``VmHWM`` (`lakana_worker.own_peak`), which the child reports about
itself at its end, or the last reading of the memory watchdog for a child that was killed.
Processor time is not inherited and always comes from ``wait4``. macOS has no ``/proc``; its
``ru_maxrss`` (in bytes) is used there.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Generator
from contextvars import ContextVar
from typing import Protocol

from lakana_core.runs.resources import AttemptResources


class Rusage(Protocol):
    """The part of ``resource.struct_rusage`` the figures are made from."""

    @property
    def ru_maxrss(self) -> int:
        """Peak resident size: kilobytes on Linux, bytes on macOS."""
        ...

    @property
    def ru_utime(self) -> float:
        """User processor time in seconds."""
        ...

    @property
    def ru_stime(self) -> float:
        """System processor time in seconds."""
        ...


def maxrss_bytes(ru_maxrss: int, platform: str = sys.platform) -> int:
    """Convert ``ru_maxrss`` to bytes.

    Parameters
    ----------
    ru_maxrss
        The value of ``ru_maxrss``.
    platform
        ``sys.platform``; macOS reports bytes, everything else kilobytes.

    Returns
    -------
    int
        The size in bytes.
    """
    return ru_maxrss if platform == "darwin" else ru_maxrss * 1024


def from_rusage(usage: Rusage, platform: str = sys.platform) -> AttemptResources:
    """Make the figures of a reaped child from its resource usage.

    Parameters
    ----------
    usage
        The usage ``os.wait4`` returned.
    platform
        ``sys.platform``.

    Returns
    -------
    AttemptResources
        The ``ru_maxrss`` peak and the processor time; ``bytes_read`` is unknown here.
    """
    return AttemptResources(
        peak_memory_bytes=maxrss_bytes(usage.ru_maxrss, platform),
        cpu_seconds=usage.ru_utime + usage.ru_stime,
        bytes_read=None,
    )


def peak_memory(
    platform: str, *, rusage: int | None, reported: int | None, watched: int | None
) -> int | None:
    """Choose the peak memory of a reaped child.

    Parameters
    ----------
    platform
        ``sys.platform``.
    rusage
        The child's ``ru_maxrss`` in bytes; used on macOS only.
    reported
        The peak the child reported about itself, or `None` if it did not (it was killed).
    watched
        The last resident size the memory watchdog read before it killed the child, or `None`.

    Returns
    -------
    int or None
        On macOS the ``rusage`` figure. Elsewhere the larger of the two readings, or `None`
        when there is none: a guess is never made.
    """
    if platform == "darwin":
        return rusage
    readings = [value for value in (reported, watched) if value is not None]
    return max(readings) if readings else None


class UsageCollector:
    """Collects the figures of every query child that runs while it is current.

    One collector belongs to one unit of work (one run); see `usage_collector`.
    """

    def __init__(self) -> None:
        self._total = AttemptResources()

    def add(self, resources: AttemptResources) -> None:
        """Add the figures of one reaped child.

        Parameters
        ----------
        resources
            The child's figures.
        """
        self._total = self._total.merged(resources)

    @property
    def resources(self) -> AttemptResources:
        """The figures of all children added so far: the largest peak, the sums of the rest."""
        return self._total


_COLLECTOR: ContextVar[UsageCollector | None] = ContextVar("lakana_usage_collector", default=None)


def current_collector() -> UsageCollector | None:
    """Give the collector of the unit of work that is running.

    Returns
    -------
    UsageCollector or None
        The innermost `usage_collector` of this context, or `None` outside one.
    """
    return _COLLECTOR.get()


@contextlib.contextmanager
def usage_collector() -> Generator[UsageCollector, None, None]:
    """Make a fresh collector current for the block; an outer one is not shared with it.

    Yields
    ------
    UsageCollector
        The collector; it keeps its figures after the block.
    """
    collector = UsageCollector()
    token = _COLLECTOR.set(collector)
    try:
        yield collector
    finally:
        _COLLECTOR.reset(token)
