import { defineConfig, devices } from "@playwright/test";

const PORT = "4174";
const URL = `http://localhost:${PORT}`;
// Outside test-results/, which Playwright clears when a run starts. `uv run` runs at the
// repository root, so the database path is absolute.
const DATA = `${import.meta.dirname}/.e2e-server`;

export default defineConfig({
  testDir: "e2e-server",
  outputDir: "test-results",
  retries: 0,
  forbidOnly: true,
  // One server and one database for the whole run: the tests share it, and run in order.
  workers: 1,
  use: { baseURL: URL },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "firefox", use: { ...devices["Desktop Firefox"] } },
    { name: "webkit", use: { ...devices["Desktop Safari"] } },
  ],
  // The real lakana-server on a fresh SQLite database, serving the production build
  // (`npm run build` writes it into the server package).
  webServer: {
    command: `rm -rf '${DATA}' && mkdir -p '${DATA}' && uv run --frozen --python 3.14 lakana-server migrate && uv run --frozen --python 3.14 lakana-server`,
    cwd: "..",
    url: `${URL}/healthz`,
    timeout: 120_000,
    reuseExistingServer: false,
    env: {
      LAKANA_DATABASE__URL: `sqlite:///${DATA}/lakana.db`,
      LAKANA_UI__ENABLED: "true",
      LAKANA_PORT: PORT,
      LAKANA_UI__PUBLIC_URL: URL,
      // The local link of `lakana server start --local`, for the automatic sign-in's flow.
      LAKANA_UI__LOCAL_SIGN_IN: "true",
      LAKANA_BOOTSTRAP_PAT_KEY_ID: "e2ekey",
      LAKANA_BOOTSTRAP_PAT_SECRET: "e2esecret",
      LAKANA_SECRETS__MASTER_KEY: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    },
  },
});
