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
- **Tabs**: Overview, Columns and Preview. Every tab has its own address, so a link opens the
  tab you were looking at. Everyone sees the same tabs.

The page has no button beside the name. What you may do is in the tabs.

## Overview

Overview opens first. It is the table's address without a suffix, and it holds what you want to
know about a table at a glance. On a wide screen it has two columns; on a narrow one the blocks
follow each other.

- **Description.** The table's description, and where it comes from: "From the project in Git"
  for the text in the project's files, "Edited here" for a text someone wrote in the web
  interface. A table without one says "No description yet."
- **Columns.** The first five columns with their type, and "Key" or "Measure" when the table's
  header says the column is one. **All columns ›** opens the Columns tab.
- **Health.** The table's verdict in one word, as in the Catalog's Health column, and under it
  what is wrong, worst first and at most five lines, then "and 2 more":
  - A failed check names the check and how many rows broke it, and says what happened to the
    table: "The table was set back to its last good snapshot." or "The table was not
    published." Without a count it reads "Check `unique_sale_id` failed."
  - A check that could not run says so, with the reason: "Check `unique_sale_id` could not
    run: …". A check that warned reads "Check `unique_sale_id` warned: 3 rows broke it."
  - A signal that fired uses the server's own sentence. Without one, a short sentence names
    the kind, such as "The number of rows written is outside its usual band." If a column
    mask or a row filter applies to you, the numbers stay hidden: you get that short sentence
    and "Its values are hidden for you." and never a claim that rows rose or dropped.
  - A table without a problem says "Every check passed and no signal fired." A table with no
    checks and no signals says "Not judged yet: the table has no checks or signals."
  - When health cannot be read, the block says why and offers **Try again**. The rest of
    Overview stays as it is.
- **Facts.** Owner; Rows; Size with the number of data files, such as "3.1 MB in 12 files";
  Last write; Last read with how many people read the table in the last 7 days, such as "2 hours
  ago · 3 readers in the last 7 days" (never who); Created; and Grain, the sentence the table's header gives for what one row
  is. A figure that does not exist yet says so in words: "Not known", "Never written", "Never
  read" or "No owner".
- **Comes from · goes to.** The tables directly upstream, which feed this one, and directly
  downstream, which it feeds, as links, at most five each way and then "and 2 more". "Nothing
  upstream." and "Nothing downstream." mean there is none you may see. Jobs and connections are
  not listed here. If lineage cannot be read, the block says why and offers **Try again**.

Opening Overview runs no query. It reads the table, its health without the readers' names, and
one step of lineage each way.

## Columns

Columns lists every column of the table. Its address ends in `/columns`. A row shows:

- **Name** and **Type**, such as `long` or `decimal(10,2)`;
- **Nulls**: "Allowed" if the column may hold nulls, "Never" if not;
- **Role**: "Key" for a column that identifies a row, "Measure" for a number the table's header
  declares as one;
- **Joins**: the table and column this column refers to, as a link when that table exists;
- **Description**, or a dash with "No description";
- **Terms**: the glossary terms that link the column, as plain text.

A table with many columns, or long names, scrolls inside the list: press <kbd>Tab</kbd> to
focus the list and use the arrow keys. The page itself never scrolls sideways.

## Preview

Preview shows the first rows of the table. Its address ends in `/preview`.

## When the page cannot be shown

The name and the breadcrumb always show, so you know which table the address names. Below them,
the page says why it cannot show the table and what to do next:

- **"… is no longer in … ; it may have been dropped or renamed."** You may read that place, but
  the table is not there now. Press **Back to the Catalog**.
- **"You may not read …, or there is no table by that name."** Lakana gives the same answer for
  a table you may not read and for a name that does not exist, so that it does not reveal which
  tables exist. Check the name in the address, or ask an administrator for access. **Back to
  the Catalog** leads to the tables you may read.
- **"The table's storage cannot be reached right now."** Press **Try again** in a moment. If it
  stays, an administrator should look at [Instance health](instance-health.md).
- **Any other sentence** comes from the server and names the cause. Press **Try again**.

An address whose last part names no tab, such as `/catalog/prod/gold/orders/nothing`, answers
"This page does not exist".
