# Show some people only some rows

After this guide a table shows only the rows that meet a condition to the people you choose, and
everyone else still sees every row. You also know how to list and remove a filter, and what
Lakana refuses when you create one. You need a table (see "Create the table first" below, and
[Getting started](../tutorials/getting-started.md)), a principal or a group to filter for, and an
administrator's profile: creating, listing and removing filters needs the `MANAGE_POLICY` action,
which only the `ADMIN` role includes, and creating one also needs `READ_TABLE` on the table so
that Lakana can check its columns.

A **row filter** says: when this principal (or anyone in this group) reads this table, they get
only the rows for which this condition is true. The data in storage does not change. The filter is
applied inside every query the person runs, so a join, a view, a `SELECT *` and a write into another
table all see only the filtered rows. It is the same idea as a [column mask](mask-a-column.md), for
rows instead of values, and it has the same limits.

## Create the table first

A filter names columns of the table, so Lakana checks them when you create it, and the table must
have them. Create the table with its columns, then its filters and masks, and only then write
data or give anyone `READ_TABLE`:

<!-- source: docs/examples/filter-rows/run.sh#table -->
```bash
lakana table create gold.customers --column id:long --column region:string --column tier:string
```

The order matters for safety. A grant on the environment or the namespace covers a table the
moment it exists, so rows written before the filter exists can be read unfiltered by everyone
that grant reaches. A table that already holds data works the same way; the filter applies from
its creation on.

## Add a filter

Name the table as `namespace.table`, the person or group, and the condition:

<!-- source: docs/examples/filter-rows/run.sh#create -->
```bash
lakana row-filter create gold.customers --principal alice --expression "region = 'EU'"
lakana row-filter create gold.customers --group sales_us --expression "region = 'US' AND NOT (tier = 'vip')"
```

Pass exactly one of `--principal` and `--group`; the principal or group must exist. The condition
is a small SQL expression. It may use:

- literals, comparisons (`=`, `<>`, `<`, `<=`, `>`, `>=`) and `AND`, `OR`, `NOT`;
- the **bare** names of the table's columns: write `region`, never `customers.region` or
  `t.region`. A field of a struct column (`address.country`) is not supported, but a filter on
  another column works on a table that has a struct column, and the struct comes back whole;
- `current_user()` and `has_group('name')`;
- a date or a timestamp written as a typed literal: `DATE '2026-01-01'`, `TIMESTAMP '2026-01-01 00:00:00'`
  (a fraction of a second may follow the seconds). Lakana checks the text when you create the filter,
  so a text that is not a date is refused then and not on every row.

Everything else is refused when you create the filter, for example `IN (...)`, `LIKE`, a function
call and a subquery. A condition on a column that holds `NULL` is not true for that row, so the row
is hidden. The condition is kept as written and may be at most 100,000 characters
(`LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS`).

Each column the condition reads must exist in the table now. Lakana compares names without regard
to letter case, as the query does, so `REGION = 'EU'` is accepted for a column called `region`. A
condition that reads no column (`has_group('staff')`) is not checked against the table.

## List and remove filters

<!-- source: docs/examples/filter-rows/run.sh#list -->
```bash
lakana row-filter list --principal alice
```

`list` takes `--principal`, `--group` or `--env` to narrow the list; it shows the id of each filter,
which `revoke` needs:

`$ROW_FILTER_ID` is the id that `list` printed:

<!-- source: docs/examples/filter-rows/run.sh#revoke -->
```bash
lakana row-filter revoke "$ROW_FILTER_ID" --yes
```

Revoking is the only way to change a filter: to change a condition, create the new filter and
revoke the old one. When several filters apply to one person, a row must pass all of them. The same
commands are `client.row_filters.create(...)`, `.list(...)` and `.revoke(...)` in the Python SDK
and `/api/v1/row-filters` in the REST API.

## An expression must not fail on any stored value

A filter runs on every stored value, also on the ones it hides. If it needs to convert a value, for
example because it compares a text column with a number, it fails on the first value that cannot be
converted, and then **every query the filter covers fails**. So Lakana checks the types of the
condition against the table's columns, as they are now, when you create the filter, and refuses a
condition that would need a conversion. The columns it checks are the ones of the schema the filter
records (see [After a rename](#after-a-rename)).

**Which tables get this check:** Iceberg tables (the ones Lakana writes) and external tables of
Parquet files, whose schema is stored with the file. An external table of CSV or JSON files has no
stored schema: Lakana only guesses the types from one sample file, and the query engine reads its
own, so a check against the guess would be wrong. A filter on such a table is checked against the
allowed SQL subset only, as before, and nothing guarantees that its comparisons cannot fail; write
`'007'` for a column of text that looks like a number, and check the values. The same holds for the
columns of a Parquet table whose type is not supported yet (unsigned integers, nested types):
filter on another column, or on an Iceberg table made with `lakana sql ... --into`.

A Parquet timestamp is read as a `timestamp` whatever its stored precision (seconds,
milliseconds or microseconds), so `ts > TIMESTAMP '2026-01-01 00:00:00'` works on it. A
**nanosecond** timestamp (pandas and pyarrow write these by default) can only be compared with a
date or timestamp from `1677-09-21 00:12:43.145225` to `2262-04-11 23:47:16.854775`, the range
the nanosecond type holds; a literal outside it is refused with that range. It cannot be compared
with a date column or a microsecond timestamp column. A timestamp with a time zone (any
precision) compares only with another such column.

This is refused (the exit code is 8):

<!-- output -->
```text
Error: 'region = 5' is not a valid filter expression: `region` is text; compare it with '5', not 5.
```

and `region = '5'` is accepted.

Lakana accepts a comparison only when the query engine's implicit conversion of the two sides
cannot fail on any stored value. What it refuses, each with a message that names the column, both
types and what to write:

- A comparison (`=`, `<>`, `<`, `<=`, `>`, `>=`) whose two sides are not of one kind: a text
  column against a number (`region = 5`), a number column against text (`id = 'x'`), two columns of
  different types (`region = id`), a boolean against a number, a date against text.
- A number column against a number that does not fit its type exactly:
  - an integer column (`int`, `long`) takes a whole number written in plain digits, within the
    type's range (`id = 5`, not `id = 1.5`, `id = 1e3` or a number too large for `int`);
  - a `decimal(p,s)` column takes at most `s` decimal places and `p - s` digits before the point,
    written without an exponent (`amount = 1.5` on `decimal(10,2)`, not `amount = 1.234`);
  - a `float` or `double` column takes any finite number within the type's range.
- Two number columns of different kinds. Integer columns compare with integer columns, `float` and
  `double` columns with each other, and two `decimal` columns only when they have the same
  precision and scale. An integer against a `decimal` or a `double` column, and two different
  decimals, are refused, because the engine would convert one of them and that conversion can fail.
- A `timestamptz` column against anything but another `timestamptz` column. The read does not fix
  a time zone, so a date or timestamp literal would select different rows on different hosts.
  Time-zone-aware literals are not supported yet; store the column as a `timestamp` to compare it
  with a literal.
- A comparison with `NULL` (`region = NULL`): it never matches a row and would hide every row.
  `IS NULL` is not supported yet.
- A column or value that is not boolean used as a condition, on its own or beside `AND`, `OR` or
  `NOT`: `region`, `NOT region`, `region AND id > 1`, `1`.
- A minus sign in front of anything but a number such as `-5` (`-id` can overflow).
- A column whose type Lakana cannot compare (a struct, a list, a map).

What stays accepted:

- A number column against a number it holds exactly (`id = 5`, `amount > 1.5`, `score < 2.5`), and
  columns of one kind (`id = small_id`, `score = ratio` for two float columns).
- Text against text, and `current_user()` against a text column.
- A date or a timestamp against a typed literal.
- A date against a timestamp (`born < seen`). A date outside the timestamp range (after 10 January
  294247 or before 22 December 290309 BC, as in a corrupt row) never fails a read: where it cannot
  be converted to a timestamp, the comparison counts as unknown and the row is hidden.
  A typed literal is checked when you create the filter and must be written exactly
  `DATE 'YYYY-MM-DD'` or `TIMESTAMP 'YYYY-MM-DD HH:MM:SS'` (with an optional fraction). The engine
  also reads `DATETIME '...'` and `TIMESTAMP WITHOUT TIME ZONE '...'` as a timestamp.
- `TRUE`, `FALSE`, a boolean column on its own, and `has_group('name')`.

A date column is compared with `DATE '2026-01-01'`, a timestamp column with
`TIMESTAMP '2026-01-01 00:00:00'`; text such as `'2026-01-01'` against a date column is refused,
because the engine would have to convert it. Lakana cannot convert for you (`CAST`, `try_cast` and `TRY` are refused; the typed
literals above are the exception, also when written as `CAST('...' AS DATE)`). If a comparison needs the value in another type, store the column in that type,
or add a column of that type and filter on that one.

If you change a column's type later (for example `decimal(10,2)` to `decimal(38,2)`), a filter made
earlier is not checked again; read it against the new type.

A filter that already exists is **not** checked again when you upgrade. To find one that compares
across types, list the filters (`lakana row-filter list`) and read the `expression` of each against
the table's columns; the upgrade notes say what to look for. To change an expression, revoke it and
create the new one.

When a stored value still makes a filter fail, everyone it applies to gets this message, and the
query engine's text is not shown:

<!-- output -->
```text
The query failed on a value it could not convert or compute, under a row filter or column mask on `gold.customers`; the value is not shown. Check the casts and functions in the query. If it has none that could fail, a row filter or column mask on the table compares a value with one of another type: ask an administrator to check it.
```

The same message appears when a person's own query fails on a value of a table under a filter or
mask. In their own query, `try_cast`, `try_strptime` and `TRY(...)` return
`NULL` instead of failing. Every other error on such a table also shows this message; errors on a table with no filter
or mask are shown as before.

## When Lakana refuses

Lakana checks a filter when you create it, so a mistake is an error now and not a failure of
someone's query later. The messages are the ones the commands print; the exit code is in brackets.

| You see | Why and what to do |
|---|---|
| `'t.region = 'EU'' is not a valid filter expression: the column 't.region' is written with a qualifier. Write the bare column name, 'region'; a row filter always reads the table it is set on.` (8) | A column name in a filter has a dot in front of it (`t.region`, `read_parquet.region`, `customers.region`). Write `region`. |
| `'address.country = 'DE'' is not a valid filter expression: 'address.country' reads a field of the column 'address', and a row filter does not support struct fields. Use a column of the table as a whole.` (8) | The first part of the dotted name is a column of the table, so it is a field of a struct column. Filter on a column as a whole. |
| `Table 'gold.customers' in environment 'dev' has no column 'regoin', which the row filter reads. Its columns are: id, region. Check the spelling.` (8) | The filter reads a column the table does not have. Pick one of the columns listed. If the table has no columns yet, the message says `The table has no recorded columns.` |
| `Table 'gold.customers' has no schema recorded yet, so its columns cannot be checked. Create the table with its columns first (lakana table create NAMESPACE.TABLE --column NAME:TYPE), then create the row filter, then grant READ_TABLE or write data.` (7) | Lakana has no schema for the table: it was dropped or replaced while you created the filter. Create the table with its columns first, then the filter, then write data, as at the top of this page. |
| `The columns of table 'gold.customers' could not be read; the server's log has the cause. Create the row filter again when the table is readable.` (7) | Storage or the table's metadata could not be read. Ask the person who runs the instance to look at the server's log, then try again. |
| `You need READ_TABLE on 'gold.customers' so that the column can be checked.` (5) | To check the columns, Lakana reads the table's columns with your rights. Ask for `READ_TABLE` on the table. |
| `'sha256(a)' is not a valid filter expression: ...` (8) | The condition uses something outside the list above. |
| `'region = 5' is not a valid filter expression: `region` is text; compare it with '5', not 5.` (8) | A comparison puts a text against a number, or two columns of different types. Write the value in the column's own type, as the message says. |
| `'region' is not a valid filter expression: `region` is text, not boolean; a condition is needed here. ...` (8) | A column that is not boolean stands where a condition is needed. Compare it with a value. |
| `'born = DATE '2026-13-01'' is not a valid expression: '2026-13-01' is not a date. Write DATE 'YYYY-MM-DD', ...` (8) | A typed date or timestamp literal that is not one. Write `DATE '2026-01-01'` or `TIMESTAMP '2026-01-01 00:00:00'`. |
| `Pass exactly one of --principal or --group` (2) | Name one person or one group, not both and not neither. |
| `No principal named 'nobody'.` (6) | The principal does not exist. Create it first, or fix the spelling. A group says `No group named '...'.` |
| `No table 'nope' in 'dev'/gold.` (6) | The table does not exist in that environment. Check the name and `--env`. |
| `'bob' may not MANAGE_POLICY this.` (5) | Your profile is not an administrator's. Ask one to create the filter. |
| `'gold.names' is a view; row filters and column masks apply to tables. Attach them to the view's base tables.` (8) | A view has no data of its own: filter the table the view reads. |
| `'gold.churn' is an artifact; row filters and column masks cannot be attached to an artifact. Whoever may read an artifact reads all of it.` (8) | A filter cannot cover a file. |
| `The row-filter expression is 100001 characters long; the most Lakana accepts is 100000 (LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS). ...` (8) | The condition is far longer than a filter needs. Shorten it. |

## Columns of a table made outside Lakana

Lakana's own writers make flat tables, but a table made by other tools can have a struct column
or a column whose type was widened. A filter names top-level columns only:

- **A struct column.** A filter on another column (`region = 'EU'`) works as on any table, and
  the struct column is returned whole. A filter that names a field of it (`address.country`) is
  refused, as the table above shows. To hide a struct column from someone, use a
  [column mask](mask-a-column.md) on the column as a whole.
- **A float column that was widened to double.** Rows written before the widening hold 32-bit
  values and rows written after hold 64-bit ones, so `1.1` is not the same number in both: read
  as a double, the old `1.1` is `1.100000023841858`. An equality with a decimal literal
  (`price = 1.1`) therefore matches only the rows written after the widening, and its negation
  (`price <> 1.1`) shows the old ones. Always write a range (`price > 1.09 AND price < 1.11`)
  in a filter. A query can also use a rounded value (`round(price, 2) = 1.1`). Both match the
  rows of old and new files alike, in either order.

## When a table's schema changes

Lakana reads an Iceberg table's data files by the field id of each column, as the Iceberg
specification requires, so a filter keeps working after the schema changes:

- **An added column** reads as NULL in the rows written before it was added. A filter on it
  (`tier = 'gold'`) passes none of those rows; `tier IS NULL` passes all of them.
- **A renamed column** keeps its data under the new name, in old and new files alike. The filter
  itself does not follow the rename: see the next section.
- **A column dropped and added again** under the same name is a new column. The rows written
  before read as NULL, never as the old column's values. A write with `evolve_schema` that
  changes a column's type (other than widening it) does the same.
- **A widened column** (`int` to `long`, `float` to `double`) reads in its new type. A float
  column needs a range in a filter, as said above.

A field inside a struct, list or map column is read by name. After such a field was renamed, or
dropped and added again, every read of the table fails with ``'gold.customers' cannot be read:
the field `town` of its column `address` was renamed, ...`` until its owner writes all of its rows
again (an overwrite). Nothing is shown in the meantime.

A raw-zone (external) table has no field ids: its files are read by column name. When some of its
files lack a column it declares, a read under a row filter of that table fails with
``The data files of `TABLE` do not all have the column `COLUMN` ...``, and nothing is shown
unfiltered. A column mask whose expression reads the column fails too, with a message that
depends on the order of the files; a constant mask works. Write the files again so that every
file has the column.

A column of a type Lakana cannot read yet (nanosecond timestamps, `unknown`, and the geospatial
types of Iceberg format version 3) makes every read of the table fail with ``'gold.events' has the
column 'x' of type timestamp_ns, which Lakana cannot read yet. Ask the owner of the table to store
the column as another type.`` The whole table is refused, also by a query that does not name the
column, so that nothing is hidden without a word. The owner stores the column as a type Lakana
reads (a microsecond timestamp, for example) and writes the table's rows again.

## Filters and dropped columns

A filter is bound to a column **name**, not to the column itself. When the column is dropped
and nothing takes its name, a query of the filtered person that reads the table fails instead of
showing rows. Revoke the filter and create it again for the column it is meant for. A rename is
different: see the next section.

### After a rename

A rename can change what a filter covers: a column renamed *onto* the filtered name would be
tested in the old column's place. The filter would test the other column's values and could pass
rows it was meant to hide, and the renamed column would no longer be filtered. Lakana does not let
that happen. Each filter records which version of the table's schema it was created against. When
a column was renamed since, or replaced by a new column of the same name (dropped and added
again), and the old or the new name is one the filter reads, a read of the table by the filtered
person is refused, and nothing is shown:

<!-- output -->
```text
`gold.customers` has a row filter or column mask that uses column `region`, and a column was renamed or replaced since the policy was created, so the policy may now cover a different column. Nothing is shown. An administrator must review the table's row filters and column masks, create each one that uses a renamed or replaced column again for the column it is meant for, and then revoke the old one.
```

A column is also replaced when a write with `evolve_schema` changes its type (not a widening, such
as `int` to `long`): the column gets a new field id, reads of the table are refused until the
filter is created again, and the rows written before read as NULL in that column. The run whose
write did it, or removed the column, shows a warning naming the filter's column, and to you, as an
administrator, the filter's id ([The run that breaks a row filter or column mask says so](../explanation/when-columns-change.md#the-run-that-breaks-a-row-filter-or-column-mask-says-so)).

A rename therefore goes in this order:

1. Rename the column. From then on, reads of the table by the filtered person are refused. That is
   the point: no rows are passed or hidden by a filter that is out of date.
2. Create a filter for the column it is meant for. Lakana only accepts a filter on a column the
   table has now, so it cannot be created before the rename. Reads are still refused, because the
   old filter is still out of date, and filters are ANDed, so the new one only narrows.
3. Revoke the old filter. List the filters and masks of the table with the `list` subcommand of
   `lakana row-filter` and of `lakana column-mask`.
4. Reads work again, with the filter on the right column.

Do not revoke the old filter first: until the new one exists, the person reads every row.

A filter created after the rename records the new version of the schema, so it is not refused. A
filter that an earlier release created has no recorded version: it is judged against every schema
the table has had, so a rename from any time before also refuses the read until it is created again.

A filter stored by an earlier release is judged by the same rule, even if its text would not be
accepted by `create` today.
