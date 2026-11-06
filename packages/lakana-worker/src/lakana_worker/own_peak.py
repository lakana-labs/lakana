"""A process's own peak resident memory: ``/proc`` on Linux, ``getrusage`` on macOS.

Standard library only: the query child imports this, and may not import the core.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

_STATUS = Path("/proc/self/status")
_HWM = re.compile(r"^VmHWM:\s+(\d+) kB$", re.MULTILINE)


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
