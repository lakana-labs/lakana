# Start in the web interface

Home is the page every sign-in lands on. After this guide you know what the tour on your first
sign-in shows, what each of Home's boxes tells you, what to do when the instance is not healthy,
how to work through, or put away, the Get started list, how to set your profile picture, and
where Help and the keyboard shortcuts are. You need an account in the web interface ([Invite your team](invite-your-team.md) says how
a colleague gets one).

## The tour on your first sign-in

The first time you sign in with access to the instance, a window says "Welcome to Lakana" and
offers **Start the tour** or **Skip** (someone with no access yet gets it once an administrator
has granted them a role). The tour shows the places in the menu on the left one at a time, in the menu's order:
Home, Catalog, Queries, Jobs, Connections and Settings, then Search and Help. Each step outlines its place and says in a
sentence or two what you find there.

- Nothing moves on by itself: **Next** and **Back** go through the steps, **Skip tour** or
  **Esc** leaves it at any step.
- On a narrow window, where the menu is folded behind the **Menu** button, each step is a window
  in the middle of the page that names the place, such as "In the menu: Catalog".
- The last step names your next step from Get started (for an administrator on a new instance,
  **Connect your first repository**), links to the getting-started tutorial, the how-to guides
  and the documentation overview, and ends with **Finish**.

However you leave it, the tour does not come back by itself. **Help** → **Take the tour again**
starts it whenever you like.

## Help

**Help** sits at the bottom of the menu, above your name. It holds:

- **Take the tour again**.
- **Getting started**, **How-to guides** and **Documentation**: this documentation, in a new tab.
- **Keyboard shortcuts**: the list below.
- **Show "Get started" again**, only while you have hidden the list: brings it back and takes
  you Home.
- The version of Lakana the instance runs, such as "Lakana 0.23.0", to name when you ask for
  help.

The search (**Ctrl K**, or **⌘ K** on a Mac) has **Take the tour again** and **Keyboard
shortcuts** too: type a few letters of either.

## Your profile picture

Without a picture, a circle with your initials stands beside your name: at the bottom of the
menu on the left, and in **Settings → Access → People** for administrators. To set one:

1. Select your name at the bottom of the menu and choose **Profile picture…**.
2. **Choose a picture**: a JPEG, PNG or WebP image of up to 5 MB and 40 megapixels (for example
   7000 by 5000 pixels). A larger picture is refused with the limit; scale it down and choose it
   again.

The picture shows at once. Lakana keeps a square cut from the centre, 256 by 256 pixels, and
removes the file's details, such as where a photo was taken or the camera it came from; only the
pixels are stored. Everyone with access to the instance sees it beside your name.

- Another kind of file (an SVG, a GIF) or a larger one is refused before it is sent, with what
  to choose instead.
- "The server is busy with other picture uploads": choose **Try again** in a moment.
- A picture chosen while the previous one is still being sent is not sent; the window says so.
  Choose it again once the first is saved.
- **Remove picture** in the same window brings your initials back.

An administrator can remove anyone's picture, for example an unsuitable one: **Settings → Access → People** → the
person → **Remove picture**. The person can set a new one whenever they like.

## Keyboard shortcuts

| Keys | What they do |
|---|---|
| **Ctrl K** (**⌘ K** on a Mac) | Open the search: go to a place or run a command. Works anywhere, a text field included. |
| **Esc** | Close a menu, a window or the tour. |
| **Tab**, **Shift Tab** | Move to the next or the previous control. |
| **Enter** | Open the row that has focus. |
| **Alt ←** | Go back to the page before, from any page, such as from a table to the Catalog. Not in a text field, where it moves the cursor, and not while a menu or a window is open. |
| **Ctrl Enter** (**⌘ Enter** on a Mac) | In the editor of **Queries**: run the query. |
| **Esc** in the editor of **Queries** | Close the completion list or a tooltip; with none open, cancel the running query. |
| **Alt Shift N** (**⌥⇧N** on a Mac) | In **Queries**: start a new, empty query, as **New query** does. Works in the editor too, not while a menu or a window is open. |

Home shows everything you may see across all environments; each row carries a small label with
its environment (`dev`, `prod`), or `Instance` for something that belongs to the whole instance,
such as a worker. A person who has no access yet sees only "You have no access yet; ask an
administrator." until an administrator grants them a role.

The **Catalog** appears in the menu once you have any access at all; before that, it is not
there.

## Queries

**Queries** in the menu opens a SQL editor. Choose the environment under **Environment** (only
those where you may run queries are listed, and a protected one says so: "● prod · protected"),
write the SQL and press **Run**, or **Ctrl Enter**. While the query waits or runs, **Run** becomes
**Cancel**, and **Esc** in the editor cancels too ([Stop a query](stop-a-query.md)). The result is a
grid under the editor, with **Download CSV** and **Download spreadsheet**
([Download a query result](download-a-query-result.md#in-the-web-interface)). A query that fails
says why; when the server knows where in the SQL, the editor underlines the place, marks its line,
and **Go to line …, column …** puts the cursor there. The names of namespaces, tables and columns
complete as you type, pointing at a column shows its type and description, and **Format** lays the
SQL out. A column completes after its table and a dot (`t.avg`) or on its own (`WHERE avg`) from
the tables the query reads; a name that several of them have is offered once for each, named by
its table, and goes in with it (`t.station_id`). Leaving the screen lets the query run on; **Recent**, beside the editor, opens it again,
and **Saved** holds the queries you keep ([Save and share a query](save-and-share-a-query.md#in-the-web-interface)).
**New query** in the editor bar, or **Alt Shift N**, clears the editor and the result for a new
query. When the editor holds changes that are not saved, it asks first: **Discard changes** clears
them, **Cancel** keeps them. A query that still runs goes on, as when you leave the screen, and
**Recent** opens it again.
Someone who may not run queries anywhere yet sees why, and the command to use meanwhile.

## Jobs

**Jobs** in the menu shows first what runs now and what failed, then every job. **Running now**
lists the queued and running runs with how far each has come, and **Cancel**; **Failed, needs
attention** lists the same failed runs as Home, each with why it failed, **Resume** and **Logs**;
**All jobs** lists each job with its schedule, its last and next run (**Waiting** while a start
of it waits for another run of its project; point at it to see which), **Run** and **Pause** or
**Resume**. Each row names its environment; **Environment** shows one of them instead of all and
is remembered, and **Search jobs** narrows the three lists. A row opens its run or its job
([Find a run](find-a-run.md#find-what-failed-in-every-environment),
[Run a job on a schedule](run-a-job-on-a-schedule.md#see-the-jobs)). A control is there only for
someone who may use it.

## The boxes, top to bottom

The order is the same for everyone; what you may do changes what is inside the boxes.

1. **The update note**, once per release: "Lakana was updated to 0.23.0: what's new ›" links to
   that release's notes. See [The update note](#the-update-note).
2. **Get started**, while it has an open step and you have not hidden it. See
   [Get started](#get-started).
3. **Status**: whether the instance works. See [Healthy, Degraded, Down](#healthy-degraded-down).
4. **Needs your attention**: what failed, is late or waits for you, newest first, five at most.
   Everyone sees the jobs whose last run failed and the repositories whose last deploy failed, in
   the environments they may read. An administrator also sees late tables, failed checks, open
   recovery links and workers that are down. Everyone else also sees failed checks and late
   tables among the tables they read or opened recently, and their own failed queries. A job, a
   deploy or a check that failed several times in a row is one row, such as "nightly_ranking:
   Failed 3 times since Tuesday", and opens the newest failure. With more than five rows,
   **Show all N** shows the rest in the panel and **Show fewer** hides them again. Home keeps the
   newest 50; when there are more, a line under them says so and links to every failed run. With
   nothing to report it says "Nothing needs your attention."
5. **Recently opened**: the tables you opened, kept on the server so the list follows you to
   another browser. A table's row opens its page, a query's its run on the Queries screen, and a
   job's or a run's its page under Jobs.
6. **Your work**, only if you may deploy: your jobs with their last run, and the repositories
   you connected or deployed with the last deploy in each environment.

A row opens its object when you click anywhere on it. A table opens its page, and a query its run
on the Queries screen; a table whose environment or namespace is not known shows its name without
a link. A job opens its page under Jobs, a failed run or check the run's page, and a failed deploy
its repository on the Deploys tab, where its deploys are listed newest first, each failed one with
why it failed.

Home reads the status every 30 seconds and Needs your attention every minute, and only while
its browser tab is visible. These reads do not count as activity, so a Home left open does not
keep your session alive.

## Healthy, Degraded, Down

The status box shows a shape, a colour and a word, and one sentence that says what it means for
you.

- **Healthy**: everything works.
- **Degraded**: work gets done, but slower or at risk. Someone who is not an administrator sees
  the cause only when it affects them, for example "Queries are slower: 1 of 2 workers is down."
- **Down**: nothing gets done. Tell an administrator; they see why.

Whoever may manage environments on the instance also sees every cause, how many task slots of
the workers are busy and how much of their memory limit they use, as two bars with the numbers
beside them, and the workers by state. **Details ›** leads everyone to Settings → Status: the
state with every sentence, how many workers are up and lost, and the version the server runs;
whoever may manage environments also sees every worker with what it does and when it was last
seen, and the scheduler. The same answer is on the command line:

<!-- source: docs/examples/getting-started/run.sh#status -->
```bash
lakana status
```

What each cause means and what to do about it is in [Is my instance
healthy?](instance-health.md#what-the-status-says).

## Get started

Get started suggests what to do next, as far as you may do it. Each step is one sentence and
one link that goes straight there, and is ticked off ("Done") as soon as the instance shows it
done, in any environment:

- **An administrator**: connect a repository, invite your team, add a passkey (when you have
  none), deploy your first project (the tutorial, and the command to copy), and let CI deploy for
  you ([Deploy from GitHub Actions](deploy-from-github-actions.md)).
- **Someone who may deploy**: deploy your first project, open a table in the Catalog, run your
  first query.
- **Someone who may only read**: open a table in the Catalog, run your first query.

On a server in local mode (one person on one machine), an administrator's list is add a passkey,
deploy your first project from a folder on this machine, open a table and run a query; nothing
that needs a team.

The list disappears when every step is done. **Hide** puts it away before then, on every
browser you use; **Help** → **Show "Get started" again** brings it back.

## The update note

After the instance is upgraded to a new release, Home shows one line: "Lakana was updated to
0.23.0: what's new ›". The link opens the release notes of that version on GitHub, in a new tab;
opening it is your browser's own visit, not Lakana sending anything. **×** hides the note for
that version; the next release shows it again.

You see the note from the second release you use on: someone new to Lakana starts on the
current release, so there is nothing new to them (finishing or skipping the tour records that
release as seen). A server built from a development checkout
(its version is not a release, such as `0.23.1.dev4`) shows no note.
