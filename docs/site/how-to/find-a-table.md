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

## In the web interface

Open **Catalog** in the sidebar. It lists every table you may read, one row each, in every
environment you may read, and leaves out the rest without saying that anything is missing. A
row shows:

- **Name**: the table's name, in the accent colour, with its kind, its namespace and its
  environment under it (`Fact · gold.sales`, `prod`). The kind is Table, View, External or
  Artifact, or Fact, Dimension, Wide or Staging for a table whose asset header declares one.
- **Owner**: the owner's initials and name, with "Service account" under it when it is not a
  person. A table whose owner was deleted shows a dash, and "No owner" for a screen reader.
- **Health**: the worst thing the table's checks found, in one word with a shape and a colour:
  a red diamond for a failed check or a blocking signal ("Check failed", "Blocked: late"), an
  amber triangle for a warning ("Late", "Rows dropped"), a green circle for "Healthy", and a
  grey dash for "Not judged". When more than one thing was found, the word names the worst and
  "+2" counts the others; they are listed on the table page. The words and what each means are
  in [Table list](../reference/table-list.md).
- **Rows** and **Size**: the rows of the newest snapshot and the size of its data files, right
  aligned.
- **Last write** and **Last read**: how long ago, such as "2 hours ago". Hover a time, or let a
  screen reader read it, for the exact one. Last read tells you when, never who.

A dash with its words ("Not known", "Never written", "Never read") means the figure does not
exist yet, as for a view. Press a row, or its name, to open the table.

Press a column heading to sort by it; press it again to reverse the order. The sorted heading
shows an arrow. **Sort by** at the top right offers the same columns and **Created**, which is
not a column, and **Order** names the direction in words that fit the key ("Newest first",
"Worst first"). The list starts sorted by name, A to Z, and a table with no value for the key
comes last. The sort is in the address, so Back returns to the previous order and a link
shares it.

### Narrow the list with the tree

Beside the list, the tree of places has **All tables** on top, then one entry per environment
you may read, and the namespaces under it (`sales` under `gold` for `gold.sales`). Each entry
ends with how many tables you may read in it (`prod · 6`). Environments start open and
namespaces closed; the arrow at the left of an entry opens or closes it. Press an entry to show
only the tables at and below it. The search and the sort stay as they are. On a narrow screen
the tree sits above the list in a box that scrolls on its own.

With the keyboard, the arrow keys move through the tree, Right and Left open and close an
entry, and **Enter** chooses it. An entry is a link, so you can also open it in a new tab.

### Search

The field above the list searches table and column names, labels and descriptions. The list
follows about a quarter of a second after you stop typing. It searches inside the place you
chose in the tree; the line above the list says where, for example "3 tables in gold match".
Press **Search all namespaces** to widen the same text to every environment and namespace you
may read. **Esc** clears the field.

A row whose match is not its name shows why on the second line, with the matching part marked:
`Column store_id · int · joins dim_store`, or `Description · One row per store visit`. A
translated text adds its language. When nothing matches, the page says so and offers **Search
all namespaces**. A search that reaches 5,000 tables stops and says so; use a longer text or
choose a namespace.

### Share a view, and come back to it

The place, the search and the sort are in the address, so a link shows the same list and Back
and Forward step through your choices. Opening **Catalog** without a query takes you to the
place you chose last. This browser keeps that choice for you, nobody else sees it, and a link
with its own place always wins.

If the address names an environment or namespace that you may not read or that does not exist,
the page says so and offers **Show all tables**. If you may read no environment yet, it says
"Ask an administrator for access."

The list shows 50 tables at a time. **Show more** adds the next 50 and moves you to the first
new row. The line above the list says "The first 50 tables" until the last page is loaded, then
"120 tables".

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
