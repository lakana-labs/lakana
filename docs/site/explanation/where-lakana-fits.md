# Where Lakana fits, and why Apache Iceberg

You may already run SQL files on DuckDB, start them from a scheduler and keep the results in a
table format. This page says what Lakana adds to such a stack, what it does not have, why it
stores tables in Apache Iceberg and what that costs, and when you are better off staying with
what you have. What Lakana is for as a whole is in [Scope and philosophy](scope.md).

## What Lakana adds

Lakana runs your SQL and Python files and stores the results, as a stack you assemble yourself
does. What it adds is the part that keeps several people, and production, safe:

- **Access rules.** Nobody may do anything without a grant, and a refusal is recorded. A row
  filter shows a person only some rows; a column mask shows a replacement value instead of a
  column. Both apply inside every query the person runs, joins, views and writes into other tables
  included. See [Give a second person access](../how-to/give-a-second-person-access.md),
  [Show some people only some rows](../how-to/filter-rows.md) and
  [Mask a column for some people](../how-to/mask-a-column.md).
- **An audit log.** Who signed in, who was granted what, who deployed, ran or changed something,
  every completed query with its SQL text, and every refusal. See
  [What the audit log records](security.md#what-the-audit-log-records).
- **Lineage kept by the platform.** Lakana reads which tables and columns each SQL file uses, and
  which tables a Python file reads. You do not maintain it by hand.
  `lakana table lineage` shows it ([the command](../reference/cli.md#lakana-table-lineage)).
- **Protected environments.** In a protected environment only runs of the deployed code write;
  a person cannot change a production table by hand, whatever their grants. For an emergency, a
  person with the right to manage the environment can make a single write, with a reason that is
  recorded. See
  [Protected environments](../how-to/projects.md#protected-environments-lockdown-and-break-glass).
- **Deploys from a commit.** A deploy takes a commit from your git repository, shows what will
  change, and can be rolled back. With production in a protected environment, only deployed code
  writes there, so you can always say which commit produced a table. See [Deploy, dry-run, roll back](../how-to/projects.md#deploy-dry-run-roll-back).
- **One REST API for all of it.** The command line and the Python SDK are clients of the same API,
  so whatever you do by hand, CI can do too, for example
  [start a job](../how-to/run-a-job-on-a-schedule.md). The [reference pages](../reference/index.md)
  list the routes of each feature.

## What it does not have

- **No templating and no packages for SQL.** A SQL file holds exactly one query, and it names
  other tables by their names ([Project layout](../reference/project-layout.md)). There are no
  macros and no shared packages of SQL; what to do instead is in
  [Moving SQL transformations to Lakana](moving-sql-transformations.md).
- **Simple orchestration.** A job starts by hand, through the API, or on a cron schedule. A job
  started by another job or by an event, and building a table by merging changed rows, come after
  v1.0.0 ([Roadmap](roadmap.md#after-v100)).
- **No hosted service.** You run Lakana yourself, on a laptop, a server or a cloud account.
- **No web interface yet.** You work through the command line, the SDK or the REST API; the
  interface is planned before the public beta.
- **One machine per query.** Each query runs on DuckDB on one worker, and a worker runs one query
  at a time. You scale by adding workers; spreading one query across machines is out of scope by
  decision ([Scope](scope.md#out-of-scope-by-decision)). There is no streaming either.
- **Python tasks are isolated, not sandboxed.** The Compose bundle runs each task in a container
  of its own, which keeps projects apart from each other and from the worker, but shares the
  host's kernel: it is no sandbox for hostile code. Local mode runs tasks as processes of the
  worker, so there whoever may deploy Python code can read what the worker reads
  ([why](python-assets.md#isolation-one-instance-is-one-trust-domain)).
- **Not ready for production data.** Lakana is pre-beta: the API and the data layout may still
  change between releases ([Upgrade policy](upgrades.md)).

## Why Apache Iceberg

- **An open table format.** A table is a set of data files and metadata files in your own object
  storage (any store that speaks the S3 API) or in local files, laid out as the open Apache Iceberg
  specification says. Your data is not kept in a format only Lakana understands.
- **Other engines can read the tables.** Lakana serves an Iceberg REST catalog next to its own API.
  Another engine, or your own Iceberg client, can find the tables through it and, with access to
  the storage, read them without Lakana's query engine. Grants and protected environments still
  apply to it.
- **A standard interface.** The catalog speaks the Iceberg REST catalog protocol, not one of
  Lakana's own, so a tool that supports that protocol needs nothing from Lakana to connect.

What that choice costs:

- **Tables are read by field id**, as the specification requires. A renamed column keeps its data,
  but a column dropped and added again is a new column, and its older rows read as NULL. A table
  with a column type Lakana cannot read yet, such as a nanosecond timestamp, is refused as a whole
  ([When a table's schema changes](../how-to/filter-rows.md#when-a-tables-schema-changes)).
- **Commits are checked.** A commit from another client through the catalog is refused when a data
  file has no field ids, when it adds more than 2,000 files, or when the check takes longer than
  60 seconds; split a large load into several commits
  ([the refusals](../reference/checks.md#when-a-commit-through-the-iceberg-rest-catalog-is-refused)).
- **Iceberg tables need Parquet data files.** The data files of an Iceberg table must be Parquet,
  and a commit that adds ORC or Avro files is refused
  ([the refusals](../reference/checks.md#when-a-commit-through-the-iceberg-rest-catalog-is-refused)).
- **Filters and masks follow type rules.** Because a filter or mask runs on every stored value,
  Lakana refuses one that would need a conversion (`region = 5` on a text column), and a mask must
  give a value of the column's own type. A filter or mask names a column, so after a rename reads
  are refused until an administrator creates it again and revokes the old one
  ([An expression must not fail](../how-to/filter-rows.md#an-expression-must-not-fail-on-any-stored-value)).
- **Snapshots need maintenance.** Every write keeps the table's earlier state as a snapshot. Old
  snapshots and files that nothing uses any more stay in storage until you run
  [`lakana table maintain`](../reference/checks.md#lakana-table-maintain).
- **Filters and masks guard only Lakana's query path.** Anyone who reads the files with storage
  credentials of their own reads them unfiltered; Lakana's catalog gives out no storage credentials
  ([What a mask does not protect](../how-to/mask-a-column.md#what-a-mask-does-not-protect)).

## When to stay with what you have

- **You work alone and will keep doing so.** Grants, the audit log and protected environments
  protect people and production from each other. One person on their own files does not need them.
- **Your data or your queries need more than one machine.** Lakana runs each query on one worker
  and will not spread it across machines.
- **Your project relies on SQL templating, macros or shared packages.** Lakana has nothing for
  them; you would write each file out as plain SQL.
- **You need streaming, or a hosted service.** Lakana works on batches, and you run it yourself.
- **You need a platform for production data today.** Lakana is pre-beta.
