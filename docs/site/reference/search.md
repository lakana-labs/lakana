# Search

One search over the tables, columns, descriptions, runs and principals of an environment: the
route, the matching and ranking rules, who sees what, the limits, the command, the SDK, and how the
columns of tables that existed before search are filled in. For the steps, see
[Find a table](../how-to/find-a-table.md).

## What is matched

| Kind | Matched against | `matched` is |
|---|---|---|
| `table` | the table's name, its name with the namespace (`silver.orders`), its shown description, its declared grain | `name`, `description` or `grain` |
| `column` | the column's name, its shown description | `name` or `description` |
| `run` | the run's id (four hex characters or more, with or without dashes), its job, its repository | `id`, `job` or `repository` |
| `principal` | the principal's name | `name` |

The *grain* is the sentence the table's asset header declares (`grain:`); a table that declares none
has no grain to match. Key columns are found as columns. The declared kind (`fact`, `dimension`,
`wide`, `staging`) is not matched as text: to find the tables of one kind, filter with
`--model-kind` (`model_kind` over REST and in the SDK).

A table is a managed table, an external table, a view or an artifact, as in the table listing
(`table_kind` is `TABLE`, `EXTERNAL`, `VIEW` or `ARTIFACT`). An artifact has no columns and no
description. The *shown* description is the edit if there is one, else the project's text
([Descriptions](descriptions.md)); a column's text is searched only while the column is in the
table's schema.

Matching ignores case and finds the text anywhere. Case is ignored for ASCII letters on SQLite (local
mode) and for all letters on Postgres. `%`, `_` and `\` in the text match themselves. The
text is trimmed and must be 1 to 200 characters.

## Ranking and paging

Results are ordered by: how well the name matched (0 exact, 1 starts with, 2 contains, 3 only the
description matched), then the kind (tables, columns, runs, principals), then the name. A match in a grain ranks as a match in the description. A page has
`limit` results (1 to 100, 20 by default) after `offset` (0 or more). Both count only what you may
read.

## Who sees what

| Result | Needs |
|---|---|
| table, column | `READ_TABLE` on the table, asked per table as the table listing asks it (grants on the table, its namespace or the environment, and a task token's asset scope all count) |
| run | `READ_ENVIRONMENT` |
| principal | the right to list principals: `MANAGE_PRINCIPAL`, which the `ADMIN` role holds |

A result you may not see is left out, never refused. A description, a grain and a kind are shown, even as a snippet, only
for a table you may read. An environment that does not exist is `404` for a caller who may read
environments; for anyone else the answer is an empty page, as for an environment that exists.

The search examines at most 5,000 candidate matches in the order above. A caller who may read only
a few of very many matches may miss some; a longer or more exact text finds them.

## REST

`GET /api/v1/environments/{env}/search?q=TEXT&kind=&model_kind=&limit=&offset=`

| Parameter | Meaning |
|---|---|
| `q` | Required. The text, 1 to 200 characters after trimming. |
| `kind` | Optional: `table`, `column`, `run` or `principal`. |
| `model_kind` | Optional: `fact`, `dimension`, `wide` or `staging`. Only tables whose asset header declares this kind; columns, runs and principals are left out. Another value is `422`. |
| `limit` | Results per page, 1 to 100 (20). |
| `offset` | Results to skip (0). |

The answer is `{"results": [...], "limit": N, "offset": N, "has_more": true|false}`. Every result has
`kind`, `name` (`silver.orders`, `silver.orders.status`, the run id, or the principal's name),
`matched` and `snippet`; the rest depends on the kind and is `null` otherwise.

| Kind | Further fields |
|---|---|
| `table` | `namespace`, `table`, `table_kind`, `model_kind` (the declared kind, else `null`) |
| `column` | `namespace`, `table`, `column`, `column_type` |
| `run` | `run_id`, `job`, `repository`, `status` |
| `principal` | `principal`, `principal_type` |

`snippet` is set when `matched` is `description` or `grain`: at most 160 characters of that text around
the first hit. A bad `q`, `limit`, `offset` or `kind` is `422` and says the range.

## Command and SDK

`lakana search TEXT [--kind KIND] [--model-kind MODEL_KIND] [--limit N] [--offset N] [--env ENV]` shows one page and, when more follow, the command for the next; a table that declares a kind shows it as `name [kind]`; with `--output json`
it prints the whole answer. `client.search(environment, q, kind=None, model_kind=None, limit=20, offset=0)` returns a
`SearchPage` with `results` (`SearchHit` objects with the fields above), `limit`, `offset` and
`has_more`.

## Where the columns come from

Search finds columns in the table `table_columns`, written in the same transaction as every change of
a schema: a created table, a commit that changes the schema (also through the Iceberg REST catalog),
an external table, and a created or replaced view. Tables that existed before search have none stored.
The worker fills them in its housekeeping sweep: up to 50 tables per pass, those with no columns
stored yet, in no fixed order. Views and external tables are read from the database; a managed table's
columns are read from its metadata file in object storage. The sweep runs every
`LAKANA_RUN__SWEEP_INTERVAL` seconds (3,600 by default), so 1,000 existing tables take about 20 hours;
a table whose columns are not stored yet is found by its name and description, but not by its columns.
With the sweeps switched off (`LAKANA_RUN__SWEEP_INTERVAL=0`) they are never filled in, except by the
table's next schema change. A table whose metadata cannot be read is logged and skipped.
