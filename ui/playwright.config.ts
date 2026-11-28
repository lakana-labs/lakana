import { defineConfig, devices } from "@playwright/test";

const URL = "http://127.0.0.1:4173";

export default defineConfig({
  // Only e2e/: Vitest owns src/**/*.test.tsx.
  testDir: "e2e",
  outputDir: "test-results",
  retries: 0,
  forbidOnly: true,
  use: { baseURL: URL },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "firefox", use: { ...devices["Desktop Firefox"] } },
    // WebKit runs the suite's axe checks two to three times as slowly as Firefox: at 0.23.0 a dozen
    // tests took 20 to 29 s of the default 30 there, and under load some crossed it. A test that
    // hangs still fails, after 60 s.
    { name: "webkit", use: { ...devices["Desktop Safari"] }, timeout: 60_000 },
  ],
  // The production build, served with the content security policy (vite.config.ts).
  webServer: { command: "npm run preview -- --port 4173 --strictPort", url: URL },
});
