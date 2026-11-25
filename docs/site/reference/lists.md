# Runs, queries and deploys lists

The three lists that show what happened in an environment: the filters of each, the order, the
cursor that pages through it, who sees what, the commands, the SDK and the routes. For the steps,
see [Find a run, a query or a deploy](../how-to/find-a-run.md). The dropped tables of an
environment, its expiring tables and its namespaces are paged the same way; they are described
under [Other paged lists](#other-paged-lists).

## The lists

| List | Command | Needs | Order |
|---|---|---|---|
| Runs | `lakana runs list` | `READ_ENVIRONMENT` | creation time, newest first; equal times by id |
| Queries | `lakana query list` | `SUBMIT_QUERY`; `MANAGE_ENVIRONMENT` shows everyone's | running, then queued, then (with `--all`, or an ended status) ended ones |
| Queries of every environment | `lakana query list --all-environments` | none: an environment where you hold neither `SUBMIT_QUERY` nor `MANAGE_ENVIRONMENT` is left out | as the queries of one environment, over all of them |
| Runs of every environment | `lakana runs list --all-environments` | none: an environment where you lack `READ_ENVIRONMENT` is left out | as the runs of one environment, over all of them |
| Deploys | `lakana deploys list` | `READ_ENVIRONMENT` | creation time, newest first; equal times by id |
| Deploys of every environment | `lakana deploys list --all-environments` | none: an environment where you lack `READ_ENVIRONMENT` is left out | as the deploys of one environment, over all of them |

Every filter narrows the list; filters combine with AND. A filter is never a way to see more than
the list shows without it.

## Other paged lists

| List | Command | Needs | Order |
|---|---|---|---|
| Dropped tables of an environment | `lakana table list --dropped` | `READ_ENVIRONMENT`; a table you may not read is left out | drop time, newest first; equal times by table id |
| Expiring tables | `lakana table expiring [--namespace NAMESPACE]` | `READ_ENVIRONMENT`; a table you may not read is left out | expiry time, soonest first; equal times by name, then id |
| Namespaces | `lakana ns list [--parent PATH] [--depth 1]` | `READ_NAMESPACE` on the environment | the namespace's path parts, so `a`, `a.b`, then `ab` |
| Orphans | `lakana orphans` | `READ_ENVIRONMENT`; an orphan you may not read is only counted as hidden | repository, then table name |
| Artifacts | `lakana artifact list [--namespace NS]` | `READ_ENVIRONMENT`; an artifact you may not read is left out | namespace parts, then name |

Each row of the dropped tables, the orphans and the artifacts has `actions`: what your rights
allow on it. A dropped table has `["undrop"]` when you hold `DROP_TABLE` on its namespace and
`CREATE_TABLE` on its name. An orphan has `["drop"]` when you hold `DROP_TABLE` on it; a dropped
orphan (`exists: false`) has none, so restore it from the dropped tables. An artifact has
`["drop"]` on the same right. A client offers only these actions. The server still checks every
request. See [What you may do with a table](table-list.md#what-you-may-do-with-a-table).

`--namespace` keeps that namespace and the ones beneath it. `--parent` keeps every namespace below
the parent at every depth, and `--depth 1` keeps only its direct children (the top level without a
parent). They take `--limit` and `--cursor` like the lists above.

A script that read one of these lists in one request now reads only its first 100 rows. Follow
`X-Next-Cursor` (the expiring tables, the namespaces and the artifacts) or `next_cursor` (the
dropped tables of an environment and the orphans), or use `client.tables.iter_dropped(env)`,
`client.tables.iter_expiring(env)`, `client.namespaces.iter(env)`, `client.orphans.iter(env)` and
`client.artifacts.iter(env)`, which walk every page.

## Filters

| Filter | Option and parameter | Values | Lists |
|---|---|---|---|
| Repository | `--repo`, `repository` | a repository name; an unknown name is `404` | runs, deploys |
| Status | `--status`, `status` | runs: `queued`, `running`, `succeeded`, `failed`, `cancelled`. Queries: the same words in any letter case (the list shows them in capitals). Repeat the option or separate the values with commas: `failed,cancelled` | runs, queries |
| Trigger | `--trigger`, `trigger` | `schedule`, `api`, `manual`, `deploy`; repeat or separate with commas. A run started with `lakana run` has none and matches no trigger | runs |
| Job | `--job`, `job` | the name of a job | runs |
| Since | `--since`, `since` | an ISO 8601 time with a time zone, for example `2026-10-18T09:30:00Z`. Runs: the run was created at or after it. Queries: the ended queries listed are those that ended at or after it, of any age | runs, queries |
| Submitter | `--submitter`, `submitter` | a principal's name; none of that name's queries is shown to someone who may not see them | queries |
| Origin | `--origin`, `origin` | where the query came from: `written` (SQL someone wrote), `preview` (a table's preview), `check` (the rows or the count of a check), `write` (SQL that writes a table), `maintenance` (a table's maintenance) or `task` (a read or write of a pipeline task). Repeat the option or separate the values with commas | queries |

`--since` on the command line also takes a date (`2026-10-18`) or a time without a time zone, which
is read in the time zone of your computer. The route needs the time zone: `since=2026-10-18T09:30:00`
is refused with `422`.

A query status that has ended (`succeeded`, `failed`, `cancelled`) lists the queries that ended in
the last 24 hours, with or without `--all`. With `--since`, it lists those that ended at or after that
time instead, however long ago: `--all --since 2026-10-01` lists every query that ended since
the first of October. `--since` changes only the ended queries; the running and queued ones are
always listed.

`--all-environments` lists the queries of every environment you may query at once, in the order of
the list of one environment, and adds an `environment` column after the id. In each environment you
see what its own list shows you: everyone's queries where you hold `MANAGE_ENVIRONMENT`, your own
elsewhere. An environment where you may not query is left out; the list is never refused for it.
It does not take `--env`.

## Paging

`--limit` (`limit`) is the most rows per page: 1 to 200, 100 by default. When more rows follow, the
route answers with the header `X-Next-Cursor`, and the command prints the line that shows the next
page:

<!-- output -->
```text
More results: lakana runs list --env dev --status failed --limit 50 --cursor eyJrIjogInJ1bnMiLCAibyI6ICIyMDI2LTEwLTE4VDA5OjMwOjEyLjM0NTAwMCswMDowMCIsICJpIjogIjVjMGI3ZDRlLTJmMWEtNGI4ZS05ZDYzLTBhMWU3YzRmOWIyMSJ9
```

With `--output json` the line goes to stderr, so the JSON on stdout stays one list. The cursor is
opaque: pass it back unchanged, with the same filters and the same limit.

A page continues strictly after the last row of the previous one. A run, query or deploy that
arrives while you page is never shown twice, and none that was there is skipped. The one exception
is a query that changes from queued to running between two pages, which moves to another part of
the order of queries. The cursor belongs to its list: the cursor of the runs list is refused by the
queries list, with `422` and a message that names both lists, and so is the cursor of the queries of
one environment by the list of every environment. Start again without `cursor`.

## Sorting

Every list that can grow long sorts by a column on the server, so the order holds across every
page: `--sort FIELD` and `--order asc|desc` on the command line, `sort` and `order` in the SDK and
on the route. Without them a list keeps its own order, and without `--order` a field runs in its
own direction. In the web interface the same order is chosen by pressing a column's header, and it
stays in the address, so a shared link opens sorted the same way.

| List | Command | Fields (default first) | Without `--order` |
|---|---|---|---|
| Runs, of one environment or every one | `lakana runs list` | `created`, `name` (the job's, else the repository's), `status` | `created` newest first; the others from A, runs of one name or state newest first |
| Deploys, of one environment or every one | `lakana deploys list` | `created`, `status` | `created` newest first; `status` from A, deploys of one state newest first |
| Jobs | `lakana job list` | `name`, `status` (the last run's), `last_run`, `next_run`; by repository, then name, without `--sort` | `last_run` latest first; the others from A or soonest first; a job without a value last |
| Connections, of one environment or every one | `lakana connection list` | `created`, `name`, `status` (the last test: failed first, then queued or running, then passed, never tested last) | `created` newest first; the others ascending, one state by name |
| Saved queries, of one environment or every one | `lakana query saved list` | `name`, `updated`, `created` | `name` from A; the times latest first |
| Principals (Access › People) | `lakana principal list` | `created`, `name`, `status` (active first, then by name) | `created` newest first; the others ascending |
| Audit log | `lakana audit list` | `occurred` (only `--order`) | newest first; `asc` is oldest first |

Names sort by code point after lower-casing, so `alpha`, `Beta`, `gamma` come in that order. Only
the letters A to Z are lower-cased the same everywhere: SQLite lower-cases ASCII letters only,
PostgreSQL every letter its database's locale knows, and the jobs list folds names in Python
(`ß` as `ss`), so a name with other letters can sort elsewhere on another database. Saved queries
sort by their folded name (`casefold`), stored with them, the same everywhere. Equal values sort
by the row's id, so a row whose sort value does not change while you page is never repeated or
skipped. A row whose value changes between two pages (a run that starts while the list is sorted
by state, a saved query edited while it is sorted by Changed) can move from one page to another:
it may come again on a later page, or not at all. The web interface shows a row that comes again
once. A sort shows exactly the rows the list shows without it: it never adds a row you may not
see, and the audit log records a read the same way in every order. Send the same `sort` and `order` with every page: the cursor names its
order, and a cursor of another order, or of the list's own order, is refused with `422` and a
message that names both, instead of being read in the wrong order. Another field, or another
direction, is `422` with the fields the list takes.

## Refusals

| Cause | Answer |
|---|---|
| a status, trigger, origin, `sort`, `order` or `limit` that is not allowed | `422`; the message names the parameter and the allowed values |
| `since` without a time zone, or not a time | `422`; the message shows how to write it |
| a cursor that was not issued, or came from another list or another order | `422` |
| an unknown repository | `404` |
| a caller who may not read the environment | `403` |
| a manager's list that shows other people's queries, or a page of the audit log that shows other people's events, and could not be recorded in the audit log (see [Audit](#audit)) | `503` with `Retry-After`; nothing is listed, and the message names the cause |

## Audit

A list of runs or deploys is not audited, and neither is a list of your own queries. When a manager's
query list shows someone else's query, or filters on someone else with `--submitter`, the audit log gets
one `ACTIVITY_VIEWED` event (a page of `--all-environments` one for each environment you manage
where the page shows someone else's query or `--submitter` names someone else), and so does seeing
the readers of a table
([table health](table-health.md#audit)), reading the audit log itself (`lakana audit list`) when
the page shows someone else's events or filters on someone else with `--principal-id`, and an
administrator's read of someone else's private saved query (`lakana query saved list` or `show`;
[Save and share a query](../how-to/save-and-share-a-query.md)), one event per environment shown:

| Field | Value |
|---|---|
| principal | who looked |
| object | the environment of the query list or of the saved queries, the table whose readers were shown, or the instance for the audit log |
| `detail.view` | `queries`, `table_readers`, `audit_log` or `saved_queries` |
| `detail.filters` | the filters of the first look: `include_ended` (true when `since` listed the ended runs), `status`, `origin`, `since` (UTC, only when given) and `limit` for queries, `days` for readers, `event_type`, `environment`, `since`, `until` and `limit` for the audit log, `limit` for a list of saved queries (none for one saved query) |
| `detail.rows_shown` | how many rows the answer showed |
| `detail.others_shown` | how many of them were other people's (a deleted principal's, and an event of nobody, such as a failed sign-in, included); for saved queries, other people's private ones |
| `detail.distinct_others` | how many other people those rows belong to |
| `detail.targeting_key` | `{"submitter": "NAME"}` when the query list was filtered on someone else, `{"principal_id": "ID"}` when the audit log was, else `null` |

The event never holds the names of the people seen, the ids of their queries or any SQL. The same
person, view, environment or table and targeting key write one event per window: the next page,
another status or event type, `--all` and a screen that refreshes itself add nothing. Looking only at
your own queries or your own events, filtering on yourself, and a task's token record nothing. An
instance administrator is recorded like anyone else.

If the event cannot be written, for example because the database is full, the list is refused with
`503` and shows nothing; try again in a moment, and if it keeps failing ask the operator to check the
server's database. Read the events with:

<!-- source: docs/examples/getting-started/run.sh#view-audit -->
```bash
lakana audit list --event-type ACTIVITY_VIEWED
```

| Setting | Default | Meaning |
|---|---|---|
| `LAKANA_VIEW_AUDIT__ENABLED` | `true` | Record the event; `false` turns it off. |
| `LAKANA_VIEW_AUDIT__WINDOW_MINUTES` | `60` | Minutes during which the same view by the same person writes no second event (1 to 1,440). |

### Other people's query text and column values in the audit log

Two fields of an event can hold content, which may be anything a table holds:

| Event | Field | What it holds |
|---|---|---|
| `QUERY_COMPLETED` | `detail.sql_text` | the SQL text of the query |
| `TABLE_WRITE` | `detail.watermark` | the highest value of an incremental write's key column, a value from the table, such as `date:2026-10-20` or `long:7340219` |

Reading the audit log needs `READ_AUDIT_LOG`; seeing these fields in **someone else's** events
needs `READ_QUERY_TEXT` as well, which no role carries, `ADMIN` included. `READ_QUERY_TEXT` covers
query text and column values in audit events, nothing else. Without it:

- **Query text** shows its SHA-256 and its length in characters, so that the same query can still
  be recognised across events. The hash is not a secret: someone who guesses the exact text can
  confirm the guess, which is easy for a short or common query.
- **A watermark** shows only `{"hidden": true}`: no hash and no length. Its values are numbers,
  dates and times, few enough to try them all against a hash, so a hash would give the value away.

<!-- output -->
```text
"sql_text": {"sha256": "9f1c...e2", "length": 57}
"watermark": {"hidden": true}
```

Your own events always show in full. Holding `READ_TABLE` on the table does not show a watermark
either: the right is about the audit log. A field of an event that the server does not know how to
classify shows `{"hidden": true}` too. An administrator gives the right to a person with
`lakana grant create --kind INSTANCE --principal NAME --action READ_QUERY_TEXT`; the grant and its
revoke are in the audit log like every grant, and `lakana access explain NAME` lists it. It is given
on the instance only.

How much text a new event stores is set on the server and every worker:

| Setting | Default | Meaning |
|---|---|---|
| `LAKANA_AUDIT__QUERY_TEXT` | `full` | `full` stores the text; `hash` only its SHA-256 and length, so the text itself is not kept; `off` neither (`null`). Events already written keep what they stored. A query's own record keeps its text for its submitter either way. |

## SDK and REST

| What | SDK | Route |
|---|---|---|
| Runs, one page of runs | `client.runs.list(env, ...)`; `client.runs.list_page(env, ...)` also returns the cursor | `GET /api/v1/environments/{env}/runs?repository=&status=&trigger=&job=&since=&limit=&cursor=` |
| Every run that matches | `client.runs.iter(env, ...)` | the same route, page by page |
| Queries | `client.queries.list(env, ...)`; `client.queries.list_page(env, ...)` | `GET /api/v1/environments/{env}/queries?include_ended=&status=&submitter=&origin=&since=&limit=&cursor=` |
| Queries of every environment | `client.queries.list_all(...)`; `client.queries.iter_all(...)` | `GET /api/v1/queries?environment=&include_ended=&status=&submitter=&origin=&since=&limit=&cursor=`; answers `{"items": [...], "next_cursor": ...}` |
| Environments where you may query | `client.queries.environments()` | `GET /api/v1/queries/environments`; answers `[{"name": ..., "protected": ...}]`, by name |
| Deploys | `client.deploys.list(env, ...)`; `client.deploys.list_page(env, ...)` | `GET /api/v1/environments/{env}/deploys?repository=&limit=&cursor=` |
| Dropped tables of an environment | `client.tables.list_dropped_page(env, ...)`; `client.tables.iter_dropped(env)` | `GET /api/v1/environments/{env}/dropped-tables?limit=&cursor=`; answers `{"items": [...], "next_cursor": ...}` |
| Expiring tables | `client.tables.expiring(env, ...)`; `client.tables.expiring_page(env, ...)`; `client.tables.iter_expiring(env, ...)` | `GET /api/v1/environments/{env}/expiring-tables?within_days=&namespace=&limit=&cursor=` |
| Namespaces | `client.namespaces.list(env, ...)`; `client.namespaces.list_page(env, ...)`; `client.namespaces.iter(env, ...)` | `GET /api/v1/environments/{env}/namespaces?parent=&depth=&limit=&cursor=` |

The routes answer with a JSON list, as they did before they could be paged, and the cursor of the
next page is in the `X-Next-Cursor` header (absent on the last page). `list` returns the rows of
one page; `list_page` returns a page with `items` and `next_cursor`. The SDK takes a status or
trigger as one word or a list of words, and `since` as a `datetime` with a time zone.

```python
from datetime import UTC, datetime

import lakana

client = lakana.Client()
page = client.runs.list_page(
    "dev", status=["failed", "cancelled"], since=datetime(2026, 10, 18, tzinfo=UTC), limit=50
)
for run in page.items:
    print(run.id, run.status, run.job)
if page.next_cursor is not None:
    more = client.runs.list_page(
        "dev",
        status=["failed", "cancelled"],
        since=datetime(2026, 10, 18, tzinfo=UTC),
        limit=50,
        cursor=page.next_cursor,
    )
```
