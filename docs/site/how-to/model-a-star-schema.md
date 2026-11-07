# Model a star schema

A star schema is one fact table (what happened) surrounded by dimension tables (who, what, where).
This page builds one from a template that needs no source system: four generated tables go through
bronze and silver into a gold star, and a customer who moves keeps the city she had. Every key
the project's files take is in [Asset front-matter](../reference/asset-front-matter.md).

## Start from the template

<!-- source: docs/examples/medallion/run.sh#init -->
```bash
lakana init retail --template medallion
```

It writes a project that deploys as it is: 13 assets, four checks, the layer rules, German
translations of the gold tables and a glossary. Commit it, register it with `lakana repo add`
(the command prints the lines) and build it:

<!-- source: docs/examples/medallion/run.sh#deploy -->
```bash
lakana deploy --env dev
lakana run --env dev
```

## What belongs in each layer

- **Bronze** holds data as the source delivered it. The template's bronze tables are generated
  SQL, with the faults real exports have: numbers and dates as text, stray spaces, a sale that
  arrived twice. Each bronze table writes with `write: merge` on a key that includes the time of
  the export or of the change, so running it again changes nothing, and a row delivered twice at
  different times stays in bronze until silver keeps one. To use your own data, replace a bronze
  file with an asset that reads a source ([Load data from a source](load-data-from-a-source.md)).
- **Silver** cleans it: the right types, trimmed text, one row per key. Its tables are
  `kind: staging`, and each has one hand-written check under `checks/`.
- **Gold** is the star and the tables people query.

`lakana.yaml` writes this down, and every deploy checks it ([Keep layers apart](keep-layers-apart.md)):

<!-- source: docs/examples/medallion/project/lakana.yaml -->
```yaml
# Lakana project. Reference: https://lakana-labs.github.io/lakana/how-to/projects/
repo: retail
# The language of the descriptions in the asset headers; translations/de.yaml translates the
# gold tables into German.
main_language: en
defaults:
  materialize: table
requirements:
  fields: [description]
# Layer rules: how data may flow. Bronze reads only sources, silver reads bronze, gold reads
# silver and gold. These are the medallion default; see
# https://lakana-labs.github.io/lakana/how-to/keep-layers-apart/
layers:
  bronze:
    reads: []
    write: [append, merge]
    kinds: none
  silver:
    reads: [bronze, silver]
    write: [replace, append, merge, view]
    kinds: [staging]
  gold:
    reads: [silver, gold]
    write: [replace, append, merge, history, view]
    kinds: [fact, dimension, wide]
# Table health runs at its defaults: every signal warns and none stops a run. To stop a run
# whose row count falls outside the usual band, see the commented line in
# assets/gold/fact_sales.sql and https://lakana-labs.github.io/lakana/reference/table-health/
```

A gold asset that reads bronze is refused. Save this as `assets/gold/raw_sales.sql`, commit it, and
check the deploy:

<!-- source: docs/examples/medallion/refused/assets/gold/raw_sales.sql -->
```sql
/* lakana
description: Sales straight from bronze.
kind: fact
grain: One row per sale.
key: sale_id
*/
SELECT sale_id, store_id FROM bronze.sales
```

<!-- source: docs/examples/medallion/run.sh#refused -->
```bash
lakana deploy --env dev --dry-run
```

<!-- output: medallion#refused -->
```text
Queued dry run 8a1f9664-2e95-49e3-8852-53262f2492b3
Error: assets/gold/raw_sales.sql:7: `gold.raw_sales` reads `bronze.sales`, which is in bronze, but
layers.gold.reads allows only silver and gold. Read the data from silver or gold instead, or add
bronze to layers.gold.reads in lakana.yaml
```

The message names the cause and the fix. Delete the file again to go on.

## Declare the grain

The grain says what one row of a table stands for. Say it before you write the `SELECT`, in one
sentence, and write the columns that identify a row as the `key`. Lakana generates the checks
`key_unique` and `key_not_null` from the key, so a fact that is not at its grain fails the run.

<!-- source: docs/examples/medallion/project/assets/gold/fact_sales.sql -->
```sql
/* lakana
label: Sales
description: One row per sale, pointing at the store, the product and the customer as they were on the day.
kind: fact
grain: One row per sale.
key: sale_id
references:
  store_id: gold.dim_store
  product_id: gold.dim_product
  customer_id: {to: gold.dim_customer, at: sold_on}
measures:
  net_amount:
    aggregation: sum
    currency: EUR
    label: Net revenue
columns:
  sale_id: The sale's number.
  store_id: The store that sold.
  customer_id: The customer who bought.
  product_id: The product sold.
  sold_on: The day of the sale.
  quantity: How many units were sold.
  gross_amount: Quantity times unit price in euros, before the discount.
  net_amount: The amount in euros after the discount.
# health: {volume: block}   # uncomment to stop a run whose row count is far outside its usual band
*/
SELECT
  s.sale_id,
  s.store_id,
  s.customer_id,
  s.product_id,
  s.sold_on,
  s.quantity,
  CAST(s.quantity * p.unit_price AS DECIMAL(12, 2)) AS gross_amount,
  CAST(s.quantity * p.unit_price - s.discount AS DECIMAL(12, 2)) AS net_amount
FROM silver.sales AS s
LEFT JOIN silver.products AS p ON p.product_id = s.product_id
```

## Reference the dimensions

`references:` names the dimension each column points at. Each reference becomes a check
(`ref_store_id`, `ref_product_id`, `ref_customer_id`) that returns the sales whose store, product
or customer the dimension does not have. The fact joins products with a `LEFT JOIN` so such a
sale stays visible for the check to report instead of disappearing. The dimensions are plain:

<!-- source: docs/examples/medallion/project/assets/gold/dim_store.sql -->
```sql
/* lakana
label: Stores
description: The stores, one row each.
kind: dimension
grain: One row per store.
key: store_id
columns:
  store_id: The store's number.
  name: The store's name.
  city: The city the store is in.
*/
SELECT store_id, name, city FROM silver.stores
```

## Keep the history of a customer

A customer who moves must not rewrite last month's sales. `dim_customer` writes with
`write: history`: each run opens a new version when a customer's details change and closes the
old one.

<!-- source: docs/examples/medallion/project/assets/gold/dim_customer.sql -->
```sql
/* lakana
label: Customers
description: Every version of every customer; a customer who moves gets a new row, so old sales keep the city they had.
kind: dimension
grain: One row per customer and version of their details.
key: customer_id
write: history
valid_from: changed_at
close_missing: true
columns:
  customer_id: The customer's code.
  name: The customer's name.
  city: The city the customer lived in during this version.
*/
SELECT customer_id, name, city, changed_at FROM silver.customers
```

The fact names the version it means with the sale's day (`at: sold_on`). Silver keeps only the
latest row of each customer, so the first build gives `dim_customer` one version per customer, and
every later change opens a new one. To see it, move Grace to Hamburg: replace
`assets/bronze/customers.sql` with this file, which adds one row (a new `changed_at` for
`C-002`), commit, deploy and run:

<!-- source: docs/examples/medallion/changes/customers.sql -->
```sql
/* lakana
description: The customers as the CRM exports them, with a row each time a customer changed.
write: merge
key: [customer_id, changed_at]
columns:
  customer_id: The customer's code.
  name: The customer's name, as exported.
  city: The city the customer lived in at that change.
  changed_at: When the CRM recorded this row.
*/
SELECT * FROM (
  VALUES
    ('C-001', ' Ada ', 'Hamburg', TIMESTAMPTZ '2026-09-01 12:00:00+00'),
    ('C-002', 'Grace', 'Berlin', TIMESTAMPTZ '2026-09-01 12:00:00+00'),
    ('C-002', 'Grace', 'Hamburg', TIMESTAMPTZ '2026-10-06 12:00:00+00'),
    ('C-003', 'Edsger', 'Dresden', TIMESTAMPTZ '2026-08-01 12:00:00+00'),
    ('C-003', 'Edsger', 'Munich', TIMESTAMPTZ '2026-09-01 12:00:00+00')
) AS crm(customer_id, name, city, changed_at)
```

<!-- source: docs/examples/medallion/run.sh#change -->
```bash
lakana deploy --env dev
lakana run --env dev
```

Grace now has two versions; the first is closed and starts at the earliest time, so that every
sale finds a version:

<!-- source: docs/examples/medallion/run.sh#versions -->
```bash
lakana sql "SELECT customer_id, city, CAST(valid_from AS DATE) AS valid_from, is_current FROM gold.dim_customer ORDER BY customer_id, valid_from" --env dev
```

<!-- output: medallion#versions -->
```text
┏━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━┓
┃ customer_id ┃ city    ┃ valid_from ┃ is_current ┃
┡━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━┩
│ C-001       │ Hamburg │ 0001-01-01 │ True       │
│ C-002       │ Berlin  │ 0001-01-01 │ False      │
│ C-002       │ Hamburg │ 2026-10-06 │ True       │
│ C-003       │ Munich  │ 0001-01-01 │ True       │
└─────────────┴─────────┴────────────┴────────────┘
4 rows
```

More about versions: [Keep the history of a dimension](keep-the-history-of-a-dimension.md).

## Offer a wide table

People who work in spreadsheets want one table without joins. `sales_wide` joins the fact to its
dimensions, and for the customer it picks the version valid on the day of the sale:

<!-- source: docs/examples/medallion/project/assets/gold/sales_wide.sql -->
```sql
/* lakana
label: Sales, wide
description: One row per sale with its store, product and customer written out, for spreadsheets.
kind: wide
grain: One row per sale.
key: sale_id
columns:
  sale_id: The sale's number.
  sold_on: The day of the sale.
  store: The store's name.
  store_city: The city the store is in.
  product: The product's name.
  category: The product group.
  customer_id: The customer who bought.
  customer: The customer's name.
  customer_city: The city the customer lived in on the day of the sale.
  quantity: How many units were sold.
  gross_amount: Quantity times unit price in euros, before the discount.
  net_amount: The amount in euros after the discount.
*/
SELECT
  f.sale_id,
  f.sold_on,
  st.name AS store,
  st.city AS store_city,
  p.name AS product,
  p.category,
  f.customer_id,
  c.name AS customer,
  c.city AS customer_city,
  f.quantity,
  f.gross_amount,
  f.net_amount
FROM gold.fact_sales AS f
JOIN gold.dim_store AS st ON st.store_id = f.store_id
JOIN gold.dim_product AS p ON p.product_id = f.product_id
JOIN gold.dim_customer AS c
  ON c.customer_id = f.customer_id
  AND c.valid_from <= f.sold_on
  AND (c.valid_to IS NULL OR f.sold_on < c.valid_to)
```

<!-- source: docs/examples/medallion/run.sh#joined -->
```bash
lakana sql "SELECT sale_id, sold_on, customer_city FROM gold.sales_wide WHERE customer_id = 'C-002' ORDER BY sale_id" --env dev
```

<!-- output: medallion#joined -->
```text
┏━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┓
┃ sale_id ┃ sold_on    ┃ customer_city ┃
┡━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━┩
│ 2       │ 2026-10-03 │ Berlin        │
│ 5       │ 2026-10-06 │ Berlin        │
│ 8       │ 2026-10-09 │ Hamburg       │
│ 11      │ 2026-10-12 │ Hamburg       │
└─────────┴────────────┴───────────────┘
4 rows
```

Two of Grace's sales kept Berlin and two have Hamburg.

## Name the measure and the terms

`measures:` on the fact says that `net_amount` is summed and is in euros. It is metadata: Lakana
computes nothing from it ([Declare a table's model](declare-a-tables-model.md#declare-the-measures)).
The query a spreadsheet user writes is still yours:

<!-- source: docs/examples/medallion/run.sh#star -->
```bash
lakana sql "SELECT store, SUM(net_amount) AS net_amount FROM gold.sales_wide GROUP BY store ORDER BY store" --env dev
```

<!-- output: medallion#star -->
```text
┏━━━━━━━━━┳━━━━━━━━━━━━┓
┃ store   ┃ net_amount ┃
┡━━━━━━━━━╇━━━━━━━━━━━━┩
│ Airport │ 118.00     │
│ Harbour │ 42.00      │
│ Station │ 80.00      │
└─────────┴────────────┘
3 rows
```

`glossary/sales.yaml` defines "net revenue" and "gross revenue" and links them to
`gold.fact_sales` and its columns; a deploy refuses a link to a column that does not exist
([Keep a glossary](keep-a-glossary.md)). `translations/de.yaml` shows the gold tables in German
to a reader who asks for it ([Translate labels and descriptions](translate-labels-and-descriptions.md)).

## Find what a change breaks

Before you rename a key, ask what uses it:

<!-- source: docs/examples/medallion/run.sh#impact -->
```bash
lakana graph impact gold.dim_store --column store_id --env dev
```

<!-- output: medallion#impact -->
```text
┏━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┓
┃ distance ┃ kind      ┃ name                           ┃ reason                         ┃ may_use ┃
┡━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━┩
│ 0        │ column    │ gold.dim_store.store_id        │                                │         │
│ 0        │ table     │ gold.dim_store                 │                                │         │
│ 1        │ asset     │ gold.dim_store                 │ builds gold.dim_store          │         │
│ 1        │ asset     │ gold.sales_wide                │ downstream of gold.dim_store   │ yes     │
│ 1        │ check     │ gold.dim_store/key_not_null    │ checks the key of              │         │
│          │           │                                │ gold.dim_store                 │         │
│ 1        │ check     │ gold.dim_store/key_unique      │ checks the key of              │         │
│          │           │                                │ gold.dim_store                 │         │
│ 1        │ reference │ gold.fact_sales.store_id →     │ references                     │         │
│          │           │ gold.dim_store.store_id        │ gold.dim_store.store_id        │         │
│ 2        │ check     │ gold.fact_sales/ref_store_id   │ checks the reference           │         │
│          │           │                                │ gold.fact_sales.store_id       │         │
│ 2        │ column    │ gold.fact_sales.store_id       │ references                     │         │
│          │           │                                │ gold.dim_store.store_id        │         │
│ 2        │ table     │ gold.sales_wide                │ downstream of gold.dim_store   │ yes     │
│ 3        │ table     │ gold.fact_sales                │ references                     │         │
│          │           │                                │ gold.dim_store.store_id        │         │
└──────────┴───────────┴────────────────────────────────┴────────────────────────────────┴─────────┘
Not shown: repository. Use --output json to see it.
may_use: yes means it may use the column or key without naming it (a check, a row filter or something downstream); SQL is not parsed, so look before you change it.
```

See [Find what a change breaks](find-what-a-change-breaks.md).

## Watch the table's health

Every snapshot Lakana commits records signals about the table (how many rows were added, how
many values are null, whether the columns changed), and a signal that fires only warns by default:

<!-- source: docs/examples/medallion/run.sh#health -->
```bash
lakana table health gold.fact_sales --env dev
```

While a table has only a few writes, as here, its volume and null signals read `no_history`:
there is nothing to compare with yet. To stop a run and set the table back when the row count falls outside its usual
band, uncomment the line `# health: {volume: block}` in the header of `fact_sales.sql`. A signal
that could not be computed yet (a table with too little history) never blocks. The signals,
effects and messages are in [Table health](../reference/table-health.md).

## What Lakana does not do

- **Data Vault.** The layers are bronze, silver and gold; the model is a star or a wide table.
- **A semantic or metrics layer.** A measure is a note on a column, not a computed metric, and
  there is none before 1.0.0.
- **No-code modelling.** A model is SQL in a repository, reviewed like code.
