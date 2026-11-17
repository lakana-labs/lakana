"""Tests for the CSV and spreadsheet files of a query result: exact values, or a refusal."""

import io
import math
import struct
import uuid
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal

import fastexcel
import pyarrow as pa
import pyarrow.compute
import pytest

from lakana_server import result_files
from lakana_server.result_files import (
    ResultFileRefused,
    XlsxRefused,
    csv_pieces,
    file_cells,
    refuse_unwritable,
    write_xlsx,
)

QID = "3f2a6c1e-0000-4000-8000-000000000001"
"""The query id the refusal sentences name."""


def csv_text(table: pa.Table) -> bytes:
    """Write a table as CSV, joining the pieces.

    Parameters
    ----------
    table : pyarrow.Table
        The result.

    Returns
    -------
    bytes
        The whole file.
    """
    return b"".join(csv_pieces(table))


def xlsx_bytes(table: pa.Table, environment: str = "dev") -> bytes:
    """Write a table as a spreadsheet into memory.

    Parameters
    ----------
    table : pyarrow.Table
        The result.
    environment
        The environment the refusal's command names.

    Returns
    -------
    bytes
        The workbook.
    """
    target = io.BytesIO()
    write_xlsx(table, target, query_id=QID, environment=environment)
    return target.getvalue()


def read_back(table: pa.Table) -> pa.Table:
    """Write a table as a spreadsheet and read its sheet back as Arrow.

    Parameters
    ----------
    table : pyarrow.Table
        The result.

    Returns
    -------
    pyarrow.Table
        The sheet, as `fastexcel` reads it.
    """
    return fastexcel.read_excel(xlsx_bytes(table)).load_sheet(0).to_arrow()


def sheet_xml(content: bytes) -> str:
    """Return the first sheet's XML of a workbook.

    Parameters
    ----------
    content
        The workbook.

    Returns
    -------
    str
        ``xl/worksheets/sheet1.xml``.
    """
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return archive.read("xl/worksheets/sheet1.xml").decode()


def instead(environment: str = "dev") -> str:
    """Return the end of every refusal sentence: the CSV command.

    Parameters
    ----------
    environment
        As the shell sees it.

    Returns
    -------
    str
        ``Download it as CSV instead: …``.
    """
    return (
        f"Download it as CSV instead: lakana query result {QID} --env {environment} "
        f"--format csv --out query-{QID}.csv"
    )


# --- CSV -----------------------------------------------------------------------------------------


def test_csv_quotes_like_rfc_4180_and_keeps_utf_8_and_formulas_verbatim() -> None:
    table = pa.table({"name": ["a,b", "x\r\ny", "é"], "note": ['say "hi"', "=SUM(A1)", "日本"]})
    text = csv_text(table)
    # pyarrow's quoting style "needed" (R6) quotes every string and the header names; plain
    # values need no quotes, and the rest is as RFC 4180 says.
    assert text == (
        b'"name","note"\r\n"a,b","say ""hi"""\r\n"x\r\ny","=SUM(A1)"\r\n'
        b'"\xc3\xa9","\xe6\x97\xa5\xe6\x9c\xac"\r\n'
    )
    assert not text.startswith(b"\xef\xbb\xbf")


def test_csv_writes_null_as_an_empty_field_nested_as_json_and_binary_as_hex() -> None:
    table = pa.table(
        {
            "i": pa.array([1, None], pa.int64()),
            "l": pa.array([[1, 2], None], pa.list_(pa.int64())),
            "b": pa.array([b"\x01\xff", b""], pa.binary()),
        }
    )
    assert csv_text(table) == b'"i","l","b"\r\n1,"[1,2]","\\x01\\xff"\r\n,,""\r\n'


def test_csv_pieces_come_one_per_batch_of_10000_rows() -> None:
    pieces = list(csv_pieces(pa.table({"i": pa.array(range(25_000), pa.int32())})))
    assert len(pieces) == 4
    assert pieces[0] == b'"i"\r\n'
    assert [piece.count(b"\r\n") for piece in pieces[1:]] == [10_000, 10_000, 5_000]


def test_file_cells_writes_nested_values_as_compact_json_and_a_uuid_as_text() -> None:
    one = uuid.UUID(int=1)
    table = pa.table(
        {
            "s": pa.array(
                [{"a": "日", "b": b"\x00"}], pa.struct([("a", pa.string()), ("b", pa.binary())])
            ),
            "m": pa.array([[("k", 1.5)]], pa.map_(pa.string(), pa.float64())),
            "f": pa.array([[math.nan]], pa.list_(pa.float64())),
            "u": pa.array([one.bytes], pa.uuid()),
            "d": pa.array(["x"]).dictionary_encode(),
        }
    )
    assert file_cells(table).to_pylist() == [
        {
            "s": '{"a":"日","b":"\\\\x00"}',
            "m": '[{"key":"k","value":1.5}]',
            "f": '["NaN"]',
            "u": str(one),
            "d": "x",
        }
    ]


# --- xlsx ----------------------------------------------------------------------------------------


def test_xlsx_round_trips_types() -> None:
    table = pa.table(
        {
            "i": pa.array([7], pa.int64()),
            "f": pa.array([2.5], pa.float64()),
            "b": pa.array([True]),
            "d": pa.array([date(2026, 11, 16)], pa.date32()),
            "ts": pa.array([datetime(2026, 11, 16, 9, 30, 5)], pa.timestamp("us")),
            "tz": pa.array(
                [datetime(2026, 11, 16, 9, 30, 5, tzinfo=UTC)], pa.timestamp("us", "UTC")
            ),
            "s": pa.array(["123"]),
        }
    )
    back = read_back(table)
    assert back.to_pylist() == [
        {
            "i": 7.0,
            "f": 2.5,
            "b": True,
            "d": datetime(2026, 11, 16),
            "ts": datetime(2026, 11, 16, 9, 30, 5),
            "tz": datetime(2026, 11, 16, 9, 30, 5),
            "s": "123",
        }
    ]
    xml = sheet_xml(xlsx_bytes(table))
    styles = zipfile.ZipFile(io.BytesIO(xlsx_bytes(table))).read("xl/styles.xml").decode()
    assert 'formatCode="yyyy-mm-dd hh:mm:ss &quot;UTC&quot;"' in styles
    assert 'formatCode="yyyy-mm-dd"' in styles
    assert "<f>" not in xml


def test_xlsx_writes_a_big_integer_and_a_long_decimal_as_text() -> None:
    long_decimal = Decimal("1234567890123456789012345678.1234567890")
    table = pa.table(
        {
            "big": pa.array([2**62, 2**53 - 1], pa.int64()),
            "dec": pa.array([long_decimal, Decimal("1.5000000000")], pa.decimal128(38, 10)),
            "nan": pa.array([math.nan, math.inf], pa.float64()),
        }
    )
    xml = sheet_xml(xlsx_bytes(table))
    assert str(2**62) in xml
    assert str(long_decimal) in xml
    back = read_back(table).to_pylist()
    assert back[0] == {"big": str(2**62), "dec": str(long_decimal), "nan": "NaN"}
    assert (back[1]["big"], back[1]["dec"], back[1]["nan"]) == (
        str(2**53 - 1),
        "1.5",
        "Infinity",
    )


def test_xlsx_writes_a_timestamp_of_any_zone_in_utc() -> None:
    berlin = pa.array([datetime(2026, 1, 1, 9, tzinfo=UTC)], pa.timestamp("us", "Europe/Berlin"))
    assert read_back(pa.table({"t": berlin})).to_pylist() == [{"t": datetime(2026, 1, 1, 9)}]


def test_xlsx_writes_a_float_its_16_digits_would_round_as_text() -> None:
    """XlsxWriter writes a number with 16 significant digits; a double may need 17."""
    table = pa.table({"f": pa.array([0.1 + 0.2, 1 / 3], pa.float64())})
    assert read_back(table).to_pylist()[0] == {"f": "0.30000000000000004"}
    assert "<v>0.3333333333333333</v>" in sheet_xml(xlsx_bytes(table)), "16 digits, a number"


def test_xlsx_writes_a_float32_as_the_number_its_shortest_text_names() -> None:
    """A float32 widened to a double needs 17 digits; its own shortest text needs at most 9."""
    table = pa.table({"f": pa.array([0.1, 2.5, 1.3], pa.float32())})
    assert read_back(table).to_pylist() == [{"f": 0.1}, {"f": 2.5}, {"f": 1.3}]
    assert csv_text(table) == b'"f"\r\n0.1\r\n2.5\r\n1.3\r\n'


def test_xlsx_writes_the_largest_float32_as_its_number() -> None:
    """A short text for the largest float32 rounds past it; the next precision names it."""
    largest = struct.unpack("f", struct.pack("I", 0x7F7FFFFF))[0]
    table = pa.table({"f": pa.array([largest, -largest], pa.float32())})
    read = [row["f"] for row in read_back(table).to_pylist()]
    assert [struct.unpack("f", struct.pack("f", value))[0] for value in read] == [largest, -largest]
    assert read == [3.4028235e38, -3.4028235e38], "the number its shortest text names"


INFINITY_US = 2**63 - 1
"""DuckDB's ``'infinity'::timestamp`` in any unit: the largest int64."""

INFINITY_DAY = 2**31 - 1
"""DuckDB's ``'infinity'::date``: the largest int32."""


def out_of_range_table() -> pa.Table:
    """Build a result whose temporal columns hold values Python has no date or time for.

    Returns
    -------
    pyarrow.Table
        Timestamps without and with a time zone, and dates: infinity, minus infinity, a long
        year, a year before 1, and one value Python holds.
    """
    long_year = 9_223_372_036_854_775_806
    moment = 1_700_000_000_123_456
    return pa.table(
        {
            "ts": pa.array([INFINITY_US, -INFINITY_US, long_year, moment], pa.int64()).cast(
                pa.timestamp("us")
            ),
            "tz": pa.array([INFINITY_US, -INFINITY_US, long_year, moment], pa.int64()).cast(
                pa.timestamp("us", "Europe/Berlin")
            ),
            "d": pa.array([INFINITY_DAY, -INFINITY_DAY, 2_932_897, -721_292], pa.int32()).cast(
                pa.date32()
            ),
        }
    )


OUT_OF_RANGE_TEXT = [
    {"ts": "infinity", "tz": "infinity", "d": "infinity"},
    {"ts": "-infinity", "tz": "-infinity", "d": "-infinity"},
    {
        "ts": "294247-01-10 04:00:54.775806",
        "tz": "294247-01-10 04:00:54.775806+00",
        "d": "10000-01-01",
    },
    {
        "ts": "2023-11-14 22:13:20.123456",
        "tz": "2023-11-14 22:13:20.123456+00",
        "d": "0006-03-04 (BC)",
    },
]
"""`out_of_range_table` as DuckDB prints it, a time zone's values in UTC."""


def test_a_temporal_column_outside_pythons_range_is_duckdbs_text_in_csv() -> None:
    lines = [",".join(f'"{value}"' for value in row.values()) for row in OUT_OF_RANGE_TEXT]
    expected = "\r\n".join(['"ts","tz","d"', *lines]) + "\r\n"
    assert csv_text(out_of_range_table()) == expected.encode()


def test_a_temporal_column_outside_pythons_range_is_duckdbs_text_in_xlsx() -> None:
    assert read_back(out_of_range_table()).to_pylist() == OUT_OF_RANGE_TEXT


def test_a_temporal_column_inside_pythons_range_keeps_its_type() -> None:
    table = pa.table(
        {
            "ts": pa.array([datetime(9999, 12, 31, 23, 59, 59, 999_999)], pa.timestamp("us")),
            "d": pa.array([date(1, 1, 1)], pa.date32()),
        }
    )
    assert file_cells(table).schema == table.schema


def test_xlsx_writes_a_string_starting_with_equals_as_text_not_a_formula() -> None:
    xml = sheet_xml(xlsx_bytes(pa.table({"s": ["=SUM(A1)", "=1+1"]})))
    assert "<f>" not in xml
    assert "=SUM(A1)" in xml


def test_xlsx_refuses_more_rows_than_a_sheet_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(result_files, "XLSX_MAX_ROWS", 3)
    with pytest.raises(XlsxRefused) as refused:
        xlsx_bytes(pa.table({"i": list(range(4))}))
    assert str(refused.value) == (
        "This result has 4 rows; a spreadsheet holds at most 3 below its header row. " + instead()
    )
    assert xlsx_bytes(pa.table({"i": list(range(3))})).startswith(b"PK\x03\x04")


def test_xlsx_writes_every_row_in_order_across_the_10000_row_batches() -> None:
    rows = 2 * result_files.CSV_BATCH_ROWS + 5_000
    table = pa.table(
        {"i": pa.array(range(rows), pa.int64()), "s": [f"row {n}" for n in range(rows)]}
    )
    back = read_back(table)
    assert back.num_rows == rows
    assert back.column("i").to_pylist() == list(range(rows))
    assert back.column("s").to_pylist() == [f"row {n}" for n in range(rows)]


def test_xlsx_refuses_one_row_more_than_a_sheet_holds_without_writing_it() -> None:
    """The real limit, unpatched: the refusal is decided before a row is written."""
    rows = result_files.XLSX_MAX_ROWS + 1
    target = io.BytesIO()
    with pytest.raises(XlsxRefused) as refused:
        write_xlsx(
            pa.table({"i": pa.nulls(rows, pa.int8())}), target, query_id=QID, environment="dev"
        )
    assert str(refused.value) == (
        "This result has 1,048,576 rows; a spreadsheet holds at most 1,048,575 below its "
        "header row. " + instead()
    )
    assert target.getvalue() == b""


def test_xlsx_refuses_one_column_more_than_a_sheet_holds_and_writes_the_widest_it_holds() -> None:
    """The real limit, unpatched: 16,385 columns are refused, 16,384 are written."""
    one = pa.array([1], pa.int8())
    wide = pa.Table.from_arrays(
        [one] * (result_files.XLSX_MAX_COLUMNS + 1),
        names=[f"c{n}" for n in range(result_files.XLSX_MAX_COLUMNS + 1)],
    )
    target = io.BytesIO()
    with pytest.raises(XlsxRefused) as refused:
        write_xlsx(wide, target, query_id=QID, environment="dev")
    assert str(refused.value) == (
        "This result has 16,385 columns; a spreadsheet holds at most 16,384. " + instead()
    )
    assert target.getvalue() == b""
    widest = read_back(wide.drop_columns(["c16384"]))
    assert widest.num_columns == result_files.XLSX_MAX_COLUMNS
    assert widest.column_names[-1] == "c16383"
    assert widest.to_pylist() == [dict.fromkeys(widest.column_names, 1)]


def test_xlsx_refuses_a_value_longer_than_a_cell() -> None:
    long_text = "x" * 40_000
    with pytest.raises(XlsxRefused) as refused:
        xlsx_bytes(pa.table({"ok": ["y"], "no\nte": [long_text]}))
    assert str(refused.value) == (
        "A value in column 'no\\\\nte' is 40,000 characters long; a spreadsheet cell holds at "
        "most 32,767. " + instead()
    )
    assert csv_text(pa.table({"t": [long_text]})).count(b"x") == 40_000
    at_the_limit = read_back(pa.table({"t": ["x" * 32_767]})).to_pylist()
    assert at_the_limit == [{"t": "x" * 32_767}]


def test_xlsx_refuses_a_nested_value_whose_text_is_longer_than_a_cell() -> None:
    with pytest.raises(XlsxRefused, match="A value in column 'l' is 39,999 characters long"):
        xlsx_bytes(pa.table({"l": pa.array([["x" * 39_995]], pa.list_(pa.string()))}))


def test_xlsx_refusal_quotes_the_environment_in_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(result_files, "XLSX_MAX_ROWS", 0)
    with pytest.raises(XlsxRefused) as refused:
        xlsx_bytes(pa.table({"i": [1]}), environment="my env")
    assert str(refused.value).endswith(instead("'my env'"))


BIGNUM = pa.opaque(pa.binary(), "bignum", "DuckDB")
"""The type DuckDB exports `SELECT 5::varint` as: an extension pyarrow does not know."""


def extension_table() -> pa.Table:
    """Build a result whose columns are extension types other than UUID.

    Returns
    -------
    pyarrow.Table
        An opaque binary column, a JSON column and a list of the opaque type.
    """
    return pa.table(
        {
            "big": pa.array([b"\x05\xff"], BIGNUM),
            "doc": pa.array(['{"a":1}'], pa.json_()),
            "bigs": pa.ListArray.from_arrays(
                [0, 1], pa.ExtensionArray.from_storage(BIGNUM, pa.array([b"\x01"]))
            ),
            "runs": pa.compute.call_function("run_end_encode", [pa.array([7])]),
        }
    )


def test_an_extension_column_is_written_as_its_storage_in_csv_and_xlsx() -> None:
    table = extension_table()
    assert csv_text(table) == (
        b'"big","doc","bigs","runs"\r\n"\\x05\\xff","{""a"":1}","[""\\\\x01""]",7\r\n'
    )
    assert read_back(table).to_pylist() == [
        {"big": "\\x05\\xff", "doc": '{"a":1}', "bigs": '["\\\\x01"]', "runs": 7.0}
    ]
    assert "b&apos;" not in sheet_xml(xlsx_bytes(table))


def test_csv_pieces_fails_before_it_returns_for_a_column_it_cannot_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(result_files, "_cell_column", lambda column: column)
    with pytest.raises(pa.ArrowInvalid):
        csv_pieces(pa.table({"big": pa.array([b"\x05"], BIGNUM)}))


def test_a_column_no_file_holds_is_refused_naming_the_arrow_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(result_files, "_cell_column", lambda column: column)
    table = pa.table({"ok": [1], "big": pa.array([b"\x05"], BIGNUM)})
    with pytest.raises(ResultFileRefused) as refused:
        refuse_unwritable(table, "csv", query_id=QID, environment="my env")
    assert str(refused.value) == (
        "Column 'big' has the type extension<arrow.opaque[storage_type=binary, "
        "type_name=bignum, vendor_name=DuckDB]>, which a csv file cannot hold. Download it as "
        f"Arrow instead: lakana query result {QID} --env 'my env' --format arrow "
        f"--out query-{QID}.arrow"
    )
    monkeypatch.undo()
    refuse_unwritable(extension_table(), "xlsx", query_id=QID, environment="dev")


def test_result_file_name() -> None:
    assert result_files.result_file_name(QID, "csv") == f"query-{QID}.csv"
    assert result_files.result_file_name(QID, "xlsx") == f"query-{QID}.xlsx"
    assert result_files.result_file_name(QID, "arrow") == f"query-{QID}.arrow"
