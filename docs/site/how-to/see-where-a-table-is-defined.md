# See where a table is defined

After this guide you can find the file and line in git that define a table, and read that file
in the terminal or in the web interface. You need a project deployed to the environment (see
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
or `\u202e` for a right-to-left override (U+202E); accents, other scripts and emoji are printed as they are.

`--output json` prints the answer for a script: the location's fields, and with `--show` the file
as `content`, with `bytes`, `reason` and `message`. There the text is as JSON writes it, without
the escapes above, so read it with a program rather than on the screen.

## Who may read the file

Everyone who may read the table sees where it is defined. The file itself can show more than the
table does: the other tables it reads, and the logic and values written into it. So Lakana shows it
only when all four of these hold:

- you may read the environment;
- you may read every table the file uses, and none of them is a view (other than the table
  itself), so a file that reads a view is shown to nobody here;
- no row filter or column mask applies to you on any of those tables;
- the file reads only tables: a file that reads a file path directly (such as
  `read_parquet('s3://…')`) or uses a [connection](../reference/connections.md) reads
  something no table grant covers, so it is shown to nobody here, administrators included.

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
path and commit: once per person, file and commit in each window (an hour by default; see
[Export and keep the audit log](export-and-keep-the-audit-log.md)). Seeing only the location is
not recorded. When the event cannot be written, the file is not shown: the command fails with a
message to try again.

## The same rules in a project version

`lakana project-version show` (and `--output json`, which has each asset's and check's `sql`)
follows these rules too, file by file: an asset's or a check's SQL is shown only when you would be
shown its file here, and each file it shows is recorded the same way. A check is stricter in one
point: it is hidden when any table it reads is a view, also the table it checks, since a view's
rules sit on the tables beneath it. For any other asset, the `sql` column says
`hidden (restricted)` or `not recorded`, and the sentence of the table above is printed once
under the table. The text output lists only assets; a check's reason is in `--output json`,
where a hidden entry has no `sql` and carries `content_reason` and `content_message`.

## Control characters

A deploy refuses an asset file that contains a control character, so a file Lakana records cannot
hold one; the error names the file and the line. The CLI escapes each control character when it prints a
file anyway, as an extra safeguard. See
[the upgrade note](../explanation/upgrades.md#control-characters-in-asset-files-are-refused-breaking)
for how to find them in a repository.

## In the web interface

On a table's page, the **Facts** of the Overview tab end with **Defined in**: the file's path, its
line and the first seven characters of the deployed commit, such as
"models/gold/fact_sales.sql, line 1 · a1b2c3d". A table no project defines has no Defined in.

- **The link.** When the repository is on one of the git hosts above, **Open in your git host ↗**
  opens the line at that commit in a new tab. Lakana builds the link on the server; without one,
  the path and the commit are plain text you can select and copy.
- **The file.** Press the path to open the file in a dialog. The file is read only then, so each
  opening counts as a read and is recorded as described below. The dialog names the repository,
  the path and the whole commit, and shows the file with numbered lines and the defining line
  highlighted; the text scrolls in its own area, which you can also reach with Tab and scroll with
  the arrow keys. **Close** or Escape closes it and puts the focus back on the path.
- **When the file is not shown,** the dialog shows the server's sentence for the reason in the
  table below instead of the text. When the read cannot be recorded in the audit log, it shows no
  text and says so; **Try again** reads it again.
- **Characters you could not see.** The page shows the file as plain text: markup in it is shown
  as the characters it is made of, never run. A control character, a zero-width character or a
  character that changes the direction of the text, such as U+202E (right-to-left override),
  would hide or reorder what you read, so the page shows it as a small mark with its code, such as
  `U+202E`, in the file, the path and the repository's name. Tabs and line breaks are kept.

## Use it from Python

```python
where = client.tables.definition("dev", "silver.orders")
print(where.path, where.line, where.commit, where.browse_url)

file = client.tables.definition_file("dev", "silver.orders")
print(file.content if file.content is not None else file.message)
```

`definition_file` gives the text as it is stored, without escapes: escape it yourself before you
print it to a terminal.
