// The size limits of the built interface (docs/agent/testing.md, "The ui/ gates"), gzipped
// except the fonts. Each limit is a measurement + 10 %; a raised limit needs a sentence in its
// pull request. "First load" is what every first visit loads: the entry chunk and the chunks
// index.html preloads with it (the bundler moves code the entry shares with the screens loaded
// on demand into such a chunk). Each screen loaded on demand (src/shell/routes.tsx, AreaPages),
// and each chunk only those screens share, has its own check, listed from the build.
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ASSETS = "../packages/lakana-server/src/lakana_server/ui_dist/assets";
const HERE = dirname(fileURLToPath(import.meta.url));
// Measured 2.96 kB (the former Access screen, now Settings' People, 2026-11-13: its chunk was
// 1 B over the earlier 2.96 kB limit once the Catalog's shared chunks changed the minifier's
// names) + 10 %.
const ON_DEMAND_LIMIT = "3.26 kB";
// Screens larger than ON_DEMAND_LIMIT, and the tour (held to its own, tighter one), each with a
// limit of its own; a chunk's file name is the module's name, a dash and a hash, so the start of
// the name finds it in every build.
const NAMED_LIMITS = [
  // Measured 3.91 kB (Home: the status box, Needs your attention, Recently opened, Your work,
  // Get started and the update note, after review, 2026-11-11) + 10 %.
  { name: "Home", prefix: "HomeScreen-", limit: "4.3 kB" },
  // Measured 1.79 kB (the tour: the welcome and its steps, 2026-11-11) + 10 %.
  { name: "the tour", prefix: "Tour-", limit: "1.97 kB" },
  // Measured 4.69 kB (Connect a repository: the URL and name, the host keys one by one, the
  // repository with its deploy keys per environment and Test access, with focus and
  // announcements between the steps, browser addresses and each answer to a name already
  // registered, after review, 2026-11-11) + 10 %. It loads only on /settings/repositories/new,
  // so Settings' other places stay within the shared limit.
  { name: "Connect a repository", prefix: "ConnectRepository-", limit: "5.16 kB" },
  // Measured 29.08 kB (the Catalog: the list with its columns, health word, owner, header sorting,
  // the Sort by and Order menus, paging and states, 2026-11-13, 13.44 kB; and the tree of places
  // with React Aria's NavigationTree, the search field and the match line, +15.64 kB, almost all
  // of it React Aria's Tree) + 10 %.
  // Raised from 32 kB: measured 29.64 kB with the route of a table's page (the heading and the
  // breadcrumb from the address, so the focus the shell puts on the heading survives the page's
  // own chunk and its read), 2026-11-13, + 10 %.
  // Measured 30.3 kB (2026-11-16) with the view switch, the lazy import of the views and the
  // list's skeleton as a component; the limit holds.
  // Measured 30.46 kB (2026-11-16) with the table page's lazy Actions menu in the heading row; the
  // limit holds.
  // Measured 31.05 kB (2026-11-16) with the lazy loaders of the place menu and its note, the
  // move to a namespace once the tree read after its creation arrives, and the quoted
  // environment of the deploy command; the limit holds.
  // Lowered to the measured 16.36 kB (2026-11-17) + 10 %: React Aria's Select, ListBox and Popover
  // (the Sort by and Order menus) moved into a chunk the Catalog shares with the Queries screen's
  // environment picker (format-, below).
  { name: "Catalog", prefix: "CatalogScreen-", limit: "18 kB" },
  // Measured 4.97 kB (2026-11-17) + 10 %: the Connections list (its table with the kind, where,
  // environment, last test with its time and used by, the remembered environment filter, Test
  // per row, the empty and no-environment states and the clock that ages "2 hours ago") and the
  // screen's strings. The kinds' words and fields come with the form's chunk.
  // Raised to the measured 6.92 kB (2026-11-17) + 10 %: Test per row with its 409 read, Test all
  // in an environment with its always-mounted status line, and the follower of open tests (fast,
  // then slow, three failures and Try again, ten minutes and stuck, one read per connection or
  // the environment's list in pages of 200).
  // Raised to the measured 7.22 kB (2026-11-17) + 10 %: Test all's line says when following
  // stopped, and the follower drops deleted connections.
  // Raised to the measured 9.58 kB (2026-11-17) + 10 %: a connection's page (its facts, the last
  // test with its cause and next step, the declaring assets) and Delete with its dialog.
  // Measured 10.51 kB (2026-11-17) with the route to Add, loaded on demand, and its loading
  // words; the limit holds.
  // Raised to the measured 10.93 kB (2026-11-17) + 10 %: the route to Edit, which reads the
  // connection (its skeleton, Try again and not-found state in the Edit page) before the form
  // loads on demand.
  { name: "Connections", prefix: "ConnectionsScreen-", limit: "12.02 kB" },
  // Measured 4.71 kB (2026-11-17) + 10 %: Add a connection, loaded on demand by its route: the
  // environment and the nine kinds, the name checked on blur, each kind's fields (http's after
  // its auth, the CA certificate read from a file), the secret as password fields or an existing
  // secret's name (azure's choice, gcs's key file, tcp's own keys), Save with the passkey touch,
  // and the form's strings.
  // Measured 4.83 kB (2026-11-17) with numbered tcp key rows, the focus on a new row and one
  // azure key and at most 16 tcp keys in the body; the limit holds.
  // Raised to the measured 6.4 kB (2026-11-17) + 10 %: Edit of a connection: its fields from
  // the stored ones, the body of only what changed, the CA certificate's fingerprints with
  // Remove, and the secret kept, given again, another or none, asked again when a change could
  // send it somewhere new.
  // Raised to the measured 6.56 kB (2026-11-17) + 10 %: Edit says who may edit where the
  // person may not, refuses a moving change without new values for everyone, keeps a pending
  // Remove of the CA certificate past a refused file, and starts an s3 default endpoint empty.
  { name: "Add or edit a connection", prefix: "ConnectionForm-", limit: "7.22 kB" },
  // Measured 541 B (2026-11-17) + 10 %: the Queries screen, for now its heading and the lazy
  // import of the SQL editor with the screen's strings.
  // Raised to the measured 4.83 kB (2026-11-17) + 10 %: the environment picker with its protected
  // mark, Run that becomes Cancel, the run's polling (fast, then slow, three failures and Try
  // again) without a cancel on leaving, the run's address, the error with Go to its line and
  // column, the result's line with Show 100 more up to 1,000 rows, the editor's placeholder and
  // the screen's strings. The grid and the menus come in the shared chunks below.
  // Measured 4.95 kB (2026-11-17) with the focus on Run after Try again and the alert of a
  // refused cancel; the limit holds.
  // Raised to the measured 5.86 kB (2026-11-17) + 10 % with the downloads (two buttons, the
  // command for a result too large for the browser, the file name and their sentences).
  // Measured 6.17 kB (2026-11-17) with Format and its line for SQL it cannot read; the limit
  // holds.
  // Raised to the measured 9.13 kB (2026-11-17) + 10 %: Saved and Recent beside the editor (the
  // filter by environment, the rows that open a saved query or a run, their paging and reading
  // again after a save or a delete), the Queries ▾ popover of a narrow screen, Save… with its
  // name, visibility, Save and Save as new, Delete… with its question and the note after it, the
  // saved query's address with the administrator's view of one whose SQL may not be read, and
  // their strings.
  // Measured 9.85 kB (2026-11-17) with the editor's catalog cache (queries/editor/catalog.ts),
  // which the screen now hands to the editor, so that the editor's chunk reaches no API module
  // (see the first load below); the limit holds.
  { name: "Queries", prefix: "QueriesScreen-", limit: "10.05 kB" },
  // Measured 1.1 kB (2026-11-17) + 10 %: the Jobs area's screen, for now its routes (a job, a run and
  // a task, each a page of its own below), the area's page at /jobs and the screen's strings. The
  // lists, facts and logs come in the next changes.
  // Raised to the measured 2.68 kB (2026-11-17) + 10 %: the strings of the job and run pages
  // (facts, runs, a run's facts and tasks, the controls and their notes) and the skeleton that
  // says "Loading the job…" or "Loading the run…" while their chunks load.
  // Raised to the measured 3.16 kB (2026-11-17) + 10 %: the strings of the task page (its facts,
  // the attempts' columns and outcomes, the log's status lines, notes and Save log) and the
  // skeleton that says "Loading the task…" while its chunk loads.
  { name: "Jobs", prefix: "JobsScreen-", limit: "3.48 kB" },
  // Measured 278 B (2026-11-17) + 10 %: the job's page, for now its heading and breadcrumb.
  // Raised to the measured 2.65 kB (2026-11-17) + 10 %: the job read every 30 s, Run now and
  // Pause or Resume schedule with their notes, the facts (where it is defined, the schedule in
  // words, the selection, after each deploy, where it notifies, why a start waits), the runs
  // with Status and Period in the address, paged and read again every 30 s, and the selected run.
  // Raised to the measured 2.95 kB (2026-11-18) + 10 %: without `?run=` the run shown is kept
  // while the list holds it, so a newer run does not replace the run being read.
  { name: "the job page", prefix: "JobPage-", limit: "3.25 kB" },
  // Measured 288 B (2026-11-17) + 10 %: a run's page, for now its heading and breadcrumb.
  // Raised to the measured 403 B (2026-11-17) + 10 %: the record of the run as opened; the run
  // itself is in the chunk it shares with the job page (RunDetail-, below).
  { name: "the run page", prefix: "RunPage-", limit: "0.44 kB" },
  // Measured 4.7 kB (2026-11-17) + 10 %: a run, shared by the job page and the run page: read
  // every 2 s while active, its facts, its tasks, Cancel with its question, Resume, Logs, and the
  // failed read with Try again.
  // Raised to the measured 5.34 kB (2026-11-17) + 10 % with a failed task's second line: the failure
  // with its code, cause and next step, Log, Open table and Show rows of each failed check.
  // Lowered to the measured 2.14 kB (2026-11-17) + 10 %: the run's reads, its title and
  // breadcrumb, a task's row with its failure line and controls moved into a chunk the run shares
  // with the task page (useRun-, below).
  { name: "a run", prefix: "RunDetail-", limit: "2.35 kB" },
  // Measured 4.11 kB (2026-11-17) + 10 %: what a run and a task's page share: the run read every
  // 2 s while active, its title and breadcrumb, a task's row, its failure line with the code,
  // cause and next step, and the controls beside it (Log, Show source, Open table, Show rows),
  // the failed read with Try again and the reading skeleton.
  { name: "a run and a task", prefix: "useRun-", limit: "4.52 kB" },
  // Measured 324 B (2026-11-17) + 10 %: a task's page, for now its heading and breadcrumb.
  // Raised to the measured 2.7 kB (2026-11-17) + 10 %: the task's facts, its attempts (a table of
  // ten columns with the outcomes in words) and its log, followed every 2 s from the byte offset,
  // caught up at most 16 pieces in a row, kept to 2,000,000 characters cut at a line, drawn in
  // memoised parts of 200 lines, with the command that prints it whole and Save log.
  { name: "the task page", prefix: "TaskPage-", limit: "2.97 kB" },
  // Measured 21.38 kB (2026-11-17) + 10 %: the result grid (React Aria's Table, the cells as
  // text, the Value panel, the masked and cut lines and the grid's strings), which the Preview
  // tab and the Queries screen share; loaded with the first of them.
  { name: "the result grid", prefix: "ResultGrid-", limit: "23.52 kB" },
  // Measured 16.54 kB (2026-11-17) + 10 %: React Aria's Select, ListBox and Popover with the
  // catalog's numbers and sizes (catalog/format.ts, which names the chunk), shared by the
  // Catalog's menus, the Queries screen's environment picker and result line, and the table
  // page's tabs; never in the first load.
  // Lowered to the measured 443 B (2026-11-17) + 10 %: once the Jobs screen's menus use React
  // Aria's Select too, the bundler gives it a chunk of its own (Select-, below) and the code the
  // Select shares with the Catalog's tree another (TreeDropTargetDelegate-, below); this chunk
  // keeps the catalog's numbers and sizes.
  { name: "the number formats", prefix: "format-", limit: "0.49 kB" },
  // Measured 9.85 kB (2026-11-17) + 10 %: React Aria's Select, ListBox and Popover, shared by the
  // Catalog's Sort by and Order, the environment picker of the Queries and Connections screens
  // and the Status and Period of a job's runs; never in the first load. Measured 9.82 kB
  // (2026-11-18) with the Connections screen among its importers; the limit holds.
  { name: "the menus", prefix: "Select-", limit: "10.84 kB" },
  // Measured 7.03 kB (2026-11-17) + 10 %: the React Aria collection code the menus share with
  // the Catalog's tree (the chunk is named after one of its modules); never in the first load.
  {
    name: "the menus' and the tree's collections",
    prefix: "TreeDropTargetDelegate-",
    limit: "7.73 kB",
  },
  // Measured 242 B (2026-11-17) + 10 %: the status glyph with its word, which the Queries screen
  // shares with the screens that show a state.
  { name: "the status label", prefix: "StatusLabel-", limit: "0.27 kB" },
  // Measured 125.08 kB (2026-11-17) + 10 %: the SQL editor, almost all of it CodeMirror 6 (state,
  // view, commands, language, lang-sql's grammar and keywords, autocomplete, lint) with the
  // Still Water theme, the run keys, the error mark and the sources' registry. It loads only when
  // the Queries screen opens, never in the first load.
  // Measured 127.11 kB (2026-11-17) with completion and hover from the catalog (its cache, the
  // names after FROM, a namespace or an alias, the column under the pointer) and the loader of
  // Format; the limit holds.
  // Measured 126.33 kB (2026-11-17) once the catalog cache moved to the screen's chunk; the limit
  // holds.
  { name: "the SQL editor", prefix: "SqlEditor-", limit: "137.6 kB" },
  // Measured 18.36 kB (2026-11-17) + 10 %: Format, sql-formatter with its DuckDB dialect only,
  // loaded on the first press of Format, never with the editor or in the first load.
  { name: "Format", prefix: "formatSql-", limit: "20.2 kB" },
  // Measured 398 B (2026-11-17) + 10 %: the catalog's list and tree calls (api/catalog.ts), which
  // the Catalog shares with the SQL editor's completion; never in the first load.
  { name: "the catalog's calls", prefix: "catalog-", limit: "0.44 kB" },
  // Measured 3.6 kB (2026-11-16) + 10 %: the Catalog's views other than All, loaded when one is
  // chosen: the choice of an environment, the Dropped list with its paging, Undrop and its
  // dialog with the reason of a protected environment, the hook that reads the protection and
  // the strings of the views and of the actions. It is never in the first load.
  // Lowered to the measured 2.94 kB (2026-11-16) + 10 %: the API calls of catalog management,
  // the protection hook, the reason field and the actions' strings moved into a chunk the views
  // share with the table's Actions (below).
  // Raised to the measured 3.41 kB (2026-11-16) + 10 %: the list frame, the Expiring list (when, due now and the
  // retention sentence) and the Orphans list with its Drop dialog and its sentences.
  // Lowered to the measured 2.98 kB (2026-11-16) + 10 %: the list frame and the paging by cursor
  // moved into a chunk of their own (below) once an artifact's open uploads became their second
  // importer.
  { name: "the Catalog's views", prefix: "ViewBody-", limit: "3.28 kB" },
  // Measured 1.1 kB (2026-11-16) + 10 %: the list frame (skeleton, the server's sentence with Try
  // again, the empty sentence, the scrolling table and Show more) and the paging by cursor, which
  // the Catalog's views share with an artifact's open uploads.
  // Measured 1.16 kB (2026-11-17) with rows that open their object and the table's width as the
  // caller's, for the Queries screen's Saved and Recent; the limit holds.
  { name: "the list frame", prefix: "ViewList-", limit: "1.21 kB" },
  // Measured 2.29 kB (2026-11-16) + 10 %: the Drop dialog with the break-glass reason field and
  // the strings of the actions, which the table's Actions and the Catalog's views share.
  // Lowered to the measured 0.81 kB (2026-11-16) + 10 %: the reason field, the confirm row, the
  // protection hook and the actions' strings moved into a chunk of their own (below) once the
  // place menu became their third importer.
  { name: "the Drop dialog", prefix: "DropDialog-", limit: "0.89 kB" },
  // Measured 1.91 kB (2026-11-16) + 10 %: the break-glass reason field, the confirm row, the hook
  // that reads the protection and the actions' strings, shared by the table's Actions, the
  // Catalog's views and the place menu.
  // Raised to the measured 2.1 kB (2026-11-17: 2,101 B, 1 B over once the SQL editor's catalog
  // calls changed the minifier's names; the code is unchanged) + 10 %.
  // Lowered to the measured 1.78 kB (2026-11-17) + 10 %: the confirm row moved into a chunk of
  // its own (below) once the Queries screen's Save… and Delete… became its importers without the
  // reason field.
  { name: "the reason and confirm row", prefix: "ProtectedReason-", limit: "1.96 kB" },
  // Measured 588 B (2026-11-17) + 10 %: the confirm row (the confirm button, Cancel, a second
  // action such as Save as new, and the status under them), shared by the Catalog's dialogs and
  // the Queries screen's Save… and Delete….
  { name: "the confirm row", prefix: "ConfirmRow-", limit: "0.65 kB" },
  // Measured 261 B (2026-11-17) + 10 %: the sentence of a failed read with Try again, shared by
  // the table page's tabs, the Catalog's views, an artifact's versions and the Queries screen's
  // lists (it left catalog/cells.tsx so the Queries screen does not load the Catalog's cells).
  { name: "a failed read", prefix: "ReadFailed-", limit: "0.29 kB" },
  // Measured 293 B (2026-11-17) + 10 %: whether the window is narrow enough to fold the sidebar,
  // shared by the tour and the Queries screen's library (left beside the editor, or behind
  // Queries ▾).
  { name: "the narrow window", prefix: "useFolded-", limit: "0.33 kB" },
  // Measured 1.94 kB (2026-11-16) + 10 %: the Catalog's place menu with New namespace and Delete
  // namespace, their dialogs and the strings of the places, loaded once the tree is known and an
  // environment or a namespace is chosen.
  // Raised to the measured 2.36 kB (2026-11-16) + 10 %: the loader of New external table's form
  // with the list of chunks it preloads, and the call that registers the table, which the form
  // imports from here.
  { name: "the place menu", prefix: "PlaceActions-", limit: "2.6 kB" },
  // Measured 1.23 kB (2026-11-16) + 10 %: New external table's form (name, location, format and
  // the format read from the location's ending), loaded when it is chosen.
  { name: "New external table", prefix: "ExternalTableDialog-", limit: "1.35 kB" },
  // Measured 653 B (2026-11-16) + 10 %: the strings of the places, shared by the place menu, New
  // external table's form and the place note.
  // Raised to the measured 725 B (2026-11-16) + 10 % with the note after an artifact's drop.
  { name: "the strings of the places", prefix: "placeStrings-", limit: "0.8 kB" },
  // Measured 2.67 kB (2026-11-16) + 10 %: React Aria's radio group, which the Retention dialog and
  // New external table's form share; loaded with the first of them.
  { name: "the radio group", prefix: "RadioGroup-", limit: "2.94 kB" },
  // Measured 393 B (2026-11-16) + 10 %: the note after a namespace was created or deleted,
  // loaded only when the address carries it.
  // Raised to the measured 499 B (2026-11-16) + 10 % with the note after an artifact's drop and
  // the focus it takes back from the heading the shell focuses after a move to another page.
  { name: "the place note", prefix: "PlaceNote-", limit: "0.55 kB" },
  // Measured 0.94 kB (2026-11-16) + 10 %: the API calls of catalog management, shared by the
  // table's Actions and the Catalog's views.
  // Raised to the measured 1.62 kB (2026-11-16) + 10 %: the table page reads an artifact with
  // artifacts.ts, which imports manage.ts, so the bundler now puts the table page's own calls
  // (api/table.ts, about 0.6 kB that left TablePage-) in this chunk, shared by the table page, its
  // Actions and the Catalog's views.
  { name: "the calls of catalog management", prefix: "manage-", limit: "1.78 kB" },
  // Measured 1.17 kB (2026-11-16) + 10 %: the retention sentences and calls, shared by the
  // Retention dialog and the Catalog's views.
  { name: "retention", prefix: "retention-", limit: "1.29 kB" },
  // Measured 1.96 kB (2026-11-16) + 10 %: a table's Actions menu with Drop and Rebuild from git,
  // their dialogs and the git-owned note, loaded once the table page has read the table.
  // Raised to the measured 2.44 kB (2026-11-16) + 10 %: the loaders of the two dialogs below.
  { name: "a table's Actions", prefix: "TableActions-", limit: "2.69 kB" },
  // Measured 1.17 kB (2026-11-16) + 10 %: the Maintain now form, loaded when it is chosen.
  { name: "Maintain now", prefix: "MaintainDialog-", limit: "1.29 kB" },
  // Measured 3.99 kB (2026-11-16) + 10 %: the Retention dialog (its three rules, the sentence
  // that states the retention and the date field), loaded when it is chosen.
  // Lowered to the measured 1.36 kB (2026-11-16) + 10 %: React Aria's radio group moved into a
  // chunk of its own (above) once New external table's form became its second importer.
  { name: "Retention", prefix: "RetentionDialog-", limit: "1.5 kB" },
  // Measured 9.06 kB (2026-11-16) + 10 %: React Aria's number field with its parsing and
  // formatting, which only the Maintain now and Retention forms use; loaded with the first of
  // them.
  { name: "the number field", prefix: "NumberField-", limit: "9.97 kB" },
  // Measured 1.49 kB (a table's page: the read, the record of it as opened, the header with its
  // tabs and the states, 2026-11-13) + 10 %. It loads on /catalog/{environment}/{namespace}/{table}.
  // Raised to the measured 4.07 kB (2026-11-13) + 10 % with the Overview (description, first
  // columns, health in sentences, facts, comes from and goes to) and the Columns tab.
  // Raised to the measured 5.92 kB (2026-11-13) + 10 % with the editor of labels and descriptions
  // (the fields with their counts and refusals, the reset to the text from Git, the language of
  // an edit, and the rows that open under a column).
  // Raised to the measured 7.11 kB (2026-11-13) + 10 % with Defined in: the location, the server's
  // link, the file's dialog with its numbered lines, the marked defining line and the visible marks
  // of invisible characters. React Aria's dialog itself stays in the first load.
  // Raised to the measured 7.78 kB (2026-11-13) + 10 % with the Preview tab loaded on demand from
  // the page: its import, the loading line and the code the bundler moved here with it.
  // Raised to the measured 9.97 kB (2026-11-13) + 10 % with the page's strings, which moved out of
  // the first load (about 1.8 kB gzipped less there) into this chunk, registered when it loads.
  // Measured 9.4 kB (2026-11-16) with the artifact's read, its facts and the tabs by kind, after
  // the table's calls moved to manage- (above); the limit holds.
  // Measured 9.76 kB (2026-11-16) with the lazy import of an artifact's Versions tab; the limit
  // holds.
  // Measured 9.41 kB (2026-11-17) + 10 %: Defined in's file dialog moved out to SourceButton- (below),
  // which the table page and a failed task's Show source share; the limit falls with the chunk.
  { name: "a table's page", prefix: "TablePage-", limit: "10.35 kB" },
  // Measured 5.59 kB (2026-11-17) + 10 %: Show source, the file's dialog with its numbered lines and
  // the visible marks, which the table page's Defined in and a failed task's controls share; it
  // loads with whichever of the two is opened first.
  { name: "Show source", prefix: "SourceButton-", limit: "6.15 kB" },
  // Measured 21.97 kB (2026-11-13) + 10 %: the Preview tab, almost all of it React Aria's Table
  // (the grid with one tab stop and the arrow keys inside), with the run's polling and cancelling,
  // the cells as text and the Value panel. It loads only when the Preview tab is opened.
  // Measured 22.73 kB (2026-11-17) with the grid in shell/ResultGrid.tsx, which the Queries screen
  // shares and which the bundler leaves in this chunk (22.55 kB before, with grid.en.json now in
  // it); the limit holds.
  // Lowered to the measured 1.79 kB (2026-11-17) + 10 %: the grid, its masked and cut lines and
  // its strings moved into the chunk the Preview tab shares with the Queries screen (ResultGrid-,
  // above).
  { name: "the Preview tab", prefix: "PreviewTab-", limit: "1.97 kB" },
  // Measured 4.29 kB (2026-11-13) + 10 %: the Lineage tab, its layered layout of tables, assets
  // and connections with the merging of the two answers, the drawing with its links and the
  // boxes' titles, the list beside it, the steps kept in the address, the full-screen view and
  // its own strings. It loads only when the Lineage tab is opened; the table's page stays at
  // 10.24 kB with the tab's lazy import, inside its limit.
  // Raised to the measured 5 kB (2026-11-14) + 10 %: boxes that grow to their text, measured in
  // the drawing's font with a canvas, lines cut with an ellipsis, and the full screen that fills
  // the window.
  { name: "the Lineage tab", prefix: "LineageTab-", limit: "5.5 kB" },
  // Measured 2.4 kB (2026-11-13, with the read again without names when the audit fails) + 10 %:
  // the Health tab, its checks, signals, last maintenance
  // and readers (the names for an administrator, the count for everyone else) and the focus on
  // #readers. It loads only when the Health tab is opened.
  { name: "the Health tab", prefix: "HealthTab-", limit: "2.64 kB" },
  // Measured 2.24 kB (2026-11-13) + 10 %: the Access tab, what the person may do and why, and for
  // an administrator the grants level by level, the row filters and the column masks. It loads
  // only when the Access tab is opened.
  { name: "the Access tab", prefix: "AccessTab-", limit: "2.46 kB" },
  // Measured 1.01 kB (2026-11-16, `npm run size`) + 10 %: the References tab, its two flat panels of references,
  // the hidden counts and the focus after Try again. It loads only when the tab is opened.
  { name: "the References tab", prefix: "ReferencesTab-", limit: "1.11 kB" },
  // Measured 3.04 kB (2026-11-16) + 10 %: an artifact's Versions tab, its versions 100 at a time
  // with current, who stored each and when, the deleted ones, Files one version at a time, the
  // whole version's command, Download with the size and SHA-256 check, the save under the file's
  // own name, and its own strings. It loads only when the tab is opened.
  // Raised to the measured 4.39 kB (2026-11-16) + 10 %: Delete… on a version, its dialog with the
  // size and file count, the passkey and protected-environment steps (their code stays in
  // ProtectedReason-), and what the delete freed and kept, in the command line's words.
  // Raised to the measured 5.12 kB (2026-11-16) + 10 %: your open uploads below the versions,
  // their reading that shows nothing without one or on a 403, Withdraw at once with the line that
  // takes the focus, the server's sentence and the read again after a 409.
  { name: "an artifact's Versions tab", prefix: "VersionsTab-", limit: "5.63 kB" },
  // Measured 3.83 kB (2026-11-13) + 10 %: the table page's strings, Health's and Access's
  // included. The page and the two tabs share them, so the bundler gave them a chunk of their own,
  // loaded with the page and never in the first load; the table's page fell to 8.46 kB with it.
  // Raised to the measured 4.23 kB (2026-11-14) + 10 %: the words for what a signal observed,
  // its usual band, its effect and the reasons the server records instead of values.
  // Measured 4.41 kB (2026-11-17) after the grid's strings left for shell/grid.en.json (4.55 kB
  // before); the limit, from 4.23 kB + 10 %, holds.
  { name: "the table page's strings", prefix: "tableStrings-", limit: "4.65 kB" },
  // Measured 662 B (2026-11-13) + 10 %: the status glyph, which the table page, the Health tab
  // and the Access tab share.
  { name: "the status glyph", prefix: "StatusGlyph-", limit: "0.73 kB" },
  // Measured 870 B (the catalog's addresses: the list, a table and its tabs; Home's links import
  // them, so the chunk is shared by Home and the Catalog, 2026-11-13) + 10 %.
  // Raised to the measured 994 B (2026-11-16) + 10 % with an artifact's tabs and the tabs by kind.
  // The Queries screen's addresses (queries/paths.ts, 102 B on 2026-11-17, shared by Home's links
  // and the Queries screen) make a chunk of the same prefix, held to the same limit.
  { name: "the catalog's addresses", prefix: "paths-", limit: "1.09 kB" },
  // Measured 95 B (a person's address, which Settings' People and the Health and Access tabs
  // share, 2026-11-13) + 10 %.
  { name: "a person's address", prefix: "personPath-", limit: "0.11 kB" },
  // Measured 88 B (quoting a command line to copy, which Connect a repository and the Health and
  // Access tabs share, 2026-11-13) + 10 %.
  { name: "quoting a command", prefix: "shellQuote-", limit: "0.1 kB" },
  // Measured 789 B (React Aria's text field, shared by the Catalog's search and the screens with
  // a text field; a chunk of its own since the table page's route, 2026-11-13) + 10 %.
  // Raised to the measured 1.04 kB (2026-11-16) + 10 %: React Aria's text area moved into this
  // chunk, because the Undrop dialog's reason field shares it with the table page's editor.
  { name: "the text field", prefix: "TextField-", limit: "1.15 kB" },
  // Measured 180 B (a date and time in the person's language; the Catalog's list and the table
  // page share it, 2026-11-13) + 10 %.
  { name: "dates and times", prefix: "useWhen-", limit: "0.2 kB" },
  // Measured 239 B (focus after a control removes itself; Settings' Access pages (People, Invites,
  // Recovery links) and the table page share it, 2026-11-13) + 10 %.
  { name: "focus after a change", prefix: "useFocusAfter-", limit: "0.27 kB" },
  // Measured 357 B (reading and reading again, now with the typed failure, 2026-11-13) + 10 %.
  { name: "reading for a screen", prefix: "useResource-", limit: "0.39 kB" },
];

/**
 * The size check of one chunk loaded on demand: its own limit when it is named, else the shared
 * one.
 *
 * @param {string} file - The chunk's file name.
 * @returns {{name: string, path: string, gzip: boolean, limit: string}} The check.
 */
function onDemandCheck(file) {
  const named = NAMED_LIMITS.find((entry) => file.startsWith(entry.prefix));
  return {
    name:
      named === undefined ? `JavaScript, on demand: ${file}` : `JavaScript, ${named.name}: ${file}`,
    path: `${ASSETS}/${file}`,
    gzip: true,
    limit: named === undefined ? ON_DEMAND_LIMIT : named.limit,
  };
}

/**
 * The JavaScript files a first visit loads: those index.html names.
 *
 * @returns {string[]} Their file names; the entry pattern before the first build.
 */
function firstLoad() {
  try {
    const page = readFileSync(join(HERE, ASSETS, "../index.html"), "utf8");
    return [...page.matchAll(/\/assets\/([^"]+\.js)"/g)].map((match) => String(match[1]));
  } catch {
    return ["index-*.js"];
  }
}

/**
 * The JavaScript chunks a first visit does not load.
 *
 * @param {string[]} initial - The files of the first load.
 * @returns {string[]} Their file names; none before the first build.
 */
function onDemandChunks(initial) {
  try {
    return readdirSync(join(HERE, ASSETS)).filter(
      (name) => name.endsWith(".js") && !initial.includes(name),
    );
  } catch {
    return [];
  }
}

const INITIAL = firstLoad();

export default [
  // The table page's strings load with its chunk (src/catalog/table/tableStrings.ts), which took the
  // first load from 209.3 to 207.5 kB (2026-11-13); the limit stays at 212 kB so that the
  // strings of Lineage, Health and Access, which load there too, do not use it up. With the Health
  // and Access tabs it measures 208.3 kB (2026-11-13), with the copy line and the signed-in
  // person, which the tabs share with the first load, in chunks of their own that index.html
  // preloads. With a table's Actions menu it measured 210.13 kB (2026-11-16): the menu's chunk
  // imports react-router's useNavigate, which split the router's chunk of the first load into
  // two (hooks- and lib-), and the exports between them cost 1.55 kB. vite.config.ts keeps
  // react-router in one chunk (codeSplitting.groups); it measures 208.57 kB, and the limit holds.
  // The first load depends on vite.config.ts's `shell` chunk group, which names a file inside
  // react-aria-components (dist/private/Button.mjs); build/chunk-groups.test.ts fails if an update
  // moves it, since the group would stop matching and this limit might not trip.
  // Measured 210.29 kB (2026-11-17) with the Queries area's loader in the routes (210.05 kB
  // before); CodeMirror stays in its own chunk, and the limit holds.
  // Measured 210.93 kB (2026-11-17) with the Queries screen: React Aria's form validation left
  // the text field's chunk for one of its own once the environment picker's Select shared it
  // (+0.39 kB), and the entry names the screen's new shared chunks and keeps the focus on Run
  // after its move to the run's address; the editor stays out of it, and the limit holds.
  // Measured 211.31 kB (2026-11-17, 210.99 kB before): the SQL editor's completion reads the
  // catalog through api/catalog.ts and api/table.ts, so the bundler moved the API client
  // (api/client.ts) out of the messages' chunk into one of its own that index.html preloads, and
  // the exports between them cost 0.32 kB. No editor or formatter code is in it; the limit holds.
  // Measured 211.01 kB (2026-11-17) with Saved, Recent, Save… and Delete…, all on demand; the
  // limit holds.
  // Measured 210.38 kB (2026-11-17; 209.84 kB on main in the same build): two splits undone. The
  // SQL editor no longer imports the API: the Queries screen hands it the catalog, so the API
  // client is back in the messages' chunk (-0.32 kB). vite.config.ts keeps React's JSX runtime in
  // the router's chunk, which the editor's chunk (JSX without react-i18next) had split off into a
  // chunk of its own (-0.17 kB). What is left is the Queries area's loader with the list of chunks
  // it preloads, the focus kept on Run after its move, and the download's type check in the API
  // client.
  // Measured 209.93 kB (2026-11-18; 209.8 kB on main in the same build) with the Connections
  // area's loader and the list of chunks it preloads (+0.12 kB); the limit holds. The
  // environment picker's Select, which the Connections list imports, would split React Aria's
  // form code in two as the Jobs screen's menus would; vite.config.ts's `form` chunk group keeps
  // it in one chunk.
  // Measured 192.8 kB (Help in the sidebar, its two commands in the palette, and the strings of
  // Help, the shortcuts and the tour, which en.json ships in the entry, 2026-11-11) + 10 %. Raised
  // from 192 kB: the tour and the shortcuts already load on demand; what is left is drawn on the
  // first paint or is a string of en.json, and moving the tour's strings out alone would not fit.
  {
    name: "JavaScript, first load",
    path: INITIAL.map((name) => `${ASSETS}/${name}`),
    gzip: true,
    limit: "212 kB",
  },
  ...onDemandChunks(INITIAL).map(onDemandCheck),
  // Raised from 8.03 kB: measured 7.42 kB with the Health and Access tabs' rules (their sections,
  // lists whose rows open nothing, a glyph beside its word, policy expressions in the monospace
  // face, the maintenance facts, and the table page's tabs wrapping at 320 px) (2026-11-13)
  // + 10 %.
  // Raised from 7.21 kB: measured 7.3 kB with the Lineage tab's rules (the step buttons, the
  // drawing's scrolling region, its boxes, lines and marks in light, dark and forced colours, the
  // full-screen view and the nested list) and Overview's heading row with Lineage ›
  // (2026-11-13) + 10 %.
  // Raised from 6.62 kB: measured 6.55 kB with the Overview's layout, facts, health lines and
  // columns table (2026-11-13) + 10 %.
  // Measured 6.02 kB (the Catalog's rules: the tree, the search, the bar, the scrolling table
  // region, the sorting headers, the health word and the skeleton rows, 2026-11-13) + 10 %.
  // Raised from 5.13 kB (4.66 kB measured with the profile pictures' circle, file field and
  // People cell, 2026-11-11, + 10 %: the circle is drawn on the first paint, so its rules cannot
  // wait for the dialog that loads on demand).
  // Raised from 8.16 kB: measured 8.26 kB with the shared data grid (dense rows, sticky header and
  // row-number gutter), the full-screen lineage that fills the window and Health's no-break rules
  // (2026-11-14) + 10 %.
  // Measured 8.43 kB (2026-11-16) with the menu's separator and the note under a menu's items;
  // the limit holds.
  // Raised to the measured 9.2 kB (2026-11-17) + 10 %: the Queries screen's rules (the toolbar
  // and the result's line that wrap at 320 px, the editor's resizable host and its placeholder,
  // the protected mark in the colour of danger and the alert's layout), and the shared grid's
  // rules in a file of their own with the grid's chunk.
  // Measured 9.32 kB (2026-11-17) with the library beside the editor, its lists and the popover
  // of a narrow screen; the list frame's rules moved from the Catalog's stylesheets into
  // screen.css, so they apply wherever the frame is used. The limit holds.
  // Raised to the measured 10.57 kB (2026-11-18) + 10 %: the Connections screen's rules (the
  // list's narrow columns, a status's glyph beside its words, Test all's row, a connection's
  // page with its facts and fingerprints, the form's single column, the secret's group and the
  // tcp key rows wrapping at 320 px) on top of Settings' frame (9.85 kB on main).
  // Raised to the measured 10.59 kB (2026-11-18) + 10 %: the Jobs screen's stylesheet (0.77 kB:
  // the job's facts, the runs list and its filters, a run's tasks with their failure lines, the
  // attempts and the log that wraps at 320 px), loaded with the Jobs screen only.
  // Raised to the measured 11.4 kB (2026-11-18) + 10 %: the Connections and the Jobs screens'
  // stylesheets together (10.67 kB on main), each loaded with its screen only; the first load's
  // index stylesheet is the same as main's.
  { name: "CSS", path: `${ASSETS}/*.css`, gzip: true, limit: "12.54 kB" },
  { name: "Fonts", path: `${ASSETS}/*.woff2`, gzip: false, limit: "200 kB" },
];
