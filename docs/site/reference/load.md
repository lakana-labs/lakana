# Loading a local file

`lakana load` writes a file from your machine into a table of the bronze layer, once. It is for
exploring, testing and teaching: a first look at a file, a table for a test, a lesson. A table
you clean up, and anything that should run again, belongs in an asset; a
[Python asset](python-assets.md) reads files with the [files helper](ingest.md#files).

The tutorial [Getting started](../tutorials/getting-started.md#load-a-file) shows a first load.

## Command

| Command | What it does |
|---|---|
| `lakana load FILE TABLE` | Reads `FILE`, shows its columns and row count, asks, and writes the rows into `TABLE`. A bare name means `bronze.<name>`. |

| Option | Meaning |
|---|---|
| `--env` | The environment (default: the profile's). |
| `--write` | `replace` (the default) swaps the table's rows; `append` adds to them. A first load creates the table either way. |
| `--format` | The file's format, when its extension does not say: `csv`, `parquet`, `arrow`, `json`, `ndjson`, `xlsx`, `xlsm`, `xlsb`, `xls` or `ods`. |
| `--sheet` | A spreadsheet's sheet (default: the first). Spreadsheets only. |
| `--header-row` | The row of the column names in a CSV file or a sheet, counted from 1 (the default); `0` for none, and the columns are named `column_1`, `column_2`, and so on. |
| `--yes` | Do not ask. Required without a terminal, and in a protected environment. |
| `--break-glass`, `--reason` | Write to an environment whose writes are locked, as `lakana table append` does. |

What the command does, in order:

1. It refuses a protected environment without `--yes`, before it reads the file: recurring
   loads into production belong in an asset.
2. It asks the server how large one upload may be (`LAKANA_APPEND__MAX_BYTES`, 256 MiB by
   default) and refuses a larger file before it reads it, and again before it sends the rows when
   they take more room than the file (a compressed Parquet file can).
3. It reads the file and checks with the server, without sending a row, that the load would
   succeed: the layer, your rights, the namespace, and the columns of a table that exists.
4. It shows the columns and the row count and asks.
5. It writes the rows, but only while the table is still as it was when it checked; if someone
   changed it in between, the load fails and nothing is written.

## Formats

| Format | Extensions | Read with |
|---|---|---|
| CSV | `.csv` | pyarrow, with each column's type inferred, as `lakana table append` reads it |
| Parquet | `.parquet`, `.pq` | pyarrow |
| Arrow | `.arrow`, `.ipc`, `.feather` (file or stream) | pyarrow |
| Newline-delimited JSON | `.ndjson`, `.jsonl` | pyarrow; the text must be UTF-8 |
| A JSON array | `.json` | the files helper's reader; needs the `assets` extra |
| Spreadsheets | `.xlsx`, `.xlsm`, `.xlsb`, `.xls`, `.ods` | the files helper's reader; needs the `assets` extra |

The `server` extra reads the first four. A JSON array or a spreadsheet needs the `assets` extra
of the `lakana` package as well; without it, the command says what to install.

A column whose type a table cannot store (for example one that holds only empty values) is
refused, naming the column; convert it before loading.

## SDK

```python
import pyarrow as pa

import lakana

client = lakana.Client()
rows = pa.table({"id": [1, 2], "name": ["Ada", "Bea"]})
result = client.tables.load("dev", "bronze.customers", rows, expected_snapshot_id="none")
print(result.rows, result.created, result.snapshot_id)
```

`client.tables.load(environment, "bronze.table", data, *, write="replace",
expected_snapshot_id=None, break_glass_reason=None, dry_run=False)` returns a `LoadResult` with
`snapshot_id` (`None` for a dry run), `rows`, `created` and `dry_run`.

| Argument | Meaning |
|---|---|
| `data` | A `pyarrow.Table`, a `RecordBatch`, or anything `pyarrow.table()` accepts. For a dry run, an empty table of the columns to check. |
| `write` | `"replace"` or `"append"`. A first load creates the table either way. |
| `expected_snapshot_id` | The table's current snapshot as you read it (`Table.snapshot_id`), or `"none"` for a table that does not exist yet. The load commits only while that still holds, and raises `Conflict` otherwise; it is never sent again. |
| `break_glass_reason` | Write to a protected environment outside a run: 10 to 500 characters, needs `MANAGE_ENVIRONMENT`, audited. |
| `dry_run` | Run every check, the columns against an existing table included, and write nothing. |

`client.tables.upload_limit()` returns the server's `LAKANA_APPEND__MAX_BYTES`, or `None` for a
server that does not say.

## REST route

`POST /api/v1/environments/{env}/namespaces/{path}/tables/{name}/load`, with the rows as an
uncompressed Arrow IPC stream in the body (`Content-Type: application/vnd.apache.arrow.stream`).
The route refuses a table outside bronze before it reads the body, for every client alike.

| Query parameter | Meaning |
|---|---|
| `write` | `replace` (the default) or `append`. |
| `expected_snapshot_id` | As in the SDK. |
| `break_glass_reason` | As in the SDK. |
| `dry_run` | `true` to check only. |

The answer is `{"snapshot_id": ..., "rows": ..., "created": ..., "dry_run": ...}`. The body has
the limits of an append: `LAKANA_APPEND__MAX_BYTES`, `LAKANA_APPEND__MAX_ARRAYS`,
`LAKANA_APPEND__MAX_CONCURRENT` and `LAKANA_APPEND__READ_TIMEOUT_SECONDS`
([settings](settings.md)). `GET /api/v1/info` reports the first as `upload_max_bytes`.

## Who may load

`WRITE_TABLE` on the table; a load that creates the table needs `CREATE_TABLE` as well. A
protected environment needs `--yes`, and an environment whose writes are locked needs
`--break-glass --reason` with `MANAGE_ENVIRONMENT`.

## Errors

| Status or exit | When | Message |
|---|---|---|
| `422`, exit 8 | a table outside bronze | `` `lakana load` loads into bronze only: load into `bronze.customers` and transform it into `silver.customers` with an asset. `` |
| `404`, exit 6 | the namespace does not exist | ``Namespace `bronze.raw` does not exist in 'dev': create it with `lakana ns create bronze.raw --env dev`, then load again.`` |
| `403`, exit 5 | no `WRITE_TABLE`, or no `CREATE_TABLE` for a first load | the permission |
| `409`, exit 7 | the table changed since the command looked at it | ``bronze.customers changed during the write; retry.`` |
| `422`, exit 8 | the file's columns do not fit the existing table | the column that differs, as for an append |
| exit 2 | a protected environment without `--yes` | `` `prod` is protected: recurring loads belong in an asset; pass --yes to load once (and --break-glass --reason REASON if writes are locked) `` |
| exit 2 | a file larger than one upload may be | `big.parquet (300000000 bytes) is larger than one upload may be (256 MiB); load it with a Python asset and a directory or object-store connection` |
| exit 2 | a JSON array or spreadsheet without the `assets` extra | `reading .xlsx needs the assets extra: pip install 'lakana[assets]'` |
| exit 2 | you answer no | `Aborted.` |
| exit 8 | the file is missing, cannot be read in its format, is not UTF-8, nests deeper than the helpers read, or has a column a table cannot store | the file and the cause |

## Audit

A load writes through the same code as an append or a replace: a first load and a replace record
`TABLE_WRITE`, an append `TABLE_APPENDED`, with the principal, the rows and the snapshot.
