# Moving SQL transformations to Lakana

You may already transform data with SQL files, one file per table, run by a tool that works out
the order from the references between them. This page says what each of the usual concepts of such
a tool becomes in Lakana, what has no counterpart (and what to do instead), what you get in
return, and when to stay where you are. A worked conversion, step by step, is the tutorial
[Move a SQL project to Lakana](../tutorials/move-a-sql-project.md).

Lakana runs no other tool's project, and it has no converter: you rewrite each file by hand. The
files are mostly SQL you already have, so the work is in the edges: how a table refers to another,
where a test goes, and how data gets in. Lakana decided against a second way to define a table,
so there is one way to write an asset.

## What each concept becomes

The left column uses the words most SQL-file transformation tools share; your tool may use others.

| In your tool | In Lakana | What changes |
|---|---|---|
| A **model** (one SQL file, one table) | A **SQL asset**: `assets/<namespace>/<name>.sql` becomes the table `<namespace>.<name>` | The file is one `SELECT` (or `WITH ... SELECT`, a set operation, or a `VALUES` list), with an optional `/* lakana ... */` block on top for the description, owner, tags and how it is built. The file name is the table name; a directory is a namespace. See [Project layout](../reference/project-layout.md). |
| A **reference** to another model (a function call in a template) | The other table's **name**, written in plain SQL: `FROM silver.orders_clean` | Lakana reads the names in your SQL and builds the order of the run from them. A name that no asset of the project produces is an input that must already exist in the environment. |
| How a model is **materialised**: table, view, incremental | `materialize: table` or `view`, and for a table `write: replace`, `append`, `merge` or `history` in the front-matter | A table is replaced on every run by default. A view is a view. `write: append` adds the rows whose `watermark` column is above the highest value already in the table; `write: merge` updates the rows whose `key` the table has and adds the rest. See [Write modes](../reference/asset-front-matter.md#write-modes). |
| An **ephemeral** model (inlined into the models that use it) | A `WITH` clause, or a view | Nothing inlines a file for you. |
| A **test** (not null, unique, accepted values, relationships, or a query of your own) | A **check**: `checks/<namespace>/<table>/<name>.sql`, a `SELECT` that returns the rows that are wrong | Each of the four built-in tests is a short query, and [Check your data](../how-to/check-your-data.md#five-checks-to-copy) has five to copy. A check that fails sets the table back to what it was (`block`, the default), leaves the data and stops what is built from it (`keep`), or only reports (`warn`). Reference: [Checks](../reference/checks.md). |
| A **source** (a table that something else loads) | A table that already exists in the environment, which an asset reads by its name | Load it with `lakana table create` and `lakana table append`, register files under `raw/` with `lakana table create-external`, or write a [Python asset](python-assets.md) that fetches it. A check may test a table your project only reads: see [A table your project only reads](../how-to/check-your-data.md#a-table-your-project-only-reads). |
| A **seed** (a small CSV kept in the repository) | The CSV stays in git; **loading is a command**: `lakana table create`, then `lakana table append` (`lakana table replace` to load a changed file) | A deploy does not load files. For a handful of rows an asset made of a `VALUES` list is deployed and versioned with the code, and needs no load. |
| A **run** (build everything, in order) | A **deploy** and a **run**: `lakana deploy --env dev`, then `lakana run --env dev` | A deploy checks a commit and records it as a project version; a run executes that version, never your working copy. `lakana deploy --dry-run` shows what would change. |
| A **selection** of models (`--select`, tags, graph operators) | `lakana run --select 'silver.*'`, with `--upstream` and `--downstream` | Patterns are shell globs over table names. A **job** in `lakana.yaml` gives a selection a name and, if you want, a cron schedule: [Run a job on a schedule](../how-to/run-a-job-on-a-schedule.md). |
| A **profile** and its **targets** (dev, prod) | An **environment** (`dev`, `prod`), each with its own storage and tables | The SQL does not change between environments, because the table names do not: `silver.orders_clean` is a different table in `prod` than in `dev`. What differs is the data, the grants and the settings that `environments:` in `lakana.yaml` overrides. What Lakana calls a *profile* is only the address and sign-in of an instance that the command line uses. |
| **Descriptions** and documentation of models and columns | `description` and `columns` in the front-matter | A deploy publishes them with the tables, and `lakana search` finds tables, columns and descriptions. See [Describe a table](../how-to/describe-a-table.md). |
| The **project file** (default settings, required fields) | `lakana.yaml`: `defaults`, `requirements`, `checks`, `jobs`, `environments` | See [`lakana.yaml`](../reference/lakana-yaml.md). |

## What has no counterpart

| In your tool | In Lakana | What to do |
|---|---|---|
| **Templating and macros**, and **variables** that a template reads | None, by design. A SQL file is plain SQL, and an asset may not call `getvariable`. | Write the SQL out. Where a macro repeated an expression, compute it once in an upstream view or table and read the column. Where it generated many similar files, you have as many files as tables. Where the logic is real programming, a [Python asset](python-assets.md) is the place for it: SQL for transforms, Python at the edges. |
| **Packages** of SQL from other projects | None for SQL. | Copy the SQL you need into your project as ordinary files. A Python asset can use Python packages, including those of a [private index](../how-to/private-package-index.md). |
| **Hooks** and operations (statements before or after a run or a model) | None. An asset is one `SELECT`; a statement that changes data or the session (`INSERT`, `UPDATE`, `DELETE`, `CREATE`, `SET` and the like) is refused when you deploy. | Access is given with grants, not by a statement in a model. A job can tell a chat tool or a webhook when it fails and when it works again: [Get told when a job fails](../how-to/get-told-when-a-job-fails.md). For anything else, there is nothing yet. |
| **Snapshots** (the history of a table that changes in place, one row per version) | A SQL or Python asset with `write: history` (a dimension): each run opens a version for a new or changed key and closes the one it replaces, with `valid_from`, `valid_to` and `is_current` added. A fact points at the version valid at its own time with `at:`. | Make the `SELECT` return the current state of every row. See [Keeping the history of a dimension](../reference/history.md). |
| An incremental model that **updates** the rows it already wrote (a unique key) | `write: merge` with `key:` (and optionally `watermark:`). A row whose key the table has is updated, any other is added; nothing is deleted. A key of several columns takes at most 5,000 rows per run. `write: history` keeps every version of a dimension's rows instead (a slowly changing dimension of type 2). | Make the `SELECT` return one row per key. |
| **Several statements** in one model, or a model that writes more than one table | None. One file, one `SELECT`, one table. | Split the file into one asset for each table. |
| **Custom materialisations** | None. A view, or a table written with `replace`, `append`, `merge` or `history`. | Choose the nearest of these. |

"Planned" is not a date: the [Roadmap](roadmap.md) says what comes in which order, and an issue
is a wish until a release has it.

## What Lakana adds

A tool that only builds tables leaves the rest to you. Lakana also keeps the part that matters
once several people share the data:

- **Grants.** Nobody does anything without one, and a refusal is recorded. See
  [Give a second person access](../how-to/give-a-second-person-access.md) and
  [Why may (or may not) someone do this?](../how-to/why-may-someone-do-this.md).
- **Row filters and column masks.** Show a person only some rows of a table, or a replacement
  value instead of a column, inside every query they run:
  [Show some people only some rows](../how-to/filter-rows.md) and
  [Mask a column for some people](../how-to/mask-a-column.md).
- **An audit log** of who deployed, ran, queried and was refused: see
  [What the audit log records](security.md#what-the-audit-log-records).
- **Lineage kept by the platform.** Lakana reads the tables and columns each SQL file uses, so
  you do not declare them again: `lakana table lineage` shows them.
- **Protected environments.** In a protected environment only runs of the deployed code write, so
  nobody changes a production table by hand: see
  [Protected environments](../how-to/projects.md#protected-environments-lockdown-and-break-glass).
- **Deploys from a commit,** with a dry run that shows what changes and a rollback to the project
  version before. A table can be traced to the commit that built it.
- **Checks that act.** A failing check sets the table back or stops what is built from it, instead
  of only reporting afterwards.
- **One REST API** behind the command line and the SDK, so what you do by hand, CI does the same
  way: [Deploy from GitHub Actions without a stored secret](../how-to/deploy-from-github-actions.md).

## When to stay with what you have

- You rely on **templating, macros or packages** of SQL, and rewriting them as plain SQL would
  cost more than the governance would give.
- You rely on **snapshots**, and you cannot do without them until Lakana has a counterpart.
- You work **alone** and will keep doing so. Grants, the audit log and protected environments
  protect people and production from each other.
- Your data or a single query needs **more than one machine**, or you need streaming.
- You need a **platform for production data today**: Lakana is pre-beta, and the API and the data
  layout may still change between releases ([Upgrade policy](upgrades.md)).

The page [Where Lakana fits](where-lakana-fits.md) lists what Lakana lacks as a whole, and why it
stores tables in Apache Iceberg.

## How to move, in an order that keeps you safe

1. **Start with the end of the pipeline.** Pick one table that people use, and move the tables it
   needs, with the raw tables they read. A small project first, as in the tutorial.
2. **Load the sources and seeds** into a `dev` environment, rewrite the files, and run.
3. **Compare** the result with what your current tool builds, with queries on both, until the
   numbers agree. Keep the old pipeline running until they do.
4. **Turn the tests into checks, with `checks.on_failure: warn`** at first, so that a check you got
   wrong does not stop a run, and tighten them one by one: [Adding checks to a pipeline that
   already runs](../how-to/check-your-data.md#adding-checks-to-a-pipeline-that-already-runs).
5. **Add a `prod` environment,** deploy the same commit to it, protect it, and give people
   the grants they need.
