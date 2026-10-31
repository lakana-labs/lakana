# Keep layers apart

Lakana keeps every table in one of three layers: raw data lands in **bronze**, is cleaned in
**silver**, and is modelled for use in **gold**. The catalog already keeps each table under one of
the three names. This page writes down how data may *flow* between them, so that a gold table
cannot read a raw export, a bronze table cannot be rebuilt from gold, and a staging table cannot
end up beside the facts. Every deploy and dry run then checks every asset against the rules.

The rules are opt-in: a project without `layers:` deploys as before. Once you add them, they apply
to every asset, in every environment, with no exception per asset. An environment's `defaults:`
still change how an asset that says nothing is written: with `materialize: view` among the
defaults of `prod` only, such an asset is a view in `prod` and is judged as one there, so set the
write in the asset's header when a layer allows only some writes.

## Paste the default

Add the medallion default to `lakana.yaml`. Change a rule only when your team decides so:

<!-- source: docs/examples/layer-rules/project/lakana.yaml -->
```yaml
# Lakana project. Reference: https://lakana-labs.github.io/lakana/how-to/projects/
repo: plant
layers:
  bronze:
    reads: []                                # bronze reads only sources
    write: [append, merge]
    kinds: none
  silver:
    reads: [bronze, silver]
    write: [replace, append, merge, view]
    kinds: [staging]
  gold:
    reads: [silver, gold]
    write: [replace, append, merge, view]
    kinds: [fact, dimension, wide]
```

- `reads`: the layers an asset may read (its SQL, its Python inputs and its `references:`).
  Anything outside the three layers, such as a raw-zone table or a connection, is a **source**,
  and only bronze reads sources.
- `write`: how an asset may store its result. Bronze keeps what arrived (`append` keeps every
  load, `merge` the latest of each row); add `replace` if you reload whole small sources.
- `kinds`: bronze declares no `kind:`, every silver table says it is `staging`, and gold holds
  facts, dimensions and wide tables ([Declare a table's model](declare-a-tables-model.md)).

All three layers and all three rules are required. Each rule is explained in
[`lakana.yaml`](../reference/lakana-yaml.md#layers).

## Check with a dry run

<!-- source: docs/examples/layer-rules/run.sh#dry-run -->
```bash
lakana deploy --env dev --dry-run
```

A dry run checks everything a deploy checks and changes nothing. When the project follows its
rules, the output includes `hint: layer rules: 3 assets follow them (bronze 1, silver 1, gold 1)`.

## Read what a deploy refuses

Here the gold table is built straight from bronze:

<!-- source: docs/examples/layer-rules/refused/assets/gold/meter_usage.sql -->
```sql
/* lakana
description: Energy used per meter.
kind: fact
grain: One row per meter.
key: [meter]
*/
SELECT upper(meter) AS meter, sum(kwh) AS kwh
FROM bronze.readings
GROUP BY upper(meter)
```

<!-- source: docs/examples/layer-rules/run.sh#refused -->
```bash
lakana deploy --env dev --dry-run
```

<!-- output: layer-rules#refused -->
```text
Queued dry run 0f9d3c4e-5a1b-4c2d-8e7f-6a5b4c3d2e1f
Error: assets/gold/meter_usage.sql:7: `gold.meter_usage` reads `bronze.readings`, which is in
bronze, but layers.gold.reads allows only silver and gold. Read the data from silver or gold
instead, or add bronze to layers.gold.reads in lakana.yaml
```

Each message names the asset, the rule it breaks, what it read and two ways out: change the
asset, or change the rule in `lakana.yaml`. Read the data from `silver.readings` instead, where
the meter names are already cleaned, and deploy again:

<!-- source: docs/examples/layer-rules/run.sh#fixed -->
```bash
lakana deploy --env dev
```

## Adopt the rules in passes

A project that adds `layers:` late can get many messages at once. A deploy lists the first 50 and
says how many more there are. Fix them in passes, with `lakana deploy --dry-run` after each:

1. **Assets outside the layers.** Only an artifact can be outside: move `models.churn_model` to
   `assets/gold/churn_model.py`, so it becomes `gold.churn_model`. An artifact in a layer follows
   `reads` only; it has no write mode and no kind, but a layer whose `write` is empty refuses it
   like any other asset.
2. **Write modes.** A bronze asset written with `replace` either merges on a key (`write: merge`
   with `key:`) or appends (`write: append` with `watermark:`), or you add `replace` to
   `layers.bronze.write`.
3. **Kinds.** Add `kind: staging` to silver tables and `kind: fact`, `dimension` or `wide` to gold
   tables; remove `kind:` from bronze tables.
4. **Reads.** A silver or gold asset that reads a source gets a bronze asset that loads it first;
   then it reads the bronze table. An ingestion asset (one that declares `connections`) belongs in
   bronze. A gold asset that reads bronze reads the silver table built from it.

## What the rules do not cover

- **Checks are free.** A check under `checks/` produces no data, so it may read any layer: a gold
  check that compares row counts with bronze is good practice.
- **Run time.** The rules describe how the project is built. Ad-hoc SQL, the SDK and other tools
  read what their grants allow.
- **Other repositories.** A table another repository builds is judged by its name like any other.
- **Views.** A catalog view is judged by its own name and is never opened: a gold asset may read
  `silver.recent_orders`, a view made through the REST API, even when that view reads a source.
  Build such views as assets (`materialize: view`) so that the rules see what they read.

Every message a deploy gives about the rules, with its next step:

| Broken rule | Message |
|---|---|
| A read from a layer not listed | ``` `silver.orders_clean` reads `gold.daily_sales`, which is in gold, but layers.silver.reads allows only bronze and silver. Read the data from bronze or silver instead, or add gold to layers.silver.reads in lakana.yaml``` |
| A reference to a layer not listed | ``` `gold.fact_sales` references `bronze.stores` (`references:`), which is in bronze, but layers.gold.reads allows only silver and gold. Point the reference at a silver or gold table, or add bronze to layers.gold.reads in lakana.yaml``` |
| A read where `reads: []` | ``` `bronze.orders` reads `silver.customers`, which is in silver, but layers.bronze.reads lists no layer, so bronze reads only sources. Build this table in silver, or add silver to layers.bronze.reads in lakana.yaml``` |
| A source read outside bronze | ``` `gold.daily_sales` reads `raw.pos_exports`, which is outside bronze, silver and gold, so it is a source, and only bronze reads sources (layers.gold.reads). Load it into bronze first with a bronze asset, then read the bronze table``` |
| A connection outside bronze | ``` `silver.orders` declares connection `crm`, a source, and only bronze reads sources (layers.silver.reads). Move the asset to assets/bronze/, then build silver from the bronze table``` |
| A write mode not listed | ``` `bronze.orders` is written with `replace`, but layers.bronze.write allows only append and merge. Write it with one of those (`write:` in its header), or add replace to layers.bronze.write in lakana.yaml``` |
| A view not listed | ``` `bronze.orders_v` is a view (`materialize: view`), but layers.bronze.write allows only append and merge. Build it as a table, or add view to layers.bronze.write in lakana.yaml``` |
| An empty `write` | ``` `bronze.orders` is in bronze, but layers.bronze.write is empty, so this project builds no bronze asset. Move the asset to another layer, or list its write mode in layers.bronze.write in lakana.yaml``` |
| An artifact where `write` is empty | ``` `gold.churn_model` is in gold, but layers.gold.write is empty, so this project builds no gold asset, not even an artifact. Move the asset to another layer, or list a write mode in layers.gold.write in lakana.yaml to open the layer``` |
| A kind missing | ``` `gold.daily_sales` declares no kind, but layers.gold.kinds needs every gold asset to declare one of fact, dimension, wide. Add `kind:` to its header``` |
| A kind not listed | ``` `gold.stg_orders` declares `kind: staging`, but layers.gold.kinds allows only fact, dimension, wide. Move it to the layer for staging tables, or declare one of those kinds``` |
| A kind where `kinds: none` | ``` `bronze.orders` declares `kind: staging`, but layers.bronze.kinds is none, so a bronze asset declares no kind. Remove `kind:` from its header``` |
| An asset outside the layers | ``` `models.churn_model` is outside bronze, silver and gold, but a project with `layers:` builds every asset in one of them. Move it to the layer whose assets read it, such as assets/gold/churn_model.py for gold.churn_model``` |

A Python asset's messages name `write=` and `kind=` "in its decorator", and a Python input reads
"reads `x` (input `train`)". Someone who may not read the table a message names sees this line
instead: ``` `gold.daily_sales` breaks layers.gold.reads with a read that is not shown to you```.
