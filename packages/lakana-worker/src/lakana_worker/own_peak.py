"""A process's own peak resident memory, read from ``/proc`` (Linux).

Standard library only: the query child imports this, and may not import the core.
"""

from __future__ import annotations

import re
from pathlib import Path

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


def own_peak_bytes() -> int | None:
    """Give this process's own peak resident memory.

    Returns
    -------
    int or None
        ``VmHWM`` in bytes; `None` where there is no ``/proc`` (macOS).
    """
    return read_peak_bytes(_STATUS)
