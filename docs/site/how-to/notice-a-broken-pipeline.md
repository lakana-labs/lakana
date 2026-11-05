# Notice a broken pipeline

Lakana watches every table it manages on its own: how fresh it is, how many rows each write adds,
how many values are null, which range a column stays in, and whether its columns change. It reads
this from each snapshot's metadata, never from the rows, and it needs no configuration. This page
shows how to read what it saw and how to make a signal stop a run. You need a project that you
have deployed once (see [Getting started](../tutorials/getting-started.md)).

The page works on the example `docs/examples/notice-a-broken-pipeline/` of the Lakana repository:
a table `bronze.orders`, a table `silver.orders` that is built from it, and a job `nightly` that
builds `silver.orders` and what it reads. Copy its `project/` folder into a new folder (without
the files `orders-v2.sql` and `orders-v3.sql`, which are the later versions of
`silver/orders.sql` that the page shows), or run `lakana init --no-sample` and add the files that
each step shows.

<!-- source: docs/examples/notice-a-broken-pipeline/run.sh#deploy-and-run -->
```bash
lakana deploy --env dev
lakana job run nightly --env dev
```

## See a table's signals

`lakana table health` shows the current state of each signal of a table, with the last
maintenance, the newest result of each check and who reads it:

<!-- source: docs/examples/notice-a-broken-pipeline/run.sh#health -->
```bash
lakana table health silver.orders --env dev
```

<!-- output: notice-a-broken-pipeline#health -->
```text
Last maintenance: none yet. Run it with: lakana table maintain silver.orders --env dev
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ check                     ┃ state  ┃ failing_rows ┃ finished_at               ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ health_schema (generated) │ passed │              │ 2026-11-05T00:33:12+00:00 │
└───────────────────────────┴────────┴──────────────┴───────────────────────────┘
Readers, last 30 days:
┏━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ principal ┃ read_count ┃ last_read_at                     ┃
┡━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ admin     │ 1          │ 2026-11-05T00:33:14.554101+00:00 │
└───────────┴────────────┴──────────────────────────────────┘
Signals:
┏━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ signal    ┃ column ┃ state      ┃ effect ┃ observed_at               ┃
┡━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ volume    │        │ no_history │ warn   │ 2026-11-05T00:33:12+00:00 │
│ null_rate │ id     │ no_history │ warn   │ 2026-11-05T00:33:12+00:00 │
│ null_rate │ region │ no_history │ warn   │ 2026-11-05T00:33:12+00:00 │
│ range     │ id     │ no_history │ warn   │ 2026-11-05T00:33:12+00:00 │
│ schema    │        │ ok         │ block  │ 2026-11-05T00:33:12+00:00 │
└───────────┴────────┴────────────┴────────┴───────────────────────────┘
```

The `Signals` table has one row per signal, and one per column for `null_rate` and `range`. The
`state` is one of:

- **`ok`:** the signal is inside what is usual for the table.
- **`fired`:** it is outside; the line `Fired:` under the table says how, with the usual value and
  the new one.
- **`no_history`:** the table has fewer than 8 snapshots so far, so there is no usual band yet.
  `schema` needs no history, which is why it is `ok` here and `volume` is not.
- **`no_metrics`:** Iceberg did not record what the signal needs for this write (the column's
  metrics, say), and Lakana never guesses.
- **`off`:** the signal is switched off for the table.

The `effect` column is what a firing does: `warn` by default, `block` for `schema` here, because
the header of `silver/orders.sql` says so. The bands, and what each signal observes, are in
[Table health](../reference/table-health.md#signals).

## Make a signal stop a run

The header of the asset asks for the `schema` signal to block:

<!-- source: docs/examples/notice-a-broken-pipeline/project/assets/silver/orders.sql -->
```sql
/* lakana
health: {schema: block}
*/
SELECT id, region FROM bronze.orders
```

Now someone adds the column `amount` to the `SELECT`. Replace `silver/orders.sql` with
`orders-v2.sql` and deploy and run again:

<!-- source: docs/examples/notice-a-broken-pipeline/run.sh#change -->
```bash
git add -A
git commit -q -m "Add the amount to silver.orders"
lakana deploy --env dev
lakana job run nightly --env dev
```

<!-- output -->
```text
Queued deploy 35965e28-ba00-40cf-8ea5-feb2a5c7f4f0
Deployed readings in dev at 6f88d1f50b9d03d18da160d9eb498c6a638b4a75
┏━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┓
┃ change      ┃ table         ┃
┡━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━┩
│ sql_changed │ silver.orders │
└─────────────┴───────────────┘
Hint: run 'lakana run --env dev' to run what you just deployed.
012bda68-6af7-4dc7-a3a8-17d6a1b7db40
bronze.orders  pending
silver.orders  pending
bronze.orders  succeeded  attempt 1/3  0.0s
silver.orders  failed  attempt 1/3  1.0s  failed: check_blocked. Next: Read the failed checks with 'lakana runs show RUN_ID', fix the data or the check, then run again.
Run 012bda68-6af7-4dc7-a3a8-17d6a1b7db40 failed
┏━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ asset         ┃ status    ┃ attempt ┃ error                                                                          ┃
┡━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ bronze.orders │ succeeded │ 1/3     │                                                                                │
│ silver.orders │ failed    │ 1/3     │ Check `silver.orders/health_schema` failed: health signal schema fired at      │
│               │           │         │ snapshot 5033228317085625887; `lakana table health silver.orders --env dev     │
│               │           │         │ --signals` shows its usual band and new value to readers who may see them; fix │
│               │           │         │ the data the asset reads, or let such data through with health: {schema: warn} │
│               │           │         │ in the asset header (assets/silver/orders.sql). The table was set back to what │
│               │           │         │ it was before this run, and nothing downstream was built. After a fix, deploy  │
│               │           │         │ and run again; if the project did not change: `lakana runs resume              │
│               │           │         │ 012bda68-6af7-4dc7-a3a8-17d6a1b7db40 --env dev`.                               │
└───────────────┴───────────┴─────────┴────────────────────────────────────────────────────────────────────────────────┘
Not shown: duration, rows. Use --output json to see them.

silver.orders failed: check_blocked
  A check of the table failed, so the data was not published.
  Next: Read the failed checks with 'lakana runs show RUN_ID', fix the data or the check, then run again.

silver.orders               FAILED   0 of 1 checks passed, rolled back
  health_schema (generated) FAILED   health signal schema fired at snapshot 5033228317085625887; `lakana table health silver.orders --env dev --signals` shows its usual band and new value to readers who may see them; fix the data the asset reads, or let such data through with health: {schema: warn} in the asset header
```

The run failed, so the command exits with status 1. A blocking signal is one more check of the
table, named `health_schema` and marked `(generated)`: it fired at the snapshot the run wrote, so
the run failed and the table was set back to what it was before the run, as with a failed
[blocking check](check-your-data.md#block-keep-or-warn). Nothing that reads `silver.orders` was
built, and readers still see the table with two columns. The message holds no value, because
everyone who may read the run reads it; the history has them:

<!-- source: docs/examples/notice-a-broken-pipeline/run.sh#signals -->
```bash
lakana table health silver.orders --env dev --signals --signal schema
```

<!-- output -->
```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┓
┃ observed_at               ┃ signal ┃ column ┃ state            ┃ effect ┃ snapshot_id         ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━┩
│ 2026-11-05T00:33:19+00:00 │ schema │        │ fired (set back) │ block  │ 5033228317085625887 │
│ 2026-11-05T00:33:12+00:00 │ schema │        │ ok               │ block  │ 8968045175124190761 │
└───────────────────────────┴────────┴────────┴──────────────────┴────────┴─────────────────────┘
Fired: silver.orders: schema changed: added amount (decimal(3, 1)) (snapshot 5033228317085625887, 05 Nov 00:33 UTC); 
check that what reads silver.orders still works, or set health: {schema: off} if such changes are expected
```

The newest row is the firing, `fired (set back)`: the snapshot the run wrote is no longer the
table's data, but the history keeps the row, so the reason for the stop stays there to look at.
The `Fired:` line names what changed: the column `amount` was added.

## Let a change through, or switch a signal off

If the new column is wanted, tell the table that such a change is fine. Replace the file with
`orders-v3.sql`, whose header says `schema: warn`, and deploy and run again:

<!-- source: docs/examples/notice-a-broken-pipeline/project/assets/silver/orders-v3.sql -->
```sql
/* lakana
health: {schema: warn}
*/
SELECT id, region, amount FROM bronze.orders
```

<!-- source: docs/examples/notice-a-broken-pipeline/run.sh#let-it-through -->
```bash
git add -A
git commit -q -m "Let the amount through"
lakana deploy --env dev
lakana job run nightly --env dev
```

The run succeeded and `silver.orders` has three columns. The `schema` signal fired again, because
it compares with the snapshot the table was set back to, and it now only warns: the table's
history shows `fired` with the effect `warn`. `warn` is the default, so `lakana table get` lists
no effect for the table.

The other forms of `health:` go in the same place, the `/* lakana */` block of an asset. Switch
every signal off for a table:

```yaml
health: off
```

Make `volume` block and leave `range` alone:

```yaml
health: {volume: block, range: off}
```

Name single columns of `null_rate` and `range`; the columns must be ones the `SELECT` returns:

```yaml
health: {range: {price: off}}
```

Write `off` without quotes: YAML reads it as false, which Lakana takes as off. To change a
signal for every table of the project, put `health:` in `lakana.yaml`. An asset's own entries win
over it:

```yaml
health: {null_rate: off}
```

`freshness` can warn or be off, but it cannot block: it fires between runs, when there is no
write to stop.

## Get told

A signal that fires and warns is sent to the `notify:` of every job that builds the table, with
the event `health`: the table, what changed and the command that shows the history. A signal that
keeps firing sends one message, not one per run. How to declare `notify:` and what the message
looks like: [Get told when a job fails](get-told-when-a-job-fails.md).

## Next

- [Table health](../reference/table-health.md): every signal, the band, what each effect does,
  freshness and how long signals are kept.
- [Check your data](check-your-data.md): state in SQL what must be true of the rows themselves.
