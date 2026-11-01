# Keep the history of a dimension

When a customer moves, last year's orders should still count for the region the customer lived in
then. This page builds a customer dimension that keeps every version, and joins orders to the
version that was valid when each order was placed. Every option and message is in
[Keeping the history of a dimension](../reference/history.md).

## Build the dimension

Return the current state of every customer, and say when each row last changed:

<!-- source: docs/examples/history/project/assets/gold/dim_customer.sql -->
```sql
/* lakana
description: Every version of every customer.
kind: dimension
grain: One row per customer and version of their details.
key: customer_id
write: history
valid_from: changed_at
close_missing: true
*/
SELECT customer_id, name, region, changed_at FROM silver.customers
```

Leave `valid_from:` out if the source has no such column: a new version then starts when the run
began. Leave `close_missing:` out if a customer that disappears from the source should stay
current. A customer's first version always starts at 0001-01-01 UTC, so that every order finds it.

## Read the current customers

`is_current` marks the one open version of each customer. After the second run of the example
in the reference, Ada is gone from the source and her version is closed:

<!-- source: docs/examples/history/run.sh#current -->
```bash
lakana sql "SELECT customer_id, name, region FROM gold.dim_customer WHERE is_current ORDER BY customer_id" --env dev
```

<!-- output: history#current -->
```text
┏━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━┓
┃ customer_id ┃ name   ┃ region ┃
┡━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━┩
│ C-002       │ Grace  │ south  │
│ C-003       │ Edsger │ west   │
└─────────────┴────────┴────────┘
2 rows
```

## Join each order to the customer as they were

A version is valid from `valid_from` up to, not including, `valid_to`; the current version has
no `valid_to`. Join on the key and the time:

<!-- source: docs/examples/history/run.sh#joined -->
```bash
lakana sql "SELECT f.order_id, d.region FROM gold.fact_orders AS f JOIN gold.dim_customer AS d ON d.customer_id = f.customer_id AND d.valid_from <= f.ordered_at AND (d.valid_to IS NULL OR f.ordered_at < d.valid_to) ORDER BY f.order_id" --env dev
```

<!-- output: history#joined -->
```text
┏━━━━━━━━━━┳━━━━━━━━┓
┃ order_id ┃ region ┃
┡━━━━━━━━━━╇━━━━━━━━┩
│ 1        │ north  │
│ 2        │ north  │
└──────────┴────────┘
2 rows
```

Order 2 belongs to Grace's north version: it was placed before she moved south.

An order placed exactly when the customer moved belongs to the new version. A `DATE` column such
as `ordered_at` is compared as midnight UTC; cast it in the join if your times are in another zone.

The first version's `valid_from`, 0001-01-01, is outside the range of a nanosecond timestamp.
Converting `valid_from` to `timestamp[ns]` raises `ArrowInvalid`, and pandas before version 2
fails the same way; read it as microseconds, or keep the Arrow type, when you load the table in
Python.

To join many times, store the version once: build the fact with the join above, keep
`d.dim_customer_key` as a column of the fact, and join on that column alone
(`ON d.dim_customer_key = f.dim_customer_key`).

## Let a check prove every order finds its version

Declare the reference with the order's time, and the deploy generates a check that returns every
order whose customer had no version at that time:

<!-- source: docs/examples/history/project/assets/gold/fact_orders.sql -->
```sql
/* lakana
description: One row per order, pointing at the customer as they were when they ordered.
kind: fact
grain: One row per order.
key: order_id
references:
  customer_id: {to: gold.dim_customer, at: ordered_at}
*/
SELECT order_id, customer_id, ordered_at FROM silver.orders
```

A fact that stores the version's key points at it with
`customer_version: {to: gold.dim_customer.dim_customer_key, at: ordered_at}`; the check then
also proves that the stored version is the one valid at the order's time. A deploy refuses a
reference to a history dimension without `at:`, and says what to write.

## From a Python asset

A Python asset takes the same settings as arguments: `@asset(kind="dimension", key="customer_id",
write="history", valid_from="changed_at", close_missing=True, ...)`. See
[Python assets](../reference/history.md#python-assets).
