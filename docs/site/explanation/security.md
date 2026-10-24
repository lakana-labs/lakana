# Security overview

How Lakana decides who may do what, where it keeps secrets, what it records, and how a query is
kept inside what its author may read. This page is the short version for people who use or run an
instance; each part links to the page with the steps and the exact rules.

## Who may do what

Every request is made by a **principal**: a person or a service account, signed in with a token.
A principal may do something only because a **grant** says so. A grant gives an action, or a
**role** (a named set of actions such as `VIEWER`, `ANALYST`, `DATA_SCIENTIST`, `DATA_ENGINEER` or
`ADMIN`), to a principal or a group, on an object: the instance, an environment, a namespace or a
table. Without a grant the answer is no, and the refusal is recorded.

- **Give the smallest set.** A role on an environment covers every table in it, tables created
  later included. [Give a second person access](../how-to/give-a-second-person-access.md) shows the
  grants for a reader, and how to take access away.
- **Ask why.** [Why may (or may not) someone do this?](../how-to/why-may-someone-do-this.md) names the
  grant, role or group behind an answer. The lists and the actions are in
  [Grants, permissions, principals and groups](../reference/access.md).
- **Row filters** show a person only the rows that meet a condition. **Column masks** show a
  replacement value instead of a column. Both are applied inside every query the person runs, a join, a
  view and a write into another table included, and they follow a person or a group. Create them
  before you give access: [Show some people only some rows](../how-to/filter-rows.md) and
  [Mask a column for some people](../how-to/mask-a-column.md).
- **CI jobs need no stored token.** A GitHub Actions or GitLab CI job can exchange the OIDC ID
  token its provider gives it for a Lakana token that may queue one deploy of the job's own commit
  to one environment, when a CI trust rule an administrator wrote names its repository and branch
  or environment. The token lasts an hour and still needs the service account's grant
  ([CI trust rules and CI tokens](../reference/ci-trust-rules.md)).
- **Protected environments** accept writes only from deployed projects, so a person cannot change a
  production table by hand ([Projects and deploys](../how-to/projects.md#protected-environments-lockdown-and-break-glass)).

## Where secrets live

A secret (a storage key, a repository token, a package index password, a webhook address, a
connection's password) is stored encrypted in the instance's database, under a master key that you
set with `LAKANA_SECRETS__MASTER_KEY`. No command or page returns its value again, and no route
returns it to a person; to change it you set it again. A secret is **bound** to one use, for
example one package index, one repository or one connection, and Lakana refuses it for anything
else, so the person who registers an index cannot send a secret to a host of their choice. A secret
is decrypted only where it is used, and only while it is used.

The one answer that carries a secret's values goes to a running task, for the connection secret of
a connection its asset declares ([How a task gets its credential](../reference/connections.md#how-a-task-gets-its-credential)).
The server answers only the token of a task that is running now, in its own environment, for a name
its deployed asset declares; it answers an undeclared name the same way whether or not a
connection of that name exists. The task keeps the credential in memory for the attempt, never in
an environment variable or a file, and each value is shown as `***` in the task's log, its
failure message and its trace, also in its common encodings (a `repr`, JSON, a URL); a value
shorter than 4 characters, another transformation of it, or a value derived from it (such as an
OAuth2 access token) is not hidden unless a Lakana helper registers it. A worker that cannot
decrypt a declared connection's secret when the attempt starts does not start it; a secret
added, bound or rotated while an attempt runs may appear in the log the worker captures, so
change secrets between runs. Every fetch is in the audit log; refusals are recorded
at a limited rate per principal and the rest counted. The code you
deploy can read the credentials of the connections its asset declares and send them anywhere the
task can reach, so review an asset's `connections` with its code.

Keep the master key and the `.env` file of a Compose bundle with the people who run the instance: whoever
holds them holds every secret, and a lost master key makes every stored secret unrecoverable. The steps
are in [Manage secrets](../how-to/manage-secrets.md) and
[Use a private package index](../how-to/private-package-index.md).

## What the audit log records

The audit log is an append-only list of events: who signed in or failed to, what was granted
and revoked, who created, rotated or deleted a secret, who changed an environment, a filter or a mask,
who deployed, ran or undid something, and every refusal. A completed query is recorded with its SQL
text. For that reason reading the log needs its own right, `READ_AUDIT_LOG`, which the `ADMIN` role
holds, and seeing query text and column values in **other people's** events (the SQL text of a
query, the watermark of an incremental write) needs one more, `READ_QUERY_TEXT`, which no role
holds: an administrator sees a query's SHA-256 and length instead, and of a watermark only that
it is hidden, until someone grants the right to them by name. Your own events always show in full. An operator can also store less
text in new events (`LAKANA_AUDIT__QUERY_TEXT`: `full`, `hash` or `off`); see
[Other people's query text and column values](../reference/lists.md#other-peoples-query-text-and-column-values-in-the-audit-log).
`lakana audit list` filters it by event type, kind (`--kind connection`), environment, principal and time
([the command](../reference/cli.md#lakana-audit-list)). To keep the log bounded, repeated refusals of one
principal are counted after a number per minute that an operator sets
(`LAKANA_DENIAL_AUDIT__PER_PRINCIPAL_PER_MINUTE`, [settings](../reference/settings.md)).
For the same reason a successful sign-in is recorded once per token every 15 minutes, not on every
request: a screen that refreshes itself every few seconds would otherwise write thousands of
`AUTHENTICATION_SUCCEEDED` events a day. The next event of the token says in `detail.suppressed` how
many sign-ins were left out. Each token counts on its own, and its first use, a new token and the
first use after a refused attempt always write an event. Each server process counts on its own, so
a deployment with two server processes can write two events per window. Failed sign-ins are
recorded as before. `LAKANA_AUDIT__SIGN_IN_WINDOW_MINUTES` sets the window (0 records every
request; [settings](../reference/settings.md)).

Seeing other people's activity is recorded too. When a manager lists other people's queries
(`lakana query list`), an administrator sees who read a table (`lakana table health`), or someone
reads other people's events in the audit log itself (`lakana audit list`), the log gets
an `ACTIVITY_VIEWED` event: who looked, at which environment or table, with which filters, and how many
rows of how many other people the answer showed, never their names or their SQL. It is written once per
person and view per hour, so paging, other filters and a screen that refreshes itself add nothing; a
filter on one person (`--submitter`) is its own view. Looking only at your own activity records nothing.
If the event cannot be written, the read is refused rather than served unrecorded. The events and
the setting that turns them off are in [the lists reference](../reference/lists.md#audit).

## How a query is kept inside its limits

Policies are applied by rewriting the SQL a person submits, but Lakana does not rely on the rewrite
alone, and it does not try to forbid dangerous DuckDB functions one by one. A query is kept inside
what its author may read by **credential scoping**:

- The worker works out, from the catalog, the files the author may read, and gives DuckDB a
  time-limited link to exactly those files.
- The process that runs the query holds no storage credential, no database address and no token,
  and it opens only the locations of the tables the query reads (in local mode, their directories).
  A query that reads another path or address is refused, and one it was not given a link for has
  nothing to read it with.
- Limits on time, memory, rows and bytes stop a query that asks for too much
  ([Stop a query](../how-to/stop-a-query.md)).

A query only reads. It is one `SELECT` (also with `WITH`, or a set operation such as `UNION`), or
a `VALUES` list such as `VALUES (1, 'a'), (2, 'b')`, whose columns are named `col0`, `col1` and
so on. A statement that changes data or the session is refused wherever it sits, also inside a CTE
or a subquery, and so is a query that reads files directly instead of a table: register the data
as a table and select from it. A `PIVOT` lists the values of its `ON` column, as in
`SELECT * FROM (PIVOT gold.sales ON region IN ('eu', 'us') USING sum(amount))`; without the list
it is refused with `A PIVOT needs the values of its ON column listed`, because DuckDB would run it
as two statements.

## What this does not protect

- **Under the local runner, one instance is one trust domain for Python code.** A Python task runs as
  the worker's operating system user, so someone who may deploy Python code can read what the worker
  reads, filters and masks included. Deploy Python only from people you would trust with the stored data,
  or run one instance per group that does not trust the other
  ([why](python-assets.md#isolation-one-instance-is-one-trust-domain)). With the docker runner, the
  Compose bundle's default, each task runs in a container without the worker's credentials; containers
  share the host's kernel, so this is not a sandbox for hostile code
  ([Run Python tasks in containers](../how-to/run-python-tasks-in-containers.md)).
- **Files read outside Lakana.** A filter or a mask guards Lakana's own query path, not the files in
  the object storage. Keep the storage credentials with the people who run the instance.
- **Local mode.** With storage on the local disk there is no cloud credential to scope, so a local
  instance relies on the other checks alone. Use a team deployment ([Deploying with Compose](../how-to/deploy-with-compose.md))
  for more than one person.

For the connections an instance makes, see [What leaves the machine](what-leaves-the-machine.md).
