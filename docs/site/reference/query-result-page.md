# The result page

A query that read rows has a result you can fetch as one JSON page, with every cell cut to a size a
screen can hold. The web interface and `lakana table preview` read this page. The whole result, never
cut, is a file in Arrow, CSV or a spreadsheet (`format=arrow`, `csv` or `xlsx`; `lakana query
result`, `lakana sql --out`); see [The whole result as a file](#the-whole-result-as-a-file).

The page is `GET /api/v1/environments/{env}/queries/{query_id}/result` with the query parameters
`format=json`, `offset` and `limit`.

A preview is a query: `POST /api/v1/environments/{env}/namespaces/{path}/tables/{name}/preview`
queues it and answers `202` with the `query_id`. See [Preview a table](../how-to/preview-a-table.md).

The page of `silver.orders`, as `lakana --output json table preview silver.orders` prints it (the
CLI and SDK write `preview_table` for the REST key `preview`, which holds `{"table": ...}`):

<!-- source: docs/examples/preview-a-table/run.sh#json -->
```bash
lakana --output json table preview silver.orders
```

```json
{
  "columns": [
    {"name": "order_id", "type": "int64", "type_cut": false},
    {"name": "status", "type": "string", "type_cut": false}
  ],
  "rows": [["1", "open"], ["2", "shipped"], ["3", "cancelled"]],
  "row_count": 3,
  "next_offset": null,
  "duration_ms": 119,
  "masked_columns": [],
  "preview_table": "silver.orders",
  "preview_table_cut": false,
  "row_cut": [],
  "columns_cut": 0,
  "masked_columns_cut": 0
}
```

## Parameters

| Parameter | Meaning |
|---|---|
| `format` | `json` for this page; `arrow` (the default), `csv` or `xlsx` for the whole result. |
| `limit` | Rows on the page: 1 to 1,000, default 100. A preview's result holds at most 100 rows, and `limit` above 100 is a `422`. |
| `offset` | Rows to skip, at least 0. An offset at or past the end gives `rows: []`. |

`limit` and `offset` with `format=arrow`, `csv` or `xlsx` are a `422`: a file is always the whole
result. A `limit` below 1 and an `offset` below 0 are a `422` too. A query that has not succeeded, or is not a read,
answers `409` with its own message.

## Fields

| Field | Meaning |
|---|---|
| `columns` | The result's columns in order: `name`, `type` (the Arrow type name, at most 256 characters) and `type_cut`, `true` when the type name was cut. |
| `rows` | The rows of this page, each a list of cells in column order. |
| `row_count` | The rows the result has. |
| `next_offset` | The offset of the next page, or `null` at the end. Ask for it as `offset` and every row comes once, none twice. |
| `duration_ms` | How long the query ran, or `null` when not known. |
| `masked_columns` | The columns masked for the reader, each `{"table", "column"}`; `[]` when none; `null` for a run from before this was recorded and for a run without a stored result. |
| `preview` | `{"table": "gold.orders"}` for a preview, the table as the run's own query names it; `null` for any other query. |
| `preview_table_cut` | `true` when the table name was cut to fit the page. The SDK and `--output json` show it next to `preview_table`; the REST answer has it as `preview.cut`, present only when `true`. |
| `row_cut` | The positions on this page of the rows the page budget cut inside. A cell cut at its own 64 KiB limit shows as a `{"cut": true ...}` value in the cell and its row is not listed here. |
| `columns_cut` | How many columns were left out of the page, in two cases. When the column header filled the page, the columns that did not fit are left out and the page has no rows although `row_count` is above 0. When a row was cut inside the page, the cells of that row that did not fit are left out. Either way the JSON page holds no more of them; select fewer or narrower columns. |
| `masked_columns_cut` | How many further masked columns were left out because the page was full. |

`row_cut`, `columns_cut` and `masked_columns_cut` are always present, empty or `0` when nothing was
cut.

## How a value is written

| Type | Written as |
|---|---|
| 8, 16 and 32-bit integers, floats | a JSON number |
| 64-bit integers | a string, so that no digit is lost |
| decimals | a string |
| `NaN`, `Infinity`, `-Infinity` | the strings `"NaN"`, `"Infinity"`, `"-Infinity"` |
| booleans, null | `true`, `false`, `null` |
| text | a string |
| dates, times, timestamps (with or without a zone), durations | text in Arrow's form, such as `1970-01-01 00:16:40.000000123` or `10000-01-01 00:00:00.000` |
| uuid | the UUID as text |
| intervals | `{"months": 1, "days": 2, "nanoseconds": 3}` |
| binary | `{"binary": n}`, `n` being the size in bytes; also inside a list or a struct |
| struct | an object, in field order |
| list | a list |
| map | a list of `{"key": ..., "value": ...}` |
| union | its value |
| anything else | its text |

## Size limits

A page is built to a budget, so no result can fill a browser or a terminal:

- **A cell** is at most 64 KiB written. A string longer than 10,922 characters is cut to its start:
  `{"cut": true, "text": "..."}`. A cell that is too large for any other reason is `{"cut": true}`.
- **A list** shows its first 100 elements, followed by one `{"cut": true}`. The limit applies at
  every level of nesting.
- **The page** is at most 4 MiB. When the rows do not fit, the page holds the rows that do, and
  `next_offset` points at the first row left. A single row of the page may be cut inside by the
  page budget: it is listed in `row_cut`, and `columns_cut` counts its cells left out. A cell cut at
  its own 64 KiB limit is not listed in `row_cut`; its value is `{"cut": true ...}`.

## When the server is busy

Making a page costs the server memory, so it converts four pages at a time per server and one at a
time per person. A fifth, or a second of yours, is answered `503` with `Retry-After: 1` and the
type `ResultPageLimitError`. Ask again after that many seconds. `QueryRun.page` of the SDK and
`lakana table preview` do it for you, up to five times.

## The whole result as a file

`format=arrow` (the default), `format=csv` and `format=xlsx` answer the whole result, never a page
and never fewer rows than it has:

| Format | Media type | What it holds |
|---|---|---|
| `arrow` | `application/vnd.apache.arrow.file` | The stored Arrow IPC file, as the worker wrote it. |
| `csv` | `text/csv; charset=utf-8` | RFC 4180 with CRLF row ends, UTF-8 without a byte-order mark. Every text value and every header name is quoted. A value is written as it is: a text starting with `=` is not escaped, so open the file in a spreadsheet as data, or take `xlsx`. |
| `xlsx` | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | One sheet, the column names in its first row. Text is always text, never a formula. A number a spreadsheet would round (an integer beyond ±9,007,199,254,740,991, a decimal of more than 15 significant digits, a float its 16 digits do not hold) and NaN or infinity are text. A timestamp with a time zone is in UTC, shown with `UTC`. |

In both CSV and xlsx, a nested value (a list, struct, map or union) is its compact JSON text, a
binary value is `\x` and two hex digits per byte, as DuckDB prints a blob, and NULL is an empty field
or cell.

A spreadsheet holds at most 1,048,575 rows below its header row, 16,384 columns, and 32,767
characters in a cell. A result over any of these is refused with a `422` of the type `XlsxRefused`
before anything is sent, and the message names the command that downloads it as CSV instead.

A file is streamed, so there is no `Content-Length`. These headers say what it is:

| Header | Meaning |
|---|---|
| `Content-Disposition` | `attachment; filename="query-{query_id}.arrow"` (`.csv`, `.xlsx`). |
| `X-Result-Rows` | The rows of the result, as the worker recorded them. |
| `X-Result-Arrow-Bytes` | The size of the stored Arrow file. |

A query from before Lakana recorded these sizes has neither size header; its file is the same.

Every download of a file is recorded in the audit log as `QUERY_RESULT_EXPORTED`, with the query's
id, the format, the rows and the size. A JSON page is not. A pipeline task that reads its inputs
writes none.

Making a CSV or a spreadsheet costs the server memory, so it prepares two at a time per server and
one at a time per person, held until the file is sent. Another is answered `503` with
`Retry-After: 1` and the type `ResultFileLimitError`. The Arrow file is sent from storage as it is
and is never refused for this.
