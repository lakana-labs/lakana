# Declare a table's model

A table's model says what the table is and how it relates to others: its `kind` (a `fact`, a
`dimension`, a `wide` table for consumption, or a `staging` step), its `grain` (one sentence on what
exactly one row stands for), its `key` (the columns that identify a row) and its `references` (for
a column, the table its values point at). You write it in the header of the asset that builds the
table. People read it in `lakana table get` and find tables by it; the platform turns the key and
each reference into checks that run after every build, and builds a referenced table first. This
page builds a small star, one fact and two dimensions, and walks through what each part does.

## Write the headers

Each dimension declares its key. A dimension needs one, because a fact refers to it by that key:

<!-- source: docs/examples/see-a-tables-model/project/assets/gold/dim_store.sql -->
```sql
/* lakana
description: The stores.
kind: dimension
grain: One row per store.
key: store_id
*/
SELECT store_id, name FROM bronze.stores
```

<!-- source: docs/examples/see-a-tables-model/project/assets/gold/dim_product.sql -->
```sql
/* lakana
description: The products.
kind: dimension
grain: One row per product.
key: sku
*/
SELECT sku, name FROM bronze.products
```

The fact declares its grain, its key, and the dimension each of its `store_id` and `sku` points
at. `checks: references: warn` makes a missing product or store a warning instead of a failure, so
the table is still published while you clear up the data:

<!-- source: docs/examples/see-a-tables-model/project/assets/gold/fact_sales.sql -->
```sql
/* lakana
description: Sales per store, product and day.
kind: fact
grain: One row per store, product and calendar day.
key: [date, store_id, sku]
references:
  store_id: gold.dim_store
  sku: gold.dim_product
checks:
  references: warn
*/
SELECT date, store_id, sku, SUM(amount) AS amount
FROM bronze.sales
GROUP BY date, store_id, sku
```

`store_id: gold.dim_store` is the short form: it points at the target's single key column. To
point at another column, write it out (`sku: gold.dim_product.sku`). The rules for each field and
each kind are in
[Model fields](../reference/asset-front-matter.md#model-fields-kind-grain-key-and-references).

## Deploy, and see the generated checks

<!-- source: docs/examples/see-a-tables-model/run.sh#deploy -->
```bash
lakana deploy --env dev
```

<!-- output: see-a-tables-model#deploy -->
```text
Queued deploy 998eb674-5380-4c98-bc5e-6adf7fa08289
Deployed shop in dev at a0cc65e071af8181cdbbf28fef82a4821f37912d
┏━━━━━━━━┳━━━━━━━━━━━━━━━━━━┓
┃ change ┃ table            ┃
┡━━━━━━━━╇━━━━━━━━━━━━━━━━━━┩
│ added  │ bronze.products  │
│ added  │ bronze.sales     │
│ added  │ bronze.stores    │
│ added  │ gold.dim_product │
│ added  │ gold.dim_store   │
│ added  │ gold.fact_sales  │
└────────┴──────────────────┘
Checks:
  gold.dim_product: key_not_null (generated), key_unique (generated)
  gold.dim_store: key_not_null (generated), key_unique (generated)
  gold.fact_sales: key_not_null (generated), key_unique (generated), ref_sku (generated) (warn), ref_store_id (generated) (warn)
Hint: run 'lakana run --env dev' to run what you just deployed.
```

No file under `checks/` was written. Each `key:` became two checks, `key_unique` (no two rows share
a key) and `key_not_null` (no key column is null), and each reference became `ref_<column>` (every
non-null value exists in the target's key). `(warn)` marks the checks that only warn; the others
block, as a hand-written check does by default. What each check returns is in
[Generated checks](../reference/checks.md#generated-checks).

## Run, and read a reference check that warns

One sale in `bronze.sales` is of the product `soup`, which `gold.dim_product` does not list.

<!-- source: docs/examples/see-a-tables-model/run.sh#run -->
```bash
lakana run --env dev
```

The run succeeds, because the reference checks only warn. Its summary, and its checks in name
order, are in `lakana runs show`:

<!-- source: docs/examples/see-a-tables-model/run.sh#show -->
```bash
lakana runs show "$RUN_ID" --env dev
```

<!-- output: see-a-tables-model#show -->
```text
┌─────────────────────┬──────────────────────────────────────────┐
│ id                  │ ebf30d83-682e-4440-bdbc-0e746209e52c     │
│ repository          │ shop                                     │
│ commit_sha          │ 82ed7196cd20852da62c39842143b603889547a7 │
│ kind                │ full                                     │
│ job                 │                                          │
│ trigger             │                                          │
│ scheduled_for       │                                          │
│ missed              │ 0                                        │
│ status              │ succeeded                                │
│ cancel_requested    │ False                                    │
│ triggered_by        │ admin                                    │
│ run_as              │ admin                                    │
│ resumed_from_run_id │                                          │
│ error_message       │                                          │
│ created_at          │ 2026-10-29T20:20:36.264575+00:00         │
│ started_at          │ 2026-10-29T20:20:36+00:00                │
│ finished_at         │ 2026-10-29T20:20:41+00:00                │
└─────────────────────┴──────────────────────────────────────────┘
┏━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ asset            ┃ status    ┃ attempt ┃ rows ┃ error                                    ┃
┡━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ bronze.products  │ succeeded │ 1/3     │ 2    │                                          │
│ bronze.sales     │ succeeded │ 1/3     │ 4    │                                          │
│ bronze.stores    │ succeeded │ 1/3     │ 2    │                                          │
│ gold.dim_product │ succeeded │ 1/3     │ 2    │                                          │
│ gold.dim_store   │ succeeded │ 1/3     │ 2    │                                          │
│ gold.fact_sales  │ succeeded │ 1/3     │ 4    │ note: 1 check warned: `ref_sku` (1 row). │
└──────────────────┴───────────┴─────────┴──────┴──────────────────────────────────────────┘
Not shown: duration. Use --output json to see it.

gold.dim_product           passed   2 of 2 checks passed
  key_not_null (generated) passed
  key_unique (generated)   passed
gold.dim_store             passed   2 of 2 checks passed
  key_not_null (generated) passed
  key_unique (generated)   passed
gold.fact_sales            warned   4 of 4 checks passed
  key_not_null (generated) passed
  key_unique (generated)   passed
  ref_sku (generated)      warned   1 row
  ref_store_id (generated) passed
```

A check that warns counts as passed, and its outcome is `warned`. `lakana check list` shows the
newest results of a table's checks, with the same `(generated)` mark:

<!-- source: docs/examples/see-a-tables-model/run.sh#check-list -->
```bash
lakana check list --env dev --table gold.fact_sales
```

The rows the check found are the sales whose product is missing:

<!-- source: docs/examples/see-a-tables-model/run.sh#check-rows -->
```bash
lakana check rows "$RUN_ID" gold.fact_sales/ref_sku --env dev
```

<!-- output: see-a-tables-model#check-rows -->
```text
┏━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━┳━━━━━━━━┓
┃ date       ┃ store_id ┃ sku  ┃ amount ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━╇━━━━━━━━┩
│ 2026-10-02 │ 2        │ soup │ 40.0   │
└────────────┴──────────┴──────┴────────┘
1 rows
```

The fix is in the data: list the product in `bronze.products`, commit, deploy and run again.

<!-- source: docs/examples/see-a-tables-model/fixed/assets/bronze/products.sql -->
```sql
/* lakana
description: Three products, written by hand.
*/
SELECT 'tea' AS sku, 'Green tea' AS name
UNION ALL SELECT 'cake', 'Carrot cake'
UNION ALL SELECT 'soup', 'Tomato soup'
```

<!-- source: docs/examples/see-a-tables-model/run.sh#run-fixed -->
```bash
lakana run --env dev
```

Now every check of the three tables passes.

## See the model, and find tables by it

`lakana table get` shows the model after the columns, with the newest result of each generated
check; [See a table's model](describe-a-table.md#see-a-tables-model) shows the block and explains
each line.

<!-- source: docs/examples/see-a-tables-model/run.sh#get -->
```bash
lakana table get gold.fact_sales --env dev
```

`lakana table references` shows the references in both directions: what a table points at, and
which tables point at it.

<!-- source: docs/examples/see-a-tables-model/run.sh#references -->
```bash
lakana table references gold.dim_store --env dev
```

<!-- output: see-a-tables-model#references -->
```text
References: none.
Referenced by:
┏━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━┓
┃ table           ┃ column   ┃ target_column ┃
┡━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━┩
│ gold.fact_sales │ store_id │ store_id      │
└─────────────────┴──────────┴───────────────┘
```

Search finds a table by the words of its grain. The kind is not matched as text: to keep only the
tables of one kind, add `--model-kind` (`fact`, `dimension`, `wide` or `staging`):

<!-- source: docs/examples/see-a-tables-model/run.sh#search -->
```bash
lakana search "calendar" --model-kind fact --env dev
```

<!-- output: see-a-tables-model#search -->
```text
┏━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ kind  ┃ name                   ┃ matched ┃ where ┃ snippet                                      ┃
┡━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ table │ gold.fact_sales [fact] │ grain   │ table │ One row per store, product and calendar day. │
└───────┴────────────────────────┴─────────┴───────┴──────────────────────────────────────────────┘
```

`lakana table list` takes `--model-kind` too. The search rules are in
[Search](../reference/search.md).

## A reference sets the run order

A table the project builds and another table references is built first, as if the referencing
table read it. So selecting the fact with `--upstream` builds the two dimensions too. When a
dimension fails, or a blocking check sets it back, the fact is not built: here `bronze.products`
lists the product `soup` twice, `gold.dim_product`'s `key_unique` fails and blocks, and
`gold.fact_sales` ends `upstream_failed`.

<!-- source: docs/examples/see-a-tables-model/run.sh#upstream -->
```bash
lakana run --env dev --select gold.fact_sales --upstream
```

This holds whatever `checks: references:` says: `warn` lets the fact be published with a missing
key, but not built after a dimension that failed. Two tables that reference each other are a cycle,
and the deploy refuses it. The details are in
[A reference sets the run order](../reference/checks.md#a-reference-sets-the-run-order).

## When a generated check errors

A deploy does not know the types of the columns, nor the columns of a `SELECT *` or a Python asset.
So before a generated check counts, the worker compares the columns and types of the table it
wrote. A problem makes the check `errored`, and a blocking check that errored sets the table back
like a failed one. The message says which of these it is:

| The message starts with | It means | What to do |
|---|---|---|
| ``Key column `…` is not a column of …`` | The table has no such column (a column that differs only in letter case is named) | Fix `key:` or the `SELECT`, and deploy again. |
| ``Key column `…` is double, a floating-point type`` | A float or double cannot identify a row | Cast the column in the asset, for example to `DECIMAL`, `BIGINT` or `VARCHAR`. |
| ``Reference column `…` is not a column of …`` | The table has no such column | Fix `references:` or the `SELECT`, and deploy again. |
| ``The reference `…` … cannot be checked`` | The target is gone, the run's principal may not read it, or it no longer has the key column; the message does not say which | Check the target and its column, and grant the run's principal `READ_TABLE` on it. |
| ``The reference `…` … has a type that differs`` | The column and the target's key column have different types (only `int` and `long` match each other) | Cast the column to the type `lakana table get` shows for the target's column. |

The full texts are in
[Before a generated check counts](../reference/checks.md#before-a-generated-check-counts).

## When a deploy refuses

The model is checked at every deploy, and a header that breaks a rule stops the deploy with the
file and line; nothing changes in the environment. Here `gold.dim_product` was given `key: none`:

<!-- source: docs/examples/see-a-tables-model/run.sh#refused -->
```bash
lakana deploy --env dev
```

<!-- output: see-a-tables-model#refused -->
```text
Queued deploy 64d1c3e0-9900-44d7-8b1a-89a352c8f6ee
Error: assets/gold/dim_product.sql:1: Asset `gold.dim_product` is a dimension and needs `key:`;
`key: none` is not allowed for a dimension, because nothing could refer to it.
```

Put the key back, commit and deploy. The other messages, for a missing grain or key, a reference
whose target does not exist or that you may not read, a target without a key, or a reference to a
`staging` table, are in
[Model fields](../reference/asset-front-matter.md#model-fields-kind-grain-key-and-references).

## What a reader sees

Everyone who may read a table sees its model. A reference to a table the reader may not read is
not listed: `lakana table get` says how many there are (`1 more reference to a table you cannot
read`), `lakana table references` says the same of the tables that reference this one (`3 more from
tables you cannot read`), and a hidden reference never says whether its target exists. The
`ref_<column>` check of such a reference is left out of every listing of checks, which say instead
how many were left out (`1 check of a reference to a table you cannot read is not shown.`), and
search never shows the grain or kind of a table the reader may not read. Key checks and
hand-written checks are shown as always. The details are in
[See a table's model](describe-a-table.md#see-a-tables-model).
