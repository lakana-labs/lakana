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
// Measured 2.69 kB (the Access screen, 2026-11-09) + 10 %.
const ON_DEMAND_LIMIT = "2.96 kB";
// Screens larger than ON_DEMAND_LIMIT, and the tour (held to its own, tighter one), each with a
// limit of its own; a chunk's file name is the module's name, a dash and a hash, so the start of
// the name finds it in every build.
const NAMED_LIMITS = [
  // Measured 3.91 kB (Home: the status box, Needs your attention, Recently opened, Your work,
  // Get started and the update note, after review, 2026-11-11) + 10 %.
  { name: "Home", prefix: "HomeScreen-", limit: "4.3 kB" },
  // Measured 1.79 kB (the tour: the welcome and its steps, 2026-11-11) + 10 %.
  { name: "the tour", prefix: "Tour-", limit: "1.97 kB" },
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
  // Measured 4.66 kB (the profile pictures' circle, file field and People cell, 2026-11-11)
  // + 10 %. Raised from 4.47 kB: the circle is drawn on the first paint (the user menu's
  // button), so its rules cannot wait for the dialog that loads on demand.
  { name: "CSS", path: `${ASSETS}/*.css`, gzip: true, limit: "5.13 kB" },
  { name: "Fonts", path: `${ASSETS}/*.woff2`, gzip: false, limit: "200 kB" },
];
