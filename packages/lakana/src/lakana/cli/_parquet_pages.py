"""Read the page headers of a Parquet column chunk, never its pages.

A page's header says how large the page is once decompressed, and the reader decompresses it
to exactly that size, so the headers bound what decoding a column chunk can take: no value is
larger than the page it is decoded from, a dictionary page included. That holds whatever the
encoding: a dictionary or shared prefixes (`DELTA_BYTE_ARRAY`) can make 1,024 rows of a large
value out of a few bytes, but never one value larger than its page.

Headers are Thrift structs in the compact protocol: `PageHeader` field 1 is the page type,
field 2 its uncompressed size and field 3 its compressed size, all `i32`; the rest is skipped.
"""

from __future__ import annotations

import mmap
from dataclasses import dataclass

Buffer = bytes | mmap.mmap
"""The file's bytes: a memory map of it, or bytes in a test."""
_I32 = 5
_STRUCT = 12
_FIXED = {1: 0, 2: 0, 3: 1, 7: 8, 13: 16}
"""Bytes a value of each fixed-size compact type takes (booleans live in the type nibble)."""
_VARINT = frozenset({4, 5, 6})
_BINARY = 8
_LIST = frozenset({9, 10})
_MAP = 11
_MAX_DEPTH = 16


class PageHeaderError(ValueError):
    """A page header that cannot be read: truncated, malformed or nested too deeply."""


@dataclass(frozen=True)
class ChunkPages:
    """What the page headers of one column chunk say.

    Parameters
    ----------
    total
        The pages' uncompressed sizes added up.
    largest
        The largest page's uncompressed size: no value decodes larger.
    """

    total: int
    largest: int


def _varint(buf: Buffer, at: int) -> tuple[int, int]:
    """Read an unsigned variable-length integer.

    Parameters
    ----------
    buf
        The bytes.
    at
        Where it starts.

    Returns
    -------
    tuple of int
        The value and the position after it.

    Raises
    ------
    PageHeaderError
        For more than 10 bytes, more than any 64-bit value takes.
    """
    value = 0
    for shift in range(0, 70, 7):
        byte = buf[at]
        at += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, at
    raise PageHeaderError("a variable-length integer is longer than 10 bytes")


def _zigzag(value: int) -> int:
    """Decode a zigzag-encoded signed integer.

    Parameters
    ----------
    value
        The encoded value.

    Returns
    -------
    int
        The signed value.
    """
    return (value >> 1) ^ -(value & 1)


def _skip(buf: Buffer, at: int, kind: int, depth: int) -> int:
    """Skip one value of a compact type.

    Parameters
    ----------
    buf
        The bytes.
    at
        Where the value starts.
    kind
        Its compact type.
    depth
        How deeply it is nested.

    Returns
    -------
    int
        The position after it.

    Raises
    ------
    PageHeaderError
        For an unknown type or nesting deeper than 16 levels.
    """
    if depth > _MAX_DEPTH:
        raise PageHeaderError(f"a page header nests deeper than {_MAX_DEPTH} levels")
    if kind in _FIXED:
        return at + _FIXED[kind]
    if kind in _VARINT:
        return _varint(buf, at)[1]
    if kind == _BINARY:
        size, at = _varint(buf, at)
        return at + size
    if kind in _LIST:
        head = buf[at]
        size, at = head >> 4, at + 1
        if size == 15:
            size, at = _varint(buf, at)
        return _skip_many(buf, at, head & 0x0F, size, depth)
    if kind == _MAP:
        size, at = _varint(buf, at)
        if not size:
            return at
        head, at = buf[at], at + 1
        for _ in range(size):
            at = _skip(buf, at, head >> 4, depth + 1)
            at = _skip(buf, at, head & 0x0F, depth + 1)
        return at
    if kind == _STRUCT:
        return _fields(buf, at, depth + 1)[1]
    raise PageHeaderError(f"a page header holds an unknown field type {kind}")


def _skip_many(buf: Buffer, at: int, kind: int, size: int, depth: int) -> int:
    """Skip the elements of a list or a set.

    Parameters
    ----------
    buf
        The bytes.
    at
        Where the first element starts.
    kind
        The elements' compact type.
    size
        How many there are.
    depth
        How deeply the list is nested.

    Returns
    -------
    int
        The position after the last element.
    """
    if kind in (1, 2):
        return at + size
    if kind in _FIXED:
        return at + size * _FIXED[kind]
    for _ in range(size):
        at = _skip(buf, at, kind, depth + 1)
    return at


def _fields(buf: Buffer, at: int, depth: int) -> tuple[dict[int, int], int]:
    """Read a struct, keeping its `i32` fields and skipping the rest.

    Parameters
    ----------
    buf
        The bytes.
    at
        Where the struct starts.
    depth
        How deeply it is nested.

    Returns
    -------
    tuple
        The `i32` fields by field id, and the position after the struct.
    """
    found: dict[int, int] = {}
    field = 0
    while True:
        head = buf[at]
        at += 1
        kind = head & 0x0F
        if not kind:
            return found, at
        if head >> 4:
            field += head >> 4
        else:
            raw, at = _varint(buf, at)
            field = _zigzag(raw)
        if kind == _I32:
            raw, at = _varint(buf, at)
            found[field] = _zigzag(raw)
        else:
            at = _skip(buf, at, kind, depth)


def page_header(buf: Buffer, at: int) -> tuple[int, int, int]:
    """Read the page header that starts at `at`.

    Parameters
    ----------
    buf
        The file's bytes.
    at
        Where the header starts.

    Returns
    -------
    tuple of int
        The page's uncompressed size, its compressed size, and where the page itself starts.

    Raises
    ------
    PageHeaderError
        For a header that is truncated, malformed, or lacks a size.
    """
    try:
        found, end = _fields(buf, at, 0)
    except IndexError:
        raise PageHeaderError("a page header ends early") from None
    kind, uncompressed, compressed = found.get(1, -1), found.get(2, -1), found.get(3, -1)
    if min(kind, uncompressed, compressed) < 0:
        raise PageHeaderError("a page header lacks its type or sizes")
    return uncompressed, compressed, end


def chunk_pages(buf: Buffer, start: int, length: int) -> ChunkPages:
    """Add up the page headers of a column chunk.

    Parameters
    ----------
    buf
        The file's bytes.
    start
        Where the chunk's first page starts (its dictionary page, if it has one).
    length
        The chunk's compressed size, as its metadata gives it and the reader reads it.

    Returns
    -------
    ChunkPages
        The pages' total and largest uncompressed sizes.

    Raises
    ------
    PageHeaderError
        For a chunk that starts before the file does, or a page header that cannot be read.
    """
    if start < 0:
        raise PageHeaderError(f"a column chunk starts at {start}, before the file does")
    total = largest = 0
    at = start
    while at < start + length:
        uncompressed, compressed, at = page_header(buf, at)
        at += compressed
        total += uncompressed
        largest = max(largest, uncompressed)
    return ChunkPages(total, largest)
