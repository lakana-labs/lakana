# Merging rows into a table

A merge sends rows with a key: a row whose key the table already has replaces that row, and any
other row is added. Nothing is deleted. Use it to keep a table in step with a source that sends
changed rows again, such as a customer list exported each night with its changes. To add rows
without matching, use an append; to swap every row, use a replace.

This page is about merging rows you send. An asset that merges its own result on every run says
`write: merge` with `key:` in its header; see
[Write modes](asset-front-matter.md#write-modes).

## What a merge does

- **One snapshot.** The merge commits one new snapshot of the table. A reader sees the table
  before the merge or after it, never half of it, and the table's history has no snapshot in
  between to go back to.
- **The key.** One column, or several (`key=["id", "region"]`). Two rows match when every key
  column is equal.
- **Counts.** The answer says how many rows were inserted and how many were updated. A row whose
  key matches and whose values are all equal to the table's is neither: it changes nothing. A
  merge that changes nothing commits nothing and returns no snapshot.
- **The first merge.** With `expected_snapshot_id="none"`, a merge into a table that does not
  exist yet creates it from the rows, after the same checks of the key.
- **Sending it twice.** A merge sent again changes nothing the second time, so retrying one
  whose answer you did not get is safe. (With `expected_snapshot_id`, the second one is refused
  as a conflict instead, since the first moved the table on.)

## Command

| Command | What it does |
|---|---|
| `lakana table merge NAME FILE --key COLUMN` | Merges a Parquet, CSV or Arrow file into the existing table `NAME`. Repeat `--key` for a key of several columns. |

The file is fitted to the table's columns first, as for `lakana table append`: a value that does
not fit is refused before anything is sent. It takes `--env`, `--format`, `--source`,
`--break-glass` and `--reason` as `lakana table append` does, and prints the counts:
`Merged into dev/bronze.customers: 1 rows inserted, 2 updated`. The command does not create a
table; create it first with `lakana table create`, or merge from the SDK with
`expected_snapshot_id="none"`.

## SDK

```python
import pyarrow as pa

import lakana

client = lakana.Client()
rows = pa.table({"id": [2, 3], "name": ["Bea", "Cem"]})
result = client.tables.merge("dev", "bronze.customers", rows, key="id")
print(result.rows_inserted, result.rows_updated, result.snapshot_id)
```

`client.tables.merge(environment, "namespace.table", data, *, key, sources=None,
source_versions=None, break_glass_reason=None, expected_snapshot_id=None)` returns a
`MergeResult` with `snapshot_id` (`None` when nothing changed), `rows_inserted` and
`rows_updated`.

| Argument | Meaning |
|---|---|
| `data` | A `pyarrow.Table`, a `RecordBatch`, a Polars or pandas frame, or anything `pyarrow.table()` accepts. Its columns must be the table's columns. |
| `key` | The column, or a list of the columns, that identify a row. Required. |
| `sources` | The tables the rows came from, recorded as lineage. Each must exist and be readable by you. |
| `source_versions` | The version of each source that is an artifact, when you read a specific one. |
| `break_glass_reason` | Write to a protected environment outside a run: 10 to 500 characters, needs `MANAGE_ENVIRONMENT`, audited. |
| `expected_snapshot_id` | The table's current snapshot as you read it, or `"none"` for "no snapshot yet". The merge commits only while that still holds; otherwise it raises `Conflict` at once. `"none"` also creates a missing table. |

Without `expected_snapshot_id`, a merge that loses a race with another writer is sent once more,
as an append is.

## REST route

`POST /api/v1/environments/{env}/namespaces/{path}/tables/{name}/merge`, with the rows as an
uncompressed Arrow IPC stream in the body (`Content-Type: application/vnd.apache.arrow.stream`).

| Query parameter | Meaning |
|---|---|
| `key` | A key column; required. Repeat it for a key of several columns (`key=id&key=region`). |
| `expected_snapshot_id` | As in the SDK. At most once. |
| `source` | A table the rows came from; repeatable. |
| `source_version` | `<version>:<namespace.name>` for an artifact source; repeatable. |
| `break_glass_reason` | As in the SDK. |

The answer is `{"snapshot_id": ..., "rows_inserted": ..., "rows_updated": ...}`. The body has the
same limits as an append: `LAKANA_APPEND__MAX_BYTES` (256 MiB by default),
`LAKANA_APPEND__MAX_ARRAYS`, `LAKANA_APPEND__MAX_CONCURRENT` and
`LAKANA_APPEND__READ_TIMEOUT_SECONDS` ([settings](settings.md)).

## Who may merge

`WRITE_TABLE` and `READ_TABLE` on the table: a merge compares your rows with the table's, and
its counts and refusals tell you what it found, so it needs the right to read them. Creating a
missing table with `expected_snapshot_id="none"` needs `CREATE_TABLE` as well. A task may merge
into its own asset, as it may append to it, although its token may otherwise read only the
task's inputs: for a merge into its own asset that limit of the token is lifted, but the
principal the task runs as must still hold `READ_TABLE` on the table, as it would to merge with
its own token. A task may not merge into any other table.

A merge is refused while a row filter or column mask on the table applies to you, for the same
reason: it would compare your rows with rows or values you can't see. This holds for a task
too: a task acts for its run's principal, and a filter or mask for that principal refuses the
task's merge. Someone the table is not filtered or masked for can run it, or an owner can lift
the filter or mask for you.

## Errors

| Status | When | Message |
|---|---|---|
| `422` | a key column the table does not have | ``key column `customer_id` is not a column of bronze.customers`` |
| `422` | a key column the rows do not have | ``key column `id` is not a column of the rows sent`` |
| `422` | a key column that is null | ``key column `id` is null in 3 rows`` |
| `422` | a key that repeats in the rows | ``key `id` repeats in 12 rows of this batch; make it unique (for example with `unique(subset=['id'], keep='last')`)`` |
| `422` | a key of the rows that matches two or more rows of the table | ``key `id` repeats in 2 rows of bronze.customers that this batch matches; a merge needs one row per key. Find them with `SELECT id, count(*) FROM bronze.customers GROUP BY id HAVING count(*) > 1` and remove the repeats (for example by replacing the table) before you merge.`` |
| `422` | a key column holding lists or structs | the column and its type |
| `422` | a key of several columns with more than 5,000 rows | ``A merge on a key of several columns takes at most 5,000 rows per batch, and this batch has 12,000. Merge on one key column (a column that holds the whole key), or send batches of at most 5,000 rows.`` |
| `422` | a row filter or column mask on the table applies to you | ``Merges into bronze.customers are refused while a row filter or column mask applies to you on it: a merge compares your rows with rows you can't see. Ask someone the table is not filtered or masked for to run it, or ask an owner to lift the filter or mask for you.`` |
| `422` | rows that do not fit the table | the column that differs, as for an append |
| `422` | no `key` parameter | the parameter that is missing |
| `404` | the table does not exist and `expected_snapshot_id` is not `none`, or a source does not exist | the name |
| `403` | no `WRITE_TABLE` or `READ_TABLE`, or no `CREATE_TABLE` where the merge would create the table | the permission |
| `409` | `expected_snapshot_id` no longer matches, or another writer committed first | ``bronze.customers changed during the write; retry.`` |
| `409` | `expected_snapshot_id` is `none` but the table already has data; refused before the body is read | ``bronze.customers already has data, so it cannot be created again. A run or load that creates a table: retry, it will update the table. A call that sets expected_snapshot_id: pass the table's current snapshot id or leave it out.`` |
| `413`, `408`, `503` | the body is over the limit, arrives too slowly, or the server is busy with other uploads | as for an append |

The repeated-key and 5,000-row messages above are written for someone who sends the rows. An
asset that merges is told what to change in its own code instead: a SQL asset to keep one row per
key in its `SELECT` (with a `QUALIFY`), a Python asset to return one row per key, and both to
merge on one key column or to use a watermark (see [Write modes](asset-front-matter.md#write-modes)).

The messages give counts, never the values of a key. In the SDK each status is the usual error
from `lakana.errors` (`ValidationError`, `NotFound`, `PermissionDenied`, `Conflict`).

## Audit

One `TABLE_WRITE` event per merge that changed the table, in the same transaction as the commit:

| Field | Value |
|---|---|
| `detail.mode` | `merge` |
| `detail.target` | the table |
| `detail.snapshot_id` | the new snapshot |
| `detail.rows_inserted`, `detail.rows_updated` | the counts |
| `detail.rows` | the two counts added up |
| `detail.key` | the key columns |

The first merge, which creates the table, records the event of a create (`detail.mode` is
`create`).

## Limits

- The server reads the rows of the table whose key the batch has, and holds them in memory while
  it compares them. On one key column, a merge of 100,000 rows into a table of 1,000,000 took
  about 4 seconds and raised the server's memory by about 300 MiB.
- A key of several columns takes at most 5,000 rows per batch; a larger batch is refused before
  anything is read or written. Such a key is much slower: the time grows with the square of the
  batch's rows, and 5,000 rows took about 12 seconds. The time also grows with the table: every
  row of the table that the merge cannot skip by its files' key ranges is compared with each row
  of the batch, about 1.5 seconds per million rows for a batch of 5,000 (5,000 rows into a table
  of 5 million took about 12 seconds). A key of one column has no such limit, so
  give the table one column that holds the whole key, or send batches of at most 5,000 rows.
