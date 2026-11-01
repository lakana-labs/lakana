"""Handing the memory a large write freed back to the system.

A history write runs in the worker for a SQL asset and in the server for the REST route a Python
asset calls; both call `trim_heap` after it, so it lives here rather than in either package.
"""

from __future__ import annotations

import ctypes
import gc
import sys


def _libc() -> ctypes.CDLL:
    """Load the C library the process already uses.

    Returns
    -------
    ctypes.CDLL
        The library; its functions are looked up by name.

    Raises
    ------
    OSError
        If it cannot be loaded.
    """
    return ctypes.CDLL(None)


def trim_heap(platform: str = sys.platform) -> None:
    """Hand the memory a large write freed back to the system.

    A history write builds and drops millions of Python objects (pyiceberg's manifests and
    records, the diff's lists). Some sit in reference cycles that only the cyclic collector
    frees, and glibc keeps the pages it freed; both would count against every later query under
    the worker's memory limit, and against the server's memory the same way. Measured in the
    worker at a million open versions of 8 columns, all changed: two writes in a row left the
    process about 1.7 GiB above its start, and about 0.3 GiB with this (`gc.collect`, then
    `malloc_trim(0)`, about 2.5 s), which also lowered the second write's peak from about 3.0 to
    2.3 GiB. The collection runs everywhere; the trim only on
    Linux with glibc (musl has no `malloc_trim`, macOS and Windows another allocator).

    Parameters
    ----------
    platform
        `sys.platform`; a parameter so that a test can pretend another one.
    """
    gc.collect()
    if not platform.startswith("linux"):
        return
    try:
        trim = _libc().malloc_trim
    except (OSError, AttributeError):
        return
    trim(0)
