// Runs the visual regression suite (playwright.visual.config.ts) inside the pinned Playwright
// image, the same one locally and in CI, so the screenshots are drawn with the same fonts and the
// same renderer everywhere (docs/agent/testing.md, "Visual regression"). Its arguments go to
// `playwright test`: `npm --prefix ui run visual:update` passes `--update-snapshots=changed`.
// It needs Docker and a build (`npm --prefix ui run build`).
//
// The dependencies come from `npm ci` inside the image, kept in a Docker volume named after the
// lock's hash, never the host's node_modules: on macOS those hold darwin binaries the Linux
// container cannot load.
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

/**
 * The image, by the digest of its linux/amd64 manifest: its tag is the version of
 * `@playwright/test` in package.json, which `scripts/tests/test_workflows.py` holds equal. The
 * amd64 build is the one CI's runners draw with; an arm64 machine runs it emulated (PLATFORM),
 * since the arm64 build's Chromium draws other pixels.
 */
export const IMAGE =
  "mcr.microsoft.com/playwright:v1.65.1-noble@sha256:766137e2d9caad4744187e53cdc89320ba6f2eb57cff097374020944b44dd0a9";

/** The platform of IMAGE, asked for explicitly on every host. */
const PLATFORM = "linux/amd64";

/** The memory the container may use, swap included: a few Chromium pages and the preview. */
const MEMORY = "6g";

/** The label of the dependency volumes, for `docker volume ls --filter label=…`. */
const LABEL = "lakana.ui-visual";

/** The file `npm ci` leaves in a volume once it finished; a volume without it is installed again. */
const INSTALLED = ".lakana-ui-visual-installed";

const ui = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const root = resolve(ui, "..");
const build = resolve(root, "packages/lakana-server/src/lakana_server/ui_dist/index.html");

/**
 * Runs a command, its output shown.
 *
 * @param {string} command - The program.
 * @param {string[]} args - Its arguments.
 * @returns {number} Its exit status.
 */
function run(command, args) {
  const done = spawnSync(command, args, { stdio: "inherit" });
  if (done.error !== undefined) {
    console.error(`${command} did not start: ${done.error.message}`);
    process.exit(2);
  }
  return done.status ?? 1;
}

if (!existsSync(build)) {
  console.error("No build to test: run `npm --prefix ui run build` first.");
  process.exit(2);
}
// --mount takes comma-separated fields, so a comma in the path would split it.
if (root.includes(",")) {
  console.error(
    `The repository's path contains a comma, which docker --mount cannot take: ${root}`,
  );
  process.exit(2);
}

const lock = createHash("sha256")
  .update(readFileSync(resolve(ui, "package-lock.json")))
  .digest("hex");
const volume = `lakana-ui-visual-${lock.slice(0, 16)}`;
const repository = `--mount=type=bind,source=${root},target=/work`;
const modules = `--mount=type=volume,source=${volume},target=/work/ui/node_modules`;
const owner =
  typeof process.getuid === "function" && typeof process.getgid === "function"
    ? `${String(process.getuid())}:${String(process.getgid())}`
    : null;

// Install once per lock: as root (a new volume belongs to root), then hand it to the caller.
const probe = spawnSync("docker", ["volume", "inspect", volume], { stdio: "ignore" });
const marked =
  probe.status === 0 &&
  spawnSync(
    "docker",
    [
      "run",
      "--rm",
      `--platform=${PLATFORM}`,
      `--mount=type=volume,source=${volume},target=/m`,
      IMAGE,
      "test",
      "-f",
      `/m/${INSTALLED}`,
    ],
    { stdio: "ignore" },
  ).status === 0;
if (!marked) {
  console.log(`Installing the dependencies into the Docker volume ${volume} (once per lock).`);
  if (probe.status !== 0) {
    run("docker", ["volume", "create", `--label=${LABEL}`, volume]);
  }
  const install = [
    "npm ci --ignore-scripts --no-audit --no-fund",
    ...(owner === null ? [] : [`chown -R ${owner} node_modules`]),
    `touch node_modules/${INSTALLED}`,
  ].join(" && ");
  const status = run("docker", [
    "run",
    "--rm",
    `--platform=${PLATFORM}`,
    "--env=HOME=/tmp",
    "--env=npm_config_update_notifier=false",
    repository,
    modules,
    "--workdir=/work/ui",
    IMAGE,
    "sh",
    "-c",
    install,
  ]);
  if (status !== 0) {
    process.exit(status);
  }
}

const args = process.argv.slice(2);
const status = run("docker", [
  "run",
  "--rm",
  "--init",
  "--ipc=host",
  `--platform=${PLATFORM}`,
  `--memory=${MEMORY}`,
  `--memory-swap=${MEMORY}`,
  ...(owner === null ? [] : [`--user=${owner}`]),
  // The container's own HOME and temporary files stay inside the container.
  "--env=HOME=/tmp",
  ...(process.env["CI"] === undefined ? [] : ["--env=CI"]),
  repository,
  modules,
  "--workdir=/work/ui",
  IMAGE,
  "node",
  "node_modules/@playwright/test/cli.js",
  "test",
  "--config=playwright.visual.config.ts",
  ...args,
]);

// After an update, name the pictures it wrote, each to be looked at before the commit.
if (args.some((arg) => arg === "-u" || arg.startsWith("--update-snapshots"))) {
  console.log("\nBaselines written (look at each before you commit it):");
  run("git", ["-C", root, "status", "--short", "--", "ui/e2e/visual/screenshots"]);
}
process.exit(status);
