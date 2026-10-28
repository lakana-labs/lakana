# When a table's columns change

A table's columns change when a write lets the table take the schema of its result:

- a SQL asset with `write: append` and `evolve_schema: true`
  ([Asset front-matter](../reference/asset-front-matter.md#append));
- a Python asset with `@asset(evolve_schema=True)`, which replaces its table on every run
  ([Python assets](../reference/python-assets.md#when-the-output-changes-shape));
- a query or an upload with `--evolve-schema`, or `client.tables.replace(..., evolve_schema=True)`
  in Python.

Without that option a write whose result has other columns than the table fails and changes
nothing. This page says what the table does with each kind of change, and what a removed column
means for everyone who reads the table.

## What the table does

Lakana compares the result's columns with the table's by name. Iceberg, the table format, gives
every column an id of its own, and Lakana reads the data by that id, never by the name.

| The result | The table afterwards |
|---|---|
| has a column the table lacks | The column is added. Rows written before read as NULL in it. |
| has a column in a wider type: `int` to `long`, `float` to `double`, a decimal with more digits at the same scale | The column widens and keeps its data. |
| has a column in a narrower type: `int` where the table has `long`, `float` where it has `double` | Nothing changes: the values are stored in the table's wider type. |
| has a column in a type that does not widen, such as `string` where the table has `long` | The column becomes a **new column** of the same name. Rows written before read as NULL in it, never as the old values. A row filter or column mask that uses the column refuses the reads it applies to until an administrator creates it again (below). |
| lacks a column the table has | The column is **removed** from the table. |
| has a column of another name | Lakana does not rename on its own: the old column is removed and a new one is added. |

A replace writes every row again, so its table holds only what the last run returned: "rows
written before" matters for an append, whose earlier rows stay.

A Python asset meets the narrower and wider types more often than SQL does. Polars makes whole
numbers `Int64` and fractions `Float64` unless the code says otherwise, so a table first written
from an `Int32` or a `Float32` column widens when a later run returns the defaults. Without
`evolve_schema` that later run fails, with `retyped id: int -> long` in the message.

## What a removed column means for readers

The column is gone from the table as everyone sees it now. Everything that names it fails, and
everything that reads all columns quietly gets one fewer.

- **A query that names it** fails:
  `Query failed: Binder Error: Referenced column "gone" not found in FROM clause!`.
  A `SELECT *` does not fail: it returns the columns the table has now.
- **A view that names it** fails the same way each time it is read, until its SQL is changed.
  A view on `SELECT *` shows the columns the table has now.
- **A downstream asset that names it** fails at its next run with the same error, and every asset
  after it in that run ends `upstream_failed`. A SQL asset on `SELECT *` loses the column from
  its own result; a SQL asset that replaces its table cannot change its schema, so it fails with
  ``The result does not match <table>'s schema (dropped gone …)``. List the columns it needs
  instead of `*`. Do not drop its table so that the next run creates it again: the new table has
  none of the old table's grants, row filters or column masks, and its readers would see every
  row and column until an administrator creates them again. A Python asset gets its
  input without the column, and code that uses the column fails in the function.
- **A check whose SQL names it** errors, like any query that names it.
- **A row filter or a column mask on it** stops the reads of the people it applies to. See
  [Filters and dropped columns](../how-to/filter-rows.md#filters-and-dropped-columns) and the end
  of [When Lakana refuses](../how-to/mask-a-column.md#when-lakana-refuses) for masks.

## What a retyped column means for row filters and column masks

A column whose type changes in a way that does not widen is a new column under the old name. A row
filter or column mask that uses it was made for the old column, so a read of the table by the
people it applies to is refused, and nothing is shown: ``` `gold.payments` has a row filter or column mask that uses column `amount`, and a column was renamed or replaced since the policy was created, so the policy may now cover a different column. Nothing is shown. An administrator must review … ```.
An administrator creates the filter or mask again for the new column, then revokes the old one
([After a rename](../how-to/filter-rows.md#after-a-rename) has the order). A widened column keeps
its policies: it is the same column. Before an asset with `evolve_schema` changes a column's type,
ask whoever manages the table's policies, so the reads do not stop by surprise.

Adding the column back does not bring its data back: it returns as a new column, empty in every row
written before.

## The old data is still in the old snapshots

Removing a column deletes no data. Every write leaves a **snapshot**, the state of the table it
wrote, and the snapshots from before the change still hold the column in their data files.

- Lakana's own queries always read the current snapshot. They cannot reach the old one: a query
  with `AT (...)` is refused.
- An engine that reads the table through Lakana's Iceberg REST catalog, with storage credentials
  of its own (the catalog hands out none), can read an older snapshot, with that snapshot's
  columns, as long as the table keeps it.
- A blocking check that fails after the run sets the table back to the snapshot before it, and the
  columns with it ([Checks](../reference/checks.md)).
- `lakana table maintain` removes snapshots older than seven days by default, but always keeps the
  last five. Only then are the removed column's values deleted from storage.

If a column was removed because its values must not be kept, removing it is not enough: its
values stay readable through the old snapshots until maintenance has removed them all.
