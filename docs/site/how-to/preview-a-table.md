# Preview a table

After this guide you can look at the first rows of a table in one command, without writing SQL, and
you know what a preview hides from you. You need an environment with a table that has data (see
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

A table with no rows prints `No rows.`. A preview never shows more than 100 rows; to see further
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

`--output json` prints the page as the server answers it, for a script:

<!-- source: docs/examples/preview-a-table/run.sh#json -->
```bash
lakana --output json table preview silver.orders
```

The fields of the page are described in [The result page](../reference/query-result-page.md).

## When a preview is refused

- **Your role cannot run queries in the environment.** A viewer can read tables in the catalogue but
  runs no query, so the preview is refused with the next step: ask an administrator for the
  `ANALYST` role there.
- **The name is an artifact.** An artifact holds files, not rows. Download it with
  `lakana artifact download` instead; the message names the command.
- **The table changed while the preview waited.** The run fails with a message to try again.
- **The namespace's name contains a dot.** A query cannot name such a table yet; rename the
  namespace to preview it.

A namespace may be nested, as in `gold.finance.sales`: the table is the last part of the name.

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
