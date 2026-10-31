# Project layout

What a deploy reads from a repository: the files under `assets/`, `checks/` and `translations/`, and the names
they give to tables. For the steps that create and deploy a project, see
[Projects and deploys](../how-to/projects.md).

## Assets

`assets/<namespace…>/<name>.sql` (a SQL asset) or `.py` (a Python asset, see [Python assets](python-assets.md))
becomes the table `<namespace…>.<name>`; nested directories are nested namespaces. No path
component may contain a dot, and a table or namespace the deploy creates may not contain a `%`
(one that already exists keeps its name and deploys as before). Each SQL file holds exactly one query (`SELECT`, `WITH … SELECT` or a set operation). A table produced by another
asset of the same repository is a DAG edge; anything else is an external input and must exist in
the environment (readable by whoever deploys) or be produced by another repository's current
deployment.

Refused: symlinks, files over 1 MiB, SQL longer than 100,000 characters (`LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS`), more than 2000 assets, a `.sql` and a `.py` file for the
same table, `__init__.py` under `assets/`, and paths that collide once case-folded or
Unicode-normalised (`Events.sql` next to `events.sql`, `model.sql` next to `Model.py`, or two
spellings of the same accented name) — they would be one table on a case-insensitive
filesystem. A `.py` file under `assets/` is a Python asset (see [Python assets](python-assets.md)). A repository
with a `.py` file under `assets/` is also refused if it holds a symlink anywhere, or a
directory the worker cannot list, because a Python task unpacks the whole repository and
rejects links.

## The `checks/` directory

`checks/<namespace…>/<table>/<name>.sql` is a **check** of one table: a `SELECT` that returns the
rows that are wrong. A deploy reads `checks/` with `assets/`. Every run tests the table after it
writes it, and a check that fails sets the table back and stops what is built from it. A project
without `checks/` runs as it always did. [Check your data](../how-to/check-your-data.md) writes the first
check, shows the output of a failing one and gives five checks to copy; the exact rules are in
[Checks](checks.md).

## The `translations/` directory

`translations/<language>.yaml` holds one language's labels and descriptions of the project's
tables, columns and measures, keyed by table: `translations/fr.yaml`, `translations/de-CH.yaml`.
The folder holds nothing else, at most 50 files of at most 1 MiB each. A project with translation
files declares `main_language:` in `lakana.yaml`. The format and every rule are in
[Translations](translations.md).
