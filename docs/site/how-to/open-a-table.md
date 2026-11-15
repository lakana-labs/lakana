# Open a table

After this guide you can open a table's page in the web interface, move between its tabs, and
understand what the page says when something is missing. You need to be signed in to the web
interface (see [Start in the web interface](start-in-the-web-interface.md)) and to be allowed to
read at least one table.

## Open the page

Press a table's row in the **Catalog** (see [Find a table](find-a-table.md)), a table's name in
**Recently opened** on Home, or go to its address, `/catalog/{environment}/{namespace}/{table}`.
Opening a table puts it in **Recently opened** on Home.

The page has:

- **A breadcrumb** above the name: Catalog, the environment, the namespace and the table. Each
  part but the last is a link, so one press takes you back to the list narrowed to that place.
- **The table's name** as the heading, with its label under it when the table has one.
- **A line under the name** with what the table is and where it lives, such as `Fact · gold.sales`,
  and the environment's label.
- **Tabs**: Overview, Columns, Preview, Lineage, Health and Access. Every tab has its own
  address, so a link opens the tab you were looking at. Everyone sees the same tabs.
- **Actions** beside the name, once the table is read, when there is something you may do to the
  table (see [Actions](#actions)).

## Actions

**Actions** opens a menu of what you may do to the table as a whole. It lists only what the
server says you may do, so an item you see is one you are allowed to use; with none, the page has
no **Actions** at all. An artifact's page has no **Actions** either: drop an artifact with
`lakana artifact drop`.

- **Maintain now…** queues maintenance for the table, as `lakana table maintain` does. See
  [Maintain a table now](#maintain-a-table-now).
- **Retention…** shows and changes when the table expires, as `lakana table retention` does. See
  [Let tables expire](let-tables-expire.md#see-a-tables-retention).
- **Rebuild from git…** drops a table a deployed project builds and queues the run that builds it
  again. It asks why, for the audit log. See
  [Rebuild a table from git](drop-and-restore-a-table.md#rebuild-a-table-from-git).
- **Drop…** asks first, then drops the table and opens the **Dropped** view of its namespace,
  where you can undo it. See [Drop a table](drop-and-restore-a-table.md#drop-a-table).

When a deployed project builds the table, **Drop…** is shown but cannot be chosen, and the menu
says `Defined in analytics/assets/silver/clean.sql — change it in git`, with the table's own
repository and file. A screen reader hears that sentence when the menu opens. Change the table in
git, or use **Rebuild from git…**.

Drop and Rebuild from git ask for a recent passkey touch: one from the last 15 minutes. When yours
is older, the browser asks you to touch your passkey and the action is sent again. If the touch
fails, the dialog stays open with the reason, nothing is dropped, and **Cancel** closes it. In a
protected environment Drop also asks why you break the protection, which the audit log records.
Rebuild from git asks for no second reason: the one reason it asks for is also sent as the reason
you break the protection, as `--break-glass` does.

### Maintain a table now

Maintenance expires a table's old snapshots, deletes the files nothing references any more and
removes the tags of failed checks (see `lakana table maintain` in the
[CLI reference](../reference/cli.md#lakana-table-maintain)). In the web interface, choose
**Actions**, then **Maintain now…**. The form starts with the server's defaults, the same as the
command's:

- **Keep snapshots younger than (days):** 7, from 0 up.
- **Always keep the latest snapshots:** 5, from 1 up.
- **Delete unreferenced files older than (hours):** 72, from 1 to 87,600.
- **Dry run: report, change nothing:** off.

Press **Maintain now**. The dialog then says "Maintenance is queued as query" with the query's
identifier and gives a command to copy, `lakana query status <id> --env <environment>`. Maintenance
runs in the background; the web interface does not show its progress yet, so follow it with that
command. If the server refuses, for example because you lack the right to maintain the table, the
form stays open with the server's sentence. **Maintain now…** is in the menu only for a managed
table you may maintain.

## Overview

Overview opens first. It is the table's address without a suffix, and it holds what you want to
know about a table at a glance. On a wide screen it has two columns; on a narrow one the blocks
follow each other.

- **Description.** The table's description, and where it comes from: "From the project in Git"
  for the text in the project's files, "Edited here" for a text someone wrote in the web
  interface. A table without one says "No description yet." If you may edit it, **Edit** is
  beside it; see [In the web interface](describe-a-table.md#in-the-web-interface).
- **Columns.** The first five columns with their type, and "Key" or "Measure" when the table's
  header says the column is one. **All columns ›** opens the Columns tab.
- **Health.** The table's verdict in one word, as in the Catalog's Health column, and under it
  what is wrong, worst first and at most five lines, then "and 2 more", which opens the Health
  tab. **Health details ›**, beside the heading, opens it too:
  - A failed check names the check and how many rows broke it, and says what happened to the
    table: "The table was set back to its last good snapshot." or "The table was not
    published." Without a count it reads "Check `unique_sale_id` failed."
  - A check that could not run says so, with the reason: "Check `unique_sale_id` could not
    run: …". A check that warned reads "Check `unique_sale_id` warned: 3 rows broke it."
  - A signal that fired uses the server's own sentence. Without one, a short sentence names
    the kind, such as "The number of rows written is outside its usual band." If a column
    mask or a row filter applies to you, the numbers stay hidden: you get that short sentence
    and "Its values are hidden for you." and never a claim that rows rose or dropped.
  - A table without a problem says "Every check passed and no signal fired." When some checks
    are hidden from you, it says "Every check you may see passed and no signal fired." A table
    with no checks and no signals says "Not judged yet: the table has no checks or signals."
  - When health cannot be read, the block says why and offers **Try again**. The rest of
    Overview stays as it is.
- **Facts.** Owner; Rows; Size with the number of data files, such as "3.1 MB in 12 files";
  Last write; Last read with how many people read the table in the last 7 days, such as "2 hours
  ago · 3 readers in the last 7 days" (never who; an administrator gets **Readers ›**, which opens
  the names on the Health tab); Created; and Grain, the sentence the table's
  header gives for what one row is; and Defined in, the file, line and commit in git that define
  the table, whose path opens the file (see
  [See where a table is defined](see-where-a-table-is-defined.md#in-the-web-interface)). A figure
  that does not exist yet says so in words: "Not known", "Never written", "Never read" or "No
  owner".
- **Comes from · goes to.** The tables directly upstream, which feed this one, and directly
  downstream, which it feeds, as links, at most five each way and then "and 2 more". "Nothing
  upstream." and "Nothing downstream." mean there is none you may see. Jobs and connections are
  not listed here; **Lineage ›**, beside the heading, opens the Lineage tab, which shows them.
  If lineage cannot be read, the block says why and offers **Try again**.

Opening Overview runs no query. It reads the table, its health without the readers' names, and
one step of lineage each way.

## Columns

Columns lists every column of the table. Its address ends in `/columns`. A row shows:

- **Name** and **Type**, such as `long` or `decimal(10,2)`;
- **Nulls**: "Allowed" if the column may hold nulls, "Never" if not;
- **Role**: "Key" for a column that identifies a row, "Measure" for a number the table's header
  declares as one;
- **Joins**: the table and column this column refers to, as a link when that table exists;
- **Description**, or a dash with "No description". If you may edit the table's texts, each row
  has **Edit** for the column's label and description, and **Back to the text from Git** once the
  text was edited here; see [In the web interface](describe-a-table.md#in-the-web-interface);
- **Terms**: the glossary terms that link the column, as plain text.

A table with many columns, or long names, scrolls inside the list: press <kbd>Tab</kbd> to
focus the list and use the arrow keys. The page itself never scrolls sideways.

## Preview

Preview shows the first 100 rows of the table, read as you, and names the columns masked for
you. Its address ends in `/preview`. The preview runs only while the tab is open, and leaving the
tab cancels one that still waits. A person who may read the table but not run queries sees one
line that says so and what to ask for. See
[In the web interface](preview-a-table.md#in-the-web-interface).

## Lineage

Lineage shows where the table's data comes from and where it goes. Its address ends in
`/lineage`. Like Preview, the drawing takes the whole width of the window; the list under it
keeps the width of the other tabs.

- **What is drawn.** The tables upstream to the left, the tables downstream to the right, and
  between them the assets that build them, each box naming the job that runs the asset, such as
  "Job daily_sales" with "asset gold.sales.fact_sales · analytics" under it, or "Run by no
  job". At the edges are the connections an asset loads from or writes to, with their kind,
  such as "PostgreSQL"; a connection the environment does not have says "Connection not set
  up". Columns are not drawn.
- **One step at a time.** The tab opens one step each way: a step crosses one asset. **One step
  up** and **One step down** add a step, up to ten; a button goes when nothing is further that
  way. The steps are kept in the address, such as `/lineage?up=2`, so **Back** returns to fewer
  steps and a link opens the same view.
- **Opening a table.** Press a table's box to open its page, or a table's link in the list
  below. Jobs and connections are not links yet. A box grows to fit its longest line, up to
  about 320 px; a longer line ends in "…". Hold the pointer over the box to read its names
  whole, or read them in the list below.
- **Full screen.** **Full screen** fills the window with the same drawing at its natural size,
  in a region that scrolls (press <kbd>Tab</kbd> to focus it, then the arrow keys). **Close** or <kbd>Esc</kbd>
  returns to the tab.
- **As a list.** Under the drawing, the same tables, assets, jobs and connections are listed in
  words, with each table a link. A large drawing scrolls in its own region: press <kbd>Tab</kbd>
  to focus it and use the arrow keys.

Lineage looks the same to everyone who may read the table, except that what you may not see is
simply absent: a table you may not read, and the assets, jobs and connections of an environment
you may not read, are not drawn, and nothing counts them. If you see only the table itself, the
tab says "Nothing upstream or downstream." A very large lineage shows its first 500 nodes each way
and says "Showing the first 500 nodes each way." If lineage cannot be read, the tab says why and
offers **Try again**.

## Health

Health shows everything the Overview's health block sums up. Its address ends in `/health`.

- **Checks.** Every check of the table with its latest result (Passed, Failed, Warned or "Could
  not run" with the reason), the number of rows it found ("Not counted" when the check does not
  count them) and when it finished. A reference check whose target is a table you may not read is
  not shown; the tab counts them: "2 reference checks are not shown: their target is a table you
  may not read." Every earlier result is a command away: the tab shows
  `lakana check list --env 'prod' --table 'gold.sales.fact_sales'` with **Copy command**. On a
  narrow screen the checks, like the signals, scroll sideways in their own area, which you can
  reach with <kbd>Tab</kbd> and scroll with the arrow keys.
- **Signals.** Every signal that watches the table: what it watches (the whole table, or a
  column), its state, the value it observed, in words such as "54 minutes since the last
  write", "3 rows added, 3 in total", "Nothing loaded" or "0 of 3 values empty", or why there
  is no value, such as "Paused" or "No figures recorded for this snapshot", its usual band in the same
  words (such as "up to 24 hours", "900 to 1,100 rows added" or "up to 5.0 % empty", and a range's
  dates as dates), its effect (Warns, "Blocks publication" or Off) and when it last fired. Words are never broken across lines; when
  the window is too narrow for the list, it scrolls sideways in its own area. Under the list, each signal that fired has its sentence,
  as on Overview. If a column mask or a row filter applies to you, the observed value and the
  band say "Hidden for you".
- **Last maintenance.** When the last maintenance that changed the table finished, whether it
  completed or stopped at a safe point, how many snapshots it expired, how many files it deleted
  and their size, and how many tags of failed checks it removed.
- **Readers.** An administrator sees who read the table in the last 7 days, each name a link to
  the person, with how many reads and when the last one was. Seeing the names is recorded in the
  audit log. If that record cannot be written, the tab still shows everything else, shows the
  count instead of the names and says "Who read the table cannot be shown: seeing it could not be
  recorded in the audit log." Open the tab again in a moment; if it keeps failing, ask an
  administrator to check the server. Everyone else sees only how many people read it, such as "3
  people read it in the last 7 days." **Readers ›** on Overview opens this part directly.

If health cannot be read, the tab says why and offers **Try again**.

## Access

Access shows what you may do with the table and why. Its address ends in `/access`. It only
shows; changing access comes with the Access area.

- **What you may do.** Read, Write, Drop and Edit texts, each "Allowed" or "Not allowed". An
  allowed action says how it reaches you, such as "Through the Analyst role on namespace gold in
  prod." or "Through the group analysts, which has the Viewer role on table gold.sales.fact_sales
  in prod." A refused one says why, or "Ask an administrator." You may edit the texts if you own
  the table or may drop it; otherwise the tab names the owner to ask.
- **Who has access** (administrators only). The grants on the table, on its namespace and each
  namespace above it, and on the environment, each level under its own heading: the person or
  service account (a link to them), or the group, and the role or action with when it was
  granted. A level with more than 200 grants shows the first 200 and names the command that lists
  all, such as `lakana grant list --env 'prod' --ns 'gold.sales' --table 'fact_sales'`. A grant
  whose person was removed while the list was read says "a removed person or group" with its id.
  Grants on the whole instance, such as the Admin role, are not listed here; `lakana grant list`
  shows every grant.
- **Row filters** and **Column masks** (administrators only). Who each one is for, the masked
  column for a mask, and the expression, shown as text.

A part that cannot be read says why and offers **Try again**; the other parts stay. See
[Why may (or may not) someone do this?](why-may-someone-do-this.md#in-the-web-interface) for the
same answer from the command line.

## When the page cannot be shown

The name and the breadcrumb always show, so you know which table the address names. Below them,
the page says why it cannot show the table and what to do next:

- **"There is no table … in … › …. Check the name in the address; if it existed, it may have
  been dropped or renamed."** You may read that place, but it has no table by that name: the
  name in the address is misspelt, or the table was dropped or renamed. Lakana gives the same
  answer for both. Correct the name in the address, or press **Back to the Catalog**.
- **"You may not read …, or there is no table by that name."** Lakana gives the same answer for
  a table you may not read and for a name that does not exist, so that it does not reveal which
  tables exist. Check the name in the address, or ask an administrator for access. **Back to
  the Catalog** leads to the tables you may read.
- **"The table's storage cannot be reached right now."** Press **Try again** in a moment. If it
  stays, an administrator should look at [Instance health](instance-health.md).
- **Any other sentence** comes from the server and names the cause. Press **Try again**.

An address whose last part names no tab, such as `/catalog/prod/gold/orders/nothing`, answers
"This page does not exist".
