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
| `filters`, `masks` | row filter → its table; column mask → its column |
| `links` | glossary term → a table or a column |
| `runs` | job → an asset it runs |
| `has_column` | table → its column |

## Questions

| Question | REST (`GET /api/v1/environments/{env}/graph/...`) | SDK (`client.graph.`) | CLI (`lakana graph ...`) |
|---|---|---|---|
| What feeds it, what it feeds | `lineage?node=&direction=up\|down\|both&depth=&lang=&limit=&cursor=` | `lineage(env, node, direction=, depth=, lang=, page_size=)` | `lineage NODE [--column] [--direction] [--depth] [--lang]` |
| What lies next to it | `neighbours?node=&kinds=&lang=&limit=&cursor=` | `neighbours(env, node, kinds=, lang=, page_size=)` | `neighbours NODE [--column] [--kind]... [--lang]` |

**Lineage** walks the tables and assets upstream (what feeds the start), downstream (what it
feeds) or both (the default). `depth` counts the assets crossed, from 1 to 10, and is 3 when not
given. An asset at the start counts as 0, with the tables it reads or builds. A column starts at
its table, plus the references that name the column and what they join it to.

In the CLI's table, the `side` column says where each node lies: `up` feeds the start, `down` is
fed by it, `both` is on a loop through it, `start` is where the walk began, and `join` is joined to
a column start by a reference. With `-o json`, the edges say the same. Both commands show a table's
label in the language `--lang` asks for.

**Neighbours** lists what is one edge away, in either direction, optionally only edges of the
kinds you name.

Every answer is a page of at most 500 nodes, sorted by distance, then id, with the edges between
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

A node's `repository`, and a table's `owner` (the owner of the asset that builds it), come from
what is deployed to the environment: you see them only with `READ_ENVIRONMENT`. Without it they
are `null`.

## Limits and refusals

- `depth` is 1 to 10 and the page size (`limit` in REST, `page_size` in the SDK) is 1 to 500;
  outside them, the answer is `422` with the range. A cursor the server did not issue for this list
  (lineage or neighbours), or a malformed one, is refused with `422` before any work is done.
- A question that would follow more than 200,000 edges is refused with `422`. Ask a narrower one:
  a smaller depth, one direction, or neighbours of some kinds.
- A walk that runs longer than 10 seconds is stopped with `503` and `Retry-After`. The time counts
  everything the question does, including what it looks at and you may not see. Try again, or ask
  a narrower question.
