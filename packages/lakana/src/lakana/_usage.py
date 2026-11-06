"""What a task process reports about itself: the bytes its readers took in, and its peak memory.

The readers of `lakana.ingest` and the table reads of `lakana.resources.queries` call
`add_bytes_read` where bytes enter the process, once each. The result record carries the total
(`record_fields`), which the worker stores with the attempt. Standard library only, and nothing
of the other Lakana packages: this runs in the task's own environment.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

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
# A copy of `lakana_worker.own_peak.read_peak_bytes`: this runs in the task's own environment, which
# has the SDK and nothing of the worker, and the worker's query child may import no SDK either.
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


# jscpd:ignore-end


def record_fields() -> dict[str, int]:
    """Give the figures this process adds to its result record.

    Returns
    -------
    dict
        ``bytes_read`` once a reader has counted, and ``peak_memory_bytes`` (this process's own
        ``VmHWM``) where ``/proc`` has it (Linux). Empty when neither is known.
    """
    fields: dict[str, int] = {}
    counted = bytes_read()
    if counted is not None:
        fields["bytes_read"] = counted
    peak = read_peak_bytes(_STATUS)
    if peak is not None:
        fields["peak_memory_bytes"] = peak
    return fields
