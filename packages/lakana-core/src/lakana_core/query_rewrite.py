"""Rewriting submitted SQL to read scoped files instead of catalog tables.

This is sqlglot's "policy rewriting" role: the executor never lets DuckDB resolve a table reference
against ambient storage. Every qualified table name in the query is replaced
with a CTE reading exactly the files the catalog already resolved for it.

Those file URLs are **never interpolated into the SQL**. A URL is derived
from a storage object key, and a raw-zone landing area accepts arbitrary
writes by design, so an object key is attacker-controlled text: a key
containing a single quote would close the string literal an interpolated
list built and run the rest of the key as SQL inside another principal's
authorized query. Instead each CTE reads `read_parquet(getvariable('v_<n>_files'))`
and the query process sets that variable to the URL list, bound as a value
(`SET VARIABLE "v_0_files" = ?`), before it runs the statement, which then
takes no parameters at all — see `RewrittenQuery`.

Those variables must in turn be unreachable from the user's own SQL: on S3
they hold presigned URLs of the raw files, and a caller holding one reads the
file directly, past every row filter and column mask. `getvariable` and
`duckdb_variables` are blacklisted, `json_serialize_plan` is blacklisted (it
plans the SQL text it is given, and the plan shows a variable's value), a `SET`
or `RESET` of a variable is a statement that is not a read and is refused, and
every `exp.Placeholder`/`exp.Parameter` in the user's tree is refused too
(`check_blacklist`, and again in every parse `rewrite_query`/`extract_table_refs`
make — see `_reject_placeholders`). That last rule is load-bearing, not
tidiness: DuckDB 2.0 reads the session variable of that name for a `$name`
nothing is bound to, so `SELECT $v_0_files` would return the URL list, and the
placeholder refusal is what keeps it out of reach.

A `TableSource`'s `row_filters` and `column_masks` are a second kind of data
reaching the generated SQL, and unlike the URLs they are spliced into the SQL
text directly rather than bound as a parameter. That is safe only because
they are trusted, not because this module checks them: both are required to
have already passed `lakana_core.policy.expressions.validate_restricted_expression`
and `lakana_core.policy.resolver.PolicyResolver`'s context substitution before
they ever reach a `TableSource`. This module performs no independent
validation of that expression text — see `TableSource` and `_reader_select_sql`.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import NamedTuple, ParamSpec, TypeVar

import sqlglot
import sqlglot.expressions as exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope, traverse_scope

from lakana_core.errors import LakanaError, ValidationError
from lakana_core.sql_syntax import SQL_SYNTAX_ERRORS

_BLACKLISTED_FUNCTIONS = frozenset(
    {
        "read_parquet",
        "parquet_scan",
        "read_csv",
        "read_csv_auto",
        "read_json",
        "read_json_auto",
        "read_json_objects_auto",
        "read_ndjson",
        "read_ndjson_auto",
        "read_ndjson_objects",
        "read_json_objects",
        "read_text",
        "read_blob",
        "read_duckdb",
        "iceberg_scan",
        "delta_scan",
        "sqlite_scan",
        "postgres_scan",
        "mysql_scan",
        "postgres_query",
        "postgres_execute",
        "mysql_query",
        "mysql_execute",
        "sqlite_attach",
        "glob",
        "query",
        "query_table",
        "sniff_csv",
        "parquet_metadata",
        "parquet_schema",
        "parquet_file_metadata",
        "parquet_kv_metadata",
        "parquet_full_metadata",
        "parquet_bloom_probe",
        "json_execute_serialized_sql",
        "json_serialize_plan",
        "which_secret",
        "current_query",
        "current_setting",
        "duckdb_settings",
        "duckdb_secrets",
        "duckdb_extensions",
        "duckdb_databases",
        "duckdb_memory",
        "duckdb_temporary_files",
        "duckdb_external_file_cache",
        "duckdb_logs",
        "duckdb_logs_parsed",
        "duckdb_log_contexts",
        "enable_logging",
        "duckdb_prepared_statements",
        "duckdb_variables",
        "getvariable",
        "duckdb_views",
        "duckdb_tables",
        "duckdb_columns",
        "duckdb_constraints",
        "duckdb_dependencies",
        "duckdb_indexes",
        "duckdb_schemas",
        "duckdb_sequences",
        "pragma_database_size",
        "pragma_metadata_info",
        "pragma_storage_info",
        "pragma_table_info",
        "pragma_show",
        "pragma_user_agent",
        "pragma_version",
        "pragma_platform",
        "sql_auto_complete",
        "stats",
        "duckdb_profiling_settings",
        "pg_get_viewdef",
        "pg_get_constraintdef",
        "pg_get_expr",
        "get_block_size",
        "index_key",
        "setval",
        "nextval",
        "currval",
        "to_aggregate_state",
    }
)
"""Function calls a user's own SQL must never make directly.

Defence in depth (spec Section 6.1): the executor's own rewrite (this module)
is what legitimately calls these, on SQL it constructs itself, never on the
user's original text — see `rewrite_query`, called only after this check
passes against the *original* SQL.

Every DuckDB alias of a file reader has to be listed, not just the canonical
name: `parquet_scan` is `read_parquet`, `read_csv_auto` is `read_csv`, and
`read_ndjson`/`read_ndjson_auto`/`read_ndjson_objects`/`read_json_objects`/
`read_json_objects_auto` are all `read_json` variants — missing any one of
them is a working bypass. `read_text`, `read_blob`, `read_duckdb` and `glob`
read and enumerate files of any type at all: `read_duckdb` opens another
DuckDB database file directly (its own catalog, not this connection's).

Everything from `query` onward is a second family, added as a hardening step (the full list came
from running `SELECT
function_name FROM duckdb_functions() WHERE function_type = 'table'`
against DuckDB 1.5.5, swept again on 2.0.0, and picking out every function that reads or evaluates
something other than the rows this module's own rewrite hands it):
`query`/`query_table`/`json_execute_serialized_sql` each evaluate a
*string argument* as SQL/a table reference — the AST-blacklist's whole
premise (block by function name) means nothing once a query can construct
and run new SQL from a string at runtime, and `json_execute_serialized_sql`
in particular takes `json_serialize_sql`'s own output, so a query naming
only `json_execute_serialized_sql` in its own text (nothing that looks like
`read_parquet` to an AST walk) can still read an arbitrary file once
executed — see `test_json_execute_serialized_sql_would_otherwise_read_another_files_rows`
for a reproduction against a raw connection. `json_serialize_plan` is a scalar that
plans the SQL text it is given, and DuckDB folds `getvariable(...)` into that plan, so it
would show the session variables that carry a table's file URLs; see
`test_json_serialize_plan_would_otherwise_show_a_variables_value`. `sniff_csv`/`parquet_metadata`/
`parquet_schema`/`parquet_file_metadata`/`parquet_kv_metadata`/
`parquet_full_metadata`/`parquet_bloom_probe` all read a named file's
structure or contents directly, the same class of bypass as `read_parquet`
itself, just reading metadata instead of rows. `which_secret` reveals which
configured credential DuckDB would use for a given path — not a file read,
but a secret-scope disclosure this module treats the same way.

Everything from `current_query` onward is a third family, added later,
and it is *introspection*: functions that return the connection's own state
rather than data. `current_query` is the one that matters most — it returns
the full *rewritten* statement as an ordinary result value, row-filter
predicates and column-mask expressions spliced into the CTEs included, plus
the names of the URL variables, and since a write run persists its
result, it could store that text in a table others read. The rest were
probed against DuckDB 1.5.5 through the real rewriter and executor:
`current_setting`/`duckdb_settings`/`duckdb_secrets`/`duckdb_extensions`/
`duckdb_databases`/`duckdb_temporary_files`/`duckdb_external_file_cache`
disclose worker settings, filesystem paths, secrets configuration or cached
file URLs; `duckdb_memory` and the `pragma_*` functions disclose host and
resource state; `duckdb_logs`/`duckdb_logs_parsed`/`duckdb_log_contexts`/
`enable_logging` can record and replay executed statements; and
`duckdb_views`/`duckdb_tables`/`duckdb_columns`/`duckdb_schemas` and their
siblings dump whatever this connection's catalog holds (today only DuckDB's
own built-ins, but a view or macro created inside the connection would show
its SQL); `pg_get_viewdef`/`pg_get_constraintdef`/`pg_get_expr`/
`get_block_size` are scalar macros over those same catalog dumps, and
`duckdb_profiling_settings` exposes profiling configuration. `getvariable`
reads a DuckDB variable of the connection, and the query path sets the values
a statement reads (file lists, the field-id map, a watermark) as variables
under names anyone can predict, so it is refused like `duckdb_variables`.
None has a use
in a governed query.

The last three entries came from the 2.0.0 sweep. `index_key` reads a catalog index by
name, the introspection class again. `setval` changes a sequence: the first scalar that
writes, where every other function a query may call only reads. `to_aggregate_state`
picks an aggregate function by a name given as *data*; nothing harmful was found behind
it, but dispatch by a name in a string is the class that made `json_execute_serialized_sql`
a bypass, and a governed query has no use for it. `nextval` (which advances a sequence)
and `currval` (which reads its state) predate 2.0.0 and are refused with `setval` for the
same reason: sequence state is connection state, not data, and no governed read needs it.

`stats` is the odd one out: it is a *scalar* that reads column statistics.
DuckDB takes Parquet statistics at bind time, below the rewriter's CTE
`WHERE`, so `stats(col)` reports the min and max of every row in the file,
including rows a row filter hides, and on a string column a prefix of the
hidden value (on 2.0.0 the whole hidden value); a user `WHERE` does not narrow it. Swept
against 1.5.5's and 2.0.0's `duckdb_functions()`: no other function reads statistics or
file metadata (the Parquet metadata readers are in the second family above). Table
functions and table macros that take a table *name* as a string
(`histogram`, `duckdb_table_sample`, `summary`) cannot see the rewriter's
CTEs, and `check_blacklist` refuses every table function outside
`_ALLOWED_TABLE_FUNCTIONS` in any `FROM` position anyway, so they are not
listed. The `pg_*` and `information_schema` views are table references that
fail catalog resolution rather than reaching DuckDB. Allowed on purpose, as
static *scalar* facts: `typeof`, `pg_typeof`, `version`, `current_database`,
`current_schema(s)` and `current_query_id`; `test_all_types` is a table
function, so it is refused like any other outside the allow-list.

**This list's criterion, not a completeness claim.** Every entry above is a
file-reading, SQL-evaluating, secret-revealing, introspecting,
statistics-reading or state-changing DuckDB function (table, scalar or macro) as of
DuckDB 2.0.0 — a *reference* list confirmed against that version's
`duckdb_functions()` output, not a permanent enumeration (the 2.0.0 sweep diffed
`duckdb_functions()` of 1.5.5 against 2.0.0, both with `httpfs` loaded: 225 names new, of
them 198 scalar, 18 table, 5 macro and 4 aggregate; the 18 table functions are refused by
`_ALLOWED_TABLE_FUNCTIONS`, and the scalars that pass were read one by one): DuckDB adds table
functions across releases, and a future one in any of these
categories needs adding here as it's found, the same way this residual
round found two the initial pass missed. This blacklist is deliberately
never the primary control for that reason — see this module's own
docstring and `lakana_worker.executor`'s: catalog resolution and credential
scoping are what make an unauthorized read structurally unreachable
regardless of which DuckDB function a query calls; this list only makes
the common, obvious bypasses fail with a clear message instead of a
confusing one.

`read_parquet` and `read_csv` parse to sqlglot's own dedicated expression
classes (`exp.ReadCSV`, `exp.ReadParquet`), whose `.sql_name()` returns the
real function name. The rest have no dedicated class — they parse as
`exp.Anonymous`, whose `.sql_name()` always returns the literal string
`"ANONYMOUS"` regardless of which function was called; `check_blacklist`
reads the real name from `.this` for those. Adding a name here that also
lacks a dedicated sqlglot expression class needs the same `exp.Anonymous`
handling, or it will silently bypass this check.

The `GLOB` *operator* (`WHERE path GLOB '*.csv'`) parses to `exp.Glob`,
which is a binary predicate and not an `exp.Func` at all, so listing the
`glob()` table function here does not reject it. Verified against sqlglot
30.18.0.

Beside this list, `check_blacklist` refuses every function whose name starts with
`_INTERNAL_FUNCTION_PREFIX` — see there.
"""

_LAMBDA_FUNCTIONS = frozenset(
    {
        "apply",
        "array_apply",
        "array_filter",
        "array_reduce",
        "array_transform",
        "filter",
        "invoke",
        "list_apply",
        "list_filter",
        "list_reduce",
        "list_transform",
        "reduce",
    }
)
"""The DuckDB functions that take a lambda argument.

The union, over DuckDB 1.5.5 and 2.0.0, of `SELECT DISTINCT function_name FROM
duckdb_functions() WHERE list_contains(parameter_types, 'LAMBDA')` (`invoke` is 2.0.0's only
addition). `COLUMNS(c -> …)` is the one other place DuckDB reads a lambda; sqlglot parses it to
`exp.Columns`. Everywhere else DuckDB reads `j -> 'a'` in a function's arguments as a JSON
extract, which sqlglot parses as a lambda: `_json_arrows_outside_lambda_functions` turns it back.
A new lambda-taking function in a DuckDB upgrade needs adding here, or its lambda becomes a JSON
extract that DuckDB refuses to bind: a failure, never a bypass.
"""


def _lambda_function_types() -> tuple[type[exp.Expr], ...]:
    """Find the sqlglot classes the functions of `_LAMBDA_FUNCTIONS` parse to.

    sqlglot gives some of them a class of their own (`list_transform` is `exp.Transform`); the
    rest stay `exp.Anonymous` and are matched by name. A name sqlglot cannot parse as a call
    (`apply`, a keyword to it) is refused at parse time anyway.

    Returns
    -------
    tuple of type
        The classes, `exp.Columns` included.
    """
    found: set[type[exp.Expr]] = {exp.Columns}
    for name in _LAMBDA_FUNCTIONS:
        for arguments in ("x -> x", "(x, y) -> x, 0"):
            try:
                parsed = sqlglot.parse_one(f"SELECT {name}(l, {arguments})", dialect="duckdb")
            except sqlglot.errors.SqlglotError:
                continue
            node = parsed.find(exp.Lambda)
            parent = None if node is None else node.parent
            if parent is not None and not isinstance(parent, exp.Anonymous):
                found.add(type(parent))
    return tuple(found)


_LAMBDA_FUNCTION_TYPES = _lambda_function_types()
"""`exp.Columns` and the sqlglot classes of the functions in `_LAMBDA_FUNCTIONS`."""


_SEQUENCE_REASON = "changes or reads a sequence, which is connection state, not data"

_NO_QUERY_USE_REASONS: dict[str, str] = {
    "setval": _SEQUENCE_REASON,
    "nextval": _SEQUENCE_REASON,
    "currval": _SEQUENCE_REASON,
    "to_aggregate_state": "picks an aggregate function by a name given as data",
}
"""Blacklisted functions refused with their own cause instead of "query the catalog instead".

Each is in `_BLACKLISTED_FUNCTIONS`; this only words the refusal. The catalog holds nothing
these calls were after, so the message names what the call does and says to remove it.
"""

_INTERNAL_FUNCTION_PREFIX = "__"
"""The prefix of DuckDB's internal function namespace, refused as a whole.

DuckDB 1.5.5 already registers `__internal_compress_*`/`__internal_decompress_*`, and 2.0.0
adds `__cast`, `__between`, `__internal_barrier`, the `__internal_tablefilter_*` family and
more: functions the planner inserts into its own plans, callable by name all the same. No
governed query needs one, each has a documented SQL form (`CAST`, `BETWEEN`) or none at
all, and a prefix rule refuses the next release's additions without a list entry. DuckDB
registers no function whose name starts with a single `_`, so the rule is exactly this
namespace. A name is compared in lower case, after sqlglot has taken off quotes and any
catalog or schema qualifier, like the entries of `_BLACKLISTED_FUNCTIONS`.
"""


def _is_operator_name(name: str) -> bool:
    """Tell whether a called function's name is an operator rather than a word.

    DuckDB 2.0 lists its operators as functions (`"<"`, `"||"`, `"IS DISTINCT FROM"`, ...),
    callable by name, quoted and qualified (`main."<"(a, b)`, `a."<"(b)`). Called by name with
    a text and a number, a comparison binds and then fails DuckDB's own type check with an
    internal error that leaves the query process's database unusable (#293). No query needs an
    operator by name, and every function DuckDB lists whose name is not made of letters, digits
    and `_` is an operator, so `check_blacklist` refuses each of them, also one a later DuckDB
    adds.

    Parameters
    ----------
    name
        The function name as written, without its schema.

    Returns
    -------
    bool
        True when the name holds a character other than a letter, a digit or `_`.
    """
    return not all(character.isalnum() or character == "_" for character in name)


def _operator_call_message(name: str) -> str:
    """Word the refusal of an operator called by name.

    Parameters
    ----------
    name
        The function name as written.

    Returns
    -------
    str
        The message, naming the call and the form to write instead.
    """
    return (
        f'"{name}" is an operator called by name, which DuckDB does not check for types; '
        "write the operator itself between its operands instead, such as id < 'a' or "
        "a IS DISTINCT FROM b."
    )


_BLACKLISTED_COMMANDS = frozenset({"load", "create"})
"""Leading keywords of statements sqlglot parses to a generic `exp.Command`.

`LOAD <extension>` and `CREATE SECRET ...` are both unsupported syntax in
sqlglot's duckdb dialect, so neither gets a dedicated expression class: both
fall back to `exp.Command`, whose `.this` is the leading keyword. `LOAD`
pulls in an extension the connection deliberately did not load, and `CREATE
SECRET ... PROVIDER credential_chain` re-acquires the worker's own ambient
cloud credentials that `lakana_worker.executor` just cleared. Naming them
gives a precise error; the `_TOP_LEVEL_READS` requirement in `check_blacklist`
rejects them regardless.
"""

_RECURSIVE_ARG = "recursive"
"""sqlglot's argument key for the `RECURSIVE` keyword on a `WITH` clause.

Read from `sqlglot.expressions.With.arg_types`, not assumed: the key is
`"recursive"` and its value is `True` only when the keyword was actually
written, `None` otherwise. Verified against sqlglot 30.18.0.
"""

_RECURSIVE_CTE_MESSAGE = "Recursive CTEs (WITH RECURSIVE) are not supported in submitted queries."
"""Why a `WITH RECURSIVE` query is refused, stated once for all three callers."""

_CONNECT_BY_MESSAGE = (
    "CONNECT BY is not supported in submitted queries: it runs as a recursive query. "
    "Write the query without hierarchical recursion, for example with one join per level."
)
"""Why a hierarchical query (`CONNECT BY`, `PRIOR`, `CONNECT_BY_ROOT`) is refused."""

_CONNECT_BY_NODES: tuple[type[exp.Expr], ...] = (exp.Connect, exp.ConnectByRoot, exp.Prior)
"""The nodes sqlglot parses Oracle's hierarchical-query syntax to.

`exp.Connect` (`[START WITH ...] CONNECT BY [NOCYCLE] ...`) hangs off a plain `exp.Select`;
sqlglot's duckdb generator writes it as a `WITH RECURSIVE` CTE only when it renders the tree,
so the parsed tree the guard checks carries no recursive `WITH` at all. `exp.Prior` and
`exp.ConnectByRoot` mean nothing outside such a query and are refused with it. Verified against
sqlglot 30.23.0.
"""

_PLACEHOLDER_MESSAGE = "Query parameters/placeholders are not allowed in a submitted query."
"""Why a query carrying a placeholder is refused.

Deliberately fixed text: it never echoes the placeholder, the query, or any
resolved file URL back to the caller.
"""

_NOT_A_READ: tuple[type[exp.Expr], ...] = (
    exp.DML,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Attach,
    exp.Detach,
    exp.Install,
    exp.Use,
    exp.Set,
    exp.Pragma,
    exp.Command,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Comment,
    exp.Describe,
    exp.Show,
    exp.Summarize,
    exp.Analyze,
    exp.Export,
    exp.Kill,
    exp.Cache,
    exp.Uncache,
    exp.Refresh,
    exp.LoadData,
    exp.Into,
)
"""sqlglot's classes of statements that are not a read, refused at any depth of a query.

`exp.DML` covers `INSERT`, `UPDATE`, `DELETE`, `MERGE` and `COPY`. `exp.Into` is `SELECT ... INTO`,
which the rewrite would render as `CREATE TABLE ... AS`. `exp.Command` is what
sqlglot falls back to for a statement it has no class for (`VACUUM`, `RESET`). Checked
against sqlglot 30.22: a `SELECT`, a set operation, `VALUES`, `PIVOT`, a DuckDB `FROM`-first
query and a parenthesised join contain none of these classes.
"""

_READ_BODIES: tuple[type[exp.Expr], ...] = (exp.Query, exp.Pivot)
"""What the body of a CTE may be. sqlglot reads `WITH m AS (CHECKPOINT)` as a CTE whose body
is a column named `CHECKPOINT`, which no class above matches, so the body is checked too."""

_NOT_A_READ_MESSAGE = (
    "A query reads data and cannot change it: `{keyword}` is not allowed, also not inside a "
    "CTE or subquery."
)
"""Why a query holding a statement that is not a read is refused."""

_KEYWORD_CHARS = 40
"""The most characters of a refused statement's leading word the message repeats."""

_READERS = {"parquet": "read_parquet", "csv": "read_csv", "json": "read_json_auto"}
"""Maps a raw-zone table's stored file format to the DuckDB reader function."""


class ReadField(NamedTuple):
    """One column of an Iceberg read, as the field-id map hands it to DuckDB.

    Parameters
    ----------
    field_id
        The column's Iceberg field id.
    name
        The column's name in the schema the read uses.
    duckdb_type
        Its DuckDB type, as DuckDB's type parser reads it (`BIGINT`, `STRUCT("a" INTEGER)`).
    """

    field_id: int
    name: str
    duckdb_type: str


def _schema_variable(index: int) -> str:
    """Name the variable one read's field-id map is set under.

    Parameters
    ----------
    index
        The read's number, as for `_files_variable`.

    Returns
    -------
    str
        ``"v_<index>_schema"``.
    """
    return f"v_{index}_schema"


def _files_variable(index: int) -> str:
    """Name the variable one read's file list is set under.

    The name is predictable on purpose: no user SQL can reach a variable, so a random part would
    protect nothing. `getvariable` and `duckdb_variables` are blacklisted; `json_serialize_plan`
    is blacklisted too, because a function that plans SQL given to it as a value folds
    `getvariable(...)` into the plan it returns; `SET` and `RESET` are refused; and every
    placeholder is refused, because DuckDB 2.0 reads the variable `v` for an unbound `$v` (see
    `_reject_placeholders`).

    Parameters
    ----------
    index
        The read's position among the reads of this rewrite that have files.

    Returns
    -------
    str
        ``"v_<index>_files"``.
    """
    return f"v_{index}_files"


_WITH_ARG = "with_"
"""sqlglot's argument key for a query's `WITH` clause.

Read rather than assumed: `sqlglot.expressions.Query.ctes` reads this exact
key, so a rename in sqlglot breaks `ctes` too and this module's own tests
fail loudly rather than silently dropping the caller's CTEs.
"""

_TOP_LEVEL_READS: tuple[type[exp.Expr], ...] = (exp.Query, exp.Values)
"""What a submitted statement as a whole may be: a query, or a bare `VALUES` list.

sqlglot parses a top-level `VALUES (1), (2)` to `exp.Values`, which is not an `exp.Query`;
DuckDB runs it as an ordinary read. Every other check walks the whole tree, so a `VALUES`
list gets each of them exactly as a `SELECT` list does.
"""

_VALUES_ALIAS = "__lakana_values"
"""The alias a bare `VALUES` list takes when `_parse_checked` makes it a `SELECT` of it."""

_ALIASED_VALUES_MESSAGE = (
    "A VALUES list on its own takes no alias; to name its columns, write "
    "SELECT * FROM (VALUES ...) AS t(a, b)."
)
"""Why a bare `VALUES` list with an alias is refused.

sqlglot reads `VALUES (1) AS t(a)` as an aliased `exp.Values`; DuckDB refuses it with a parser
error. Making it a `SELECT` of the list would quietly run text DuckDB itself rejects, so it is
refused here with the spelling that works.
"""

_VALUES_CLAUSE_MESSAGE = (
    "A VALUES list on its own takes no {clause}; write "
    "SELECT * FROM (VALUES ...) AS t(a, b) {clause} ... instead."
)
"""Why a bare `VALUES` list carrying a clause other than `ORDER BY`, `LIMIT` or `OFFSET` is
refused, naming the clause and the form that takes it."""

_NO_SUCH_CLAUSE_MESSAGE = "DuckDB has no {clause}; {hint}."
"""Why a bare `VALUES` list carrying a clause DuckDB takes nowhere, not even in a `SELECT`, is
refused, with what to do instead."""

_CLAUSES_DUCKDB_LACKS = {
    "SORT BY": "use ORDER BY",
    "CLUSTER BY": "use ORDER BY",
    "DISTRIBUTE BY": "remove it",
    "FOR UPDATE": "remove it, a query takes no lock",
    "CONNECT BY": "remove it",
    "MATCH_RECOGNIZE": "remove it",
    "PREWHERE": "use WHERE in a SELECT of the list",
    "SETTINGS": "remove it",
    "FORMAT": "remove it",
    "OPTION": "remove it",
    "FOR": "remove it",
}
"""The clauses of `_VALUES_CLAUSES` that DuckDB refuses in a `SELECT` too, each with the hint
its refusal gives. Checked against DuckDB 1.5: each is a parser error after
`SELECT * FROM (VALUES (1)) AS t(a)`, where `WHERE`, `GROUP BY`, `WINDOW`, `TABLESAMPLE`,
`JOIN`, `PIVOT` and `LATERAL` run."""

_VALUES_MODIFIERS = ("order", "limit", "offset")
"""The `exp.Values` arguments that apply to the whole result and move to the `SELECT` around it.

An `ORDER BY` left inside a derived table need not order the outer result.
"""

_VALUES_ARGS = frozenset({"expressions", *_VALUES_MODIFIERS})
"""The only arguments a bare `VALUES` list may carry: its rows and `_VALUES_MODIFIERS`.

An allow-list: sqlglot attaches every query modifier (`WHERE`, `GROUP BY`, `JOIN`, `SORT BY`,
`FOR UPDATE`, ...) to whatever a query parses to, an `exp.Values` included, although
`exp.Values.arg_types` lists none of them. DuckDB refuses each of them on a bare `VALUES`, and
`_select_of_values` would drop them or render them in the wrong place, so any argument not
listed here is refused, also one a later sqlglot adds.
"""

_VALUES_CLAUSES = {
    "alias": "alias",
    "where": "WHERE",
    "group": "GROUP BY",
    "having": "HAVING",
    "qualify": "QUALIFY",
    "windows": "WINDOW",
    "joins": "JOIN",
    "laterals": "LATERAL",
    "pivots": "PIVOT",
    "sample": "TABLESAMPLE",
    "sort": "SORT BY",
    "cluster": "CLUSTER BY",
    "distribute": "DISTRIBUTE BY",
    "locks": "FOR UPDATE",
    "connect": "CONNECT BY",
    "match": "MATCH_RECOGNIZE",
    "prewhere": "PREWHERE",
    "settings": "SETTINGS",
    "format": "FORMAT",
    "options": "OPTION",
    "for_": "FOR",
}
"""How a refusal names each argument of `exp.Values.arg_types` and `exp.QUERY_MODIFIERS`
outside `_VALUES_ARGS`. An argument missing here is refused all the same, under its own key;
a test fails when sqlglot adds one, so it gets a name."""


@dataclass(frozen=True)
class TableSource:
    """Where one referenced table's rows come from.

    Parameters
    ----------
    urls
        The already-resolved, already-authorized file URLs to read. Treated
        as data throughout: set as a DuckDB variable the user's own
        SQL cannot address, never rendered into SQL text.
    file_format
        The stored format, one of `"parquet"`, `"csv"`, or `"json"`.
    columns
        The table's declared columns, mapped to the type names DuckDB should
        give them. Used when `urls` is empty, to stand in for the files with
        a typed relation of zero rows — an Iceberg table with no current
        snapshot has no data files at all, and `read_parquet([])` is an
        error rather than an empty result. Also used, whenever `row_filters`
        or `column_masks` is non-empty, to enumerate an explicit column list
        for the generated CTE instead of a blind `SELECT *` — see
        `_reader_select_sql`.
    row_filters
        Row-filter predicates already resolved and context-substituted for
        the requesting principal (`lakana_core.policy.PolicyResolver`) — ANDed
        together into the generated CTE's own `WHERE` clause. Empty means no
        filter applies. Spliced verbatim into the generated SQL text, never
        bound as a parameter: this is safe only because each expression is
        required to have already passed
        `lakana_core.policy.expressions.validate_restricted_expression` and
        `PolicyResolver`'s substitution before construction. These are
        trusted, pre-validated SQL fragments, not user input — this module
        performs no independent validation of them.
    column_masks
        Column name to its already-resolved, already-substituted mask
        expression. A column with no entry here is selected unmasked. Empty
        means no mask applies. Cast to the column's declared type and rendered into the
        generated SQL text, never bound as a parameter, for the same reason and under the same
        pre-validation requirement as `row_filters`.
    mask_types
        Column name to the structured DuckDB type a type-checked mask on it is cast to
        (`lakana_core.policy.cast_types`: built from the column's Iceberg type, or from the Arrow
        type name stored for a raw-zone table, never parsed from a type string). A column with no
        entry here, such as a struct, is not cast.
    typed_masks
        The masked columns whose mask was checked against the column's type when it was created
        (`ColumnMask.type_checked`). Only these are cast; every other mask is used as written.
    declared_columns_only
        Read the files through the explicit list of `columns` also when no policy applies,
        never with ``SELECT *``. Set for a read at an older snapshot of a table, whose files
        can hold a column that `columns` leaves out on purpose; `columns` must then not be
        empty.
    field_ids
        The columns of an Iceberg read, by field id, in the order the read returns them.
        When given, the files are read through DuckDB's field-id map (`read_parquet(...,
        schema = ...)`), so a renamed, re-added or widened column reads its own data and a
        column a file lacks reads as NULL. Empty for a raw-zone table, whose files are read by
        name. Must hold every key of `columns`; set as a variable, never rendered into SQL.
    """

    urls: Sequence[str]
    file_format: str
    columns: Mapping[str, str] = field(default_factory=dict)
    row_filters: Sequence[str] = field(default_factory=tuple)
    column_masks: Mapping[str, str] = field(default_factory=dict)
    declared_columns_only: bool = False
    field_ids: Sequence[ReadField] = field(default_factory=tuple)
    mask_types: Mapping[str, exp.DataType] = field(default_factory=dict)
    typed_masks: frozenset[str] = frozenset()


@dataclass(frozen=True)
class RewrittenQuery:
    """A rewritten query and the variables to set before executing it.

    Parameters
    ----------
    sql
        The rewritten SQL. Every resolved file list is read through
        `getvariable('v_<n>_files')`, never as literal text, and no placeholder appears in
        it: the user's own SQL may contain none and nothing is bound to the statement.
    parameters
        Variable name to the value the query process sets it to before it runs the
        statement (`SET VARIABLE "<name>" = ?`, the value bound): one URL list, as a DuckDB
        LIST, per CTE that reads files, and, for an Iceberg read, its field-id map
        (`v_<n>_schema`), numbered in the order the CTEs were generated.
    """

    sql: str
    parameters: dict[str, list[str] | tuple[ReadField, ...]]


MAX_VIEW_DEPTH = 10
"""How deeply views may nest."""

MAX_EXPANDED_VIEWS = 100
"""Most view references one query may expand in total.

`max_depth` alone bounds how deep a chain may nest, not how many times a
query, or a view in it, may reference the *same* view: a view referencing
the next one `k` times, chained `d` levels deep, expands `k**d` references
without this — cheap to construct (one `CREATE OR REPLACE VIEW` per level)
and expensive for every later reader, since expansion runs again on every
query that touches the outermost view. This cap is checked against
`expanded` — the same list `expand_views` returns as `ExpandedQuery.views`
— before each reference is expanded, so a query already over the limit
fails before doing further work rather than after building a queryable but
enormous tree.
"""


@dataclass(frozen=True)
class ViewDefinition:
    """A view, as `expand_views` needs it.

    Parameters
    ----------
    name
        The view's dotted name.
    sql
        Its stored SELECT.
    references
        The dotted names it was created against, for error messages.
    """

    name: str
    sql: str
    references: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExpandedQuery:
    """A query with every view reference replaced by its definition.

    Parameters
    ----------
    sql
        The expanded SQL, or the original text when no view is referenced.
    views
        The dotted names of the views expanded, innermost first, once per
        reference.
    """

    sql: str
    views: tuple[str, ...]


_ALLOWED_TABLE_FUNCTIONS = frozenset({"range", "generate_series", "unnest"})
"""Table functions a query may call: pure generators that read no external data.

Deliberately small and explicit. Every other table function — a file or URL
reader, a foreign-database scanner, an introspection function, or one this
list has never heard of — is refused by `check_blacklist` in any `FROM`-item
position (`FROM fn()`, `LATERAL fn()`, `TABLE(fn())`), so a function DuckDB
adds later is refused by default rather than allowed. sqlglot parses `range`
as `exp.GenerateSeries` and a `FROM unnest(...)` as `exp.Unnest`; the latter
is never an `exp.Table`, so its entry here is a belt-and-braces spelling
guard rather than something the current parser reaches.
"""


_READER_PREFIXES = ("read_",)
_READER_SUFFIXES = ("_scan",)
_READER_NAMES = frozenset({"glob", "sniff_csv"})

_LOAD_FIRST = (
    'load the data into a table first (see "Load data from a source" in the user '
    "documentation) and read that table."
)


def _refused_table_function(function: str) -> str:
    """Say why a table function in `FROM` is refused and what to do next.

    Parameters
    ----------
    function
        The lowercase name `_from_item_function_name` found (or `rows from` / `lateral`).

    Returns
    -------
    str
        For a known file, URL or database reader, the cause (it reads outside the catalog's
        access rules); for any other name, a line that asks to check the name.
    """
    known = (
        function.startswith(_READER_PREFIXES)
        or function.endswith(_READER_SUFFIXES)
        or function in _READER_NAMES
    )
    load_first = _LOAD_FIRST[0].upper() + _LOAD_FIRST[1:]
    if known:
        return (
            f"table function {function} is not allowed: it reads data directly (a file, a URL "
            "or another database), outside the access rules of the catalog, so only range, "
            f"generate_series and unnest may be called in FROM. {load_first}"
        )
    return (
        f"table function {function} is not allowed: only range, generate_series and unnest "
        f"may be called in FROM; check the name, or {_LOAD_FIRST}"
    )


def _from_item_function_name(node: exp.Table | exp.Lateral) -> str | None:
    """Name the function a `FROM` item calls, if it calls one.

    Parameters
    ----------
    node
        An `exp.Table` or `exp.Lateral` node.

    Returns
    -------
    str or None
        The lowercase function name when the item is a table function call
        (`FROM range(10)`, `LATERAL fn(...)`, `TABLE(fn(...))` — the inner
        function is named, not `table`; a schema-qualified `LATERAL main.fn()`
        is unwrapped to `fn`); `"rows from"` for the nameless node
        `ROWS FROM (...)` produces; `"lateral"` for any `LATERAL` shape that is
        neither a subquery nor a function call (fail closed); `None` for a
        named table or a lateral subquery.
    """
    inner = node.this
    if isinstance(node, exp.Lateral):
        if isinstance(inner, exp.Subquery):
            return None
        while isinstance(inner, exp.Dot):
            inner = inner.expression
        if not isinstance(inner, exp.Func):
            return "lateral"
    elif inner is None:
        return "rows from"
    if not isinstance(inner, exp.Func):
        return None
    while isinstance(inner, exp.Anonymous) and inner.name.lower() == "table":
        wrapped = inner.expressions[0] if inner.expressions else None
        if not isinstance(wrapped, exp.Func):
            break
        inner = wrapped
    return inner.name.lower() if isinstance(inner, exp.Anonymous) else inner.sql_name().lower()


def _dotted_table_name(table: exp.Table) -> str:
    """Build a dotted table name from a sqlglot Table node's catalog/db/name parts.

    Parameters
    ----------
    table
        A parsed `exp.Table` node.

    Returns
    -------
    str
        The dotted name (e.g. "gold.sales.monthly"), joining whichever of
        `catalog`, `db`, and the table's own name are present.
    """
    name_parts = (table.args.get("catalog"), table.args.get("db"), table.this)
    return ".".join(part.name for part in name_parts if part)


logger = logging.getLogger(__name__)

_P = ParamSpec("_P")
_R = TypeVar("_R")

_PARSER_FAULTS = (ValueError, AttributeError)
"""What sqlglot's parser raises, besides its own syntax errors, on text it half-reads."""


def _parse_sql(sql: str, dialect: str) -> exp.Expr:
    """Parse submitted SQL; a parser fault on malformed text is a syntax refusal.

    Parameters
    ----------
    sql
        The text to parse.
    dialect
        The SQL dialect to parse `sql` as.

    Returns
    -------
    sqlglot.expressions.Expr
        The parsed statement, with every arrow DuckDB reads as a JSON extract turned into one
        (`_json_arrows_outside_lambda_functions`), so every check sees what DuckDB runs.

    Raises
    ------
    sqlglot.errors.ParseError
        If sqlglot's parser itself fails on the text with an error that is not a
        `ParseError`: a `ValueError` (it assumes a name is a string where the text put a
        placeholder) or an `AttributeError` (it reads a struct key from a node it left empty).
        It is a `ParseError` so that every handler of `SQL_SYNTAX_ERRORS`, such as the one
        that names a broken stored view, treats it as the syntax error it is.
    """
    try:
        parsed = sqlglot.parse_one(sql, dialect=dialect)
    except _PARSER_FAULTS as exc:
        raise sqlglot.errors.ParseError("the text could not be read") from exc
    return _json_arrows_outside_lambda_functions(parsed)


def _render(parsed: exp.Expr, dialect: str) -> str:
    """Render a checked tree as the SQL the query process runs, every lambda in keyword form.

    DuckDB 2.0 refuses the arrow lambda (`x -> x + 1` is a Binder Error) and DuckDB 1.5
    accepts both forms, so every `exp.Lambda` is rendered `LAMBDA x : x + 1` (`LAMBDA x, y :
    …` for two parameters) whichever form the caller wrote. Only the spelling of the node
    changes: its parameters and body are the subtrees the guard already checked, rendered by
    sqlglot as any other expression, so no text reaches DuckDB that the guard did not see.
    The keyword form is also the one DuckDB parses as sqlglot does: `x -> x -> '$.a'` (a
    lambda whose body is a JSON extract) did not bind in the arrow form on DuckDB 1.5.

    A lambda's body extends as far to the right as DuckDB can read it, so a lambda anywhere but
    in the arguments of a function that takes one (or already in parentheses) is wrapped in
    parentheses: sqlglot turns `mod(x -> x + 10, 4)` into the operator `% 4`, and a bare
    `LAMBDA x : x + 10 % 4` would take the `% 4` into the body, a grouping the guard never
    checked. DuckDB refuses such a lambda either way; wrapped, it refuses the tree that was
    checked. Turning the lambda into a JSON extract instead, as in a function's argument, would
    also turn a lambda the caller parenthesised inside a function that takes one.

    Parameters
    ----------
    parsed
        The tree to render; its lambdas are marked for the keyword form, and an operand lambda
        wrapped in parentheses, in place.
    dialect
        The SQL dialect to render in.

    Returns
    -------
    str
        The SQL text.
    """
    for node in list(parsed.find_all(exp.Lambda)):
        node.set("colon", True)
        if not isinstance(node.parent, exp.Paren) and not _takes_a_lambda(node.parent):
            wrapper = exp.Paren()
            node.replace(wrapper)
            wrapper.set("this", node)
    return parsed.sql(dialect=dialect)


def _takes_a_lambda(function: exp.Expr | None) -> bool:
    """Say whether DuckDB reads a lambda in the arguments of `function`.

    Parameters
    ----------
    function
        The parent of a parsed `exp.Lambda`.

    Returns
    -------
    bool
        Whether it is `COLUMNS` or a function of `_LAMBDA_FUNCTIONS`.
    """
    if isinstance(function, exp.Anonymous):
        return function.name.lower() in _LAMBDA_FUNCTIONS
    return isinstance(function, _LAMBDA_FUNCTION_TYPES)


def _is_arrow(node: exp.Expr) -> bool:
    """Say whether `node` is a JSON extract written as `->` or `->>`, not as a function call.

    Parameters
    ----------
    node
        A node of a lambda's body.

    Returns
    -------
    bool
        Whether it is the operator form (sqlglot sets `only_json_types` only for it).
    """
    return (
        isinstance(node, (exp.JSONExtract, exp.JSONExtractScalar))
        and "only_json_types" in node.args
    )


def _json_extract(subject: exp.Expr, right: exp.Expr) -> exp.Expr:
    """Build `subject -> right`, associating a chain of arrows to the left as DuckDB does.

    Parameters
    ----------
    subject
        The left operand: the column the caller named.
    right
        What followed the first arrow, as sqlglot parsed it.

    Returns
    -------
    sqlglot.expressions.Expr
        `subject -> right`; for `right` = `'a' -> 'b'`, `(subject -> 'a') -> 'b'`.
    """
    if _is_arrow(right):
        right.set("this", _json_extract(subject, right.this))
        return right
    return exp.JSONExtract(this=subject, expression=right, only_json_types=False)


def _json_arrows_outside_lambda_functions(parsed: exp.Expr) -> exp.Expr:
    """Turn a lambda DuckDB reads as a JSON extract into that extract.

    sqlglot parses `j -> 'a'` in any function's arguments as a lambda; DuckDB reads it as a
    lambda only in a function that takes one (`_LAMBDA_FUNCTIONS`, `COLUMNS`) and as the JSON
    extract `j -> 'a'` everywhere else. Done right after parsing, so the guard checks, and the
    rewrite renders, what DuckDB runs: `_render` gives a real lambda the keyword form, and the
    extract renders as an arrow again. A lambda of two parameters, or one written with the
    keyword, is never JSON.

    Parameters
    ----------
    parsed
        The parsed statement; changed in place.

    Returns
    -------
    sqlglot.expressions.Expr
        `parsed`. A lambda's parent is always a function, so the root is never replaced.
    """
    for node in list(parsed.find_all(exp.Lambda, bfs=False))[::-1]:
        parameters = node.expressions
        if (
            node.args.get("colon")
            or len(parameters) != 1
            or not isinstance(node.parent, exp.Func)
            or _takes_a_lambda(node.parent)
        ):
            continue
        subject = exp.column(parameters[0].copy())
        node.replace(_json_extract(subject, node.this))
    return parsed


def _fail_closed(func: Callable[_P, _R]) -> Callable[_P, _R]:
    """Make a guard entry point refuse, never raise, on an unexpected error.

    Parameters
    ----------
    func
        A public entry point that parses and walks submitted SQL.

    Returns
    -------
    collections.abc.Callable
        `func`, except that an exception other than the expected ones (`ValidationError`,
        another `LakanaError`, the syntax errors in `SQL_SYNTAX_ERRORS`, `MemoryError`)
        becomes a `ValidationError`. The exception's type is logged at WARNING, never the
        SQL. The result is a refusal on every path: nothing here ever allows a query.
    """

    @functools.wraps(func)
    def guarded(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        """Call the wrapped entry point.

        Parameters
        ----------
        *args
            The entry point's positional arguments.
        **kwargs
            The entry point's keyword arguments.

        Returns
        -------
        object
            What the entry point returns.

        Raises
        ------
        ValidationError
            If the entry point failed in an unexpected way.
        LakanaError
            As the entry point raises it, unchanged.
        MemoryError
            As the entry point raises it, unchanged.
        """
        try:
            return func(*args, **kwargs)
        except (LakanaError, MemoryError, *SQL_SYNTAX_ERRORS):
            raise
        except Exception as exc:
            logger.warning(
                "query guard refused a query after an unexpected %s in %s",
                type(exc).__name__,
                getattr(func, "__name__", "entry point"),
            )
            raise ValidationError(
                "We could not parse your SQL. Check its syntax and submit it again; "
                "if the query is valid, report this."
            ) from exc

    return guarded


def _reject_recursive_ctes(parsed: exp.Expr) -> None:
    """Refuse a statement that would reach DuckDB as a recursive query, anywhere in it.

    Two spellings reach DuckDB as `WITH RECURSIVE`: the clause itself, and a
    hierarchical `CONNECT BY` query, which sqlglot parses to an `exp.Connect`
    on a plain `SELECT` and writes as a recursive CTE only when it renders the
    tree (see `_CONNECT_BY_NODES`). Checking the parsed tree for the
    `RECURSIVE` keyword alone would miss the second, so both are refused here
    and the invariant holds for the SQL DuckDB is actually given: no
    submitted query runs as a recursive query.

    This closes a whole feature surface rather than one more spelling of a
    bypass. sqlglot's `optimizer/scope.py::_traverse_ctes` pre-seeds a
    recursive CTE's own name into `Scope.cte_sources` **before** traversing
    its body, and where that body is a set operation — `base_term UNION ALL
    recursive_term`, the standard shape — the pre-seeded binding is
    inherited into *both* branches. DuckDB does not bind it that way: only
    the recursive term legitimately self-references the CTE being built,
    while in the base term the same name still resolves to a real relation
    through DuckDB's ordinary replacement-scan resolution — a local file, an
    HTTP(S) URL, or a catalog table. Every scope-based classifier in this
    module therefore reads a base-term reference as "already bound, not a
    table", drops it, and authorizes nothing, while DuckDB goes and reads the
    real thing. Thirteen working spellings of that were found, reaching
    arbitrary local file reads in single-node mode and worker-side SSRF in
    team and cloud mode (fetching an `https://` URL needs no credential, and
    `disabled_filesystems` does not cover `httpfs`).

    Modelling DuckDB's recursive binding rules exactly, per branch, is the
    second special case recursive CTEs have demanded here — and getting it
    subtly wrong a third time reopens an unauthenticated read. `WITH
    RECURSIVE` is an advanced, rare feature with no part in the ad-hoc
    analyst queries M2 serves, so it is refused outright instead: a query
    either contains the keyword or it does not, which is a question with no
    edge cases.

    `find_all` rather than reading the statement's own `WITH` clause: a
    recursive CTE nested inside a subquery, a derived table, or an `IN`
    predicate is the same bug, and the outermost clause is not the only
    place one can appear. The same holds for a `CONNECT BY`.

    A `CONNECT BY` query is not known to reach the base-term disagreement
    above: sqlglot's scope reads its tree before rendering, where it holds
    no recursive CTE, so its table is classified and rewritten like any
    other. It is refused because it still hands DuckDB a recursive query —
    the feature this function closes — and a check that holds only for the
    keyword would be one more special case to get wrong.

    Parameters
    ----------
    parsed
        The parsed statement to check. Not modified.

    Raises
    ------
    ValidationError
        If any `WITH` clause in `parsed` carries the `RECURSIVE` keyword, or
        `parsed` holds a `CONNECT BY`, `PRIOR` or `CONNECT_BY_ROOT` anywhere.
    """
    for with_clause in parsed.find_all(exp.With):
        if with_clause.args.get(_RECURSIVE_ARG):
            raise ValidationError(_RECURSIVE_CTE_MESSAGE)
    if parsed.find(*_CONNECT_BY_NODES) is not None:
        raise ValidationError(_CONNECT_BY_MESSAGE)


PIVOT_VALUES_MESSAGE = (
    "A PIVOT needs the values of its ON column listed: write PIVOT ... ON a IN ('x', 'y') USING ..."
)
"""Why a PIVOT without an ``IN`` list of literals is refused, and what to write instead."""


def _reject_pivot_without_values(parsed: exp.Expr) -> None:
    """Refuse a PIVOT whose ON column does not list its values as literals.

    Without the list DuckDB runs the PIVOT as two statements: it first creates a type of the
    distinct values, then selects (``extract_statements`` gives ``CREATE`` and ``SELECT`` on
    1.5.5, ``MULTI`` on 2.0.0). The query process runs exactly one ``SELECT`` and refuses
    anything else as a defect, so the guard says what to write instead. Both syntaxes are
    covered: DuckDB's statement form (``PIVOT t ON a IN (...) USING ...``, also inside
    ``FROM (...)`` or a CTE) and the SQL-standard form (``FROM t PIVOT (... FOR a IN (...))``).
    An ``IN`` with a subquery counts as no list. ``UNPIVOT`` creates nothing and passes.

    Parameters
    ----------
    parsed
        The parsed statement to check. Not modified.

    Raises
    ------
    ValidationError
        `PIVOT_VALUES_MESSAGE`, if a PIVOT anywhere in `parsed` has an ON column (statement
        form) or a ``FOR`` field (standard form) that is not ``column IN (<values>)``.
    """
    for pivot in parsed.find_all(exp.Pivot):
        if pivot.args.get("unpivot"):
            continue
        # The standard form keeps its FOR fields in `fields` (and its aggregates in
        # `expressions`); DuckDB's statement form keeps its ON columns in `expressions`.
        columns = pivot.args.get("fields") or pivot.args.get("expressions") or []
        if not columns or any(
            not isinstance(column, exp.In)
            or column.args.get("query") is not None
            or any(value.find(exp.Query) is not None for value in column.expressions)
            for column in columns
        ):
            raise ValidationError(PIVOT_VALUES_MESSAGE)


def _reject_placeholders(parsed: exp.Expr) -> None:
    """Refuse a statement carrying a query parameter or placeholder anywhere.

    The query process sets every resolved file list as a session variable
    (`v_<n>_files`, see `_files_variable`), and on S3 those lists are
    presigned URLs of the raw files. DuckDB 2.0 resolves a `$name` that no
    parameter is bound to as the session variable of that name, so
    `SELECT $v_0_files` would return the list as an ordinary value — a URL
    anyone can then fetch directly, bypassing every row filter and column
    mask. This refusal is what keeps the variables unreachable through a
    placeholder; do not relax it. No submitted query has a legitimate use
    for a placeholder anyway: the statement runs with no parameters bound.

    `find` rather than a top-level check: a placeholder nested in a
    subquery, a CTE, a `WHERE`, or a function argument binds all the same.
    sqlglot's duckdb dialect parses `?`, `$<n>`, and `$<identifier>` to
    `exp.Placeholder`; `exp.Parameter` is what other spellings (`@x`,
    `${x}`) parse to in other dialects, refused too so a dialect change
    cannot quietly reopen this. What DuckDB executes is sqlglot's rendering
    of this tree, never the raw text, and only these two node types render
    as a placeholder.

    Parameters
    ----------
    parsed
        The parsed statement to check. Not modified.

    Raises
    ------
    ValidationError
        If `parsed` contains any `exp.Placeholder` or `exp.Parameter` node.
        The message is fixed text and never echoes the query or a URL.
    """
    if parsed.find(exp.Placeholder, exp.Parameter) is not None:
        raise ValidationError(_PLACEHOLDER_MESSAGE)


def _leading_keyword(node: exp.Expr, dialect: str) -> str:
    """Name a refused statement by its first word, as sqlglot writes it.

    Parameters
    ----------
    node
        The refused node.
    dialect
        The SQL dialect to render it in.

    Returns
    -------
    str
        The keyword in capitals, at most `_KEYWORD_CHARS` characters: for DML and `INTO` the
        statement's own keyword (`DELETE`, `INSERT`, `INTO`), for an `exp.Command` its own
        keyword (`VACUUM`), otherwise the first word (`BEGIN`).
    """
    if isinstance(node, (exp.DML, exp.Into)):
        # Its rendering can start with the statement's own `WITH` clause or a literal.
        return node.key.upper()[:_KEYWORD_CHARS]
    text = str(node.this) if isinstance(node, exp.Command) else node.sql(dialect=dialect)
    words = text.split(None, 1)
    return (words[0] if words else node.key).upper()[:_KEYWORD_CHARS]


def _reject_statements_inside(parsed: exp.Expr, dialect: str) -> None:
    """Refuse a statement that is not a read, wherever in the tree it sits.

    DuckDB 2 runs DML inside a CTE (`WITH m AS (DELETE ... RETURNING *) SELECT ...`) and its
    `extract_statements` calls the whole text a `SELECT`; sqlglot parses it as an `exp.Select`
    holding an `exp.Delete`. Requiring the statement as a whole to be a query is therefore not
    enough: every node is looked at, and so is the body of every CTE.

    Parameters
    ----------
    parsed
        The parsed statement. Not modified.
    dialect
        The SQL dialect it was parsed as, for naming the refused keyword.

    Raises
    ------
    ValidationError
        If any node is one of `_NOT_A_READ`, or a CTE's body is not one of `_READ_BODIES`. The
        message names the statement's first word and nothing else of the query.
    """
    for node in parsed.walk():
        if isinstance(node, _NOT_A_READ):
            raise ValidationError(
                _NOT_A_READ_MESSAGE.format(keyword=_leading_keyword(node, dialect))
            )
        if isinstance(node, exp.CTE) and not isinstance(node.this, _READ_BODIES):
            raise ValidationError(
                _NOT_A_READ_MESSAGE.format(keyword=_leading_keyword(node.this, dialect))
            )


def _require_unambiguous_references(parsed: exp.Expr, dialect: str) -> None:
    """Refuse a query whose own references sqlglot cannot resolve unambiguously.

    Four separate authorization bypasses were found in the hand-rolled
    classification this gate now stands in front of, each one a different way
    for two unrelated things in a query to end up sharing a name: an inner
    `WITH` binding and an outer real table; a bare binding and a qualified
    `db.x`; a derived table's alias and a real read beside it; a `WITH` name
    and an *alias* an attacker hung on a genuine external read. The common
    shape is always the same — a name that means two things at once — so the
    check made here is for that shape itself, rather than for one more of its
    spellings.

    `sqlglot.optimizer.qualify.qualify` is sqlglot's own, separately tested
    pass for exactly this: it resolves every reference in the statement and
    raises where it cannot. It runs against a **copy**, purely as a gate: the
    tree the caller goes on to classify and rewrite is the one they parsed,
    so the SQL the executor finally runs is still the user's own query with
    its table references swapped, not an optimizer's re-rendering of it
    (which renames a `COUNT(*)` result column to `_col_0`, among other
    surprises).

    `validate_qualify_columns` is off deliberately. With it on, `qualify`
    also demands that every *column* resolve to a known source, which no
    caller here can satisfy: the platform passes no schema, so an ordinary
    `SELECT id FROM bronze.a JOIN gold.b ON ...` — legal SQL DuckDB runs
    fine — would be refused. Table and alias resolution, which is what this
    gate is for, is unaffected by the flag.

    Reading `Scope.selected_sources` is what raises on two sources of one
    scope sharing a name, so every scope's is read here explicitly rather
    than trusting that `qualify` internally visited them all.

    Parameters
    ----------
    parsed
        The parsed statement to check. Not modified.
    dialect
        The SQL dialect `parsed` was parsed as.

    Raises
    ------
    ValidationError
        If sqlglot cannot resolve the statement's references unambiguously.
        Fail closed: an ambiguity sqlglot itself declines to resolve is not
        one this module may resolve on its behalf. DuckDB refuses these
        queries too — `WITH t AS (...) SELECT * FROM a.t JOIN t` is
        `Ambiguous reference to table "t"` there — so nothing a user can
        legitimately run is lost.
    """
    try:
        qualified = qualify(parsed.copy(), dialect=dialect, validate_qualify_columns=False)
        root = build_scope(qualified)
        for scope in root.traverse() if root is not None else ():
            _ = scope.selected_sources
    except Exception as exc:
        raise ValidationError(
            f"This query's table references cannot be resolved unambiguously: {exc}"
        ) from exc


def unreadable_view_message(dotted: str) -> str:
    """Say a stored view no longer parses, without any of its SQL.

    Parameters
    ----------
    dotted
        The view's dotted name, which the reader's own query used.

    Returns
    -------
    str
        A message naming the view and the next step, and none of its text, which the
        reader may not be allowed to see.
    """
    return f"View {dotted!r} could not be read. Ask its owner to recreate it."


def _select_of_values(values: exp.Values) -> exp.Select:
    """Make a bare `VALUES` list a `SELECT * FROM (VALUES ...) AS __lakana_values`.

    sqlglot builds no scope for a top-level `exp.Values` (`build_scope` returns `None`), so
    the scope-based steps here — the ambiguity gate, `WITH` binding resolution, the rewrite's
    CTEs — would see nothing of the subqueries inside it. As a `SELECT` of a derived table it
    is the shape all of them already handle, with the same result: DuckDB names the columns
    of a derived `VALUES` `col0`, `col1`, ... as it does those of a bare one. sqlglot itself
    renders a `VALUES` inside a set operation this way.

    Parameters
    ----------
    values
        The parsed top-level `VALUES` list, moved into the result (not copied).

    Returns
    -------
    sqlglot.expressions.Select
        The `SELECT` of it, carrying its `ORDER BY`, `LIMIT` and `OFFSET`.

    Raises
    ------
    ValidationError
        If the list carries an alias or a clause DuckDB refuses on a bare `VALUES`; see
        `_reject_values_clauses`.
    """
    _reject_values_clauses(values)
    select = exp.Select(expressions=[exp.Star()])
    for key in _VALUES_MODIFIERS:
        modifier = values.args.get(key)
        if modifier is not None:
            values.set(key, None)
            select.set(key, modifier)
    values.set("alias", exp.TableAlias(this=exp.to_identifier(_VALUES_ALIAS)))
    return select.from_(values, copy=False)


def _reject_values_clauses(values: exp.Values) -> None:
    """Refuse a bare `VALUES` list that carries anything but its rows, an order and a limit.

    Parameters
    ----------
    values
        The parsed top-level `VALUES` list. Not modified.

    Raises
    ------
    ValidationError
        If it carries an argument outside `_VALUES_ARGS` — an alias (`_ALIASED_VALUES_MESSAGE`)
        or a clause such as `WHERE` or `JOIN` (`_VALUES_CLAUSE_MESSAGE`, naming the clause), or
        one DuckDB never takes, such as `SORT BY` (`_NO_SUCH_CLAUSE_MESSAGE`).
    """
    for key, value in values.args.items():
        if key in _VALUES_ARGS or value is None or value == []:
            continue
        if key == "alias":
            raise ValidationError(_ALIASED_VALUES_MESSAGE)
        clause = _VALUES_CLAUSES.get(key, key.upper())
        hint = _CLAUSES_DUCKDB_LACKS.get(clause)
        if hint is not None:
            raise ValidationError(_NO_SUCH_CLAUSE_MESSAGE.format(clause=clause, hint=hint))
        raise ValidationError(_VALUES_CLAUSE_MESSAGE.format(clause=clause))


def _parse_checked(sql: str, dialect: str) -> exp.Expr:
    """Parse a query and refuse it unless its references resolve unambiguously.

    Parameters
    ----------
    sql
        The query to parse.
    dialect
        The SQL dialect to parse `sql` as.

    Returns
    -------
    sqlglot.expressions.Expr
        The parsed statement, safe to classify with `_cte_reference_ids`. A bare `VALUES`
        list comes back as the `SELECT` of it that `_select_of_values` builds.

    Raises
    ------
    ValidationError
        If the statement contains a query parameter or placeholder, which
        would read the session variables holding the URL lists — see
        `_reject_placeholders`. Or if it contains a `WITH RECURSIVE` clause, whose scope
        bindings `_cte_reference_ids` cannot classify soundly, or a `CONNECT BY`, which
        renders as one — see `_reject_recursive_ctes`. Or if it holds a statement that is not a read
        anywhere, see `_reject_statements_inside`. Or if it is a bare `VALUES` list with an
        alias or a clause other than `ORDER BY`, `LIMIT` and `OFFSET`, see
        `_reject_values_clauses`. `check_blacklist` already refuses these
        first in `lakana_worker.pipeline.run_one`, so this is defence in
        depth: the promise `extract_table_refs` makes its caller is that
        every name it leaves out is genuinely not a table, and it cannot
        keep that promise here, so it must not be reachable by a future
        caller that skips the blacklist.
    """
    parsed = _parse_sql(sql, dialect)
    if isinstance(parsed, exp.Values):
        parsed = _select_of_values(parsed)
    _reject_placeholders(parsed)
    _reject_statements_inside(parsed, dialect)
    _reject_recursive_ctes(parsed)
    _reject_pivot_without_values(parsed)
    _require_unambiguous_references(parsed, dialect)
    return parsed


def _is_cte_reference(scope: Scope, table: exp.Table) -> bool:
    """Decide whether one table node names a `WITH` binding rather than a real table.

    A `WITH` name is not a catalog table: it has no namespace, it is not
    authorized against, and it must not be rewritten. Which names are bound
    is a property of a *scope*, not of the whole query — binding `x` in an
    inner scope says nothing about a real `x` read in an outer one — so the
    question is asked of `Scope.cte_sources`, sqlglot's own record of the
    `WITH` names visible at this point, inherited into nested scopes and
    holding only bindings a `WITH` clause actually made.

    The name asked about is `table.name`, the reference's own name, never
    `alias_or_name`. An alias is attacker-chosen text that names the
    *result*, not the thing being read, and DuckDB resolves a reference by
    its name: aliasing a genuine external read to a `WITH` name does not make
    DuckDB read the binding, so it must not make this function say so either
    — that was the fourth bypass in this class.

    A table node carrying a `db` or `catalog` part is a real reference
    whatever the scope binds: DuckDB binds a `WITH` name as a single
    identifier and never resolves a qualified name to one.

    Parameters
    ----------
    scope
        The scope the reference appears in, from `build_scope`.
    table
        The table node to classify.

    Returns
    -------
    bool
        `True` if this reference resolves to a `WITH` binding visible in
        `scope`, and so is not a catalog table at all.
    """
    if table.args.get("db") is not None or table.args.get("catalog") is not None:
        return False
    return table.name in scope.cte_sources


def _cte_reference_ids(parsed: exp.Expr) -> set[int]:
    """Identify every table node that is really a reference to a `WITH` binding.

    Identity (`id`), not equality: two textually identical references are
    equal and hash equal as sqlglot expressions, but one may be a CTE
    reference and the other a real table read in a different scope — which is
    the entire distinction being drawn here.

    Anything scope resolution does not positively classify as a binding stays
    a real table reference. A statement sqlglot builds no scope for at all (a
    `DELETE`, an `UPDATE`) therefore keeps every reference: failing open here
    would mean skipping authorization, which is the failure this function
    exists to prevent. `parsed` must already have passed
    `_require_unambiguous_references`.

    Parameters
    ----------
    parsed
        The parsed statement.

    Returns
    -------
    set of int
        The `id` of every `exp.Table` node that names a `WITH` binding
        visible in the scope it appears in.
    """
    root = build_scope(parsed)
    if root is None:
        return set()
    return {
        id(table)
        for scope in root.traverse()
        for table in scope.tables
        if _is_cte_reference(scope, table)
    }


def _empty_relation(dotted: str, columns: Mapping[str, str]) -> exp.Select:
    """Build a zero-row relation shaped like a table's declared schema.

    Parameters
    ----------
    dotted
        The dotted table name, for the error message only.
    columns
        Column name to DuckDB type name. Both are rendered through sqlglot
        — the name as a quoted identifier, the type through sqlglot's own
        type parser — so neither can contribute SQL syntax of its own.

    Returns
    -------
    sqlglot.expressions.Select
        `SELECT CAST(NULL AS <type>) AS <name>, ... WHERE FALSE`.

    Raises
    ------
    ValidationError
        If the table declares no columns, leaving nothing to shape the
        empty relation from.
    """
    if not columns:
        raise ValidationError(
            f"Table {dotted!r} has no files and no declared columns to stand in for them."
        )
    projections = [
        exp.alias_(exp.cast(exp.null(), type_name, dialect="duckdb"), name, quoted=True)
        for name, type_name in columns.items()
    ]
    return exp.select(*projections).where(exp.false())


def _quoted_identifier(name: str, dialect: str) -> str:
    """Render a bare identifier name as a safely quoted, escaped SQL identifier.

    Parameters
    ----------
    name
        The identifier to quote — a column name from the catalog's declared
        schema, never trusted as pre-escaped text: an external table's
        schema is inferred from real file content, no more trusted than a
        storage object key (see this module's own docstring).
    dialect
        The SQL dialect to render the identifier for.

    Returns
    -------
    str
        The identifier, quoted and escaped through sqlglot's own renderer —
        never built by hand-splicing quote characters.
    """
    return exp.to_identifier(name, quoted=True).sql(dialect=dialect)


def _reader_select_sql(source: TableSource, index: int, dialect: str) -> str:
    """Build the SELECT reading a table's files, applying its row filters/column masks.

    Parameters
    ----------
    source
        The table's resolved files and any applicable row filters/column
        masks — already composed and context-substituted for the
        requesting principal by `lakana_core.policy.PolicyResolver.resolve`,
        never raw, unvalidated policy text.
    index
        The read's number, from which its variable names are made (`_files_variable`).
    dialect
        The SQL dialect to render the generated SQL as.

    Returns
    -------
    str
        `SELECT * FROM <reader>(getvariable('v_<n>_files'))` when no policy applies and the
        source does not ask for its declared columns only. Otherwise an explicit column list
        built from `source.columns`, with a masked column's projection replaced by its
        mask expression instead of the raw value, plus a `WHERE` clause
        ANDing every applicable row filter. A masked column's real value is
        therefore never selected in the first place, not merely hidden
        after the fact — and because this is the *only* place any query
        ever reads the table's raw files (every reference above it, in the
        user's own query, resolves against this CTE's output, however many
        joins or nested CTEs deep), applying policy here is enough on its
        own, with no separate `SELECT *`-expansion pass over the user's own
        query needed: masking survives nesting, as `test_query_rewrite.py` checks.

    Raises
    ------
    ValidationError
        If `source.row_filters` or `source.column_masks` is non-empty, or
        `source.declared_columns_only` is set, but `source.columns` is empty:
        there is no declared column list to build
        an explicit projection from, and silently falling through to an
        empty `SELECT` list would render `SELECT  FROM <reader>(...)` —
        syntactically invalid SQL that DuckDB would reject with an opaque
        parser error at execution time, far from the real cause. Or if
        `source.column_masks` names a column that is not a key of
        `source.columns`: silently ignoring the mismatched key would select
        that column's raw, unmasked value — breaking the guarantee that a
        masked column's real value never reaches the caller, via nothing
        more than an operator's typo or a case mismatch, with no
        diagnostic. Column names are matched exactly, case included:
        Lakana's identifiers are always rendered quoted, so normalizing case
        here would be inconsistent with quoted-identifier semantics
        everywhere else in this codebase. Or if `source.field_ids` is given for files that
        are not Parquet, or lacks a key of `source.columns`: such a column would not be
        read at all. Or if its map names a column outside `source.columns`, which a
        ``SELECT *`` would show, or repeats a field id or a name.
    """
    arguments = f"getvariable('{_files_variable(index)}')"
    if source.field_ids:
        known = [read.name for read in source.field_ids]
        missing = [name for name in source.columns if name not in known]
        if missing:
            raise ValidationError(
                f"The read by field id has no field id for its column {missing[0]!r}."
            )
        extra = [name for name in known if source.columns and name not in source.columns]
        if extra:
            raise ValidationError(
                f"The read by field id names the column {extra[0]!r}, which the table does "
                "not declare."
            )
        for label, values in (
            ("field id", [read.field_id for read in source.field_ids]),
            ("column", known),
        ):
            repeated = [value for value, count in Counter(values).items() if count > 1]
            if repeated:
                raise ValidationError(
                    f"The read by field id has the {label} {repeated[0]!r} twice."
                )
        if source.file_format != "parquet":
            raise ValidationError("A read by field id needs Parquet files.")
        arguments += f", schema = getvariable('{_schema_variable(index)}')"
    reader = f"{_READERS[source.file_format]}({arguments})"
    if not source.row_filters and not source.column_masks and not source.declared_columns_only:
        # B608: the interpolated values are a literal reader name from the
        # closed `_READERS` dict and a generated variable name; the URLs are
        # the variable's value, never text.
        return f"SELECT * FROM {reader}"  # nosec B608
    if not source.columns:
        raise ValidationError(
            "A row filter or column mask applies to this table, but it has no "
            "declared column list to build an explicit projection from."
        )
    unknown_masks = [name for name in source.column_masks if name not in source.columns]
    if unknown_masks:
        raise ValidationError(
            f"Column mask references unknown column {unknown_masks[0]!r}; "
            f"known columns: {sorted(source.columns)}"
        )
    projections = [
        _masked_projection(
            source.column_masks[name],
            name,
            source.mask_types.get(name) if name in source.typed_masks else None,
            dialect,
        )
        if name in source.column_masks
        else _quoted_identifier(name, dialect)
        for name in source.columns
    ]
    # B608: `projections` entries are either a safely quoted identifier or an
    # already-validated-and-substituted policy expression (never raw,
    # unvalidated user input — see
    # `lakana_core.policy.expressions.validate_restricted_expression` and
    # `lakana_core.policy.resolver`); `reader` is the same closed literal as
    # above. The URLs are still the variable's value, never text.
    sql = f"SELECT {', '.join(projections)} FROM {reader}"  # nosec B608
    if source.row_filters:
        sql += " WHERE " + " AND ".join(
            f"({_compiled_row_filter(expression, source.mask_types, dialect)})"
            for expression in source.row_filters
        )
    return sql


_DATE_TYPES = frozenset({exp.DType.DATE, exp.DType.DATE32})
"""The types a row filter's comparison treats as a date."""

_TIMESTAMP_TYPES = frozenset(
    {
        exp.DType.DATETIME,
        exp.DType.TIMESTAMP,
        exp.DType.TIMESTAMPNTZ,
        exp.DType.TIMESTAMP_S,
        exp.DType.TIMESTAMP_MS,
        exp.DType.TIMESTAMP_NS,
        exp.DType.TIMESTAMPTZ,
        exp.DType.TIMESTAMPLTZ,
    }
)
"""The types a row filter's comparison treats as a timestamp."""

_FILTER_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)
"""The comparisons a row filter may contain (`policy.expressions`)."""


def _operand_type(node: exp.Expr, types: Mapping[str, exp.DataType]) -> exp.DataType | None:
    """Give the type of one side of a row filter's comparison, when it is a column or literal.

    Parameters
    ----------
    node
        The side, possibly in parentheses.
    types
        The table's column types (`TableSource.mask_types`), by column name.

    Returns
    -------
    sqlglot.expressions.DataType or None
        The column's type (its name compared without regard to case, as the database does), a
        typed literal's type (`DATE '...'`, `TIMESTAMP '...'`), or `None` for anything else.
    """
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.Column) and not node.table:
        by_lower = {name.lower(): target for name, target in types.items()}
        return by_lower.get(node.name.lower())
    if isinstance(node, exp.Cast) and isinstance(node.this, exp.Literal):
        return node.to
    return None


def _compiled_row_filter(expression: str, types: Mapping[str, exp.DataType], dialect: str) -> str:
    """Convert the date side of each comparison of a date with a timestamp with `TRY_CAST`.

    DuckDB compares a date with a timestamp by converting the date, and that conversion fails
    for a date outside the timestamp range (after 294247-01-10 or before 290309-12-22 BC), which
    would fail every read the filter covers (#178). Written as `TRY_CAST`, such a date becomes
    `NULL`, so the comparison is unknown and the row is hidden: a filter in the subset of
    `policy.expressions` (comparisons, `AND`, `OR`, `NOT`, no `IS NULL`) is true only when it
    would be true whatever the unknown comparison was, so the conversion never lets a row
    through, and never fails. Against a constant, DuckDB 2.0's optimizer moves the conversion
    onto the constant and compares dates, which is exact for every date; either way no row
    passes a comparison that is false. A filter with no such comparison is returned as written.

    Parameters
    ----------
    expression
        One resolved, context-substituted row filter.
    types
        The table's column types (`TableSource.mask_types`), by column name.
    dialect
        The SQL dialect to parse and render it as.

    Returns
    -------
    str
        The filter, with `TRY_CAST(<date side> AS <timestamp type>)` where a date is compared
        with a timestamp.
    """
    parsed = sqlglot.parse_one(expression, dialect=dialect)
    changed = False
    for comparison in list(parsed.find_all(*_FILTER_COMPARISONS)):
        left, right = comparison.left, comparison.right
        left_type, right_type = _operand_type(left, types), _operand_type(right, types)
        if left_type is None or right_type is None:
            continue
        for side, side_type, other_type in (
            (left, left_type, right_type),
            (right, right_type, left_type),
        ):
            if side_type.this in _DATE_TYPES and other_type.this in _TIMESTAMP_TYPES:
                inner = side
                while isinstance(inner, exp.Paren):
                    inner = inner.this
                inner.replace(exp.TryCast(this=inner.copy(), to=other_type.copy()))
                changed = True
    return _render(parsed, dialect) if changed else expression


def _masked_projection(mask: str, name: str, target: exp.DataType | None, dialect: str) -> str:
    """Render a column mask as a projection, cast to the column's type when it was checked.

    A type-checked mask is cast to the column's DuckDB type so a reader gets exactly the type the
    column has without the mask. Creating the mask checked that every value fits; a value that
    no longer fits (the column the mask reads was widened since) becomes `NULL` through
    `TRY_CAST` instead of failing the read. The target
    is a structured `DataType` built from the column's Iceberg type or a stored Arrow type name
    (`lakana_core.policy.cast_types`), never parsed from a type string. Without a target, a mask
    that was never type-checked, or a column of a type that has no cast, the mask is used as it
    was stored.

    Parameters
    ----------
    mask
        The resolved, context-substituted mask expression.
    name
        The masked column's name.
    target
        The structured type to cast to, or `None` for no cast.
    dialect
        The SQL dialect to render in.

    Returns
    -------
    str
        `TRY_CAST(<mask> AS <type>) AS "<name>"`, or `<mask> AS "<name>"`.
    """
    alias = _quoted_identifier(name, dialect)
    if target is None:
        return f"{mask} AS {alias}"
    value = exp.TryCast(this=sqlglot.parse_one(mask, dialect=dialect), to=target.copy())
    return f"{value.sql(dialect=dialect)} AS {alias}"


def named_columns(sql: str, dialect: str = "duckdb") -> frozenset[str]:
    """List the columns a statement names, lower-cased.

    Parameters
    ----------
    sql
        The statement, with its views expanded.
    dialect
        The SQL dialect to parse `sql` as.

    Returns
    -------
    frozenset of str
        The name of every column reference in it (the last part of a dotted one); empty when
        the statement cannot be parsed, which `rewrite_query` refuses before this is asked.
    """
    try:
        parsed = _parse_sql(sql, dialect)
    except SQL_SYNTAX_ERRORS:
        return frozenset()
    return frozenset(column.name.lower() for column in parsed.find_all(exp.Column))


@_fail_closed
def extract_table_refs(sql: str, dialect: str = "duckdb") -> list[str]:
    """List every distinct qualified table name a query references.

    Parameters
    ----------
    sql
        The query to analyse.
    dialect
        The SQL dialect to parse `sql` as.

    Returns
    -------
    list of str
        Dotted table names (`"bronze.weather"`, `"gold.sales.monthly"`), each
        listed once, in the order first encountered. A table function
        (`FROM range(10)`) is not a table and is left out. A reference the query
        binds itself in a `WITH` clause visible at that point is not a table
        reference and is left out — resolved per scope, so a binding in one
        scope never hides a real table of the same name read in another.

    Raises
    ------
    ValidationError
        If sqlglot cannot resolve the query's references unambiguously; see
        `_require_unambiguous_references`. This list is what the caller
        authorizes against, so a reference left out of it is a reference
        never checked: where the meaning of a name is in doubt, the query is
        refused rather than classified. Also if the query contains a `WITH
        RECURSIVE` clause, whose base-term bindings no scope-based classifier
        here can read the way DuckDB does, or a `CONNECT BY`, which renders as
        one — see `_reject_recursive_ctes`.
    """
    parsed = _parse_checked(sql, dialect)
    bound = _cte_reference_ids(parsed)
    seen: dict[str, None] = {}
    for table in parsed.find_all(exp.Table):
        if id(table) not in bound and _from_item_function_name(table) is None:
            seen[_dotted_table_name(table)] = None
    return list(seen)


def expand_views(
    sql: str,
    lookup: Callable[[str], ViewDefinition | None],
    *,
    root: str | None = None,
    max_depth: int = MAX_VIEW_DEPTH,
    dialect: str = "duckdb",
) -> ExpandedQuery:
    """Replace every view reference with a subquery of the view's stored SQL.

    This must run before `extract_table_refs`/`rewrite_query` see the query:
    once views are expanded, every remaining reference is a base table, so
    grants, row filters and column masks apply to the base tables exactly
    as for a direct query, and a view can never widen access (invoker's
    rights; the "future view feature" `rewrite_query`'s
    docstring anticipates). Expansion is recursive, bounded by `max_depth`,
    and guarded against cycles; a stored view's SQL is re-checked against
    the AST guard as defence in depth.

    Parameters
    ----------
    sql
        The query.
    lookup
        Maps a dotted name to its view, or `None` for anything that is not
        a view. It is where the caller authorizes READ on each view.
        Memoized for the duration of this call: the
        same dotted name reached from two different places in the query —
        two occurrences of the same view, or the same view reached through
        two different nesting paths — is looked up (a database read plus an
        authorization check) at most once, not once per occurrence.
    root
        The view being created, if any, so a replacement can't reference
        itself through another view.
    max_depth
        How deeply views may nest.
    dialect
        The SQL dialect.

    Returns
    -------
    ExpandedQuery
        The expanded query and the views it expanded.

    Raises
    ------
    ValidationError
        If views form a cycle, nest deeper than `max_depth`; if more than
        `MAX_EXPANDED_VIEWS` references are expanded in total; if a view
        reference carries a construct expansion can't safely reproduce; or
        if a view's stored SQL fails the AST guard or doesn't parse
        unambiguously.
    """
    parsed = _parse_checked(sql, dialect)
    expanded: list[str] = []
    cache: dict[str, ViewDefinition | None] = {}

    def cached_lookup(dotted: str) -> ViewDefinition | None:
        """Call `lookup` at most once per distinct dotted name, for this call only.

        Parameters
        ----------
        dotted
            The dotted name to look up.

        Returns
        -------
        ViewDefinition or None
            `lookup`'s own result, cached. A raised exception (a denied
            view) is never cached: it aborts this call outright before a
            result exists to store.
        """
        if dotted not in cache:
            cache[dotted] = lookup(dotted)
        return cache[dotted]

    named_views: list[tuple[tuple[str, str, str], exp.Expr]] = []
    _expand_into(
        parsed,
        cached_lookup,
        () if root is None else (root,),
        expanded,
        named_views,
        max_depth,
        dialect,
    )
    if not expanded:
        return ExpandedQuery(sql=sql, views=())
    counts = _bare_name_counts(parsed)
    _drop_schema_from_qualified_columns(
        list(traverse_scope(parsed)),
        [(key, node) for key, node in named_views if counts[key[2]] == 1],
    )
    return ExpandedQuery(sql=_render(parsed, dialect), views=tuple(expanded))


_SAFE_TABLE_ARGS = frozenset({"this", "db", "catalog", "alias"})
"""`exp.Table` args that name and alias a reference, nothing more.

A view reference carrying only these needs no special handling: the
replacing `exp.Subquery` gets the same alias, and `this`/`db`/`catalog` are
what `_dotted_table_name` already read to identify the view.
"""

_TRANSFERABLE_TABLE_ARGS = frozenset({"joins", "pivots", "sample"})
"""`exp.Table` args `_expand_into` moves onto the replacing `exp.Subquery`.

`exp.Subquery.arg_types` accepts each of these directly, and moving them is
a structural transplant, not a rewrite: a `JOIN` inside a parenthesized view
reference (`FROM (gold.v JOIN silver.x ON …)` parses `joins` onto `v`'s own
`Table` node, not the enclosing `Select`'s — confirmed against the pinned
sqlglot version), a `PIVOT` and a `TABLESAMPLE` attach to whichever relation
they follow and attach the same way whether that relation is a table or,
after expansion, a subquery. Verified end to end against DuckDB for all
three (a parenthesized join, `PIVOT`, and `TABLESAMPLE`): each executes
correctly once expanded this way.

Before this fix, `_expand_into` kept only `alias` when replacing a `Table`
node, so any of these constructs on a view reference vanished silently and
the query returned wrong results with no error. Anything else `exp.Table.arg_types` allows
(`laterals`, `hints`,
`system_time` versioning, `pattern`, `ordinality`, `changes`, `partition`,
`format`, `rows_from`, `when`, `only`, `indexed`) has no established-safe
transplant onto a `Subquery`, so `_expand_into` refuses those with a
`ValidationError` instead of guessing — silent wrong results are never
acceptable here. `laterals` (a parenthesized `LATERAL VIEW`) was once carried
over, but it rendered SQL DuckDB refused with a raw parser error, so it is
refused up front like the rest.
"""


def _expand_into(
    parsed: exp.Expr,
    lookup: Callable[[str], ViewDefinition | None],
    stack: tuple[str, ...],
    expanded: list[str],
    named_views: list[tuple[tuple[str, str, str], exp.Expr]],
    max_depth: int,
    dialect: str,
) -> None:
    """Expand the view references in one parsed query, in place.

    Parameters
    ----------
    parsed
        The query, modified in place.
    lookup
        As for `expand_views`.
    stack
        The views being expanded around this query, outermost first.
    expanded
        Appended with each view expanded, innermost first.
    named_views
        Appended with the `_qualifier_key` and subquery of each view reference that had no
        alias and so took the view's name.
    max_depth
        As for `expand_views`.
    dialect
        The SQL dialect.

    Raises
    ------
    ValidationError
        As `expand_views` documents.
    """
    bound = _cte_reference_ids(parsed)
    for table in list(parsed.find_all(exp.Table)):
        if id(table) in bound:
            continue
        dotted = _dotted_table_name(table)
        view = lookup(dotted)
        if view is None:
            continue
        extra_args = set(table.args) - _SAFE_TABLE_ARGS
        unsupported = extra_args - _TRANSFERABLE_TABLE_ARGS
        if unsupported:
            raise ValidationError(
                f"{dotted!r} can't be used with {', '.join(sorted(unsupported))} yet."
            )
        if dotted in stack:
            path = " -> ".join((*stack, dotted))
            raise ValidationError(f"View {dotted!r} references itself: {path}.")
        if len(stack) >= max_depth:
            raise ValidationError(f"Views nest more than {max_depth} deep at {dotted!r}.")
        if len(expanded) >= MAX_EXPANDED_VIEWS:
            raise ValidationError(
                f"This query expands more than {MAX_EXPANDED_VIEWS} view references; "
                "simplify it or the views it reads."
            )
        try:
            check_blacklist(view.sql, dialect)
            inner = _parse_checked(view.sql, dialect)
        except SQL_SYNTAX_ERRORS as exc:
            # The stored SQL may name tables the reader cannot see: name the view only.
            raise ValidationError(unreadable_view_message(dotted)) from exc
        _expand_into(inner, lookup, (*stack, dotted), expanded, named_views, max_depth, dialect)
        alias = table.args.get("alias")
        key = _qualifier_key(table)
        if alias is None:
            alias = exp.TableAlias(this=exp.to_identifier(table.name))
        subquery = exp.Subquery(this=inner, alias=alias)
        if table.args.get("alias") is None:
            named_views.append((key, subquery))
        for arg in extra_args:
            subquery.set(arg, table.args[arg])
        table.replace(subquery)
        expanded.append(dotted)


_ALIAS_HASH_HEX_CHARS = 12


def _qualifier_key(node: exp.Table | exp.Column) -> tuple[str, str, str]:
    """Name the schema-qualified spelling of a table or of a column's table part.

    Parameters
    ----------
    node
        A table reference, or a column whose qualifier is read.

    Returns
    -------
    tuple of str
        The catalog, schema and table name, lower-cased (the database compares names
        without regard to case); an absent part is an empty string.
    """
    return (
        node.catalog.lower(),
        node.db.lower(),
        (node.name if isinstance(node, exp.Table) else node.table).lower(),
    )


def _bare_name_counts(parsed: exp.Expr) -> Counter[str]:
    """Count how often each bare name is taken by a source anywhere in a statement.

    Parameters
    ----------
    parsed
        The statement.

    Returns
    -------
    collections.Counter of str
        For each lower-cased name, the number of aliases (of tables, derived tables, CTEs and
        table functions) and unaliased table references called so, in every scope.
    """
    counts = Counter(alias.name.lower() for alias in parsed.find_all(exp.TableAlias))
    counts.update(
        table.name.lower()
        for table in parsed.find_all(exp.Table)
        if table.args.get("alias") is None
    )
    return counts


def _drop_schema_from_qualified_columns(
    scopes: Sequence[Scope], named: Sequence[tuple[tuple[str, str, str], exp.Expr]]
) -> None:
    """Cut the schema part off a column qualified with the full name of a read named by its table.

    A reference such as `gold.orders` is replaced by a read the database knows only by its
    table name, so the column `gold.orders.id` has to become `orders.id`, or the database looks
    for a schema called `gold`. Only a read whose bare name is used nowhere else in the
    statement is named that way (see `_bare_name_counts`), and only a column of the read's own
    scope is changed. In a nested scope the database would bind `orders.id` to a struct column
    called `orders` of that scope's own sources before it reached the outer read, and sqlglot
    makes a CTE body or a derived table a child of the scope around it although the read is not
    visible there. Any other dotted column is therefore left as written and fails in the
    database, as it always did, instead of binding to something else.

    Parameters
    ----------
    scopes
        The scopes of the statement, built while the reads were still in it.
    named
        The `_qualifier_key` of each read that took its bare name as alias, with the node.
    """
    owner = {id(node): scope for scope in scopes for node in (*scope.tables, *scope.derived_tables)}
    roots = {id(scope.expression): scope for scope in scopes}
    seen: set[int] = set()
    for scope in scopes:
        # A scope lists the correlated columns of its subqueries too, so each column is
        # attributed to the scope whose expression it sits in directly.
        for column in (*scope.columns, *scope.external_columns):
            if id(column) in seen or column.args.get("db") is None:
                continue
            seen.add(id(column))
            node: exp.Expr | None = column
            while node is not None and id(node) not in roots:
                node = node.parent
            own = None if node is None else roots[id(node)]
            key = _qualifier_key(column)
            for named_key, read in named:
                if named_key == key and own is owner.get(id(read)):
                    column.set("db", None)
                    column.set("catalog", None)


def _cte_alias(dotted: str) -> str:
    """Derive a table's CTE alias, distinct for every distinct dotted name.

    The sanitized prefix keeps generated SQL readable. The hash suffix is
    what makes it collision-resistant: sanitizing alone is lossy, so
    ``a.b_c`` and ``a_b.c`` would otherwise share ``__lakana_a_b_c``.

    Parameters
    ----------
    dotted
        The table's dotted name, as `_dotted_table_name` returns it.

    Returns
    -------
    str
        ``"__lakana_" + sanitized + "_" + <first 12 hex chars of sha256(dotted)>``.
    """
    digest = hashlib.sha256(dotted.encode("utf-8")).hexdigest()[:_ALIAS_HASH_HEX_CHARS]
    return "__lakana_" + re.sub(r"\W", "_", dotted) + "_" + digest


_KEPT_TABLE_ARGS = frozenset({"joins", "pivots", "sample"})
"""`exp.Table` args `rewrite_query` moves from a replaced table reference onto its replacement.

The replacement is itself an `exp.Table` (the generated CTE's name), so each of these attaches
to it exactly as it attached to the catalog name: a standard `PIVOT (...)`/`UNPIVOT (...)`, a
`TABLESAMPLE`, and the `JOIN` of a parenthesized join (`FROM (gold.a JOIN gold.b ON ...)`, which
sqlglot parses onto `a`'s own node). The policies are applied inside the CTE, so each of them
reads the filtered, masked rows. Dropping them returned wrong results with no error (#280).
"""

_REFUSED_TABLE_CLAUSES = {
    "laterals": "LATERAL VIEW",
    "hints": "a table hint",
    "system_time": "FOR SYSTEM_TIME",
    "version": "a version clause",
    "format": "a format clause",
    "pattern": "a file pattern",
    "ordinality": "WITH ORDINALITY",
    "when": "AT (...)",
    "only": "ONLY",
    "partition": "PARTITION (...)",
    "changes": "CHANGES (...)",
    "rows_from": "ROWS FROM (...)",
    "indexed": "INDEXED BY",
}
"""`exp.Table` args `rewrite_query` refuses on a catalog table, each with the name a refusal gives.

None has a meaning on the generated read, and dropping one would silently change the result (a
time-travel `AT (VERSION => 1)` would read the current rows). An argument in neither this nor
`_KEPT_TABLE_ARGS` is refused under its own key; a test fails when sqlglot adds one.
"""


def _carry_table_clauses(table: exp.Table, replacement: exp.Table, dotted: str) -> None:
    """Move the clauses attached to a catalog table reference onto the read that replaces it.

    Parameters
    ----------
    table
        The reference as the caller wrote it.
    replacement
        The reference to the generated read, modified in place.
    dotted
        The table's dotted name, for the refusal.

    Raises
    ------
    ValidationError
        If the reference carries a clause outside `_KEPT_TABLE_ARGS` and the naming args.
    """
    present = {key for key, value in table.args.items() if value not in (None, False, [])}
    refused = sorted(present - _SAFE_TABLE_ARGS - _KEPT_TABLE_ARGS)
    if refused:
        names = ", ".join(_REFUSED_TABLE_CLAUSES.get(key, key) for key in refused)
        raise ValidationError(f"{dotted!r} can't be read with {names}; remove it from the query.")
    for key in sorted(present & _KEPT_TABLE_ARGS):
        replacement.set(key, table.args[key])


@_fail_closed
def rewrite_query(
    sql: str, sources: Mapping[str, TableSource], dialect: str = "duckdb"
) -> RewrittenQuery:
    """Replace each qualified table reference with a CTE reading its resolved files.

    Parameters
    ----------
    sql
        The original query.
    sources
        Maps each dotted table name `extract_table_refs` would return from
        `sql` to where that table's rows come from.
    dialect
        The SQL dialect to parse and render `sql` as.

    Returns
    -------
    RewrittenQuery
        `sql` with every reference in `sources` replaced by a uniquely-named
        CTE (`read_parquet(getvariable('v_<n>_files'))` or the matching reader), keeping the
        original reference's alias so the rest of the query still resolves,
        plus the URL lists to set as those variables. This is what lets a
        multi-level Lakana name (`gold.sales.monthly`) run against DuckDB at
        all — the name never reaches DuckDB's own catalog, only the
        rewritten form does. A source with no `row_filters` or
        `column_masks` gets that bare reader call, unchanged from before
        those fields existed; a source with either gets an explicit column
        list instead, built from its declared `columns`, with a masked
        column's projection replaced by its mask expression and a `WHERE`
        clause ANDing every row filter — see `_reader_select_sql`.

    Raises
    ------
    ValidationError
        If a statement that is not a query (an `UPDATE`, a `DELETE`) names a
        catalog table: there is nowhere to attach the CTEs to, and no such
        statement has a defined meaning against a read-only scoped file set.
        Or if the query contains a query parameter or placeholder (see
        `_reject_placeholders`), if sqlglot cannot resolve the query's
        references unambiguously, or if the query contains a `WITH
        RECURSIVE` clause or a `CONNECT BY` — all checked
        here independently of `extract_table_refs`, so that this function
        never depends on that one having been called first. Or, for a source
        with an applicable row filter or column mask, if it declares no
        columns, or if a column mask names a column outside its declared
        columns — see `_reader_select_sql`. Or if two distinct tables would
        share one generated CTE alias — a defence against the silent reuse
        of an alias.
    """
    parsed = _parse_checked(sql, dialect)
    bound = _cte_reference_ids(parsed)
    ctes: dict[str, exp.Expr] = {}
    owners: dict[str, str] = {}
    parameters: dict[str, list[str] | tuple[ReadField, ...]] = {}
    reads = 0
    named: list[tuple[tuple[str, str, str], exp.Expr]] = []
    name_counts = _bare_name_counts(parsed)
    scopes = list(traverse_scope(parsed))
    for table in list(parsed.find_all(exp.Table)):
        dotted = _dotted_table_name(table)
        if id(table) in bound or dotted not in sources:
            continue
        source = sources[dotted]
        alias = _cte_alias(dotted)
        owner = owners.setdefault(alias, dotted)
        if owner != dotted:
            raise ValidationError(
                f"Internal alias {alias!r} was derived for both {owner!r} and {dotted!r}."
            )
        if alias not in ctes:
            if source.urls:
                index = reads
                reads += 1
                try:
                    ctes[alias] = sqlglot.parse_one(
                        _reader_select_sql(source, index, dialect), dialect=dialect
                    )
                except SQL_SYNTAX_ERRORS as exc:
                    # The text holds stored row filters and column masks, which the reader
                    # may not see: name none of it.
                    raise ValidationError(
                        "A stored row filter or column mask on a table this query reads could "
                        "not be read. Ask an administrator to correct or remove it."
                    ) from exc
                parameters[_files_variable(index)] = list(source.urls)
                if source.field_ids:
                    parameters[_schema_variable(index)] = tuple(source.field_ids)
            else:
                ctes[alias] = _empty_relation(dotted, source.columns)
        replacement = exp.to_table(alias)
        original_alias = table.args.get("alias")
        if original_alias is not None:
            replacement.set("alias", original_alias)
        elif name_counts[table.name.lower()] == 1:
            # The caller refers to the table by its own name, so that name is the alias
            # (`orders.id` resolves). Only when nothing else in the statement is called so:
            # otherwise the read keeps its generated name, as it always did, and a clash
            # fails in the database instead of binding to the wrong table.
            replacement.set("alias", exp.TableAlias(this=table.this.copy()))
            named.append((_qualifier_key(table), table))
        _carry_table_clauses(table, replacement, dotted)
        table.replace(replacement)
    _drop_schema_from_qualified_columns(scopes, named)
    if not ctes:
        return RewrittenQuery(sql=_render(parsed, dialect), parameters=parameters)
    if not isinstance(parsed, exp.Query):
        raise ValidationError(f"{parsed.key.upper()} may not reference a catalog table.")
    new_ctes = [
        exp.CTE(this=body, alias=exp.TableAlias(this=exp.to_identifier(alias)))
        for alias, body in ctes.items()
    ]
    existing = parsed.args.get(_WITH_ARG)
    if existing is None:
        parsed.set(_WITH_ARG, exp.With(expressions=new_ctes))
    else:
        # The query's own CTEs may read the tables being rewritten, so the
        # generated ones have to be defined first, not appended after.
        existing.set("expressions", [*new_ctes, *existing.expressions])
    return RewrittenQuery(sql=_render(parsed, dialect), parameters=parameters)


@_fail_closed
def check_blacklist(sql: str, dialect: str = "duckdb") -> None:
    """Reject SQL that is not a plain read, or that reads files directly.

    Two structural rules carry the weight here, together: a submitted query
    must parse to a single `exp.Query` (a `SELECT`, a `WITH ... SELECT`, a
    DuckDB `FROM`-first query, a `UNION`) or to a bare `exp.Values` list
    (`VALUES (1, 'a'), (2, 'b')`, which reads nothing; every check below walks
    its rows as it walks a `SELECT` list — see `_TOP_LEVEL_READS`), and
    `_reject_statements_inside` then refuses a statement that is
    not a read anywhere inside it. The first rejects the whole *class* of
    non-query statements at the top level in one check —
    `LOAD`, `CREATE SECRET`, `SET`, `PRAGMA`, `DESCRIBE`, `EXPLAIN`,
    `ATTACH`, `COPY`, `INSTALL` — and, because sqlglot's duckdb dialect
    parses `;`-separated text to a single `exp.Block`, multi-statement
    payloads too; the second covers the ones that sit inside a CTE or subquery.
    The named checks exist only to give a precise error message for the
    statements a user is most likely to try; they are not what makes the check
    sound.

    A second structural rule stands beside it: no submitted query may run as
    a recursive query — neither a `WITH RECURSIVE` clause nor a `CONNECT BY`
    (which sqlglot renders as `WITH RECURSIVE`) may appear anywhere. That is a
    deliberate non-support decision, not an oversight — sqlglot and DuckDB
    disagree about what a recursive CTE's name binds to inside its *base*
    term, and every scope-based classifier here inherits that disagreement
    as an authorization bypass. See `_reject_recursive_ctes`.

    A third structural rule: no submitted query may contain a query parameter
    or placeholder (`?`, `$1`, `$name`) anywhere. The query process sets each
    table's resolved file URLs as session variables, and DuckDB 2.0 reads the
    variable for an unbound `$name`, so a user placeholder would return them
    as data — see `_reject_placeholders`.

    A fourth structural rule: no statement that is not a read may sit anywhere
    inside the query, in a CTE, a subquery, a set operation or a `LATERAL` join,
    in any letter case — see `_reject_statements_inside`.

    Parameters
    ----------
    sql
        The original, pre-rewrite SQL to check.
    dialect
        The SQL dialect to parse `sql` as.

    Raises
    ------
    ValidationError
        If `sql` uses a table function other than the generators in
        `_ALLOWED_TABLE_FUNCTIONS`; if it calls a blacklisted function or one whose name
        starts with `_INTERNAL_FUNCTION_PREFIX`; if it contains an
        `ATTACH`, `COPY`, or `INSTALL` statement (each parses to sqlglot's
        own dedicated expression class — `exp.Attach`, `exp.Copy`,
        `exp.Install` — rather than a generic command node, confirmed against
        the pinned `sqlglot` version, not assumed); if it is a `LOAD` or
        `CREATE SECRET` command; if it contains a query parameter or
        placeholder; if the statement as a whole is neither a query nor a `VALUES`
        list, or is a `VALUES` list with an alias or a clause other than `ORDER BY`,
        `LIMIT` and `OFFSET`; if a statement that
        is not a read sits anywhere inside it; or if it
        contains a `WITH RECURSIVE` clause or a `CONNECT BY`.
    """
    parsed = _parse_sql(sql, dialect)
    for item in parsed.find_all(exp.Table, exp.Lateral):
        function = _from_item_function_name(item)
        if function is not None and function not in _ALLOWED_TABLE_FUNCTIONS:
            raise ValidationError(_refused_table_function(function))
    for func in parsed.find_all(exp.Func):
        name = func.name.lower() if isinstance(func, exp.Anonymous) else func.sql_name().lower()
        if name in _NO_QUERY_USE_REASONS:
            raise ValidationError(
                f"{name} {_NO_QUERY_USE_REASONS[name]}, which no query needs, so it may not be "
                "called. Remove the call; a query reads tables only."
            )
        if name in _BLACKLISTED_FUNCTIONS:
            raise ValidationError(f"{name} may not be called directly; query the catalog instead.")
        if isinstance(func, exp.Anonymous) and _is_operator_name(func.name):
            raise ValidationError(_operator_call_message(func.name))
        if name.startswith(_INTERNAL_FUNCTION_PREFIX):
            raise ValidationError(
                f"{name} is internal to DuckDB and may not be called directly; "
                "write the SQL it stands for instead, such as CAST or BETWEEN."
            )
    for node_type, label in ((exp.Attach, "ATTACH"), (exp.Copy, "COPY"), (exp.Install, "INSTALL")):
        if parsed.find(node_type) is not None:
            raise ValidationError(f"{label} is not permitted in a submitted query.")
    for command in parsed.find_all(exp.Command):
        keyword = str(command.this).lower()
        if keyword in _BLACKLISTED_COMMANDS:
            raise ValidationError(f"{keyword.upper()} is not permitted in a submitted query.")
    _reject_placeholders(parsed)
    if not isinstance(parsed, _TOP_LEVEL_READS):
        raise ValidationError(
            f"Only a single query may be submitted, not {parsed.key.upper()}; "
            "submit one SELECT statement."
        )
    if isinstance(parsed, exp.Values):
        _reject_values_clauses(parsed)
    _reject_statements_inside(parsed, dialect)
    _reject_recursive_ctes(parsed)
    _reject_pivot_without_values(parsed)
