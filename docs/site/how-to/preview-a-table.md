# Preview a table

After this guide you can look at the first rows of a table in one command or on the table's page in
the web interface, without writing SQL, and you know what a preview hides from you. You need an environment with a table that has data (see
[Getting started](../tutorials/getting-started.md)) and the right to run queries there, which the
`ANALYST` role gives.

A **preview** shows the first 100 rows of a table. It is for a quick look: what the columns hold,
whether the last load wrote anything. To filter, join or export rows, use `lakana sql`.

## Show the first rows

Name the table as `namespace.table`:

<!-- source: docs/examples/preview-a-table/run.sh#preview -->
```bash
lakana table preview silver.orders
```

The rows come first, on standard output. A line on standard error says how many rows were read, and
that you read them as yourself:

<!-- output: preview-a-table#preview -->
```text
┏━━━━━━━━━━┳━━━━━━━━━━━┓
┃ order_id ┃ status    ┃
┡━━━━━━━━━━╇━━━━━━━━━━━┩
│ 1        │ open      │
│ 2        │ shipped   │
│ 3        │ cancelled │
└──────────┴───────────┘
First 3 rows · read as you · 0.6 s
```

A table with no rows prints `No rows.`, and `Read as you.` on standard error. When the rows are
so wide that the page is full before row 100, the line says `First 12 of 100 rows` and how to read
the rest. A preview never shows more than 100 rows; to see further
rows, run a query with `lakana sql`. A cell that is very long is shown cut, with an ellipsis after
it, and binary data as its size, `<12 bytes>`. Press Ctrl-C to cancel a preview that is waiting.

## Read as you

A preview is a query that runs as you, so what you see is what a query of yours would see:

- a **row filter** that applies to you leaves out the rows it hides, and nothing says that it did;
- a **column mask** that applies to you replaces the column's values, and the line `Masked for you`
  on standard error names the column, as in `Masked for you: email` (see
  [Mask a column](mask-a-column.md)).

The line names a column of the table you asked for by its name. A column that a view takes from
another table is named with its table, such as `gold.customers.phone`. It never shows the rule, the
replacement or who set it up. Rows left out by a row filter are never counted or named: a person
sees only the rows they may read, as in any query.

When a preview needs to name more masked columns than fit in a page, the line ends with
`and N more`.

## Get the rows as JSON

`--output json` prints the page for a script. Its keys are the SDK's: the previewed table is
`preview_table`, where the REST answer has `preview`:

<!-- source: docs/examples/preview-a-table/run.sh#json -->
```bash
lakana --output json table preview silver.orders
```

The fields of the page are described in [The result page](../reference/query-result-page.md).

In the audit log, the `QUERY_COMPLETED` event of a preview has `detail.preview: true`; the event of
every other query does not have the field. An administrator can so tell a preview from a query
that reads the same table.

## When a preview is refused

- **Your role cannot run queries in the environment.** A viewer can read tables in the catalogue but
  runs no query, so the preview is refused with the next step: ask an administrator for the
  `ANALYST` role there.
- **The name is an artifact.** An artifact holds files, not rows. Download it with
  `lakana artifact download` instead; the message names the command.
- **The table changed while the preview waited.** The run fails with a message to try again.
- **A table's name contains a dot.** A query cannot name such a table yet. Only a raw REST call
  can reach this refusal, as a 409 that names the table; the SDK and the CLI split a name on its
  last dot, so there a dotted table name reads as a namespace and a table and you see the
  ordinary refusal for a table that does not exist or that you may not read. A namespace name with a dot cannot be reached at all: the REST path splits on
  every dot, so you see that same ordinary refusal. In both cases the next step is to rename the
  namespace or the table.

A namespace may be nested, as in `gold.finance.sales`: the table is the last part of the name.

## In the web interface

On a table's page, open the **Preview** tab. The preview runs only while the tab is open; the other
tabs never start one.

- **Read as you.** The tab runs the same preview as `lakana table preview`, as you: your row
  filters and column masks apply. The line over the rows says how many there are, that you read
  them as yourself, and how long the query took, such as "First 100 rows · read as you · 0.8 s".
- **Masked columns.** The line **Masked for you** names the columns a mask replaced for you, such
  as "Masked for you: email". A column that a view takes from another table is named with that
  table, such as "column email of gold.customers". It never shows the rule or the replacement.
- **While it waits.** The line says each step: checking that you may run queries, queuing the
  preview, **Queued** (after a second "Waiting for a worker…" while no worker has taken the
  query yet), **Running**, and reading the rows. **Refresh** runs the preview again; pressed
  while one waits, it cancels that one first.
- **Leaving the tab cancels it.** When you go to another tab or page, reload the page or close the
  browser tab while the preview is queued or running, the interface cancels the query, so no
  preview is left waiting for a worker. A
  preview that has finished is not touched.
- **The rows.** Each value is cut to one line. Move to a cell with Tab and the arrow keys, or click
  it, and the **Value** panel below the rows shows it whole. The rows are one stop for Tab: Tab
  enters them and Tab again leaves them, and the arrow keys move between the cells. A value with a
  character you could not see, such as U+202E (right-to-left override), shows that character as a
  small mark with its code, as in [Characters you could not see](see-where-a-table-is-defined.md#in-the-web-interface).
  Binary data reads as its size, such as "binary, 12 bytes", and a value too large to send is
  marked "(cut)", with a note over the rows.
- **It counts as a read and a query.** A preview is a query you ran: it is recorded in the audit
  log like any preview, and it counts as a read of the table in **Last read** on Overview.
- **A viewer sees one line.** Without the right to run queries in the environment, the tab says so
  in one line: "Preview runs a query as you, which needs the right to run queries in prod. Ask an
  administrator for the Analyst role there." No query is sent. An artifact has no Preview either:
  the tab says to download its files with `lakana artifact download`.

## Use it from Python

`client.tables.preview` queues the run and returns a handle; `page()` reads the rows once it has
finished:

```python
run = client.tables.preview("dev", "silver.orders")
run.wait()
page = run.page()
print(page.masked_columns)
```

`page()` asks again by itself when the server is busy converting other pages.
