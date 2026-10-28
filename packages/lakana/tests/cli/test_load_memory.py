"""Tests for how `lakana load` bounds the memory a Parquet or Arrow file takes once read."""

from __future__ import annotations

import struct
from collections.abc import Iterator
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from conftest import FakeApi, invoke, make_state

import lakana.cli.load as load_module
from lakana.cli import _capped


@pytest.fixture(autouse=True)
def _read_here(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read files in the test's own process, where the reading can be observed.

    The capped child process is tested in `test_load_capped.py`.

    Parameters
    ----------
    monkeypatch
        Pytest's monkeypatch.
    """
    monkeypatch.setattr(_capped, "enforced", lambda: False)


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


def test_an_extension_column_with_nested_storage_is_refused_before_any_row_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review I3: a tensor column holds many values per row; it is refused before it is read."""
    path = tmp_path / "tensor.parquet"
    kind = pa.fixed_shape_tensor(pa.float32(), [1024])
    storage = pa.FixedSizeListArray.from_arrays(pa.array([0.0] * 1024 * 64, pa.float32()), 1024)
    pq.write_table(pa.table({"t": pa.ExtensionArray.from_storage(kind, storage)}), path)

    def never(*args: object, **kwargs: object) -> Iterator[pa.RecordBatch]:
        raise AssertionError("the rows were read")

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", never)

    result = invoke(make_state(tmp_path, _api(1024 * 1024)), "load", str(path), "t", "--env", "dev")

    assert result.exit_code == 8, result.output
    assert "tensor.parquet: Column 't' has an unsupported Arrow type" in result.output


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


def _varint_in(value: int, width: int) -> bytes:
    """Encode a variable-length integer in exactly `width` bytes, padded with zero groups.

    Parameters
    ----------
    value
        The value.
    width
        The bytes it must take (at least its shortest encoding).

    Returns
    -------
    bytes
        The encoding.
    """
    raw = bytearray(_varint(value))
    if len(raw) < width:
        raw[-1] |= 0x80
        raw += b"\x80" * (width - len(raw) - 1) + b"\x00"
    return bytes(raw)


def _old_writer_file(path: Path, size: int) -> None:
    """Write the review's old-writer file: a second page hidden past the chunk's declared end.

    The footer claims parquet-mr 1.2.8 and a chunk that ends after its first page (1,024 short
    values); the second page, 1,024 copies of one `size`-byte value in a few bytes, lies in the
    100 bytes the reader goes on to read for such writers.

    Parameters
    ----------
    path
        Where to write it.
    size
        The repeated value's length.
    """
    options = {
        "compression": "brotli",
        "use_dictionary": False,
        "column_encoding": {"s": "DELTA_BYTE_ARRAY"},
        "data_page_size": 1,
        "write_batch_size": 1024,
    }
    pq.write_table(pa.table({"s": ["a"] * 1024}), path, **options)
    length = pq.read_metadata(path).row_group(0).column(0).total_compressed_size
    pq.write_table(pa.table({"s": ["a"] * 1024 + ["x" * size] * 1024}), path, **options)
    metadata = pq.read_metadata(path)
    column = metadata.row_group(0).column(0)
    data = bytearray(path.read_bytes())
    footer = len(data) - 8 - int.from_bytes(data[-8:-4], "little")
    sizes = [column.total_uncompressed_size, column.total_compressed_size]
    old = b"".join(b"\x16" + _varint(size << 1) for size in sizes)
    new = b"".join(b"\x16" + _varint_in(length << 1, len(_varint(size << 1))) for size in sizes)
    at = data.index(old, footer)
    data[at : at + len(old)] = new
    writer = metadata.created_by.encode()
    claimed = b"parquet-mr version 1.2.8 ("
    at = data.index(writer, footer)
    data[at : at + len(writer)] = claimed + b"a" * (len(writer) - len(claimed) - 1) + b")"
    path.write_bytes(bytes(data))


def test_an_old_parquet_mr_file_ends_in_the_size_refusal(tmp_path: Path) -> None:
    """Review I4: rows the metadata hides past a chunk's end still count against the limit."""
    path = tmp_path / "old.parquet"
    _old_writer_file(path, 65536)
    assert pq.read_table(path).num_rows == 2048

    result = invoke(make_state(tmp_path, _api(1024 * 1024)), "load", str(path), "t", "--env", "dev")

    assert result.exit_code == 2, result.output
    assert "is larger than one upload may be (1 MiB)" in result.output


def _shared_views(rows: int, size: int) -> pa.Array:
    """Build a text view column whose rows all point at one value.

    Parameters
    ----------
    rows
        How many rows.
    size
        The value's length.

    Returns
    -------
    pa.Array
        The column: `rows` views of 16 bytes over one buffer of `size` bytes.
    """
    view = struct.pack("<i4sii", size, b"xxxx", 0, 0)
    return pa.Array.from_buffers(
        pa.string_view(), rows, [None, pa.py_buffer(view * rows), pa.py_buffer(b"x" * size)]
    )


def test_views_over_one_value_count_every_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review M1: 1,024 views of one 64 KiB value are 64 MiB of text, refused before conversion."""
    path = tmp_path / "views.arrow"
    table = pa.table({"v": _shared_views(1024, 65536)})
    with pa.ipc.new_file(str(path), table.schema) as writer:
        writer.write_table(table)
    assert path.stat().st_size < 128 * 1024

    def never(table: pa.Table) -> pa.Table:
        raise AssertionError("the views were converted")

    monkeypatch.setattr(load_module, "normalize_view_columns", never)

    result = invoke(make_state(tmp_path, _api(1024 * 1024)), "load", str(path), "t", "--env", "dev")

    assert result.exit_code == 2, result.output
    assert "is larger than one upload may be (1 MiB)" in result.output


def test_a_view_column_counts_its_values_not_its_buffers() -> None:
    """Null rows count nothing, and a slice counts only its own rows."""
    column = pa.array(["ab", None, "x" * 20, "yyy"], pa.string_view())

    assert load_module._column_size(column) == 2 + 20 + 3 + 4 * 5 + 1
    assert load_module._column_size(column.slice(2)) == 20 + 3 + 4 * 3 + 1


def test_many_view_batches_count_their_values_as_they_are_added(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR review S2: the running total adds the size checked, not the shared buffers' size."""
    path = tmp_path / "manyviews.arrow"
    batch = pa.record_batch({"v": _shared_views(128, 4096)})
    with pa.ipc.new_file(str(path), batch.schema) as writer:
        for _ in range(80):
            writer.write_batch(batch)

    def never(table: pa.Table) -> pa.Table:
        raise AssertionError("the views were converted")

    monkeypatch.setattr(load_module, "normalize_view_columns", never)

    result = invoke(make_state(tmp_path, _api(1024 * 1024)), "load", str(path), "t", "--env", "dev")

    assert result.exit_code == 2, result.output
    assert "is larger than one upload may be (1 MiB)" in result.output
