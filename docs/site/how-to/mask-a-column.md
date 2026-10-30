# Mask a column for some people

After this guide a column of a table shows a replacement value, such as `***`, to the people you
choose, and everyone else still sees the real value. You also know how to list and remove a mask,
and what a mask does not protect. You need a table (see "Create the table first" below, and
[Getting started](../tutorials/getting-started.md)), a principal or a group to mask it for, and an
administrator's profile: creating, listing and removing masks needs the `MANAGE_POLICY` action, which
only the `ADMIN` role includes.

A **column mask** says: when this principal (or anyone in this group) reads this column of this
table, give them this expression instead of the stored value. The data in storage does not change.
The mask is applied inside every query the person runs, so a join, a view, a `SELECT *` and a write
into another table all see the masked value.

Create the table with its columns first, then its masks and filters, and only then write data or
give anyone `READ_TABLE` (see [Create the table first](filter-rows.md#create-the-table-first)).

A mask hides a column only from the people it names. A person it does not name sees the real value
until a mask covers them. Group membership is looked up at every query: someone you add to a masked
group is masked from their next query, and someone you remove from it sees the real value again from
their next query.

## Add a mask

Name the table as `namespace.table`, the column, the person or group, and the replacement:

<!-- not tested: needs a running instance and an administrator's profile; packages/lakana-server/tests/cli/test_column_masks_howto.py runs these commands against the real server and checks what they print -->
```bash
lakana column-mask create gold.customers ssn --principal alice --expression "'***'"
lakana column-mask create gold.customers ssn --group support --expression "substr(ssn, 8, 4)"
```

The first command masks `ssn` for the principal `alice` with the text `***`; the second masks it for
the group `support` so that they see only its last four characters. Pass exactly one of `--principal`
and `--group`. The principal or group must already exist. `lakana column-mask create` prints the
new mask's id:

<!-- output -->
```text
Created column mask 49fa89ee-275a-4b86-a6fc-76b2b5b32b7e on dev/gold.customers.ssn
```

The replacement is a small SQL expression. It may use:

- literals (`NULL`, `'***'`, numbers) and comparisons and boolean logic;
- the **bare** names of columns of the same table, which give the stored value (`ssn`, never `customers.ssn`; a field of a struct column such as `address.country` is not supported; to cover a struct column, mask the column as a whole, for example `address` with the replacement `NULL`, and a query that reads a field of it gets `NULL`);
- `current_user()` and `has_group('name')`;
- `substr(...)` or `substring(...)`, the `||` operator that joins text, and `md5(...)`;
- a date or a timestamp written as a typed literal: `DATE '1970-01-01'`,
  `TIMESTAMP '1970-01-01 00:00:00'`.

Everything else is refused when you create the mask, for example `sha256(ssn)`, `upper(ssn)`,
`id + 1`, `CASE` and a subquery. The expression is kept as written and may be at most 100,000
characters (`LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS`).

## What a reader sees

Take the table `gold.customers`, with two rows, and the mask on `ssn` for `alice` made above. Alice
runs a query with her own profile:

<!-- not tested: needs a running instance and alice's profile; packages/lakana-server/tests/cli/test_column_masks_howto.py runs these commands against the real server and checks what they print -->
```bash
lakana --profile alice sql "SELECT id, name, ssn FROM gold.customers ORDER BY id"
lakana --profile bob sql "SELECT id, name, ssn FROM gold.customers ORDER BY id"
```

Alice, who has the mask, gets the replacement:

<!-- output -->
```text
┏━━━━┳━━━━━━━┳━━━━━┓
┃ id ┃ name  ┃ ssn ┃
┡━━━━╇━━━━━━━╇━━━━━┩
│ 1  │ Ada   │ *** │
│ 2  │ Grace │ *** │
└────┴───────┴─────┘
2 rows
```

Bob, who has no mask, gets the stored values:

<!-- output -->
```text
┏━━━━┳━━━━━━━┳━━━━━━━━━━━━━┓
┃ id ┃ name  ┃ ssn         ┃
┡━━━━╇━━━━━━━╇━━━━━━━━━━━━━┩
│ 1  │ Ada   │ 111-11-1111 │
│ 2  │ Grace │ 222-22-2222 │
└────┴───────┴─────────────┘
2 rows
```

The mask applies before the query's own conditions, so Alice cannot find a value by asking for it:
`WHERE ssn = '111-11-1111'` compares the masked value and returns no row. Other columns are not
masked: Alice still sees `name`. Mask every column that identifies a person.

When two or more masks apply to the same column of the same person (one made for her, one for a group
she is in), Lakana does not guess which should win. The column reads `NULL` for her.

## List and remove masks

<!-- not tested: needs a running instance and an administrator's profile; packages/lakana-server/tests/cli/test_column_masks_howto.py runs these commands against the real server and checks what they print -->
```bash
lakana column-mask list --principal alice
lakana column-mask revoke 49fa89ee-275a-4b86-a6fc-76b2b5b32b7e --yes
```

`list` takes `--principal`, `--group` or `--env` to narrow the list; it shows the id of each mask,
which `revoke` needs:

<!-- output -->
```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━┓
┃ id                                   ┃ principal_id                         ┃ group_id ┃ environment ┃ namespace ┃ table     ┃ column ┃ expression ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━┩
│ 49fa89ee-275a-4b86-a6fc-76b2b5b32b7e │ 52acdc6f-ad90-4c8f-9f92-e0f4c990b1b1 │          │ dev         │ gold      │ customers │ ssn    │ '***'      │
└──────────────────────────────────────┴──────────────────────────────────────┴──────────┴─────────────┴───────────┴───────────┴────────┴────────────┘
```

After `revoke` (without `--yes` it asks first) the person sees the stored value again on her next
query; `list` then prints `(none)`:

<!-- output -->
```text
Revoked column mask 49fa89ee-275a-4b86-a6fc-76b2b5b32b7e
```

Revoking is the only way to change a mask: to change an expression, create the new mask and revoke
the old one. Until you revoke the old one, the two masks on the column make it read `NULL`. The same
commands are `client.column_masks.create(...)`, `.list(...)` and `.revoke(...)` in the Python SDK
and `/api/v1/column-masks` in the REST API.

## An expression must not fail on any stored value

A mask runs on every stored value, also on the ones it hides. If it needs to convert a value, for
example because it compares a text column with a number, it fails on the first value that cannot be
converted, and then every query the mask covers fails. So Lakana checks the types of the expression
against the table's columns, as they are now, when you create the mask. The rules for a comparison
are those of a [row filter](filter-rows.md#an-expression-must-not-fail-on-any-stored-value): both
sides of one kind (a number column against a number it holds exactly, columns of one kind), a
boolean where a condition is needed.

A mask has one more rule: **it must give a value of the masked column's own type.** When a person
reads the table, Lakana casts the mask to the column's declared type, so a reader gets exactly the
type the column has without the mask (a `0` on a `long` column is a `BIGINT`, not an `INTEGER`). It
accepts a mask only when every value of the column it reads fits the masked column's type. If a
value stops fitting later (a mask `b` on an `int` column `a`, and `b` is widened from `int` to
`long`), the reader sees `NULL` for that value, and the column keeps its type; the read does not
fail. This is refused (the exit code is 8):

<!-- output -->
```text
Error: '0' is not a valid mask expression: `ssn` is text, but the mask is a number. Use a value of its own type: '***', NULL, or a text function such as substr(ssn, 1, 2).
```

and `'***'` is accepted for the same column. Which value keeps the type of which column:

| Column type | A mask that keeps it |
|---|---|
| text (`string`) | `'***'`, `NULL`, `substr(ssn, 1, 2)`, `'x' \|\| ssn`, `md5(ssn)`, `current_user()`, a text column |
| `tinyint`, `smallint`, `int`, `long` | `NULL`; a whole number in plain digits within the column's range (`0`, `-1`); an integer column that is not wider |
| `float`, `double` | `NULL`; a finite number within the type's range (`0`, `1.5`); a `float` column (for a `double` column also a `double` column) |
| `decimal(p,s)` | `NULL`; a number written without an exponent with at most `s` decimal places and `p - s` digits before the point; a `decimal` column with no more decimal places and no more digits before the point; an integer column whose largest value has no more than `p - s` digits (`int` 10, `long` 19) |
| `boolean` | `FALSE`, `NULL`, a comparison such as `id = 1`, `has_group('staff')` |
| `date` | `DATE '1970-01-01'`, `NULL`, a `date` column |
| `timestamp` | `TIMESTAMP '1970-01-01 00:00:00'`, `NULL`, a `timestamp` column |
| Parquet `timestamp[ns]` (nanoseconds) | `NULL`, a `timestamp` column of any precision, or `TIMESTAMP '1970-01-01 00:00:00'`; its own comparisons and functions are type-checked, but it is not cast, because it has no type to cast to |
| any other type (a struct, a list, `uuid`, `time`, `binary`, `timestamptz`) | `NULL`, or a column of the same type |

A number that does not fit is refused, for example `99999999999` on an `int` column, `1.234` on a
`decimal(10,2)` column, or a `long` column as the mask of an `int` column. An integer column is not
accepted as the mask of a `float` or `double` column.

How Lakana decides the type of a mask: a literal has its own type (`'x'` is text, `5` is an integer,
`1.5` a decimal number, `TRUE` a boolean, `DATE '...'` a date), a column has its declared type, and
these parts have a fixed type:

| Part | Type | It needs |
|---|---|---|
| `substr(x, start, length)`, `substring(...)` | text | `x` text; `start` and `length` whole-number literals from -2147483648 to 2147483647 (not columns, not `1.5`) |
| `x \|\| y` | text | both sides text |
| `md5(x)` | text | `x` text |
| `current_user()` | text | |
| `has_group('name')`, a comparison, `AND`, `OR`, `NOT` | boolean | conditions for `AND`, `OR`, `NOT` |
| `NULL` | the type of any column | |

A comparison inside a mask follows the same rules as in a row filter. A date does not fit a
timestamp column, or the other way round. An expression whose type Lakana cannot decide is refused,
so that nothing unchecked is stored.

A mask cannot convert a value (`CAST`, `try_cast` and `TRY` are refused; the typed literals
`DATE '...'` and `TIMESTAMP '...'`, which Lakana checks when you create the mask, are the exception,
also when they are written as `CAST('...' AS DATE)`). If a mask needs the value
in another type, store the column in that type, or add a column of that type and mask that one.

A mask that already exists is **not** checked again when you upgrade, and is not cast: it reads
as it always did. List the masks (`lakana column-mask list`) and compare each expression with the
type of its column; the upgrade notes say what to look for. To get the check and the cast for it,
create the mask again and revoke the old one. A column of a type with no cast (a struct, a list, a
map) can be masked with `NULL` or the column itself only.

**Which tables get the type check and the cast:** Iceberg tables and external tables of Parquet
files. A Parquet timestamp in seconds, milliseconds or microseconds is a `timestamp`; one in
nanoseconds is stored as not type-checked, as the table above says. A mask on an external table of CSV or JSON files is checked against the allowed SQL subset
only and is never cast: Lakana only guesses the types of such a table from one sample file, while the
query engine reads its own, so the mask behaves as masks did before the type check (it is stored as
not type-checked). The reader then gets the type the engine infers, which may differ from the
column's type in the table's schema.

When a stored value still makes a mask fail, everyone it applies to gets this message, and the query
engine's text is not shown:

<!-- output -->
```text
The query failed on a value it could not convert or compute, under a row filter or column mask on `gold.customers`; the value is not shown. Check the casts and functions in the query. If it has none that could fail, a row filter or column mask on the table compares a value with one of another type: ask an administrator to check it.
```

The same message appears when a person's own query fails on a value of a table under a filter or
mask. In their own query, `try_cast`, `try_strptime` and `TRY(...)` return
`NULL` instead of failing. Every other error on such a table also shows this message; errors on a table with no filter
or mask are shown as before.

## When Lakana refuses

Lakana checks a mask when you create it, so a mistake is an error now and not a failure of someone's
query later. The messages are the ones the commands print; the exit code of the command is in
brackets.

| You see | Why and what to do |
|---|---|
| `Table 'gold.customers' in environment 'dev' has no column 'sn'. Its columns are: id, name, ssn. Check the spelling; column names are case-sensitive.` (8) | The column is not in the table. Pick one of the columns listed. If the name differs only by case, the message ends with `Did you mean 'ssn'?` |
| `'read_parquet.ssn' is not a valid mask expression: the column 'read_parquet.ssn' is written with a qualifier. Write the bare column name, 'ssn'; a column mask always reads the table it is set on.` (8) | A column name in the expression has a dot in front of it (`t.ssn`, `customers.ssn`). Write `ssn`. A name whose first part is a column of the table says instead that a column mask does not support struct fields. |
| `'sha256(ssn)' is not a valid mask expression: SHA2 is outside the restricted subset.` (8) | The expression uses something outside the list above. Use only what the list allows. |
| `'0' is not a valid mask expression: `ssn` is text, but the mask is a number. Use a value of its own type: '***', NULL, ...` (8) | The mask has another type than its column. Use a value of the column's own type, as the message lists, or see the table above. |
| `'ssn = 5' is not a valid mask expression: `ssn` is text; compare it with '5', not 5.` (8) | A comparison inside the mask puts a text against a number. Write the value in the column's own type. |
| `Pass exactly one of --principal or --group` (2) | Name one person or one group, not both and not neither. |
| `No principal named 'nobody'.` (6) | The principal does not exist. Create it first, or fix the spelling. A group says `No group named '...'.` |
| `No table 'nope' in 'dev'/gold.` (6) | The table does not exist in that environment. Check the name and `--env`. |
| `'bob' may not MANAGE_POLICY this.` (5) | Your profile is not an administrator's. Ask one to create the mask. |
| `'gold.names' is a view; row filters and column masks apply to tables. Attach them to the view's base tables.` (8) | A view has no data of its own: mask the table the view reads. Whoever reads the view gets the masks of the tables beneath it. |
| `'gold.churn' is an artifact; row filters and column masks cannot be attached to an artifact. Whoever may read an artifact reads all of it.` (8) | A mask cannot cover a file. See "What a mask does not protect". |
| `You need READ_TABLE on 'gold.customers' so that the column can be checked.` (5) | To check the column, Lakana reads the table's columns with your rights. Ask for `READ_TABLE` on the table. |
| `Table 'gold.customers' has no schema recorded yet, so its columns cannot be checked. Create the table with its columns first (lakana table create NAMESPACE.TABLE --column NAME:TYPE), then create the mask, then grant READ_TABLE or write data.` (7) | Lakana has no schema for the table: it was dropped or replaced while you created the mask. Create the table with its columns first, then the mask, then write data (see "Create the table first" in [Show some people only some rows](filter-rows.md#create-the-table-first)). |
| `The columns of table 'gold.customers' could not be read; the server's log has the cause. Create the mask again when the table is readable.` (7) | Storage or the table's metadata could not be read. Ask the person who runs the instance to look at the server's log, then try again. |
| `The column-mask expression is 100001 characters long; the most Lakana accepts is 100000 (LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS). ...` (8) | The expression is far longer than a mask needs. Shorten it. |

A column added to the table later reads as NULL in the rows written before it, also under a
mask, and a renamed column keeps its data. See [Show some people only some rows](filter-rows.md#when-a-tables-schema-changes).

A mask is bound to a column **name**, not to the column itself. When the column is dropped and
nothing takes its name, every query of the masked person that reads the table fails with
`Column mask references unknown column 'ssn'; known columns: ['id', 'name']`. Revoke the mask and
create it again for the column it is meant for. A rename is different: see the next section.

### After a rename

A rename can change what a mask covers: a column renamed *onto* the masked name would be masked in
the old column's place, and the renamed column would show its values in clear under its new name.
Lakana does not let that happen. Each mask records which version of the table's schema it was
created against. When a column was renamed since, or replaced by a new column of the same name
(dropped and added again), and the old or the new name is one the mask uses (the masked column, or
a column its expression reads), a read of the table by the masked person is refused, and nothing
is shown:

<!-- output -->
```text
`gold.customers` has a row filter or column mask that uses column `ssn`, and a column was renamed or replaced since the policy was created, so the policy may now cover a different column. Nothing is shown. An administrator must review the table's row filters and column masks, create each one that uses a renamed or replaced column again for the column it is meant for, and then revoke the old one.
```

A column is also replaced when a write with `evolve_schema` changes its type (not a widening, such
as `int` to `long`): the column gets a new field id, reads of the table are refused until the mask
is created again, and the rows written before read as NULL in that column. The run whose
write did it, or removed the column, shows you, as an administrator, the mask's id, its column and
the revoke command; other readers of the run see only that the table's columns changed
([The run that changes a column says so](../explanation/when-columns-change.md#the-run-that-changes-a-column-says-so)).

A rename therefore goes in this order:

1. Rename the column. From then on, reads of the table by the masked person are refused. That is
   the point: nothing shows in clear while the mask is out of date.
2. Create a mask for the column it is meant for. Lakana only accepts a mask on a column the table
   has now, so it cannot be created before the rename. Reads are still refused, because the old
   mask is still out of date, and two masks on one column read as `NULL`.
3. Revoke the old mask. List the masks and row filters of the table with the `list` subcommand of
   `lakana column-mask` and of `lakana row-filter`.
4. Reads work again, with the mask on the right column.

Do not revoke the old mask first: until the new one exists, the renamed column would show its
values in clear.

A mask created after the rename records the new version of the schema, so it is not refused. A mask
that an earlier release created has no recorded version: it is judged against every schema the table
has had, so a rename from any time before also refuses the read until it is created again.

## What a mask does not protect

A mask is a way to keep a column out of the answers of Lakana's own queries. It is not
encryption, and it does not cover the following.

- **Files read directly.** Masks are applied by Lakana's query path. Anyone who holds credentials to
  the object storage by some other means, or who reads files through the Iceberg REST endpoint
  with storage credentials of their own, reads the raw files with no mask. Lakana's Iceberg REST
  endpoint does not give out storage credentials. Keep the storage credentials with the people who run
  the instance.
- **Artifacts.** A mask cannot be attached to a file. A model that was trained on the unmasked table
  can reveal what it was trained on to everyone who may read the artifact. Choose with that in mind who
  trains a model on a table that holds masked columns, and who may read the model.
- **Inference from aggregates.** A mask stops someone from reading a value. It does not stop them from
  working out a value by combining enough counts, sums and groupings of other columns. Lakana does not
  try to prevent that.
- **What the replacement gives away.** `substr` shows part of the value on purpose, and `md5` gives
  everyone with the mask a fixed fingerprint: for a value that has few possibilities, such as a
  birth date, anyone can compute the fingerprints of all of them and read the value back. Use `NULL` or
  a fixed text when the value must not be reconstructed.
- **Copies.** A write run copies what its author sees. If an unmasked person writes the real values
  into another table, that table has no mask until you create one; and a person who is masked writes
  only masked values.
- **People who can change masks.** The administrators who may create and revoke masks may take them
  away. An administrator is masked too, if a mask names them: nothing is exempt from the masks that
  apply to a person. A compromised administrator account is not a case Lakana defends against.
- **Python tasks under the local runner.** With `LAKANA_RUN__RUNNER=local`, and in local mode, a Python
  task runs as the worker's operating system user, with the task's own token as its only limit. Anyone
  who may deploy Python code can therefore read whatever the worker can read, including the files of
  every table and the credentials the worker holds, and masks do not apply to that. One Lakana instance
  is one trust domain: deploy Python code only from people you would trust with the stored data, or run
  one instance per client. With the docker runner (the Compose bundle's default) a task runs in a
  container without the worker's credentials, and its token is its limit
  ([Run Python tasks in containers](run-python-tasks-in-containers.md)).
- **Anyone no mask names.** See the start of this guide: a mask is added per person or per group.

Row filters (`lakana row-filter`) work the same way for rows: they hide the rows that fail a condition.
They have the same limits; see [Show some people only some rows](filter-rows.md).
