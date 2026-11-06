# Metadata graph

Lakana holds what your projects declare about your data: which asset reads and builds which
table, the references between tables, measures, checks, glossary terms, row filters, column masks
and jobs. The metadata graph puts these together, so you can ask what feeds a table, what it feeds,
and what lies next to it. It is computed from the declarations when you ask, in every repository
deployed to the environment. Runs do not change it; the lineage that runs record is shown
separately, per table.

## Nodes and edges

Every node has an id that stays the same, which you may store and link to:

| Node | Id |
|---|---|
| Table (managed, external, view, artifact) | `table:<table id>` |
| Column | `column:<table id>/<column>` |
| Asset | `asset:<repository>/<asset>` |
| Reference | `reference:<id>` (kept while its table and column stay) |
| Measure | `measure:<id>` |
| Glossary term | `term:<id>` |
| Check | `check:<table id>/<check>` |
| Row filter, column mask | `row_filter:<id>`, `column_mask:<id>` |
| Job | `job:<repository>/<job>` |

Where a command or route asks for a node, you may also give a table's dotted name
(`gold.dim_store`), a column as `column:gold.dim_store/store_id` (or `--column store_id` in the
CLI), a check as `check:gold.fact_sales/ref_store_id`, and a term by its key (`term:net_revenue`).
Answers always carry the ids.

| Edge | From → to |
|---|---|
| `reads` | asset → a table it reads (its SQL, or a Python asset's inputs), in any repository |
| `builds` | asset → the table it produces |
| `references` | reference → its column, → the table and the column it points at |
| `measures` | measure → its column |
| `checks` | check → its table; a generated `ref_` check → its reference; a key check → the key's columns |
| `filters`, `masks` | row filter → its table; column mask → its column, by the column's current name: a mask follows its column through a rename, and one that is not bound to a field id (or whose column is gone) stays with the name it was made with. The current names are read from storage, each table once per question and only in the first half of the 10-second budget; when storage is slow or does not answer, a mask stays with the name it was made with rather than stopping the question |
| `links` | glossary term → a table or a column |
| `runs` | job → an asset it runs |
| `has_column` | table → its column |

## Questions

| Question | REST (`GET /api/v1/environments/{env}/graph/...`) | SDK (`client.graph.`) | CLI (`lakana graph ...`) |
|---|---|---|---|
| What feeds it, what it feeds | `lineage?node=&direction=up\|down\|both&depth=&lang=&limit=&cursor=` | `lineage(env, node, direction=, depth=, lang=, page_size=)` | `lineage NODE [--column] [--direction] [--depth] [--lang]` |
| What lies next to it | `neighbours?node=&kinds=&lang=&limit=&cursor=` | `neighbours(env, node, kinds=, lang=, page_size=)` | `neighbours NODE [--column] [--kind]... [--lang]` |
| What depends on it | `impact?node=&key=&lang=&limit=&cursor=` | `impact(env, node, key=, lang=, page_size=)` | `impact NODE [--column \| --key] [--lang]` |
| How two tables join | `join-path?from=&to=&lang=` | `join_path(env, from_table, to_table, lang=)` | `join-path FROM TO [--lang]` |

**Lineage** walks the tables and assets upstream (what feeds the start), downstream (what it
feeds) or both (the default). `depth` counts the assets crossed, from 1 to 10, and is 3 when not
given. An asset at the start counts as 0, with the tables it reads or builds. A column starts at
its table, plus the references that name the column and what they join it to.

In the CLI's table, the `side` column says where each node lies: `up` feeds the start, `down` is
fed by it, `both` is on a loop through it, `start` is where the walk began, and `join` is joined to
a column start by a reference. With `-o json`, the edges say the same. Both commands show a table's
label in the language `--lang` asks for. The CLI's table lists the nodes by distance, then kind,
then name; JSON and the SDK keep the server's order (by distance, then node id), which the cursors
follow.

For example, what feeds `gold.fact_sales`, in the two-repository project of
[Find what a change breaks](../how-to/find-what-a-change-breaks.md):

<!-- source: docs/examples/find-what-a-change-breaks/run.sh#lineage -->
```bash
lakana graph lineage gold.fact_sales --direction up --env dev
```

<!-- output: find-what-a-change-breaks#lineage -->
```text
┏━━━━━━━━━━┳━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━┓
┃ distance ┃ side  ┃ kind  ┃ name            ┃ label ┃ repository ┃
┡━━━━━━━━━━╇━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━┩
│ 0        │ start │ table │ gold.fact_sales │       │ sales      │
│ 1        │ up    │ asset │ gold.fact_sales │       │ sales      │
│ 1        │ up    │ table │ bronze.sales    │       │ sales      │
│ 1        │ up    │ table │ gold.dim_store  │       │ stores     │
│ 2        │ up    │ asset │ bronze.sales    │       │ sales      │
│ 2        │ up    │ asset │ gold.dim_store  │       │ stores     │
│ 2        │ up    │ table │ bronze.stores   │       │ stores     │
│ 3        │ up    │ asset │ bronze.stores   │       │ stores     │
└──────────┴───────┴───────┴─────────────────┴───────┴────────────┘
side: up feeds the start, down is fed by it, both is on a loop through it, join is joined to a column start by a reference.
```

The runs record a lineage of their own, shown per table by `lakana table lineage`; this one comes
from the declarations, before and without a run.

**Neighbours** lists what is one edge away, in either direction, optionally only edges of the
kinds you name.

Every lineage, neighbours and impact answer is a page of at most 500 nodes, sorted by distance, then id, with the edges between
them:

```json
{
  "nodes": [
    {"id": "table:7c1e…", "kind": "table", "name": "gold.dim_store", "distance": 0,
     "table_type": "managed", "model_kind": "dimension", "grain": "One row per store.",
     "key": ["store_id"], "owner": "stores-team", "repository": "stores", "label": null,
     "column_type": null, "reason": null, "may_use": false}
  ],
  "edges": [
    {"kind": "builds", "source": "asset:stores/gold.dim_store", "target": "table:7c1e…"}
  ],
  "next_cursor": null
}
```

Pass `next_cursor` back as `cursor` with the same question for the next page. Each edge is on the
page of its later node, so all pages together carry each edge once. The SDK and the CLI read every
page for you.

## Impact

**Impact** lists everything the projects declare that depends on a table, a column or a table's
declared key (`key=true`, `--key`; refused for a column, and for a table that declares no key).
The start is a table or a column. The answer holds:

- the references to it, and their generated `ref_` checks;
- the measures, column masks and glossary terms on its columns, and the checks on its key;
- its checks, row filters and glossary terms;
- the asset that builds it, everything downstream of it (the assets and the tables they build), and
  the jobs that run any of those assets.

Every node but the start carries a `reason`, for example `references gold.dim_store.store_id`,
`checks the key of gold.dim_store`, `links gold.dim_store.store_id`, `builds gold.dim_store` or
`downstream of gold.dim_store`. For a column or a key, what only *may* use it has `may_use: true`:
the table's hand-written checks, its row filters, and everything downstream, because Lakana does
not read SQL to find which columns a query uses. References to the table and terms on the table
name the table, not the column, so they are left out of a column's answer. A measure on a column
reaches a key's impact only through the fact table that declares it. Each node carries its
`distance` from the start, settled by the shortest way. In the CLI's table, `may_use` shows as
`yes`.

## Join paths

**Join paths** shows how two tables join over declared references, in either direction. It
returns every path of the shortest length, at most 4 steps long, and at most 10 paths in the order
of their references' ids; `more_paths` is `true` when more of that length exist. A step names the
reference, both tables with their kind and grain, and the columns to join on. A step along a
reference, from the table that declares it to its target, is `many_to_one`. A step against it is
`one_to_many` with `fans_out: true`: each row may meet many, so a sum over the near side's numbers
counts rows more than once. A reference whose column, on either side, no longer exists is never a
step, because a join on it would fail. When there is no path (or none you may see), `paths` is
empty and `reason` says so, with the same text either way.

```json
{
  "paths": [
    {"steps": [
      {"reference": "reference:2b9d…", "from_table": "table:41a7…", "from_name": "gold.fact_sales",
       "to_table": "table:7c1e…", "to_name": "gold.dim_store",
       "from_columns": ["store_id"], "to_columns": ["store_id"],
       "direction": "many_to_one", "fans_out": false,
       "from_model_kind": "fact", "from_grain": "One row per store and calendar day.",
       "to_model_kind": "dimension", "to_grain": "One row per store."}
    ]}
  ],
  "reason": null,
  "more_paths": false
}
```

## Who sees what

| Node | You need |
|---|---|
| A table, its columns, checks and measures | `READ_TABLE` on the table |
| A reference, a generated `ref_` check | `READ_TABLE` on its table and on the table it points at |
| A row filter, a column mask | the right to manage policies, and `READ_TABLE` on its table |
| An asset | `READ_ENVIRONMENT`, and `READ_TABLE` on the table it builds |
| A job, a glossary term | `READ_ENVIRONMENT` |

A walk stops at a node you may not see. Nothing beyond it is reached through it, no path through
it is shown, and no count or gap tells that it is there. A node beyond it still shows when another
route you may see reaches it. A start you may not see is answered like one that does not exist.

A job shows its edge to an asset only when you may see the asset.

Impact and join paths follow the same rules: a reason names only a node you may see, a join path
never runs through a table or reference you may not see, and the text for "no path" is the same
whether there is none or you may see none.

A node's `repository`, and a table's `owner` (the owner of the asset that builds it), come from
what is deployed to the environment: you see them only with `READ_ENVIRONMENT`. Without it they
are `null`.

## Limits and refusals

- `depth` is 1 to 10 and the page size (`limit` in REST, `page_size` in the SDK) is 1 to 500;
  outside them, the answer is `422` with the range. A cursor the server did not issue for this list
  (lineage, neighbours or impact), or a malformed one, is refused with `422` before any work is done.
- A question that would follow more than 200,000 edges is refused with `422`. Ask a narrower one:
  a smaller depth, one direction, or neighbours of some kinds; instead of impact, lineage
  downstream with a small depth.
- A walk that runs longer than 10 seconds is stopped with `503` and `Retry-After`. The time counts
  everything the question does, including what it looks at and you may not see. Try again, or ask
  a narrower question.
- A join path asks for two different tables: the same table twice, or a start that is not a table,
  is refused with `422`.

## The deploy warning

A deploy that removes a column, or stops building a table, that another repository declares
something on or reads still goes through and prints a warning. The line names the removed column or
table, the command that shows everything it affects, and the declarations and assets of other
repositories that use it, each with its repository. The how-to shows one:
[What a deploy tells you](../how-to/find-what-a-change-breaks.md#what-a-deploy-tells-you).

- It never refuses the deploy.
- It covers removed tables and columns. A changed column type is not seen at deploy, because a
  project records no types; the run that changes it names what other repositories use the column
  for ([What a run that changes a column type tells you](../how-to/find-what-a-change-breaks.md#what-a-run-that-changes-a-column-type-tells-you)).
  A column counts as removed only when both the deployed and the new version of its asset list
  their columns, which a `SELECT` with named columns does; `SELECT *` and Python assets are not
  checked.
- It lists the references, measures, checks and glossary terms of other repositories that
  name the removed column or table, and the assets of other repositories that read it. For a
  removed table, that is every asset that reads it. For a removed column, it is an asset that
  lists its columns and whose lineage takes one of them from the removed column, with the
  measures, checks, references and glossary terms on the columns it takes; an asset with
  `SELECT *`, a Python asset, or one that uses the column only in a join or a filter is not
  named. What only *may* use it (the `may_use` nodes) and what lies further downstream are left
  to `lakana graph impact`, so the check stays short however much is downstream.
- It is computed with what the deployer may see: a deployer without `READ_ENVIRONMENT` gets no
  impact warning, because the repositories of what depends on the table are not visible to them.
- It is shown to the deployer, and to a reader of the deploy who has `READ_ENVIRONMENT` and may
  read every table it names (the removed table, the tables of the references, measures and checks
  it lists, and the tables the assets it lists build). Other readers are not shown it, and nothing tells them it exists.
- A CI token is never shown it, not even for its own deploy: a CI token reads nothing, so the
  warning does not appear in a CI log. A task token of the deployer is shown it only when its
  declared assets cover every table it names. See it as a person with
  `lakana deploys get DEPLOY_ID --env ENV`.
- At most 20 removed tables and columns are checked; one more line says how many were not. The
  check has a budget of 2 seconds and 200,000 edges. The time is read between its steps (while
  loading the graph, after each repository's project, after deciding what the deployer may see, after each removed table or column,
  and after building the lines), so a check can run past 2 seconds by the length of one step. A
  check past either budget ends, the deploy goes on, and one line says Lakana could not check in
  time.
