"""Tests for `lakana load`."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.ipc
import pyarrow.parquet as pq
import pytest
from conftest import FakeApi, invoke, make_state

_ENV = "/api/v1/environments/dev"
_BRONZE = f"{_ENV}/namespaces/bronze/tables/customers"
_SILVER_TEXT = (
    "`lakana load` loads into bronze only: load into `bronze.customers` and transform it into "
    "`silver.customers` with an asset."
)


def _environment(protected: bool = False) -> dict[str, object]:
    """Describe `dev` as the environments route does.

    Parameters
    ----------
    protected
        Whether `dev` is protected.

    Returns
    -------
    dict
        The JSON body.
    """
    return {
        "name": "dev",
        "storage_root": "file:///data",
        "storage_secret_ref": None,
        "is_protected": protected,
        "properties": {},
    }


def _api(
    *,
    exists: int | None = None,
    protected: bool = False,
    limit: int | None = 256 * 1024 * 1024,
    refuse: str | None = None,
) -> FakeApi:
    """Build a fake API for one load into `bronze.customers`.

    Parameters
    ----------
    exists
        The table's current snapshot, or `None` when it does not exist.
    protected
        Whether `dev` is protected.
    limit
        The upload limit the info route reports, or `None` for an older server.
    refuse
        A 422 text the load route answers with, or `None`.

    Returns
    -------
    FakeApi
        The fake; its `requests` record every call.
    """
    api = FakeApi()
    api.on("GET", _ENV, json=_environment(protected))
    info: dict[str, object] = {"version": "0.21.0", "artifact_limits": {}}
    if limit is not None:
        info["upload_max_bytes"] = limit
    api.on("GET", "/api/v1/info", json=info)
    if exists is None:
        api.on("GET", _BRONZE, status=404, json={"detail": "not found"})
    else:
        api.on(
            "GET",
            _BRONZE,
            json={
                "name": "customers",
                "namespace": "bronze",
                "uuid": "u1",
                "metadata_location": "file:///m.json",
                "snapshot_id": exists,
                "schema": [],
            },
        )

    def load(request: httpx.Request) -> httpx.Response:
        if refuse is not None:
            return httpx.Response(422, json={"detail": refuse})
        dry = request.url.params["dry_run"] == "true"
        with pyarrow.ipc.open_stream(pa.BufferReader(request.content)) as reader:
            rows = reader.read_all().num_rows
        return httpx.Response(
            200,
            json={
                "snapshot_id": None if dry else 9,
                "rows": 0 if dry else rows,
                "created": exists is None,
                "dry_run": dry,
            },
        )

    api.routes["POST", f"{_BRONZE}/load"] = load
    api.routes["POST", f"{_ENV}/namespaces/silver/tables/customers/load"] = load
    return api


def _loads(api: FakeApi) -> list[httpx.Request]:
    """Pick the load requests.

    Parameters
    ----------
    api
        The fake.

    Returns
    -------
    list of httpx.Request
        Every request to a load route, in order.
    """
    return [r for r in api.requests if r.url.path.endswith("/load")]


def _rows_sent(request: httpx.Request) -> pa.Table:
    """Decode the Arrow stream a request sent.

    Parameters
    ----------
    request
        A load request.

    Returns
    -------
    pa.Table
        Its rows.
    """
    with pyarrow.ipc.open_stream(pa.BufferReader(request.content)) as reader:
        return reader.read_all()


def _csv(tmp_path: Path) -> Path:
    """Write a small CSV file.

    Parameters
    ----------
    tmp_path
        Where.

    Returns
    -------
    Path
        The file.
    """
    path = tmp_path / "customers.csv"
    path.write_text("id,name\n1,a\n2,b\n3,c\n")
    return path


def test_a_bare_name_loads_into_bronze_after_a_dry_run(tmp_path: Path) -> None:
    """Bare name → bronze; the dry run carries an empty stream of the schema and `none`."""
    api = _api()

    result = invoke(
        make_state(tmp_path, api), "load", str(_csv(tmp_path)), "customers", "--env", "dev", "--yes"
    )

    assert result.exit_code == 0, result.output
    dry, real = _loads(api)
    assert dry.url.params["dry_run"] == "true"
    assert dry.url.params["expected_snapshot_id"] == "none"
    assert _rows_sent(dry).num_rows == 0
    assert _rows_sent(dry).schema == _rows_sent(real).schema
    assert real.url.params["dry_run"] == "false"
    assert real.url.params["expected_snapshot_id"] == "none"
    assert real.url.params["write"] == "replace"
    assert _rows_sent(real).num_rows == 3
    assert "3 rows, 2 columns" in result.stderr
    assert "id: int64" in result.stderr
    assert "Loaded 3 rows into dev/bronze.customers (created the table)" in result.stdout


def test_append_pins_the_tables_current_snapshot(tmp_path: Path) -> None:
    """A later load with `--write append` names the snapshot it read."""
    api = _api(exists=41)

    result = invoke(
        make_state(tmp_path, api),
        "load",
        str(_csv(tmp_path)),
        "bronze.customers",
        "--env",
        "dev",
        "--write",
        "append",
        "--yes",
    )

    assert result.exit_code == 0, result.output
    for request in _loads(api):
        assert request.url.params["expected_snapshot_id"] == "41"
        assert request.url.params["write"] == "append"


def test_without_yes_it_asks_and_a_no_sends_nothing_after_the_dry_run(tmp_path: Path) -> None:
    """Declining stops before the rows are sent."""
    api = _api()

    result = invoke(
        make_state(tmp_path, api, interactive=True),
        "load",
        str(_csv(tmp_path)),
        "customers",
        "--env",
        "dev",
        input="n\n",
    )

    assert result.exit_code == 2, result.output
    assert "Aborted." in result.output
    assert [r.url.params["dry_run"] for r in _loads(api)] == ["true"]


_PROTECTED = (
    "`dev` is protected: a load there needs --yes --break-glass --reason REASON "
    "(MANAGE_ENVIRONMENT, audited); recurring loads belong in an asset"
)


@pytest.mark.parametrize("options", [(), ("--yes",)])
def test_a_protected_environment_needs_yes_and_break_glass(
    tmp_path: Path, options: tuple[str, ...]
) -> None:
    """Without both, nothing is read or sent: the server would refuse the write anyway."""
    api = _api(protected=True)

    result = invoke(
        make_state(tmp_path, api, interactive=True),
        "load",
        str(_csv(tmp_path)),
        "customers",
        "--env",
        "dev",
        *options,
    )

    assert result.exit_code == 2, result.output
    assert _PROTECTED in " ".join(result.output.split())
    assert _loads(api) == []


def test_a_protected_environment_loads_with_yes_and_break_glass(tmp_path: Path) -> None:
    """Both requests carry the reason; the server checks the right and audits the write."""
    api = _api(protected=True)

    result = invoke(
        make_state(tmp_path, api),
        "load",
        str(_csv(tmp_path)),
        "customers",
        "--env",
        "dev",
        "--yes",
        "--break-glass",
        "--reason",
        "INC-7: load the corrected export once",
    )

    assert result.exit_code == 0, result.output
    loads = _loads(api)
    assert [r.url.params["dry_run"] for r in loads] == ["true", "false"]
    assert {r.url.params["break_glass_reason"] for r in loads} == {
        "INC-7: load the corrected export once"
    }


def test_a_target_outside_bronze_shows_the_servers_text_and_uploads_no_rows(
    tmp_path: Path,
) -> None:
    """The CLI holds no copy of the rule: the dry run brings the refusal, unchanged."""
    api = _api(refuse=_SILVER_TEXT)

    result = invoke(
        make_state(tmp_path, api),
        "load",
        str(_csv(tmp_path)),
        "silver.customers",
        "--env",
        "dev",
        "--yes",
    )

    assert result.exit_code != 0
    assert _SILVER_TEXT in " ".join(result.output.split())
    (only,) = _loads(api)
    assert only.url.params["dry_run"] == "true"
    assert _rows_sent(only).num_rows == 0


def test_a_file_over_the_upload_limit_is_refused_before_it_is_read(tmp_path: Path) -> None:
    """The limit comes from the server; the message names it and the way instead."""
    api = _api(limit=10)

    result = invoke(
        make_state(tmp_path, api), "load", str(_csv(tmp_path)), "customers", "--env", "dev", "--yes"
    )

    assert result.exit_code == 2, result.output
    text = " ".join(result.output.split())
    assert "is larger than one upload may be (10 bytes)" in text
    assert "load it with a Python asset and a directory or object-store connection" in text
    assert _loads(api) == []


def test_parquet_that_unpacks_past_the_limit_is_refused_before_upload(tmp_path: Path) -> None:
    """A small compressed file is measured as the stream it would become."""
    path = tmp_path / "zeros.parquet"
    pq.write_table(pa.table({"v": pa.array([0] * 100_000, pa.int64())}), path)
    assert path.stat().st_size < 200_000
    api = _api(limit=200_000)

    result = invoke(make_state(tmp_path, api), "load", str(path), "zeros", "--env", "dev", "--yes")

    assert result.exit_code == 2, result.output
    assert "is larger than one upload may be" in result.output
    assert _loads(api) == []


def test_an_older_server_without_a_limit_leaves_the_check_to_it(tmp_path: Path) -> None:
    """No `upload_max_bytes`: the load goes ahead; the server's 413 would apply."""
    api = _api(limit=None)

    result = invoke(
        make_state(tmp_path, api), "load", str(_csv(tmp_path)), "customers", "--env", "dev", "--yes"
    )

    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("fmt", ["parquet", "arrow", "ndjson", "json"])
def test_each_format_is_read(tmp_path: Path, fmt: str) -> None:
    """Parquet, Arrow, NDJSON and a JSON array give the same rows."""
    rows = pa.table({"id": pa.array([1, 2], pa.int64()), "name": pa.array(["a", "b"])})
    path = tmp_path / f"customers.{fmt}"
    if fmt == "parquet":
        pq.write_table(rows, path)
    elif fmt == "arrow":
        with pyarrow.ipc.new_file(str(path), rows.schema) as writer:
            writer.write_table(rows)
    elif fmt == "ndjson":
        path.write_text('{"id": 1, "name": "a"}\n{"id": 2, "name": "b"}\n')
    else:
        path.write_text(json.dumps(rows.to_pylist()))
    api = _api()

    result = invoke(
        make_state(tmp_path, api), "load", str(path), "customers", "--env", "dev", "--yes"
    )

    assert result.exit_code == 0, result.output
    sent = _rows_sent(_loads(api)[-1])
    assert sent.column("id").to_pylist() == [1, 2]
    assert sent.column("name").to_pylist() == ["a", "b"]


def test_a_spreadsheet_with_sheet_and_header_row(tmp_path: Path) -> None:
    """`--sheet` and `--header-row` pick the sheet and the row of the names."""
    pytest.importorskip("fastexcel")
    xlsxwriter = pytest.importorskip("xlsxwriter")
    path = tmp_path / "book.xlsx"
    workbook = xlsxwriter.Workbook(str(path))
    workbook.add_worksheet("first").write_row(0, 0, ["ignored"])
    sheet = workbook.add_worksheet("customers")
    sheet.write_row(0, 0, ["exported by the shop"])
    sheet.write_row(1, 0, ["id", "name"])
    sheet.write_row(2, 0, [1, "a"])
    workbook.close()
    api = _api()

    result = invoke(
        make_state(tmp_path, api),
        "load",
        str(path),
        "customers",
        "--env",
        "dev",
        "--sheet",
        "customers",
        "--header-row",
        "2",
        "--yes",
    )

    assert result.exit_code == 0, result.output
    assert _rows_sent(_loads(api)[-1]).column_names == ["id", "name"]


def test_a_csv_without_a_header_row_gets_numbered_columns(tmp_path: Path) -> None:
    """`--header-row 0`: column_1, column_2, as files.read names them."""
    path = tmp_path / "plain.csv"
    path.write_text("1,a\n2,b\n")
    api = _api()

    result = invoke(
        make_state(tmp_path, api),
        "load",
        str(path),
        "customers",
        "--env",
        "dev",
        "--header-row",
        "0",
        "--yes",
    )

    assert result.exit_code == 0, result.output
    assert _rows_sent(_loads(api)[-1]).column_names == ["column_1", "column_2"]


def test_a_spreadsheet_without_the_assets_extra_names_the_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without polars or fastexcel: the pip command, before anything is sent."""
    import builtins

    real = builtins.__import__

    def without_fastexcel(name: str, *args: object, **kwargs: object) -> object:
        if name == "fastexcel":
            raise ImportError(name)
        return real(name, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(builtins, "__import__", without_fastexcel)
    path = tmp_path / "book.xlsx"
    path.write_bytes(b"PK\x03\x04")
    api = _api()

    result = invoke(
        make_state(tmp_path, api), "load", str(path), "customers", "--env", "dev", "--yes"
    )

    assert result.exit_code == 2, result.output
    assert "reading .xlsx needs the assets extra: pip install 'lakana[assets]'" in result.output
    assert _loads(api) == []


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (["--sheet", "x"], "--sheet applies to spreadsheets"),
        (["--write", "merge"], "--write must be replace or append"),
        (["--format", "xml"], "--format must be one of"),
    ],
)
def test_options_the_file_does_not_take_are_refused(
    tmp_path: Path, options: list[str], message: str
) -> None:
    """A wrong option is a usage error naming it, before anything is sent."""
    api = _api()

    result = invoke(
        make_state(tmp_path, api),
        "load",
        str(_csv(tmp_path)),
        "customers",
        "--env",
        "dev",
        "--yes",
        *options,
    )

    assert result.exit_code == 2, result.output
    assert message in " ".join(result.output.split())
    assert _loads(api) == []


def test_ndjson_nested_too_deep_is_refused(tmp_path: Path) -> None:
    """The helpers' depth limit applies before pyarrow parses (it would crash)."""
    path = tmp_path / "deep.ndjson"
    path.write_text('{"a": ' + "[" * 200 + "]" * 200 + "}\n")
    api = _api()

    result = invoke(make_state(tmp_path, api), "load", str(path), "deep", "--env", "dev", "--yes")

    assert result.exit_code == 8, result.output
    assert "deeper than 100 levels" in result.output
    assert _loads(api) == []


def test_a_column_type_a_table_cannot_store_is_named(tmp_path: Path) -> None:
    """A null-typed column (an all-empty JSON field) is refused naming the column."""
    path = tmp_path / "nulls.ndjson"
    path.write_text('{"id": 1, "nothing": null}\n')
    api = _api()

    result = invoke(make_state(tmp_path, api), "load", str(path), "nulls", "--env", "dev", "--yes")

    assert result.exit_code == 8, result.output
    assert (
        "nulls.ndjson: column `nothing` has no values in any row, so its type cannot be "
        "inferred; fill it or drop it, then load it again"
    ) in " ".join(result.output.split())
    assert _loads(api) == []


def test_another_type_a_table_cannot_store_says_to_convert_it(tmp_path: Path) -> None:
    """A duration column is named, with the type, once; the inner period is not doubled."""
    path = tmp_path / "waits.parquet"
    pq.write_table(pa.table({"wait": pa.array([1, 2], pa.duration("s"))}), path)
    api = _api()

    result = invoke(make_state(tmp_path, api), "load", str(path), "t", "--env", "dev", "--yes")

    assert result.exit_code == 8, result.output
    assert (
        "waits.parquet: Column 'wait' has an unsupported Arrow type: duration[s]; convert the "
        "column before loading"
    ) in " ".join(result.output.split())
    assert _loads(api) == []


def test_an_all_empty_csv_column_is_named_with_what_to_do(tmp_path: Path) -> None:
    """An empty column has no type to infer; the user can fill it or drop it."""
    path = tmp_path / "nullcol.csv"
    path.write_text("a,b\n1,\n2,\n")
    api = _api()

    result = invoke(make_state(tmp_path, api), "load", str(path), "t", "--env", "dev", "--yes")

    assert result.exit_code == 8, result.output
    assert (
        "nullcol.csv: column `b` has no values in any row, so its type cannot be inferred; fill "
        "it or drop it, then load it again"
    ) in " ".join(result.output.split())
    assert _loads(api) == []


def test_a_repeated_column_name_is_refused_before_anything_is_sent(tmp_path: Path) -> None:
    """A table cannot have two columns of one name; the file is named, not "the result"."""
    path = tmp_path / "dup.csv"
    path.write_text("a,a\n1,2\n")
    api = _api()

    result = invoke(make_state(tmp_path, api), "load", str(path), "t", "--env", "dev", "--yes")

    assert result.exit_code == 8, result.output
    assert "dup.csv has the column `a` more than once; rename one and load it again" in (
        " ".join(result.output.split())
    )
    assert _loads(api) == []


def test_parquet_strings_that_decode_past_the_limit_stop_after_one_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated strings shrink to almost nothing on disk; reading stops at the limit."""
    path = tmp_path / "strings.parquet"
    pq.write_table(pa.table({"s": pa.array(["x" * 4096] * 50 * 1024)}), path, compression="zstd")
    assert path.stat().st_size < 200_000
    yielded: list[int] = []
    iter_batches = pq.ParquetFile.iter_batches

    def counted(self: pq.ParquetFile, *args: object, **kwargs: object) -> Iterator[pa.RecordBatch]:
        for batch in iter_batches(self, *args, **kwargs):
            yielded.append(batch.num_rows)
            yield batch

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", counted)
    api = _api(limit=1024 * 1024)

    result = invoke(make_state(tmp_path, api), "load", str(path), "s", "--env", "dev", "--yes")

    assert result.exit_code == 2, result.output
    assert "is larger than one upload may be (1 MiB)" in result.output
    assert len(yielded) == 1
    assert _loads(api) == []


@pytest.mark.parametrize("kind", ["file", "stream", "dictionary"])
def test_a_compressed_arrow_file_is_refused_before_it_is_decoded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Compressed IPC is refused outright, as the server refuses it: nothing is decompressed."""
    path = tmp_path / "zeros.arrow"
    values = (
        pa.array(["a"] * 1_000_000).dictionary_encode()
        if kind == "dictionary"
        else pa.array([0] * 1_000_000, pa.int64())
    )
    table = pa.table({"v": values})
    options = pyarrow.ipc.IpcWriteOptions(compression="zstd")
    new = pyarrow.ipc.new_stream if kind == "stream" else pyarrow.ipc.new_file
    with new(str(path), table.schema, options=options) as writer:
        writer.write_table(table)

    def never(*args: object, **kwargs: object) -> None:
        raise AssertionError("the batches were decoded")

    monkeypatch.setattr(pyarrow.ipc, "open_file", never)
    monkeypatch.setattr(pyarrow.ipc, "open_stream", never)
    api = _api()

    result = invoke(make_state(tmp_path, api), "load", str(path), "z", "--env", "dev", "--yes")

    assert result.exit_code == 8, result.output
    assert (
        "zeros.arrow is compressed, which Lakana does not read: write it uncompressed (or as "
        "Parquet) and load it again"
    ) in " ".join(result.output.split())
    assert _loads(api) == []


def test_an_uncompressed_arrow_file_with_a_dictionary_is_read(tmp_path: Path) -> None:
    """A dictionary batch without compression passes the scan."""
    path = tmp_path / "dict.arrow"
    table = pa.table({"v": pa.array(["a", "b", "a"]).dictionary_encode()})
    with pyarrow.ipc.new_file(str(path), table.schema) as writer:
        writer.write_table(table)
    api = _api()

    result = invoke(
        make_state(tmp_path, api), "load", str(path), "customers", "--env", "dev", "--yes"
    )

    assert result.exit_code == 0, result.output
    assert _rows_sent(_loads(api)[-1]).num_rows == 3


def test_a_parquet_file_that_expands_past_the_limit_is_refused_before_it_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file's metadata says how large its rows are; the rows are never decoded."""
    path = tmp_path / "zeros.parquet"
    pq.write_table(pa.table({"v": pa.array([0] * 1_000_000, pa.int64())}), path)
    assert path.stat().st_size < 100_000

    def never(*args: object, **kwargs: object) -> pa.Table:
        raise AssertionError("the rows were read")

    monkeypatch.setattr(pq, "read_table", never)
    api = _api(limit=1024 * 1024)

    result = invoke(make_state(tmp_path, api), "load", str(path), "zeros", "--env", "dev", "--yes")

    assert result.exit_code == 2, result.output
    assert "zeros.parquet (8000000 bytes) is larger than one upload may be (1 MiB)" in (
        " ".join(result.output.split())
    )
    assert _loads(api) == []


def test_a_parquet_file_within_the_limit_is_read(tmp_path: Path) -> None:
    """Strings count by their uncompressed size; a small file passes the estimate."""
    path = tmp_path / "names.parquet"
    pq.write_table(pa.table({"id": [1, 2], "name": ["a", None], "ok": [True, False]}), path)
    api = _api()

    result = invoke(
        make_state(tmp_path, api), "load", str(path), "customers", "--env", "dev", "--yes"
    )

    assert result.exit_code == 0, result.output
    assert _rows_sent(_loads(api)[-1]).num_rows == 2


def _fail(tmp_path: Path, name: str, content: bytes, *options: str) -> tuple[int, str, FakeApi]:
    """Load a file that should be refused before anything is sent.

    Parameters
    ----------
    tmp_path
        Where to write it.
    name
        The file's name.
    content
        Its bytes.
    *options
        More options.

    Returns
    -------
    tuple
        The exit code, the output with whitespace collapsed, and the fake.
    """
    path = tmp_path / name
    path.write_bytes(content)
    api = _api()
    result = invoke(
        make_state(tmp_path, api), "load", str(path), "t", "--env", "dev", "--yes", *options
    )
    return result.exit_code, " ".join(result.output.split()), api


@pytest.mark.parametrize(
    ("name", "content", "options", "code", "message"),
    [
        ("data.txt", b"x", (), 2, "Can't tell the format of data.txt; pass --format"),
        ("data.ndjson", b'{"a": "\xff"}\n', (), 8, "is not UTF-8 (byte 0xff at position 7)"),
        (
            "data.csv",
            b"name\nM\xfcnchen\n",
            (),
            8,
            "data.csv is not UTF-8 (byte 0xfc at position 6); save it as UTF-8 and load it again",
        ),
        (
            "hdr.csv",
            b"a,b\n",
            (),
            8,
            "hdr.csv has no rows, so there is nothing to load and no column types to infer; "
            "add rows and load it again",
        ),
        (
            "empty.json",
            b"[]",
            (),
            8,
            "empty.json has no columns; a load needs at least one column and one row: add them "
            "and load it again",
        ),
        ("data.parquet", b"not parquet", (), 8, "Could not read data.parquet as parquet"),
        ("data.json", b"[{]", (), 8, "data.json"),
        ("data.parquet", b"x", ("--header-row", "2"), 2, "--header-row applies to CSV files"),
        ("data.csv", b"a\n1\n", ("--header-row", "-1"), 2, "--header-row is the 1-based row"),
    ],
)
def test_a_file_that_cannot_be_read_names_the_cause(
    tmp_path: Path, name: str, content: bytes, options: tuple[str, ...], code: int, message: str
) -> None:
    """Each refusal says what is wrong with the file or the option, and nothing is sent."""
    exit_code, output, api = _fail(tmp_path, name, content, *options)

    assert exit_code == code, output
    assert message in output
    assert _loads(api) == []


def test_a_workbook_that_unpacks_far_past_its_size_is_refused(tmp_path: Path) -> None:
    """A zip bomb dressed as a workbook is refused before it is parsed."""
    pytest.importorskip("fastexcel")
    import zipfile

    path = tmp_path / "bomb.xlsx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", b"\0" * (20 * 1024 * 1024))
    api = _api()

    result = invoke(make_state(tmp_path, api), "load", str(path), "t", "--env", "dev", "--yes")

    assert result.exit_code == 8, result.output
    assert "unpacks to more than 16 times its size" in result.output
    assert _loads(api) == []


def test_an_explicit_format_and_an_arrow_stream_file(tmp_path: Path) -> None:
    """`--format arrow` reads an IPC stream (not only the file format) whatever the name."""
    rows = pa.table({"id": pa.array([1], pa.int64())})
    path = tmp_path / "rows.bin"
    with pyarrow.ipc.new_stream(str(path), rows.schema) as writer:
        writer.write_table(rows)
    api = _api()

    result = invoke(
        make_state(tmp_path, api),
        "load",
        str(path),
        "customers",
        "--env",
        "dev",
        "--format",
        "arrow",
        "--yes",
    )

    assert result.exit_code == 0, result.output
    assert _rows_sent(_loads(api)[-1]).equals(rows)


def test_a_missing_file_is_named(tmp_path: Path) -> None:
    """Exit 8, the path, nothing sent."""
    api = _api()

    result = invoke(
        make_state(tmp_path, api), "load", str(tmp_path / "gone.csv"), "t", "--env", "dev"
    )

    assert result.exit_code == 8, result.output
    assert "No such file" in result.output
    assert _loads(api) == []
