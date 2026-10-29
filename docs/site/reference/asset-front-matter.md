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
| `replace` (the default) | Replaces every row of the table with what the `SELECT` returns. | no `watermark:` (a `key:` is allowed: it says which columns identify a row) |
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
Checks:
  silver.customers: key_not_null, key_unique
hint: silver.orders_log: `materialize: incremental` is now written `write: append` with `watermark: order_id`; both work
```

A project version that uses `write: append` or `merge` needs workers of this release or later: a
worker of an earlier one refuses its manifest instead of running the asset as a replace.

## Model fields: kind, grain, key and references

Five keys say what the asset's table is, so that the people who read it, and the checks that
guard it, know what one row stands for and which tables it points at. A Python asset takes the
same names as arguments of `@asset(...)` ([Python assets](python-assets.md)). The keys of an
asset's block:

```yaml
kind: fact                            # fact | dimension | wide | staging
grain: One row per store and calendar day.
key: [date, store_id]                 # one column may be written as a plain string
references:
  store_id: gold.dim_store            # short form: that table's single key column
  product_sku: gold.dim_product.sku   # long form: a column you name
checks:
  key: block                          # block | warn | keep
  references: warn
```

| Key | What it says | Rules |
|---|---|---|
| `kind` | `fact`: events or measurements at a grain. `dimension`: the things facts refer to. `wide`: a denormalised table for consumption. `staging`: an intermediate step, not for consumers. | One of the four. Without `kind`, no rule of the table below applies. |
| `grain` | One sentence saying what exactly one row stands for. It is for people. | One line, not empty, at most 300 characters. |
| `key` | The columns that identify one row; usually the grain written as columns. It is also the key `write: merge` matches rows on: there is one key, not a second one for merging. | Distinct, non-empty columns of the `SELECT`, at most 8 (a merge key too), or `none` where the kind allows it (below). Allowed with every write mode. |
| `references` | For each column, the table its values point at. A null in a reference column is allowed; a reference that is required is also in `key`. | The column is a column of the `SELECT`; the target is checked as "References" below says. |
| `checks` | How the checks generated from `key` and `references` fail: `block`, `warn` or `keep`, with the rules of a check's `on_failure` (`keep` never on a view, an append or a merge). | Only `key` and `references`, and only for a field the asset declares. A deploy turns `key` into the checks `key_unique` and `key_not_null`, and each reference into `ref_<column>`; they count toward the 50 checks of a table, a hand-written check of the same table may not use their names, and a referenced table the project builds is built first. See [Generated checks](checks.md#generated-checks). |

### Rules for each kind

| `kind` | Requires | Notes |
|---|---|---|
| `fact` | `grain` **and** `key` | A fact whose rows really have no unique set of columns (for example raw readings where identical rows are valid) declares `key: none` and says why in `grain`. |
| `dimension` | `grain` **and** a `key` | `key: none` is refused: a dimension without a key cannot be referenced. |
| `wide` | `grain` **and** `key` | `key: none` is allowed, as for a fact. |
| `staging` | nothing | It may still declare `key` and `references`; if it does, they are checked. |

`key: none` needs `kind: fact` or `kind: wide`, and a merge refuses it: there is nothing to merge
on.

### References

A reference names its target in one of two forms:

- **Short form**, `store_id: gold.dim_store`: the target's key column. The target must declare a
  key of exactly one column.
- **Long form**, `product_sku: gold.dim_product.sku`: the column you name. The target need not
  declare a key, and the column must be one of its columns when the deploy knows them.

A deploy resolves each target against the project first, then against the environment it deploys
to:

- **A table of the project.** Its block says its key and its columns.
- **A table another repository builds.** It counts once it exists in the environment and the
  deploying principal may read it (`READ_TABLE`); its key comes from that repository's current
  deploy. A table that does not exist yet, and one you may not read, get the same message, which
  says nothing else about the table: deploy the other repository first, or ask for `READ_TABLE`.
  When a run principal is bound to the repository in the environment
  (`lakana deployments set-run-as`), it must be able to read the target as well, since its runs
  read it; otherwise the deploy names the target and the run principal.
- **A table no project builds.** It declares no key, so it needs the long form.

A `fact`, `dimension` or `wide` table may not reference a `staging` table: a model points at
modelled tables.

A namespace may have several levels, so `a.b.c` can read two ways: as the table `a.b.c`, or as the
column `c` of the table `a.b`. When only one of them is a table, that one is meant. When both are,
the deploy refuses the reference unless it knows that `a.b` has no column `c`; rename one of the
two tables.

A `SELECT *` or a Python asset has no columns at deploy, so its key and reference columns are
checked when it runs. Column types are checked only then, for every asset: a key column may not be
`float` or `double`, and a reference column must have its target column's type, where only `int`
and `long` match each other. A problem makes the generated check `errored`; the texts and what to
do about each are in [Before a generated check counts](checks.md#before-a-generated-check-counts).

### Errors

Each of these fails the deploy with an issue at the block's line:

| Issue | Cause |
|---|---|
| ``kind: must be one of fact, dimension, wide, staging; got 'cube'`` | Another kind. |
| ``grain: must not be empty: say in one sentence what one row is``, ``grain: must be one line``, ``grain: must be at most 300 characters`` | A grain that is empty, of several lines or too long. |
| ``Asset `gold.fact_orders` is a fact and needs `grain:`: say in one sentence what one row is.`` | A `fact`, `dimension` or `wide` table without `grain`. |
| ``Asset `gold.fact_orders` is a fact and needs `key:`: list the columns that identify one row (usually the grain's columns), or write `key: none` and say in `grain` why no columns are unique.`` | A `fact` or `wide` table without `key`. |
| ``Asset `gold.dim_customer` is a dimension and needs `key:`; `key: none` is not allowed for a dimension, because nothing could refer to it.`` | A dimension without a key, or with `key: none`. |
| ``` `key: none` is for a fact or a wide table whose rows no columns identify; declare `kind: fact` or `kind: wide`, or remove `key: none` ``` | `key: none` on another kind, or without `kind`. |
| ``` `write: merge` needs key columns; `key: none` says that no columns identify a row, so there is nothing to merge on ``` | `write: merge` with `key: none`. |
| ``a key has at most 8 columns, and `key:` lists 9; shorten it to the columns that identify one row`` | A key of more than 8 columns, a merge key's included. List only the columns that identify one row. |
| ``a key column must not be empty; write the name of a column of the asset`` | `key: ""` or an empty name in the list. |
| ``Key column `id` is not a column of `gold.fact_orders`'s SELECT; add it to the SELECT, or change `key:`.`` | A key column the `SELECT` does not return. |
| ``Reference column `store_id` is not a column of `gold.fact_orders`'s SELECT; add it to the SELECT, or remove the reference.`` | A reference column the `SELECT` does not return. |
| ``references: reference `store_id: dim_store`: … Write `ns.table` or `ns.table.column`.`` | A target that is not a table name. |
| ``references: reference `store_id` names a target that must not contain a control, format or surrogate character or a line separator`` | A target with a line break, a control character or a bidirectional control. |
| ``references: a reference column must not be empty`` | A reference whose column is `""`. |
| ``Reference target `gold.dim_store` does not exist in environment `prod`, or you may not read it; build it first, or ask for READ_TABLE on it.`` | The target is neither in the project nor a table of the environment that you may read. |
| ``Reference `customer_id: gold.dim_customer`: `gold.dim_customer` declares no key. Declare `key:` in its asset, or name the column: `customer_id: gold.dim_customer.<column>`.`` | The short form, for a target without a key (`key: none` too). |
| ``Reference `store_id: gold.dim_store`: `gold.dim_store` has a key of 2 columns; name the column: `store_id: gold.dim_store.<column>`.`` | The short form, for a target with a key of several columns. |
| ``Reference `store_id: gold.dim_store.nope`: `gold.dim_store` has no column `nope`; name one of its columns, or use the short form to point at its key.`` | The long form names a column the target does not have. |
| ``Reference target `dim.store` cannot be read by `svc-etl`, the run principal bound to this repository in `prod`, so its runs could not check it; grant it READ_TABLE on `dim.store`, or bind another run principal with `lakana deployments set-run-as`.`` | You may read the target, but the run principal bound to the repository may not. |
| ``` `gold.fact_orders` (a fact) references `staging.orders_clean`, a staging table; point it at a modelled table. ``` | A `fact`, `dimension` or `wide` table references a `staging` table. |
| ``Reference `store_id: a.b.store_id`: `a.b.store_id` is a table, and so is `a.b`, which has a column `store_id`; rename one of them so that the reference names one thing.`` | A target that reads two ways (above). When the deploy does not know the columns of `a.b`, the message says ``which would read `store_id` as its column``. |
| ``Reference `model_id: models.churn`: `models.churn` is an artifact, not a table, so no column can point at it; point the reference at a table.`` | The target is an artifact, of the project or of the environment. |
| ``Reference `store_id: gold.dim_store` is too long to store: the column `…` has 1025 bytes, and each of the column, the target table and the target column may have at most 1024 bytes; shorten that name.`` | A reference column, its target table's dotted name or its target column is longer than 1024 bytes in UTF-8. Shorten the name the message points at. |
| ``` cycle: gold.a -> gold.b -> gold.a (a reference in `references:` counts as a dependency: the target is built first); remove one of the references, or check it with a hand-written check under `checks/` ``` | Two assets of the project reference each other, directly or through others. A target the project builds is built before the table that references it, so such references cannot all hold; remove one of them. |
| ``A check named `key_unique` is generated for `gold.fact_orders`; rename `checks/gold/fact_orders/key_unique.sql`.`` | A hand-written check of a table has the name of a check that table's header generates (`key_unique`, `key_not_null`, `ref_<column>`). Rename the file. |
| ``` `checks: key: keep` cannot be used on `gold.fact_orders`: it is written with `write: merge`, … Use `checks: key: block`: … ``` | `keep` for a generated check of a view, an append or a merge, as for a hand-written check's `on_failure`. |
| ``checks: takes only key and references``, ``checks: key must be block, keep or warn`` | Another check family or failure mode. |
| ``` `checks: key` sets how the key checks fail, but the asset declares no `key:`; declare `key:`, or remove `checks: key` ``` | A `checks` family for a field the asset does not declare (`references` likewise). |

A project version in which an asset declares a model field (`key:` alone counts, so every project
with a `write: merge` asset) needs workers of this release or later: a worker of an earlier one
refuses its manifest. A deploy's changes list a table whose `kind`, `grain`, `references`,
`checks`, or the `key` of an asset that does not merge, changed as `model_changed`; the key of a
merge stays a change of how the table is written.
