# Keeping the history of a dimension

A dimension changes: a customer moves, a product changes category. A table written with
`replace` or `merge` keeps only the latest state, so last year's revenue by region quietly moves
to this year's regions. `write: history` keeps every version of every row instead (a "slowly
changing dimension of type 2"), and a fact can point at the version that was valid when its row
happened.

## What a run does

The asset's `SELECT` returns the **current state** of every row, as for a replace. Each run
compares it with the table's open versions and, in one snapshot:

- opens the first version of a key the table has not seen, which reaches back to the earliest
  time ([When a version starts](#when-a-version-starts));
- closes the open version of a key whose values changed and opens a new one;
- leaves a key whose values are the same alone;
- with `close_missing: true`, closes the open version of a key the `SELECT` no longer returns.

Nothing is deleted. A run that changes nothing commits nothing. A reader sees the table before
or after a run, never half of it.

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

After a first run, Grace moves from north to south, Edsger is new and Ada is gone from the CRM.
The second run leaves:

<!-- output: history#versions -->
```text
┏━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━┓
┃ customer_id ┃ name   ┃ region ┃ valid_from ┃ is_current ┃ closed ┃
┡━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━┩
│ C-001       │ Ada    │ north  │ 0001-01-01 │ False      │ True   │
│ C-002       │ Grace  │ north  │ 0001-01-01 │ False      │ True   │
│ C-002       │ Grace  │ south  │ 2026-02-01 │ True       │ False  │
│ C-003       │ Edsger │ west   │ 0001-01-01 │ True       │ False  │
└─────────────┴────────┴────────┴────────────┴────────────┴────────┘
4 rows
```

Each key's first version, Edsger's too, starts at 0001-01-01: a first version reaches back to the
earliest time, so that every order finds its customer ([When a version starts](#when-a-version-starts)).
Grace's move starts at the `changed_at` the CRM gave it.

## The header

| Key | Meaning | Rules |
|---|---|---|
| `write: history` | Keep every version. | Needs `kind: dimension`, with `grain:` and a `key:` (not `none`). The key is the natural key, at most 8 columns. No `watermark:`: the `SELECT` returns every row every time. No `evolve_schema`. |
| `valid_from:` | The column that says when a row's values took effect. | Optional. Without it a change starts its new version at the run's start time. A key's first version starts at the earliest time either way ([When a version starts](#when-a-version-starts)). A `date` (midnight UTC), a `timestamp` (read as UTC) or a `timestamptz`; never a key column. The column is not stored: its value is the version's `valid_from`, so `columns:` may not describe it and `references:` may not name it. |
| `close_missing:` | Close the open version of a key the `SELECT` no longer returns. | `true` or `false` (the default). The version is closed at the run's start time, never deleted. |

A Python asset gives the same keys to `@asset(write="history", valid_from=…, close_missing=…)`. The deploy's other rules for these keys are in
[Asset front-matter](asset-front-matter.md#rules).

## The columns Lakana adds

The table has the `SELECT`'s columns (without the `valid_from:` column), then:

| Column | Type | Description, as `lakana table get` shows it |
|---|---|---|
| `<table>_key` | `string`, required | Identifies this version: a hash of the key (`customer_id`) and `valid_from`. A fact can store it to point at exactly this version. |
| `valid_from` | `timestamptz`, required | When this version became valid (UTC); a key's first version starts at the earliest time, 0001-01-01T00:00:00Z, so that every row that references the key finds it. |
| `valid_to` | `timestamptz` | When this version stopped being valid (UTC); empty while it is the current version. |
| `is_current` | `boolean`, required | Whether this is the current version of its key. |

`<table>` is the last part of the table's name: `gold.dim_customer` has `dim_customer_key`. A
version is valid from `valid_from` up to, not including, `valid_to`; a closed version's
`valid_to` is the `valid_from` of the version that replaced it, or the run's start time when it
was closed because its key went missing. The `SELECT` may not return a column with one of these
names, in any letter case, and `columns:` may not describe them.

### The surrogate key

`<table>_key` is the same in every release, so a fact can store it:

1. The input is the bytes `lakana-history-key-v1` and a zero byte, then one part per key column in
   `key:` order, then one part for `valid_from`.
2. A part is a tag byte, the payload's length as 4 bytes big-endian, and the payload: `s` and the
   UTF-8 text for a string; `i` and the decimal digits for an integer; `n` and the digits with
   exactly the column's scale for a decimal (`12.50`; a negative scale has no fractional digits,
   so the payload is the plain integer); `d` and `YYYY-MM-DD` for a date; `t` and the
   microseconds since 1970-01-01T00:00:00Z for a timestamp and for `valid_from`; `b` and
   `true` or `false`; `x` and the bytes of a binary.
3. The key is the first 16 bytes of the input's SHA-256, as 32 lowercase hex characters.

For example, `customer_id = 'C-001'` with `valid_from` 2026-10-31T00:00:00Z
(1793404800000000 µs) gives `7e18e0a16407926388ca6000694864ce`, and its first version, with
`valid_from` 0001-01-01T00:00:00Z (-62135596800000000 µs), gives
`fe8185b5f5c48867dc8170a32ef03416`. A key column of another type (`float`, `double`, `time`, a
nested type) is refused when the run starts, with the cast to write (a `double` becomes
`CAST(price AS DECIMAL(18, 2))` or text), and so is a uuid (cast it with
`CAST(id AS VARCHAR)`) and a timestamp key finer than a microsecond (cast it with
`CAST(ts AS TIMESTAMP)`).

## How a change is found

Every column but the key and the `valid_from:` column is compared. Two values are the same when
both are NULL or both are equal: a NaN equals a NaN, `-0.0` equals `0.0`, a decimal compares at
its scale, and a string byte for byte, so `'Ada '` differs from `'Ada'`. A row whose values are
the same is skipped before its `valid_from` is looked at: a source that only moves its
timestamps opens nothing.

## When a version starts

**A key's first version** (a key the table has never held, in no version open or closed)
starts at the earliest time, 0001-01-01T00:00:00Z, with or without `valid_from:`. It reaches
back so that every row of a fact finds a version once the key exists, also an order placed
before the run that first saw its customer, and every row of a first load. The cost: the
`valid_from` of a first version is not when the key appeared. The run that added it is in
the table's snapshots and in the run history.

**Every later version** (a change, or a key that comes back after it was closed) starts at the
run's start time: the time the run began, the same for every table and every retry of one run.
With `valid_from:` it starts at that column's value instead. Then, for each new version:

- the value may not be NULL, nor later than the run's start time, nor finer than a microsecond
  (checked for a first version's row too, although its version starts at the earliest time);
- it must be later than the `valid_from` of the version it closes;
- a key that comes back after being closed may not start before its last version ended.

The last rule holds without `valid_from:` too, with the run's start time: a run that started
before another run closed the key cannot reopen it. A run that started before another run that
already wrote the table cannot replace or close a first version either, since that run may have
opened it with newer values. A run that would break one of these writes nothing (below).

## What a run can refuse

The messages give counts, never values. Nothing is written.

| When | Message |
|---|---|
| An empty result with `close_missing: true` | ``The asset's result is empty, and `close_missing: true` would close every open version of gold.dim_customer; nothing was written. If the source really is empty, remove `close_missing:` for this run.`` |
| A column named like an added one (a `SELECT *` is checked when it runs) | ``Column `is_current` of `gold.dim_customer`'s SELECT has a name `write: history` adds itself (valid_from, valid_to, is_current, dim_customer_key); rename it in the SELECT, for example `AS source_is_current`.`` |
| A key column of another type | ``key column `price` has type double; a history key must be a string, an integer, a decimal, a date, a timestamp, a boolean or binary. Cast it in the SELECT, for example `CAST(price AS DECIMAL(18, 2))` (with the scale its values need) or `CAST(price AS VARCHAR)`.`` For a type other than a `float` or `double`, the example is `CAST(… AS VARCHAR)` alone. |
| A nested column | ``Column `tags` has type list<string>; a history write compares every column and cannot compare this type. Turn it into a string in the SELECT, for example with `to_json(tags)`.`` |
| A `valid_from:` column of another type | ``valid_from column `changed_at` has type string; it must be a date or a timestamp`` |
| A `valid_from:` finer than a microsecond | ``valid_from column `changed_at` has timestamps finer than microseconds, which a version cannot start at; cast it in the SELECT, for example `CAST(changed_at AS TIMESTAMP)`.`` |
| A NULL `valid_from` | ``valid_from column `changed_at` is null in 4 rows; every version needs a start`` |
| A `valid_from` in the future | ``valid_from column `changed_at` is later than the run's start time (2026-10-31T06:00:00Z) in 2 rows; a version cannot start in the future.`` |
| A changed row that does not start later | ``valid_from column `changed_at` is at or before the open version's valid_from in 3 rows whose values changed; a new version must start after the one it closes. Fix the times in the source, or leave `valid_from:` out to use the run's start time.`` |
| An older run that commits after a newer one | ``2 open versions of gold.dim_customer start at or after this run's start time (2026-10-31T06:00:00Z); a run that started earlier cannot replace or close them. Start a new run.`` For first versions, which start at the earliest time: ``A run that started after this one (2026-10-31T07:00:00Z) already wrote gold.dim_customer, and this run (2026-10-31T06:00:00Z) would replace or close 2 first versions that it may have opened with newer values, so nothing was written. Start a new run.`` |
| A returning key that starts too early | ``valid_from column `changed_at` is before the end of the key's last version in 1 row that opens a key again; versions of one key must not overlap.`` Without `valid_from:`: ``1 row that opens a key again would start at this run's start time (2026-10-31T06:00:00Z), before the end of the key's last version; a newer run already changed gold.dim_customer. Start a new run.`` |
| A second write of one run | ``This run already wrote this table's history (the current snapshot of gold.dim_customer is its write), and a run opens at most one version per key. Start a new run to record the newer values.`` |
| A table its first history run did not create | ``gold.dim_customer exists without the columns `write: history` adds; a history table is created by its first history run. Drop it (`lakana table delete gold.dim_customer --env dev`) so that the next run creates it with every row as an open version, or build the history into a new asset.`` |
| Another key than the table was built with | ``gold.dim_customer keeps its history by `customer_id`, and the asset now declares `key: [customer_id, region]`; a history table keeps its key. Declare the old key again, or build the new key into a new asset (its history starts with its first run).`` |
| Columns added or removed | ``The result does not match gold.dim_customer's columns (added segment (string)); a history table keeps its columns. Make the SELECT return the columns and types gold.dim_customer has; to add a column, build a new history asset (its history starts with its first run).`` |
| Two open versions of a key | ``2 keys of gold.dim_customer have more than one open version; there must be one per key, so nothing was written. Drop the table (`lakana table delete gold.dim_customer --env dev`) and let the next run create it again (its history starts again with that run).`` Lakana's own runs never leave two open versions; a writer outside Lakana did. Drop the table as the text says, and the next run creates it with one open version per key. |
| Delete files from another Iceberg writer | ``gold.dim_customer has row-level delete files, which an Iceberg writer outside Lakana added; a history write cannot keep its versions right with them, so nothing was read or written. Compact the table with that writer so that no delete files remain, or drop it (`lakana table delete gold.dim_customer --env dev`) and let the next run create it again (its history starts again with that run).`` |
| More open versions than the setting allows | ``gold.dim_customer has 1200000 open versions, more than LAKANA_RUN__MAX_HISTORY_OPEN_VERSIONS (1000000) allows. A history write holds every open version in the worker's memory, roughly 2 GiB per million open versions of 8 columns and more for wider rows, so nothing was read or written. Ask an operator to raise LAKANA_RUN__MAX_HISTORY_OPEN_VERSIONS together with the worker's memory limit (LAKANA_WORKER__MEMORY_LIMIT).`` The same text starts ``The asset's result for gold.dim_customer has 1200000 rows, and each can open a version: more than …`` for a result with more rows than the setting, and ``gold.dim_customer would have 1000004 open versions after this write, more than …`` (ending "so nothing was written") for a write that would leave more open versions; see [Limits](#limits). |
| A null or repeated key | as for a merge, with the `QUALIFY` to write ([Merging rows](merge.md#errors)) |

## Checks

The header generates three key checks ([Generated checks](checks.md#generated-checks)):
`key_not_null`, `key_unique`, which for a history table returns every version that overlaps
another of its key, ends before it starts, or whose `is_current` disagrees with `valid_to`, and
`surrogate_key_unique`. `checks: key:` sets how all three fail. `keep` is refused: building the
table again never removes a version, so use `block`, which takes the failed run's versions out
again.

## References to a dimension that keeps history

A table that points at a history dimension says which version each row means, with the row's
time:

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

`at:` names a column of the table that references, the one that says when its row happened. The
check `ref_customer_id` then returns each order whose customer has no version valid at
`ordered_at` (a `date` counts as midnight UTC), and each order with a customer but no
`ordered_at`. `to:` takes what the plain form takes: `gold.dim_customer` for the key, or
`gold.dim_customer.dim_customer_key` for an order that stores the version's key. A history
dimension that references another uses its own start: `at: valid_from`, not the name of its
`valid_from:` column, which it does not store.

Because a key's first version reaches back to the earliest time, an order dated before the run
that first saw its customer still finds that version, and so does every order of a first load.
What fails is an order whose customer the dimension does not have yet: run the dimension first
(a project that builds both does), or fix the key. A deploy refuses a reference to a history
dimension without `at:`:

<!-- source: docs/examples/history/run.sh#refused -->
```bash
lakana deploy --env dev --dry-run
```

and the message names what to write: ``` `gold.fact_orders` references `gold.dim_customer` (`customer_id`), which keeps the history of its rows (`write: history`); say which version each row means with the row's time: `customer_id: {to: gold.dim_customer, at: <column>}` ```. The other texts are in [References](asset-front-matter.md#references).

`lakana table get` shows the time next to the reference (`(exists, valid at ordered_at)`), and
`lakana table references` has an `at` column in its "References" and its "Referenced by" lists;
REST and the SDK carry it as `at`.

## Who may write

The run's principal needs `WRITE_TABLE` and `READ_TABLE` on the table (a history write reads
every open version), and `CREATE_TABLE` for the first run. A history write is refused while a
row filter or column mask on the table applies to that principal: ``History writes into
gold.dim_customer are refused while a row filter or column mask applies to you on it: a history
write compares your rows with rows you can't see.`` Ask someone the table is not filtered or
masked for to run it, or ask an owner to lift the filter or mask for you.

## Audit

One `TABLE_WRITE` event per run that changed the table, in the same transaction as the commit:
`detail.mode` is `history`, `detail.versions_opened`, `detail.versions_closed` and
`detail.closed_missing` count what changed, `detail.rows` adds the first two, `detail.key` names
the key, `detail.valid_from` the column or `run`, and `detail.run_started_at` the run's start
time. The first run, which creates the table, records the event of a create.

## Limits

- The run holds its whole result and the table's open versions in the worker's memory while it
  compares them, so a large dimension counts against the worker's memory limit
  (`LAKANA_WORKER__MEMORY_LIMIT`). The result may have at most
  `LAKANA_QUERY_LIMITS__MAX_WRITE_ROWS` rows. The open versions may number at most
  `LAKANA_RUN__MAX_HISTORY_OPEN_VERSIONS` (1,000,000 by default). Before a run reads any
  version, both the table's open versions and the rows of its result must be within it (each
  row can open a version; the first run is checked before it creates the table). After the
  comparison, the open versions the write would leave must be within it too. A run over it
  writes nothing and names the setting. Raise it together with the worker's memory limit.
  On the REST path (a Python asset) the server holds the open versions, and
  `LAKANA_APPEND__MAX_CONCURRENT_HISTORY` (1 by default) bounds how many such writes run at once;
  one past it is refused with 503 and retried.
- Measured on one machine, with a dimension of 1,000,000 open versions of 8 columns, the worker's
  memory peaked at about 1,312 MiB (1.5 s) when 10,000 keys changed, 5,000 were new and 5,000 went
  missing, at 1,760 MiB (8.6 s) when half the keys changed and at 2,251 MiB (16.1 s) when all
  did: roughly 2 GiB per million open versions, and more for wider rows.
- After a history write the worker frees the memory it held (a garbage collection and a trim of
  the heap, about 2.5 s for a million versions; other threads of the worker pause during the
  collection). This is not a leak that grows with every write: a worker that started at 401 MiB held 659 MiB
  after one write of a million versions and 741 MiB after a second, so the second added about
  82 MiB, and the worker stays about 0.34 GiB above where it started.
- A key of several columns has no cap on its rows, unlike a merge.
- Adding a column to a history table is not possible yet: build a new history asset.
