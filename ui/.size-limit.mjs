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
  { name: "Catalog", prefix: "CatalogScreen-", limit: "32.6 kB" },
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
  { name: "a table's page", prefix: "TablePage-", limit: "10.97 kB" },
  // Measured 21.97 kB (2026-11-13) + 10 %: the Preview tab, almost all of it React Aria's Table
  // (the grid with one tab stop and the arrow keys inside), with the run's polling and cancelling,
  // the cells as text and the Value panel. It loads only when the Preview tab is opened.
  { name: "the Preview tab", prefix: "PreviewTab-", limit: "24.17 kB" },
  // Measured 4.06 kB (2026-11-13) + 10 %: the Lineage tab, its layered layout of tables, assets
  // and connections, the drawing with its links, the list beside it, the steps kept in the
  // address, the full-screen view and its own strings. It loads only when the Lineage tab is
  // opened; the table's page stays at 10.32 kB with the tab's lazy import, inside its limit.
  { name: "the Lineage tab", prefix: "LineageTab-", limit: "4.47 kB" },
  // Measured 870 B (the catalog's addresses: the list, a table and its tabs; Home's links import
  // them, so the chunk is shared by Home and the Catalog, 2026-11-13) + 10 %.
  { name: "the catalog's addresses", prefix: "paths-", limit: "0.96 kB" },
  // Measured 789 B (React Aria's text field, shared by the Catalog's search and the screens with
  // a text field; a chunk of its own since the table page's route, 2026-11-13) + 10 %.
  { name: "the text field", prefix: "TextField-", limit: "0.87 kB" },
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
  // strings of Lineage, Health and Access, which load there too, do not use it up.
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
  { name: "CSS", path: `${ASSETS}/*.css`, gzip: true, limit: "8.03 kB" },
  { name: "Fonts", path: `${ASSETS}/*.woff2`, gzip: false, limit: "200 kB" },
];
