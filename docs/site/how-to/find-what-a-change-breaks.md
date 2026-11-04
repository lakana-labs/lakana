# Find what a change breaks

Before you rename or remove a column, ask Lakana what depends on it: the references, checks,
measures, glossary terms, policies and jobs that the projects of every repository declare. Lakana
answers from those declarations and shows only what you may see.

This page uses two repositories: `stores` builds the dimension `gold.dim_store`, and `sales`
builds the fact `gold.fact_sales`, which references it. Both are deployed to `dev` and have run:

<!-- source: docs/examples/find-what-a-change-breaks/run.sh#deploy-sales -->
```bash
lakana deploy --env dev
lakana run --env dev
```

## Ask what depends on a column

<!-- source: docs/examples/find-what-a-change-breaks/run.sh#impact -->
```bash
lakana graph impact gold.dim_store --column store_id --env dev
```

<!-- output: find-what-a-change-breaks#impact -->
```text
┏━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┓
┃ distance ┃ kind      ┃ name                           ┃ reason                         ┃ may_use ┃
┡━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━┩
│ 0        │ column    │ gold.dim_store.store_id        │                                │         │
│ 0        │ table     │ gold.dim_store                 │                                │         │
│ 1        │ asset     │ gold.fact_sales                │ downstream of gold.dim_store   │ yes     │
│ 1        │ asset     │ gold.dim_store                 │ builds gold.dim_store          │         │
│ 1        │ check     │ gold.dim_store/key_not_null    │ checks the key of              │         │
│          │           │                                │ gold.dim_store                 │         │
│ 1        │ check     │ gold.dim_store/key_unique      │ checks the key of              │         │
│          │           │                                │ gold.dim_store                 │         │
│ 1        │ reference │ gold.fact_sales.store_id →     │ references                     │         │
│          │           │ gold.dim_store.store_id        │ gold.dim_store.store_id        │         │
│ 1        │ term      │ store                          │ links gold.dim_store.store_id  │         │
│ 2        │ check     │ gold.fact_sales/ref_store_id   │ checks the reference           │         │
│          │           │                                │ gold.fact_sales.store_id       │         │
│ 2        │ column    │ gold.fact_sales.store_id       │ references                     │         │
│          │           │                                │ gold.dim_store.store_id        │         │
│ 2        │ table     │ gold.fact_sales                │ references                     │         │
│          │           │                                │ gold.dim_store.store_id        │         │
└──────────┴───────────┴────────────────────────────────┴────────────────────────────────┴─────────┘
Not shown: repository. Use --output json to see it.
may_use: yes means it may use the column or key without naming it (a check, a row filter or something downstream); SQL is not parsed, so look before you change it.
```

Each line says why it is listed. `may_use` marks what only may use the column: its table's own
checks and row filters, and everything built from the table, because Lakana does not read SQL to
find which columns a query uses. Drop `--column` to ask about the whole table, or use `--key`
for its declared key.

## See how two tables join

<!-- source: docs/examples/find-what-a-change-breaks/run.sh#join-path -->
```bash
lakana graph join-path gold.fact_sales gold.dim_store --env dev
```

<!-- output: find-what-a-change-breaks#join-path -->
```text
Path 1:
  gold.fact_sales → gold.dim_store on store_id = store_id (many to one: fact → dimension)
```

A step that "fans out" goes from one row to many: joining a dimension to a fact multiplies the
dimension's rows, which matters when you sum one of its numbers.

## What a deploy tells you

When a deploy removes a column or stops building a table that another repository declares
something on, it still goes through, and it warns you. Here `stores` renames the key of
`gold.dim_store`, which removes `store_id`:

<!-- source: docs/examples/find-what-a-change-breaks/run.sh#warned -->
```bash
lakana deploy --env dev
```

<!-- output: find-what-a-change-breaks#warned -->
```text
Queued deploy f3a14187-362d-4584-8000-af806306fc08
Deployed stores in dev at 80e5a76bdd56e27bf3069498bb89bac7cdd31cb4
┏━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┓
┃ change        ┃ table          ┃
┡━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━┩
│ sql_changed   │ gold.dim_store │
│ model_changed │ gold.dim_store │
└───────────────┴────────────────┘
Checks:
  gold.dim_store: key_not_null (generated), key_unique (generated)
warning: this deploy removes the column `store_id` of `gold.dim_store`, which other repositories' declarations use; tell their owners, or keep it, and see everything it affects with: lakana graph impact gold.dim_store --column store_id --env dev — the declarations: check `gold.fact_sales/ref_store_id` (sales), glossary term `store` (sales), reference `gold.fact_sales.store_id → gold.dim_store.store_id` (sales)
Hint: run 'lakana run --env dev' to run what you just deployed.
```

Tell the owners of the other repository, or keep the column. The warning names the commands to see
everything the change affects. You see it if you ran the deploy, or if you may read every table it
names; other readers of the deploy are not shown it.

The warning covers removed tables and columns only. Lakana cannot tell from a project that a
column's type changed, because types come from the first run; that case is not warned about yet
([#446](https://github.com/lakana-labs/lakana/issues/446)).

For every node, edge, route and limit, see [Metadata graph](../reference/graph.md).
