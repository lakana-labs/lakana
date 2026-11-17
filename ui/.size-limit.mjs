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
// Measured 2.96 kB (the Access screen, 2026-11-13: its chunk was 1 B over the earlier 2.96 kB
// limit once the Catalog's shared chunks changed the minifier's names) + 10 %.
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
  { name: "Catalog", prefix: "CatalogScreen-", limit: "32.6 kB" },
  // Measured 541 B (2026-11-17) + 10 %: the Queries screen, for now its heading and the lazy
  // import of the SQL editor with the screen's strings.
  { name: "Queries", prefix: "QueriesScreen-", limit: "0.6 kB" },
  // Measured 125.08 kB (2026-11-17) + 10 %: the SQL editor, almost all of it CodeMirror 6 (state,
  // view, commands, language, lang-sql's grammar and keywords, autocomplete, lint) with the
  // Still Water theme, the run keys, the error mark and the sources' registry. It loads only when
  // the Queries screen opens, never in the first load.
  { name: "the SQL editor", prefix: "SqlEditor-", limit: "137.6 kB" },
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
  { name: "the reason and confirm row", prefix: "ProtectedReason-", limit: "2.1 kB" },
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
  { name: "a table's page", prefix: "TablePage-", limit: "10.97 kB" },
  // Measured 21.97 kB (2026-11-13) + 10 %: the Preview tab, almost all of it React Aria's Table
  // (the grid with one tab stop and the arrow keys inside), with the run's polling and cancelling,
  // the cells as text and the Value panel. It loads only when the Preview tab is opened.
  // Measured 22.73 kB (2026-11-17) with the grid in shell/ResultGrid.tsx, which the Queries screen
  // shares and which the bundler leaves in this chunk (22.55 kB before, with grid.en.json now in
  // it); the limit holds.
  { name: "the Preview tab", prefix: "PreviewTab-", limit: "24.17 kB" },
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
  { name: "the catalog's addresses", prefix: "paths-", limit: "1.09 kB" },
  // Measured 95 B (a person's address, which the Access area and the Health and Access tabs
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
  // Measured 239 B (focus after a control removes itself; the Access screens and the table page
  // share it, 2026-11-13) + 10 %.
  { name: "focus after a change", prefix: "useFocusAfter-", limit: "0.27 kB" },
  // Measured 357 B (reading and reading again, now with the typed failure, 2026-11-13) + 10 %.
  { name: "reading for a screen", prefix: "useResource-", limit: "0.39 kB" },
  // Measured 219 B (the row of tabs; the router's link code it shared moved into the first load,
  // 2026-11-13) + 10 %.
  // Raised to the measured 1.11 kB (2026-11-13) + 10 %: React Aria's text area and the form
  // parts the table page's editor shares with the text field moved into this chunk.
  { name: "the tabs", prefix: "SectionTabs-", limit: "1.22 kB" },
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
  // Measured 210.29 kB (2026-11-17) with the Queries area's loader in the routes (210.05 kB
  // before); CodeMirror stays in its own chunk, and the limit holds.
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
  { name: "CSS", path: `${ASSETS}/*.css`, gzip: true, limit: "9.09 kB" },
  { name: "Fonts", path: `${ASSETS}/*.woff2`, gzip: false, limit: "200 kB" },
];
