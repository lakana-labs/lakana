import { availableParallelism } from "node:os";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["src/test/setup.ts"],
    // Vitest's default, one worker per CPU but one, at most 8: 19 workers on a 20-thread machine
    // needed more than 4 GB for the coverage run (#586). CI's runners keep the default.
    maxWorkers: Math.min(8, Math.max(availableParallelism() - 1, 1)),
    // Only unit tests: Playwright owns e2e/*.spec.ts. scripts/ holds the lint ban's test.
    include: ["src/**/*.test.{ts,tsx}", "build/**/*.test.ts", "scripts/**/*.test.ts"],
    coverage: {
      provider: "v8",
      include: ["src/**/*.{ts,tsx}"],
      // main.tsx only mounts <App/>, like `if __name__ == "__main__"` in Python; App.tsx is
      // only the browser router's wiring (the routes are tested through appRoutes, the wiring
      // by the Playwright tests).
      exclude: ["src/**/*.test.{ts,tsx}", "src/test/**", "src/main.tsx", "src/App.tsx"],
      reporter: ["text-summary", "json-summary"],
      thresholds: { lines: 90, branches: 90, functions: 90, statements: 90 },
    },
  },
});
