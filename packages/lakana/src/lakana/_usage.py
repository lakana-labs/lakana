"""What a task process reports about itself: the bytes its readers took in, and its peak memory.

The readers of `lakana.ingest` and the table reads of `lakana.resources.queries` call
`add_bytes_read` where bytes enter the process, once each. The result record carries the total
(`record_fields`), which the worker stores with the attempt. Standard library only, and nothing
of the other Lakana packages: this runs in the task's own environment.
"""

from __future__ import annotations

import re
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

_lock = threading.Lock()
_total: int | None = None
_STATUS = Path("/proc/self/status")
_HWM = re.compile(r"^VmHWM:\s+(\d+) kB$", re.MULTILINE)


def add_bytes_read(count: int) -> None:
    """Count bytes a reader took in.

    Parameters
    ----------
    count
        The number of bytes. A negative number is ignored; zero counts as a reader that ran and
        read nothing (the total is then 0, not unknown).
    """
    global _total
    if count < 0:
        return
    with _lock:
        _total = (_total or 0) + count


def bytes_read() -> int | None:
    """Give the bytes counted since the process started.

    Returns
    -------
    int or None
        The total, or `None` if no reader has counted yet.
    """
    with _lock:
        return _total


def reset() -> None:
    """Forget the total; for tests."""
    global _total
    with _lock:
        _total = None


# jscpd:ignore-start
# A copy of `lakana_worker.own_peak.read_peak_bytes` and `own_peak_bytes`: this runs in the task's
# own environment, which has the SDK and nothing of the worker, and the worker's query child may
# import no SDK either.
def read_peak_bytes(status: Path) -> int | None:
    """Read the high-water mark of resident memory from a ``/proc/<pid>/status`` file.

    Parameters
    ----------
    status
        The file.

    Returns
    -------
    int or None
        ``VmHWM`` in bytes, or `None` if the file or the line is not there.
    """
    try:
        match = _HWM.search(status.read_text())
    except (OSError, ValueError):
        return None
    return None if match is None else int(match.group(1)) * 1024


def own_peak_bytes(
    platform: str = sys.platform, getrusage: Callable[[int], Any] | None = None
) -> int | None:
    """Give this process's own peak resident memory.

    Parameters
    ----------
    platform
        ``sys.platform``.
    getrusage
        ``resource.getrusage``; given by tests, otherwise the real one (macOS only).

    Returns
    -------
    int or None
        On Linux ``VmHWM`` in bytes; on macOS ``ru_maxrss`` of this process, which is in bytes
        there and is the process's own (it is not inherited from the parent). `None` elsewhere,
        and where the reading is missing or not above zero.
    """
    if platform == "darwin":
        import resource  # no such module on Windows, so only here

        read = getrusage or resource.getrusage
        peak = int(read(resource.RUSAGE_SELF).ru_maxrss)
        return peak if peak > 0 else None
    if platform.startswith("linux"):
        return read_peak_bytes(_STATUS)
    return None


# jscpd:ignore-end


def record_fields() -> dict[str, int]:
    """Give the figures this process adds to its result record.

    Returns
    -------
    dict
        ``bytes_read`` once a reader has counted, and ``peak_memory_bytes`` (this process's
        own peak: ``VmHWM`` on Linux, ``ru_maxrss`` on macOS). Empty when neither is known.
    """
    fields: dict[str, int] = {}
    counted = bytes_read()
    if counted is not None:
        fields["bytes_read"] = counted
    peak = own_peak_bytes()
    if peak is not None:
        fields["peak_memory_bytes"] = peak
    return fields
