"""Read the page headers of a Parquet column chunk, never its pages.

A page's header says how large the page is once decompressed, and the reader decompresses a
compressed page to exactly that size; an uncompressed page it reads as stored, whatever the
header says, so a page counts the larger of the two sizes. The headers so bound what decoding a
column chunk can take: no value is larger than the page it is decoded from, a dictionary page
included. That holds whatever the
encoding: a dictionary or shared prefixes (`DELTA_BYTE_ARRAY`) can make 1,024 rows of a large
value out of a few bytes, but never one value larger than its page.

Headers are Thrift structs in the compact protocol: `PageHeader` field 1 is the page type,
field 2 its uncompressed size and field 3 its compressed size, all `i32`; the rest is skipped.
A list or map may announce no more entries than the bytes left could hold and nesting stops at
16 levels, so reading a header takes work in proportion to its size.
"""

from __future__ import annotations

import mmap
from dataclasses import dataclass
from typing import NamedTuple

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
_DATA_PAGES = frozenset({5, 8})
"""`PageHeader`'s `data_page_header` and `data_page_header_v2`: field 1 of each is `num_values`."""
_PADDING = 100
"""How far past a chunk's declared end the reader goes on for values it still needs."""


class Page(NamedTuple):
    """One page header.

    Parameters
    ----------
    uncompressed
        The page's uncompressed size.
    compressed
        Its stored size.
    values
        The values a data page holds; 0 for any other page.
    end
        Where the page itself starts, after its header.
    """

    uncompressed: int
    compressed: int
    values: int
    end: int


class PageHeaderError(ValueError):
    """A page header that cannot be read: truncated, malformed or nested too deeply."""


@dataclass(frozen=True)
class ChunkPages:
    """What the page headers of one column chunk say.

    Parameters
    ----------
    total
        The pages' sizes added up, each the larger of its uncompressed and its stored size.
    largest
        The largest page's size, counted the same way: no value decodes larger.
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


def _within(buf: Buffer, at: int) -> int:
    """Check that a position is still inside the bytes.

    Parameters
    ----------
    buf
        The bytes.
    at
        The position after a value.

    Returns
    -------
    int
        `at`.

    Raises
    ------
    PageHeaderError
        When the value ran past the end.
    """
    if at > len(buf):
        raise PageHeaderError("a page header ends early")
    return at


def _count(buf: Buffer, at: int, size: int, entry: int) -> int:
    """Refuse a list or map with more entries than the bytes left could hold.

    Every entry takes at least `entry` bytes, so a larger count is malformed; refusing it keeps
    the work of reading a header within its size.

    Parameters
    ----------
    buf
        The bytes.
    at
        Where the first entry starts.
    size
        The entries the header announces.
    entry
        The fewest bytes one entry takes.

    Returns
    -------
    int
        `size`.

    Raises
    ------
    PageHeaderError
        For more entries than the bytes left.
    """
    left = len(buf) - at
    if size * entry > left:
        raise PageHeaderError(
            f"a page header holds a list or map of {size} entries, more entries than the "
            f"{left} bytes left"
        )
    return size


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
        For an unknown type, nesting deeper than 16 levels, or a value past the end.
    """
    if depth > _MAX_DEPTH:
        raise PageHeaderError(f"a page header nests deeper than {_MAX_DEPTH} levels")
    if kind in _FIXED:
        return _within(buf, at + _FIXED[kind])
    if kind in _VARINT:
        return _varint(buf, at)[1]
    if kind == _BINARY:
        size, at = _varint(buf, at)
        return _within(buf, at + size)
    if kind in _LIST:
        head = buf[at]
        size, at = head >> 4, at + 1
        if size == 15:
            size, at = _varint(buf, at)
        for _ in range(_count(buf, at, size, 1)):
            at = _element(buf, at, head & 0x0F, depth + 1)
        return at
    if kind == _MAP:
        size, at = _varint(buf, at)
        if not size:
            return at
        head, at = buf[at], at + 1
        for _ in range(_count(buf, at, size, 2)):
            at = _element(buf, at, head >> 4, depth + 1)
            at = _element(buf, at, head & 0x0F, depth + 1)
        return at
    if kind == _STRUCT:
        return _fields(buf, at, depth + 1)[1]
    raise PageHeaderError(f"a page header holds an unknown field type {kind}")


def _element(buf: Buffer, at: int, kind: int, depth: int) -> int:
    """Skip one element of a list, a set or a map, where a boolean takes one byte.

    Parameters
    ----------
    buf
        The bytes.
    at
        Where the element starts.
    kind
        Its compact type.
    depth
        How deeply it is nested.

    Returns
    -------
    int
        The position after it.
    """
    if kind in (1, 2):
        return _within(buf, at + 1)
    return _skip(buf, at, kind, depth)


def _fields(
    buf: Buffer, at: int, depth: int, inner: frozenset[int] = frozenset()
) -> tuple[dict[int, int], int]:
    """Read a struct, keeping its `i32` fields and skipping the rest.

    Parameters
    ----------
    buf
        The bytes.
    at
        Where the struct starts.
    depth
        How deeply it is nested.
    inner
        Ids of struct fields whose own field 1 (an `i32`) is kept under their id.

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
        elif kind == _STRUCT and field in inner:
            sub, at = _fields(buf, at, depth + 1)
            found[field] = sub.get(1, 0)
        else:
            at = _skip(buf, at, kind, depth)


def page_header(buf: Buffer, at: int) -> Page:
    """Read the page header that starts at `at`.

    Parameters
    ----------
    buf
        The file's bytes.
    at
        Where the header starts.

    Returns
    -------
    Page
        Its sizes, its values, and where the page itself starts.

    Raises
    ------
    PageHeaderError
        For a header that is truncated, malformed, or lacks a size.
    """
    try:
        found, end = _fields(buf, at, 0, _DATA_PAGES)
    except IndexError:
        raise PageHeaderError("a page header ends early") from None
    kind, uncompressed, compressed = found.get(1, -1), found.get(2, -1), found.get(3, -1)
    if min(kind, uncompressed, compressed) < 0:
        raise PageHeaderError("a page header lacks its type or sizes")
    values = max(found.get(5, 0), found.get(8, 0), 0)
    return Page(uncompressed, compressed, values, end)


def chunk_pages(buf: Buffer, start: int, length: int, values: int) -> ChunkPages:
    """Add up the page headers of a column chunk, as far as the reader reads them.

    The reader reads the chunk's declared length and, for files that old writers (parquet-mr
    before 1.2.9) wrote with a length too short, up to 100 bytes more while values are
    missing. Those pages are walked for every file: a page the reader would not reach only
    makes the bound larger.

    Parameters
    ----------
    buf
        The file's bytes.
    start
        Where the chunk's first page starts (its dictionary page, if it has one).
    length
        The chunk's compressed size, as its metadata gives it.
    values
        The chunk's values, as its metadata gives them.

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
    total = largest = seen = 0
    end = start + length
    padded = min(end + _PADDING, len(buf))
    at = start
    while at < end or (at < padded and seen < values):
        page = page_header(buf, at)
        at = page.end + page.compressed
        seen += page.values
        size = max(page.uncompressed, page.compressed)
        total += size
        largest = max(largest, size)
    return ChunkPages(total, largest)
