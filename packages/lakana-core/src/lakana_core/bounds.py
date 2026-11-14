"""Bounds that several parts of core share, defined once.

A version number, a retention count and a lineage source's version are all stored in a 32-bit
integer column; a SHA-256 digest is written as 64 lower-case hex digits wherever it is stored.
"""

from __future__ import annotations

import re

MAX_INT32 = 2**31 - 1
"""The largest value a 32-bit integer column holds: the largest version, retention or count."""

SHA256_HEX = re.compile(r"\A[0-9a-f]{64}\Z")
"""A SHA-256 digest as stored: exactly 64 lower-case hex digits."""
