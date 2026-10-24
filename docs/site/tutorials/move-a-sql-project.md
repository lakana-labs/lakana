# Move a SQL project to Lakana

At the end of this tutorial you will have converted a small project of SQL files by hand into a
Lakana project, with a view, an incremental table, a table and five checks, deployed it to your
own machine, run it twice and queried the result. You will know where each part of a SQL-file
project went. The idea behind each step, and what has no counterpart in Lakana, are on the page
[Moving SQL transformations to Lakana](../explanation/moving-sql-transformations.md).

You need the instance of [Getting started](getting-started.md): installed, and started with
`lakana server start --local` in another terminal. The project here was written for this
tutorial; it is not anyone else's. Allow fifteen minutes.

## The project you start with

A small shop keeps its orders in a table that something else loads (`orders`) and has a list of
three regions in a CSV file. The SQL-file project that builds its report has:

- **a seed:** `regions.csv`, three rows, with a region code and a name;
- **a source:** the loaded `orders` table, declared in a schema file so that models can refer to it;
- **three models,** one SQL file each: a view that cleans the orders (it writes the status in lower
  case), an incremental table that adds the region name to every order and only adds orders that
  are new, and a table that sums the paid orders by region;
- **five tests** in the schema file: an order number is unique and present, a status is one of
  three values, a region code is a known region, and a custom query that finds a negative amount;
- **a profile** with two targets, `dev` and `prod`.

The models refer to each other and to the source with a function call inside the SQL. Lakana has
nothing like it: a file names the other table in plain SQL. Here is where each part goes.

| In the old project | In Lakana |
|---|---|
| the seed `regions.csv` | the same file, kept in git, loaded with a command |
| the source | the table `bronze.orders`, loaded with a command |
| the cleaning view | the asset `silver.orders_clean`, `materialize: view` |
| the incremental table | the asset `silver.orders_enriched`, `write: append` |
| the report table | the asset `gold.revenue_by_region` |
| the five tests | five files under `checks/silver/orders_enriched/` |
| the two targets | the environments `dev` and `prod`; this tutorial uses `dev` |

## Create the project

<!-- source: docs/examples/move-a-sql-project/run.sh#init -->
```bash
lakana init shop --no-sample
cd shop
```

`--no-sample` leaves out the three assets and the check that `lakana init` otherwise writes. It
writes `lakana.yaml` and the folders. Next to the folders, make a folder `data/` for the three
CSV files that follow. They are not part of what a deploy reads; they stay in git so that anyone
can load them again.

`data/regions.csv` is the seed:

<!-- source: docs/examples/move-a-sql-project/project/data/regions.csv -->
```text
region_code,region_name
N,North
S,South
E,East
```

`data/orders-1.csv` holds the first six orders. The status is written in different ways, as it
often is in data that someone else loads:

<!-- source: docs/examples/move-a-sql-project/project/data/orders-1.csv -->
```text
order_id,region_code,status,amount_cents,order_date
1,N,paid,2500,2026-03-01
2,S,Paid,1800,2026-03-01
3,N,refunded,900,2026-03-02
4,E,paid,1200,2026-03-02
5,S,paid,3000,2026-03-02
6,N,pending,700,2026-03-03
```

`data/orders-2.csv` holds two more orders, which arrive later:

<!-- source: docs/examples/move-a-sql-project/project/data/orders-2.csv -->
```text
order_id,region_code,status,amount_cents,order_date
7,N,paid,4000,2026-03-04
8,E,paid,800,2026-03-04
```

## Write the assets

An **asset** is one SQL file under `assets/`. Its path is the name of its table: the file
`assets/silver/orders_clean.sql` builds `silver.orders_clean`. The `/* lakana ... */` block on top
says what the table is and how it is built; the rest is the `SELECT`. All keys of the block are in
[Asset front-matter](../reference/asset-front-matter.md).

The view cleans the status. It reads `bronze.orders` by its name, where the old model called a
function with the name of the source:

<!-- source: docs/examples/move-a-sql-project/project/assets/silver/orders_clean.sql -->
```sql
/* lakana
description: The loaded orders with the status written one way.
materialize: view
columns:
  order_id: The order's number.
  region_code: The code of the region the order was placed in.
  status: paid, refunded or pending, in lower case.
  amount_cents: The order's amount in cents.
  order_date: The day of the order.
*/
SELECT
  order_id,
  region_code,
  LOWER(TRIM(status)) AS status,
  amount_cents,
  order_date
FROM bronze.orders
```

The incremental asset joins the region name to every order. `write: append` with
`watermark: order_id` makes a run add only the orders whose number is above the highest one
already in the table, and the first run creates the table with every order. It reads the view and the seed table by their names. The
`LEFT JOIN` keeps an order whose region is unknown, so that a check can find it instead of the
join hiding it:

<!-- source: docs/examples/move-a-sql-project/project/assets/silver/orders_enriched.sql -->
```sql
/* lakana
description: Every order with the name of its region. Each run adds the orders that are new.
write: append
watermark: order_id
columns:
  order_id: The order's number.
  region_name: The name of the region, empty when the region code is unknown.
  status: paid, refunded or pending.
  amount_cents: The order's amount in cents.
  order_date: The day of the order.
*/
SELECT
  orders.order_id,
  regions.region_name,
  orders.status,
  orders.amount_cents,
  orders.order_date
FROM silver.orders_clean AS orders
LEFT JOIN bronze.regions AS regions ON regions.region_code = orders.region_code
```

The report is a table, rebuilt in full on every run:

<!-- source: docs/examples/move-a-sql-project/project/assets/gold/revenue_by_region.sql -->
```sql
/* lakana
description: The paid orders and their revenue in each region.
columns:
  region_name: The region.
  orders: How many paid orders the region has.
  revenue_cents: The sum of the paid orders in cents.
*/
SELECT
  region_name,
  COUNT(*) AS orders,
  CAST(SUM(amount_cents) AS BIGINT) AS revenue_cents
FROM silver.orders_enriched
WHERE status = 'paid'
GROUP BY region_name
```

No file says which one is built first. Lakana reads the names in the `FROM` clauses and works out
that the view comes before the incremental table and the incremental table before the report.

## Write the checks

A **check** is a SQL file under `checks/<namespace>/<table>/` that returns the rows that are
wrong. No rows, the check passes. This is the form of all five old tests; the way to write each
kind is in [Check your data](../how-to/check-your-data.md#five-checks-to-copy). Put them in
`checks/silver/orders_enriched/`.

The order number is unique:

<!-- source: docs/examples/move-a-sql-project/project/checks/silver/orders_enriched/order_id_is_unique.sql -->
```sql
/* lakana
description: No order number appears twice.
*/
SELECT order_id, COUNT(*) AS copies
FROM silver.orders_enriched
GROUP BY order_id
HAVING COUNT(*) > 1
```

The order number is present:

<!-- source: docs/examples/move-a-sql-project/project/checks/silver/orders_enriched/order_id_is_present.sql -->
```sql
/* lakana
description: Every order has a number.
*/
SELECT order_date, amount_cents
FROM silver.orders_enriched
WHERE order_id IS NULL
```

The status is one of the accepted values (a missing status fails too):

<!-- source: docs/examples/move-a-sql-project/project/checks/silver/orders_enriched/status_is_known.sql -->
```sql
/* lakana
description: The status is paid, refunded or pending.
*/
SELECT order_id, status
FROM silver.orders_enriched
WHERE status IS NULL OR status NOT IN ('paid', 'refunded', 'pending')
```

Every order has a region. This was a test of the relation between two models:

<!-- source: docs/examples/move-a-sql-project/project/checks/silver/orders_enriched/region_is_known.sql -->
```sql
/* lakana
description: Every order belongs to a region in the regions table.
*/
SELECT order_id
FROM silver.orders_enriched
WHERE region_name IS NULL
```

The query of your own, which needs no new concept:

<!-- source: docs/examples/move-a-sql-project/project/checks/silver/orders_enriched/amount_is_not_negative.sql -->
```sql
/* lakana
description: An order never has a negative amount.
*/
SELECT order_id, amount_cents
FROM silver.orders_enriched
WHERE amount_cents < 0
```

A check that fails sets the table back to what it was before the run and stops what is built from
it (`on_failure: block`, the default). To add checks to a pipeline that already runs without
stopping it, set the default to `warn`: see
[Adding checks to a pipeline that already runs](../how-to/check-your-data.md#adding-checks-to-a-pipeline-that-already-runs).

## Load the seed and the source

A deploy needs the tables its assets read to exist, so the two tables that the assets read
come first. `bronze.regions` and `bronze.orders` are not built by any asset; you create them with
their columns and append the CSV files. The command reads each file with the columns' types, and
refuses a file with a column that does not fit before it sends anything:

<!-- source: docs/examples/move-a-sql-project/run.sh#load -->
```bash
lakana table create bronze.regions --column region_code:string --column region_name:string
lakana table append bronze.regions data/regions.csv
lakana table create bronze.orders --column order_id:long --column region_code:string \
  --column status:string --column amount_cents:long --column order_date:date
lakana table append bronze.orders data/orders-1.csv
```

A seed that changes later is loaded again with `lakana table replace`. In a real project the
orders would arrive from your loader, or from a [Python asset](../explanation/python-assets.md)
that fetches them, and not from a file you append by hand.

## Commit and deploy

<!-- source: docs/examples/move-a-sql-project/run.sh#commit -->
```bash
git init
git add -A
git commit -m "Orders, regions and five checks"
```

Register the repository, and ask for a dry run first. It validates the project and lists what a
deploy would add, without changing anything:

<!-- source: docs/examples/move-a-sql-project/run.sh#dry-run -->
```bash
lakana repo add shop --kind local --url "$PWD"
lakana deploy --env dev --dry-run
```

The list names three tables to add and the five checks. When it says something else, the message
names the file, the line, the cause and the next step; fix it, commit and try again. Then deploy:

<!-- source: docs/examples/move-a-sql-project/run.sh#deploy -->
```bash
lakana deploy --env dev
```

A deploy records the commit as a project version for the environment `dev`. [Getting
started](getting-started.md#deploy) shows what its output means.

## Run

<!-- source: docs/examples/move-a-sql-project/run.sh#run -->
```bash
lakana run --env dev
```

The run builds the three tables in order. After each table it runs that table's checks, and it
lists the result of each. The first run of the incremental asset creates its table with all six
orders.

## Query

<!-- source: docs/examples/move-a-sql-project/run.sh#query -->
```bash
lakana sql "SELECT * FROM gold.revenue_by_region ORDER BY revenue_cents DESC"
```

You will see this result:

<!-- output: move-a-sql-project#query -->
```text
┏━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━┓
┃ region_name ┃ orders ┃ revenue_cents ┃
┡━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━┩
│ South       │ 2      │ 4800          │
│ North       │ 1      │ 2500          │
│ East        │ 1      │ 1200          │
└─────────────┴────────┴───────────────┘
3 rows
```

South has two paid orders, one of them written `Paid` in the file, which the view turned into
`paid`. The refunded and the pending orders are not in the sum.

## New orders arrive

Load the second file and run again. You do not deploy again: a deploy is for new code, and a run
reads the tables as they are.

<!-- source: docs/examples/move-a-sql-project/run.sh#more-orders -->
```bash
lakana table append bronze.orders data/orders-2.csv
lakana run --env dev
```

The view reads all eight orders. The incremental asset adds only the two whose numbers are above
the highest it has, 6, and the report is rebuilt from all eight:

<!-- source: docs/examples/move-a-sql-project/run.sh#query-again -->
```bash
lakana sql "SELECT * FROM gold.revenue_by_region ORDER BY revenue_cents DESC"
```

<!-- output: move-a-sql-project#query-again -->
```text
┏━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━┓
┃ region_name ┃ orders ┃ revenue_cents ┃
┡━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━┩
│ North       │ 2      │ 6500          │
│ South       │ 2      │ 4800          │
│ East        │ 2      │ 2000          │
└─────────────┴────────┴───────────────┘
3 rows
```

North is first now, with the new order of 4000.

## See where a table comes from

Lakana worked out the lineage from the names in your SQL; you declared none of it:

<!-- source: docs/examples/move-a-sql-project/run.sh#lineage -->
```bash
lakana table lineage gold.revenue_by_region --env dev
```

It prints a tree of the tables that feed `gold.revenue_by_region`, starting with
`silver.orders_enriched`.

CI runs every command on this page on every change, and checks the two results above and what
the commands did to the tables: the six and then eight orders in the incremental table, and the
status of the runs.

## Where next

- Add a second environment, deploy the same commit to it and protect it:
  [Projects and deploys](../how-to/projects.md).
- Give a colleague the right to read only the report, with a row filter or a masked column:
  [Give a second person access](../how-to/give-a-second-person-access.md),
  [Show some people only some rows](../how-to/filter-rows.md).
- Start the run every morning: [Run a job on a schedule](../how-to/run-a-job-on-a-schedule.md).
- See what a failing check does: [Check your data](../how-to/check-your-data.md).
- What has no counterpart, such as macros and snapshots, and when to stay with what you have:
  [Moving SQL transformations to Lakana](../explanation/moving-sql-transformations.md).
