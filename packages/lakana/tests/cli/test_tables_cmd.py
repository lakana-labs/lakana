"""Tests for `lakana table *`."""

from __future__ import annotations

import io
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.ipc
import pytest
from conftest import FakeApi, invoke, make_state
from typer.testing import Result

from lakana.cli._state import CliError
from lakana.cli.tables import parse_column_spec


def _plain(text: str) -> str:
    """Return CLI output without ANSI codes or Rich box borders, whitespace collapsed.

    CI forces colour and wraps help and error boxes, which can split an option
    name across styled fragments (the same normalization the `env` help test uses).

    Parameters
    ----------
    text
        Raw stdout or stderr.

    Returns
    -------
    str
        The plain, single-spaced text.
    """
    plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
    for border in "│╭╮╰╯─":
        plain = plain.replace(border, " ")
    return " ".join(plain.split())


_COLUMNS: list[dict[str, object]] = [
    {"name": "id", "type": "long", "nullable": False},
    {"name": "region", "type": "string", "nullable": True},
]
_TABLE = {
    "name": "sales",
    "namespace": "bronze",
    "uuid": "u1",
    "metadata_location": "file:///m.json",
    "schema": _COLUMNS,
}
_BASE = "/api/v1/environments/dev/namespaces/bronze/tables"


def test_parse_column_spec() -> None:
    """`name:type` is required; a trailing `?` makes it nullable; the type may contain commas."""
    assert parse_column_spec("id:long") == {"name": "id", "type": "long", "nullable": False}
    assert parse_column_spec("amount:decimal(10,2)?") == {
        "name": "amount",
        "type": "decimal(10,2)",
        "nullable": True,
    }
    for bad in ("id", ":long", "id:", "id:?"):
        with pytest.raises(CliError):
            parse_column_spec(bad)


def test_table_create_from_columns(tmp_path: Path) -> None:
    """`--column` specs become the JSON schema; the confirmation names the environment."""
    api = FakeApi()
    api.on("POST", _BASE, status=201, json=_TABLE)

    result = invoke(
        make_state(tmp_path, api),
        "table",
        "create",
        "bronze.sales",
        "--env",
        "dev",
        "--column",
        "id:long",
        "--column",
        "region:string?",
    )

    assert result.exit_code == 0, result.stderr
    assert "Created table dev/bronze.sales" in result.stdout
    assert json.loads(api.requests[0].content) == {
        "name": "sales",
        "schema": [
            {"name": "id", "type": "long", "nullable": False},
            {"name": "region", "type": "string", "nullable": True},
        ],
    }


def test_table_create_from_a_schema_file(tmp_path: Path) -> None:
    """`--schema FILE` sends that JSON list as-is."""
    api = FakeApi()
    api.on("POST", _BASE, status=201, json=_TABLE)
    schema_file = tmp_path / "schema.json"
    schema_file.write_text(json.dumps(_TABLE["schema"]))

    result = invoke(
        make_state(tmp_path, api),
        "table",
        "create",
        "bronze.sales",
        "--env",
        "dev",
        "--schema",
        str(schema_file),
    )

    assert result.exit_code == 0, result.stderr
    assert json.loads(api.requests[0].content)["schema"] == _TABLE["schema"]


def test_table_create_needs_exactly_one_schema_source(tmp_path: Path) -> None:
    """Neither or both of `--schema`/`--column` is a usage error."""
    state = make_state(tmp_path, FakeApi())
    schema_file = tmp_path / "s.json"
    schema_file.write_text("[]")

    neither = invoke(state, "table", "create", "bronze.sales", "--env", "dev")
    both = invoke(
        state,
        "table",
        "create",
        "bronze.sales",
        "--env",
        "dev",
        "--schema",
        str(schema_file),
        "--column",
        "id:long",
    )

    assert (neither.exit_code, both.exit_code) == (2, 2)


def test_table_create_reports_a_missing_schema_file_with_exit_8(tmp_path: Path) -> None:
    """A `--schema` path that can't be read is a client-side file error."""
    result = invoke(
        make_state(tmp_path, FakeApi()),
        "table",
        "create",
        "bronze.sales",
        "--env",
        "dev",
        "--schema",
        str(tmp_path / "nope.json"),
    )

    assert result.exit_code == 8


def test_table_create_reports_a_schema_file_that_is_not_a_list_with_exit_8(tmp_path: Path) -> None:
    """A `--schema` file holding a JSON object, not a list, is a client-side file error."""
    schema_file = tmp_path / "schema.json"
    schema_file.write_text(json.dumps({"not": "a list"}))

    result = invoke(
        make_state(tmp_path, FakeApi()),
        "table",
        "create",
        "bronze.sales",
        "--env",
        "dev",
        "--schema",
        str(schema_file),
    )

    assert result.exit_code == 8


def test_table_get_shows_the_columns(tmp_path: Path) -> None:
    """`table get` prints the table and its schema; JSON carries `columns`."""
    api = FakeApi()
    api.on("GET", f"{_BASE}/sales", json=_TABLE)

    table_mode = invoke(make_state(tmp_path, api), "table", "get", "bronze.sales", "--env", "dev")
    json_mode = invoke(
        make_state(tmp_path, api),
        "--output",
        "json",
        "table",
        "get",
        "bronze.sales",
        "--env",
        "dev",
    )

    assert table_mode.exit_code == 0, table_mode.stderr
    assert "region" in table_mode.stdout
    header = next(line for line in table_mode.stdout.splitlines() if "nullable" in line)
    assert header.split() == [
        "┃", "name", "┃", "type", "┃", "nullable", "┃", "description", "┃",
        "description_source", "┃",
    ]  # fmt: skip
    assert json.loads(json_mode.stdout)["columns"][0] == {
        "name": "id",
        "type": "long",
        "nullable": False,
        "description": None,
        "description_source": None,
        "label": None,
        "label_source": None,
        "label_language": None,
        "description_language": None,
    }


def test_table_get_shows_the_description_and_the_columns_descriptions(tmp_path: Path) -> None:
    """The table's text comes with its source; each column's text is in the schema listing."""
    api = FakeApi()
    columns = [
        {**_COLUMNS[0], "description": "Order id", "description_source": "project"},
        _COLUMNS[1],
    ]
    api.on(
        "GET",
        f"{_BASE}/sales",
        json={
            **_TABLE,
            "schema": columns,
            "description": "All orders",
            "description_source": "edited",
        },
    )

    result = invoke(make_state(tmp_path, api), "table", "get", "bronze.sales", "--env", "dev")

    assert result.exit_code == 0, result.stderr
    plain = _plain(result.stdout)
    assert "All orders" in plain
    assert "edited" in plain
    assert "Order id" in plain


def test_table_describe_sets_the_table_description(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "PATCH",
        f"{_BASE}/sales",
        json={**_TABLE, "description": "Hi", "description_source": "edited"},
    )

    result = invoke(
        make_state(tmp_path, api),
        "table",
        "describe",
        "bronze.sales",
        "--text",
        "Hi",
        "--env",
        "dev",
    )

    assert result.exit_code == 0, result.stderr
    assert json.loads(api.requests[-1].content) == {"description": "Hi"}
    assert "bronze.sales" in result.stdout


def test_table_describe_with_a_column_sets_the_column_description(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("PUT", f"{_BASE}/sales/columns/id/description", json=_TABLE)

    result = invoke(
        make_state(tmp_path, api),
        "table",
        "describe",
        "bronze.sales",
        "--column",
        "id",
        "--text",
        "Key",
        "--env",
        "dev",
    )

    assert result.exit_code == 0, result.stderr
    assert json.loads(api.requests[-1].content) == {"description": "Key"}
    assert "id" in result.stdout


def test_table_describe_reset_sends_null_for_a_table_and_for_a_column(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "PATCH",
        f"{_BASE}/sales",
        json={**_TABLE, "description": "From git", "description_source": "project"},
    )
    api.on("PUT", f"{_BASE}/sales/columns/id/description", json=_TABLE)

    table = invoke(
        make_state(tmp_path, api), "table", "describe", "bronze.sales", "--reset", "--env", "dev"
    )
    column = invoke(
        make_state(tmp_path, api),
        "table",
        "describe",
        "bronze.sales",
        "--column",
        "id",
        "--reset",
        "--env",
        "dev",
    )

    assert table.exit_code == 0, table.stderr
    assert column.exit_code == 0, column.stderr
    assert [json.loads(r.content) for r in api.requests] == [
        {"description": None, "label": None}
    ] * 2
    assert "project" in table.stdout
    reset_line = "Reset the description and label of bronze.sales in dev (en); it now shows"
    assert f"{reset_line} the project's text" in _plain(table.stdout)


@pytest.mark.parametrize("flags", [[], ["--text", "a", "--reset"], ["--label", "a", "--reset"]])
def test_table_describe_needs_exactly_one_of_text_and_reset(
    tmp_path: Path, flags: list[str]
) -> None:
    api = FakeApi()

    result = invoke(
        make_state(tmp_path, api), "table", "describe", "bronze.sales", *flags, "--env", "dev"
    )

    assert result.exit_code != 0
    assert (
        "Give --text TEXT and/or --label LABEL to set, or --reset to go back to the project's "
        "texts."
    ) in _plain(result.stderr)
    assert api.requests == []
    assert "--text" in _plain(
        invoke(make_state(tmp_path, api), "table", "describe", "--help").stdout
    )


def test_table_describe_sets_a_label_in_a_language(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "PATCH",
        f"{_BASE}/sales",
        json={**_TABLE, "label": "Ventes", "label_language": "fr", "language": "fr"},
    )
    result = invoke(
        make_state(tmp_path, api),
        "table",
        "describe",
        "bronze.sales",
        "--label",
        "Ventes",
        "--lang",
        "fr",
        "--env",
        "dev",
    )
    assert result.exit_code == 0, result.stderr
    assert json.loads(api.requests[-1].content) == {"label": "Ventes"}
    assert api.requests[-1].url.params["lang"] == "fr"
    assert "Described bronze.sales in dev (fr)" in result.stdout


def test_table_describe_without_lang_names_the_main_language(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "PATCH",
        f"{_BASE}/sales",
        json={
            **_TABLE,
            "description": "Hi",
            "description_source": "edited",
            "main_language": "de",
        },
    )
    result = invoke(
        make_state(tmp_path, api),
        "table",
        "describe",
        "bronze.sales",
        "--text",
        "Hi",
        "--env",
        "dev",
    )
    assert result.exit_code == 0, result.stderr
    assert "lang" not in api.requests[-1].url.params
    assert "Described bronze.sales in dev (de)" in result.stdout


def test_table_describe_reset_of_a_column_in_a_language_says_what_shows(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("PUT", f"{_BASE}/sales/columns/id/description", json=_TABLE)
    result = invoke(
        make_state(tmp_path, api),
        "table",
        "describe",
        "bronze.sales",
        "--column",
        "id",
        "--reset",
        "--lang",
        "fr",
        "--env",
        "dev",
    )
    assert result.exit_code == 0, result.stderr
    assert (
        "Reset the description and label of bronze.sales column id in dev (fr); it now has no text"
        in (_plain(result.stdout))
    )


@pytest.mark.parametrize(
    ("answer", "after"),
    [
        (
            {"description": "Hand", "description_source": "edited", "description_language": "en"},
            "it now shows an edit in en",
        ),
        (
            {
                "description": "From git",
                "description_source": "project",
                "description_language": "en",
                "label": "Hand",
                "label_source": "edited",
                "label_language": "de",
            },
            "it now shows an edit and the project's text in de, en",
        ),
    ],
)
def test_table_describe_reset_says_which_text_shows_now_and_in_which_language(
    tmp_path: Path, answer: dict[str, str], after: str
) -> None:
    api = FakeApi()
    api.on("PATCH", f"{_BASE}/sales", json={**_TABLE, **answer})
    result = invoke(
        make_state(tmp_path, api),
        "table",
        "describe",
        "bronze.sales",
        "--reset",
        "--lang",
        "fr",
        "--env",
        "dev",
    )
    assert result.exit_code == 0, result.stderr
    assert f"in dev (fr); {after}" in _plain(result.stdout)


def test_table_append_conforms_a_csv_and_streams_it(tmp_path: Path) -> None:
    """A CSV is cast to the table's types and sent as an Arrow stream."""
    api = FakeApi()
    api.on("GET", f"{_BASE}/sales", json=_TABLE)
    api.on("POST", f"{_BASE}/sales/data", json={"rows": 2, "files": 1, "snapshot_id": 9})
    csv = tmp_path / "data.csv"
    csv.write_text("region,id\neu,1\n,2\n")

    result = invoke(
        make_state(tmp_path, api), "table", "append", "bronze.sales", str(csv), "--env", "dev"
    )

    assert result.exit_code == 0, result.stderr
    assert "Appended 2 rows to dev/bronze.sales" in result.stdout
    sent = pyarrow.ipc.open_stream(io.BytesIO(api.requests[-1].content)).read_all()
    assert sent.schema == pa.schema(
        [pa.field("id", pa.int64(), nullable=False), pa.field("region", pa.string())]
    )
    assert sent.column("region").to_pylist() == ["eu", None]


def test_table_append_names_a_bad_column_and_sends_nothing(tmp_path: Path) -> None:
    """A CSV value that can't be cast exits 8 before any data is sent."""
    api = FakeApi()
    api.on("GET", f"{_BASE}/sales", json=_TABLE)
    csv = tmp_path / "data.csv"
    csv.write_text("id,region\nseven,eu\n")

    result = invoke(
        make_state(tmp_path, api), "table", "append", "bronze.sales", str(csv), "--env", "dev"
    )

    assert result.exit_code == 8
    assert "'id'" in result.stderr
    assert [r.method for r in api.requests] == ["GET"]


def test_table_list_and_delete(tmp_path: Path) -> None:
    """`table list NS` lists; `table delete` asks first."""
    api = FakeApi()
    api.on("GET", _BASE, json=[_TABLE])
    api.on("DELETE", f"{_BASE}/sales", status=204)

    listed = invoke(make_state(tmp_path, api), "table", "list", "bronze", "--env", "dev")
    refused = invoke(make_state(tmp_path, api), "table", "delete", "bronze.sales", "--env", "dev")
    deleted = invoke(
        make_state(tmp_path, api), "table", "delete", "bronze.sales", "--env", "dev", "--yes"
    )

    assert "sales" in listed.stdout
    assert refused.exit_code == 2
    assert deleted.exit_code == 0, deleted.stderr
    assert "Deleted table dev/bronze.sales" in deleted.stdout


def test_table_delete_says_how_to_undo_and_purge_asks_for_it(tmp_path: Path) -> None:
    """The confirmation names `undrop`; `--purge` sends `purge=true` and says it is final."""
    api = FakeApi()
    api.on("DELETE", f"{_BASE}/sales", status=204)

    kept = invoke(
        make_state(tmp_path, api), "table", "delete", "bronze.sales", "--env", "dev", "--yes"
    )
    purged = invoke(
        make_state(tmp_path, api),
        "table",
        "delete",
        "bronze.sales",
        "--env",
        "dev",
        "--purge",
        "--yes",
    )

    assert kept.exit_code == 0, kept.stderr
    assert "lakana table undrop bronze.sales --env dev" in _plain(kept.stdout)
    assert "lakana table list bronze --env dev --dropped" in _plain(kept.stdout)
    assert purged.exit_code == 0, purged.stderr
    assert "cannot be restored" in _plain(purged.stdout)
    assert [str(r.url.query, "ascii") for r in api.requests] == ["", "purge=true"]


def test_table_delete_with_purge_asks_a_harder_question(tmp_path: Path) -> None:
    """Without `--yes`, the prompt for `--purge` says the files go at the next sweep."""
    api = FakeApi()
    result = invoke(
        make_state(tmp_path, api), "table", "delete", "bronze.sales", "--purge", "--env", "dev"
    )
    assert result.exit_code == 2
    assert "cannot be undone" in _plain(result.stderr + result.stdout)
    assert api.requests == []


def test_table_list_dropped_shows_the_tombstones(tmp_path: Path) -> None:
    """`--dropped` lists what can be restored and until when."""
    api = FakeApi()
    api.on(
        "GET",
        "/api/v1/environments/dev/namespaces/bronze/dropped-tables",
        json=[
            {
                "name": "sales",
                "namespace": "bronze",
                "uuid": "u1",
                "kind": "TABLE",
                "dropped_at": "2026-10-19T12:00:00Z",
                "purge_after": "2026-10-26T12:00:00Z",
                "restorable": True,
            }
        ],
    )

    result = invoke(
        make_state(tmp_path, api), "table", "list", "bronze", "--dropped", "--env", "dev"
    )
    as_json = invoke(
        make_state(tmp_path, api),
        "--output",
        "json",
        "table",
        "list",
        "bronze",
        "--dropped",
        "--env",
        "dev",
    )

    assert result.exit_code == 0, result.stderr
    text = _plain(result.stdout)
    assert "sales" in text and "2026-10-26" in text and "yes" in text
    assert json.loads(as_json.stdout)[0]["restorable"] is True


def test_table_undrop_restores_and_names_the_new_name(tmp_path: Path) -> None:
    """`undrop` POSTs to the route; `--as` sends `new_name` and the line names both."""
    api = FakeApi()
    api.on("POST", f"{_BASE}/sales/undrop", json={**_TABLE, "name": "sales_before"})

    plain = invoke(make_state(tmp_path, api), "table", "undrop", "bronze.sales", "--env", "dev")
    renamed = invoke(
        make_state(tmp_path, api),
        "table",
        "undrop",
        "bronze.sales",
        "--as",
        "sales_before",
        "--env",
        "dev",
    )

    assert plain.exit_code == 0, plain.stderr
    assert "Restored table dev/bronze.sales" in _plain(plain.stdout)
    assert renamed.exit_code == 0, renamed.stderr
    assert "as bronze.sales_before" in _plain(renamed.stdout)
    assert [json.loads(r.content) for r in api.requests] == [
        {"new_name": None, "break_glass_reason": None},
        {"new_name": "sales_before", "break_glass_reason": None},
    ]


def test_table_create_external(tmp_path: Path) -> None:
    """`create-external` forwards the location pattern and format."""
    api = FakeApi()
    api.on(
        "POST",
        f"{_BASE}/external",
        status=201,
        json={
            "name": "raw",
            "namespace": "bronze",
            "location_pattern": "raw/x/*.csv",
            "format": "csv",
            "schema": {"a": "string"},
        },
    )

    result = invoke(
        make_state(tmp_path, api),
        "table",
        "create-external",
        "bronze.raw",
        "--env",
        "dev",
        "--location",
        "raw/x/*.csv",
        "--format",
        "csv",
    )

    assert result.exit_code == 0, result.stderr
    body = json.loads(api.requests[0].content)
    assert (body["location_pattern"], body["format"]) == ("raw/x/*.csv", "csv")


def _append_csv(
    tmp_path: Path, columns: list[dict[str, object]], text: str
) -> tuple[Result, FakeApi]:
    """Append `text` as a CSV to a table with `columns`, returning the result and the fake.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    columns
        The table's JSON column list.
    text
        The CSV file's content.

    Returns
    -------
    tuple
        The CLI result and the fake API that recorded the requests.
    """
    api = FakeApi()
    api.on("GET", f"{_BASE}/sales", json={**_TABLE, "schema": columns})
    api.on("POST", f"{_BASE}/sales/data", json={"rows": 1, "files": 1, "snapshot_id": 9})
    csv = tmp_path / "data.csv"
    csv.write_text(text)
    result = invoke(
        make_state(tmp_path, api), "table", "append", "bronze.sales", str(csv), "--env", "dev"
    )
    return result, api


def _sent(api: FakeApi) -> pa.Table:
    """Decode the Arrow stream the last request sent.

    Parameters
    ----------
    api
        The fake API.

    Returns
    -------
    pa.Table
        The appended data.
    """
    return pyarrow.ipc.open_stream(io.BytesIO(api.requests[-1].content)).read_all()


def test_table_append_keeps_leading_zeros_in_a_string_column(tmp_path: Path) -> None:
    """Final review C1: a CSV is parsed with the table's types, so "01234" stays a string."""
    result, api = _append_csv(
        tmp_path, [{"name": "zip", "type": "string", "nullable": True}], "zip\n01234\n00042\n"
    )

    assert result.exit_code == 0, result.stderr
    assert _sent(api).column("zip").to_pylist() == ["01234", "00042"]


def test_table_append_parses_a_wide_decimal_exactly(tmp_path: Path) -> None:
    """Final review C1: a decimal(38,19) value never goes through a double."""
    result, api = _append_csv(
        tmp_path,
        [{"name": "x", "type": "decimal(38,19)", "nullable": True}],
        "x\n0.1234567890123456789\n",
    )

    assert result.exit_code == 0, result.stderr
    assert _sent(api).column("x").to_pylist() == [Decimal("0.1234567890123456789")]


def test_table_append_keeps_an_iso_timestamp_string_byte_identical(tmp_path: Path) -> None:
    """Final review C1: timestamp-looking text in a string column isn't reformatted."""
    result, api = _append_csv(
        tmp_path,
        [{"name": "s", "type": "string", "nullable": True}],
        "s\n2024-01-01T00:00:00Z\n",
    )

    assert result.exit_code == 0, result.stderr
    assert _sent(api).column("s").to_pylist() == ["2024-01-01T00:00:00Z"]


def test_table_append_keeps_scientific_notation_text_in_a_string_column(tmp_path: Path) -> None:
    """Final review C1: "1e5" in a string column stays "1e5", not "100000"."""
    result, api = _append_csv(
        tmp_path, [{"name": "s", "type": "string", "nullable": True}], "s\n1e5\n"
    )

    assert result.exit_code == 0, result.stderr
    assert _sent(api).column("s").to_pylist() == ["1e5"]


def test_table_append_refuses_a_lossy_decimal_naming_the_column(tmp_path: Path) -> None:
    """Final review C1: "2.999" into decimal(10,2) still exits 8 naming the column."""
    result, api = _append_csv(
        tmp_path, [{"name": "amount", "type": "decimal(10,2)", "nullable": True}], "amount\n2.999\n"
    )

    assert result.exit_code == 8
    assert "Column 'amount' can't be read as decimal(10,2)" in result.stderr
    assert [r.method for r in api.requests] == ["GET"]


def test_table_append_names_the_column_a_value_does_not_parse_in(tmp_path: Path) -> None:
    """Final review C1: a parse failure names the CSV column, not its index."""
    result, api = _append_csv(tmp_path, _COLUMNS, "region,id\neu,1\nus,seven\n")

    assert result.exit_code == 8
    assert "Column 'id' can't be read as long" in result.stderr
    assert "#1" not in result.stderr
    assert [r.method for r in api.requests] == ["GET"]


def test_table_append_accepts_a_header_only_csv(tmp_path: Path) -> None:
    """Final review C1: zero rows still conform when parsed with the table's types."""
    result, api = _append_csv(tmp_path, _COLUMNS, "id,region\n")

    assert result.exit_code == 0, result.stderr
    assert _sent(api).num_rows == 0


def test_table_append_keeps_na_literal_in_a_string_column(tmp_path: Path) -> None:
    """Final review C1: only an empty field is null; "NA" stays literal text."""
    result, api = _append_csv(tmp_path, _COLUMNS, "id,region\n1,NA\n2,\n")

    assert result.exit_code == 0, result.stderr
    assert _sent(api).column("region").to_pylist() == ["NA", None]


def test_table_append_still_reports_columns_missing_from_the_csv(tmp_path: Path) -> None:
    """Final review C1: a column the CSV lacks, or one it adds, is still reported."""
    result, _ = _append_csv(tmp_path, _COLUMNS, "id,extra\n1,x\n")

    assert result.exit_code == 8
    assert "missing ['region']; unexpected ['extra']" in result.stderr


@pytest.mark.parametrize("command", ["append", "replace"])
def test_table_append_refuses_a_csv_that_is_not_utf8(tmp_path: Path, command: str) -> None:
    """#351: a Latin-1 CSV is refused as `lakana load` refuses it, before anything is sent."""
    api = FakeApi()
    api.on("GET", f"{_BASE}/sales", json=_TABLE)
    csv = tmp_path / "data.csv"
    csv.write_bytes("id,region\n1,M\u00fcnchen\n".encode("latin-1"))
    extra = ["--evolve-schema"] if command == "replace" else []

    result = invoke(
        make_state(tmp_path, api),
        "table",
        command,
        "bronze.sales",
        str(csv),
        "--env",
        "dev",
        *extra,
    )

    assert result.exit_code == 8, result.output
    assert (
        "data.csv is not UTF-8 (byte 0xfc at position 13); convert it to UTF-8 and run the "
        "command again"
    ) in _plain(result.stderr)
    assert all(r.method == "GET" for r in api.requests)


@pytest.mark.parametrize("command", ["append", "replace", "merge"])
def test_table_help_says_a_csv_must_be_utf8(tmp_path: Path, command: str) -> None:
    """Every command that reads a CSV says it must be UTF-8, `replace --evolve-schema` too."""
    result = invoke(make_state(tmp_path, FakeApi()), "table", command, "--help")

    assert "A CSV must be UTF-8" in _plain(result.stdout)


def test_table_append_reads_a_utf8_csv_with_a_byte_order_mark(tmp_path: Path) -> None:
    """A byte order mark is dropped, as `lakana load` drops it; the first column keeps its name."""
    result, api = _append_csv(tmp_path, _COLUMNS, "\ufeffid,region\n1,M\u00fcnchen\n")

    assert result.exit_code == 0, result.stderr
    assert _sent(api).column("region").to_pylist() == ["M\u00fcnchen"]


def test_table_create_column_help_shows_a_quoted_nullable_example(tmp_path: Path) -> None:
    """Final review minor 10: shells glob `?`, so the help's example is quoted."""
    result = invoke(make_state(tmp_path, FakeApi()), "table", "create", "--help")

    assert "'region:string?'" in " ".join(result.stdout.split())


def test_table_append_against_a_server_without_schemas_says_to_upgrade(tmp_path: Path) -> None:
    """With no columns reported (an older server), append exits 11 before reading data."""
    api = FakeApi()
    old = {k: v for k, v in _TABLE.items() if k != "schema"}
    api.on("GET", f"{_BASE}/sales", json=old)
    csv = tmp_path / "data.csv"
    csv.write_text("id,region\n1,eu\n")

    result = invoke(
        make_state(tmp_path, api), "table", "append", "bronze.sales", str(csv), "--env", "dev"
    )

    assert result.exit_code == 11
    assert "upgrade" in result.stderr.lower()
    assert [r.method for r in api.requests] == ["GET"]


def test_table_replace_conforms_the_file_and_puts_it(tmp_path: Path) -> None:
    """`table replace` conforms like `append`, then PUTs, passing `--source`s through."""
    api = FakeApi()
    api.on("GET", f"{_BASE}/sales", json=_TABLE)
    api.on(
        "PUT",
        f"{_BASE}/sales/data",
        json={"rows": 1, "files": 1, "snapshot_id": 4, "schema_diff": None},
    )
    csv = tmp_path / "data.csv"
    csv.write_text("id,region\n1,eu\n")

    result = invoke(
        make_state(tmp_path, api),
        "table",
        "replace",
        "bronze.sales",
        str(csv),
        "--source",
        "bronze.raw",
        "--env",
        "dev",
    )

    assert result.exit_code == 0, result.stderr
    assert "Replaced dev/bronze.sales with 1 rows" in result.stdout
    put = api.requests[-1]
    assert put.method == "PUT"
    assert put.url.params.multi_items() == [("evolve_schema", "false"), ("source", "bronze.raw")]
    sent = pyarrow.ipc.open_stream(io.BytesIO(put.content)).read_all()
    assert sent.schema.field("id").type == pa.int64()


def test_table_replace_with_evolve_schema_sends_the_file_as_it_is(tmp_path: Path) -> None:
    """With `--evolve-schema` the file isn't conformed (the table's schema is what changes)."""
    api = FakeApi()
    api.on("PUT", f"{_BASE}/sales/data", json={"rows": 1, "files": 1, "snapshot_id": 4})
    csv = tmp_path / "data.csv"
    csv.write_text("id,extra\n1,x\n")

    result = invoke(
        make_state(tmp_path, api),
        "table",
        "replace",
        "bronze.sales",
        str(csv),
        "--evolve-schema",
        "--env",
        "dev",
    )

    assert result.exit_code == 0, result.stderr
    assert [r.method for r in api.requests] == ["PUT"]
    sent = pyarrow.ipc.open_stream(io.BytesIO(api.requests[-1].content)).read_all()
    assert sent.column_names == ["id", "extra"]


def test_table_merge_conforms_the_file_and_posts_it_with_the_key(tmp_path: Path) -> None:
    """`table merge` conforms like `append`, sends each `--key`, and prints the counts."""
    api = FakeApi()
    api.on("GET", f"{_BASE}/sales", json=_TABLE)
    api.on(
        "POST",
        f"{_BASE}/sales/merge",
        json={"snapshot_id": 12, "rows_inserted": 1, "rows_updated": 2},
    )
    csv = tmp_path / "data.csv"
    csv.write_text("region,id\neu,1\nus,2\n,3\n")

    result = invoke(
        make_state(tmp_path, api),
        "table",
        "merge",
        "bronze.sales",
        str(csv),
        "--key",
        "id",
        "--key",
        "region",
        "--source",
        "bronze.raw",
        "--env",
        "dev",
    )

    assert result.exit_code == 0, result.stderr
    assert "Merged into dev/bronze.sales: 1 rows inserted, 2 updated" in result.stdout
    post = api.requests[-1]
    assert post.method == "POST"
    assert post.url.params.multi_items() == [
        ("key", "id"),
        ("key", "region"),
        ("source", "bronze.raw"),
    ]
    sent = pyarrow.ipc.open_stream(io.BytesIO(post.content)).read_all()
    assert sent.schema == pa.schema(
        [pa.field("id", pa.int64(), nullable=False), pa.field("region", pa.string())]
    )


def test_table_merge_needs_a_key(tmp_path: Path) -> None:
    """Without `--key` nothing is read or sent, and the error names the option."""
    api = FakeApi()
    csv = tmp_path / "data.csv"
    csv.write_text("id,region\n1,eu\n")

    result = invoke(
        make_state(tmp_path, api), "table", "merge", "bronze.sales", str(csv), "--env", "dev"
    )

    assert result.exit_code != 0
    assert "--key" in _plain(result.stderr)
    assert api.requests == []


def test_table_append_passes_sources_through(tmp_path: Path) -> None:
    """`--source` on `table append` becomes the route's `source` parameter."""
    api = FakeApi()
    api.on("GET", f"{_BASE}/sales", json=_TABLE)
    api.on("POST", f"{_BASE}/sales/data", json={"rows": 1, "files": 1, "snapshot_id": 9})
    csv = tmp_path / "data.csv"
    csv.write_text("id,region\n1,eu\n")

    result = invoke(
        make_state(tmp_path, api),
        "table",
        "append",
        "bronze.sales",
        str(csv),
        "--source",
        "bronze.a",
        "--source",
        "bronze.b",
        "--env",
        "dev",
    )

    assert result.exit_code == 0, result.stderr
    assert api.requests[-1].url.params.multi_items() == [
        ("source", "bronze.a"),
        ("source", "bronze.b"),
    ]


def test_table_list_shows_the_kind(tmp_path: Path) -> None:
    """`table list` gains a `kind` column."""
    api = FakeApi()
    api.on("GET", _BASE, json=[_TABLE, {**_TABLE, "name": "v", "kind": "VIEW"}])

    result = invoke(make_state(tmp_path, api), "table", "list", "bronze", "--env", "dev")

    assert result.exit_code == 0, result.stderr
    assert "kind" in result.stdout
    assert "VIEW" in result.stdout


def test_table_get_on_a_view_shows_its_sql_and_references(tmp_path: Path) -> None:
    """`table get` on a view shows its SQL, references and columns."""
    api = FakeApi()
    api.on(
        "GET",
        f"{_BASE}/v",
        json={
            **_TABLE,
            "name": "v",
            "kind": "VIEW",
            "metadata_location": None,
            "sql": "SELECT id FROM bronze.sales",
            "references": [{"id": "u0", "name": "bronze.sales"}],
        },
    )

    result = invoke(make_state(tmp_path, api), "table", "get", "bronze.v", "--env", "dev")

    assert result.exit_code == 0, result.stderr
    assert "SELECT id FROM bronze.sales" in result.stdout
    assert "bronze.sales" in result.stdout


def test_table_get_on_a_view_with_hidden_references_says_the_sql_is_hidden(
    tmp_path: Path,
) -> None:
    """A reader who can't read every reference sees a count, never the SQL."""
    api = FakeApi()
    api.on(
        "GET",
        f"{_BASE}/v",
        json={
            **_TABLE,
            "name": "v",
            "kind": "VIEW",
            "metadata_location": None,
            "sql": None,
            "references": [],
            "hidden_references": 2,
        },
    )

    result = invoke(make_state(tmp_path, api), "table", "get", "bronze.v", "--env", "dev")

    assert result.exit_code == 0, result.stderr
    assert "SQL hidden: 2 referenced table(s) you can't read" in result.stdout


_LINEAGE = {
    "root_id": "t",
    "direction": "upstream",
    "depth": 3,
    "nodes": [
        {"id": "t", "name": "bronze.sales", "kind": "TABLE", "dropped": False},
        {"id": "v", "name": "bronze.v", "kind": "VIEW", "dropped": False},
        {"id": "o", "name": "bronze.old", "kind": None, "dropped": True},
    ],
    "edges": [
        {
            "source_id": "v",
            "target_id": "t",
            "source_snapshot_id": None,
            "target_snapshot_id": 2,
            "query_run_id": None,
            "created_at": "2026-09-29T10:00:00Z",
        },
        {
            "source_id": "o",
            "target_id": "v",
            "source_snapshot_id": None,
            "target_snapshot_id": None,
            "query_run_id": None,
            "created_at": "2026-09-28T10:00:00Z",
        },
    ],
    "hidden_nodes": 2,
    "truncated": False,
}


def test_table_lineage_prints_a_tree(tmp_path: Path) -> None:
    """`table lineage` prints the graph as a tree, marking kinds, drops and hidden nodes."""
    api = FakeApi()
    api.on("GET", f"{_BASE}/sales/lineage", json=_LINEAGE)

    result = invoke(make_state(tmp_path, api), "table", "lineage", "bronze.sales", "--env", "dev")

    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines() == [
        "bronze.sales",
        "└── bronze.v [view]",
        "    └── bronze.old (dropped)",
        "(2 more nodes hidden: you can't read them)",
    ]


def test_table_lineage_prints_the_declared_connections(tmp_path: Path) -> None:
    """A connection source prints as `connection <name> (<kind>)`; a deleted one says so."""
    api = FakeApi()
    api.on(
        "GET",
        f"{_BASE}/customers/lineage",
        json={
            "root_id": "t",
            "direction": "upstream",
            "depth": 3,
            "nodes": [
                {"id": "t", "name": "bronze.customers", "kind": "TABLE", "dropped": False},
                {
                    "id": "c",
                    "name": "crm-db",
                    "kind": "CONNECTION",
                    "dropped": False,
                    "connection_kind": "postgresql",
                },
                {"id": "d", "name": "old-files", "kind": "CONNECTION", "dropped": True},
            ],
            "edges": [
                {
                    "source_id": source,
                    "target_id": "t",
                    "source_snapshot_id": None,
                    "target_snapshot_id": 2,
                    "query_run_id": None,
                    "created_at": "2026-10-24T10:00:00Z",
                }
                for source in ("c", "d")
            ],
            "hidden_nodes": 0,
            "truncated": False,
        },
    )

    result = invoke(
        make_state(tmp_path, api), "table", "lineage", "bronze.customers", "--env", "dev"
    )

    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines() == [
        "bronze.customers",
        "├── connection crm-db (postgresql)",
        "└── connection old-files (deleted)",
    ]


def test_table_lineage_passes_direction_and_depth_and_prints_json(tmp_path: Path) -> None:
    """`--downstream --depth N` go to the API; `--output json` prints the graph."""
    api = FakeApi()
    api.on("GET", f"{_BASE}/sales/lineage", json=_LINEAGE)

    result = invoke(
        make_state(tmp_path, api),
        "--output",
        "json",
        "table",
        "lineage",
        "bronze.sales",
        "--downstream",
        "--depth",
        "5",
        "--env",
        "dev",
    )

    assert result.exit_code == 0, result.stderr
    assert dict(api.requests[-1].url.params) == {"direction": "downstream", "depth": "5"}
    assert json.loads(result.stdout)["hidden_nodes"] == 2


def test_table_lineage_expands_a_self_edge_once_and_omits_the_hidden_line(
    tmp_path: Path,
) -> None:
    """A self-edge prints once under the root, and no hidden line appears at zero."""
    api = FakeApi()
    api.on(
        "GET",
        f"{_BASE}/sales/lineage",
        json={
            "root_id": "t",
            "direction": "upstream",
            "depth": 3,
            "nodes": [{"id": "t", "name": "bronze.sales", "kind": "TABLE", "dropped": False}],
            "edges": [
                {
                    "source_id": "t",
                    "target_id": "t",
                    "source_snapshot_id": 1,
                    "target_snapshot_id": 2,
                    "query_run_id": None,
                    "created_at": "2026-09-29T10:00:00Z",
                }
            ],
            "hidden_nodes": 0,
            "truncated": False,
        },
    )

    result = invoke(make_state(tmp_path, api), "table", "lineage", "bronze.sales", "--env", "dev")

    assert result.stdout.splitlines() == ["bronze.sales", "└── bronze.sales (see above)"]


def _node(node_id: str, name: str) -> dict[str, object]:
    return {"id": node_id, "name": name, "kind": "TABLE", "dropped": False}


def _edge(source: str, target: str) -> dict[str, object]:
    return {
        "source_id": source,
        "target_id": target,
        "source_snapshot_id": None,
        "target_snapshot_id": None,
        "query_run_id": None,
        "created_at": "2026-09-29T10:00:00Z",
    }


def test_table_lineage_expands_a_shared_ancestor_once(tmp_path: Path) -> None:
    """A diamond A<-B, A<-C, B<-D, C<-D prints D's subtree once, then `(see above)`."""
    api = FakeApi()
    api.on(
        "GET",
        f"{_BASE}/sales/lineage",
        json={
            "root_id": "a",
            "direction": "upstream",
            "depth": 3,
            "nodes": [_node(i, f"bronze.{i}") for i in "abcde"],
            "edges": [
                _edge("b", "a"),
                _edge("c", "a"),
                _edge("d", "b"),
                _edge("d", "c"),
                _edge("e", "d"),
            ],
            "hidden_nodes": 1,
            "truncated": True,
        },
    )

    result = invoke(make_state(tmp_path, api), "table", "lineage", "bronze.sales", "--env", "dev")

    assert result.stdout.splitlines() == [
        "bronze.a",
        "├── bronze.b",
        "│   └── bronze.d",
        "│       └── bronze.e",
        "└── bronze.c",
        "    └── bronze.d (see above)",
        "(1 more node hidden: you can't read it)",
        "… truncated at 500 nodes",
    ]


def _maintenance_api(
    dry_run: bool,
    note: str | None = None,
    expired: list[int] | None = None,
    *,
    conflict: bool = False,
    tags_removed: list[str] | None = None,
) -> FakeApi:
    """Build a fake that accepts a maintenance run and reports it finished.

    Parameters
    ----------
    dry_run
        Whether the reported run was a dry run.
    note
        The report's note, if any.
    expired
        The expired snapshot ids (default two).
    conflict
        The report's `conflict` flag.
    tags_removed
        The report's `tags_removed`; none when `None`.

    Returns
    -------
    FakeApi
        The fake.
    """
    api = FakeApi()
    api.on("POST", f"{_BASE}/sales/maintenance", status=202, json={"query_id": "m1"})
    api.on(
        "GET",
        "/api/v1/environments/dev/queries/m1",
        json={
            "query_id": "m1",
            "status": "SUCCEEDED",
            "error_message": None,
            "lineage": None,
            "write_mode": "maintain",
            "target": "bronze.sales",
            "maintenance": {
                "request": {"keep_days": 3, "keep_last": 2, "dry_run": dry_run},
                "report": {
                    "dry_run": dry_run,
                    "snapshots_expired": [1, 2] if expired is None else expired,
                    "files_deleted": 4,
                    "bytes_deleted": 2048,
                    "note": note,
                    "conflict": conflict,
                    "tags_removed": tags_removed or [],
                },
            },
        },
    )
    return api


def test_table_maintain_waits_and_reports(tmp_path: Path) -> None:
    """`table maintain` sends the options, waits, and prints the report."""
    api = _maintenance_api(dry_run=False)

    result = invoke(
        make_state(tmp_path, api),
        "table",
        "maintain",
        "bronze.sales",
        "--keep-days",
        "3",
        "--keep-last",
        "2",
        "--env",
        "dev",
    )

    assert result.exit_code == 0, result.stderr
    assert json.loads(api.requests[0].content) == {
        "keep_days": 3,
        "keep_last": 2,
        "dry_run": False,
        "older_than_hours": 72,
    }
    assert (
        "Maintained dev/bronze.sales: expired 2 snapshots, deleted 4 files (2048 bytes)"
        in result.stdout
    )


@pytest.mark.parametrize(
    ("dry_run", "tags", "line"),
    [
        (
            False,
            ["lakana-check-failed-r1"],
            "Maintained dev/bronze.sales: expired 2 snapshots, deleted 4 files (2048 bytes), "
            "removed 1 tag of a failed check older than 7 days",
        ),
        (
            False,
            ["lakana-check-failed-r1", "lakana-check-failed-r2"],
            "Maintained dev/bronze.sales: expired 2 snapshots, deleted 4 files (2048 bytes), "
            "removed 2 tags of failed checks older than 7 days",
        ),
        (
            True,
            ["lakana-check-failed-r1"],
            "Dry run for dev/bronze.sales: would expire 2 snapshots, would delete 4 files "
            "(2048 bytes), would remove 1 tag of a failed check older than 7 days",
        ),
    ],
)
def test_table_maintain_says_which_tags_of_failed_checks_it_removed(
    tmp_path: Path, dry_run: bool, tags: list[str], line: str
) -> None:
    """The line names the tags of failed checks removed (or, for a dry run, to be removed)."""
    api = _maintenance_api(dry_run=dry_run, tags_removed=tags)

    result = invoke(
        make_state(tmp_path, api),
        "table",
        "maintain",
        "bronze.sales",
        *(["--dry-run"] if dry_run else []),
        "--env",
        "dev",
    )

    assert result.exit_code == 0, result.stderr
    assert line in result.stdout


def test_table_maintain_sends_a_custom_older_than_hours(tmp_path: Path) -> None:
    """`--older-than-hours` goes out in the request."""
    api = _maintenance_api(dry_run=False)

    result = invoke(
        make_state(tmp_path, api),
        "table",
        "maintain",
        "bronze.sales",
        "--older-than-hours",
        "24",
        "--env",
        "dev",
    )

    assert result.exit_code == 0, result.stderr
    assert json.loads(api.requests[0].content)["older_than_hours"] == 24


def test_table_maintain_rejects_zero_older_than_hours(tmp_path: Path) -> None:
    """`--older-than-hours 0` is a usage error, before any request."""
    api = _maintenance_api(dry_run=False)

    result = invoke(
        make_state(tmp_path, api),
        "table",
        "maintain",
        "bronze.sales",
        "--older-than-hours",
        "0",
        "--env",
        "dev",
    )

    assert result.exit_code == 2
    assert "--older-than-hours" in _plain(result.stderr)
    assert api.requests == []


def test_table_maintain_help_says_young_orphans_are_never_deleted(tmp_path: Path) -> None:
    """The option's help states the safety guarantee."""
    result = invoke(make_state(tmp_path, FakeApi()), "table", "maintain", "--help")

    assert "--older-than-hours" in _plain(result.stdout)
    assert "never deleted" in " ".join(result.stdout.split())


def test_table_maintain_dry_run_says_what_it_would_do(tmp_path: Path) -> None:
    """`--dry-run` reports in the conditional."""
    result = invoke(
        make_state(tmp_path, _maintenance_api(dry_run=True)),
        "table",
        "maintain",
        "bronze.sales",
        "--dry-run",
        "--env",
        "dev",
    )

    assert result.exit_code == 0, result.stderr
    assert (
        "Dry run for dev/bronze.sales: would expire 2 snapshots, would delete 4 files (2048 bytes)"
        in result.stdout
    )


def test_table_maintain_prints_the_orphan_skip_note_and_succeeds(tmp_path: Path) -> None:
    """A skipped orphan removal is told to the user; the run still succeeded."""
    note = "referenced paths outside the table location; orphan removal skipped."
    result = invoke(
        make_state(tmp_path, _maintenance_api(dry_run=False, note=note)),
        "table",
        "maintain",
        "bronze.sales",
        "--env",
        "dev",
    )

    assert result.exit_code == 0, result.stderr
    assert f"deleted 4 files (2048 bytes). {note}" in result.stdout


def test_table_maintain_fails_when_the_table_changed_during_maintenance(tmp_path: Path) -> None:
    """A lost race applied nothing: exit 1, so a scheduler or script sees the failure.

    Decided by the report's machine-readable `conflict`, not by its note's
    wording: here the note says something else entirely.
    """
    result = invoke(
        make_state(
            tmp_path,
            _maintenance_api(dry_run=False, note="anything", expired=[], conflict=True),
        ),
        "table",
        "maintain",
        "bronze.sales",
        "--env",
        "dev",
    )

    assert result.exit_code == 1
    assert (
        "Maintenance of dev/bronze.sales not applied: table changed during maintenance; retry"
        in result.stderr
    )
    assert "Maintained" not in result.stdout


def test_table_maintain_ignores_the_notes_wording_without_conflict(tmp_path: Path) -> None:
    """The old retry note alone, with `conflict` false, is a success — wording isn't a signal."""
    result = invoke(
        make_state(
            tmp_path,
            _maintenance_api(dry_run=False, note="table changed during maintenance, retry"),
        ),
        "table",
        "maintain",
        "bronze.sales",
        "--env",
        "dev",
    )

    assert result.exit_code == 0, result.stderr


def test_table_maintain_rejects_an_older_than_hours_above_the_cap(tmp_path: Path) -> None:
    """`--older-than-hours 87601` (over ten years) is a usage error, before any request."""
    api = _maintenance_api(dry_run=False)

    result = invoke(
        make_state(tmp_path, api),
        "table",
        "maintain",
        "bronze.sales",
        "--older-than-hours",
        "87601",
        "--env",
        "dev",
    )

    assert result.exit_code == 2
    assert "--older-than-hours" in _plain(result.stderr)
    assert api.requests == []


def test_table_get_on_an_artifact_points_to_the_artifact_commands(tmp_path: Path) -> None:
    api = FakeApi()
    artifact = {
        "name": "churn",
        "namespace": "models",
        "uuid": "u",
        "kind": "ARTIFACT",
        "metadata_location": None,
        "schema": [],
    }
    api.on("GET", "/api/v1/environments/dev/namespaces/models/tables/churn", json=artifact)
    result = invoke(make_state(tmp_path, api), "table", "get", "models.churn", "--env", "dev")
    assert result.exit_code == 0, result.stderr
    assert (
        "models.churn is an artifact. See its versions with: "
        "lakana artifact versions list models.churn" in _plain(result.stdout)
    )
    assert "ARTIFACT" in result.stdout
    as_json = invoke(
        make_state(tmp_path, api),
        "--output",
        "json",
        "table",
        "get",
        "models.churn",
        "--env",
        "dev",
    )
    assert json.loads(as_json.stdout)["kind"] == "ARTIFACT"


def test_table_get_on_an_artifact_with_a_hostile_name_shows_it_escaped(tmp_path: Path) -> None:
    """The name is echoed in a sentence and in a command; neither keeps a raw escape."""
    name = "ch\x1b[2Jurn"
    api = FakeApi()
    artifact = {
        "name": name,
        "namespace": "models",
        "uuid": "u",
        "kind": "ARTIFACT",
        "metadata_location": None,
        "schema": [],
    }
    api.on("GET", f"/api/v1/environments/dev/namespaces/models/tables/{name}", json=artifact)
    result = invoke(make_state(tmp_path, api), "table", "get", f"models.{name}", "--env", "dev")
    assert result.exit_code == 0, result.stderr
    assert "\x1b" not in result.stdout
    assert "ch\\x1b[2Jurn is an artifact" in _plain(result.stdout)


_NOT_PUBLISHED = {
    **_TABLE,
    "state": "not_published",
    "state_message": (
        "`bronze.sales` has not been published: its first build failed the check `unique_id` "
        "(run r1). It is published by the next run in which its checks pass."
    ),
}


def test_table_list_has_a_state_column(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "GET",
        _BASE,
        json=[
            _NOT_PUBLISHED,
            {**_TABLE, "name": "fine", "state": "published", "state_message": None},
            {**_TABLE, "name": "v", "kind": "VIEW", "state": "published"},
        ],
    )

    result = invoke(make_state(tmp_path, api), "table", "list", "bronze", "--env", "dev")

    assert result.exit_code == 0, result.stderr
    rows = {
        cells[1]: cells[3:5]
        for line in result.stdout.splitlines()
        if (cells := [c.strip() for c in line.split("│")]) and len(cells) > 5
    }
    assert "state" in result.stdout
    assert rows["sales"] == ["TABLE", "not published"]
    assert rows["fine"] == ["TABLE", "published"]
    assert rows["v"] == ["VIEW", ""]


def test_table_get_prints_why_a_table_is_not_published(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", f"{_BASE}/sales", json=_NOT_PUBLISHED)
    api.on("GET", f"{_BASE}/fine", json={**_TABLE, "name": "fine"})

    stopped = invoke(make_state(tmp_path, api), "table", "get", "bronze.sales", "--env", "dev")
    fine = invoke(make_state(tmp_path, api), "table", "get", "bronze.fine", "--env", "dev")

    assert stopped.exit_code == 0, stopped.stderr
    lines = [line.strip() for line in stopped.stdout.splitlines()]
    assert "not published" in stopped.stdout
    message = str(_NOT_PUBLISHED["state_message"])
    assert message in lines
    assert lines.index(message) > max(i for i, line in enumerate(lines) if "state" in line)
    assert fine.exit_code == 0, fine.stderr
    assert "has not been published" not in fine.stdout
    assert "published" in fine.stdout


def test_table_get_prints_why_a_table_is_kept(tmp_path: Path) -> None:
    api = FakeApi()
    message = (
        "Kept after a failed check (`bronze.sales/unique_id`, run 7): the table holds the data "
        "that failed. Tasks do not read it until its checks pass."
    )
    api.on("GET", f"{_BASE}/sales", json={**_TABLE, "state": "kept", "state_message": message})

    kept = invoke(make_state(tmp_path, api), "table", "get", "bronze.sales", "--env", "dev")

    assert kept.exit_code == 0, kept.stderr
    lines = [line.strip() for line in kept.stdout.splitlines()]
    assert message in lines
    assert _state_row(kept.stdout) == ["kept"]


def test_table_get_in_json_carries_the_state_as_the_server_sent_it(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", f"{_BASE}/sales", json=_NOT_PUBLISHED)

    result = invoke(
        make_state(tmp_path, api),
        "--output",
        "json",
        "table",
        "get",
        "bronze.sales",
        "--env",
        "dev",
    )

    assert json.loads(result.stdout)["state"] == "not_published"
    assert json.loads(result.stdout)["state_message"] == _NOT_PUBLISHED["state_message"]


def test_the_state_message_cannot_inject_terminal_controls(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "GET",
        f"{_BASE}/sales",
        json={**_NOT_PUBLISHED, "state_message": "stopped \x1b[2J\x1b]0;owned\x07 here"},
    )

    result = invoke(make_state(tmp_path, api), "table", "get", "bronze.sales", "--env", "dev")

    assert result.exit_code == 0, result.stderr
    assert "\x1b" not in result.stdout
    assert "\x07" not in result.stdout
    assert "stopped" in result.stdout


def _state_row(stdout: str) -> list[str] | None:
    """Find the `state` row of a key/value panel.

    Parameters
    ----------
    stdout
        What `table get` printed.

    Returns
    -------
    list of str or None
        The cells of the row after the `state` label, or `None` when there is no such row.
    """
    for line in stdout.splitlines():
        cells = [c.strip() for c in line.split("│")]
        if len(cells) > 2 and cells[1] == "state":
            return cells[2:-1]
    return None


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({}, ["published"]),
        ({"state": "not_published", "state_message": "stopped."}, ["not published"]),
        ({"state": "kept", "state_message": "Kept after a failed check."}, ["kept"]),
        ({"name": "v", "kind": "VIEW", "metadata_location": None, "sql": "SELECT 1"}, [""]),
        ({"name": "e", "kind": "EXTERNAL", "metadata_location": None}, [""]),
        ({"name": "a", "kind": "ARTIFACT", "metadata_location": None, "schema": []}, [""]),
    ],
    ids=["published", "not-published", "kept", "view", "external", "artifact"],
)
def test_table_get_has_a_state_row_for_every_kind(
    tmp_path: Path, body: dict[str, object], expected: list[str]
) -> None:
    api = FakeApi()
    name = str(body.get("name", "sales"))
    api.on("GET", f"{_BASE}/{name}", json={**_TABLE, **body})

    result = invoke(make_state(tmp_path, api), "table", "get", f"bronze.{name}", "--env", "dev")

    assert result.exit_code == 0, result.stderr
    assert _state_row(result.stdout) == expected


_HEALTH: dict[str, Any] = {
    "days": 30,
    "last_maintenance": {
        "run_id": "r1",
        "finished_at": "2026-10-11T12:00:00Z",
        "status": "complete",
        "snapshots_expired": 3,
        "files_deleted": 4,
        "bytes_deleted": 2048,
        "tags_removed": 1,
    },
    "checks": [
        {
            "name": "bronze.sales/unique_id",
            "check": "unique_id",
            "run_id": "r2",
            "outcome": "failed",
            "on_failure": "keep",
            "failing_rows": 2,
            "error": None,
            "rollback_outcome": None,
            "kept": True,
            "not_published": False,
            "finished_at": "2026-10-11T13:00:00Z",
        },
        {
            "name": "bronze.sales/positive",
            "check": "positive",
            "run_id": "r3",
            "outcome": "failed",
            "on_failure": "block",
            "failing_rows": 1,
            "error": None,
            "rollback_outcome": "unpublished",
            "kept": False,
            "not_published": True,
            "finished_at": "2026-10-11T13:00:00Z",
        },
        {
            "name": "bronze.sales/few_nulls",
            "check": "few_nulls",
            "run_id": "r4",
            "outcome": "passed",
            "on_failure": "warn",
            "failing_rows": 0,
            "error": None,
            "rollback_outcome": None,
            "kept": False,
            "not_published": False,
            "finished_at": "2026-10-11T13:00:00Z",
        },
    ],
    "readers": [{"principal": "ada", "read_count": 5, "last_read_at": "2026-10-11T14:00:00Z"}],
}


def _health(tmp_path: Path, body: dict[str, Any], *args: str) -> tuple[FakeApi, Result]:
    """Run `lakana table health bronze.sales` against a canned answer.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    body
        The route's JSON answer.
    *args
        More arguments for the command.

    Returns
    -------
    tuple
        The fake API and the result.
    """
    api = FakeApi()
    api.on("GET", f"{_BASE}/sales/health", json=body)
    result = invoke(
        make_state(tmp_path, api), "table", "health", "bronze.sales", "--env", "dev", *args
    )
    return api, result


def test_table_health_shows_maintenance_checks_and_readers(tmp_path: Path) -> None:
    api, result = _health(tmp_path, _HEALTH)

    assert result.exit_code == 0, result.stderr
    assert api.requests[-1].url.params.get("days") is None
    plain = _plain(result.stdout)
    assert "Last maintenance: 2026-10-11 12:00 UTC, complete" in plain
    assert "Expired 3 snapshots, deleted 4 files (2.0 KiB)" in plain
    assert "removed 1 tag" in plain
    assert "unique_id" in plain and "failed (kept)" in plain
    assert "positive" in plain and "not published" in plain
    assert "few_nulls" in plain and "passed" in plain
    assert "Readers, last 30 days" in plain
    assert "ada" in plain and "5" in plain


def test_table_health_marks_a_generated_check(tmp_path: Path) -> None:
    generated = {
        **_HEALTH["checks"][0],
        "name": "bronze.sales/key_unique",
        "check": "key_unique",
        "generated": True,
    }
    _, result = _health(tmp_path, {**_HEALTH, "checks": [generated, *_HEALTH["checks"][1:]]})

    assert result.exit_code == 0, result.stderr
    plain = _plain(result.stdout)
    assert "key_unique (generated)" in plain
    assert "positive (generated)" not in plain


def test_table_health_says_when_there_is_nothing_yet_and_when_readers_are_hidden(
    tmp_path: Path,
) -> None:
    _, result = _health(
        tmp_path, {"days": 30, "last_maintenance": None, "checks": [], "readers": None}
    )

    assert result.exit_code == 0, result.stderr
    plain = _plain(result.stdout)
    assert "Last maintenance: none yet" in plain
    assert "lakana table maintain bronze.sales --env dev" in plain
    assert "Checks: none recorded" in plain
    assert "Readers: not shown" in plain
    assert "ADMIN" in plain


def test_table_health_says_when_nobody_read_the_table(tmp_path: Path) -> None:
    _, result = _health(
        tmp_path, {"days": 7, "last_maintenance": None, "checks": [], "readers": []}, "--days", "7"
    )

    assert "Readers, last 7 days: none" in _plain(result.stdout)


def test_table_health_marks_a_stopped_maintenance_and_one_tag_or_none(tmp_path: Path) -> None:
    body = {
        **_HEALTH,
        "last_maintenance": {**_HEALTH["last_maintenance"], "status": "stopped", "tags_removed": 0},
    }
    _, result = _health(tmp_path, body)

    plain = _plain(result.stdout)
    assert "stopped" in plain
    assert "run it again to continue" in plain
    assert "tag" not in plain


def test_table_health_sends_days(tmp_path: Path) -> None:
    api, result = _health(tmp_path, _HEALTH, "--days", "7")

    assert result.exit_code == 0, result.stderr
    assert api.requests[-1].url.params.get("days") == "7"


def test_table_health_refuses_days_below_one(tmp_path: Path) -> None:
    api = FakeApi()
    result = invoke(
        make_state(tmp_path, api), "table", "health", "bronze.sales", "--days", "0", "--env", "dev"
    )

    assert result.exit_code == 2
    assert api.requests == []


def test_table_health_as_json(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", f"{_BASE}/sales/health", json=_HEALTH)

    result = invoke(
        make_state(tmp_path, api),
        "--output",
        "json",
        "table",
        "health",
        "bronze.sales",
        "--env",
        "dev",
    )

    body = json.loads(result.stdout)
    assert body["days"] == 30
    assert body["last_maintenance"]["status"] == "complete"
    assert [c["check"] for c in body["checks"]] == ["unique_id", "positive", "few_nulls"]
    assert body["readers"][0]["principal"] == "ada"


_FACT_BASE = "/api/v1/environments/dev/namespaces/gold/tables"
_MODEL: dict[str, Any] = {
    "kind": "fact",
    "grain": "One row per store and day.",
    "key": ["date", "store_id"],
    "key_none": False,
    "references": [
        {
            "id": "gold.fact_sales/ref_store_id",
            "table": "gold.fact_sales",
            "column": "store_id",
            "target": "gold.dim_store",
            "target_column": "store_id",
            "target_exists": True,
        }
    ],
    "hidden_references": 0,
    "checks": [
        {
            "name": "gold.fact_sales/key_unique",
            "check": "key_unique",
            "run_id": "3f2c9d1e-8b7a-4c55-9e0a-1b2c3d4e5f60",
            "outcome": "passed",
            "on_failure": "block",
            "failing_rows": 0,
            "error": None,
            "rollback_outcome": None,
            "kept": False,
            "not_published": False,
            "finished_at": "2026-10-29T10:00:00Z",
            "generated": True,
        }
    ],
}
_FACT = {**_TABLE, "name": "fact_sales", "namespace": "gold", "model": _MODEL}


def _get_fact(tmp_path: Path, model: dict[str, Any]) -> str:
    """Run `table get gold.fact_sales` against a fact with this model.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    model
        The `model` of the body.

    Returns
    -------
    str
        Standard output.
    """
    api = FakeApi()
    api.on("GET", f"{_FACT_BASE}/fact_sales", json={**_FACT, "model": model})
    result = invoke(
        make_state(tmp_path, api),
        "table",
        "get",
        "gold.fact_sales",
        "--env",
        "dev",
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stderr
    return result.stdout


def test_table_get_shows_the_model_block(tmp_path: Path) -> None:
    out = _get_fact(tmp_path, _MODEL)
    assert "Model: fact" in out
    assert "Grain: One row per store and day." in out
    assert "Key: date, store_id" in out
    assert "store_id → gold.dim_store.store_id (exists)" in out
    assert "key_unique (generated)" in out


def test_table_get_lists_the_measures_and_their_check(tmp_path: Path) -> None:
    measure = {"not_over": [], "unit": None, "currency": None, "label": None, "description": None}
    out = _get_fact(
        tmp_path,
        {
            **_MODEL,
            "measures": [
                {
                    **measure,
                    "id": "gold.fact_sales/measure_amount",
                    "column": "amount",
                    "aggregation": "sum",
                    "additivity": "additive",
                    "currency": "EUR",
                    "label": "Amount",
                    "description": "Not printed.",
                },
                {
                    **measure,
                    "id": "gold.fact_sales/measure_stock",
                    "column": "stock",
                    "aggregation": "sum",
                    "additivity": "semi",
                    "not_over": ["date"],
                    "unit": "count",
                },
            ],
            "checks": [
                *_MODEL["checks"],
                {**_MODEL["checks"][0], "name": "gold.fact_sales/measures", "check": "measures"},
            ],
        },
    )
    assert (
        "Measures:\n"
        "  amount: sum, additive, EUR — Amount\n"
        "  stock: sum, semi-additive (not over date), count\n"
    ) in out
    assert "Not printed." not in out
    assert "measures (generated)" in out
    assert "Measures:" not in _get_fact(tmp_path, _MODEL)


_AMOUNT = {
    "id": "gold.fact_sales/measure_amount",
    "column": "amount",
    "aggregation": "sum",
    "additivity": "additive",
    "not_over": [],
    "unit": None,
    "currency": "EUR",
    "label": None,
    "description": None,
}
"""A measure in the body of `table get`."""


def test_table_get_writes_no_trailing_comma_without_a_unit_or_currency(tmp_path: Path) -> None:
    out = _get_fact(
        tmp_path,
        {
            **_MODEL,
            "measures": [
                {**_AMOUNT, "aggregation": "count", "currency": None},
                {**_AMOUNT, "column": "rows", "currency": None, "label": "Rows"},
            ],
        },
    )
    assert "  amount: count, additive\n" in out
    assert "  rows: sum, additive — Rows\n" in out


def test_table_get_json_carries_the_measures(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "GET",
        f"{_FACT_BASE}/fact_sales",
        json={**_FACT, "model": {**_MODEL, "measures": [_AMOUNT]}},
    )
    result = invoke(
        make_state(tmp_path, api),
        "--output",
        "json",
        "table",
        "get",
        "gold.fact_sales",
        "--env",
        "dev",
    )
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["model"]["measures"] == [
        {**_AMOUNT, "label_language": None, "description_language": None}
    ]


def test_table_get_says_key_none_and_counts_hidden_references(tmp_path: Path) -> None:
    out = _get_fact(
        tmp_path,
        {
            **_MODEL,
            "key": [],
            "key_none": True,
            "references": [
                {**_MODEL["references"][0], "target_exists": False},
            ],
            "hidden_references": 2,
            "checks": [],
        },
    )
    assert "Key: none — grain not checked" in out
    assert "store_id → gold.dim_store.store_id (missing)" in out
    assert "2 more references to tables you cannot read" in out
    assert "key_unique" not in out


def test_table_get_without_a_kind_or_a_key(tmp_path: Path) -> None:
    out = _get_fact(
        tmp_path,
        {**_MODEL, "kind": None, "grain": None, "key": [], "references": [], "checks": []},
    )
    assert "Model: (no kind)" in out
    assert "Grain:" not in out
    assert "Key: (none declared)" in out


def test_table_get_of_a_table_without_a_model_shows_no_block(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", f"{_BASE}/sales", json=_TABLE)
    result = invoke(make_state(tmp_path, api), "table", "get", "bronze.sales", "--env", "dev")
    assert result.exit_code == 0, result.stderr
    assert "Model:" not in result.stdout


def test_table_get_json_carries_the_model(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", f"{_FACT_BASE}/fact_sales", json=_FACT)
    result = invoke(
        make_state(tmp_path, api),
        "--output",
        "json",
        "table",
        "get",
        "gold.fact_sales",
        "--env",
        "dev",
    )
    assert result.exit_code == 0, result.stderr
    model = json.loads(result.stdout)["model"]
    assert model["key"] == ["date", "store_id"]
    assert model["references"][0]["id"] == "gold.fact_sales/ref_store_id"


def test_table_list_passes_the_model_kind(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _FACT_BASE, json=[_FACT])
    result = invoke(
        make_state(tmp_path, api), "table", "list", "gold", "--model-kind", "fact", "--env", "dev"
    )
    assert result.exit_code == 0, result.stderr
    assert dict(api.requests[-1].url.params) == {"model_kind": "fact"}
    assert "fact_sales" in result.stdout


_REFERENCES = {
    "table": "gold.dim_store",
    "outgoing": [],
    "hidden_outgoing": 0,
    "incoming": [_MODEL["references"][0]],
    "hidden_incoming": 0,
}


def _references(tmp_path: Path, body: dict[str, Any], *options: str) -> Result:
    """Run `table references gold.dim_store` against a canned answer.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    body
        The answer.
    *options
        Options before the command.

    Returns
    -------
    Result
        The result.
    """
    api = FakeApi()
    api.on("GET", f"{_FACT_BASE}/dim_store/references", json=body)
    return invoke(
        make_state(tmp_path, api),
        *options,
        "table",
        "references",
        "gold.dim_store",
        "--env",
        "dev",
        env={"COLUMNS": "200"},
    )


def test_table_references_prints_both_directions(tmp_path: Path) -> None:
    result = _references(tmp_path, _REFERENCES)
    assert result.exit_code == 0, result.stderr
    out = result.stdout
    assert "References: none." in out
    assert "Referenced by:" in out
    assert "gold.fact_sales" in out
    assert "store_id" in out
    assert "cannot read" not in out


def test_table_references_counts_what_it_hides(tmp_path: Path) -> None:
    body = {
        **_REFERENCES,
        "outgoing": [{**_MODEL["references"][0], "table": "gold.dim_store"}],
        "hidden_outgoing": 1,
        "incoming": [],
        "hidden_incoming": 3,
    }
    result = _references(tmp_path, body)
    assert result.exit_code == 0, result.stderr
    out = result.stdout
    assert "References:" in out
    assert "Referenced by: none." in out
    assert "1 more reference to a table you cannot read" in out
    assert "3 more from tables you cannot read" in out


def test_table_references_json_keeps_the_object(tmp_path: Path) -> None:
    result = _references(tmp_path, _REFERENCES, "--output", "json")
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["incoming"][0]["id"] == "gold.fact_sales/ref_store_id"


def test_table_list_names_the_option_when_the_kind_is_refused(tmp_path: Path) -> None:
    """The server's refusal names its parameter `model_kind`; the CLI names its option."""
    api = FakeApi()
    api.on(
        "GET",
        _FACT_BASE,
        status=422,
        json={"detail": "model_kind must be one of fact, dimension, wide, staging; got 'cube'"},
    )
    result = invoke(
        make_state(tmp_path, api), "table", "list", "gold", "--model-kind", "cube", "--env", "dev"
    )
    assert result.exit_code == 8
    plain = _plain(result.stderr)
    assert "--model-kind must be one of fact, dimension, wide, staging; got 'cube'" in plain
    assert "model_kind" not in plain.replace("--model-kind", "")


def test_table_list_passes_any_other_refusal_on(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _FACT_BASE, status=422, json={"detail": "something else"})
    result = invoke(
        make_state(tmp_path, api), "table", "list", "gold", "--model-kind", "fact", "--env", "dev"
    )
    assert result.exit_code == 8
    assert "something else" in _plain(result.stderr)


def test_table_health_counts_the_checks_it_does_not_show(tmp_path: Path) -> None:
    _, result = _health(tmp_path, {**_HEALTH, "hidden_checks": 1})
    assert result.exit_code == 0, result.stderr
    assert "1 check of a reference to a table you cannot read is not shown." in _plain(
        result.stdout
    )
    _, plain = _health(tmp_path, _HEALTH)
    assert "cannot read" not in plain.stdout


def test_table_health_with_only_withheld_checks_says_so_and_not_none(tmp_path: Path) -> None:
    _, result = _health(tmp_path, {**_HEALTH, "checks": [], "hidden_checks": 2})
    plain = _plain(result.stdout)
    assert "2 checks of references to tables you cannot read are not shown." in plain
    assert "none recorded" not in plain


def test_table_get_shows_the_label_the_languages_and_the_coverage(tmp_path: Path) -> None:
    """`table get --lang` sends `lang` and shows the label, a text's language and the coverage."""
    api = FakeApi()
    api.on(
        "GET",
        f"{_FACT_BASE}/fact_sales",
        json={
            **_FACT,
            "label": "Ventes",
            "label_source": "project",
            "label_language": "fr",
            "description": "One row per sale.",
            "description_source": "project",
            "description_language": "en",
            "language": "fr",
            "main_language": "en",
            "translations": [{"language": "fr", "translated": 2, "total": 4}],
        },
    )
    result = invoke(
        make_state(tmp_path, api),
        "table",
        "get",
        "gold.fact_sales",
        "--env",
        "dev",
        "--lang",
        "fr",
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stderr
    assert api.requests[-1].url.params["lang"] == "fr"
    assert "Label: Ventes" in result.stdout
    assert "Description (from the project) [en]: One row per sale." in result.stdout
    assert "Translations: fr 2 of 4" in result.stdout


def test_table_get_marks_each_text_in_another_language(tmp_path: Path) -> None:
    """Column labels get their own column; a text not in the asked-for language says its own."""
    same = _get_fact(tmp_path, {**_MODEL, "grain_language": "en"})
    assert "Grain: One row per store and day." in same
    api = FakeApi()
    api.on(
        "GET",
        f"{_FACT_BASE}/fact_sales",
        json={
            **_FACT,
            "schema": [
                {**_COLUMNS[0], "label": "Identifiant", "label_language": "fr"},
                {**_COLUMNS[1], "description": "The region.", "description_language": "en"},
            ],
            "label": "Ventes",
            "label_source": "edited",
            "label_language": "fr",
            "language": "fr",
            "main_language": "en",
            "model": {
                **_MODEL,
                "grain_language": "en",
                "measures": [{**_AMOUNT, "label": "Amount", "label_language": "en"}],
            },
        },
    )
    result = invoke(
        make_state(tmp_path, api),
        "table",
        "get",
        "gold.fact_sales",
        "--env",
        "dev",
        "--lang",
        "fr",
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stderr
    assert "Label (edited): Ventes" in result.stdout
    assert "Grain [en]: One row per store and day." in result.stdout
    assert "  amount: sum, additive, EUR — Amount [en]\n" in result.stdout
    plain = _plain(result.stdout)
    assert "label" in plain
    assert "Identifiant " in plain and "Identifiant [" not in plain
    assert "The region. [en]" in plain


def test_table_list_passes_lang(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _FACT_BASE, json=[_FACT])
    result = invoke(
        make_state(tmp_path, api), "table", "list", "gold", "--lang", "de", "--env", "dev"
    )
    assert result.exit_code == 0, result.stderr
    assert dict(api.requests[-1].url.params) == {"lang": "de"}
