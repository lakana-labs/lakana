// Runs the visual regression suite (playwright.visual.config.ts) inside the pinned Playwright
// image, the same one locally and in CI, so the screenshots are drawn with the same fonts and the
// same renderer everywhere (docs/agent/testing.md, "Visual regression"). Its arguments go to
// `playwright test`: `npm --prefix ui run visual:update` passes `--update-snapshots=changed`.
// It needs Docker and a build (`npm --prefix ui run build`).
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

/**
 * The image: its tag is the version of `@playwright/test` in package.json, which
 * `scripts/tests/test_workflows.py` holds equal, and its digest pins the exact build.
 */
export const IMAGE =
  "mcr.microsoft.com/playwright:v1.65.1-noble@sha256:1507cbfc550a63a6b7396791ca4f00df17beb56342d2447b3f106c306f525579";

/** The memory the container may use: a few Chromium pages and the preview server. */
const MEMORY = "6g";

const ui = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const root = resolve(ui, "..");
const build = resolve(root, "packages/lakana-server/src/lakana_server/ui_dist/index.html");

if (!existsSync(build)) {
  console.error("No build to test: run `npm --prefix ui run build` first.");
  process.exit(2);
}

const user =
  typeof process.getuid === "function" && typeof process.getgid === "function"
    ? ["--user", `${String(process.getuid())}:${String(process.getgid())}`]
    : [];

const done = spawnSync(
  "docker",
  [
    "run",
    "--rm",
    "--init",
    "--ipc=host",
    `--memory=${MEMORY}`,
    ...user,
    // The container's own HOME and temporary files stay inside the container.
    "--env=HOME=/tmp",
    ...(process.env["CI"] === undefined ? [] : ["--env=CI"]),
    `--volume=${root}:/work`,
    "--workdir=/work/ui",
    IMAGE,
    "node",
    "node_modules/@playwright/test/cli.js",
    "test",
    "--config=playwright.visual.config.ts",
    ...process.argv.slice(2),
  ],
  { stdio: "inherit" },
);

if (done.error !== undefined) {
  console.error(`Docker did not start: ${done.error.message}`);
  process.exit(2);
}
process.exit(done.status ?? 1);
