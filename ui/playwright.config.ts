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
    { name: "webkit", use: { ...devices["Desktop Safari"] } },
  ],
  // The production build, served with the content security policy (vite.config.ts).
  webServer: { command: "npm run preview -- --port 4173 --strictPort", url: URL },
});
