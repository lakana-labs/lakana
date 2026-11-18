import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

import { FORM_CHUNK, SHELL_CHUNK } from "./build/chunk-groups.ts";
import { cspFile } from "./build/csp-file.ts";
import { browserTargets } from "./build/targets.ts";
import { CONTENT_SECURITY_POLICY } from "./csp.ts";
import packageJson from "./package.json" with { type: "json" };

const targets = browserTargets(packageJson.browserslist);

export default defineConfig({
  base: "/",
  // cspFile writes the policy into the build, for lakana-server to send (build/csp-file.ts).
  plugins: [react(), cspFile()],
  build: {
    // Inside the server package: the wheel ships the interface (git-ignored, built in CI).
    outDir: "../packages/lakana-server/src/lakana_server/ui_dist",
    emptyOutDir: true,
    target: targets,
    // Vite hands this to Lightning CSS, which lowers the CSS for the same browsers.
    cssTarget: targets,
    // The content security policy allows no data: URL, so no asset is inlined as one.
    assetsInlineLimit: 0,
    // size-limit (.size-limit.mjs) is the gate on gzipped sizes; Vite's own warning on the raw
    // size would only repeat it on every build.
    chunkSizeWarningLimit: 700,
    // The minifier strips the bundled packages' licence headers; this file carries their
    // notices instead (MIT and Apache-2.0 ask for them in every copy), and lakana-server serves
    // it at /licenses/THIRD-PARTY-NOTICES.md (Vite's default, .vite/license.md, is not served).
    license: { fileName: "licenses/THIRD-PARTY-NOTICES.md" },
    rolldownOptions: {
      output: {
        // react-router stays in one chunk of the first load. A screen loaded on demand that
        // uses only some of its hooks (a table's Actions uses useNavigate) would otherwise split
        // it in two, and the exports between the parts cost about 1.5 kB gzipped there. React's
        // JSX runtime goes with it: the SQL editor's chunk renders JSX without react-i18next, and
        // would otherwise split the runtime off into a chunk of its own (0.17 kB more there).
        // The shell's Page and not-found state and React Aria's Button stay in one chunk of the
        // first load, with what they import. Settings' places, each loaded on its own, would
        // otherwise split them, React Aria's Link and its press and hover hooks into chunks of
        // their own, and the exports between them cost about 1.5 kB.
        // React Aria's form code stays in one chunk of the first load too. The Select menus that
        // the Connections, Queries and Jobs screens load on demand use its validation but not its
        // text field, and would otherwise split it in two (about 0.39 kB more).
        // The patterns (build/chunk-groups.ts) name files inside React Aria's packages; their test
        // fails if an update moves one.
        codeSplitting: {
          groups: [
            {
              name: "lib",
              test: /node_modules[\\/](react-router[\\/]|react[\\/](jsx-runtime|cjs[\\/]react-jsx-runtime))/,
            },
            {
              name: "shell",
              test: SHELL_CHUNK,
            },
            {
              name: "form",
              test: FORM_CHUNK,
            },
          ],
        },
      },
    },
  },
  preview: {
    host: "127.0.0.1",
    headers: { "Content-Security-Policy": CONTENT_SECURITY_POLICY },
  },
});
