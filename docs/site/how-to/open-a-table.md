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

Overview opens first. It is the table's address without a suffix.

## Columns

Columns lists every column of the table. Its address ends in `/columns`.

## Preview

Preview shows the first rows of the table. Its address ends in `/preview`.

## When the page cannot be shown

The name and the breadcrumb always show, so you know which table the address names. Below them,
the page says why it cannot show the table and what to do next:

- **"There is no table … that you may read."** The table does not exist, or you may not read it;
  Lakana gives the same answer for both so that it does not reveal which tables exist. Press
  **Back to the Catalog**.
- **"You may not read …"** Ask the table's owner or an administrator for access.
- **"The table's storage cannot be reached right now."** Press **Try again** in a moment. If it
  stays, an administrator should look at [Instance health](instance-health.md).
- **Any other sentence** comes from the server and names the cause. Press **Try again**.

An address whose last part names no tab, such as `/catalog/prod/gold/orders/nothing`, answers
"This page does not exist".
