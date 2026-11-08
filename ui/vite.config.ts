import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

import { browserTargets } from "./build/targets.ts";
import { CONTENT_SECURITY_POLICY } from "./csp.ts";
import packageJson from "./package.json" with { type: "json" };

const targets = browserTargets(packageJson.browserslist);

export default defineConfig({
  base: "/",
  plugins: [react()],
  build: {
    // Inside the server package: the wheel ships the interface (git-ignored, built in CI).
    outDir: "../packages/lakana-server/src/lakana_server/ui_dist",
    emptyOutDir: true,
    target: targets,
    // Vite hands this to Lightning CSS, which lowers the CSS for the same browsers.
    cssTarget: targets,
    // The content security policy allows no data: URL, so no asset is inlined as one.
    assetsInlineLimit: 0,
  },
  preview: {
    host: "127.0.0.1",
    headers: { "Content-Security-Policy": CONTENT_SECURITY_POLICY },
  },
});
