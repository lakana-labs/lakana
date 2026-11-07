# Find what a change breaks

Before you rename or remove a column, ask Lakana what depends on it: the references, checks,
measures, glossary terms, policies and jobs that the projects of every repository declare. Lakana
answers from those declarations and shows only what you may see.

This page uses two repositories: `stores` builds the dimension `gold.dim_store`, and `sales`
builds the fact `gold.fact_sales`, which references it. Both were added with `lakana repo add`, then deployed to `dev` and run:

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
│ 1        │ asset     │ gold.dim_store                 │ builds gold.dim_store          │         │
│ 1        │ asset     │ gold.fact_sales                │ downstream of gold.dim_store   │ yes     │
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
warning: this deploy removes the column `store_id` of `gold.dim_store`, which other repositories' declarations or assets use; tell their owners, or keep it, and see everything it affects with: lakana graph impact --env dev gold.dim_store --column store_id — what uses it: check `gold.fact_sales/ref_store_id` (sales), glossary term `store` (sales), reference `gold.fact_sales.store_id → gold.dim_store.store_id` (sales)
Hint: run 'lakana run --env dev' to run what you just deployed.
```

Tell the owners of the other repository, or keep the column. The warning names the command that
shows everything the change affects. You see it if you ran the deploy, or if you may read the
environment (`READ_ENVIRONMENT`) and every table it names; other readers of the deploy are not
shown it. A CI token is not shown it either, so it stays out of CI logs: look the deploy up with
`lakana deploys get DEPLOY_ID --env ENV`.

The warning also names the assets of other repositories that read what the deploy removes: every
asset that reads a table the deploy stops building, and, for a removed column, every asset that
lists its columns (a `SELECT` with named columns) and takes one of them from it, with the measures
and glossary terms on the columns it takes. A table the deploy stops building stays, so its readers
keep running on data that no longer changes; a reader of a removed column fails at its next run.
Either way their owners need to know.

The warning covers removed tables and columns only. Lakana cannot tell from a project that a
column's type changed, because types come from the first run; the run that changes it warns
instead (below).

## What a run that changes a column type tells you

A run whose write changes a column's type in a way that does not widen (a text column that becomes
a number, a `BIGINT` that becomes an `INTEGER`) replaces the column with a new one under the same
name. At that write Lakana asks the metadata graph what other repositories use the column for, and
`lakana runs show RUN --env ENV` (and `lakana run`, when the run ends) prints it under the task's
note, one paragraph per column, for example:
``This run changed the type of column `store_name` of `gold.dim_store`, which other repositories use; tell their owners, and see everything it affects with: lakana graph impact --env dev gold.dim_store --column store_name — what uses it: asset `gold.fact_sales` (sales), glossary term `net_revenue` (sales), measure `gold.fact_sales.net_amount` (sales)``

It names the references, checks and glossary terms of other repositories on the column, the assets
that read it (an asset that lists its columns and takes one of them from it), and the measures,
checks and glossary terms on the columns those assets take from it. The run's own repository is
left out. The write goes through either way; tell the owners, who may need to change a measure's
aggregation or a reference.

- You are shown a use only if you may read the environment (`READ_ENVIRONMENT`) and every table it
  names: the changed table, the use's own table, and the table an asset builds. A use you may not
  see is left out, without a count.
- The SDK has the same in `RunTask.impact_warnings`, the REST API in `impact_warnings` on each task
  of `GET /api/v1/environments/{env}/runs/{id}`: per column its table, its name, the uses (kind,
  name, repository) and the paragraph.
- Lakana gives itself 2 seconds for this. The time is read between the steps of the check (while
  it reads the environment's projects, one repository at a time, after deciding what can be seen,
  after each column and after building the paragraphs), so it can run past 2 seconds by the length
  of one step. When it cannot finish in time, or the graph cannot be read, the run still writes,
  and the paragraph says that Lakana could not check in time and names the `lakana graph impact`
  command to run yourself. A retyped column whose name holds a control or format character, or is
  too long to look up in the graph, gets its own paragraph saying so (rename the column) instead.
- A write outside a run (`lakana query --evolve-schema`, an upload) is not checked: it has no run to
  show it on.

For every node, edge, route and limit, see [Metadata graph](../reference/graph.md).
