# Register files as an external table

After this guide you can make files that already lie under `raw/` in an environment's storage
readable as a table, on the command line or in the web interface, also in a protected environment,
and look under `raw/` to find them.
You need an environment and a namespace for the table (see
[Organise tables in namespaces](organise-tables-in-namespaces.md)).

## When to use one

Use an external table when another system already drops files into the environment's storage,
under `raw/`: a nightly export, a feed from a partner, files a sync tool copies. Registering them
copies nothing. The table reads the files where they lie, every time it is read, so a file added
later is read too, and assets and queries read the table by its name like any other
(`bronze.orders`). Registering needs `CREATE_TABLE` on the namespace.

For data that is not in the storage yet, load it with an asset instead (see
[Load data from a source](load-data-from-a-source.md)).

## Register one

With the files `raw/orders/2026-11-01.csv` and `raw/orders/2026-11-02.csv` in the storage of
`dev`, register the table `bronze.orders` over them:

<!-- source: docs/examples/register-files-as-an-external-table/run.sh#register -->
```bash
lakana table create-external bronze.orders --location 'raw/orders/*.csv' --format csv --env dev
```

<!-- output: register-files-as-an-external-table#register -->
```text
Created external table dev/bronze.orders
```

Quote the location, so that your shell leaves the `*` to Lakana. Lakana reads one matching file to
learn the table's columns and their types.

## The location rules

A location is a directory under `raw/` and a file pattern, such as `raw/orders/*.csv`:

- **The shape.** It starts with `raw/`, has at least one directory after it, and ends in a file
  name or a pattern. Only the last part may hold `*`, `?` or `[...]`, and `**` is refused. The
  server names what is wrong, such as `Location pattern 'data/orders/*.csv' must start with
  'raw/'.` or `Location pattern 'raw/*.csv' must have the shape 'raw/<dir...>/<glob>'.`
- **A directory belongs to one table.** The directory (`raw/orders`) is the table's own: no other
  external table in the environment may use it, or a directory inside it or above it. A second
  table there is refused with `That directory is already claimed by another table.` Give each
  table its own directory.
- **Only the directory's own files.** The pattern matches the files directly in the directory,
  never those in a directory below it: `raw/orders/archive/2026-10-01.csv` is not part of
  `bronze.orders`.
- **At least one file must match,** because Lakana reads one to learn the columns. Otherwise:
  `No file matches 'raw/returns/*.csv'.` Put the first file in place, then register.

## Formats

`--format` says how to read the files; every file of a table has the same format.

- `csv`: comma-separated values with a header row, which names the columns.
- `json`: one JSON object per line (often `.jsonl`); the keys name the columns.
- `parquet`: Parquet files, which carry their own columns and types.

## A name a deployed project declares

When a deployed project declares the name, by an asset `assets/bronze/orders.sql` for example,
only that project may create the table, and the registration is refused with the file that
declares it:

`` `bronze.orders` is declared by a deployed project. Defined in analytics/assets/bronze/orders.sql. Choose another name, or remove it there and deploy first. ``

If you may not read the name, the sentence is
`A deployed project defines this table; change it in git.`

## In a protected environment

A protected environment accepts writes only from runs of the deployed code (see
[Projects](projects.md#protected-environments-lockdown-and-break-glass)). To register a table there
you need `MANAGE_ENVIRONMENT` as well, and a reason of 10 to 500 characters on one line, which the
audit log records:

<!-- source: docs/examples/register-files-as-an-external-table/run.sh#protected -->
```bash
lakana table create-external bronze.orders --location 'raw/orders/*.csv' --format csv --env prod --break-glass --reason "The orders feed lands in prod first"
```

<!-- output: register-files-as-an-external-table#protected -->
```text
warning: break-glass write to prod: it is audited, and the next run of the deployed code may overwrite it; real fixes go through a deploy.
Created external table prod/bronze.orders
```

Without the two options the server refuses:
``Environment 'prod' is protected: only runs of the deployed project version may write. See `lakana run`.``

## In the web interface

Open **Catalog** and choose the namespace in the tree. **Actions** above the tree shows what you
may do there; choose **New external table…**. It is offered on a namespace you may create tables
in, never on an environment. The dialog "New external table in bronze" asks for:

- **Name**: the table's name without its namespace, such as `orders`.
- **Location**: it starts with `raw/`; complete it, such as `raw/orders/*.csv`, or press
  **Browse…** beside it to pick it (see below).
- **Format**: **CSV**, **JSON** or **Parquet**. It follows the location's ending (`.csv`, `.json`
  or `.jsonl`, `.parquet`, in upper or lower case) until you pick one yourself; with another
  ending, pick one.

Press **Register**, or Enter in a field once the button can be pressed. The new table's page opens.
The server judges the name and the location: a refusal, such as a location it refuses, no file that
matches or a name a deployed project declares, stays in the dialog word for word, with the fields
as you typed them. In a protected environment the dialog asks "Why you break the protection of
prod", the same reason `--reason` takes, and **Register** can be pressed once it is given.

### Browse… instead of typing

**Browse…** beside **Location** opens the folders of `raw/` under the field, one folder at a time:
each folder and file with its size, **Show more** for a long folder, and the trail of folders
above it to go back up.

- Choose a folder to open it.
- Choose a file to use its folder and every file there with its ending: `2026-11-02.csv` in
  `raw/feeds` gives `raw/feeds/*.csv`.
- **Use this folder** takes the ending most of the folder's files shown have (on a tie, one with a
  format, `.csv`, `.json`, `.jsonl` or `.parquet`, wins).

**Format** then follows the new location again, and you can still change it or edit the location
by hand, for any pattern. A file directly in `raw/` cannot be chosen: a table needs a folder below
it. A folder another external table uses, and everything in it, cannot be chosen either; it says
"in use by bronze.orders" when you may read that table, otherwise only "in use". A folder that
only holds another table's folder further down opens, so you can reach the free folders beside
it; registering that folder itself is refused, as the location rules say.

Browsing needs the same right as registering: `CREATE_TABLE` on at least one namespace of the
environment. Each folder you open is recorded in the audit log (see below).

## Look under raw/ from the command line

`lakana raw ls` lists a folder the same way, one page at a time:

<!-- source: docs/examples/register-files-as-an-external-table/run.sh#ls -->
```bash
lakana raw ls raw/orders --env dev
```

It prints each entry's name (a folder with a `/` after it), kind, size and the table that uses
it. `--limit` and `--cursor` page through a long folder; the last line names the command for the
next page.

## Through the APIs

- **REST.** `POST /api/v1/environments/{env}/namespaces/{path}/tables/external` with
  `{"name": "orders", "location_pattern": "raw/orders/*.csv", "format": "csv"}` registers one,
  and in a protected environment takes `break_glass_reason` in the body. It answers the table and
  the columns it read.
- **SDK.** `client.tables.create_external(env, "bronze.orders", "raw/orders/*.csv", "csv")`, with
  `break_glass_reason=` for a protected environment.
- **Listing raw/.** `GET /api/v1/environments/{env}/raw?path=raw/orders&cursor=&limit=` answers
  `{"path", "items": [{"name", "kind", "size", "claimed_by"}], "next_cursor"}`: `kind` is `folder`
  or `file`, `size` is `null` for a folder, and `claimed_by` is `null` for a free entry or
  `{"table": "bronze.orders"}` (`{"table": null}` for a table you may not read). It reads one
  level of one folder and one page of it (1 to 200 entries, 100 by default); a path outside
  `raw/` (a leading `/`, a `..`) is refused with 422, and a caller without `CREATE_TABLE` on any
  namespace of the environment gets 403. In the SDK: `client.raw.list(env, "raw/orders")`, or
  `client.raw.iter(env, "raw/orders")` for every entry.

## Afterwards

Dropping an external table removes the catalog entry only: its files stay, and it cannot be
restored; register it again (see [Drop and restore a table](drop-and-restore-a-table.md)).
`TABLE_CREATED` in the audit log records the registration, by the person who did it, from the
command line, the SDK or the web interface alike (see
[Export and keep the audit log](export-and-keep-the-audit-log.md)). Each folder of `raw/` someone
lists, with **Browse…**, `lakana raw ls` or the API, is recorded as `ACTIVITY_VIEWED` with the view
`raw_files` and the folder, once per person and folder within `LAKANA_VIEW_AUDIT__WINDOW_MINUTES`;
if that record cannot be written, the listing is refused with 503 and shows nothing.
