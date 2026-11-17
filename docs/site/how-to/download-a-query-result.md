# Download a query result

After this guide you can save the whole result of a query as a CSV file or a spreadsheet, as a
Parquet or Arrow file, or from Python, and you know what a spreadsheet cannot hold. You need an
environment where you may run queries (the `ANALYST` role gives that right); see
[Getting started](../tutorials/getting-started.md).

A download is always the **whole result**, never the page you see on a screen. The server makes the
CSV and the spreadsheet from the stored result, writes the whole file on its own disk first, and
then sends it in pieces, so a large result does not have to fit in your computer's memory.

## A whole result as CSV or a spreadsheet

`lakana sql` saves the result of a query when you give `--out`. The format comes from the file's
extension (`.csv` or `.xlsx`), or from `--format`:

<!-- source: docs/examples/download-a-query-result/run.sh#sql-out -->
```bash
lakana sql "SELECT 1 AS n, 'a,b' AS s" --env dev --out one.xlsx
```

To save the result of a query you already ran, give its id to `lakana query result`:

<!-- source: docs/examples/download-a-query-result/run.sh#result-csv -->
```bash
id=$(lakana --output json query submit "SELECT 1 AS n, 'a,b' AS s" --env dev | python3 -c 'import json, sys; print(json.load(sys.stdin)["id"])')
lakana query wait "$id" --env dev
lakana query result "$id" --env dev --format csv --out one.csv
```

`--format` is one of `parquet`, `csv`, `arrow` and `xlsx`. Without `--out`:

- `lakana query result ID --format csv` writes the CSV to standard output, so you can pipe it into
  another program;
- `--format xlsx` is refused with exit status 2 and the sentence `--format xlsx writes a binary file;
  add --out FILE.xlsx`, since a spreadsheet is not text a terminal can show.

The file is written beside its final name as `one.csv.part` and renamed when it is complete. A
download that breaks half way leaves no `.part` file, and a file that was there before stays as it
was.

## Parquet and Arrow

`--format parquet` and `--format arrow` (or the extension `.parquet`, `.arrow`) are written by the
CLI itself, from the result it fetches in Arrow. They are not made by the server, and
`lakana sql … --out x.parquet` works as before.

## From Python

`QueryRun.download` saves the server's file at a path:

```python
run = client.queries.submit("SELECT 1 AS n, 'a,b' AS s", environment="dev")
run.wait()
run.download("orders.xlsx", format="xlsx")  # returns the bytes written
```

`format` is `"csv"` or `"xlsx"` (the type `lakana.ResultFileFormat`). The file is written as
`orders.xlsx.part` and moved onto the name when it is complete; a failure removes the part file.
`run.write_file(handle, format="csv")` writes into a file you opened for binary writing instead.
The SDK reads no spreadsheet and needs no library for one: it only moves the bytes the server sends.

A refusal (see the next section) is raised as `lakana.ValidationError` with the server's sentence.
If the server is busy preparing other files, the answer is a `lakana.ServerError` with status 503;
ask again a moment later.

## What a spreadsheet cannot hold

A spreadsheet cell and a spreadsheet sheet have limits that a table does not. Lakana never writes a
value that the spreadsheet would change without saying so, and never cuts one:

- **Numbers a spreadsheet would round are written as text.** A whole number beyond
  ±9,007,199,254,740,991 (2⁵³−1), a decimal of more than 15 significant digits, and a
  floating-point number that needs 17 significant digits to be read back exactly are text in the
  cell. So are NaN and infinity. Text keeps every digit; to calculate with it, convert it in the
  spreadsheet or take the CSV. A 32-bit floating-point number (`FLOAT`) is the number its shortest
  text names, as in the CSV: `0.1`, not `0.10000000149011612`.
- **Dates beyond the calendar are text.** DuckDB's `infinity` and `-infinity`, a year after 9999
  and a year before 1 have no date in a spreadsheet. A date or timestamp column that holds one is
  written, in the CSV and the spreadsheet alike, as the text DuckDB prints: `infinity`,
  `294247-01-10 04:00:54.775806`, `0006-03-04 (BC)`, a time zone's values in UTC with `+00`.
- **Times are shown to the second.** A timestamp is a real date and time in the cell, shown as
  `yyyy-mm-dd hh:mm:ss`. The part of a second below that stays in the cell's value, but the cell
  does not show it. A timestamp with a time zone is in UTC and shows `UTC` after the time. A time of
  day without a date is text.
- **Text is always text.** A value that starts with `=` is not a formula in the spreadsheet.
- **Some results are refused.** A spreadsheet holds at most 1,048,575 rows below its header row,
  16,384 columns, and 32,767 characters in a cell. A result over one of these limits is refused
  before the first byte is sent, with a sentence that names the limit and ends with the command that
  saves a CSV instead:

  > This result has 2,000,000 rows; a spreadsheet holds at most 1,048,575 below its header row.
  > Download it as CSV instead: lakana query result 7f3c… --env dev --format csv --out query-7f3c….csv

  The `lakana query result` command in the sentence is complete: copy it. A result is refused the
  same way for a text of more than 32,767 characters in one cell, naming the column.
- **A column no file can hold is refused.** For a column of a type that neither CSV nor a
  spreadsheet can write, the answer is a refusal that offers the result as Arrow
  (`--format arrow`) instead.
- **A nested value is its JSON text.** A list, a struct, a map and a union become compact JSON in
  one cell. Binary data is `\x` and two hex digits per byte. NULL is an empty cell.

A server never truncates a result to make it fit: a limit is either met or the download is refused
with a sentence. A result is also capped at 1,000,000 rows when it is made, and a query that
reads more fails with its own message.

You cannot ask for a page and a file at once: `limit` and `offset` with a file format are a
refusal. A file is the whole result.

## CSV details

The CSV follows RFC 4180 and is UTF-8 without a byte-order mark:

- rows end with CRLF, and the first row holds the column names;
- every text value and every column name is quoted, a number is not, and a quote inside a value is
  doubled;
- a value that starts with `=`, `+`, `-` or `@` is written **as it is**. Lakana does not change it
  into text a spreadsheet would not run. If you open the file in a spreadsheet program, import it as
  data (not by double-clicking), or take the `xlsx` file, which never makes a formula of text;
- a nested value, binary data and NULL are written as in the spreadsheet.

Numbers and times are written exactly, with every digit; the CSV has none of the spreadsheet's
limits on rows and columns.

## Every download is audited

Each download of a whole result as Arrow, CSV or a spreadsheet writes one `QUERY_RESULT_EXPORTED`
event to the audit log, before the first byte is sent. It names the environment, the query, the
format, the number of rows and the size of the stored result. A refused download writes none. The
JSON pages that the web interface shows are not downloads and write none. A pipeline task that reads
its inputs as Arrow through its own token is not audited either, since that is how a task runs, not
an export. The same token downloading CSV or a spreadsheet is audited like anyone else.

## Find your queries again

To find a query again, list the queries of every environment you may query, and add
`--origin written` for the queries typed by hand (a preview, a check and a task are other
origins):

<!-- source: docs/examples/download-a-query-result/run.sh#list -->
```bash
lakana query list --all-environments --origin written --since 2026-01-01 --all
```

`--since` takes a date, or a date and time; without a zone it is read in your computer's local
time. It lists the queries that ended from then on, of any age, and implies `--all`; without it,
`--all` lists those of the last 24 hours. The table has an `environment` column, and its first
column is the id that `lakana query result` takes. Add `--env NAME` instead of
`--all-environments` to see one environment.

You see your own queries, except where you hold `MANAGE_ENVIRONMENT`: there the list holds
everyone's queries, and a page that shows someone else's is recorded in the audit log
(`ACTIVITY_VIEWED`). Add `--submitter NAME`, with your own name, to list only yours.

## In the web interface

Part 4 of the web interface will add a Queries screen that offers the same download of a whole
result as CSV or a spreadsheet.
