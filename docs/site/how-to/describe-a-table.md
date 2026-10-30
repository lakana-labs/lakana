# Describe a table

After this guide a table and its columns carry a description that people see in `lakana table get`
and through the REST API. You know where the text comes from, how to change it by hand, and how to
go back to the text in your project. You need a table (see [Getting started](../tutorials/getting-started.md))
and to own it or to hold the right to drop it, which the data engineer and admin roles include.

A table has two possible descriptions, and Lakana shows one of them:

- **The project's text.** It is the `description:` and `columns:` of the asset's header. Every
  deploy publishes it, so git stays the place where the text is kept up to date.
- **An edit.** It is a text you set with `lakana table describe`. It is shown instead of the
  project's text, and a deploy never replaces it.

The same holds for each column.

## Write the text in the project

Put `description:` and `columns:` in the header of the asset that builds the table:

<!-- source: docs/examples/describe-a-table/project/assets/silver/orders.sql -->
```sql
/* lakana
description: One row per customer order.
columns:
  id: The order's number.
  status: Where the order stands.
*/
SELECT id, status FROM bronze.orders
```

A deploy writes these texts onto the table, and onto its columns, as the project's text. A table
the project builds is described as soon as the first run creates it. A column that the header does
not name has no project text.

## Change a description by hand

Name the table as `namespace.table`, and add `--column` to describe one of its columns:

<!-- source: docs/examples/describe-a-table/run.sh#describe -->
```bash
lakana table describe silver.orders --text "One row per customer order, whatever its state."
lakana table describe silver.orders --column status --text "open, shipped or cancelled."
```

A description is at most 4,000 characters and is not blank. A longer one is refused with the limit
and a request to write a shorter one. A column must be in the table's schema now.

`lakana table get` shows the table's text, whether it is your edit or the project's, and the text of
each column:

<!-- source: docs/examples/describe-a-table/run.sh#get -->
```bash
lakana table get silver.orders
```

With `--output json` the answer also says where each text came from: `description_source` is
`edited` for your text and `project` for the header's.

## Go back to the project's text

`--reset` removes your edit, so the project's text shows again. A table the project does not describe
then shows no description.

<!-- source: docs/examples/describe-a-table/run.sh#reset -->
```bash
lakana table describe silver.orders --reset
lakana table describe silver.orders --column status --reset
```

## Who may change a description

The table's owner, which is whoever created it, and anyone who holds `DROP_TABLE` on it. Changing a
table's or a column's text also needs the right to read the table. A refusal is recorded in the audit log like any
other. Every change is recorded as `TABLE_DESCRIBED` or `COLUMN_DESCRIBED`; the record holds the table,
the column and the length of the edit before and after, not the text. The facts of the routes, the
SDK and the audit events are in [Descriptions](../reference/descriptions.md).

## See a table's model

A table's model is what the header of the asset that builds it declares: its `kind`, its `grain`,
its `key` and its `references` (see [Model fields](../reference/asset-front-matter.md#model-fields-kind-grain-key-and-references)).
Only the header sets it: a deploy writes it, and neither a command nor the REST API changes it.
Here a fact declares its grain, its key and the dimensions its `store_id` and `sku` point at
([Declare a table's model](declare-a-tables-model.md) walks through writing one):

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

After a deploy and a run, `lakana table get` shows the model after the columns, with the newest
result of each check the header generates:

<!-- source: docs/examples/see-a-tables-model/run.sh#get -->
```bash
lakana table get gold.fact_sales --env dev
```

<!-- output -->
```text
Model: fact
Grain: One row per store, product and calendar day.
Key: date, store_id, sku
  sku → gold.dim_product.sku (exists)
  store_id → gold.dim_store.store_id (exists)
┏━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ check                    ┃ state  ┃ failing_rows ┃ finished_at               ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ key_not_null (generated) │ passed │ 0            │ 2026-10-29T20:19:56+00:00 │
│ key_unique (generated)   │ passed │ 0            │ 2026-10-29T20:19:56+00:00 │
│ ref_sku (generated)      │ passed │ 0            │ 2026-10-29T20:19:56+00:00 │
│ ref_store_id (generated) │ passed │ 0            │ 2026-10-29T20:19:56+00:00 │
└──────────────────────────┴────────┴──────────────┴───────────────────────────┘
```

Each reference says whether its target table exists now (`exists` or `missing`). `Key: none —
grain not checked` stands for `key: none`. A table whose header declares no model has no Model
block, and `model` is `null` in `--output json`.

`lakana table references` shows both directions: what the table references, and which tables
reference it. These are the references the model declares, not lineage: lineage is how data
flowed, references are what the model says relates.

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

To list only the tables of one kind, add `--model-kind` with `fact`, `dimension`, `wide` or
`staging`; any other value is refused with the four kinds:

<!-- source: docs/examples/see-a-tables-model/run.sh#list -->
```bash
lakana table list gold --model-kind fact --env dev
```

<!-- output: see-a-tables-model#list -->
```text
┏━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ name       ┃ namespace ┃ kind  ┃ state     ┃ uuid                                 ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ fact_sales │ gold      │ TABLE │ published │ 29cfdd97-5629-47e5-a919-d4f3866dd47a │
└────────────┴───────────┴───────┴───────────┴──────────────────────────────────────┘
```

In the SDK the same is `client.tables.get(env, name).model`,
`client.tables.references(env, name)` and `client.tables.list(env, namespace, model_kind="fact")`;
over REST it is `model` in the table's body, `GET .../tables/{name}/references` and
`?model_kind=fact` on the listing.

**What you see depends on what you may read.** You need to be able to read the table. A reference
to a table you may not read is not listed: `lakana table get` says how many there are
(`1 more reference to a table you cannot read`), and so does `hidden_references` in the body.
The same holds for the tables that reference this one (`3 more from tables you cannot read`,
`hidden_incoming`). A hidden reference never says whether its target exists, and neither does its
generated check. A result of a `ref_<column>` check is hidden also when the target its run
checked is one you may not read, even if the header now points the reference at a table you may
read; the next run replaces it. The `ref_<column>` check of a reference to a table you may not read is not
shown anywhere: not in the Model block, `lakana table health`, `lakana check list`,
`lakana runs show` or `lakana run`, nor in their REST and SDK answers. In its place they say how
many were left out (`1 check of a reference to a table you cannot read is not shown.`;
`hidden_checks` on table health and on each task of a run, `hidden` on a page of check results),
and `lakana check rows` for such a check answers as for a check the run does not have. A task's
message (the `error` column of `lakana runs show` and `lakana run`, and each attempt's message in
`lakana runs logs`) leaves such a check and its row count out, and says nothing of it when no other
check is left to mention. A message that names such a check in a way that cannot be taken apart
is replaced by ``This task's message is not shown: it names a check of a reference to a table you
cannot read. The checks you may see are listed with the task.``; the checks it lists are still in
`lakana runs show` and `lakana check list`. Key checks and hand-written checks are shown as before. Still not
filtered: the free-text log of a task, the note of a table that was kept or not published,
notifications of a failed job and the audit log, which can name such a check with its row count.

**References follow names.** A reference names its target as the header writes it, so it is
listed with whichever table has that name now, as the reference's generated check reads it. A
table renamed through the Iceberg REST catalog (an engine's `ALTER TABLE … RENAME`) is no longer
the target of the references to its old name, and a new table that takes the old name is. The
renamed table keeps the model its header gave it under the old name, listed under its new name,
until it is dropped; the project builds a new table under the old name, with the model again. A
table restored with `lakana table undrop` gets back the model it had when it was dropped, with its
references. A deploy while the table was dropped could not write onto it, so the model can be
older than the header until the next deploy; restored under another name (`--as`), it stays
as after a rename. When no project builds the table any more, nothing replaces the restored model:
it stays until the table is dropped again.
