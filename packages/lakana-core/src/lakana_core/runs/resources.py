"""What an attempt used: peak memory, processor time and bytes read.

A figure is `None` when nobody measured it. Zero is a measurement, so the two never mix.
"""

from __future__ import annotations

import math
import operator
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

_Figure = TypeVar("_Figure", int, float)
"""An optional figure: a count or a duration."""
MAX_BYTES = 2**63 - 1
"""The largest count the database columns (signed 64-bit) hold."""


@dataclass(frozen=True)
class AttemptResources:
    """The figures measured for one attempt or one query run; any may be unknown.

    Parameters
    ----------
    peak_memory_bytes
        Highest resident memory reached, in bytes, or `None` if not measured.
    cpu_seconds
        Processor time used (user plus system), in seconds, or `None` if not measured.
    bytes_read
        Bytes read from tables, files and sources, or `None` if not counted.
    """

    peak_memory_bytes: int | None = None
    cpu_seconds: float | None = None
    bytes_read: int | None = None

    def merged(self, other: AttemptResources) -> AttemptResources:
        """Combine two measurements of the same attempt.

        Memory is a high-water mark, so the larger one stands; processor time and bytes read
        add up. A figure unknown on one side is taken from the other, and stays unknown if
        both are.

        Parameters
        ----------
        other
            The second measurement.

        Returns
        -------
        AttemptResources
            The combined figures.
        """
        return AttemptResources(
            peak_memory_bytes=_combine(self.peak_memory_bytes, other.peak_memory_bytes, max),
            cpu_seconds=_combine(self.cpu_seconds, other.cpu_seconds, operator.add),
            bytes_read=_combine(self.bytes_read, other.bytes_read, operator.add),
        )

    @property
    def known(self) -> bool:
        """Say whether any figure was measured.

        Returns
        -------
        bool
            `True` if at least one figure is not `None`.
        """
        return not (
            self.peak_memory_bytes is None and self.cpu_seconds is None and self.bytes_read is None
        )


def _combine(
    a: _Figure | None, b: _Figure | None, both: Callable[[_Figure, _Figure], _Figure]
) -> _Figure | None:
    """Combine two optional figures; an unknown one is ignored.

    Parameters
    ----------
    a
        The first, or `None`.
    b
        The second, or `None`.
    both
        How two known figures combine (`operator.add` or `max`).

    Returns
    -------
    int or float or None
        `None` only if both are `None`.
    """
    if a is None or b is None:
        return b if a is None else a
    return both(a, b)


def valid_count(value: object) -> int | None:
    """Accept a byte count reported by a process we do not trust.

    Parameters
    ----------
    value
        What the process reported.

    Returns
    -------
    int or None
        The value if it is an `int` (not a `bool`) from 0 to `MAX_BYTES`, else `None`.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= MAX_BYTES else None


def valid_seconds(value: object) -> float | None:
    """Accept a duration reported by a process we do not trust.

    Parameters
    ----------
    value
        What the process reported.

    Returns
    -------
    float or None
        The value if it is a finite `int` or `float` (not a `bool`) of at least 0, else `None`.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None
