# Find a table

After this guide you can find a table, a column, a run or a person from one text, in the
terminal, the SDK or through REST. You need an environment with at least one table (see
[Getting started](../tutorials/getting-started.md)).

`lakana search TEXT` looks for the text, in any case, anywhere in:

- the names of tables, views, external tables and artifacts, with or without the namespace
  (`silver.ord` finds `silver.orders`);
- the names of columns;
- the descriptions of tables and of columns (see [Describe a table](describe-a-table.md)); the
  text shown is the one `lakana table get` shows;
- the grain a table's asset header declares (`grain: One row per store and day`), which shows when
  you search for a part of that sentence;
- the label and description of a column's measure (`label: Sales amount`, see
  [Declare the measures](declare-a-tables-model.md#declare-the-measures)), which finds the column
  with `matched` `measure`;
- runs: the first four or more characters of a run's id, its job and its repository;
- people and service accounts, if you may list them.

## Search

<!-- source: docs/examples/find-a-table/run.sh#search -->
```bash
lakana search order
```

Each row says what was found (`kind`), its name, which field matched, and where it is: the kind of
a table, the type of a column, the repository and job of a run. A description that matched shows a
piece of it, at most 160 characters around the hit.

The best matches come first: a name that is exactly your text, then names that start with it, then
names that contain it, then descriptions that contain it. Among equals, tables come before columns,
columns before runs and runs before people.

## Look for one kind of thing

`--kind` takes `table`, `column`, `run` or `principal`:

<!-- source: docs/examples/find-a-table/run.sh#kind -->
```bash
lakana search customer --kind table
```

Descriptions are searched as part of tables and of columns, so `--kind table` finds a table by its
description.

## Look for one kind of model table

A table whose asset header declares a `kind` shows it in brackets after its name, as in
`gold.dim_store [dimension]`. `--model-kind` keeps only the tables of one kind (`fact`,
`dimension`, `wide` or `staging`) and leaves out columns, runs and people:

<!-- source: docs/examples/find-a-table/run.sh#model-kind -->
```bash
lakana search store --model-kind dimension
```

A table that declares no kind is never kept by this filter. See
[Asset front-matter](../reference/asset-front-matter.md) for what the header declares.

## Use the answer in a script

<!-- source: docs/examples/find-a-table/run.sh#json -->
```bash
lakana --output json search status --kind column
```

The JSON has `results`, `limit`, `offset` and `has_more`. When `has_more` is `true`, ask for the
next page with `--offset`; the command prints the exact command to run. It shows 20 results unless
you pass `--limit` (at most 100).

## What you see

You see only what you may read. A table and its columns appear only if you may read the table, and
so do its description, its grain and its kind: a text of a table you cannot read is never shown, not even as a piece of a
result. Runs need the right to read the environment. People appear only for a caller who may list
them (`MANAGE_PRINCIPAL`, which administrators hold). What you may not see is left out, not refused, so a search never tells you
that something exists.

Matching ignores case for ASCII letters on SQLite (local mode) and for all letters on Postgres, so
`äpfel` does not find `Äpfel` on SQLite.

A text that finds nothing says what search looks at. The facts (limits, ranking, the route, how
existing tables are filled in) are in [Search](../reference/search.md).
