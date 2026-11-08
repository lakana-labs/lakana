import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

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
  },
  preview: {
    host: "127.0.0.1",
    headers: { "Content-Security-Policy": CONTENT_SECURITY_POLICY },
  },
});
