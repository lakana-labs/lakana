# The ingest helpers

`lakana.ingest` is a set of helpers for the code of an asset: they read what a connection
declares and give the asset a table. They run inside a task, so a helper reads only the
connections the asset lists in `@asset(connections=[...])`, with the task's own credential and
the network rules of [connections](connections.md#how-a-task-gets-its-credential). They need
the `lakana[assets]` extra, which a project that runs assets already depends on; it now includes
`fastexcel`, the spreadsheet reader.

## files

`lakana.ingest.files` reads flat files (CSV, Parquet, JSON, spreadsheets) from a `directory`
connection and returns a `pyarrow.Table`. A directory connection names an ingestion root the
worker has and a path below it; the files are mounted read-only.

```python
from lakana import asset
from lakana.ingest import files


@asset(connections=["exports"], write="append", watermark="_source_modified")
def orders(ctx):
    return files.read("exports", "orders/*.csv", since=ctx.watermark())
```

### files.read

`files.read(connection, pattern, *, ...)` reads every file that matches `pattern` and stacks the
rows into one table.

| Parameter | Default | Meaning |
|---|---|---|
| `connection` | required | A connection the asset declares, of kind `directory`. |
| `pattern` | required | A glob relative to the connection's path: `*`, `?` and `[...]` within a name, `**` for any number of directories (`orders/*.csv`, `**/*.parquet`). An empty pattern, `..`, an absolute path and more than 32 segments are refused. |
| `format` | from the extension | `csv`, `parquet`, `json`, `ndjson` (`jsonl` is the same), `xlsx`, `xlsm`, `xlsb`, `xls` or `ods`. |
| `sheet` | first sheet | A spreadsheet's sheet, by name. |
| `header_row` | `1` | The 1-based row of the column names in a CSV file or a sheet; `0` for none. |
| `schema` | none | Column name to Polars type, such as `{"id": pl.Int64}`. Those columns get that type; the others are inferred. |
| `since` | left out | See [Reading only new files](#reading-only-new-files). |
| `modified_from` | `"mtime"` | Where a file's time comes from; only with `since`. |
| `max_file_bytes` | 256 MiB | The largest file read. |
| `max_total_bytes` | 256 MiB | The most bytes read in all. |
| `max_files` | `10_000` | The most files read. |

Files that differ are combined: a column a file lacks is filled with nulls, and a column whose
types differ takes the type that fits both (Int64 and Float64 give Float64).

### Formats

| Extension | Reader |
|---|---|
| `.csv` | Polars CSV |
| `.parquet` | Polars Parquet |
| `.json` | Polars JSON (an array of objects) |
| `.ndjson`, `.jsonl` | Polars newline-delimited JSON |
| `.xlsx`, `.xlsm`, `.xlsb`, `.xls`, `.ods` | `fastexcel` (calamine) |

Any other extension needs `format=`. `sheet=` on a file without sheets, and `header_row=` other
than `1` on JSON or Parquet, are refused rather than ignored. `schema=` names must exist in each
file: a column it names that a file lacks fails, naming the file and the column.

### Reading only new files

- **Left out:** every file is read and the table has only the files' columns.
- **`since=None`** (the first run of an asset with `watermark="_source_modified"`): every file
  is read and the column `_source_modified` holds each row's file time (UTC).
- **`since=<datetime>`**, usually `ctx.watermark()`: only files modified strictly after it are
  read, and the column is added. A naive datetime is UTC. Any other value is refused.

A file modified exactly at the watermark is not read again. A source that writes new files
with an old time, or an export that never changes its date, is better read by name:
`modified_from="export-%Y-%m-%d.csv"` takes the time from a `strptime` pattern applied to the
file's name (the part after the last `/`; a date means midnight UTC). A name that does not fit
the pattern is skipped, with a line on the task's standard error. `modified_from` without
`since` is refused.

When no file is new, the table has zero rows. With `schema=` it has those columns, plus
`_source_modified` when `since` was passed; without `schema=` it has no columns.

### Links and unreadable directories

Symbolic links below the root are never followed. A link, a pipe, a socket, a device, or a
directory deeper than 32 levels that the pattern needs is skipped, and the task's log says so with a line such as
``files.read: `exports`: skipped 'b.csv' (a symbolic link, not a regular file, or too deep)``.

A directory the pattern needs that the worker cannot open or list fails the read, naming it. It
is never skipped silently: an incremental read would advance its watermark past files it never
saw.

### files.list

`files.list(connection, pattern, *, max_files=10_000)` returns the matching files without
reading them: a table with `name` (string, relative to the connection's path), `size` (int64
bytes) and `modified` (timestamp in UTC).

### Errors

| Message starts or contains | Cause | Next step |
|---|---|---|
| `files.read reads directory connections; ... is postgresql: use sql.read` | The connection is of another kind. | Use the helper the message names. |
| `... names ingestion root ..., which this worker does not have` | The worker has no such root. | Set the root as the message says, or run the asset on a worker that has it. |
| `cannot tell the format of ...; pass format=` | The extension is none of the formats. | Pass `format=`. |
| `... cannot be listed ...; give this worker read access to it or narrow the pattern` | A directory the pattern needs is unreadable. | Give the worker read access, or narrow the pattern. |
| `... has no column ..., which schema= names` | A file lacks a column `schema=` names. | Fix the name in `schema=`, or the file. |
| `... column ... is ..., which does not fit ... in the files before it` | A column has types that do not fit across files. | Pass `schema=` with one type for it, or narrow the pattern. |
| `... check the file, or pass schema= with the column's type` | A file has a value its column cannot hold. The message names file, column, value and problem. | Fix the file, or pass `schema=` with a type that holds it. |
| `the pattern ... matches more than max_files (...)` | Too many files match. | Narrow the pattern, pass `since=`, or raise `max_files`. |
| `... is N bytes, more than max_file_bytes (...)` | One file is too large. | Split the file, or raise `max_file_bytes`. |
| `the files matching ... hold more than max_total_bytes (...)` | The files together are too large. | Narrow the pattern or pass `since=` to read fewer files, or raise `max_total_bytes`. |
| `does not say when ... was modified; pass modified_from=` | The source gives no file time. | Pass `modified_from=` with the date pattern of the name. |
| `modified_from= applies only with since=` | `modified_from` without `since`. | Pass `since=`, or drop `modified_from`. |
| `since= takes a datetime or None` | `since` has another type. | Pass `ctx.watermark()` of an asset with `watermark="_source_modified"`. |
