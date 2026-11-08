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
  {
    name: "JavaScript, first load",
    path: INITIAL.map((name) => `${ASSETS}/${name}`),
    gzip: true,
    limit: "192 kB",
  },
  ...onDemandChunks(INITIAL).map((name) => ({
    name: `JavaScript, on demand: ${name}`,
    path: `${ASSETS}/${name}`,
    gzip: true,
    limit: ON_DEMAND_LIMIT,
  })),
  // Measured 3.43 kB (the Access and Settings screens, 2026-11-09) + 10 %.
  { name: "CSS", path: `${ASSETS}/*.css`, gzip: true, limit: "3.8 kB" },
  { name: "Fonts", path: `${ASSETS}/*.woff2`, gzip: false, limit: "200 kB" },
];
