"""Tests for reading Parquet page headers (`lakana.cli._parquet_pages`) and their use by `load`."""

from __future__ import annotations

import struct
from collections.abc import Iterator
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from conftest import FakeApi, invoke, make_state

import lakana.cli.load as load_module
from lakana.cli._parquet_pages import PageHeaderError, chunk_pages, page_header


def _varint(value: int) -> bytes:
    """Encode an unsigned variable-length integer.

    Parameters
    ----------
    value
        The value.

    Returns
    -------
    bytes
        Its encoding.
    """
    out = bytearray()
    while True:
        byte, value = value & 0x7F, value >> 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _i32(value: int) -> bytes:
    """Encode an `i32` field one id after the previous one.

    Parameters
    ----------
    value
        The value.

    Returns
    -------
    bytes
        The field header and the zigzag value.
    """
    return bytes([0x15]) + _varint((value << 1) ^ (value >> 63))


_SIZES = _i32(0) + _i32(100) + _i32(10)
"""Fields 1 to 3 of a page header: a data page of 100 bytes, 10 compressed."""


@pytest.mark.parametrize(
    "extra",
    [
        bytes([0x11]),
        bytes([0x12]),
        bytes([0x13, 0x7F]),
        bytes([0x14]) + _varint(5),
        bytes([0x16]) + _varint(2**40),
        bytes([0x17]) + struct.pack("<d", 1.5),
        bytes([0x18]) + _varint(3) + b"abc",
        bytes([0x19, 0x25]) + _varint(2) + _varint(4),
        bytes([0x1A, 0xF3]) + _varint(16) + bytes(16),
        bytes([0x19, 0x31]) + b"\x01\x02\x01",
        bytes([0x19, 0x27]) + bytes(16),
        bytes([0x19, 0x1C]) + b"\x00",
        bytes([0x1B]) + _varint(2) + bytes([0x85]) + (_varint(1) + b"k" + _varint(2)) * 2,
        bytes([0x1B]) + _varint(0),
        bytes([0x1C]) + _i32(7) + b"\x00",
        bytes([0x1D]) + bytes(16),
        bytes([0x05]) + _varint(80) + _varint(14),
    ],
    ids=[
        "true",
        "false",
        "i8",
        "i16",
        "i64",
        "double",
        "binary",
        "list",
        "long-set",
        "bools",
        "doubles",
        "structs",
        "map",
        "empty-map",
        "struct",
        "uuid",
        "long-field-id",
    ],
)
def test_a_header_reads_its_sizes_past_any_other_field(extra: bytes) -> None:
    """Every compact type is skipped; the sizes and the header's end are found."""
    header = _SIZES + extra + b"\x00"

    assert page_header(header + b"page", 0) == (100, 10, len(header))


@pytest.mark.parametrize(
    ("header", "message"),
    [
        (_SIZES + bytes([0x1E]) + b"\x00", "unknown field type 14"),
        (_SIZES + bytes([0x1C]) * 20 + bytes(20), "nests deeper than 16 levels"),
        (bytes([0x15]) + b"\x80" * 11, "longer than 10 bytes"),
        (_SIZES[:-1], "ends early"),
        (_SIZES + bytes([0x18]) + _varint(1000) + b"\x00", "ends early"),
        (_i32(0) + b"\x00", "lacks its type or sizes"),
        (_i32(0) + _i32(-1) + _i32(10) + b"\x00", "lacks its type or sizes"),
    ],
    ids=[
        "unknown-type",
        "too-deep",
        "long-varint",
        "truncated",
        "past-end",
        "no-sizes",
        "negative",
    ],
)
def test_a_header_that_cannot_be_read_is_refused(header: bytes, message: str) -> None:
    """A malformed header raises one error type, whatever is wrong with it."""
    with pytest.raises(PageHeaderError, match=message):
        page_header(header, 0)


def test_a_chunk_adds_its_pages_up() -> None:
    """Each page is skipped by its compressed size; the largest is kept."""
    first = _i32(2) + _i32(300) + _i32(4) + b"\x00"
    second = _i32(0) + _i32(50) + _i32(2) + b"\x00"
    buf = first + b"dict" + second + b"ab"

    assert chunk_pages(buf, 0, len(buf)) == load_module._parquet_pages.ChunkPages(350, 300)
    with pytest.raises(PageHeaderError, match="starts at -1"):
        chunk_pages(buf, -1, len(buf))


def test_the_largest_page_bounds_a_repeated_value(tmp_path: Path) -> None:
    """A file pyarrow wrote, with statistics, checksums and v2 pages, reads as its metadata says."""
    path = tmp_path / "v2.parquet"
    pq.write_table(
        pa.table({"s": ["y" * 70_000] * 10, "n": list(range(10))}),
        path,
        data_page_version="2.0",
        write_page_checksum=True,
    )
    metadata = pq.read_metadata(path)

    (chunks,) = load_module._page_headers(path, metadata)

    assert chunks[0].largest >= 70_000
    for index, chunk in enumerate(chunks):
        column = metadata.row_group(0).column(index)
        assert 0 < chunk.total <= column.total_uncompressed_size


def _api(limit: int | None) -> FakeApi:
    """Build a fake API for a load the file check refuses.

    Parameters
    ----------
    limit
        The upload limit the info route reports, or `None`.

    Returns
    -------
    FakeApi
        The fake.
    """
    api = FakeApi()
    api.on(
        "GET",
        "/api/v1/environments/dev",
        json={
            "name": "dev",
            "storage_root": "file:///data",
            "storage_secret_ref": None,
            "is_protected": False,
            "properties": {},
        },
    )
    info: dict[str, object] = {"version": "0.21.0", "artifact_limits": {}}
    if limit is not None:
        info["upload_max_bytes"] = limit
    api.on("GET", "/api/v1/info", json=info)
    return api


def test_a_broken_page_header_names_the_file(tmp_path: Path) -> None:
    """A page header that cannot be read is refused as an unreadable file."""
    path = tmp_path / "broken`.parquet"
    pq.write_table(pa.table({"n": list(range(100))}), path, use_dictionary=False)
    start = pq.read_metadata(path).row_group(0).column(0).data_page_offset
    data = bytearray(path.read_bytes())
    data[start] = 0xFF
    path.write_bytes(bytes(data))

    result = invoke(make_state(tmp_path, _api(None)), "load", str(path), "t", "--env", "dev")

    assert result.exit_code == 8, result.output
    assert "Could not read broken`.parquet as parquet: a page header holds an unknown field" in (
        " ".join(result.output.split())
    )


def test_a_nested_column_is_refused_before_any_row_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A table stores no nested column, and a row of one has no bound: it is refused first."""
    path = tmp_path / "nested.parquet"
    pq.write_table(pa.table({"n": [1], "l": [[1, 2]]}), path)

    def never(*args: object, **kwargs: object) -> Iterator[pa.RecordBatch]:
        raise AssertionError("the rows were read")

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", never)

    result = invoke(make_state(tmp_path, _api(None)), "load", str(path), "t", "--env", "dev")

    assert result.exit_code == 8, result.output
    assert "nested.parquet: Column 'l' has an unsupported Arrow type" in result.output


@pytest.mark.parametrize(
    ("values", "size"),
    [
        (pa.array(["abc", "de"], pa.large_string()), 3 + 3 + 2 + 8 * 4 + 1),
        (pa.array([7, 8], pa.int64()), 3 * 8 + 1),
        (pa.array([[1, 2]]), None),
    ],
    ids=["large-string", "fixed-width", "nested"],
)
def test_a_dictionary_column_counts_the_values_it_decodes_to(
    values: pa.Array, size: int | None
) -> None:
    """Text adds up its values' lengths and offsets; a fixed width counts per row; else at most."""
    column = pa.DictionaryArray.from_arrays(pa.array([0, 0, 1 % len(values)], pa.int8()), values)

    expected = size if size is not None else 3 * values.nbytes + 1
    assert load_module._column_size(column) == expected


def test_without_a_limit_a_row_group_is_read_in_full_batches(tmp_path: Path) -> None:
    """An older server says no limit: batches are as large as ever."""
    budget = load_module._Budget(tmp_path / "f.parquet", None)

    assert budget.rows(10**9) == load_module._BATCH_ROWS
