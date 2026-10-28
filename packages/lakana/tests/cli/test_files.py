"""Tests for reading, writing and conforming data files."""

from __future__ import annotations

import datetime as dt
import time
import tracemalloc
import uuid
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.ipc as ipc
import pyarrow.parquet as pq
import pytest

from lakana._models import Column
from lakana.cli import _files
from lakana.cli._state import CliError


@pytest.mark.parametrize(
    ("name", "explicit", "expected"),
    [
        ("a.parquet", None, "parquet"),
        ("a.PQ", None, "parquet"),
        ("a.csv", None, "csv"),
        ("a.arrow", None, "arrow"),
        ("a.feather", None, "arrow"),
        ("a.txt", "csv", "csv"),
    ],
)
def test_infer_format(name: str, explicit: str | None, expected: str) -> None:
    """The extension picks the format; `--format` overrides it."""
    assert _files.infer_format(Path(name), explicit) == expected


def test_infer_format_refuses_an_unknown_extension() -> None:
    """An unrecognized extension without `--format` is a usage error."""
    with pytest.raises(CliError) as caught:
        _files.infer_format(Path("data.xlsx"), None)

    assert caught.value.exit_code == 2
    assert "--format" in str(caught.value)


def test_infer_format_refuses_an_invalid_explicit_format() -> None:
    """An unrecognized `--format` value is a usage error naming the valid ones."""
    with pytest.raises(CliError) as caught:
        _files.infer_format(Path("data.csv"), "xlsx")

    assert caught.value.exit_code == 2
    assert "parquet" in str(caught.value)


@pytest.mark.parametrize("fmt", _files.FORMATS)
def test_write_then_read_round_trips(tmp_path: Path, fmt: str) -> None:
    """Each format written by the CLI reads back equal."""
    table = pa.table({"id": pa.array([1, 2], pa.int64()), "name": ["a", "b"]})
    path = tmp_path / f"t.{fmt}"

    _files.write_table(table, path, fmt)

    assert _files.read_table(path, fmt).equals(table)


def test_read_table_reports_a_missing_file_with_exit_8(tmp_path: Path) -> None:
    """A missing or unreadable file is a client-side file error (exit 8)."""
    with pytest.raises(CliError) as caught:
        _files.read_table(tmp_path / "nope.csv", "csv")

    assert caught.value.exit_code == 8


def test_read_table_reports_a_corrupt_file_with_exit_8(tmp_path: Path) -> None:
    """Bytes that aren't Parquet are a file error, not a traceback."""
    path = tmp_path / "bad.parquet"
    path.write_bytes(b"not parquet")

    with pytest.raises(CliError) as caught:
        _files.read_table(path, "parquet")

    assert caught.value.exit_code == 8


def test_read_table_falls_back_to_the_stream_format_for_arrow(tmp_path: Path) -> None:
    """An Arrow IPC *stream* file (not the file format) is still read as `arrow`."""
    table = pa.table({"id": pa.array([1, 2], pa.int64())})
    path = tmp_path / "t.arrow"
    with ipc.new_stream(str(path), table.schema) as writer:
        writer.write_table(table)

    assert _files.read_table(path, "arrow").equals(table)


def test_write_table_reports_an_unwritable_path_with_exit_8(tmp_path: Path) -> None:
    """A destination whose parent directory doesn't exist is a file error, not a traceback."""
    table = pa.table({"id": pa.array([1], pa.int64())})

    with pytest.raises(CliError) as caught:
        _files.write_table(table, tmp_path / "no_such_dir" / "t.parquet", "parquet")

    assert caught.value.exit_code == 8


_COLUMNS = [
    Column(name="id", type="int", nullable=False),
    Column(name="amount", type="decimal(10,2)", nullable=True),
    Column(name="ts", type="timestamptz", nullable=True),
    Column(name="uid", type="uuid", nullable=True),
]


def test_conform_casts_a_csv_to_the_table_types_in_table_order(tmp_path: Path) -> None:
    """CSV-inferred types are cast, reordered, and given the table's nullability."""
    path = tmp_path / "in.csv"
    u = uuid.uuid4()
    path.write_text(f"uid,ts,amount,id\n{u},2026-01-01T00:00:00,1.50,7\n")

    table = _files.conform(_files.read_table(path, "csv"), _COLUMNS)

    assert table.schema == pa.schema(
        [
            pa.field("id", pa.int32(), nullable=False),
            pa.field("amount", pa.decimal128(10, 2)),
            pa.field("ts", pa.timestamp("us", tz="UTC")),
            pa.field("uid", pa.uuid()),
        ]
    )
    assert table.column("amount").to_pylist() == [Decimal("1.50")]
    assert table.column("ts").to_pylist() == [dt.datetime(2026, 1, 1, tzinfo=dt.UTC)]
    assert table.column("uid").to_pylist() == [u]


def test_conform_names_missing_and_unexpected_columns() -> None:
    """A file whose columns don't match the table fails naming both sides."""
    with pytest.raises(CliError) as caught:
        _files.conform(
            pa.table({"id": [1], "extra": [2]}),
            [Column("id", "long", False), Column("gone", "long", True)],
        )

    assert caught.value.exit_code == 8
    assert "gone" in str(caught.value)
    assert "extra" in str(caught.value)


def test_conform_names_a_column_that_cannot_be_cast() -> None:
    """A value that doesn't fit the type fails naming the column."""
    with pytest.raises(CliError) as caught:
        _files.conform(pa.table({"id": ["seven"]}), [Column("id", "long", False)])

    assert caught.value.exit_code == 8
    assert "'id'" in str(caught.value)


def test_conform_refuses_nulls_in_a_required_column() -> None:
    """A null in a non-nullable column fails client-side, naming the column."""
    with pytest.raises(CliError, match="'id' is required"):
        _files.conform(
            pa.table({"id": pa.array([1, None], pa.int64())}), [Column("id", "long", False)]
        )


def test_conform_refuses_duplicate_column_names() -> None:
    """Two file columns with one name are ambiguous."""
    table = pa.Table.from_arrays([pa.array([1]), pa.array([2])], names=["id", "id"])

    with pytest.raises(CliError, match="duplicate"):
        _files.conform(table, [Column("id", "long", False)])


def test_conform_accepts_a_header_only_csv(tmp_path: Path) -> None:
    """Review focus 2: zero rows conform to the table's types."""
    path = tmp_path / "empty.csv"
    path.write_text("id,amount\n")

    table = _files.conform(
        _files.read_table(path, "csv"),
        [Column("id", "long", False), Column("amount", "decimal(10,2)", True)],
    )

    assert table.num_rows == 0
    assert table.schema.field("id").type == pa.int64()


def test_conform_keeps_a_parquet_file_that_already_matches(tmp_path: Path) -> None:
    """Parquet with the right types passes through unchanged (plan ruling 5)."""
    path = tmp_path / "t.parquet"
    pq.write_table(pa.table({"id": pa.array([1], pa.int64())}), path)

    table = _files.conform(_files.read_table(path, "parquet"), [Column("id", "long", True)])

    assert table.column("id").to_pylist() == [1]


def test_read_table_keeps_csv_null_look_alike_tokens_as_literal_strings(tmp_path: Path) -> None:
    """Fix round 1, finding 1: only an empty field is null, not 'NA'/'null'/'n/a' and friends.

    `pyarrow.csv.ConvertOptions(strings_can_be_null=True)` alone applies pyarrow's ~18
    default null tokens, silently turning values like 'NA' or 'null' into nulls. Only an
    unquoted empty field should become null.
    """
    path = tmp_path / "codes.csv"
    path.write_text("code\nfoo\nNA\nnull\nn/a\n")

    assert _files.read_table(path, "csv").column("code").to_pylist() == [
        "foo",
        "NA",
        "null",
        "n/a",
    ]


def test_conform_turns_an_empty_csv_field_into_null(tmp_path: Path) -> None:
    """An empty field in a nullable string column becomes null, not `''`."""
    path = tmp_path / "codes.csv"
    path.write_text("code,other\nfoo,x\n,y\n")

    table = _files.conform(
        _files.read_table(path, "csv"),
        [Column("code", "string", True), Column("other", "string", True)],
    )

    assert table.column("code").to_pylist() == ["foo", None]


def test_conform_refuses_a_decimal_cast_that_loses_precision(tmp_path: Path) -> None:
    """Fix round 1, finding 2: a value with more precision than the target scale exits 8."""
    path = tmp_path / "amounts.csv"
    path.write_text("amount\n2.999\n")

    with pytest.raises(CliError) as caught:
        _files.conform(_files.read_table(path, "csv"), [Column("amount", "decimal(10,2)", True)])

    assert caught.value.exit_code == 8
    assert "'amount'" in str(caught.value)


def test_conform_accepts_a_decimal_cast_that_fits_exactly(tmp_path: Path) -> None:
    """A value that fits the target scale exactly is not rejected."""
    path = tmp_path / "amounts.csv"
    path.write_text("amount\n1.25\n")

    table = _files.conform(
        _files.read_table(path, "csv"), [Column("amount", "decimal(10,2)", True)]
    )

    assert table.column("amount").to_pylist() == [Decimal("1.25")]


def test_conform_refuses_a_realistic_two_decimal_amount(tmp_path: Path) -> None:
    """A double whose text was exactly 2 decimal places must not be rejected as lossy.

    `float(19.99)`'s nearest double is not the exact decimal 19.99, so a naive
    cast-back-and-compare-as-double check would flag this as a false positive.
    """
    path = tmp_path / "amounts.csv"
    path.write_text("amount\n19.99\n")

    table = _files.conform(
        _files.read_table(path, "csv"), [Column("amount", "decimal(10,2)", True)]
    )

    assert table.column("amount").to_pylist() == [Decimal("19.99")]


def test_conform_refuses_a_float_cast_that_overflows_to_infinity(tmp_path: Path) -> None:
    """A value too large for the target float width exits 8, not a silent `inf`."""
    path = tmp_path / "t.parquet"
    pq.write_table(pa.table({"value": pa.array([1e40], pa.float64())}), path)

    with pytest.raises(CliError) as caught:
        _files.conform(_files.read_table(path, "parquet"), [Column("value", "float", True)])

    assert caught.value.exit_code == 8
    assert "'value'" in str(caught.value)


def test_conform_accepts_a_float_cast_with_ordinary_precision_loss(tmp_path: Path) -> None:
    """`double -> float32` precision loss that doesn't overflow is expected and passes."""
    path = tmp_path / "t.parquet"
    pq.write_table(pa.table({"value": pa.array([0.1], pa.float64())}), path)

    table = _files.conform(_files.read_table(path, "parquet"), [Column("value", "float", True)])

    assert table.column("value").to_pylist() == pytest.approx([0.1])


def test_conform_skips_null_values_in_the_decimal_exactness_check(tmp_path: Path) -> None:
    """A null in a nullable decimal column isn't compared against, just passed through."""
    path = tmp_path / "amounts.csv"
    path.write_text("amount,other\n1.25,x\n,y\n")

    table = _files.conform(
        _files.read_table(path, "csv"),
        [Column("amount", "decimal(10,2)", True), Column("other", "string", True)],
    )

    assert table.column("amount").to_pylist() == [Decimal("1.25"), None]


def test_conform_accepts_a_float_cast_of_an_already_infinite_value(tmp_path: Path) -> None:
    """A source value that was already infinite isn't flagged as a new overflow."""
    path = tmp_path / "t.parquet"
    pq.write_table(pa.table({"value": pa.array([float("inf")], pa.float64())}), path)

    table = _files.conform(_files.read_table(path, "parquet"), [Column("value", "float", True)])

    assert table.column("value").to_pylist() == [float("inf")]


def test_conform_accepts_float32_source_values_that_fit_the_decimal_scale(
    tmp_path: Path,
) -> None:
    """Fix round 2, finding 1: a `float32` source must not be compared against double text.

    `float32(19.99)` is really `19.989999771118164`; a per-value round trip
    through `Decimal(repr(...))` (an earlier approach) would compare that
    against the double-precision text `"19.99"` and wrongly reject it.
    Rounding must happen in the source's own float type.
    """
    path = tmp_path / "t.parquet"
    pq.write_table(pa.table({"amount": pa.array([19.99, 0.10, 2.99], pa.float32())}), path)

    table = _files.conform(
        _files.read_table(path, "parquet"), [Column("amount", "decimal(10,2)", True)]
    )

    assert table.column("amount").to_pylist() == [
        Decimal("19.99"),
        Decimal("0.10"),
        Decimal("2.99"),
    ]


def test_conform_refuses_a_float32_source_value_that_loses_precision(tmp_path: Path) -> None:
    """A `float32` value with real precision beyond the target scale is still rejected."""
    path = tmp_path / "t.parquet"
    pq.write_table(pa.table({"amount": pa.array([2.999], pa.float32())}), path)

    with pytest.raises(CliError) as caught:
        _files.conform(
            _files.read_table(path, "parquet"), [Column("amount", "decimal(10,2)", True)]
        )

    assert caught.value.exit_code == 8
    assert "'amount'" in str(caught.value)


def test_conform_refuses_a_string_decimal_cast_that_loses_precision() -> None:
    """Pin pyarrow's own behavior: a string source already refuses a lossy decimal rescale.

    No extra check is needed for string sources: pyarrow's decimal cast
    parses the string's exact text and raises `ArrowInvalid` itself
    ("Rescaling Decimal value would cause data loss") when it has more
    scale than the target allows.
    """
    table = pa.table({"amount": pa.array(["2.999"], pa.string())})

    with pytest.raises(CliError) as caught:
        _files.conform(table, [Column("amount", "decimal(10,2)", True)])

    assert caught.value.exit_code == 8
    assert "'amount'" in str(caught.value)


def test_conform_refuses_a_decimal_precision_overflow(tmp_path: Path) -> None:
    """A value whose integer digits exceed `precision - scale` fails, naming the column.

    Pins pyarrow's own overflow check on the forward cast (`ArrowInvalid`,
    "... overflow"): no extra bound check is needed on top of it.
    """
    path = tmp_path / "t.parquet"
    pq.write_table(pa.table({"amount": pa.array([123456789.5], pa.float64())}), path)

    with pytest.raises(CliError) as caught:
        _files.conform(_files.read_table(path, "parquet"), [Column("amount", "decimal(5,1)", True)])

    assert caught.value.exit_code == 8
    assert "'amount'" in str(caught.value)


def test_conform_accepts_an_integer_source_cast_to_a_floating_target() -> None:
    """A non-floating source can't already be infinite; the overflow check still runs cleanly."""
    table = pa.table({"value": pa.array([5], pa.int64())})

    result = _files.conform(table, [Column("value", "double", True)])

    assert result.column("value").to_pylist() == [5.0]


def test_conform_casts_a_million_row_decimal_column_quickly(tmp_path: Path) -> None:
    """Fix round 2, finding 2: the decimal exactness check is vectorized, not a Python loop.

    A generous bound (well under the ~0.7s/1M-rows a Python `zip(...to_pylist())`
    loop would take) so CI doesn't flake, while still catching a regression
    back to a row-by-row check.
    """
    path = tmp_path / "t.parquet"
    pq.write_table(pa.table({"amount": pa.array([1.5] * 1_000_000, pa.float64())}), path)
    table = _files.read_table(path, "parquet")

    started = time.perf_counter()
    _files.conform(table, [Column("amount", "decimal(10,2)", True)])
    elapsed = time.perf_counter() - started

    assert elapsed < 2.0


def test_read_table_parses_a_csv_with_the_given_column_types(tmp_path: Path) -> None:
    """Final review C1: with the table's columns, text is parsed straight into their types."""
    path = tmp_path / "in.csv"
    path.write_text("zip,amount,n\n01234,0.1234567890123456789,7\n")
    columns = [
        Column("zip", "string", True),
        Column("amount", "decimal(38,19)", True),
        Column("n", "int", True),
    ]

    table = _files.read_table(path, "csv", columns)

    assert table.schema == pa.schema(
        [("zip", pa.string()), ("amount", pa.decimal128(38, 19)), ("n", pa.int32())]
    )
    assert table.to_pylist() == [
        {"zip": "01234", "amount": Decimal("0.1234567890123456789"), "n": 7}
    ]


def test_read_table_reads_uuid_and_timestamptz_csv_columns_as_text(tmp_path: Path) -> None:
    """The uuid and timestamptz text is left for `conform`, which converts it exactly."""
    path = tmp_path / "in.csv"
    u = uuid.uuid4()
    path.write_text(f"uid,naive,zoned\n{u},2026-01-01T00:00:00,2026-01-01T02:00:00+02:00\n")
    columns = [
        Column("uid", "uuid", True),
        Column("naive", "timestamptz", True),
        Column("zoned", "timestamptz", True),
    ]

    table = _files.conform(_files.read_table(path, "csv", columns), columns)

    assert table.to_pylist() == [
        {
            "uid": u,
            "naive": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            "zoned": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        }
    ]


def test_read_table_leaves_an_unsupported_column_type_to_conform(tmp_path: Path) -> None:
    """A column type with no Arrow mapping is read as inferred; `conform` reports it."""
    path = tmp_path / "in.csv"
    path.write_text("tags\nx\n")
    columns = [Column("tags", "list<string>", True)]

    table = _files.read_table(path, "csv", columns)

    with pytest.raises(CliError, match="Column 'tags' can't be read as list<string>"):
        _files.conform(table, columns)


def test_read_table_names_the_csv_column_a_value_does_not_parse_in(tmp_path: Path) -> None:
    """A parse failure is reported against the column name and its JSON type (exit 8)."""
    path = tmp_path / "in.csv"
    path.write_text("region,id\neu,1\nus,seven\n")

    with pytest.raises(CliError) as caught:
        _files.read_table(
            path, "csv", [Column("id", "long", False), Column("region", "string", True)]
        )

    assert caught.value.exit_code == 8
    assert str(caught.value) == (
        "Column 'id' can't be read as long: CSV conversion error to int64: invalid value 'seven'"
    )


def test_read_table_refuses_a_csv_value_that_overflows_a_float_column(tmp_path: Path) -> None:
    """Residual bug: a value too wide for float32 must fail, not silently parse as `inf`.

    `_csv_parse_type` used to hand pyarrow's `ConvertOptions` the target's own
    float32 type, so pyarrow parsed the CSV text straight into float32 and a
    too-large value became `inf` during the read itself — before `_cast`'s
    overflow guard ever saw it, since `source.type == target` made `_cast`
    skip `_check_no_new_overflow` entirely.
    """
    path = tmp_path / "amounts.csv"
    path.write_text("value\n1e39\n")
    columns = [Column("value", "float", True)]

    with pytest.raises(CliError) as caught:
        _files.conform(_files.read_table(path, "csv", columns), columns)

    assert caught.value.exit_code == 8
    assert "'value'" in str(caught.value)


def test_read_table_parses_an_ordinary_csv_float_as_float32(tmp_path: Path) -> None:
    """A value well within float32 range still ends up typed float32 after conform."""
    path = tmp_path / "amounts.csv"
    path.write_text("value\n0.1\n")
    columns = [Column("value", "float", True)]

    table = _files.conform(_files.read_table(path, "csv", columns), columns)

    assert table.schema.field("value").type == pa.float32()
    assert table.column("value").to_pylist() == pytest.approx([0.1])


def test_read_table_parses_a_csv_float_within_float32_range(tmp_path: Path) -> None:
    """A value near float32's max, but within range, is accepted and typed float32."""
    path = tmp_path / "amounts.csv"
    path.write_text("value\n3.4e38\n")
    columns = [Column("value", "float", True)]

    table = _files.conform(_files.read_table(path, "csv", columns), columns)

    assert table.schema.field("value").type == pa.float32()
    assert table.column("value").to_pylist() == pytest.approx([3.4e38], rel=1e-6)


def test_read_table_still_parses_a_double_csv_column_straight_into_float64(
    tmp_path: Path,
) -> None:
    """A `double` target is unaffected: CSV text is still parsed straight into float64."""
    path = tmp_path / "amounts.csv"
    path.write_text("value\n1e39\n")
    columns = [Column("value", "double", True)]

    table = _files.read_table(path, "csv", columns)

    assert table.schema.field("value").type == pa.float64()
    assert table.column("value").to_pylist() == [1e39]


def test_read_table_quotes_pyarrow_when_no_column_can_be_named(tmp_path: Path) -> None:
    """An error that names no column (a ragged row) quotes pyarrow's message (exit 8)."""
    path = tmp_path / "in.csv"
    path.write_text("id,region\n1,eu,extra\n")

    with pytest.raises(CliError) as caught:
        _files.read_table(
            path, "csv", [Column("id", "long", False), Column("region", "string", True)]
        )

    assert caught.value.exit_code == 8
    assert str(caught.value).startswith(f"Could not read {path} as csv: ")
    assert "Expected 2 columns, got 3" in str(caught.value)


def test_read_table_quotes_pyarrow_when_the_header_cannot_be_reread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the header can't be read back to name column #N, pyarrow's message is quoted."""
    path = tmp_path / "gone.csv"
    path.write_text("id\nx\n")

    def fail(*args: object, **kwargs: object) -> pa.Table:
        path.unlink()
        raise pa.ArrowInvalid("In CSV column #0: CSV conversion error to int64: invalid value 'x'")

    monkeypatch.setattr(_files.pacsv, "read_csv", fail)

    with pytest.raises(CliError) as caught:
        _files.read_table(path, "csv", [Column("id", "long", False)])

    assert caught.value.exit_code == 8
    assert "In CSV column #0" in str(caught.value)


@pytest.mark.parametrize(
    "source",
    [pa.array([0, 1, 2], pa.int64()), pa.array([1, -1], pa.int8()), pa.array([0.0, 0.5])],
    ids=["int64-2", "int8-minus-1", "float-half"],
)
def test_conform_refuses_a_number_other_than_0_or_1_for_a_boolean(source: pa.Array) -> None:
    """Final review minor 8: 2 must not silently become True; exit 8 naming the column."""
    with pytest.raises(CliError) as caught:
        _files.conform(pa.table({"flag": source}), [Column("flag", "boolean", True)])

    assert caught.value.exit_code == 8
    assert "Column 'flag' can't be read as boolean" in str(caught.value)
    assert "only 0 and 1" in str(caught.value)


def test_conform_accepts_0_1_and_null_for_a_boolean() -> None:
    """0, 1 and null are the only numbers a boolean column takes."""
    table = pa.table({"flag": pa.array([0, 1, None], pa.int32())})

    result = _files.conform(table, [Column("flag", "boolean", True)])

    assert result.column("flag").to_pylist() == [False, True, None]


def test_strict_utf8_checks_in_pieces_without_decoding_the_whole_file(tmp_path: Path) -> None:
    """Review M2: the check holds no decoded copy of the file, whatever its size."""
    data = b"\xef\xbb\xbf" + b"a,\xc3\xbc\n" * 3_000_000
    tracemalloc.start()
    try:
        _files.strict_utf8(tmp_path / "big.csv", data)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert peak < len(data) // 2


@pytest.mark.parametrize(
    ("data", "position", "byte"),
    [
        (b"a" * (1024 * 1024 - 1) + b"\xc3\xbc" + b"b\xff", 1024 * 1024 + 2, 0xFF),
        (b"a" * (1024 * 1024 - 1) + b"\xc3", 1024 * 1024 - 1, 0xC3),
        (b"\xef\xbb\xbfab\xfc", 2, 0xFC),
    ],
    ids=["across-pieces", "cut-at-the-end", "after-a-bom"],
)
def test_strict_utf8_names_the_first_bad_byte(
    tmp_path: Path, data: bytes, position: int, byte: int
) -> None:
    """A character split between two pieces is fine; the position counts from the text's start."""
    with pytest.raises(CliError) as caught:
        _files.strict_utf8(tmp_path / "in.csv", data)

    assert caught.value.exit_code == 8
    assert str(caught.value) == (
        f"in.csv is not UTF-8 (byte {byte:#04x} at position {position}); convert it to UTF-8 "
        "and run the command again"
    )
