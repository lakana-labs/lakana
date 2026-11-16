# SQL in queries

Facts about the SQL a query, a view, a check or a SQL asset may contain, where Lakana's
behaviour differs from what you may expect from DuckDB alone. Lakana checks every statement
before DuckDB sees it and hands DuckDB its own rendering of what it checked.

## Lambdas

A lambda is an argument of a list function such as `list_transform`, `list_filter` or
`list_reduce`. Write it in any of these forms; all three mean the same:

| You write | Parameters |
|---|---|
| `x -> x + 1`, `(x, y) -> x + y` | one, or two in parentheses |
| `lambda x: x + 1`, `lambda x, y: x + y` | one or two |
| `LAMBDA x : x + 1`, `LAMBDA x, y : x + y` | one or two |

Lakana hands DuckDB every lambda as `LAMBDA x : x + 1`, the only form DuckDB 2.0 accepts
(it refuses `x -> x + 1` with "Deprecated lambda arrow (->) detected"). Queries, views and
SQL assets written with `->` keep running; there is nothing to change.

An arrow is a lambda only in a function that takes one: `list_transform`, `list_apply`,
`list_filter`, `list_reduce`, their `array_` names, `filter` and `COLUMNS`. Write `list_apply`
for DuckDB's `apply` and `list_reduce` for its `reduce`: Lakana cannot read `apply` with a
lambda, and reads `reduce` with its arguments in another order, so DuckDB refuses it. Anywhere else, as in `upper(j -> '$.a')` or `coalesce(j -> 'a', 'x')`, it is the JSON operator,
as in DuckDB, and runs as one. A lambda written `lambda x:` or with two parameters is always
a lambda.

A lambda's body is checked like the rest of the query: a function a query may not call is
refused inside a lambda too. A row filter or a column mask may not contain a lambda.

## Integer division and remainder by zero

`//` (integer division) and `%` (remainder) by zero stop the query with "Division by zero in
expression (...)". Wrap the expression in `TRY(...)` to get `NULL` instead, or divide by
`NULLIF(x, 0)`: `TRY(total // count)`, `total % NULLIF(count, 0)`. `/` by zero gives
`Infinity` (or `NaN` for `0 / 0`), as before.

Lakana ran on DuckDB 1.5 before, where both returned `NULL`; a query or asset that relied on
that now fails with the message above until you add `TRY` or `NULLIF`.

## `~` matches the whole text

`'abc' ~ 'b'` is false in Lakana: `~` and `!~` match the whole text, as `regexp_full_match`
does. DuckDB 2.0 alone treats `~` as a partial match. For a partial match write
`regexp_matches(x, 'b')`, or `'.*b.*'` as the pattern. `SIMILAR TO` matches the whole text
too.

## A table function's alias is its value

In `SELECT i FROM generate_series(1, 3) i`, `i` is the number (a `bigint`), so the query
returns the numbers 1 to 3 and a write stores them as a `long` column. The same holds for
`range` and `unnest`. `i.generate_series` still works. On DuckDB 1.5 `i` was a struct, which a
write refused.

## A column that is only `NULL` needs a type

`SELECT NULL AS x` has no type of its own. Give it one with `CAST`:
`CAST(NULL AS VARCHAR) AS x`, or the type the column should have in the table.

| Where | Without the `CAST` |
|---|---|
| A read | Runs; the column is empty. |
| A view | Is created; on DuckDB 2.0 the column's type reads `null`. |
| A write to a table | On DuckDB 2.0 refused with "Column 'x' is NULL without a type, which a Lakana table can't store; write `CAST(NULL AS <type>) AS x`, for example `CAST(NULL AS VARCHAR)`." On DuckDB 1.5 the column is created as an `integer`, which is rarely what you want. |

## Clauses after a table name

A `PIVOT (...)` or `UNPIVOT (...)` in the standard form, a `TABLESAMPLE` and a join inside
parentheses work on a table as in DuckDB:
`SELECT * FROM gold.sales PIVOT (sum(amount) FOR region IN ('eu', 'us'))`. They see the rows
after your row filters and column masks: a masked column pivots on its masked value.

These clauses have no meaning on a Lakana table and are refused with "'gold.sales' can't be read
with ...; remove it from the query.": `AT (...)`, `ONLY`, `WITH ORDINALITY`, a table hint such as
`WITH (NOLOCK)`, `FOR SYSTEM_TIME`, `PARTITION (...)`, `CHANGES (...)`, `ROWS FROM (...)`,
`INDEXED BY` and `LATERAL VIEW`.

## `TABLE t` and `SORT BY`

`TABLE gold.sales` is shorthand for `SELECT * FROM gold.sales`: it returns the rows your row
filters leave, with your column masks applied. `SORT BY` is refused with "SORT BY is not
supported; use ORDER BY." in a query, a SQL asset or a check; write `ORDER BY` instead.

Lakana before this release accepted `SORT BY` and ran the query without it, so a view created then
can hold one. A query over that view now fails with "View 'gold.v' could not be read. Ask its owner
to recreate it."; the owner creates the view again with `ORDER BY` in place of `SORT BY`.

## Recursive queries are refused

A query may not run recursively, in either spelling:

| You write | Refused with |
|---|---|
| `WITH RECURSIVE r AS (...)` | "Recursive CTEs (WITH RECURSIVE) are not supported in submitted queries." |
| `CONNECT BY`, with or without `START WITH`, `NOCYCLE`, `PRIOR`, `CONNECT_BY_ROOT` or `LEVEL` | "CONNECT BY is not supported in submitted queries: it runs as a recursive query. Write the query without hierarchical recursion, for example with one join per level." |

This holds anywhere in the statement, in a CTE, a subquery or a view. To walk a hierarchy of
known depth, join the table to itself once per level; for a deep or unknown depth, compute the
path in a Python asset that writes it to a table, and query that table.

## Functions a query may not call

A query reads tables, so a function that reads files, runs SQL from a string, reveals the
engine's settings or changes its state is refused, and the message names the function. Two
groups have a message of their own:

| Functions | Refused because | What to do |
|---|---|---|
| `setval`, `nextval`, `currval` | they change or read a sequence, which is the engine's state, not data | remove the call |
| `to_aggregate_state` | it picks an aggregate function by a name given as data | remove the call |
| any name starting with `__` | it is internal to DuckDB | write the SQL it stands for, such as `CAST` or `BETWEEN` |
| an operator called by name, such as `"<"(id, 'a')`, `main."="(a, b)` or `"IS DISTINCT FROM"(a, b)` | DuckDB does not check its types when it is called by name, and fails with an internal error | write the operator itself: `id < 'a'`, `a IS DISTINCT FROM b` |

## A view the checks now refuse

A view's SQL is checked when the view is created. A later Lakana may check more strictly, so a
view created earlier can hold SQL that is now refused. A query over such a view that fails tells
you which view it is:

`View 'gold.v' is refused by Lakana's query checks, which may have become stricter since it was created. Ask its owner to recreate it; the error then says what to change.`

The message shows none of the view's SQL, because you may not be allowed to read what the view
reads. Whoever owns the view creates it again with the same SQL: the refusal then names what is
not allowed (a function from the list above, for example), and they change the SQL until the
view is created.
