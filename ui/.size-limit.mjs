// The size limits of the built interface (docs/agent/testing.md, "The ui/ gates"), gzipped
// except the fonts. Each limit is a measurement + 10 %; a raised limit needs a sentence in its
// pull request. The entry chunk is what every first visit loads; each screen loaded on demand
// (src/shell/routes.tsx, AreaPages) is its own chunk with its own check, listed from the build.
import { readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ASSETS = "../packages/lakana-server/src/lakana_server/ui_dist/assets";
const ENTRY = /^index-.*\.js$/;
// No screen is loaded on demand yet: the first one fails here until its measured limit is set.
const ON_DEMAND_LIMIT = "0 B";

/**
 * The JavaScript chunks other than the entry chunk.
 *
 * @returns {string[]} Their file names; none before the first build.
 */
function onDemandChunks() {
  try {
    return readdirSync(join(dirname(fileURLToPath(import.meta.url)), ASSETS)).filter(
      (name) => name.endsWith(".js") && !ENTRY.test(name),
    );
  } catch {
    return [];
  }
}

export default [
  { name: "JavaScript, entry chunk", path: `${ASSETS}/index-*.js`, gzip: true, limit: "192 kB" },
  ...onDemandChunks().map((name) => ({
    name: `JavaScript, on demand: ${name}`,
    path: `${ASSETS}/${name}`,
    gzip: true,
    limit: ON_DEMAND_LIMIT,
  })),
  { name: "CSS", path: `${ASSETS}/*.css`, gzip: true, limit: "3.1 kB" },
  { name: "Fonts", path: `${ASSETS}/*.woff2`, gzip: false, limit: "200 kB" },
];
