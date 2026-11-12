# See where a table is defined

After this guide you can find the file and line in git that define a table, and read that file
without leaving the terminal. You need a project deployed to the environment (see
[Projects](projects.md)) and the right to read the table.

A table that a project builds is **defined** by one of the project's asset files. Lakana records
the files of each deploy, so it can tell you which file, at which commit, built the table you are
looking at.

## Find the file

Name the table as `namespace.table`:

<!-- source: docs/examples/see-where-a-table-is-defined/run.sh#where -->
```bash
lakana table definition silver.orders
```

<!-- output: see-where-a-table-is-defined#where -->
```text
Defined in assets/silver/orders.sql, line 1
Repository  analytics
Commit      f3d2be0 (deployed 2026-11-12 23:50 UTC)
```

The answer is the file of the environment's current deployment, at the commit that was deployed,
never the head of the branch: what you read is what built the table, even when the branch has
moved on since. `deployed` is when that version was first deployed to the environment.

When the repository is on github.com, gitlab.com, codeberg.org, gitea.com, bitbucket.org or Azure
DevOps, a fourth line, `Open`, gives a link to the line on the git host. A repository on a git
server of your own, such as a self-hosted GitLab, or a local one as in this example, has no link.

A table that was created by hand, or renamed after a deploy, is not defined by a project, and the
command says so:

<!-- source: docs/examples/see-where-a-table-is-defined/run.sh#undefined -->
```bash
lakana table definition silver.notes
```

<!-- output: see-where-a-table-is-defined#undefined -->
```text
'silver.notes' is not defined by a project deployed to 'dev'; it was created or renamed outside a project.
```

## Read the file

`--show` also prints the file, as it was deployed:

<!-- source: docs/examples/see-where-a-table-is-defined/run.sh#show -->
```bash
lakana table definition silver.orders --show
```

<!-- output: see-where-a-table-is-defined#show -->
```text
Defined in assets/silver/orders.sql, line 1
Repository  analytics
Commit      f3d2be0 (deployed 2026-11-12 23:50 UTC)
/* lakana
description: One row per customer order.
*/
SELECT * FROM (
  VALUES
    (1, 'open'),
    (2, 'shipped'),
    (3, 'cancelled')
) AS orders(order_id, status)
```

The file goes to standard output and the location lines to standard error, so redirecting
standard output saves the file alone. Tabs and line breaks are kept. A character that could change
what your terminal shows is printed as an escape instead, such as `\x1b` for an escape character
or `‮` for a right-to-left override; accents, other scripts and emoji are printed as they are.

`--output json` prints the answer for a script: the location's fields, and with `--show` the file
as `content`, with `bytes`, `reason` and `message`. There the text is as JSON writes it, without
the escapes above, so read it with a program rather than on the screen.

## Who may read the file

Everyone who may read the table sees where it is defined. The file itself can show more than the
table does: the other tables it reads, and the logic and values written into it. So Lakana shows it
only to a person for whom all three of these hold:

- you may read the environment;
- you may read every table the file uses, and none of them is a view (other than the table
  itself), so a file that reads a view is shown to nobody here;
- no row filter or column mask applies to you on any of those tables.

These are checked when you ask, against the rules of that moment (see
[Access](../reference/access.md), [Filter rows](filter-rows.md) and [Mask a column](mask-a-column.md)).

A password, token or key typed into a project file is shown to everyone who passes these rules, so
keep credentials in Lakana's secrets and name them in the file. [Manage secrets](manage-secrets.md)
shows how. Comments in a file are shown too.

## When the file is not shown

The location is always printed; instead of the file, a line on standard error says why, and the
command still succeeds:

| Reason | What it means | What to do |
|---|---|---|
| `restricted` | You do not pass the rules above. Lakana does not say which one, so a file never reveals what it reads. | Open the file in your git host if you have access there, or ask an administrator. |
| `not_recorded` | Lakana has no record of the file for the deployed version: it was deployed before Lakana kept project files, or its code no longer matches what this release records. | Deploy a new commit. |
| `too_large` | The file is larger than 1 MiB. | Open it in your git host. |

## Reading a file is recorded

Each time someone reads a file, the audit log records a `SOURCE_VIEWED` event with the repository,
path and commit: once per person, table and commit in each window (an hour by default; see
[Export and keep the audit log](export-and-keep-the-audit-log.md)). Seeing only the location is
not recorded. When the event cannot be written, the file is not shown: the command fails with a
message to try again.

## Control characters

A deploy refuses an asset file that contains a control character, so a file Lakana records cannot
hold one; the error names the file and the line. Files deployed before that rule are still printed
safely, with each control character escaped. See
[the upgrade note](../explanation/upgrades.md#control-characters-in-asset-files-are-refused-breaking)
for how to find them in a repository.

## Use it from Python

```python
where = client.tables.definition("dev", "silver.orders")
print(where.path, where.line, where.commit, where.browse_url)

file = client.tables.definition_file("dev", "silver.orders")
print(file.content if file.content is not None else file.message)
```

`definition_file` gives the text as it is stored, without escapes: escape it yourself before you
print it to a terminal.
