# Asset front-matter

The `/* lakana … */` block that starts an asset file: every key it takes and what a deploy
does with it. The layout of the files is in [Project layout](project-layout.md).

An optional `/* lakana … */` block, first in the file:

<!-- not tested: SQL is not parsed by the block checker; a test in `scripts/tests/test_docs_site.py` parses this header with the front-matter reader -->
```sql
/* lakana
description: Cleaned hourly weather observations, nulls dropped.
owner: data-eng
tags: [weather, hourly]
write: append                 # replace | append | merge, see "Write modes" below
watermark: ts                 # append needs it; merge may have it
evolve_schema: false          # append only
retries: 3                    # run policy, see lakana.yaml: "Retries and timeouts"
retry_delay: 1m
timeout: 5m
columns:
  station_id: WMO station identifier.
*/
SELECT station_id, ts, temp_c FROM bronze.weather WHERE temp_c IS NOT NULL
```

`description` and `columns` describe the table and its columns. A deploy publishes them as the
project's text, and a description set with `lakana table describe` wins over them and survives
every deploy; see [Describe a table](../how-to/describe-a-table.md).

An asset may call only the generator table functions `range`, `generate_series` and `unnest`,
which read no data and are not inputs; every other table function (`read_parquet`, `iceberg_scan`,
`glob`, anything unknown) fails the deploy with an issue at `path:line` (the line where the SQL body starts) — register the
data as a table and reference it.

An asset is one `SELECT` (also with `WITH`, or a set operation such as `UNION`), or a `VALUES`
list such as `VALUES (1, 'a'), (2, 'b')`. A statement that changes data or the session (`INSERT`, `UPDATE`,
`DELETE`, `MERGE`, `COPY`, `CREATE`, `DROP`, `ATTACH`, `SET` and the like) fails the deploy
wherever it sits, also inside a CTE or a subquery, with ``A query reads data and cannot change
it: `DELETE` is not allowed, also not inside a CTE or subquery.`` A query may not call
`getvariable` either, and a `PIVOT` must list the values of its `ON` column
(`ON region IN ('eu', 'us')`).

Some keys exist only as arguments of a Python asset's `@asset(...)` decorator: `output`,
`keep_versions`, `decimals` and `connections`. In a SQL block each fails the deploy with
``` `connections` applies only to a Python asset, not to a SQL file; declare the asset in a `.py` file with `@asset(connections=["crm-db"])`, or remove `connections` from this block ```
(the key and the example change with the key). See [Python assets](python-assets.md).

Unknown keys are errors. YAML anchors, aliases, `<<` merge keys and duplicate keys are refused
(the error names the file and line); `evolve_schema` and `column_docs` must be real booleans, not
quoted strings or numbers. `evolve_schema` that changes a column's type (other than widening it)
makes the column a new one: the rows written before read as NULL in it, and the row filters and
column masks that use it must be created again, as reads under them are refused until then
([When a table's columns change](../explanation/when-columns-change.md)). Each block is at most 64 KiB, and it ends at the first `*/`, even
inside a string or scalar (when the leftover text then fails to parse, the error says so).
`materialize: table` (the default) makes the asset a table, written as its `write:` says (below);
`materialize: view` makes it a view, which takes no `write:`.

## Write modes

`write:` says what each run does to the asset's table. The same three modes, with the same rules,
are the `write=`, `key=` and `watermark=` arguments of a Python asset's `@asset(...)`, with one
difference: the platform filters a SQL asset's rows on the watermark, but not the rows a Python
function returns. A Python asset that appends must return only the rows past `ctx.watermark()`
(and `ctx.watermark(default=...)` gives the first run its start), see
[The asset's context](python-assets.md#the-assets-context).

| `write:` | What a run does | Needs |
|---|---|---|
| `replace` (the default) | Replaces every row of the table with what the `SELECT` returns. | neither `key:` nor `watermark:` (it takes neither) |
| `append` | The first run creates the table with every row; each later run adds only the rows whose `watermark` column is above the highest value already in the table. | `watermark:` |
| `merge` | Matches the rows on `key:`: a row whose key the table has replaces that row, any other row is added. Nothing is deleted. With `watermark:`, a run reads only the rows above the highest value in the table. | `key:` (one or more columns); `watermark:` is optional |

Every first run creates the table, so the run's principal needs the right to create tables in the
asset's namespace (`CREATE_TABLE`), not only to write them. Without it the first run fails with a
message that names the right and the table; the next run works once you grant it, or create the
table yourself. A first run that returns no rows creates the table empty.

### `append`

<!-- source: docs/examples/write-modes/project/assets/silver/customer_changes.sql -->
```sql
/* lakana
description: Every change of a customer, oldest first; each run adds the new ones.
write: append
watermark: updated_at
*/
SELECT id, name, updated_at FROM bronze.customer_updates
```

The watermark must be a column of the result, of an integer, decimal, date or timestamp type; a
first run with another column fails before it creates anything. A row that arrives late, with a
value at or below the watermark, is not added. After Grace's name changed and Edsger arrived,
the second run added both rows and kept the first ones:

<!-- output: write-modes#appended -->
```text
┏━━━━┳━━━━━━━━━━━━━━┓
┃ id ┃ name         ┃
┡━━━━╇━━━━━━━━━━━━━━┩
│ 1  │ Ada          │
│ 2  │ Grace        │
│ 2  │ Grace Hopper │
│ 3  │ Edsger       │
└────┴──────────────┘
4 rows
```

`evolve_schema: true` lets an append change the table's schema when the result's columns change;
it works with `write: append` only. A column the result no longer has is removed from the table,
and every query, view and downstream asset that names it fails: see
[When a table's columns change](../explanation/when-columns-change.md). A Python asset takes
`evolve_schema=True` the other way round, with `write="replace"` only, and accepts
`evolve_schema=False` with every `write` (see
[When the output changes shape](python-assets.md#when-the-output-changes-shape)).

### `merge`

<!-- source: docs/examples/write-modes/project/assets/silver/customers.sql -->
```sql
/* lakana
description: One row per customer, kept up to date.
write: merge
key: [id]
*/
SELECT id, name, updated_at FROM bronze.customer_updates
```

The same two runs leave one row per customer:

<!-- output: write-modes#merged -->
```text
┏━━━━┳━━━━━━━━━━━━━━┓
┃ id ┃ name         ┃
┡━━━━╇━━━━━━━━━━━━━━┩
│ 1  │ Ada          │
│ 2  │ Grace Hopper │
│ 3  │ Edsger       │
└────┴──────────────┘
3 rows
```

Each run commits one new snapshot, so a reader sees the table before or after the merge, never
half of it, and running the same merge again changes nothing. Every run checks the rows first,
the first one too: a run whose result has a null key, or a key in more than one row, fails and
writes nothing (``key `id` repeats in 2 rows of the asset's result; keep one row per key in its
SELECT, …``); make the `SELECT` return one row per key, for example with
`QUALIFY row_number() OVER (PARTITION BY id ORDER BY updated_at DESC) = 1`.
A key of several columns (`key: [id, region]`) takes at most 5,000 rows per run; on one column
there is no such cap. A merge holds its whole result in the worker's memory while it compares it
with the table (at most `LAKANA_QUERY_LIMITS__MAX_WRITE_ROWS` rows), so a large one counts against
the worker's memory limit (`LAKANA_WORKER__MEMORY_LIMIT`). A merge compares the rows with the table's, so the run's principal needs
`READ_TABLE` on the table as well as the right to write it, and a merge is refused while a row
filter or column mask on the table applies to that principal. The details, the counts and the
audit event are in [Merging rows into a table](merge.md).

### `replace`

`replace` needs nothing more: leave `write:` out, or write `write: replace`.

### Rules

Each of these fails the deploy with an issue at the block's line:

| Issue | Cause |
|---|---|
| ``write: must be one of replace, append, merge; got 'upsert'`` | Another value. |
| ``` `write: append` needs `watermark:` — the column whose largest value the next run starts after ``` | `append` without `watermark:`. |
| ``` `write: merge` needs `key:` — the columns that identify a row ``` | `merge` without `key:`. |
| ``` `write: replace` takes no `watermark:`; to add only new rows use `write: append` ``` | `replace` with `watermark:`. |
| ``key column `id` is listed twice`` | A repeated key column. |
| ``` `materialize: view` cannot take `write:` ``` | A view with `write:`. |
| ``` `evolve_schema` works with `write: append` only ``` | `evolve_schema` on any other asset. |
| ``use one: `write: append` with `watermark:` replaces `materialize: incremental` `` | Both spellings in one block. |

### The older spelling

`materialize: incremental` with `incremental_key:` is the older spelling of `write: append` with
`watermark:`; both work, and a deploy prints a hint:

<!-- output: write-modes#hint -->
```text
Queued dry run 3dc2c4aa-8f40-4985-9106-1d92966b24eb
Dry run of analytics in dev at abcc6826166c7e41e9bbed945192487e8ef38035
┏━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┓
┃ change ┃ table             ┃
┡━━━━━━━━╇━━━━━━━━━━━━━━━━━━━┩
│ added  │ silver.orders_log │
└────────┴───────────────────┘
hint: silver.orders_log: `materialize: incremental` is now written `write: append` with `watermark: order_id`; both work
```

A project version that uses `write: append` or `merge` needs workers of this release or later: a
worker of an earlier one refuses its manifest instead of running the asset as a replace.
