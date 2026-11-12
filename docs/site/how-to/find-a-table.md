# Find a table

After this guide you can find a table, a column, a run or a person from one text, in the
terminal, the SDK or through REST. You need an environment with at least one table (see
[Getting started](../tutorials/getting-started.md)).

`lakana search TEXT` looks for the text, in any case, anywhere in:

- the names of tables, views, external tables and artifacts, with or without the namespace
  (`silver.ord` finds `silver.orders`);
- the names of columns;
- the labels of tables and of columns, which rank like names; a translated label, description or
  grain matches in your language (`--lang`, else your profile's language), and the `matched`
  column shows a translation's language, `label [fr]` (see
  [Translate labels and descriptions](translate-labels-and-descriptions.md#find-a-table-by-its-label));
- the descriptions of tables and of columns (see [Describe a table](describe-a-table.md)); the
  text shown is the one `lakana table get` shows;
- the grain a table's asset header declares (`grain: One row per store and day`), which shows when
  you search for a part of that sentence;
- the label and description of a column's measure (`label: Sales amount`, see
  [Declare the measures](declare-a-tables-model.md#declare-the-measures)), which finds the column
  with `matched` `measure`;
- runs: the first four or more characters of a run's id, its job and its repository;
- people and service accounts, if you may list them.
- glossary terms (`kind` `term`) by their name, synonyms and definition, and the tables and columns a term
  links by the term's name and synonyms, with `matched` `term` (see [Keep a glossary](keep-a-glossary.md)).

## Search

<!-- source: docs/examples/find-a-table/run.sh#search -->
```bash
lakana search order
```

Each row says what was found (`kind`), its name, which field matched, and where it is: the kind of
a table, the type of a column, the repository and job of a run. A description that matched shows a
piece of it, at most 160 characters around the hit.

The best matches come first: a name or label that is exactly your text, then names and labels that
start with it, then those that contain it, then descriptions that contain it. A table or column is
listed once, under its best match. Among equals, tables come before columns,
columns before runs, runs before people and people before terms.

## Look for one kind of thing

`--kind` takes `table`, `column`, `run`, `principal` or `term`:

<!-- source: docs/examples/find-a-table/run.sh#kind -->
```bash
lakana search customer --kind table
```

Descriptions are searched as part of tables and of columns, so `--kind table` finds a table by its
description.

## Look for one kind of model table

A table whose asset header declares a `kind` shows it in brackets after its name, as in
`gold.dim_store [dimension]`. `--model-kind` keeps only the tables of one kind (`fact`,
`dimension`, `wide` or `staging`) and leaves out columns, runs, people and terms:

<!-- source: docs/examples/find-a-table/run.sh#model-kind -->
```bash
lakana search store --model-kind dimension
```

A table that declares no kind is never kept by this filter. See
[Asset front-matter](../reference/asset-front-matter.md) for what the header declares.

## Browse every table you may read

`lakana search` finds things of every kind; `lakana table list` without a namespace lists tables
only, one page at a time, each with its owner, its health in one word, its rows and size, and when
it was last written and last read. It lists the tables you may read in your environment, or in
every environment with `--all-environments`, and sorts them by what you want to know first:

<!-- source: docs/examples/find-a-table/run.sh#table-list -->
```bash
lakana table list --all-environments --sort last_write
```

`--sort` takes `name`, `owner`, `health`, `rows`, `size`, `last_write`, `last_read` and `created`;
a table with no value for it comes last. Add `--search TEXT` to keep the tables whose name, label,
description or grain, or a column's name, label, description or measure, contains the text, and
`--under NAMESPACE` to stay in one namespace and the ones beneath it. A `match` column then says
what matched, such as `column store_id: joins dim_store`:

<!-- source: docs/examples/find-a-table/run.sh#table-list-search -->
```bash
lakana table list --search order --under silver
```

The command prints at most 50 tables (`--limit`, at most 200). When there are more, the last line
is the exact command for the next page. The list shows only what you may read and leaves the rest
out without saying that anything is missing. Every column, the sorts, the REST routes and the SDK
calls are in [Table list](../reference/table-list.md).

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
