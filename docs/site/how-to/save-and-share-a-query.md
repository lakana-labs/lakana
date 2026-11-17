# Save and share a query

After this guide you can keep a query under a name, share it with the people who work in the same
environment, find it again, change it, and delete it, from the terminal, from Python and in the web
interface. You need
an environment where you may run queries (the `ANALYST` role gives that right); see
[Getting started](../tutorials/getting-started.md).

## Why save a query

A query you typed once is gone from your screen. You can find it again in the list of your queries
(see [Download a query result](download-a-query-result.md)), but that list is a history, not a
shelf. A saved query is SQL kept under a name in one environment. You keep it for yourself, or
share it with everyone who may run queries there, and nobody has to copy it through git or a chat.

A saved query is **text and an environment, nothing more**. It never runs by itself, and it never
runs with its author's rights: whoever runs it runs an ordinary query with their own rights, so a
row filter, a column mask or a missing grant applies to them as to any other query. A query that has
to run on a schedule, or that others depend on, is a job in code, not a saved query; see
[Run a job on a schedule](run-a-job-on-a-schedule.md).

## Save one

Give the command a name and the SQL, and the environment:

<!-- source: docs/examples/save-and-share-a-query/run.sh#create -->
```bash
lakana query saved create "Orders by status" "SELECT status, count(*) FROM gold.sales.orders GROUP BY status" --env dev
```

The command prints the name and the id of the saved query. The id is what the other commands take.
Saving does not run the query. To save a long query, give `-` as the SQL and write it on standard
input: `lakana query saved create "Daily orders" - --env dev < daily.sql`.

A name is one line of 1 to 200 visible characters; spaces around it are removed, and a line break or
an invisible character is refused. Your saved queries in an environment have different names: saving
a second one under a name you already use is refused with a sentence that names the first one's id,
so you can update it instead. Another person may use the same name for theirs.

A new saved query is **private**: only you see it. The SQL is stored as you sent it, up to the same
length as a query you submit; SQL holding a NUL character is refused.

## Share it with the environment

Add `--everyone` when you save it, or later with `lakana query saved update ID --everyone`:

<!-- source: docs/examples/save-and-share-a-query/run.sh#share -->
```bash
lakana query saved update "$id" --env dev --everyone
```

`--private` takes it back. A shared saved query is visible to everyone who holds the right to
submit queries in its environment, so nobody sees a query they could not run. They see its name,
its owner and its SQL; they cannot change or delete it. A person who wants to change it saves a
copy under their own name. Sharing does not give anyone more access to data: they run it with their
own rights.

Who sees a saved query:

| You are | You see |
|---|---|
| its owner | it, always, with its SQL; also if you may no longer run queries in the environment, so that you can clean up |
| someone who may run queries in the environment | the shared ones, with their SQL |
| an administrator | every saved query (see below for a private one's SQL) |
| anyone else | nothing: it is answered as a saved query that does not exist |

A saved query of an environment that has been deleted is hidden with it, and is back, unchanged,
when the environment is restored.

## Find and open your saved queries

`list` shows the saved queries you see in one environment, by name, and `--all-environments` shows
every environment at once, with an `environment` column:

<!-- source: docs/examples/save-and-share-a-query/run.sh#list -->
```bash
lakana query saved list --env dev
lakana query saved list --all-environments
```

Without `--env` and without `--all-environments`, the command uses your profile's environment. The
list never holds the SQL. A long list ends with the command that shows the next page.

`show` prints one saved query's fields and then its SQL. With `--sql` it prints only the SQL, so you
can run it by piping it into `lakana sql`:

<!-- source: docs/examples/save-and-share-a-query/run.sh#show-sql-run -->
```bash
lakana query saved show "$id" --env dev --sql | lakana sql - --env dev
```

The query runs as a query of yours, with your rights, and is in your list of queries like any other.
If the SQL holds a control or invisible character, `--sql` prints nothing and exits with status 1,
since the escaped text it could print would be another query; read it with `--output json`.

## Change and delete

Only the owner changes a saved query, and only while the owner may still run queries in the
environment. `update` takes the fields to change (`--name`, `--sql`, `--private` or `--everyone`;
`--sql -` reads the SQL from standard input); it reads the saved query first and sends every field, so what you leave out stays as it is:

<!-- source: docs/examples/save-and-share-a-query/run.sh#update -->
```bash
lakana query saved update "$id" --env dev --name "Orders by state"
```

The environment of a saved query never changes. To keep a query in another environment, save it
there under a new name.

The owner deletes a saved query, and so does an administrator. `delete` asks you first; `--yes` skips
the question:

<!-- source: docs/examples/save-and-share-a-query/run.sh#delete -->
```bash
lakana query saved delete "$id" --env dev --yes
```

Each item of the list says what you may do with it: the owner's `actions` are `update` and
`delete`, an administrator's who is not the owner is `delete`, anyone else's is empty. An owner who
no longer holds the right to submit queries in the environment still sees and deletes their saved
queries, but has `delete` only, until the right is back.

## What an administrator sees of a private query

An instance administrator sees the **name, the owner and the environment** of every private saved
query, and may delete it. The SQL of someone else's private query stays hidden: an administrator
reads it only if they also hold `READ_QUERY_TEXT`, a right that no role holds, not even `ADMIN`
(see [Access](../reference/access.md)). Without it, the answer has no `sql` field at all (the JSON has
no `sql` key; the SDK reads `None`), `lakana query saved show` prints `You may not read the SQL of
this query.`, and with `--sql` it prints that sentence as an error and exits with status 1. A shared
saved query's SQL is not hidden from an administrator.

Seeing someone else's private saved query, by id or in a list, is itself recorded in the audit log
(an `ACTIVITY_VIEWED` event with `view` `saved_queries`, once per administrator and environment
an hour, never with the name or the SQL; see [Lists](../reference/lists.md#audit)). If that event
cannot be written, the read is refused with 503 and shows nothing.

## When the owner leaves

Lakana deactivates a person and does not delete them, so a deactivated owner's saved queries stay
as they were. When an operator removes a principal's row from the database, that person's **private**
saved queries go with it, and their **shared** ones stay. A shared saved query whose owner is gone
shows `former owner` in the list; nobody can change it, and only an administrator can delete it.

## From Python

`client.saved_queries` has the same operations. `list` returns one page and `iter` goes through
every page; without an environment, both span every environment you may query:

```python
saved = client.saved_queries.create(
    "dev",
    "Orders by status",
    "SELECT status, count(*) FROM gold.sales.orders GROUP BY status",
    visibility="everyone",
)
for item in client.saved_queries.iter():  # every environment
    print(item.environment, item.name, item.owner, item.actions)

shown = client.saved_queries.get("dev", saved.id)
if shown.sql is not None:  # None when you may not read it
    run = client.queries.submit(shown.sql, environment="dev")

client.saved_queries.update(
    "dev", saved.id, name="Orders by state", sql=shown.sql, visibility="private"
)
client.saved_queries.delete("dev", saved.id)
```

`update` replaces every field, so give them all. The `lakana.SavedQuery` has `sql`, `visibility`,
`owner` (`None` for a former owner, with `former_owner` true) and `actions`. A refusal is raised as
`lakana.PermissionDenied`, `lakana.NotFound`, `lakana.Conflict` or `lakana.ValidationError`, with
the server's sentence. A task's token may not use saved queries at all; they belong to people and
service accounts.

## What the audit log records

Creating, changing and deleting a saved query write `SAVED_QUERY_CREATED`, `SAVED_QUERY_UPDATED` and
`SAVED_QUERY_DELETED` events; see [Export and keep the audit log](export-and-keep-the-audit-log.md).
They name the saved query's id and name, its visibility and its owner, and never its SQL.

## In the web interface

The **Queries** screen keeps your saved queries beside the editor, under **Saved**; on a narrow
window they are behind the **Queries ▾** button. **Show**, above the lists, narrows them (and
**Recent**) to one environment, or shows **All environments**.

**Save the query in the editor:**

1. Choose its environment under **Environment**, and write the SQL.
2. Press **Save…**. Give it a **Name**, and under **Who may run it** choose **Only me** or
   **Everyone who can run queries in dev**. The window names the environment it is saved in.
3. Press **Save as new**. The screen moves to the saved query's own address, which you can
   bookmark or send to a colleague who may see it.

A name you already use in that environment is refused in the window with the server's sentence;
what you typed stays, so you can change the name.

**Open one:** choose its name under **Saved**. Its SQL comes into the editor, and **Environment**
moves to its environment. A shared one says **everyone**; one that is not yours says "by" and its
owner, or **former owner**.

**Change one of yours:** open it, edit, press **Save…**, then **Save**. **Save as new** keeps the
old one and saves a copy instead, for example under a new name. A saved query that is not yours
offers only **Save as new**, as your own copy. If you moved **Environment** to another environment,
only **Save as new** is offered, in that environment: a saved query's environment never changes.

**Delete one:** open it, press **Delete…**, and **Delete** in the window that asks "Delete the saved
query …?". The SQL stays in the editor and its runs stay under **Recent**; the screen says
"Deleted …".

An administrator who opens someone else's private saved query sees its name, its owner and its
environment, the line "You may not read the SQL of this query.", an empty editor and **Delete…**;
**Run** and **Save…** stay off.

**Recent** lists the queries you wrote in the last 30 days, newest first, with their state, how long
they ran and their environment. One opens on its own address with its SQL, its state and, while the
server keeps it, its result.
